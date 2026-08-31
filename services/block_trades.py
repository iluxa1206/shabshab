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
полный проход), из них ≥5 млн ₽ всего 506, ≥1 млн — порядка полутора тысяч;
ndm — 4 656 сделок (1 страница).
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
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from services.portfolio_db import _connect, _lock
from services.screener_core import money_floor

from services.pools import run_bg

logger = logging.getLogger(__name__)

_ISS = "https://iss.moex.com/iss"
_MSK = timezone(timedelta(hours=3))
_PAGE = 5000                  # максимальный limit сквозной ленты ISS
_HIST_PAGE = 100              # пагинация history-эндпоинтов ISS

# ── что пишем и что остаётся ────────────────────────────────────────────────
# Пишем ВСЁ, что отдаёт ISS (порог записи 0): пока день идёт, лента должна
# показывать рынок целиком — мелкие принты в неликвиде и адресные сделки по
# бумагам вне юниверса больше нигде не сохраняются (tick-архив Alor знает только
# юниверс и только безадресные борды).
#
# Ночью день ужимается до архивного порога: остаются сделки от 5 млн ₽. Замеры
# 2026-08-11: за день по рынку 272k безадресных сделок (≈70 МБ с индексами),
# из них ≥5 млн — около 700 строк. То есть архив растёт на доли мегабайта в
# день, а полный поток живёт ровно столько, сколько нужен.
BLOCK_MIN_VALUE_RUB = float(os.getenv("BLOCK_MIN_VALUE_RUB", "0"))
BLOCK_ARCHIVE_MIN_RUB = float(os.getenv("BLOCK_ARCHIVE_MIN_RUB", "5000000"))
# Сколько последних дней держим целиком (1 = только сегодня).
BLOCK_RAW_DAYS = max(int(os.getenv("BLOCK_RAW_DAYS", "1")), 1)
# Порог расчёта спреда: солвер по всему потоку не нужен — мельче этого сделки
# всё равно не переживут ночь, а внутри дня спред интересен по крупным.
BLOCK_YIDX_MIN_RUB = float(os.getenv("BLOCK_YIDX_MIN_RUB", "1000000"))
# глубина, на которую считаем спред ТИКАМ Alor (см. unpriced): тик закрывает
# только окно ожидания ISS, дальше в ленте всё равно побеждает строка из ISS
TICK_YIDX_DAYS = int(os.getenv("TICK_YIDX_DAYS", "3"))
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


# ВАЛЮТА РАСЧЁТОВ — СВОЙСТВО БОРДА, А НЕ БУМАГИ. Замещайка торгуется сразу на
# двух: TQOB (рубли) и TQOY (юани), и VALUE в ленте ISS приходит В ВАЛЮТЕ БОРДА.
# Справочник бумаг отдаёт один CURRENCYID на SECID (у нас — с первой встреченной
# борд-строки), поэтому юаневая сделка попадала в базу помеченной как рублёвая:
# 136 из 210 строк TQOY по RU000A10FAK6 лежали в юанях под cur='SUR', а сверка
# тиков затягивала это значение в рублёвый объём — занижение в ~12,7 раза.
_BOARD_CCY_RE = re.compile(r"\((USD|EUR|CNY)\)")
_board_ccy: dict = {"at": None, "map": {}}


async def board_ccy_map(client: Optional[httpx.AsyncClient] = None,
                        force: bool = False) -> dict[str, str]:
    """{BOARDID: валюта расчётов} — только для НЕрублёвых бордов.

    Источник — список бордов MOEX: валюта зашита в название («Т+: Облигации
    (CNY) - безадрес.»). Кэш на сутки: борды заводят раз в годы."""
    today = date.today().isoformat()
    if not force and _board_ccy["at"] == today and _board_ccy["map"]:
        return _board_ccy["map"]
    from services.market_data import _moex_get
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        r = await _moex_get(client, f"{_ISS}/engines/stock/markets/bonds/boards.json",
                            params={"iss.meta": "off", "iss.only": "boards"},
                            timeout=20.0)
    finally:
        if own:
            await client.aclose()
    if r is None or r.status_code != 200:
        logger.warning("block: список бордов недоступен, валюта расчётов по кэшу")
        return _board_ccy["map"]
    b = (r.json() or {}).get("boards", {})
    cols, rows = b.get("columns", []), b.get("data", [])
    if not cols or "boardid" not in cols or "title" not in cols:
        return _board_ccy["map"]
    bi, ti = cols.index("boardid"), cols.index("title")
    out = {}
    for row in rows:
        m = _BOARD_CCY_RE.search(row[ti] or "")
        if m and row[bi]:
            out[row[bi]] = m.group(1)
    if out:
        _board_ccy["map"], _board_ccy["at"] = out, today
    return _board_ccy["map"]


