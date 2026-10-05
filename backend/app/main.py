import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.modules.temp_zone import StagingError, confirm_staged, list_staged, stage_lot

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

# ---- 唯一的货架世界：lots 中 status='on_shelf' 且 qty_remain>0 ----
# 暂存行只存在于 staged_lots，以下任何视图、顶条、消费、下架扫描都看不到。
SHELF_WHERE = "lots.status='on_shelf' AND lots.qty_remain>0"
# 三处共用同一过期资格：expiry 早于今天。顶条标 expired、下架名单含它、
# FEFO 候选排除它（expiry 为空视为不过期）——三处永远是同一个世界。
CONSUMABLE_WHERE = f"{SHELF_WHERE} AND (expiry IS NULL OR expiry >= ?)"

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = f"""SELECT lots.*, items.name, items.layer, items.unit FROM lots
            JOIN items ON items.id=lots.item_id WHERE {SHELF_WHERE}"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    """顶条与过期下架共用同一条资格：expiry < 今天 才算过期。

    临期(soon)：今天 <= expiry <= today+warn_days。暂存批不在 lots 中，
    天然不参与，不可能被顶条当紧急而下架名单漏掉。
    """
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today()
    rows = [dict(r) for r in c.execute(
        f"""SELECT lots.*, items.name, items.layer FROM lots
            JOIN items ON items.id=lots.item_id
            WHERE {SHELF_WHERE} AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        exp = date.fromisoformat(r["expiry"])
        delta = (exp - today).days
        if exp < today:
            r["level"] = "expired"; out.append(r)
        elif delta <= warn:
            r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    # 入库只进暂存：确认落层前，全层竖列与层页都不得出现该批。
    c = connect()
    try:
        sid = stage_lot(c, body.item_id, body.qty, body.expiry)
    except StagingError as e:
        c.close(); raise HTTPException(e.http_status, e.reason)
    c.close()
    return {"staged_id": sid}

@app.get("/api/staged")
def staged():
    c = connect(); rows = list_staged(c); c.close(); return rows

class StagedConfirmIn(BaseModel):
    staged_id: int

@app.post("/api/staged/confirm")
def staged_confirm(body: StagedConfirmIn):
    c = connect()
    try:
        out = confirm_staged(c, body.staged_id)
    except StagingError as e:
        c.close(); raise HTTPException(e.http_status, e.reason)
    c.close()
    return out

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    # 读候选批与写扣减放进同一个 BEGIN IMMEDIATE：与落层/下架互斥，
    # 禁止「暂存已空、全层没有，回包却引用幽灵批」。
    c.execute("BEGIN IMMEDIATE")
    try:
        today_iso = date.today().isoformat()
        lots = [dict(r) for r in c.execute(
            f"SELECT * FROM lots WHERE item_id=? AND {CONSUMABLE_WHERE}",
            (body.item_id, today_iso))]
        result = consume_fefo(lots, body.qty)
        if not result["ok"] and result["reason"] == "qty_non_positive":
            c.execute("ROLLBACK"); c.close(); raise HTTPException(400, result["reason"])
        if not result["ok"]:
            c.execute("ROLLBACK"); c.close(); raise HTTPException(409, result)
        # 扣减前按 id 复核每个批仍在货架且余量充足，引用不到幽灵批。
        for d in result["deductions"]:
            row = c.execute(
                "SELECT qty_remain FROM lots WHERE id=? AND status='on_shelf'",
                (d["lot_id"],)).fetchone()
            if row is None or float(row["qty_remain"]) + 1e-9 < float(d["take"]):
                c.execute("ROLLBACK"); c.close()
                raise HTTPException(409, {"ok": False, "reason": "lot_changed",
                                          "deductions": [], "short": body.qty})
        for d in result["deductions"]:
            c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?",
                      (d["take"], d["lot_id"]))
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        c.execute("COMMIT")
    finally:
        c.close()
    return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    # 与顶条同一资格世界：expiry < 今天、且仍在货架有余量。
    c.execute("BEGIN IMMEDIATE")
    try:
        lots = [dict(r) for r in c.execute(
            f"SELECT * FROM lots WHERE {SHELF_WHERE}")]
        ids = expire_lots(lots, date.today().isoformat())
        for i in ids:
            c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
        c.execute("COMMIT")
    finally:
        c.close()
    return {"expired_ids": ids}

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
