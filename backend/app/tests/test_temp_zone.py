"""暂存落层端到端：暂存隐身、确认上架、三处资格同一世界、脏数据保护。"""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import connect


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(main.app) as c:
        yield c


def today_offset(days):
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


def lot_ids(rows):
    return {r["id"] for r in rows}


def test_staged_invisible_until_confirm(client):
    before_all = client.get("/api/fridge").json()
    before_upper = client.get("/api/fridge?layer=upper").json()

    r = client.post("/api/lots", json={"item_id": 1, "qty": 2, "expiry": today_offset(30)})
    assert r.status_code == 200
    sid = r.json()["staged_id"]

    # 暂存行存在，但全层竖列与上层页都不得出现
    assert [s["id"] for s in client.get("/api/staged").json()] == [sid]
    assert client.get("/api/fridge").json() == before_all
    assert client.get("/api/fridge?layer=upper").json() == before_upper

    # 确认落层
    out = client.post("/api/staged/confirm", json={"staged_id": sid}).json()
    lid = out["lot_id"]
    assert out["layer"] == "upper"

    # 全层与该层页看到的是同一新批
    all_rows = client.get("/api/fridge").json()
    upper_rows = client.get("/api/fridge?layer=upper").json()
    assert lid in lot_ids(all_rows)
    assert lid in lot_ids(upper_rows)
    new_all = next(r for r in all_rows if r["id"] == lid)
    new_up = next(r for r in upper_rows if r["id"] == lid)
    assert new_all == new_up
    assert new_all["qty_remain"] == 2
    # 暂存已清空
    assert client.get("/api/staged").json() == []


def test_expired_staged_one_world_across_alerts_consume_sweep(client):
    # 暂存一个到期日早于今天的批
    sid = client.post("/api/lots", json={"item_id": 1, "qty": 2,
                                        "expiry": today_offset(-1)}).json()["staged_id"]
    # 未确认前：顶条没有它，下架也扫不到它
    alerts_before = client.get("/api/alerts").json()
    swept_before = client.post("/api/expire-sweep").json()["expired_ids"]
    assert all(a.get("qty_remain") != 2 or a["expiry"] != today_offset(-1) for a in alerts_before)

    lid = client.post("/api/staged/confirm", json={"staged_id": sid}).json()["lot_id"]

    # 顶条：按过期标 expired
    alerts = client.get("/api/alerts").json()
    mine = next(a for a in alerts if a["id"] == lid)
    assert mine["level"] == "expired"

    # 按临期消费：过期批不得被扣到（货架上的种子牛奶也都已过期 -> short 409）
    r = client.post("/api/consume", json={"item_id": 1, "qty": 1})
    assert r.status_code == 409
    deductions = r.json().get("deductions", [])
    assert all(d["lot_id"] != lid for d in deductions)

    # 过期下架：名单必须有它（与顶条同一资格 expiry < 今天）
    swept = client.post("/api/expire-sweep").json()["expired_ids"]
    assert lid in swept

    # 下架后：全层消失、顶条消失
    assert lid not in lot_ids(client.get("/api/fridge").json())
    assert lid not in {a["id"] for a in client.get("/api/alerts").json()}


def test_today_expiry_is_soon_world_not_expired(client):
    # 到期日 == 今天：顶条是 soon（不是 expired），消费扣得到，下架不扫
    sid = client.post("/api/lots", json={"item_id": 2, "qty": 5,
                                        "expiry": today_offset(0)}).json()["staged_id"]
    lid = client.post("/api/staged/confirm", json={"staged_id": sid}).json()["lot_id"]
    a = next(x for x in client.get("/api/alerts").json() if x["id"] == lid)
    assert a["level"] == "soon" and a["days_left"] == 0
    assert lid not in client.post("/api/expire-sweep").json()["expired_ids"]
    r = client.post("/api/consume", json={"item_id": 2, "qty": 5})
    assert r.status_code == 200 and any(d["lot_id"] == lid for d in r.json()["deductions"])


