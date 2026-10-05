"""暂存落层（temp_zone）。

入库先进 staged_lots 暂存：此时货架世界（全层竖列 / 层页 / 顶条 /
FEFO 消费 / 过期下架扫描）都看不到该批。只有 confirm_staged 在一个
BEGIN IMMEDIATE 事务里把批写入 lots(status='on_shelf') 并删除暂存行，
全层与该层页才在同一时刻看到同一新批，不会出现半上架或幽灵批。
"""
from datetime import datetime, timezone


class StagingError(Exception):
    def __init__(self, reason: str, http_status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


def stage_lot(c, item_id, qty, expiry) -> int:
    """入库进暂存。品项不存在 / 数量非正直接拒绝，不落任何行。"""
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        raise StagingError("qty_non_positive", 400)
    if qty <= 0:
        raise StagingError("qty_non_positive", 400)
    item = c.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        raise StagingError("item_not_found", 404)
    cur = c.execute(
        "INSERT INTO staged_lots(item_id,qty,expiry,created_at) VALUES (?,?,?,?)",
        (item_id, qty, expiry, datetime.now(timezone.utc).isoformat()),
    )
    c.commit()
    return cur.lastrowid


def list_staged(c) -> list[dict]:
    """暂存列表（带品名/所在层/单位），仅供确认落层，不进任何货架视图。"""
    return [dict(r) for r in c.execute(
        """SELECT s.*, items.name, items.layer, items.unit
           FROM staged_lots s JOIN items ON items.id=s.item_id
           ORDER BY s.id""")]


def confirm_staged(c, staged_id: int) -> dict:
    """确认落层：暂存行 -> lots(on_shelf, clean)，同一事务内删暂存。

    暂存不存在 -> 404；数量非正或品项已不存在 -> 400 且 lots 绝不增行。
    返回 {"lot_id", "item_id", "layer"}，供前端指向全层/该层页同一新批。
    """
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute(
            "SELECT * FROM staged_lots WHERE id=?", (staged_id,)).fetchone()
        if not row:
            raise StagingError("not_staged", 404)
        item = c.execute(
            "SELECT id, layer FROM items WHERE id=?", (row["item_id"],)).fetchone()
        try:
            qty = float(row["qty"])
        except (TypeError, ValueError):
            qty = 0
        if not item or qty <= 0:
            # 脏暂存行：确认失败，保留原行供排查，lots 不增行。
            raise StagingError("invalid_staged", 400)
        cur = c.execute(
            """INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)
               VALUES (?,?,?,?,?,?)""",
            (row["item_id"], qty, qty, row["expiry"], "on_shelf", "clean"),
        )
        lot_id = cur.lastrowid
        c.execute("DELETE FROM staged_lots WHERE id=?", (staged_id,))
        c.execute("COMMIT")
        return {"lot_id": lot_id, "item_id": row["item_id"], "layer": item["layer"]}
    except StagingError:
        c.execute("ROLLBACK")
        raise
    except Exception:
        c.execute("ROLLBACK")
        raise
