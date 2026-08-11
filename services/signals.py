"""Вкладка СИГНАЛЫ: фильтры скринера веб-аккаунта и доставка событий в браузер
(WS-пуш → всплывающее окно, звук, системное уведомление).

Мониторинг СОБЫТИЙНЫЙ, а не по расписанию: тик сравнивает текущий набор
бумаг с прошлым и шлёт только изменения — бумага попала в набор (`new`) либо
её цена / спред / объём сдвинулись на change_pct (`price`/`spread`/`money`).
Молчащий рынок молчит; шевеление видно в тот же тик.

Тик частый (см. SIGNALS_INTERVAL), потому что данные уже в памяти: стаканы
текут push'ом от Alor в market_cache['depth'] (services/universe_stream), а
метрики считает движок universe_stream. Сеть на такте не трогаем.

Условия фильтра, VWAP на объём и прогон по рынку — общие с Telegram-ботом,
живут в services/screener_core.py."""
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock
from services.screener_core import (RATINGS, FilterError, evaluate,  # noqa: F401
                                    evaluate_candidates, market_snapshot,
                                    normalize_params, static_candidates)

logger = logging.getLogger(__name__)

EVENTS_LIMIT = 100          # длина ленты, отдаваемой вкладке
_MAX_FILTERS_PER_USER = 30
_EVENTS_KEEP = 500          # хвост истории на пользователя


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json"))
    for k in ("enabled", "sound", "desktop"):
        d[k] = bool(d[k])
    return d


# --- CRUD (per user_email) ---