def test_invalid_inbound_rejected(client):
    n_lots_before = len(client.get("/api/fridge").json())
    assert client.post("/api/lots", json={"item_id": 999, "qty": 1,
                                          "expiry": "2026-12-01"}).status_code == 404
    assert client.post("/api/lots", json={"item_id": 1, "qty": 0,
                                          "expiry": "2026-12-01"}).status_code == 400
    assert client.post("/api/lots", json={"item_id": 1, "qty": -3,
                                          "expiry": "2026-12-01"}).status_code == 400
    assert client.get("/api/staged").json() == []
    assert len(client.get("/api/fridge").json()) == n_lots_before


def test_invalid_confirm_does_not_add_lot(client, monkeypatch):
    # 直接塞一条数量非正的脏暂存行与一条品项不存在的暂存行
    c = connect()
    cur = c.execute("INSERT INTO staged_lots(item_id,qty,expiry,created_at) VALUES (1,0,'2026-12-01','')")
    bad_qty = cur.lastrowid
    cur2 = c.execute("INSERT INTO staged_lots(item_id,qty,expiry,created_at) VALUES (999,2,'2026-12-01','')")
    bad_item = cur2.lastrowid
    c.commit(); c.close()

    lots_before = lot_ids(client.get("/api/fridge").json())

    r1 = client.post("/api/staged/confirm", json={"staged_id": bad_qty})
    r2 = client.post("/api/staged/confirm", json={"staged_id": bad_item})
    r3 = client.post("/api/staged/confirm", json={"staged_id": 99999})
    assert r1.status_code == 400
    assert r2.status_code == 400
    assert r3.status_code == 404

    # lots 不增行；脏暂存行仍在（失败可排查），不会被静默上架
    assert lot_ids(client.get("/api/fridge").json()) == lots_before
    c = connect()
    kept = {r["id"] for r in c.execute(
        "SELECT id FROM staged_lots WHERE id IN (?,?)", (bad_qty, bad_item))}
    c.close()
    assert kept == {bad_qty, bad_item}
    # qty 非正的脏行仍可在暂存列表（JOIN 得到品项）中看到
    staged_ids = {s["id"] for s in client.get("/api/staged").json()}
    assert bad_qty in staged_ids


def test_dirty_seeds_not_cleaned_by_landing(client):
    # 种子：id=4 冻饺 dirty（已过期），id=5 鸡蛋 dirty（负数量）
    sid = client.post("/api/lots", json={"item_id": 3, "qty": 1,
                                        "expiry": today_offset(10)}).json()["staged_id"]
    client.post("/api/staged/confirm", json={"staged_id": sid})
    client.post("/api/expire-sweep")

    c = connect()
    dq4 = c.execute("SELECT data_quality,status FROM lots WHERE id=4").fetchone()
    dq5 = c.execute("SELECT data_quality,status,qty_remain FROM lots WHERE id=5").fetchone()
    c.close()
    assert dict(dq4)["data_quality"] == "dirty"   # 下架可改 status，不得洗成 clean
    assert dict(dq5)["data_quality"] == "dirty"

    # 负数量鸡蛋：全层不可见、FEFO 扣不到
    fridge_ids = lot_ids(client.get("/api/fridge").json())
    assert 5 not in fridge_ids
    r = client.post("/api/consume", json={"item_id": 2, "qty": 100})
    assert all(d["lot_id"] != 5 for d in r.json().get("deductions", []))


def test_new_lot_deducted_fefo_after_landing(client):
    # 新批到期早于种子鸡蛋(2026-11-01)且在未来 -> FEFO 先扣新批
    sid = client.post("/api/lots", json={"item_id": 2, "qty": 4,
                                        "expiry": "2026-10-20"}).json()["staged_id"]
    lid = client.post("/api/staged/confirm", json={"staged_id": sid}).json()["lot_id"]
    r = client.post("/api/consume", json={"item_id": 2, "qty": 3})
    assert r.status_code == 200
    assert r.json()["deductions"][0]["lot_id"] == lid
    row = next(x for x in client.get("/api/fridge").json() if x["id"] == lid)
    assert row["qty_remain"] == 1
