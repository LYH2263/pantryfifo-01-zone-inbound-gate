"""暂存落层 + 一种世界资格测试。

只依赖 stdlib(不 import fastapi),与端点同用一份模块/引擎逻辑。
"""
import pytest

from app import seed
from app.db import connect
from app.engines.fefo import consumable_lots, expire_lots, is_expired
from app.modules import temp_zone

TODAY = "2026-10-04"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    c = connect()
    yield c
    c.close()


def lots_rows(c):
    return [dict(r) for r in c.execute("SELECT * FROM lots")]


def on_shelf(c):
    # 与 /api/fridge 同源: 全层竖列与层页只读 lots.on_shelf。
    return [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]


def test_inbound_goes_to_staging_not_on_shelf(db):
    before = len(lots_rows(db))
    r = temp_zone.stage(db, 1, 5, "2026-12-01")
    assert r["ok"]
    db.commit()
    # 暂存可见,但 lots 不增行 -> 全层竖列/层页/顶条/下架/消费都看不到该批。
    assert len(lots_rows(db)) == before
    pending = temp_zone.list_pending(db)
    assert len(pending) == 1 and pending[0]["name"] == "牛奶" and pending[0]["qty"] == 5


def test_confirm_writes_lot_visible_everywhere(db):
    sid = temp_zone.stage(db, 1, 5, "2026-12-01")["staging_id"]
    r = temp_zone.confirm(db, sid)
    db.commit()
    assert r["ok"] and r["lot_id"]
    lot = [l for l in on_shelf(db) if l["id"] == r["lot_id"]]
    assert len(lot) == 1
    assert lot[0]["qty_remain"] == 5 and lot[0]["expiry"] == "2026-12-01"
    assert lot[0]["data_quality"] == "clean"
    # 同一新批: 全层(无 layer 过滤)与层页(按 layer 过滤)看到的是同一 lot_id。
    all_layer = {l["id"] for l in on_shelf(db)}
    upper = {l["id"] for l in on_shelf(db) if l["item_id"] == 1}
    assert r["lot_id"] in all_layer and r["lot_id"] in upper
    # 暂存已空。
    assert temp_zone.list_pending(db) == []


def test_confirm_twice_is_staging_empty(db):
    sid = temp_zone.stage(db, 1, 5, "2026-12-01")["staging_id"]
    assert temp_zone.confirm(db, sid)["ok"]
    before = len(lots_rows(db))
    r = temp_zone.confirm(db, sid)
    assert r == {"ok": False, "reason": "staging_empty"}
    assert len(lots_rows(db)) == before  # 不二次落层、不产生幽灵批


def test_confirm_unknown_id(db):
    before = len(lots_rows(db))
    assert temp_zone.confirm(db, 9999) == {"ok": False, "reason": "staging_empty"}
    assert len(lots_rows(db)) == before


def test_confirm_qty_non_positive_fails(db):
    sid = temp_zone.stage(db, 1, 0, "2026-12-01")["staging_id"]
    sid2 = temp_zone.stage(db, 1, -2, "2026-12-01")["staging_id"]
    before = len(lots_rows(db))
    assert temp_zone.confirm(db, sid) == {"ok": False, "reason": "qty_non_positive"}
    assert temp_zone.confirm(db, sid2) == {"ok": False, "reason": "qty_non_positive"}
    assert len(lots_rows(db)) == before  # lots 不增行
    assert len(temp_zone.list_pending(db)) == 2  # 仍待处理,未被吞掉


def test_confirm_item_missing_fails(db):
    db.execute(
        "INSERT INTO staging_lots(item_id,qty,expiry,status,created_at) VALUES (?,?,?,?,?)",
        (9999, 3, "2026-12-01", "pending", "2026-10-04T00:00:00+00:00"))
    sid = db.execute("SELECT MAX(id) i FROM staging_lots").fetchone()["i"]
    before = len(lots_rows(db))
    assert temp_zone.confirm(db, sid) == {"ok": False, "reason": "item_not_found"}
    assert len(lots_rows(db)) == before


def test_stage_item_missing_rejected(db):
    assert temp_zone.stage(db, 9999, 1, "2026-12-01") == {"ok": False, "reason": "item_not_found"}
    assert temp_zone.list_pending(db) == []


def test_dirty_seeds_not_cleaned_by_confirm(db):
    sid = temp_zone.stage(db, 1, 1, "2026-12-01")["staging_id"]
    assert temp_zone.confirm(db, sid)["ok"]
    db.commit()
    dirty = {l["id"]: l for l in lots_rows(db) if l["data_quality"] == "dirty"}
    assert len(dirty) == 2
    dumpling = [l for l in dirty.values() if l["item_id"] == 3][0]
    egg = [l for l in dirty.values() if l["item_id"] == 2 and l["qty_remain"] < 0][0]
    assert dumpling["qty_remain"] == 1 and dumpling["expiry"] == "2025-01-01"
    assert egg["qty_remain"] == -3  # 负数量鸡蛋原样保留


def test_expired_staged_lot_one_world_after_confirm(db):
    # 暂存中已过期(到期日早于今天)的批: 确认落层后三处资格必须一致。
    before = len(lots_rows(db))
    sid = temp_zone.stage(db, 1, 7, "2026-10-02")["staging_id"]
    # 暂存期间: 不进 lots,三处都无从谈起。
    assert len(lots_rows(db)) == before
    lid = temp_zone.confirm(db, sid)["lot_id"]
    db.commit()
    lot = [l for l in lots_rows(db) if l["id"] == lid][0]
    # 顶条: 判 expired;过期下架: 必中;按临期消费: 不可扣。同一判定 is_expired。
    assert is_expired(lot, TODAY) is True
    assert lid in expire_lots(on_shelf(db), TODAY)
    assert lid not in {l["id"] for l in consumable_lots(on_shelf(db), TODAY)}


def test_fresh_staged_lot_consumable_not_swept(db):
    sid = temp_zone.stage(db, 1, 2, "2026-10-06")["staging_id"]
    lid = temp_zone.confirm(db, sid)["lot_id"]
    db.commit()
    shelf = on_shelf(db)
    assert lid in {l["id"] for l in consumable_lots(shelf, TODAY)}
    assert lid not in expire_lots(shelf, TODAY)
    assert not is_expired([l for l in shelf if l["id"] == lid][0], TODAY)


def test_seed_idempotent_and_staging_table_on_existing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    seed.init_db()  # 老库再启动: 补表但不重播种子、不动 dirty
    c = connect()
    assert len(lots_rows(c)) == 5
    assert len([l for l in lots_rows(c) if l["data_quality"] == "dirty"]) == 2
    c.execute("SELECT * FROM staging_lots")  # 表已存在
    c.close()
