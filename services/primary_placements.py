"""История размещений с биржи: дневные итоги бордов «Размещение» (ISS).

Зачем отдельно от вкладки анонсов (services/primary_calendar): анонс — это
чужой прогноз ДО выхода бумаги на биржу, а здесь ФАКТ — по какой цене и на
какой объём выпуск реально разместился, включая доразмещения через месяцы.

Источник — ISS history по борду размещения:
  GET /iss/history/engines/stock/markets/ndm/boards/{PSAU|PAUS|PACY}/securities.json
      ?date=YYYY-MM-DD  — одна страница (60-90 строк) на дату и борд.
Глубина: ISS отдаёт эти борды на годы назад (проверено 2021-03-17), поэтому
история качается бэкфиллом; вперёд её же дописывает ночной такт.

Каветаты источника, которые из кода не видны:
  • WAPRICE на бордах размещения ВСЕГДА пустой — цена только CLOSE (% номинала).
    Средневзвес по выпуску считаем сами, взвешивая CLOSE объёмом дня;
  • две трети строк дня — мусор с NUMTRADES=0: бумага числится на борде, но
    сделок не было. Пишем только строки с числом сделок > 0, иначе таблица
    забивается вечно висящими субфедералами;
  • размещение почти никогда не однодневное: ВТБ капает ежедневно месяцами,
    у корпората за первым днём идут доразмещения. Поэтому в базе строка =
    (бумага, день), а витрина показывает АГРЕГАТ по выпуску с разворотом;
  • VALUE на валютных бордах (PAUS/PACY) приходит в валюте борда — пересчёт в
    рубли курсом ДНЯ (services/fx), как в block_trades. Без курса дня строка
    остаётся без рублёвого объёма (value_rub=NULL), а не врёт рублями;
  • у ОФЗ SECID ≠ ISIN, а справочник торгуемых бумаг (block_trades.secid_map)
    забывает выпуск в день погашения. Поэтому ISIN и эмитент берутся из sec_ref
    — справочника ВСЕГО рынка, включая погашенные бумаги (см. sync_sec_ref);
    в самой строке дня isin остаётся тем, что было известно при записи.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

# Клиент MOEX — только через фабрику: она одна знает про MOEX_PROXY
from services.market_data import moex_client

from services.portfolio_db import _connect, _lock
from services.pools import run_bg

logger = logging.getLogger(__name__)

_ISS = "https://iss.moex.com/iss"
_MSK = timezone(timedelta(hours=3))
_PAGE = 100

# Борды размещения. «Выкуп» (PSBB/PSBU/PSBY) сюда НЕ входит: это обратная
# операция, эмитент забирает бумагу с рынка.
BOARDS = tuple((os.getenv("PLACEMENT_BOARDS") or "PSAU,PAUS,PACY").split(","))
# Глубина бэкфилла по умолчанию (календарных дней назад).
BACKFILL_DAYS = int(os.getenv("PLACEMENT_BACKFILL_DAYS", "365"))


# ────────────────────────── запись ──────────────────────────

def upsert_rows(rows: list[dict], secmap: dict, bccy: dict, rates: dict) -> int:
    """Строки ISS history → placement_day. Пустые дни (NUMTRADES=0) отбрасываем."""
    out = []
    for r in rows:
        sec, d, board = r.get("SECID"), r.get("TRADEDATE"), r.get("BOARDID")
        if not sec or not d or not board:
            continue
        if not (r.get("NUMTRADES") or 0):
            continue
        val = r.get("VALUE")
        cur = bccy.get(board) or "SUR"
        val_rub = None
        if val is not None:
            if cur == "SUR":
                val_rub = float(val)
            else:
                rate = rates.get((cur, d))
                val_rub = float(val) * rate if rate else None
        meta = secmap.get(sec) or {}
        out.append((sec, d, board, meta.get("isin") or sec,
                    r.get("SHORTNAME") or meta.get("name"),
                    r.get("NUMTRADES"),
                    float(val) if val is not None else None, val_rub,
                    r.get("CLOSE"), r.get("VOLUME"),
                    r.get("FACEVALUE") or meta.get("face"),
                    r.get("COUPONPERCENT"), cur))
    if not out:
        return 0
    with _lock, _connect() as c:
        cur_ = c.executemany(
            "INSERT INTO placement_day(secid,date,board,isin,shortname,numtrades,"
            "value,value_rub,price,volume,face,coupon_pct,cur) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(secid,date,board) DO UPDATE SET isin=excluded.isin,"
            "shortname=excluded.shortname, numtrades=excluded.numtrades,"
            "value=excluded.value, value_rub=excluded.value_rub, price=excluded.price,"
            "volume=excluded.volume, face=excluded.face, coupon_pct=excluded.coupon_pct,"
            "cur=excluded.cur", out)
        return cur_.rowcount or 0


def _mark_day(d: str, rows: int) -> None:
    """Дата отмечается СХОДИВШЕЙ даже при нуле строк: выходные и тихие дни без
    размещений иначе перезапрашивались бы вечно на каждом бэкфилле."""
    with _lock, _connect() as c:
        c.execute("INSERT INTO placement_sync(date,rows,at) VALUES(?,?,?) "
                  "ON CONFLICT(date) DO UPDATE SET rows=excluded.rows, at=excluded.at",
                  (d, rows, datetime.now(timezone.utc).isoformat(timespec="seconds")))


def days_present() -> set[str]:
    with _connect() as c:
        return {r[0] for r in c.execute("SELECT date FROM placement_sync")}


async def fetch_date(d: str, client: httpx.AsyncClient, secmap: dict,
                     bccy: dict, rates: dict) -> int:
    """Все борды размещения за одну дату → база. → сколько строк записано."""
    from services.market_data import moex_client, _moex_get
    saved = 0
    for board in BOARDS:
        start = 0
        while True:
            r = await _moex_get(
                client,
                f"{_ISS}/history/engines/stock/markets/ndm/boards/{board}/securities.json",
                params={"date": d, "start": start, "iss.meta": "off",
                        "iss.only": "history", "limit": _PAGE}, timeout=30.0)
            if r is None or r.status_code != 200:
                logger.warning("placements %s %s: HTTP %s", d, board,
                               r.status_code if r is not None else "timeout")
                break
            from services.block_trades import _iss_rows
            rows = _iss_rows(r.json(), "history")
            if not rows:
                break
            saved += await run_bg(upsert_rows, rows, secmap, bccy, rates)
            start += len(rows)
            if len(rows) < _PAGE:
                break
    return saved


async def backfill(days: int = BACKFILL_DAYS, force: bool = False) -> dict:
    """Бэкфилл окна назад. Идемпотентно: уже сходившие даты пропускаются —
    итог дня в ISS задним числом не меняется (force перечитывает всё).

    Сегодняшний день не берём: history публикуется после закрытия сессии."""
    from services.block_trades import board_ccy_map, secid_map
    from services import fx as fx_svc

    have = set() if force else await run_bg(days_present)
    today = datetime.now(_MSK).date()
    saved, fetched = 0, 0
    async with moex_client() as client:
        secmap = await secid_map(client)
        bccy = await board_ccy_map(client)
        ccys = {c for b, c in bccy.items() if b in BOARDS and c != "SUR"}
        for i in range(1, max(days, 1) + 1):
            d = (today - timedelta(days=i)).isoformat()
            if d in have:
                continue
            # курс дня нужен только валютным бордам размещения (их единицы)
            rates = {(c, d): await run_bg(fx_svc.rate_on, c, d) for c in ccys}
            n = await fetch_date(d, client, secmap, bccy, rates)
            await run_bg(_mark_day, d, n)
            fetched += 1
            saved += n
    return {"days_requested": days, "days_fetched": fetched, "rows": saved}


# ───────────────── справочник бумаг всего рынка (эмитенты) ─────────────────

def upsert_sec_ref(rows: list[dict]) -> int:
    out = [(r.get("secid"), r.get("isin") or r.get("secid"), r.get("shortname"),
            r.get("name"), r.get("emitent_id"), r.get("emitent_title"),
            r.get("type"), r.get("is_traded"),
            datetime.now(timezone.utc).isoformat(timespec="seconds"))
           for r in rows if r.get("secid")]
    if not out:
        return 0
    with _lock, _connect() as c:
        cur = c.executemany(
            "INSERT INTO sec_ref(secid,isin,shortname,name,emitent_id,emitent_title,"
            "type,is_traded,at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(secid) DO UPDATE SET isin=excluded.isin,"
            "shortname=excluded.shortname, name=excluded.name,"
            "emitent_id=excluded.emitent_id, emitent_title=excluded.emitent_title,"
            "type=excluded.type, is_traded=excluded.is_traded, at=excluded.at", out)
        return cur.rowcount or 0


async def sync_sec_ref(max_pages: int = 250) -> dict:
    """Справочник ВСЕХ облигаций MOEX (с погашенными) → sec_ref.

    Один проход = 15,7 тыс. строк за ~35 с (замер 2026-09-06), поэтому гоняется
    раз в сутки, а не на запрос. Нужен ради ЭМИТЕНТА: реестр знает его только у
    бумаг, которые мы прайсим (337 из 1546 размещений года), а справочник
    торгуемых бумаг забывает выпуск в день погашения."""
    from services.market_data import _moex_get
    saved, start, pages = 0, 0, 0
    async with moex_client() as client:
        while pages < max_pages:
            r = await _moex_get(
                client, f"{_ISS}/securities.json",
                params={"iss.meta": "off", "group_by": "group",
                        "group_by_filter": "stock_bonds", "limit": _PAGE,
                        "start": start}, timeout=30.0)
            if r is None or r.status_code != 200:
                logger.warning("sec_ref: HTTP %s",
                               r.status_code if r is not None else "timeout")
                break
            from services.block_trades import _iss_rows
            rows = _iss_rows(r.json(), "securities")
            if not rows:
                break
            saved += await run_bg(upsert_sec_ref, rows)
            start += len(rows)
            pages += 1
            if len(rows) < _PAGE:
                break
    return {"rows": saved, "pages": pages}


# ───────────────── паспорт выпуска (объём эмиссии, листинг) ─────────────────

# Сколько погашенных бумаг добираем поштучно за один прогон. Батч знает только
# торгуемые, а из 1546 размещений года 404 уже погашены — их паспорт стоит по
# запросу на бумагу, и вываливать эту тысячу в один такт незачем: доля
# размещения у погашенной бумаги не меняется никогда.
DETAILS_LAZY_LIMIT = int(os.getenv("PLACEMENT_DETAILS_LIMIT", "250"))


def upsert_details(rows: list[tuple]) -> int:
    """rows: (secid, issue_size, placed, list_level, mat_date, offer_date, face)."""
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = [(*r, now) for r in rows]
    with _lock, _connect() as c:
        # UPDATE, а не INSERT: строка справочника уже создана sync_sec_ref, и
        # заводить её отсюда значило бы плодить записи без имени и эмитента
        cur = c.executemany(
            "UPDATE sec_ref SET issue_size=COALESCE(?, issue_size), "
            "issue_size_placed=COALESCE(?, issue_size_placed), "
            "list_level=COALESCE(?, list_level), mat_date=COALESCE(?, mat_date), "
            "offer_date=COALESCE(?, offer_date), face_value=COALESCE(?, face_value), "
            "details_at=? WHERE secid=?",
            [(r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[0]) for r in out])
        return cur.rowcount or 0


def _details_missing(limit: int) -> list[str]:
    """Размещения, чей паспорт ещё не собран (батч их не знает — погашены)."""
    with _connect() as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT p.secid FROM placement_day p "
            "LEFT JOIN sec_ref s ON s.secid = p.secid "
            "WHERE s.details_at IS NULL ORDER BY p.date DESC LIMIT ?", (limit,))]


async def sync_sec_details(lazy_limit: int = DETAILS_LAZY_LIMIT) -> dict:
    """Объём эмиссии, размещённый объём, листинг, погашение и оферта → sec_ref.

    Два источника, и это не дублирование:
      • батч торгуемых бумаг (один запрос на 3,4 тыс. строк) — ЕДИНСТВЕННОЕ
        место, где биржа отдаёт ISSUESIZEPLACED, то есть сколько выпуска
        реально размещено. Наша сумма по дням заменой не служит: часть книги
        могла пройти до начала собранной истории;
      • поштучный паспорт (`/iss/securities/{secid}`) для погашенных, которых в
        батче уже нет. Там placed отсутствует — остаётся объём эмиссии, и доля
        считается по нашей сумме размещённых дней.
    """
    from services.market_data import _moex_get
    from services.block_trades import _iss_rows

    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    batch, lazy = 0, 0
    async with moex_client() as client:
        r = await _moex_get(client, f"{_ISS}/engines/stock/markets/bonds/securities.json",
                            params={"iss.meta": "off", "iss.only": "securities"},
                            timeout=60.0)
        if r is not None and r.status_code == 200:
            seen, rows = set(), []
            for x in _iss_rows(r.json(), "securities"):
                sec = x.get("SECID")
                if not sec or sec in seen:      # бумага повторяется по бордам
                    continue
                seen.add(sec)
                rows.append((sec, _f(x.get("ISSUESIZE")), _f(x.get("ISSUESIZEPLACED")),
                             x.get("LISTLEVEL"), x.get("MATDATE") or None,
                             x.get("OFFERDATE") or None, _f(x.get("FACEVALUE"))))
            batch = await run_bg(upsert_details, rows)
        else:
            logger.warning("паспорта выпусков: батч недоступен (%s)",
                           r.status_code if r is not None else "timeout")

        missing = await run_bg(_details_missing, lazy_limit)
        one = []
        for sec in missing:
            rr = await _moex_get(client, f"{_ISS}/securities/{sec}.json",
                                 params={"iss.meta": "off", "iss.only": "description"},
                                 timeout=20.0)
            if rr is None or rr.status_code != 200:
                continue
            d = {x.get("name"): x.get("value") for x in _iss_rows(rr.json(), "description")}
            one.append((sec, _f(d.get("ISSUESIZE")), _f(d.get("ISSUESIZEPLACED")),
                        d.get("LISTLEVEL"), d.get("MATDATE") or None,
                        d.get("OFFERDATE") or None, _f(d.get("FACEVALUE"))))
        lazy = await run_bg(upsert_details, one)
    return {"batch": batch, "lazy": lazy, "lazy_pending": max(len(missing) - len(one), 0)}


# ────────────────────────── чтение ──────────────────────────

# Сколько дней без сделок на борде считаем «размещение ещё идёт». Отсчёт от
# ПОСЛЕДНЕЙ СОБРАННОЙ ДАТЫ, а не от сегодня: история ISS публикуется вечером и
# не растёт в выходные — иначе в понедельник утром «идущих» не осталось бы ни
# одного.
ACTIVE_DAYS = int(os.getenv("PLACEMENT_ACTIVE_DAYS", "7"))
# Потолок выдачи. Дефолт держим выше годового объёма (1546 выпусков), чтобы
# витрина никогда не получала молча обрезанный список; о срезе всё равно
# сообщаем — см. truncated в api/routes/primary.
MAX_ROWS = 5000


def aggregates(d_from: Optional[str] = None, d_to: Optional[str] = None,
               q: Optional[str] = None, min_rub: float = 0.0,
               limit: int = MAX_ROWS, active_only: bool = False) -> list[dict]:
    """Строка = ВЫПУСК: первый день размещения, объём, средневзвешенная цена.

    ОКНО ФИЛЬТРУЕТ ПЕРВЫЙ ДЕНЬ ВЫПУСКА, а не отдельные дни, и агрегат всегда
    считается по ВСЕЙ его истории. Иначе продолжающееся размещение (250 выпусков
    из 1546 идут дольше дня, хвосты до 192 дней) обрезалось по границе окна и
    подписывалось её датой: Сульфур1P2 в окне «3 месяца» выглядел как
    «разместился 08.06 на 65 млн» вместо «12.11.2025, 264 млн». Дата и деньги
    врали одновременно, причём тем сильнее, чем длиннее книга.

    active_only — «книга ещё набирается» (сделки на борде за последние
    ACTIVE_DAYS дней собранной истории). Окно дат при этом НЕ применяется: такие
    выпуски как раз и стартовали задолго до него — с окном фильтр показывал бы
    пустоту ровно там, где он нужен.

    Средневзвес считаем по объёму в ШТУКАХ, а не по деньгам: у валютного борда
    рублёвый объём зависит от курса дня, и цена размещения (% номинала) от него
    не должна зависеть вовсе."""
    args: list = []
    # ISIN берём из справочника рынка, а не из строки дня: у ОФЗ SECID с ним не
    # совпадает, а в момент записи бумаги могло не быть в справочнике торгуемых.
    sql = (
        "WITH first_dates AS (SELECT secid, MIN(date) d FROM placement_day GROUP BY secid), "
        "first_prices AS (SELECT p.secid, "
        "SUM(p.price*p.volume)/NULLIF(SUM(p.volume),0) first_price "
        "FROM placement_day p JOIN first_dates f ON p.secid=f.secid AND p.date=f.d "
        "WHERE p.price > 0 AND p.volume > 0 GROUP BY p.secid) "
        "SELECT p.secid, COALESCE(s.isin, p.isin) isin, "
        "MAX(fp.first_price) first_price, "
        "MAX(p.shortname) shortname, MAX(s.emitent_title) emitent_moex, "
        "MAX(s.name) full_name, MAX(s.type) sec_type, "
        # паспорт выпуска: объём эмиссии и сколько его разместила биржа
        "MAX(s.issue_size) issue_size, MAX(s.issue_size_placed) issue_size_placed, "
        "MAX(s.list_level) list_level, MAX(s.mat_date) mat_date, "
        "MAX(s.offer_date) offer_date, "
        "MIN(p.date) first_date, MAX(p.date) last_date, "
        # дней РАЗМЕЩЕНИЯ, а не строк таблицы: бумага может пройти день сразу по
        # двум бордам (22 таких дня в истории) — это один день, а не два
        "COUNT(DISTINCT p.date) days, "
        "SUM(p.numtrades) numtrades, SUM(p.value_rub) value_rub, "
        "SUM(p.volume) volume, MAX(p.face) face, MAX(p.coupon_pct) coupon_pct, "
        "MAX(p.cur) cur, "
        "SUM(CASE WHEN p.price > 0 AND p.volume > 0 THEN p.price*p.volume END) / "
        "NULLIF(SUM(CASE WHEN p.price > 0 AND p.volume > 0 THEN p.volume END),0) wa_price, "
        "MIN(p.price) price_min, MAX(p.price) price_max, "
        "MAX(p.date) >= DATE((SELECT MAX(date) FROM placement_day), ?) active "
        "FROM placement_day p LEFT JOIN sec_ref s ON s.secid = p.secid "
        "LEFT JOIN first_prices fp ON fp.secid = p.secid "
        "GROUP BY p.secid"
    )
    args.append(f"-{ACTIVE_DAYS} days")

    having = []
    if active_only:
        having.append("active = 1")
    if d_from and not active_only:
        having.append("first_date >= ?")
        args.append(d_from)
    if d_to and not active_only:
        having.append("first_date <= ?")
        args.append(d_to)
    if min_rub:
        having.append("value_rub >= ?")
        args.append(min_rub)
    if q:
        having.append("(UPPER(shortname) LIKE ? OR UPPER(p.secid) LIKE ? "
                      "OR UPPER(emitent_moex) LIKE ?)")
        needle = f"%{q.strip().upper()}%"
        args += [needle, needle, needle]
    if having:
        sql += " HAVING " + " AND ".join(having)
    sql += " ORDER BY first_date DESC, value_rub DESC LIMIT ?"
    args.append(max(1, min(int(limit), MAX_ROWS)))
    with _connect() as c:
        return [dict(r) for r in c.execute(sql, args)]


def debut_map() -> dict[str, dict]:
    """ПЕРВЫЕ ТОРГИ после книги: {secid: {date, close, waprice}}.

    Зачем рядом со спредом книги: спред считается только флоатерам реестра (227
    выпусков из 1546), а цена первого дня на бирже есть у 774 — она отвечает на
    «как рынок встретил выпуск» там, где нашей оценки нет вовсе: у фиксов,
    субфедов и всего, что мы не прайсим.

    Окно 10 дней: у неликвида торги начинаются не в день размещения. Берём
    первый день с ценой, а внутри дня — борд с бОльшим оборотом (бумага может
    идти сразу на TQCB и TQOB, и цена «главного» борда репрезентативнее)."""
    with _connect() as c:
        rows = c.execute("""
            WITH p AS (SELECT secid, MAX(isin) isin, MIN(date) d
                       FROM placement_day GROUP BY secid),
                 t AS (SELECT p.secid, MIN(b.date) d0
                       FROM p JOIN bond_day b ON b.isin = p.isin
                       WHERE b.date >= p.d AND b.date <= DATE(p.d, '+10 days')
                         AND b.close IS NOT NULL
                       GROUP BY p.secid)
            SELECT t.secid, t.d0 date, b.close, b.waprice
            FROM t JOIN p ON p.secid = t.secid
                   JOIN bond_day b ON b.isin = p.isin AND b.date = t.d0
            GROUP BY t.secid HAVING b.value = MAX(b.value)""").fetchall()
    return {r["secid"]: dict(r) for r in rows}


def days_of(secid: str) -> list[dict]:
    """Дневная раскладка одного выпуска (разворот строки витрины)."""
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT date, board, numtrades, value, value_rub, price, volume, face, cur "
            "FROM placement_day WHERE secid = ? ORDER BY date", (secid,))]


def stats() -> dict:
    with _connect() as c:
        r = c.execute("SELECT COUNT(*) rows, COUNT(DISTINCT secid) issues, "
                      "MIN(date) d_min, MAX(date) d_max FROM placement_day").fetchone()
        synced = c.execute("SELECT COUNT(*) n FROM placement_sync").fetchone()["n"]
    return {**dict(r), "days_synced": synced}