def create(user_email: str, name: str, params: dict, *, change_pct: float = 10.0,
           sound: bool = True, desktop: bool = True) -> dict:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise FilterError("Название: 1–60 символов")
    change_pct = float(change_pct if change_pct is not None else 10.0)
    if not (0.1 <= change_pct <= 100):
        raise FilterError("Порог изменения: 0,1–100 %")
    p = normalize_params(params)
    with _lock, _connect() as c:
        n = c.execute("SELECT COUNT(*) FROM signal_filters WHERE user_email=?",
                      (user_email,)).fetchone()[0]
        if n >= _MAX_FILTERS_PER_USER:
            raise FilterError(f"Больше {_MAX_FILTERS_PER_USER} фильтров не поддерживается")
        cur = c.execute(
            "INSERT INTO signal_filters(user_email,name,params_json,change_pct,"
            "sound,desktop,created_at) VALUES(?,?,?,?,?,?,?)",
            (user_email, name, json.dumps(p, ensure_ascii=False), change_pct,
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
           change_pct: Optional[float] = None, sound: Optional[bool] = None,
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
    if change_pct is not None:
        change_pct = float(change_pct)
        if not (0.1 <= change_pct <= 100):
            raise FilterError("Порог изменения: 0,1–100 %")
        sets.append("change_pct=?"); args.append(change_pct)
    if params is not None:
        sets.append("params_json=?")
        args.append(json.dumps(normalize_params(params), ensure_ascii=False))
        # условия сменились — прошлое состояние набора недействительно
        _reset_state(fid)
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
            c.execute("DELETE FROM signal_state WHERE filter_id=?", (fid,))
            c.execute("DELETE FROM signal_events WHERE filter_id=?", (fid,))
        return cur.rowcount > 0


def _reset_state(fid: int) -> None:
    with _lock, _connect() as c:
        c.execute("DELETE FROM signal_state WHERE filter_id=?", (fid,))


# --- лента событий ---

def events_for_user(user_email: str, limit: int = EVENTS_LIMIT) -> List[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT e.*, f.name AS filter_name FROM signal_events e "
            "LEFT JOIN signal_filters f ON f.id = e.filter_id "
            "WHERE e.user_email=? ORDER BY e.fired_at DESC, e.id DESC LIMIT ?",
            (user_email, int(limit))).fetchall()
    return [dict(r) for r in rows]


def unseen_count(user_email: str) -> int:
    with _connect() as c:
        return c.execute("SELECT COUNT(*) FROM signal_events "
                         "WHERE user_email=? AND seen=0", (user_email,)).fetchone()[0]


def mark_seen(user_email: str) -> int:
    with _lock, _connect() as c:
        cur = c.execute("UPDATE signal_events SET seen=1 WHERE user_email=? AND seen=0",
                        (user_email,))
        return cur.rowcount


def clear_events(user_email: str) -> int:
    """Чистит только ленту. Состояние набора не трогаем — иначе все бумаги
    тут же переоткрылись бы как «новые» и лента наполнилась заново."""
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM signal_events WHERE user_email=?", (user_email,))
        return cur.rowcount


def _trim_events(user_email: str) -> None:
    with _lock, _connect() as c:
        c.execute(
            "DELETE FROM signal_events WHERE user_email=? AND id NOT IN "
            "(SELECT id FROM signal_events WHERE user_email=? "
            " ORDER BY fired_at DESC, id DESC LIMIT ?)",
            (user_email, user_email, _EVENTS_KEEP))


# --- детект событий ---

def _changed(prev: Optional[float], cur: Optional[float], pct: float) -> bool:
    """Метрика «шевельнулась» на pct% в любую сторону относительно прошлого
    значения. Появление значения там, где его не было, — тоже событие."""
    if cur is None:
        return False
    if prev is None:
        return True
    base = abs(prev)
    if base < 1e-9:
        return abs(cur) > 1e-9
    return abs(cur - prev) / base * 100.0 >= pct


def detect_events(fid: int, user_email: str, side: str, change_pct: float,
                  matches: List[dict], want_money: Optional[float]) -> List[dict]:
    """Сравнивает набор с прошлым состоянием → только события. Состояние
    обновляется по ВСЕМ текущим бумагам (в том числе не давшим события), а
    выпавшие из набора забываются: вернутся — снова «новая»."""
    now = _now()
    events = []
    present = {m["isin"] for m in matches}
    with _lock, _connect() as c:
        known = {r["isin"]: r for r in c.execute(
            "SELECT isin, val_bps, price, money_rub FROM signal_state WHERE filter_id=?",
            (fid,)).fetchall()}

        for m in matches:
            prev = known.get(m["isin"])
            if prev is None:
                reason = "new"
            elif _changed(prev["price"], m.get("price"), change_pct):
                reason = "price"
            elif _changed(prev["val_bps"], m.get("val_bps"), change_pct):
                reason = "spread"
            elif _changed(prev["money_rub"], m.get("money_rub"), change_pct):
                reason = "money"
            else:
                reason = None

            if reason:
                c.execute(
                    "INSERT INTO signal_events(filter_id,user_email,isin,name,side,"
                    "val_bps,price,money_rub,want_money_rub,levels,single_px,reason,"
                    "prev_val_bps,prev_price,prev_money_rub,fired_at,seen) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (fid, user_email, m["isin"], m.get("name"), side,
                     m.get("val_bps"), m.get("price"), m.get("money_rub"),
                     want_money, m.get("levels"), m.get("single_px"), reason,
                     prev["val_bps"] if prev else None,
                     prev["price"] if prev else None,
                     prev["money_rub"] if prev else None, now))
                events.append(dict(
                    m, reason=reason, fired_at=now, want_money_rub=want_money,
                    prev_price=prev["price"] if prev else None,
                    prev_val_bps=prev["val_bps"] if prev else None,
                    prev_money_rub=prev["money_rub"] if prev else None))

            # состояние двигаем только когда событие зафиксировано, иначе
            # медленный дрейф по чуть-чуть никогда не наберёт порог
            if reason or prev is None:
                c.execute(
                    "INSERT INTO signal_state(filter_id,isin,val_bps,price,money_rub,"
                    "updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(filter_id,isin) DO UPDATE SET val_bps=excluded.val_bps, "
                    "price=excluded.price, money_rub=excluded.money_rub, "
                    "updated_at=excluded.updated_at",
                    (fid, m["isin"], m.get("val_bps"), m.get("price"),
                     m.get("money_rub"), now))

        for isin in [i for i in known if i not in present]:
            c.execute("DELETE FROM signal_state WHERE filter_id=? AND isin=?", (fid, isin))
    return events


async def preview(user_email: str, params: dict, limit: int = 20) -> dict:
    """Прогон фильтра «прямо сейчас», без записи — форма показывает, что
    попадёт под условия, до сохранения."""
    p = normalize_params(params)
    uni, metrics, depth_map = await market_snapshot()
    if not metrics:
        return {"ready": False, "total": 0, "matches": []}
    ms = evaluate(p, uni, metrics, depth_map)
    return {"ready": True, "total": len(ms), "matches": ms[:limit]}


# --- цикл мониторинга ---

_candidates_cache: dict = {}      # fid → (params_json, [строки универса])


def _candidates(f: dict, uni: List[dict]) -> List[dict]:
    """Статический отбор кешируется на фильтр: рейтинг/эмитент/срок/суборд не
    меняются от тика к тику, а перебор 577 бумаг каждые пару секунд — пустая
    работа. Кэш сбрасывается сменой условий или обновлением универса."""
    key = (json.dumps(f["params"], sort_keys=True), len(uni))
    hit = _candidates_cache.get(f["id"])
    if hit and hit[0] == key:
        return hit[1]
    cands = static_candidates(f["params"], uni)
    _candidates_cache[f["id"]] = (key, cands)
    return cands


async def run_cycle() -> int:
    """Один тик: enabled-фильтры против снапшота рынка. События пишутся в ленту
    и пушатся в браузер. → число фильтров, давших события."""
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
            cands = _candidates(f, uni)
            matches = evaluate_candidates(f["params"], cands, metrics, depth_map)
            events = detect_events(f["id"], f["user_email"], f["params"]["side"],
                                   f.get("change_pct") or 10.0, matches,
                                   f["params"].get("min_money_rub"))
            if not events:
                continue
            _trim_events(f["user_email"])
            await wsmod.manager.broadcast_signal(f["user_email"], {
                "type": "signal",
                "filter_id": f["id"], "filter_name": f["name"],
                "side": f["params"]["side"],
                "sound": f["sound"], "desktop": f["desktop"],
                "matches": events,
            })
            fired += 1
        except Exception as e:
            logger.warning("signal filter %s error: %s", f.get("id"), e)
    return fired
