"""Крупные сделки по всем облигациям MOEX: безадресные + РПС (адресные).

Зачем отдельный слой, если есть tick-архив (services/trades_archive):
  • тик-архив льётся из Alor ПО ОДНОЙ БУМАГЕ и только по безадресным бордам
    (в базе живьём: TQCB/TQOB/TQRD) — адресных сделок там нет вообще;
  • крупняк чаще всего идёт именно адресно: РПС с ЦК (PTOB), РПС (PSOB),
    размещения (PSAU), выкупы (PSBB). Замер 2026-08-11: за день по рынку
    133 адресных сделки ≥5 млн ₽ — их не видно ни в стакане, ни в alltrades;
  • тик-архив ограничен юниверсом реестра, а блок в соседнем выпуске того же
    эмитента — тоже сигнал.

Источник — ISS, СКВОЗНАЯ лента всего рынка (не по бумаге):
  GET /iss/engines/stock/markets/{bonds|ndm}/trades.json
      tradeno=<последний вычитанный>&next_trade=1&limit=5000 — инкремент;
      без tradeno — с начала сессии, пагинация start.
Замеры на 2026-08-11: bonds — 271 947 сделок за день (55 страниц, ~100 c на
полный проход), из них ≥5 млн ₽ всего 506; ndm — 4 656 сделок (1 страница).
Поэтому опрашиваем сквозным курсором: за минуту прирост — единицы страниц.

Протухший курсор безопасен: ISS на неизвестный tradeno отдаёт ленту с начала
сессии, то есть деградирует до полного прохода, а не до дыры.

Глубина назад: поштучных адресных сделок за прошлые дни ISS не отдаёт вообще
(history market=ndm — только дневные агрегаты по бумаге и борду). Их и
бэкфиллим в block_day; поштучная лента копится вперёд.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

_ISS = "https://iss.moex.com/iss"
_MSK = timezone(timedelta(hours=3))
_PAGE = 5000                  # максимальный limit сквозной ленты ISS
_HIST_PAGE = 100              # пагинация history-эндпоинтов ISS

# Порог записи. Всё, что мельче, не пишем: за день по рынку 272k безадресных
# сделок, из них крупных сотни — база не должна повторять tick-архив.
BLOCK_MIN_VALUE_RUB = float(os.getenv("BLOCK_MIN_VALUE_RUB", "5000000"))
BLOCK_BACKFILL_DAYS = int(os.getenv("BLOCK_BACKFILL_DAYS", "30"))
# Полный проход ленты с начала сессии дорогой (55 страниц) — держим потолок,
# чтобы сбой курсора не превратил каждый опрос в стомегабайтную выкачку.
_MAX_PAGES = int(os.getenv("BLOCK_MAX_PAGES", "80"))

MARKETS = ("bonds", "ndm")


# ────────────────────────── справочник бумаг ──────────────────────────

_secmap: dict = {"at": None, "map": {}}


def _iss_rows(payload: dict, block: str) -> list[dict]:
    """Блок ISS {columns, data} → список словарей (ISS отдаёт колоночный формат)."""
    b = payload.get(block) or {}
    cols = b.get("columns") or []
    return [dict(zip(cols, row)) for row in (b.get("data") or [])]


async def secid_map(client: Optional[httpx.AsyncClient] = None,
                    force: bool = False) -> dict[str, dict]:
    """SECID → {isin, face} по ВСЕМ облигациям MOEX (3140 бумаг, один запрос).

    Нужна, потому что лента ISS идентифицирует бумагу через SECID, а у ОФЗ он
    не совпадает с ISIN (SU26248RMFS3 ↔ RU000A...). Кэш на сутки: состав
    рынка меняется медленнее, а в ленте появляются новые размещения — их
    подхватит следующий дневной рефреш (до него сделка ляжет с isin=secid).
    """
    today = date.today().isoformat()
    if not force and _secmap["at"] == today and _secmap["map"]:
        return _secmap["map"]
    from services.market_data import _moex_get
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        r = await _moex_get(
            client, f"{_ISS}/engines/stock/markets/bonds/securities.json",
            params={"iss.meta": "off", "iss.only": "securities",
                    "securities.columns": "SECID,ISIN,FACEVALUE,SHORTNAME,CURRENCYID"},
            timeout=30.0)
    finally:
        if own:
            await client.aclose()
    if r is None or r.status_code != 200:
        logger.warning("block: справочник бумаг недоступен, работаем на старом кэше")
        return _secmap["map"]
    out: dict[str, dict] = {}
    for row in _iss_rows(r.json(), "securities"):
        sec = row.get("SECID")
        if not sec or sec in out:            # бумага повторяется по бордам
            continue
        out[sec] = {"isin": row.get("ISIN") or sec,
                    "face": float(row["FACEVALUE"]) if row.get("FACEVALUE") else None,
                    "name": row.get("SHORTNAME"),
                    "cur": row.get("CURRENCYID")}
    if out:
        _secmap["map"], _secmap["at"] = out, today
    return _secmap["map"]


# ────────────────────────── курсор ленты ──────────────────────────

def get_cursor(market: str) -> Optional[int]:
    with _connect() as c:
        r = c.execute("SELECT last_tradeno FROM block_cursor WHERE market=?",
                      (market,)).fetchone()
    return int(r["last_tradeno"]) if r else None


def set_cursor(market: str, tradeno: int, session_date: Optional[str] = None) -> None:
    """Курсор двигаем только ВПЕРЁД: параллельный ручной прогон не должен
    откатывать демон назад и заставлять его перечитывать сессию."""
    now = datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S")
    with _lock, _connect() as c:
        c.execute(
            "INSERT INTO block_cursor(market,last_tradeno,session_date,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(market) DO UPDATE SET "
            "last_tradeno=MAX(last_tradeno,excluded.last_tradeno), "
            "session_date=excluded.session_date, updated_at=excluded.updated_at",
            (market, int(tradeno), session_date, now))


# ────────────────────────── сбор поштучных сделок ──────────────────────────

def _ts(row: dict) -> str:
    """TRADEDATE + TRADETIME → 'YYYY-MM-DD HH:MM:SS' МСК (ISS отдаёт МСК)."""
    d = row.get("TRADEDATE") or row.get("TRADE_SESSION_DATE") or date.today().isoformat()
    t = (row.get("TRADETIME") or "00:00:00")[:8]
    return f"{d} {t}"


def _side(row: dict) -> Optional[str]:
    """BUYSELL безадресной ленты ('B'/'S') → сторона агрессора. У адресных
    сделок стороны нет по определению — сделка договорная, агрессора нет."""
    v = (row.get("BUYSELL") or "").strip().upper()
    return {"B": "buy", "S": "sell"}.get(v)


def upsert_trades(rows: list[dict], market: str, secmap: dict) -> tuple[int, set[str]]:
    """Пишет сделки ≥ порога. Возвращает (записано, незнакомые SECID).

    INSERT OR IGNORE по TRADENO — перечитанная сессия (протухший курсор) дублей
    не плодит. Бумаги вне справочника облигаций отбрасываем: в ndm рядом с
    облигациями торгуются акции, ПАИ и ETF (PTEQ/PSIF/PTTF), а нам нужны только
    бонды. Незнакомые SECID возвращаем наверх — свежее размещение может просто
    не успеть попасть в суточный кэш справочника."""
    out, unknown = [], set()
    for r in rows:
        val = r.get("VALUE")
        if val is None or float(val) < BLOCK_MIN_VALUE_RUB:
            continue
        tid, sec = r.get("TRADENO"), r.get("SECID")
        if tid is None or not sec:
            continue
        meta = secmap.get(sec)
        if meta is None:
            unknown.add(sec)
            continue
        out.append((int(tid), meta.get("isin") or sec, sec, _ts(r), market,
                    r.get("BOARDID"), r.get("PRICE"), r.get("QUANTITY"),
                    float(val), r.get("YIELD"), _side(r), meta.get("face"),
                    meta.get("cur")))
    if not out:
        return 0, unknown
    with _lock, _connect() as c:
        cur = c.executemany(
            "INSERT OR IGNORE INTO block_trade"
            "(trade_id,isin,secid,ts,market,board,price,qty,value,yld,side,face,cur) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
        return cur.rowcount or 0, unknown


async def sweep_market(market: str, client: Optional[httpx.AsyncClient] = None,
                       from_start: bool = False) -> dict:
    """Инкрементальный проход сквозной ленты рынка. Возвращает статистику.

    Курсор двигаем ТОЛЬКО по фактически прочитанным строкам: оборванная на
    середине пагинация (таймаут ISS) оставляет курсор на последней прочитанной
    сделке, следующий проход продолжит ровно оттуда.
    """
    from services.market_data import _moex_get
    own = client is None
    client = client or httpx.AsyncClient()
    secmap = await secid_map(client)
    cursor = None if from_start else get_cursor(market)
    seen = saved = pages = 0
    last = cursor
    session_date = None
    refreshed = False        # справочник за проход обновляем не более раза
    try:
        while pages < _MAX_PAGES:
            params = {"iss.meta": "off", "iss.only": "trades", "limit": _PAGE}
            if last:
                params.update({"tradeno": last, "next_trade": 1})
            else:
                params["start"] = seen
            r = await _moex_get(client, f"{_ISS}/engines/stock/markets/{market}/trades.json",
                                params=params, timeout=60.0)
            pages += 1
            if r is None or r.status_code != 200:
                logger.warning("block sweep %s: HTTP %s", market,
                               r.status_code if r is not None else "timeout")
                break
            # разбор 5000 строк JSON держит event loop десятки мс — в поток
            rows = await asyncio.to_thread(lambda: _iss_rows(r.json(), "trades"))
            if not rows:
                break
            seen += len(rows)
            n, unknown = await asyncio.to_thread(upsert_trades, rows, market, secmap)
            if unknown and not refreshed:
                # свежее размещение ещё не в суточном кэше справочника — иначе
                # первая (и самая крупная) сделка нового выпуска потерялась бы
                refreshed = True
                secmap = await secid_map(client, force=True)
                extra, _ = await asyncio.to_thread(upsert_trades, rows, market, secmap)
                n += extra
            saved += n
            last = rows[-1].get("TRADENO") or last
            session_date = rows[-1].get("TRADEDATE") or session_date
            if len(rows) < _PAGE:
                break
    finally:
        if own:
            await client.aclose()
    if last and last != cursor:
        set_cursor(market, last, session_date)
    return {"market": market, "seen": seen, "saved": saved, "pages": pages,
            "cursor": last}


async def sweep(from_start: bool = False) -> dict:
    """Проход по обоим рынкам одним клиентом. Последовательно: ISS под общим
    семафором market_data, параллелить нечего."""
    async with httpx.AsyncClient() as client:
        res = [await sweep_market(m, client, from_start=from_start) for m in MARKETS]
    return {"markets": res, "saved": sum(x["saved"] for x in res),
            "seen": sum(x["seen"] for x in res)}


# ────────────────────────── бэкфилл дневных агрегатов РПС ──────────────────────────

def upsert_days(rows: list[dict], secmap: dict) -> int:
    out = []
    for r in rows:
        sec, d, board = r.get("SECID"), r.get("TRADEDATE"), r.get("BOARDID")
        if not sec or not d or not board:
            continue
        if sec not in secmap:            # не облигация (в ndm живут и акции, и ПАИ)
            continue
        meta = secmap[sec]
        out.append((meta.get("isin") or sec, d, board, sec, r.get("NUMTRADES"),
                    r.get("VALUE"), r.get("WAPRICE"), r.get("CLOSE"),
                    r.get("VOLUME"), r.get("FACEVALUE") or meta.get("face")))
    if not out:
        return 0
    with _lock, _connect() as c:
        cur = c.executemany(
            "INSERT INTO block_day(isin,date,board,secid,numtrades,value,waprice,"
            "close,volume,face) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(isin,date,board) DO UPDATE SET numtrades=excluded.numtrades,"
            "value=excluded.value, waprice=excluded.waprice, close=excluded.close,"
            "volume=excluded.volume, face=excluded.face", out)
        return cur.rowcount or 0


async def backfill_day(d: str, client: Optional[httpx.AsyncClient] = None) -> int:
    """Дневные РПС-агрегаты за одну дату (весь рынок ndm — 185-250 строк)."""
    from services.market_data import _moex_get
    own = client is None
    client = client or httpx.AsyncClient()
    secmap = await secid_map(client)
    saved, start = 0, 0
    try:
        while True:
            r = await _moex_get(
                client, f"{_ISS}/history/engines/stock/markets/ndm/securities.json",
                params={"date": d, "start": start, "iss.meta": "off",
                        "iss.only": "history", "limit": _HIST_PAGE}, timeout=30.0)
            if r is None or r.status_code != 200:
                logger.warning("block backfill %s: HTTP %s", d,
                               r.status_code if r is not None else "timeout")
                break
            rows = _iss_rows(r.json(), "history")
            if not rows:
                break
            saved += await asyncio.to_thread(upsert_days, rows, secmap)
            start += len(rows)
            if len(rows) < _HIST_PAGE:
                break
    finally:
        if own:
            await client.aclose()
    return saved


def days_present() -> set[str]:
    with _connect() as c:
        return {r[0] for r in c.execute("SELECT DISTINCT date FROM block_day")}


async def backfill(days: int = BLOCK_BACKFILL_DAYS, force: bool = False) -> dict:
    """Бэкфилл дневных агрегатов за последние `days` календарных дней.

    Идемпотентно: уже залитые даты пропускаем (force=True — перезалить).
    Сегодняшний день не берём: дневная история ISS появляется вечером, а
    поштучная лента текущей сессии и так собирается sweep'ом."""
    have = set() if force else days_present()
    today = datetime.now(_MSK).date()
    saved, fetched = 0, 0
    async with httpx.AsyncClient() as client:
        await secid_map(client)
        for i in range(1, days + 1):
            d = (today - timedelta(days=i)).isoformat()
            if d in have:
                continue
            n = await backfill_day(d, client)
            fetched += 1
            saved += n
    return {"days_requested": days, "days_fetched": fetched, "rows": saved}


