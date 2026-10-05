"""FEFO consume: earliest expiry first among positive remaining lots.

一种世界资格约定(today 一律 date.today().isoformat()):
- 过期 <=> expiry 早于今天(expiry < today),全系统只有这一个判定。
- 顶条: expired 批次标 'expired',未过期且 days_left <= warn_days 标 'soon'。
- 过期下架: 扫走的正是 is_expired 的批。
- 按临期消费: 只有未过期的批可扣( consumable_lots )。
三处共用下方谓词,不会出现顶条当紧急、消费扣得到、下架名单却没有的批。
"""
from datetime import date


def is_expired(lot: dict, today: str) -> bool:
    """唯一过期判定: 有到期日且早于今天。"""
    exp = lot.get("expiry")
    return bool(exp) and exp < today


def days_until(lot: dict, today: str) -> int | None:
    """距到期天数; 无到期日返回 None。"""
    exp = lot.get("expiry")
    if not exp:
        return None
    return (date.fromisoformat(exp) - date.fromisoformat(today)).days


def consumable_lots(lots: list[dict], today: str) -> list[dict]:
    """按临期消费的资格集: 剩余为正且未过期(过期批只能下架,不可扣)。"""
    return [
        l for l in lots
        if float(l.get("qty_remain", 0)) > 0 and not is_expired(l, today)
    ]


def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return sorted(
        [l for l in lots if float(l.get("qty_remain", 0)) > 0],
        key=lambda l: (l.get("expiry") or "9999-99-99", l.get("id") or 0),
    )

def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Return deductions list and leftover demand. Mutates copies only."""
    need = float(qty)
    if need <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    ordered = sort_lots_fefo(lots)
    deductions = []
    for lot in ordered:
        if need <= 0:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, need)
        deductions.append({"lot_id": lot["id"], "take": take, "expiry": lot.get("expiry")})
        need -= take
    if need > 1e-9:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}

def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: remaining>0 and expiry < today."""
    out = []
    for l in lots:
        if is_expired(l, today) and float(l.get("qty_remain", 0)) > 0:
            out.append(l["id"])
    return out
