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
from datetime import date, datetime, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock
from services.screener_core import (BLOCK_BASES, RATINGS, FilterError,  # noqa: F401
                                    block_matches, evaluate,
                                    evaluate_candidates, market_snapshot,
                                    normalize_block_params, normalize_params,
                                    static_candidates)

logger = logging.getLogger(__name__)

EVENTS_LIMIT = 100          # длина ленты, отдаваемой вкладке
_MAX_FILTERS_PER_USER = 30
_EVENTS_KEEP = 500          # хвост истории на пользователя


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json"))
    d["kind"] = d.get("kind") or "book"
    for k in ("enabled", "sound", "desktop"):
        d[k] = bool(d[k])
    return d


def _normalize(kind: str, params: dict) -> dict:
    return normalize_block_params(params) if kind == "block" else normalize_params(params)


def _check_kind(kind: Optional[str]) -> str:
    kind = (kind or "book").strip()
    if kind not in ("book", "block"):
        raise FilterError("kind: book | block")
    return kind


# --- CRUD (per user_email) ---

def create(user_email: str, name: str, params: dict, *, change_pct: float = 10.0,
           sound: bool = True, desktop: bool = True, kind: str = "book") -> dict:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise FilterError("Название: 1–60 символов")
    kind = _check_kind(kind)
    change_pct = float(change_pct if change_pct is not None else 10.0)
    if not (0.1 <= change_pct <= 100):
        raise FilterError("Порог изменения: 0,1–100 %")
    p = _normalize(kind, params)
    with _lock, _connect() as c:
        n = c.execute("SELECT COUNT(*) FROM signal_filters WHERE user_email=?",
                      (user_email,)).fetchone()[0]
        if n >= _MAX_FILTERS_PER_USER:
            raise FilterError(f"Больше {_MAX_FILTERS_PER_USER} фильтров не поддерживается")
        cur = c.execute(
            "INSERT INTO signal_filters(user_email,name,params_json,change_pct,"
            "sound,desktop,kind,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_email, name, json.dumps(p, ensure_ascii=False), change_pct,
             int(bool(sound)), int(bool(desktop)), kind, _now()))
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
    """Включённые фильтры СТАКАНА — их гоняет run_cycle. Блочные сюда не
    попадают: они срабатывают не на снимок рынка, а на приход сделки."""
    with _connect() as c:
        rows = c.execute("SELECT * FROM signal_filters WHERE enabled=1 "
                         "AND COALESCE(kind,'book')='book'").fetchall()
    return [_row(r) for r in rows]


def list_enabled_blocks() -> List[dict]:
    """Включённые фильтры крупных сделок — их применяет block_trades."""
    with _connect() as c:
        rows = c.execute("SELECT * FROM signal_filters WHERE enabled=1 "
                         "AND kind='block' ORDER BY id").fetchall()
    return [_row(r) for r in rows]


def block_filter_owners() -> set:
    """Кто вообще завёл блок-фильтр — включённый ИЛИ выключенный.

    Этим отделяется «настроил сам» от «ничего не трогал»: у первых умолчание из
    env не работает вовсе, иначе выключенный фильтр воскрешал бы дефолтный
    звонок и выключить уведомления было бы нечем."""
    with _connect() as c:
        return {r["user_email"] for r in c.execute(
            "SELECT DISTINCT user_email FROM signal_filters WHERE kind='block'").fetchall()}


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
        args.append(json.dumps(_normalize(f["kind"], params), ensure_ascii=False))
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


def delete_all(user_email: str, kind: Optional[str] = None) -> int:
    """Сносит фильтры пользователя разом — вместе с их состоянием и их
    событиями в ленте, ровно как поштучный delete().

    kind ограничивает вид (book|block): колонки в UI сносятся раздельно,
    «удалить все» в одной не должно тронуть вторую."""
    kind = _check_kind(kind) if kind else None
    with _lock, _connect() as c:
        q = "SELECT id FROM signal_filters WHERE user_email=?"
        args = [user_email]
        if kind:
            q += " AND COALESCE(kind,'book')=?"
            args.append(kind)
        ids = [r["id"] for r in c.execute(q, args).fetchall()]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        c.execute(f"DELETE FROM signal_state WHERE filter_id IN ({marks})", ids)
        c.execute(f"DELETE FROM signal_events WHERE filter_id IN ({marks})", ids)
        c.execute(f"DELETE FROM signal_filters WHERE id IN ({marks})", ids)
    return len(ids)