# ────────────────────────── чтение ──────────────────────────

def _where(frm: Optional[str], till: Optional[str], min_value: float,
           market: Optional[str], boards: Optional[list[str]],
           isins: Optional[list[str]], side: Optional[str]) -> tuple[str, list]:
    q, args = " WHERE 1=1", []
    if frm:
        q += " AND ts >= ?"
        args.append(frm)
    if till:
        q += " AND ts <= ?"
        args.append(till + " 23:59:59" if len(till) == 10 else till)
    if min_value:
        q += " AND value >= ?"
        args.append(min_value)
    if market in MARKETS:
        q += " AND market = ?"
        args.append(market)
    if boards:
        q += f" AND board IN ({','.join('?' * len(boards))})"
        args.extend(boards)
    if isins:
        q += f" AND isin IN ({','.join('?' * len(isins))})"
        args.extend(isins)
    if side in ("buy", "sell"):
        q += " AND side = ?"
        args.append(side)
    return q, args


def read_blocks(frm: Optional[str] = None, till: Optional[str] = None,
                min_value: float = 0, market: Optional[str] = None,
                boards: Optional[list[str]] = None, isins: Optional[list[str]] = None,
                side: Optional[str] = None, limit: int = 500) -> list[dict]:
    """Лента крупных сделок, новые сверху."""
    where, args = _where(frm, till, min_value, market, boards, isins, side)
    q = ("SELECT trade_id,isin,secid,ts,market,board,price,qty,value,yld,side,face,cur "
         "FROM block_trade" + where + " ORDER BY ts DESC, trade_id DESC LIMIT ?")
    args.append(limit)
    with _connect() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def blocks_stats(frm: Optional[str] = None, till: Optional[str] = None,
                 min_value: float = 0, market: Optional[str] = None,
                 boards: Optional[list[str]] = None, isins: Optional[list[str]] = None,
                 side: Optional[str] = None, top: int = 10) -> dict:
    """Итоги окна по ВСЕМ подходящим сделкам, а не по срезанным лимитом.

    Обороты суммируем только по рублёвым выпускам: у валютных бумаг VALUE
    приходит в валюте расчётов, и сложение дало бы бессмысленное число."""
    where, args = _where(frm, till, min_value, market, boards, isins, side)
    _V = "SUM(CASE WHEN cur IS NULL OR cur='SUR' THEN value ELSE 0 END)"
    with _connect() as c:
        tot = c.execute(f"SELECT COUNT(*) n, {_V} v FROM block_trade" + where,
                        args).fetchone()
        by_mkt = c.execute(f"SELECT market, COUNT(*) n, {_V} v FROM block_trade"
                           + where + " GROUP BY market", args).fetchall()
        tops = c.execute(f"SELECT isin, COUNT(*) n, {_V} v FROM block_trade" + where
                         + " GROUP BY isin ORDER BY v DESC LIMIT ?", [*args, top]).fetchall()
        last = c.execute("SELECT MAX(ts) t FROM block_trade").fetchone()
    return {"n": tot["n"] or 0, "value": tot["v"] or 0,
            "by_market": {r["market"]: {"n": r["n"], "value": r["v"] or 0} for r in by_mkt},
            "top": [{"isin": r["isin"], "n": r["n"], "value": r["v"] or 0} for r in tops],
            "archive_till": last["t"] if last else None}


