import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, consumable_lots, days_until, expire_lots, is_expired
from app.modules import temp_zone

app = FastAPI(title="Pantryfifo", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    # 只读 lots(on_shelf): 暂存中的批不在这里,全层竖列与层页都看不到。
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    # 顶条与下架、消费共用同一过期判定(is_expired: expiry < today)。
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if is_expired(r, today):
            r["level"] = "expired"
            out.append(r)
        else:
            delta = days_until(r, today)
            if delta is not None and delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    # 入库先落暂存,不写 lots;确认落层才上架。
    c = connect()
    r = temp_zone.stage(c, body.item_id, body.qty, body.expiry)
    if not r["ok"]:
        c.close(); raise HTTPException(404, r["reason"])
    c.commit(); c.close()
    return {"id": r["staging_id"], "status": "pending"}

@app.get("/api/staging")
def staging_list():
    c = connect(); rows = temp_zone.list_pending(c); c.close(); return rows

@app.post("/api/staging/{staging_id}/confirm")
def staging_confirm(staging_id: int):
    # 确认落层: 与消费、下架同一写锁串行,要么整批上架可见,要么整体失败。
    c = connect(tx=True)
    try:
        r = temp_zone.confirm(c, staging_id)
        if not r["ok"]:
            c.rollback()
            raise HTTPException(400 if r["reason"] == "qty_non_positive" else 404, r["reason"])
        c.commit()
        return r
    finally:
        c.close()

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect(tx=True)
    try:
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf'", (body.item_id,))]
        # 一种世界: 过期批不可消费(只能下架),消费资格 = 未过期且剩余为正。
        eligible = consumable_lots(lots, date.today().isoformat())
        result = consume_fefo(eligible, body.qty)
        if not result["ok"] and result["reason"] == "qty_non_positive":
            c.rollback(); raise HTTPException(400, result["reason"])
        if not result["ok"]:
            c.rollback(); raise HTTPException(409, result)
        for d in result["deductions"]:
            c.execute(
                "UPDATE lots SET qty_remain = qty_remain - ? WHERE id=? AND status='on_shelf' AND qty_remain >= ?",
                (d["take"], d["lot_id"], d["take"]))
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        c.commit(); return result
    finally:
        c.close()

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect(tx=True)
    try:
        lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
        ids = expire_lots(lots, date.today().isoformat())
        for i in ids:
            # 条件更新: 只扫走上架中且剩余为正的批,与消费/落层互不留幽灵态。
            c.execute("UPDATE lots SET status='expired' WHERE id=? AND status='on_shelf' AND qty_remain>0", (i,))
        c.commit(); return {"expired_ids": ids}
    finally:
        c.close()

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
