"""Вкладка СИГНАЛЫ: фильтры скринера, привязанные к веб-аккаунту, и доставка
срабатываний в браузер (WS-пуш → тост, звук, системное уведомление).

Условия фильтра и прогон по рынку — общие с Telegram-ботом
(services/screener_core.py): вкладка и бот обязаны показывать одни и те же
цифры. Здесь — хранение (signal_filters/signal_hits), анти-спам и рассылка.

Цикл гоняет api.main.signals_worker в торговые часы."""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock
from services.screener_core import (RATINGS, FilterError, evaluate,  # noqa: F401
                                    market_snapshot, normalize_params)

logger = logging.getLogger(__name__)

HITS_LIMIT = 100            # длина ленты, отдаваемой вкладке
_MAX_FILTERS_PER_USER = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json"))
    for k in ("enabled", "sound", "desktop"):
        d[k] = bool(d[k])
    return d


# --- CRUD (per user_email) ---

def create(user_email: str, name: str, params: dict, *, cooldown_min: int = 60,
           sound: bool = True, desktop: bool = True) -> dict:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise FilterError("Название: 1–60 символов")
    cooldown_min = int(cooldown_min or 60)
    if not (1 <= cooldown_min <= 2880):
        raise FilterError("Пауза: 1–2880 минут")
    p = normalize_params(params)
    with _lock, _connect() as c:
        n = c.execute("SELECT COUNT(*) FROM signal_filters WHERE user_email=?",
                      (user_email,)).fetchone()[0]
        if n >= _MAX_FILTERS_PER_USER:
            raise FilterError(f"Больше {_MAX_FILTERS_PER_USER} фильтров не поддерживается")
        cur = c.execute(
            "INSERT INTO signal_filters(user_email,name,params_json,cooldown_min,"
            "sound,desktop,created_at) VALUES(?,?,?,?,?,?,?)",
            (user_email, name, json.dumps(p, ensure_ascii=False), cooldown_min,
             int(bool(sound)), int(bool(desktop)), _now()))
        fid = cur.lastrowid
    return get(fid)


def get(fid: int) -> Optional[dict]:
    with _connect() as c:
        r = c.execute("SELECT * FROM signal_filters WHERE id=?", (fid,)).fetchone()
    return _row(r) if r else None


def list_for_user(user_email: str) -> List[dict]:
    with _connect() as c:
        rows = c.execute("SELECT * FROM signal_filters WHERE user_email=? ORDER BY id",
                         (user_email,)).fetchall()
    return [_row(r) for r in rows]


def list_enabled() -> List[dict]:
    with _connect() as c:
        rows = c.execute("SELECT * FROM signal_filters WHERE enabled=1").fetchall()
    return [_row(r) for r in rows]


def update(user_email: str, fid: int, *, name: Optional[str] = None,
           enabled: Optional[bool] = None, params: Optional[dict] = None,
           cooldown_min: Optional[int] = None, sound: Optional[bool] = None,
           desktop: Optional[bool] = None) -> Optional[dict]:
    f = get(fid)
    if not f or f["user_email"] != user_email:
        return None
    sets, args = [], []
    if name is not None:
        name = name.strip()
        if not name or len(name) > 60:
            raise FilterError("Название: 1–60 символов")
        sets.append("name=?"); args.append(name)
    if enabled is not None:
        sets.append("enabled=?"); args.append(int(bool(enabled)))
    if sound is not None:
        sets.append("sound=?"); args.append(int(bool(sound)))
    if desktop is not None:
        sets.append("desktop=?"); args.append(int(bool(desktop)))
    if params is not None:
        sets.append("params_json=?")
        args.append(json.dumps(normalize_params(params), ensure_ascii=False))
    if cooldown_min is not None:
        cooldown_min = int(cooldown_min)
        if not (1 <= cooldown_min <= 2880):
            raise FilterError("Пауза: 1–2880 минут")
        sets.append("cooldown_min=?"); args.append(cooldown_min)
    if sets:
        with _lock, _connect() as c:
            c.execute(f"UPDATE signal_filters SET {', '.join(sets)} WHERE id=?",
                      (*args, fid))
    return get(fid)