def read_days(isin: Optional[str] = None, frm: Optional[str] = None,
              till: Optional[str] = None, min_value: float = 0,
              limit: int = 1000) -> list[dict]:
    """Дневные РПС-агрегаты (то, что есть за дни ДО поштучного сбора)."""
    q = "SELECT * FROM block_day WHERE 1=1"
    args: list = []
    if isin:
        q += " AND isin = ?"
        args.append(isin)
    if frm:
        q += " AND date >= ?"
        args.append(frm)
    if till:
        q += " AND date <= ?"
        args.append(till)
    if min_value:
        q += " AND value >= ?"
        args.append(min_value)
    q += " ORDER BY date DESC, value DESC LIMIT ?"
    args.append(limit)
    with _connect() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def boards_seen(days: int = 30) -> list[dict]:
    """Какие режимы реально встречались — для фильтра на фронте (жёсткий список
    бордов ISS протух бы молча)."""
    frm = (datetime.now(_MSK).date() - timedelta(days=days)).isoformat()
    with _connect() as c:
        rows = c.execute(
            "SELECT market, board, COUNT(*) n, SUM(value) v FROM block_trade "
            "WHERE ts >= ? GROUP BY market, board ORDER BY v DESC", (frm,)).fetchall()
    return [{"market": r["market"], "board": r["board"], "n": r["n"],
             "value": r["v"] or 0} for r in rows]


