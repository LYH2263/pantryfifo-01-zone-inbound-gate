"""暂存区 temp_zone: 入库先落暂存,确认落层才写入 lots 上架。

约定:
- 暂存中的批不在 lots 表里,因此全层竖列、层页、顶条、过期下架、
  按临期消费一律看不到它;确认落层才 INSERT 进 lots(status='on_shelf')。
- 确认是校验闸门: 品项不存在或数量非正 -> 失败且 lots 不增行;
  暂存已空(不存在或已确认) -> 失败,不会二次落层造出幽灵批。
- 确认只新增 lots 行,绝不回写既有批次(种子里的 dirty 批保持 dirty)。
- 调用方(端点)负责事务边界: 在 BEGIN IMMEDIATE 连接上调用 confirm,
  与消费、下架串行化,落层确认不会和消费/下架打到同一品项的中间态。
"""
from datetime import datetime, timezone

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS staging_lots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INT,
  qty REAL,
  expiry TEXT,
  status TEXT,
  created_at TEXT
);
"""


def ensure_table(c) -> None:
    """建表(幂等)。seed.init_db 每次启动调用,老库也能补上。"""
    c.executescript(TABLE_SQL)


def stage(c, item_id: int, qty: float, expiry: str) -> dict:
    """入库进暂存。品项不存在则拒绝(不入暂存)。数量留待确认落层时校验。"""
    item = c.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        return {"ok": False, "reason": "item_not_found"}
    cur = c.execute(
        "INSERT INTO staging_lots(item_id,qty,expiry,status,created_at) VALUES (?,?,?,?,?)",
        (item_id, qty, expiry, "pending", datetime.now(timezone.utc).isoformat()),
    )
    return {"ok": True, "staging_id": cur.lastrowid}


def list_pending(c) -> list[dict]:
    """暂存列表(待落层)。LEFT JOIN: 品项被删的暂存行也要看得见、报得出错。"""
    return [
        dict(r)
        for r in c.execute(
            """SELECT staging_lots.*, items.name, items.layer, items.unit
               FROM staging_lots
               LEFT JOIN items ON items.id = staging_lots.item_id
               WHERE staging_lots.status='pending'
               ORDER BY staging_lots.id"""
        )
    ]


def confirm(c, staging_id: int) -> dict:
    """确认落层: 校验通过后把暂存批写入 lots 并标记已确认。

    失败原因: staging_empty(暂存已空/不存在) / item_not_found / qty_non_positive。
    任何失败都不会让 lots 增行。
    """
    row = c.execute(
        "SELECT * FROM staging_lots WHERE id=?", (staging_id,)
    ).fetchone()
    if not row or row["status"] != "pending":
        return {"ok": False, "reason": "staging_empty"}
    item = c.execute("SELECT id FROM items WHERE id=?", (row["item_id"],)).fetchone()
    if not item:
        return {"ok": False, "reason": "item_not_found"}
    if float(row["qty"]) <= 0:
        return {"ok": False, "reason": "qty_non_positive"}
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
        " VALUES (?,?,?,?,?,?)",
        (row["item_id"], row["qty"], row["qty"], row["expiry"], "on_shelf", "clean"),
    )
    # WHERE status='pending' 兜底: 同事务内也不可能把同一暂存落层两次。
    c.execute(
        "UPDATE staging_lots SET status='confirmed' WHERE id=? AND status='pending'",
        (staging_id,),
    )
    return {"ok": True, "staging_id": staging_id, "lot_id": cur.lastrowid}