def _reset_state(fid: int) -> None:
    with _lock, _connect() as c:
        c.execute("DELETE FROM signal_state WHERE filter_id=?", (fid,))


# --- лента событий ---

def _with_maturity(rows: List[dict]) -> List[dict]:
    """Дописывает погашение и срок до него. Считаем НА ЧТЕНИИ, а не пишем в
    событие: срок тает каждый день, а лента живёт неделями — записанное число
    лет к моменту просмотра уже врёт. Реестр недоступен — просто без срока."""
    if not rows:
        return rows
    try:
        from services import instruments_registry as reg
        labels = reg.labels_map(sorted({r["isin"] for r in rows}))
    except Exception:
        return rows
    today = date.today()
    for r in rows:
        mat = (labels.get(r["isin"]) or {}).get("maturity")
        r["maturity"] = mat
        r["years"] = None
        if mat:
            try:
                r["years"] = max(
                    0.0, (date.fromisoformat(mat) - today).days / 365.25)
            except ValueError:
                pass
    return rows


def events_for_user(user_email: str, limit: int = EVENTS_LIMIT) -> List[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT e.*, f.name AS filter_name, f.kind AS filter_kind, "
            "f.params_json AS filter_params FROM signal_events e "
            "LEFT JOIN signal_filters f ON f.id = e.filter_id "
            "WHERE e.user_email=? ORDER BY e.fired_at DESC, e.id DESC LIMIT ?",
            (user_email, int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # режим набора объёма показываем в ленте словами; фильтр могли уже
        # удалить — тогда single_px остаётся единственной уликой режима
        params = d.pop("filter_params", None)
        mode = None
        if params:
            try:
                mode = (json.loads(params) or {}).get("money_mode")
            except (TypeError, ValueError):
                mode = None
        d["money_mode"] = mode or ("single" if d.get("single_px") is not None
                                   else None)
        out.append(d)
    return _with_maturity(out)


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


def preview_block(params: dict, limit: int = 20) -> dict:
    """Сколько сделок СЕГОДНЯ попало бы под условия блок-фильтра.

    Живого набора у такого фильтра нет — событие мгновенное, поэтому вместо
    «что в наборе сейчас» показываем уже случившееся за день: иначе порог
    ставится вслепую и либо молчит неделю, либо звонит каждые пять минут."""
    from datetime import date

    from services import instruments_registry as reg

    p = normalize_block_params(params)
    day = date.today().isoformat()
    with _connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT trade_id,isin,ts,market,side,value FROM block_trade "
            "WHERE ts >= ? AND value >= ? AND (cur IS NULL OR cur='SUR') "
            "ORDER BY value DESC LIMIT 500",
            (day, p["min_value_rub"])).fetchall()]
    labels = reg.labels_map()
    hits = [r for r in rows if block_matches(r, labels.get(r["isin"]) or {}, p)]
    return {"ready": True, "total": len(hits), "capped": len(rows) >= 500,
            "matches": [{"isin": h["isin"],
                         "name": (labels.get(h["isin"]) or {}).get("name") or h["isin"],
                         "money_rub": h["value"], "ts": h["ts"]}
                        for h in hits[:limit]]}


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
            # копия в привязанный телеграм-чат (буфер, отправка пачкой)
            from services import tg_notify
            tg_notify.enqueue_signal(f["user_email"], f["id"], f["name"],
                                     f["params"]["side"], events)
            fired += 1
        except Exception as e:
            logger.warning("signal filter %s error: %s", f.get("id"), e)
    return fired