# ────────────────────────── уведомления о блоках ──────────────────────────
#
# Порог уведомления НАМНОГО выше порога записи: за день по рынку ~500 сделок
# ≥5 млн ₽ — колокольчик от такого потока стал бы шумом. В ленту КРУПНЫЕ
# пишется всё, звонит только по-настоящему крупное.
BLOCK_ALERT_MIN_RUB = float(os.getenv("BLOCK_ALERT_MIN_RUB", "100000000"))
BLOCK_ALERTS = os.getenv("BLOCK_ALERTS", "1") not in ("0", "false", "False")
_ALERT_KEY = "alert"          # строка-водяной знак в block_cursor


def pending_alerts(limit: int = 20) -> list[dict]:
    """Сделки крупнее порога уведомления, ещё не разосланные.

    Водяной знак — TRADENO (сквозной и монотонный), а не время: сделка может
    доехать в ленту позже соседней по времени, и по времени её бы пропустили."""
    mark = get_cursor(_ALERT_KEY) or 0
    with _connect() as c:
        rows = c.execute(
            "SELECT trade_id,isin,secid,ts,market,board,price,qty,value,yld,side,cur "
            "FROM block_trade WHERE trade_id > ? AND value >= ? "
            "AND (cur IS NULL OR cur='SUR') ORDER BY trade_id LIMIT ?",
            (mark, BLOCK_ALERT_MIN_RUB, limit)).fetchall()
    return [dict(r) for r in rows]