def delete(user_email: str, fid: int) -> bool:
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM signal_filters WHERE id=? AND user_email=?",
                        (fid, user_email))
        if cur.rowcount:
            c.execute("DELETE FROM signal_hits WHERE filter_id=?", (fid,))
        return cur.rowcount > 0


# --- лента срабатываний ---

def hits_for_user(user_email: str, limit: int = HITS_LIMIT) -> List[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT h.*, f.name AS filter_name FROM signal_hits h "
            "LEFT JOIN signal_filters f ON f.id = h.filter_id "
            "WHERE h.user_email=? ORDER BY h.fired_at DESC LIMIT ?",
            (user_email, int(limit))).fetchall()
    return [dict(r) for r in rows]


def mark_seen(user_email: str) -> int:
    with _lock, _connect() as c:
        cur = c.execute("UPDATE signal_hits SET seen=1 WHERE user_email=? AND seen=0",
                        (user_email,))
        return cur.rowcount


def clear_hits(user_email: str) -> int:
    """Чистит ленту. Анти-спам обнуляется вместе с ней — это осознанно: пустая
    лента должна значить «начали с чистого листа», иначе бумага молчала бы до
    конца паузы, не показавшись ни разу."""
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM signal_hits WHERE user_email=?", (user_email,))
        return cur.rowcount


def fresh_matches(fid: int, user_email: str, cooldown_min: int, side: str,
                  matches: List[dict]) -> List[dict]:
    """Отсекает бумаги, уже показанные внутри cooldown; свежие пишет в ленту."""
    if not matches:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cooldown_min)).isoformat()
    now = _now()
    fresh = []
    with _lock, _connect() as c:
        for m in matches:
            r = c.execute("SELECT fired_at FROM signal_hits WHERE filter_id=? AND isin=?",
                          (fid, m["isin"])).fetchone()
            if r and r["fired_at"] > cutoff:
                continue
            c.execute(
                "INSERT INTO signal_hits(filter_id,isin,user_email,name,side,val_bps,"
                "price,money_rub,fired_at,seen) VALUES(?,?,?,?,?,?,?,?,?,0) "
                "ON CONFLICT(filter_id,isin) DO UPDATE SET name=excluded.name, "
                "side=excluded.side, val_bps=excluded.val_bps, price=excluded.price, "
                "money_rub=excluded.money_rub, fired_at=excluded.fired_at, seen=0",
                (fid, m["isin"], user_email, m.get("name"), side, m.get("val_bps"),
                 m.get("price"), m.get("money_rub"), now))
            fresh.append(dict(m, fired_at=now))
    return fresh


async def preview(user_email: str, params: dict, limit: int = 20) -> dict:
    """Прогон фильтра «прямо сейчас», без записи в ленту — форма показывает,
    что вообще попадёт под условия, до того как их сохранят."""
    p = normalize_params(params)
    uni, metrics, depth_map = await market_snapshot()
    if not metrics:
        return {"ready": False, "total": 0, "matches": []}
    ms = evaluate(p, uni, metrics, depth_map)
    return {"ready": True, "total": len(ms), "matches": ms[:limit]}


async def run_cycle() -> int:
    """Один проход: enabled-фильтры всех пользователей против снапшота рынка.
    Свежие матчи пишутся в ленту и пушатся в браузер. → число сработавших фильтров."""
    from api.routes import ws as wsmod

    filters = list_enabled()
    if not filters:
        return 0
    uni, metrics, depth_map = await market_snapshot()
    if not metrics:
        return 0

    fired = 0
    for f in filters:
        try:
            matches = evaluate(f["params"], uni, metrics, depth_map)
            fresh = fresh_matches(f["id"], f["user_email"], f["cooldown_min"],
                                  f["params"]["side"], matches)
            if not fresh:
                continue
            await wsmod.manager.broadcast_signal(f["user_email"], {
                "type": "signal",
                "filter_id": f["id"], "filter_name": f["name"],
                "side": f["params"]["side"],
                "sound": f["sound"], "desktop": f["desktop"],
                "matches": fresh,
            })
            fired += 1
        except Exception as e:
            logger.warning("signal filter %s error: %s", f.get("id"), e)
    return fired