async def secid_map(client: Optional[httpx.AsyncClient] = None,
                    force: bool = False) -> dict[str, dict]:
    """SECID → {isin, face, name, cur, maturity} по ВСЕМ облигациям MOEX
    (3140 бумаг, один запрос).

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
                    "securities.columns": "SECID,ISIN,FACEVALUE,SHORTNAME,"
                                          "CURRENCYID,MATDATE"},
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
                    "cur": row.get("CURRENCYID"),
                    # Дата погашения нужна витринам, которые считают по СРОКУ
                    # (карта рынка в дайджесте): реестр знает только флоатеры,
                    # а этот справочник — весь рынок и приезжает тем же запросом.
                    "maturity": row.get("MATDATE") or None}
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


def upsert_trades(rows: list[dict], market: str, secmap: dict,
                  bccy: Optional[dict] = None) -> tuple[int, set[str]]:
    """Пишет сделки ≥ порога. Возвращает (записано, незнакомые SECID).

    INSERT OR IGNORE по TRADENO — перечитанная сессия (протухший курсор) дублей
    не плодит. Бумаги вне справочника облигаций отбрасываем: в ndm рядом с
    облигациями торгуются акции, ПАИ и ETF (PTEQ/PSIF/PTTF), а нам нужны только
    бонды. Незнакомые SECID возвращаем наверх — свежее размещение может просто
    не успеть попасть в суточный кэш справочника.

    VALUE ПРИВОДИТСЯ К РУБЛЯМ. На валютных бордах (TQOY/TQOD/TQOE и адресные
    к ним) биржа отдаёт объём в валюте расчётов — сложить его с рублёвым
    нельзя, а помечен он был рублёвым (валюта бралась по бумаге, а бумага
    торгуется и на рублёвом борде). Курс берём НА ДЕНЬ СДЕЛКИ из архива
    (services/fx): для вчерашней сессии сегодняшний курс уже неверен. Курса
    нет — оставляем сумму в валюте и честно помечаем её cur=валюта: такие
    строки везде исключаются из рублёвых итогов."""
    out, unknown = [], set()
    now = int(time.time())
    bccy = bccy or {}
    from services import fx as fx_svc
    rate_cache: dict = {}
    for r in rows:
        val = r.get("VALUE")
        if val is None or (BLOCK_MIN_VALUE_RUB and float(val) < BLOCK_MIN_VALUE_RUB):
            continue
        tid, sec = r.get("TRADENO"), r.get("SECID")
        if tid is None or not sec:
            continue
        meta = secmap.get(sec)
        if meta is None:
            unknown.add(sec)
            continue
        ts = _ts(r)
        val = float(val)
        cur = bccy.get(r.get("BOARDID")) or meta.get("cur")
        if cur and cur != "SUR":
            key = (cur, ts[:10])
            if key not in rate_cache:
                rate_cache[key] = fx_svc.rate_on(cur, ts[:10])
            rate = rate_cache[key]
            if rate:
                val *= rate
                cur = "SUR"
        out.append((int(tid), meta.get("isin") or sec, sec, ts, market,
                    r.get("BOARDID"), r.get("PRICE"), r.get("QUANTITY"),
                    val, r.get("YIELD"), _side(r), meta.get("face"),
                    cur, now))
    if not out:
        return 0, unknown
    with _lock, _connect() as c:
        cur = c.executemany(
            "INSERT OR IGNORE INTO block_trade"
            "(trade_id,isin,secid,ts,market,board,price,qty,value,yld,side,face,cur,"
            "ins_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
        return cur.rowcount or 0, unknown


async def repair_currency_values(days: int = 60, dry_run: bool = False,
                                 tol: float = 0.2) -> dict:
    """Приводит УЖЕ ЗАПИСАННЫЕ суммы валютных бордов к рублям.

    До 2026-08-31 строка с валютного борда ложилась с VALUE в валюте расчётов и
    пометкой cur='SUR' (валюта бралась по бумаге, а бумага торгуется и на
    рублёвом борде). Такую строку нельзя ни сложить в оборот, ни скопировать в
    тик — сверка объёмов затягивала юани в рублёвое поле.

    Отличаем «в валюте» от «в рублях» по самой сделке: рублёвый объём обязан
    сойтись с qty × номинал × цена% × курс дня. Не сошёлся, а без курса —
    сошёлся, значит сумма в валюте: домножаем и помечаем рублёвой. Строки, где
    не сходится ни так, ни так, не трогаем — гадать не о чем."""
    from services import fx as fx_svc
    bccy = await board_ccy_map()
    if not bccy:
        return {"rows": 0, "delta": 0.0, "skipped": 0, "dry_run": dry_run,
                "note": "карта бордов недоступна"}
    frm = (date.today() - timedelta(days=max(days, 1))).isoformat()
    boards = sorted(bccy)
    ph = ",".join("?" * len(boards))
    with _connect() as c:
        rows = c.execute(
            f"SELECT trade_id, ts, board, qty, price, value, face, cur FROM block_trade "
            f"WHERE board IN ({ph}) AND ts >= ? AND qty > 0 AND price > 0 AND face > 0 "
            f"AND value > 0", [*boards, frm]).fetchall()
    fixed, skipped = [], 0
    delta = 0.0
    rate_cache: dict = {}
    for r in rows:
        ccy = bccy.get(r["board"])
        key = (ccy, r["ts"][:10])
        if key not in rate_cache:
            rate_cache[key] = fx_svc.rate_on(ccy, r["ts"][:10])
        rate = rate_cache[key]
        if not rate:
            skipped += 1
            continue
        base = r["qty"] * r["face"] * r["price"] / 100      # объём в ВАЛЮТЕ номинала
        exp_rub = base * rate
        if abs(r["value"] - exp_rub) / exp_rub <= tol:
            continue                                        # уже рубли
        if abs(r["value"] * rate - exp_rub) / exp_rub > tol:
            skipped += 1                                    # не сходится никак
            continue
        val = round(r["value"] * rate, 2)
        delta += val - r["value"]
        fixed.append((val, r["trade_id"]))
    if fixed and not dry_run:
        with _lock, _connect() as c:
            c.executemany("UPDATE block_trade SET value=?, cur='SUR' WHERE trade_id=?",
                          fixed)
    return {"rows": len(fixed), "delta": round(delta, 2), "skipped": skipped,
            "seen": len(rows), "dry_run": dry_run}


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
    bccy = await board_ccy_map(client)
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
            rows = await run_bg(lambda: _iss_rows(r.json(), "trades"))
            if not rows:
                break
            seen += len(rows)
            n, unknown = await run_bg(upsert_trades, rows, market, secmap, bccy)
            if unknown and not refreshed:
                # свежее размещение ещё не в суточном кэше справочника — иначе
                # первая (и самая крупная) сделка нового выпуска потерялась бы
                refreshed = True
                secmap = await secid_map(client, force=True)
                extra, _ = await run_bg(upsert_trades, rows, market, secmap, bccy)
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


async def sweep(from_start: bool = False, with_metrics: bool = True) -> dict:
    """Проход по обоим рынкам одним клиентом. Последовательно: ISS под общим
    семафором market_data, параллелить нечего."""
    async with httpx.AsyncClient() as client:
        res = [await sweep_market(m, client, from_start=from_start) for m in MARKETS]
    out = {"markets": res, "saved": sum(x["saved"] for x in res),
           "seen": sum(x["seen"] for x in res)}
    if with_metrics and out["saved"]:
        out["priced"] = await price_new_trades()
    return out


# ────────────────────────── спред сделки ──────────────────────────

def prune(archive_min: Optional[float] = None, raw_days: Optional[int] = None,
          dry_run: bool = False) -> dict:
    """Ужимает прошедшие дни до архивного порога: полный поток нужен только
    внутри дня, дальше от него остаются крупные сделки.

    Идём по дням, а не одним DELETE по всей таблице: 272k строк за день — это
    длинная транзакция под локом WAL-писателя, а демон в это время пишет
    следующий такт. Индекс ix_block_ts делает выборку дня дешёвой."""
    thr = BLOCK_ARCHIVE_MIN_RUB if archive_min is None else float(archive_min)
    days = BLOCK_RAW_DAYS if raw_days is None else max(int(raw_days), 1)
    floor = (datetime.now(_MSK).date() - timedelta(days=days - 1)).isoformat()
    with _connect() as c:
        dates = [r[0] for r in c.execute(
            "SELECT DISTINCT substr(ts,1,10) d FROM block_trade "
            "WHERE substr(ts,1,10) < ? ORDER BY d", (floor,))]
    deleted = 0
    for d in dates:
        where = "substr(ts,1,10)=? AND (value IS NULL OR value < ?)"
        if dry_run:
            with _connect() as c:
                n = c.execute(f"SELECT COUNT(*) FROM block_trade WHERE {where}",
                              (d, thr)).fetchone()[0]
        else:
            with _lock, _connect() as c:
                n = c.execute(f"DELETE FROM block_trade WHERE {where}", (d, thr)).rowcount or 0
        deleted += n
    return {"deleted": deleted, "days": len(dates), "keep_from": floor,
            "archive_min_rub": thr, "dry_run": dry_run}


def unpriced(limit: int = 400) -> list[dict]:
    """Сделки без посчитанного спреда, новые сначала.

    Считаем ТОЛЬКО в момент прихода (демоном), а не при чтении ленты: контекст
    строится на выпуск и стоит сетевого gather, поэтому первый запрос ленты по
    сотне выпусков занимал бы минуту. Демон же видит за такт десятки сделок по
    десятку бумаг, и контексты у него тёплые.

    Мелочь ниже BLOCK_YIDX_MIN_RUB не берём вовсе: в потоке 272k сделок за день,
    солвер по ним не нужен, а метку metrics_at им не ставим — эти строки всё
    равно уйдут ночным пруном.

    Тик Alor берём наравне с block_trade: сделка приходит тиком СРАЗУ, а из ISS
    та же строка приезжает лишь через ~15 минут — если ждать её, свежая сделка
    висит в ленте с прочерком всё это время. Дублей нет: тик берётся только
    когда его trade_id ещё не пришёл из ISS (там строка богаче — считаем по ней).
    """
    # Тикам резервируем долю пачки. Иначе они голодают: один такт sweep приносит
    # под две тысячи строк ISS, они забирают выборку целиком, и тик — ради
    # которого всё и затевалось (спред СРАЗУ, а не через 15 минут) — не
    # попадает в расчёт никогда.
    tick_share = max(1, limit // 4)
    with _connect() as c:
        rows = c.execute(
            "SELECT trade_id, isin, price, 'block' AS src FROM block_trade "
            "WHERE metrics_at IS NULL AND price IS NOT NULL AND value >= ? "
            "ORDER BY trade_id DESC LIMIT ?",
            (BLOCK_YIDX_MIN_RUB, max(1, limit - tick_share))).fetchall()
        out = [dict(r) for r in rows]
        left = limit - len(out)
        if left > 0:
            # только свежий хвост: тик нужен ровно для того, чтобы закрыть окно
            # ожидания ISS (~15 мин). У старых сделок ISS-двойник уже приехал и
            # посчитан, а гонять солвер по всему архиву тиков — часы работы
            # впустую (на 12.08.2026 их 47k).
            frm = (datetime.now(_MSK) - timedelta(days=TICK_YIDX_DAYS)).strftime("%Y-%m-%d")
            ticks = c.execute(
                "SELECT t.trade_id, t.isin, t.price, 'tick' AS src FROM trade_tick t "
                "WHERE t.metrics_at IS NULL AND t.price IS NOT NULL AND t.value >= ? "
                "AND t.ts >= ? "
                "AND NOT EXISTS (SELECT 1 FROM block_trade b WHERE b.trade_id = t.trade_id) "
                "ORDER BY t.trade_id DESC LIMIT ?",
                (BLOCK_YIDX_MIN_RUB, frm, left)).fetchall()
            out.extend(dict(r) for r in ticks)
    return out


def unpriced_count() -> int:
    """Сколько сделок ещё без посчитанного спреда — по этому счётчику демон
    решает, держать ли рабочий темп вне торговых часов."""
    frm = (datetime.now(_MSK) - timedelta(days=TICK_YIDX_DAYS)).strftime("%Y-%m-%d")
    with _connect() as c:
        return int(c.execute(
            "SELECT (SELECT COUNT(*) FROM block_trade WHERE metrics_at IS NULL AND value >= ?) "
            "+ (SELECT COUNT(*) FROM trade_tick WHERE metrics_at IS NULL AND value >= ? "
            "   AND ts >= ?)",     # та же граница, что в unpriced: иначе счётчик
            (BLOCK_YIDX_MIN_RUB,   # вечно ненулевой и демон не уходит в редкий такт
             BLOCK_YIDX_MIN_RUB, frm)).fetchone()[0])


def save_metrics(vals: list[tuple], table: str = "block_trade") -> int:
    """[(y_idx, dm, trade_id[, isin])] → в базу. metrics_at ставится всегда, даже
    когда спред посчитать нечем (фикс, бумага вне реестра): иначе такие строки
    возвращались бы в очередь на каждом такте демона.

    table — block_trade (ISS) или trade_tick (Alor). У block_trade trade_id и
    есть первичный ключ, а у тика ключ составной (isin, trade_id): апдейт по
    одному trade_id сканировал бы всю многомиллионную таблицу, держа writer-лок
    и сталкиваясь с живым потоком тиков («database is locked»)."""
    if not vals:
        return 0
    if table not in ("block_trade", "trade_tick"):
        raise ValueError(f"неизвестная таблица: {table}")
    now = datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S")
    if table == "trade_tick":
        q = "UPDATE trade_tick SET y_idx_bps=?, dm_bps=?, metrics_at=? WHERE isin=? AND trade_id=?"
        args = [(y, d, now, isin, tid) for y, d, tid, isin in vals]
    else:
        q = "UPDATE block_trade SET y_idx_bps=?, dm_bps=?, metrics_at=? WHERE trade_id=?"
        args = [(y, d, now, tid) for y, d, tid, *_ in vals]
    for attempt in range(5):
        try:
            with _lock, _connect() as c:
                return c.executemany(q, args).rowcount or 0
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == 4:
                raise
            time.sleep(1.5 * (attempt + 1))
    return 0


def reset_metrics(isins: list[str]) -> int:
    """Вернуть сделки бумаг в очередь расчёта спреда (metrics_at=NULL).

    Нужно, когда бумага ПОЗЖЕ получила базу/маржу: её сделки уже закрыты
    прочерком как «не флоатер», и сами в очередь не вернутся — metrics_at
    ставится всегда, иначе неоцениваемые строки крутились бы вечно."""
    ids = [i for i in (isins or []) if i]
    if not ids:
        return 0
    done = 0
    # По одной пачке на транзакцию и с ретраями: на проде архив пишут демоны, и
    # длинный UPDATE по многомиллионной таблице упирался в чужой writer-лок
    # («database is locked») — весь батч терялся из-за одной занятой секунды.
    for i in range(0, len(ids), 200):         # потолок переменных SQLite
        chunk = ids[i:i + 200]
        for attempt in range(5):
            try:
                with _lock, _connect() as c:
                    cur = c.execute(
                        f"UPDATE block_trade SET metrics_at=NULL WHERE y_idx_bps IS NULL "
                        f"AND value >= ? AND isin IN ({','.join('?' * len(chunk))})",
                        [BLOCK_YIDX_MIN_RUB, *chunk])
                    done += cur.rowcount or 0
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or attempt == 4:
                    raise
                time.sleep(1.5 * (attempt + 1))
    return done


async def price_new_trades(limit: int = 120, batch: int = 2000) -> int:
    """Досчитывает спред новым сделкам. → сколько строк обновлено.

    Сначала пачкой закрываем всё, что считать не нужно (фиксы, бумаги вне
    реестра) — иначе они забивают очередь: крупняк рынка это почти целиком
    ОФЗ-ПД, и до флоатеров расчёт просто не доходил. Солвер тратится только на
    флоатеры, `limit` — их потолок на такт."""
    rows = await run_bg(unpriced, batch)
    if not rows:
        return 0
    from services import instruments_registry as reg
    from services import trade_yidx

    labels = await run_bg(reg.labels_map)
    floats, others = [], []
    for r in rows:
        (floats if (labels.get(r["isin"]) or {}).get("base") in _FLOAT_BASES
         else others).append(r)

    done = 0
    # строки приходят из двух таблиц (ISS и тики Alor) — метки пишем каждой в свою
    def _by_table(rows_, val):
        out = 0
        for tbl in ("block_trade", "trade_tick"):
            part = [val(r) for r in rows_ if r.get("src", "block") ==
                    ("tick" if tbl == "trade_tick" else "block")]
            if part:
                out += save_metrics(part, table=tbl)
        return out

    if others:
        done += await run_bg(
            _by_table, others, lambda r: (None, None, r["trade_id"], r["isin"]))
    if floats:
        floats = floats[:limit]
        await trade_yidx.enrich(floats, labels, max_isins=limit)
        done += await run_bg(
            _by_table, floats,
            lambda r: (r.get("y_idx_bps"), r.get("dm_bps"), r["trade_id"], r["isin"]))
    return done


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
            saved += await run_bg(upsert_days, rows, secmap)
            start += len(rows)
            if len(rows) < _HIST_PAGE:
                break
    finally:
        if own:
            await client.aclose()
    return saved


# ────────────────── дневные итоги безадресных торгов (весь рынок) ──────────────────

def upsert_bond_days(rows: list[dict], secmap: dict, bccy: dict,
                     rates: dict) -> int:
    """Строки ISS history (market=bonds) → bond_day, объём в РУБЛЯХ.

    rates — {(валюта, дата): курс}, собирается снаружи: у одной даты один курс
    на все бумаги, а лезть в базу на каждую строку — это тысячи запросов."""
    out = []
    for r in rows:
        sec, d, board = r.get("SECID"), r.get("TRADEDATE"), r.get("BOARDID")
        val = r.get("VALUE")
        if not sec or not d or not board or val is None:
            continue
        meta = secmap.get(sec)
        if meta is None:               # не облигация / нет в суточном справочнике
            continue
        cur = bccy.get(board) or "SUR"
        if cur != "SUR":
            rate = rates.get((cur, d))
            if not rate:
                # без курса дня рублёвого объёма нет — строку пропускаем целиком,
                # иначе юани лягут в рублёвую колонку (см. block_trade.cur)
                continue
            val = float(val) * rate
        out.append((meta.get("isin") or sec, d, board, sec, r.get("NUMTRADES"),
                    float(val), r.get("WAPRICE"), r.get("CLOSE"), r.get("VOLUME"),
                    r.get("FACEVALUE") or meta.get("face"), cur))
    if not out:
        return 0
    with _lock, _connect() as c:
        cur_ = c.executemany(
            "INSERT INTO bond_day(isin,date,board,secid,numtrades,value,waprice,"
            "close,volume,face,cur) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(isin,date,board) DO UPDATE SET numtrades=excluded.numtrades,"
            "value=excluded.value, waprice=excluded.waprice, close=excluded.close,"
            "volume=excluded.volume, face=excluded.face, cur=excluded.cur", out)
        return cur_.rowcount or 0


async def backfill_bond_day(d: str, client: Optional[httpx.AsyncClient] = None) -> int:
    """Дневные итоги безадресных торгов за одну дату (весь рынок, все борды)."""
    from services.market_data import _moex_get
    from services import fx as fx_svc
    own = client is None
    client = client or httpx.AsyncClient()
    secmap = await secid_map(client)
    bccy = await board_ccy_map(client)
    rates = {(ccy, d): await run_bg(fx_svc.rate_on, ccy, d) for ccy in set(bccy.values())}
    saved, start = 0, 0
    try:
        while True:
            r = await _moex_get(
                client, f"{_ISS}/history/engines/stock/markets/bonds/securities.json",
                params={"date": d, "start": start, "iss.meta": "off",
                        "iss.only": "history", "limit": _HIST_PAGE}, timeout=30.0)
            if r is None or r.status_code != 200:
                logger.warning("bond day %s: HTTP %s", d,
                               r.status_code if r is not None else "timeout")
                break
            rows = _iss_rows(r.json(), "history")
            if not rows:
                break
            saved += await run_bg(upsert_bond_days, rows, secmap, bccy, rates)
            start += len(rows)
            if len(rows) < _HIST_PAGE:
                break
    finally:
        if own:
            await client.aclose()
    return saved


def _traded_boards() -> list[str]:
    """Борды, на которых реально идут безадресные торги. Берём из уже собранной
    дневной истории (bond_day) — она приезжает с БОРДОМ каждой строки, значит
    список всегда соответствует рынку. Пусто (первый запуск) — режимы по
    умолчанию, дальше список наполнится сам."""
    with _connect() as c:
        rows = c.execute(
            "SELECT DISTINCT board FROM bond_day WHERE date >= ?",
            ((date.today() - timedelta(days=7)).isoformat(),)).fetchall()
    boards = sorted(r["board"] for r in rows if r["board"])
    return boards or ["TQCB", "TQOB", "TQIR", "TQRD", "TQOY", "TQOD", "TQOE"]


async def snapshot_bond_day_today(client: Optional[httpx.AsyncClient] = None) -> int:
    """Итог ТЕКУЩЕГО дня из marketdata: history публикуется только после закрытия.

    Биржа сама отдаёт рублёвый оборот дня (VALTODAY_RUR) — для валютных бордов
    это надёжнее нашего пересчёта, курс там биржевой. Строка дня переписывается
    на каждом такте: VALTODAY растёт по ходу сессии."""
    from services.market_data import _moex_get
    own = client is None
    client = client or httpx.AsyncClient()
    secmap = await secid_map(client)
    bccy = await board_ccy_map(client)
    day = datetime.now(_MSK).date().isoformat()
    # ПО БОРДАМ, а не одним запросом: market-level marketdata отдаёт бумагу
    # только на её основном режиме, и у выпуска, который торгуется и на TQCB, и
    # на TQOB/TQOY, половина оборота терялась (замер 2026-08-31: 1969 строк
    # против 3158 в дневной истории того же рынка).
    boards = await run_bg(_traded_boards)
    rows: list = []
    cols: list = []
    try:
        for board in boards:
            r = await _moex_get(
                client, f"{_ISS}/engines/stock/markets/bonds/boards/{board}/securities.json",
                params={"iss.meta": "off", "iss.only": "marketdata",
                        "marketdata.columns": "SECID,BOARDID,VALTODAY,VALTODAY_RUR,"
                                              "VOLTODAY,NUMTRADES,WAPRICE,LAST"},
                timeout=40.0)
            if r is None or r.status_code != 200:
                logger.warning("bond day today %s: HTTP %s", board,
                               r.status_code if r is not None else "timeout")
                continue
            md = (r.json() or {}).get("marketdata", {})
            cols = md.get("columns", []) or cols
            rows.extend(md.get("data", []) or [])
    finally:
        if own:
            await client.aclose()
    if not rows or not cols:
        return 0
    g = {n: cols.index(n) for n in cols}
    out = []
    for row in rows:
        sec, board = row[g["SECID"]], row[g["BOARDID"]]
        meta = secmap.get(sec)
        if meta is None or not board:
            continue
        cur = bccy.get(board) or "SUR"
        # VALTODAY_RUR — рублёвый эквивалент от самой биржи; на рублёвом борде
        # он равен VALTODAY, а на валютном избавляет от нашего курса
        val = row[g.get("VALTODAY_RUR")] if "VALTODAY_RUR" in g else None
        if not val:
            val = row[g["VALTODAY"]] if cur == "SUR" else None
        if not val:
            continue
        out.append((meta.get("isin") or sec, day, board, sec, row[g.get("NUMTRADES")],
                    float(val), row[g.get("WAPRICE")], row[g.get("LAST")],
                    row[g.get("VOLTODAY")], meta.get("face"), cur))
    if not out:
        return 0

    def _write():
        with _lock, _connect() as c:
            cur_ = c.executemany(
                "INSERT INTO bond_day(isin,date,board,secid,numtrades,value,waprice,"
                "close,volume,face,cur) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(isin,date,board) DO UPDATE SET numtrades=excluded.numtrades,"
                "value=excluded.value, waprice=excluded.waprice, close=excluded.close,"
                "volume=excluded.volume, cur=excluded.cur", out)
            return cur_.rowcount or 0
    return await run_bg(_write)


def bond_days_present() -> set[str]:
    with _connect() as c:
        return {r[0] for r in c.execute("SELECT DISTINCT date FROM bond_day")}


async def backfill_bond_days(days: int = 30, force: bool = False) -> dict:
    """Догружает дневные итоги за окно назад. Уже собранные даты пропускает —
    итог дня в ISS не меняется задним числом (force перечитывает всё)."""
    have = set() if force else await run_bg(bond_days_present)
    today = datetime.now(_MSK).date()
    saved, days_done = 0, []
    async with httpx.AsyncClient() as client:
        for k in range(1, max(days, 1) + 1):
            d = (today - timedelta(days=k)).isoformat()
            if d in have:
                continue
            n = await backfill_bond_day(d, client)
            if n:
                saved += n
                days_done.append(d)
    return {"saved": saved, "days": days_done}


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

# Порог, за которым список бумаг едет во временную таблицу, а не в плейсхолдеры:
# у SQLite потолок переменных на запрос (999 в старых сборках), а «только
# флоатеры» — это 1300+ ISIN. Раньше такой фильтр приходилось молча выключать.
_INLINE_ISINS = 400
_TMP = "_isin_filter"


def _bind_isins(c, isins: Optional[list[str]]) -> bool:
    """Кладёт большой список бумаг во временную таблицу соединения.
    → True, если фильтр ушёл в таблицу (WHERE смотрит в неё)."""
    if not isins or len(isins) <= _INLINE_ISINS:
        return False
    c.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_TMP}(isin TEXT PRIMARY KEY)")
    c.execute(f"DELETE FROM {_TMP}")
    c.executemany(f"INSERT OR IGNORE INTO {_TMP}(isin) VALUES(?)",
                  [(i,) for i in isins])
    return True


def _where(frm: Optional[str], till: Optional[str], min_value: float,
           market: Optional[str], boards: Optional[list[str]],
           isins: Optional[list[str]], side: Optional[str],
           isins_in_tmp: bool = False) -> tuple[str, list]:
    q, args = " WHERE 1=1", []
    if frm:
        q += " AND ts >= ?"
        args.append(frm)
    if till:
        q += " AND ts <= ?"
        args.append(till + " 23:59:59" if len(till) == 10 else till)
    if min_value:
        # порог с ЛЮФТОМ: «от 50 млн» показывает и сделку на 48 — человек
        # называет порядок, а не границу до рубля (screener_core.money_floor)
        q += " AND value >= ?"
        args.append(money_floor(min_value))
    if market in MARKETS:
        q += " AND market = ?"
        args.append(market)
    if boards:
        q += f" AND board IN ({','.join('?' * len(boards))})"
        args.extend(boards)
    if isins_in_tmp:
        q += f" AND isin IN (SELECT isin FROM {_TMP})"
    elif isins:
        q += f" AND isin IN ({','.join('?' * len(isins))})"
        args.extend(isins)
    if side in ("buy", "sell"):
        q += " AND side = ?"
        args.append(side)
    return q, args


def read_blocks(frm: Optional[str] = None, till: Optional[str] = None,
                min_value: float = 0, market: Optional[str] = None,
                boards: Optional[list[str]] = None, isins: Optional[list[str]] = None,
                side: Optional[str] = None, limit: int = 500,
                order: str = "ts") -> list[dict]:
    """Лента крупных сделок, новые сверху.

    order='ts' — последние limit сделок; order='value' — limit САМЫХ крупных за
    окно (маркерам РПС на графике нужно именно это: с сортировкой по времени
    лимит молча срезал дальнюю половину окна, и точки обрывались на середине —
    те же грабли, что уже чинили у слоя крупных сделок, см. trades_archive.
    read_trades). Возвращается всегда по времени, новые сверху.

    Спред сделки (y_idx_bps/dm_bps) считает демон при её приходе — отдаём его
    наружу: без этих полей маркер РПС на графике оставался без цифры, хотя она
    посчитана и лежит в той же строке."""
    with _connect() as c:
        tmp = _bind_isins(c, isins)
        where, args = _where(frm, till, min_value, market, boards, isins, side, tmp)
        cols = ("trade_id,isin,secid,ts,market,board,price,qty,value,yld,side,face,cur,"
                "y_idx_bps,dm_bps")
        tail = (" ORDER BY value DESC LIMIT ?" if order == "value"
                else " ORDER BY ts DESC, trade_id DESC LIMIT ?")
        rows = [dict(r) for r in c.execute(f"SELECT {cols} FROM block_trade{where}{tail}",
                                           [*args, limit]).fetchall()]
    rows.sort(key=lambda r: (r.get("ts") or "", r.get("trade_id") or 0), reverse=True)
    return rows


def count_blocks(frm: Optional[str] = None, till: Optional[str] = None,
                 min_value: float = 0, market: Optional[str] = None,
                 boards: Optional[list[str]] = None, isins: Optional[list[str]] = None,
                 side: Optional[str] = None) -> int:
    """Сколько сделок под фильтр вообще подходит: клиент должен отличать
    «столько и было» от «лимит срезал остальное»."""
    with _connect() as c:
        tmp = _bind_isins(c, isins)
        where, args = _where(frm, till, min_value, market, boards, isins, side, tmp)
        return c.execute("SELECT COUNT(*) FROM block_trade" + where, args).fetchone()[0]


def blocks_stats(frm: Optional[str] = None, till: Optional[str] = None,
                 min_value: float = 0, market: Optional[str] = None,
                 boards: Optional[list[str]] = None, isins: Optional[list[str]] = None,
                 side: Optional[str] = None, top: int = 10) -> dict:
    """Итоги окна по ВСЕМ подходящим сделкам, а не по срезанным лимитом.

    Обороты суммируем только по рублёвым выпускам: у валютных бумаг VALUE
    приходит в валюте расчётов, и сложение дало бы бессмысленное число."""
    _V = "SUM(CASE WHEN cur IS NULL OR cur='SUR' THEN value ELSE 0 END)"
    with _connect() as c:
        tmp = _bind_isins(c, isins)
        where, args = _where(frm, till, min_value, market, boards, isins, side, tmp)
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


def _days_where(c, isin: Optional[str], frm: Optional[str], till: Optional[str],
                min_value: float, isins: Optional[list[str]]):
    """Условие выборки дневных агрегатов — ОДНО на строки и на итоги: считать
    итоги по другому набору условий, чем показана таблица, нельзя."""
    q, args = " WHERE 1=1", []
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
        args.append(money_floor(min_value))
    if not isin and isins is not None:
        if _bind_isins(c, isins):
            q += f" AND isin IN (SELECT isin FROM {_TMP})"
        elif isins:
            q += f" AND isin IN ({','.join('?' * len(isins))})"
            args.extend(isins)
        else:
            return None, None
    return q, args


def read_days(isin: Optional[str] = None, frm: Optional[str] = None,
              till: Optional[str] = None, min_value: float = 0,
              limit: int = 1000, isins: Optional[list[str]] = None) -> list[dict]:
    """Дневные РПС-агрегаты (то, что есть за дни ДО поштучного сбора).

    isins — охват (скоуп/эмитенты/срок до погашения); длинный список уезжает во
    временную таблицу, как и в остальных чтениях архива."""
    with _connect() as c:
        where, args = _days_where(c, isin, frm, till, min_value, isins)
        if where is None:
            return []
        q = ("SELECT * FROM block_day" + where
             + " ORDER BY date DESC, value DESC LIMIT ?")
        return [dict(r) for r in c.execute(q, [*args, limit]).fetchall()]


def days_stats(isin: Optional[str] = None, frm: Optional[str] = None,
               till: Optional[str] = None, min_value: float = 0,
               isins: Optional[list[str]] = None, top: int = 10) -> dict:
    """Итоги режима «по дням» — по ВСЕМ подходящим бумаго-дням окна, а не по
    странице, которую видно в таблице (лимит режет строки, но не итоги)."""
    with _connect() as c:
        where, args = _days_where(c, isin, frm, till, min_value, isins)
        if where is None:
            return {"n": 0, "value": 0, "trades": 0, "top": [], "archive_till": None}
        tot = c.execute("SELECT COUNT(*) n, SUM(value) v, SUM(numtrades) t "
                        "FROM block_day" + where, args).fetchone()
        tops = c.execute("SELECT isin, COUNT(*) n, SUM(value) v FROM block_day"
                         + where + " GROUP BY isin ORDER BY v DESC LIMIT ?",
                         [*args, top]).fetchall()
        last = c.execute("SELECT MAX(date) d FROM block_day").fetchone()
    return {"n": tot["n"] or 0, "value": tot["v"] or 0, "trades": tot["t"] or 0,
            "top": [{"isin": r["isin"], "n": r["n"], "value": r["v"] or 0}
                    for r in tops],
            "archive_till": last["d"] if last else None}


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
# Порог уведомления НАМНОГО выше порога записи: за день по рынку ~2500 сделок
# ≥1 млн ₽ — колокольчик от такого потока стал бы шумом. В ленту пишется всё,
# звонит только по-настоящему крупное.
BLOCK_ALERT_MIN_RUB = float(os.getenv("BLOCK_ALERT_MIN_RUB", "100000000"))
# Звоним ТОЛЬКО по флоатерам: десктоп про них, блок в фиксе (а это почти весь
# крупняк рынка — ОФЗ-ПД) в колокольчике только мешает. В ленте фиксы видны,
# если явно переключить охват на весь рынок.
BLOCK_ALERT_FLOATERS_ONLY = os.getenv("BLOCK_ALERT_FLOATERS_ONLY", "1") not in ("0", "false", "False")
_FLOAT_BASES = ("KEYRATE", "RUONIA")
BLOCK_ALERTS = os.getenv("BLOCK_ALERTS", "1") not in ("0", "false", "False")
# Сколько минут строка живёт в очереди звонка. Считается от МОМЕНТА ЗАПИСИ
# (ins_at), не от времени сделки: адресные приезжают из ISS с 15-минутным
# лагом и по времени сделки были бы «протухшими» уже на входе. Окно нужно,
# чтобы рестарт процесса или перечитанная сессия не вывалили в колокольчик
# пачку старого.
ALERT_MAX_AGE_MIN = float(os.getenv("BLOCK_ALERT_MAX_AGE_MIN", "10"))
# Рассылка идёт из двух мест (живой поток Alor и такт ISS-ленты) — выборка и
# пометка обязаны быть неделимыми, иначе одна сделка звонит дважды.
_notify_lock = asyncio.Lock()


def pending_alerts(limit: int = 50, min_value: Optional[float] = None) -> list[dict]:
    """Сделки крупнее порога уведомления, ещё не разосланные.

    Очередь — флаг alerted на строке, а не водяной знак по TRADENO: живой тик
    Alor (см. ingest_ticks) приезжает раньше ISS-строк с меньшими номерами, и
    сдвинутый знак похоронил бы их навсегда.

    min_value — самый низкий порог среди активных получателей: выборка общая, а
    кому что звонить, решается уже по строкам."""
    thr = BLOCK_ALERT_MIN_RUB if min_value is None else min_value
    floor_ts = int(time.time() - ALERT_MAX_AGE_MIN * 60)
    with _connect() as c:
        rows = c.execute(
            "SELECT trade_id,isin,secid,ts,market,board,price,qty,value,yld,side,cur,"
            "y_idx_bps "
            "FROM block_trade WHERE alerted = 0 AND ins_at >= ? AND value >= ? "
            "AND (cur IS NULL OR cur='SUR') ORDER BY trade_id LIMIT ?",
            (floor_ts, thr, limit)).fetchall()
    return [dict(r) for r in rows]


def mark_alerted(trade_ids) -> None:
    """Снимает строки с очереди. Помечаем ВСЕ просмотренные, а не только
    разосланные: иначе пачка отфильтрованных сделок вставала бы перед выборкой
    намертво (она ограничена limit) и подходящая за ней не позвонила бы."""
    ids = [int(t) for t in (trade_ids if isinstance(trade_ids, (list, tuple, set))
                            else [trade_ids])]
    if not ids:
        return
    with _lock, _connect() as c:
        c.executemany("UPDATE block_trade SET alerted = 1 WHERE trade_id = ?",
                      [(i,) for i in ids])


# Порог записи живых тиков в ленту блоков. Пересчитывается из активных
# фильтров — тянуть в block_trade весь поток Alor смысла нет, а взять порог
# выше чужого фильтра значит молча потерять его сигнал.
_floor_cache: dict = {"at": 0.0, "val": None}
_FLOOR_TTL = 60.0


async def alert_floor() -> float:
    """Минимальный порог в рублях среди активных получателей (кэш на минуту)."""
    now = time.time()
    if _floor_cache["val"] is not None and now - _floor_cache["at"] < _FLOOR_TTL:
        return _floor_cache["val"]
    from services import signals
    try:
        bfilters = await run_bg(signals.list_enabled_blocks)
        vals = [f["params"]["min_value_rub"] for f in bfilters
                if f.get("params", {}).get("min_value_rub") is not None]
    except Exception as e:
        logger.warning("block alert floor: %s", e)
        vals = []
    # люфт — здесь же: выборка обязана быть не уже, чем условие block_matches,
    # иначе сделка на 48 млн под фильтром «от 50» не доедет до проверки
    val = money_floor(min(vals + [BLOCK_ALERT_MIN_RUB]))
    _floor_cache["at"], _floor_cache["val"] = now, val
    return val


def ingest_ticks(rows: list[dict]) -> int:
    """Живые безадресные сделки Alor → block_trade (очередь звонка).

    Зачем дублировать источник: колокольчик по безадресным не должен ждать
    ISS с его 15 минутами. Alor даёт тот же TRADENO, поэтому доехавшая позже
    ISS-копия отсекается INSERT OR IGNORE и вторым звонком не станет.
    Адресные (РПС) сюда не попадают вовсе — подписки на них у брокера нет,
    они по-прежнему приезжают из ISS с лагом.

    rows: {isin, trade_id, ts, price, qty, value, side, board}."""
    out = []
    now = int(time.time())
    smap = _secmap["map"]
    by_isin = {v["isin"]: (sec, v.get("face")) for sec, v in smap.items()
               if v.get("isin")}
    for r in rows:
        if r.get("trade_id") is None or not r.get("isin") or not r.get("value"):
            continue
        sec, face = by_isin.get(r["isin"], (r["isin"], None))
        out.append((int(r["trade_id"]), r["isin"], sec, r["ts"], "bonds",
                    r.get("board"), r.get("price"), r.get("qty"),
                    float(r["value"]), None, r.get("side"), face, "SUR", now))
    if not out:
        return 0
    with _lock, _connect() as c:
        cur = c.executemany(
            "INSERT OR IGNORE INTO block_trade"
            "(trade_id,isin,secid,ts,market,board,price,qty,value,yld,side,face,cur,"
            "ins_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
        return cur.rowcount or 0


async def notify_blocks() -> int:
    """Рассылает новые крупные блоки в ленту СИГНАЛОВ и в колокольчик.

    Кому и что звонит, решают ФИЛЬТРЫ пользователя вида kind='block'
    (services/signals): порог в рублях, режим торгов, база купона, отбор бумаг.
    У кого таких фильтров нет — работает умолчание из env (BLOCK_ALERT_MIN_RUB
    + только флоатеры): исторический режим, чтобы включённые уведомления не
    пропали молча у тех, кто фильтр не заводил.

    События кладутся в signal_events с filter_id фильтра (0 — для умолчания,
    это не фильтр скринера, а рыночное событие); UI отличает их по
    reason='block'. Возвращает число разосланных сделок."""
    if not BLOCK_ALERTS:
        return 0
    async with _notify_lock:
        return await _notify_blocks()


async def _notify_blocks() -> int:
    from api.routes import ws as wsmod
    from services import signals
    from services.auth_users import list_users
    from services import instruments_registry as reg

    users = [u["email"] for u in await run_bg(list_users) if u.get("email")]
    if not users:
        return 0
    bfilters = await run_bg(signals.list_enabled_blocks)
    by_user: dict[str, list] = {}
    for f in bfilters:
        by_user.setdefault(f["user_email"], []).append(f)
    # адресат телеграма у каждого фильтра свой (канал «Р5» ↔ фильтр «Р5»);
    # 0 — умолчание из env, у него канала нет по определению
    targets = {f["id"]: f.get("tg_target_id") for f in bfilters}
    # Умолчание — только для тех, кто блок-фильтров не заводил вовсе: иначе
    # выключенный фильтр воскрешал бы дефолтный звонок.
    owners = await run_bg(signals.block_filter_owners)
    legacy_users = [u for u in users if u not in owners]

    # Порог выборки — минимальный из тех, что кому-то нужен: тянуть из базы
    # мельче бессмысленно, крупнее — значит молча потерять чужой сигнал.
    floor = money_floor(
        min([f["params"]["min_value_rub"] for f in bfilters]
            + ([BLOCK_ALERT_MIN_RUB] if legacy_users else []) or [BLOCK_ALERT_MIN_RUB]))
    rows = await run_bg(pending_alerts, 50, floor)
    if not rows:
        return 0

    labels = await run_bg(reg.labels_map)
    names = {v["isin"]: v.get("name") for v in _secmap["map"].values() if v.get("isin")}
    # Время события — В UTC С ТАЙМЗОНОЙ, как у событий стакана (services/signals).
    # Раньше блоки писались строкой МСК без зоны, и лента, сортированная строкой,
    # мешала их с событиями стакана в разнобой; браузер к тому же считал такую
    # строку локальным временем и рисовал не тот час.
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(_MSK).date()

    # Спред нужен ДО отбора, если хоть один фильтр ограничивает его диапазоном:
    # непосчитанный y_idx такой фильтр трактует как «не подходит», и сделка
    # молча терялась бы. Обычно он уже посчитан демоном (price_new_trades), тут
    # закрывается только хвост — строк в выборке единицы.
    if any(f["params"].get("spread_min") is not None
           or f["params"].get("spread_max") is not None for f in bfilters):
        cold = [r for r in rows if r.get("y_idx_bps") is None]
        if cold:
            from services import trade_yidx
            await trade_yidx.for_rows(cold)

    # С очереди снимаем ВЕСЬ просмотренный кусок, а не только разосланное:
    # иначе пачка отфильтрованных сделок подряд встала бы перед выборкой
    # намертво и подходящая за ней никогда бы не позвонила (выборка ограничена
    # limit).
    seen_ids = [r["trade_id"] for r in rows]

    def _legacy_ok(r: dict) -> bool:
        if (r.get("value") or 0) < BLOCK_ALERT_MIN_RUB:
            return False
        if not BLOCK_ALERT_FLOATERS_ONLY:
            return True
        return (labels.get(r["isin"]) or {}).get("base") in _FLOAT_BASES

    # (user, filter_id, filter_name, sound, desktop) → сделки
    routed: dict[tuple, list[dict]] = {}
    # горизонт прайсинга к справочной метке: срок в фильтре и в уведомлении
    # считается той же методикой, что окно срока в мониторе
    from services.market_data import MarketDataService
    _mx = MarketDataService.universe_metrics() or {}
    for r in rows:
        meta = signals.block_meta(labels, r["isin"], _mx)
        for u in legacy_users:
            if _legacy_ok(r):
                routed.setdefault((u, 0, "Крупная сделка", True, True), []).append(r)
        for u, fs in by_user.items():
            # первый подошедший фильтр забирает сделку — НО ОТДЕЛЬНО ПО КАЖДОМУ
            # АДРЕСАТУ: два письма об одном принте в один чат — шум, а вот
            # разные каналы («Р5» и «Ф5») ждут одну и ту же сделку каждый у
            # себя. Раньше ключом был только пользователь, и более широкий
            # фильтр (Ф5, порог 1 млн) забирал сделку себе, а канал Р5 (порог
            # 50 млн) не получал НИЧЕГО ни разу.
            taken: set = set()
            for f in fs:
                dest = f.get("tg_target_id")
                if dest in taken:
                    continue
                if signals.block_matches(r, meta, f["params"], today):
                    taken.add(dest)
                    routed.setdefault((u, f["id"], f["name"], f["sound"],
                                       f["desktop"]), []).append(r)
    if not routed:
        await run_bg(mark_alerted, seen_ids)
        return 0

    # Спред сделки: «блок на 300 млн» без уровня ничего не говорит — важно, по
    # какому Y-IDX его забрали. Обычно он уже посчитан тем же тактом демона
    # (price_new_trades идёт сразу после sweep); досчитываем только хвост —
    # сделок в очереди звонка единицы, это дёшево.
    hot = {r["trade_id"]: r for rs in routed.values() for r in rs}
    todo = [r for r in hot.values() if r.get("y_idx_bps") is None]
    if todo:
        from services import trade_yidx
        await trade_yidx.for_rows(todo)

    def _match(r: dict) -> dict:
        lb = signals.block_meta(labels, r["isin"], _mx)
        return {
            # срок — до ГОРИЗОНТА ПРАЙСИНГА: «блок на 600 млн» читается
            # по-разному для годовой бумаги и для десятилетней, и та же
            # методика стоит в фильтре и в мониторе
            "maturity": lb.get("maturity"), "years": signals.meta_years(lb, today),
            # id сделки едет в уведомление: по нему телеграм отличает соседние
            # принты друг от друга (одно сообщение на сделку, см. tg_notify._group)
            "trade_id": r.get("trade_id"), "isin": r["isin"],
            "name": lb.get("name") or names.get(r["isin"]) or r["isin"],
            "price": r["price"], "money_rub": r["value"],
            "val_bps": r.get("y_idx_bps"),      # тем же ключом, что у алертов стакана
            "board": r["board"], "negotiated": r["market"] == "ndm",
            "side": r["side"], "ts": r["ts"], "reason": "block",
            "rating": lb.get("rating"), "fired_at": now,
            "base": lb.get("base"), "margin_bps": lb.get("margin_bps"),
            "cpy": lb.get("coupons_per_year"),
        }

    payloads = {k: [_match(r) for r in rs] for k, rs in routed.items()}

    def _persist():
        with _lock, _connect() as c:
            c.executemany(
                "INSERT INTO signal_events(filter_id,user_email,isin,name,side,"
                "price,money_rub,val_bps,board,negotiated,reason,fired_at,seen) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'block',?,0)",
                [(fid, u, m["isin"], m["name"], m["side"], m["price"],
                  m["money_rub"], m["val_bps"], m["board"],
                  1 if m["negotiated"] else 0, now)
                 for (u, fid, _n, _s, _d), ms in payloads.items() for m in ms])
    await run_bg(_persist)

    from services import tg_notify
    for (u, fid, fname, snd, desk), ms in payloads.items():
        await wsmod.manager.broadcast_signal(u, {
            "type": "block", "filter_id": fid, "filter_name": fname,
            "side": None, "sound": bool(snd), "desktop": bool(desk), "matches": ms,
        })
        # копия в привязанный телеграм-чат (буфер, отправка пачкой)
        tg_notify.enqueue_signal(u, fid, fname, None, ms, kind="block",
                                 target_id=targets.get(fid))
    await run_bg(mark_alerted, seen_ids)
    return len(hot)


# Сколько минут «свежая» сделка имеет право ехать до записи. Живой тик Alor
# доезжает за секунды; всё, что дольше, пришло ISS-дрейном с его 15 минутами.
LIVE_CAPTURE_SEC = float(os.getenv("LIVE_CAPTURE_SEC", "60"))


def live_capture(minutes: int = 60, min_value: float = 1_000_000.0) -> dict:
    """Доля КРУПНЫХ БИРЖЕВЫХ сделок, пойманных живьём за последние `minutes`.

    Прямой измеритель того, работает ли стрим НА САМОМ ДЕЛЕ: сокеты могут быть
    подняты, тики капать, а часть бумаг при этом молча ехать через ISS. Считаем
    по разнице «время сделки → время записи» и только по безадресным (РПС Alor
    не отдаёт вовсе, там 15 минут — норма).

    Свежий хвост окна отбрасываем: сделку, случившуюся минуту назад, ISS ещё не
    привозил, и она посчиталась бы «живой» просто потому, что альтернативы не
    было. → {total, live, ratio} (ratio=None, если считать не на чем)."""
    now = int(time.time())
    lo = now - minutes * 60
    hi = now - int(ALERT_MAX_AGE_MIN * 60)      # хвост окна ещё не разрешился
    with _connect() as c:
        rows = c.execute(
            "SELECT ts, ins_at FROM block_trade WHERE ins_at>=? AND ins_at<=? "
            "AND market!='ndm' AND value>=?", (lo, hi, min_value)).fetchall()
    total = live = 0
    for r in rows:
        try:
            t = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_MSK)
        except (ValueError, TypeError):
            continue
        total += 1
        if r["ins_at"] - t.timestamp() < LIVE_CAPTURE_SEC:
            live += 1
    return {"total": total, "live": live,
            "ratio": (live / total) if total else None}


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
            "archive_min_rub": BLOCK_ARCHIVE_MIN_RUB, "raw_days": BLOCK_RAW_DAYS,
            "yidx_min_rub": BLOCK_YIDX_MIN_RUB,
            "cursors": [dict(r) for r in cur]}