def mark_alerted(trade_id: int) -> None:
    set_cursor(_ALERT_KEY, trade_id)


def seed_alert_mark() -> None:
    """На холодную ставим знак на текущий максимум: первый запуск не должен
    вывалить в колокольчик весь сегодняшний бэкфилл."""
    if get_cursor(_ALERT_KEY):
        return
    with _connect() as c:
        r = c.execute("SELECT MAX(trade_id) m FROM block_trade").fetchone()
    if r and r["m"]:
        set_cursor(_ALERT_KEY, int(r["m"]))


async def notify_blocks() -> int:
    """Рассылает новые крупные блоки в ленту СИГНАЛОВ и в колокольчик.

    События кладутся в signal_events с filter_id=0 — это не фильтр скринера, а
    рыночное событие, общее для всех пользователей; UI отличает его по
    reason='block'. Возвращает число разосланных сделок."""
    if not BLOCK_ALERTS:
        return 0
    rows = await asyncio.to_thread(pending_alerts)
    if not rows:
        return 0
    from api.routes import ws as wsmod
    from services.auth_users import list_users
    from services import instruments_registry as reg

    labels = await asyncio.to_thread(reg.labels_map)
    names = {v["isin"]: v.get("name") for v in _secmap["map"].values() if v.get("isin")}
    users = [u["email"] for u in await asyncio.to_thread(list_users) if u.get("email")]
    now = datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S")

    matches = []
    for r in rows:
        lb = labels.get(r["isin"]) or {}
        matches.append({
            "isin": r["isin"],
            "name": lb.get("name") or names.get(r["isin"]) or r["isin"],
            "price": r["price"], "money_rub": r["value"],
            "board": r["board"], "negotiated": r["market"] == "ndm",
            "side": r["side"], "ts": r["ts"], "reason": "block",
            "rating": lb.get("rating"), "fired_at": now,
        })

    def _persist():
        with _lock, _connect() as c:
            c.executemany(
                "INSERT INTO signal_events(filter_id,user_email,isin,name,side,"
                "price,money_rub,reason,fired_at,seen) VALUES(0,?,?,?,?,?,?,'block',?,0)",
                [(u, m["isin"], m["name"], m["side"], m["price"], m["money_rub"], now)
                 for u in users for m in matches])
    await asyncio.to_thread(_persist)

    for u in users:
        await wsmod.manager.broadcast_signal(u, {
            "type": "block", "filter_id": 0, "filter_name": "Крупная сделка",
            "side": None, "sound": True, "desktop": True, "matches": matches,
        })
    mark_alerted(max(r["trade_id"] for r in rows))
    return len(rows)


def db_stats() -> dict:
    """Состояние слоя — для /api/status."""
    with _connect() as c:
        t = c.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM block_trade").fetchone()
        d = c.execute("SELECT COUNT(*) n, MIN(date) a, MAX(date) b FROM block_day").fetchone()
        cur = c.execute("SELECT market, last_tradeno, session_date, updated_at "
                        "FROM block_cursor").fetchall()
    return {"blocks": t["n"], "blocks_from": t["a"], "blocks_till": t["b"],
            "days": d["n"], "days_from": d["a"], "days_till": d["b"],
            "min_value_rub": BLOCK_MIN_VALUE_RUB,
            "cursors": [dict(r) for r in cur]}
