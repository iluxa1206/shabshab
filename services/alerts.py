"""Алерты по стакану, per-user. Хранилище — portfolio.db (таблица alerts, v3).
Мониторинг — фоновый воркер (api.main.alerts_monitor) против Alor-стакана:
для каждого активного алерта проверяет уровни нужной стороны, при выполнении
условия по метрике + накопленному объёму переводит active→fired."""
import logging
from datetime import datetime, timezone
from typing import Optional, List

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

METRICS = {"price", "ytm", "dm", "gspread"}
SIDES = {"buy", "sell"}
OPS = {"<=", ">="}
UNITS = {"bonds", "rub"}

_COLS = ("user_email", "isin", "kind", "side", "metric", "op", "threshold",
         "min_volume", "volume_unit", "note", "status", "created_at",
         "fired_at", "fired_price", "fired_volume")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AlertError(ValueError):
    pass


def create(user_email: str, *, isin: str, side: str, metric: str, op: str,
           threshold: float, min_volume: float = 0.0, volume_unit: str = "bonds",
           kind: str = "floater", note: Optional[str] = None) -> dict:
    isin = (isin or "").strip().upper()
    if not isin:
        raise AlertError("Не указан ISIN")
    if side not in SIDES:
        raise AlertError("side: buy|sell")
    if metric not in METRICS:
        raise AlertError("metric: price|ytm|dm|gspread")
    if op not in OPS:
        raise AlertError("op: <= | >=")
    if volume_unit not in UNITS:
        raise AlertError("volume_unit: bonds|rub")
    try:
        threshold = float(threshold)
        min_volume = float(min_volume or 0)
    except (TypeError, ValueError):
        raise AlertError("threshold/min_volume должны быть числом")
    if min_volume < 0:
        raise AlertError("min_volume ≥ 0")
    with _lock, _connect() as c:
        cur = c.execute(
            "INSERT INTO alerts(user_email,isin,kind,side,metric,op,threshold,"
            "min_volume,volume_unit,note,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?, 'active', ?)",
            (user_email, isin, kind, side, metric, op, threshold,
             min_volume, volume_unit, note, _now()))
        aid = cur.lastrowid
    return get(aid)


def get(aid: int) -> Optional[dict]:
    with _connect() as c:
        r = c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None


def list_for_user(user_email: str) -> List[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM alerts WHERE user_email=? "
            "ORDER BY (status='active') DESC, "
            "COALESCE(fired_at, created_at) DESC", (user_email,)).fetchall()
    return [dict(r) for r in rows]


def cancel(user_email: str, aid: int) -> bool:
    """Отмена активного алерта (soft: status=cancelled)."""
    with _lock, _connect() as c:
        cur = c.execute("UPDATE alerts SET status='cancelled' "
                        "WHERE id=? AND user_email=? AND status='active'",
                        (aid, user_email))
        return cur.rowcount > 0


def delete(user_email: str, aid: int) -> bool:
    """Полное удаление (для сработавших/отменённых — чистка истории)."""
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM alerts WHERE id=? AND user_email=?",
                        (aid, user_email))
        return cur.rowcount > 0


def active_all() -> List[dict]:
    """Все активные алерты всех юзеров — для монитора."""
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM alerts WHERE status='active'").fetchall()]


def mark_fired(aid: int, price: float, volume: float) -> bool:
    with _lock, _connect() as c:
        cur = c.execute(
            "UPDATE alerts SET status='fired', fired_at=?, fired_price=?, fired_volume=? "
            "WHERE id=? AND status='active'", (_now(), price, volume, aid))
        return cur.rowcount > 0


# ─────────────────────────── матчинг стакана ───────────────────────────

def _metric_val(level: dict, metric: str) -> Optional[float]:
    if metric == "price":
        return level.get("price")
    if metric == "ytm":
        return level.get("yield_pct")
    if metric == "dm":
        return level.get("dm_bps")
    if metric == "gspread":
        return level.get("g_spread_bps")
    return None


def evaluate(alert: dict, levels: List[dict], face: Optional[float]) -> Optional[dict]:
    """levels — уровни нужной стороны (для buy: asks по возрастанию цены; для
    sell: bids по убыванию — «лучшие» первыми), каждый {price, qty, yield_pct,
    dm_bps, g_spread_bps}. Условие: набрать min_volume на уровнях, где
    metric op threshold («на этом уровне/лучше»). Возвращает {price, volume} при
    выполнении, иначе None. price = цена лучшего сматчившего уровня."""
    op, thr, metric = alert["op"], alert["threshold"], alert["metric"]
    unit, min_vol = alert["volume_unit"], alert.get("min_volume") or 0.0
    ok = (lambda v: v <= thr) if op == "<=" else (lambda v: v >= thr)

    matched = []
    for lv in levels:
        mv = _metric_val(lv, metric)
        if mv is not None and ok(mv):
            matched.append(lv)
    if not matched:
        return None

    if unit == "rub":
        vol = sum((lv.get("qty") or 0) * (face or 0) * ((lv.get("price") or 0) / 100.0)
                  for lv in matched)
    else:
        vol = sum(lv.get("qty") or 0 for lv in matched)

    if vol + 1e-9 >= min_vol:
        return {"price": matched[0].get("price"), "volume": round(vol, 2)}
    return None
