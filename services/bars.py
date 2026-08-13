"""Часовые бары по бумаге: средневзвешенная цена часа + спред по ней.

Цена — часовые свечи MOEX ISS (interval=60): vwap = value/volume/face*100, то
есть та же средневзвешенная, что дневной WAPRICE, только с часовым шагом
(сверено на RU000A109K40 2026-07-20: Σчасов → 99.9715 против WAPRICE 99.97).
Номинал берётся из дневной истории (FACEVALUE), иначе амортизируемые бумаги
дали бы съехавший процент.

Спред считается не только по vwap, но и по каждой цене бара (y_open/high/low/
close_bps) — иначе свеча спреда на графике собиралась из vwap соседних часов и в
день с одним-двумя торговавшими часами вырождалась в палку. Спред обратен цене:
y_high_bps — спред по МАКСИМАЛЬНОЙ цене, то есть минимальный спред бара.

Спред: сегодняшние бары прайсятся живой моделью (build_metrics_fn), ПРОШЛЫЕ дни —
честным as-of (backdate.asof_bar_metrics: кривая/НКД/номинал того дня). Раньше
всё окно прайсилось сегодняшней моделью — у бумаг близко к погашению это давало
сотни bps мусора на исторических датах (Магнит5Р03: −697 в мае при честных +80).
Пересчёт прошлого дорог → ensure_bars делает его фоном, а стейл-спреды прошлых
версий (metrics_ver) сразу занулит: панель падает на дневную honest-серию.

Глубина: свечи ISS отдают часы на годы назад, ограничения ~30 дней (как у тикового
архива Alor) здесь нет. buy_*/sell_* и trades — из тикового архива, см.
services/trades_archive.enrich_bars_with_ticks.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

_ISS = "https://iss.moex.com/iss"
_HISTORY_PAGE = 100          # ISS отдаёт history страницами по 100 строк
_DEFAULT_FACE = 1000.0


# ─────────────────────────── загрузка из ISS ───────────────────────────

async def fetch_hour_candles(client: httpx.AsyncClient, secid: str, board: str,
                             frm: str, till: str) -> list[dict]:
    """Часовые свечи [frm, till] (ISO-даты). Время МСК, цены — % номинала."""
    from services.market_data import _moex_get
    out: list[dict] = []
    start = 0
    while True:
        r = await _moex_get(
            client,
            f"{_ISS}/engines/stock/markets/bonds/boards/{board}/securities/{secid}/candles.json",
            params={"from": frm, "till": till, "interval": 60, "start": start,
                    "iss.meta": "off"},
            timeout=25.0)
        if r is None or r.status_code != 200:
            break
        c = r.json().get("candles", {})
        cols, rows = c.get("columns", []), c.get("data", [])
        if not rows:
            break
        out.extend(dict(zip(cols, row)) for row in rows)
        if len(rows) < 500:      # страница свечей ISS = 500
            break
        start += len(rows)
    return out


async def fetch_daily_face(client: httpx.AsyncClient, secid: str, board: str,
                           frm: str, till: str) -> dict[str, float]:
    """{'YYYY-MM-DD': FACEVALUE} из дневной истории. Номинал амортизируемых
    бумаг меняется — без него vwap в процентах поедет."""
    from services.market_data import _moex_get
    out: dict[str, float] = {}
    start = 0
    while True:
        r = await _moex_get(
            client,
            f"{_ISS}/history/engines/stock/markets/bonds/boards/{board}/securities/{secid}.json",
            params={"from": frm, "till": till, "start": start, "iss.meta": "off",
                    "iss.only": "history",
                    "history.columns": "TRADEDATE,FACEVALUE"},
            timeout=25.0)
        if r is None or r.status_code != 200:
            break
        h = r.json().get("history", {})
        cols, rows = h.get("columns", []), h.get("data", [])
        if not rows:
            break
        di, fi = cols.index("TRADEDATE"), cols.index("FACEVALUE")
        for row in rows:
            if row[di] and row[fi]:
                out[row[di]] = float(row[fi])
        if len(rows) < _HISTORY_PAGE:
            break
        start += len(rows)
    return out


def _face_for(faces: dict[str, float], day: str, fallback: float) -> float:
    """Номинал на день бара. Дня нет в истории (свеча вечерней сессии, отнесённая
    биржей к следующему торговому дню) — берём ближайший предыдущий."""
    f = faces.get(day)
    if f:
        return f
    prev = [d for d in faces if d <= day]
    return faces[max(prev)] if prev else fallback


def tick_vwap_hours(isin: str, day: str) -> dict[str, float]:
    """{'YYYY-MM-DD HH:00': Σ(price·qty)/Σqty} по тиковому архиву за день.

    Цена тика Alor уже в % номинала — такой средневзвес не зависит от FACEVALUE
    и потому верен в день, когда номинал изменился (см. _implied_face)."""
    with _connect() as c:
        rows = c.execute(
            "SELECT substr(ts,1,13)||':00' h, SUM(price*qty) n, SUM(qty) q "
            "FROM trade_tick WHERE isin=? AND ts>=? AND ts<? GROUP BY h",
            (isin, day, day + " 24")).fetchall()
    return {r["h"]: r["n"] / r["q"] for r in rows if r["q"]}


def _implied_face(candles_of_day: list[dict]) -> Optional[float]:
    """Номинал дня, выведенный из самих свечей: у часа с ЕДИНОЙ ценой (high==low)
    value/volume — это ровно цена одной бумаги в рублях, значит
    face = value/volume/(цена%/100).

    Нужен, потому что FACEVALUE за СЕГОДНЯ ISS не отдаёт: дневная строка history
    появляется после закрытия, а /securities до конца дня показывает вчерашний
    номинал. У бумаг с амортизацией сегодня или с валютным/индексируемым
    номиналом (пересчитывается ежедневно) вчерашний номинал уводил сегодняшний
    средневзвес на проценты — вплоть до 11 пп мимо диапазона сделок дня."""
    vals = []
    for c in candles_of_day:
        vol, val, h, l = c.get("volume"), c.get("value"), c.get("high"), c.get("low")
        if vol and val and h and l and h == l:
            vals.append(val / vol / (h / 100))
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


# ─────────────────────────── сборка баров ───────────────────────────

async def build_bars(isin: str, days: int = 30, kind: str = "floater",
                     board: Optional[str] = None, with_metrics: bool = True) -> list[dict]:
    """Часовые бары бумаги за последние `days` календарных дней (без записи в БД).
    with_metrics=False — только цена/объём (быстро, без загрузки модели бумаги)."""
    from services.backdate import resolve_market

    secid, brd = await resolve_market(isin, board)
    secid, brd = secid or isin, brd or "TQCB"
    till = date.today().isoformat()
    frm = (date.today() - timedelta(days=days)).isoformat()

    async with httpx.AsyncClient() as client:
        candles, faces = await asyncio.gather(
            fetch_hour_candles(client, secid, brd, frm, till),
            fetch_daily_face(client, secid, brd, frm, till))
    if not candles:
        return []

    metrics_fn = None
    asof_fn = None
    if with_metrics:
        from services.orderbook_svc import build_metrics_fn
        try:
            metrics_fn, _calc_date, face_ref = await build_metrics_fn(isin, kind)
        except Exception as e:      # модель не собралась — отдаём бары без спреда
            logger.warning("bars %s: модель не загрузилась (%s) — только цена", isin, e)
            face_ref = None
        # прошлые дни — честный as-of (пока только флоатеры: у фиксов нет
        # архива G-кривой, их g-спред остаётся оценкой сегодняшней моделью)
        if kind == "floater" and metrics_fn is not None and days > 0:
            from services.backdate import asof_bar_metrics
            try:
                asof_fn = await asof_bar_metrics(isin, days, board)
            except Exception as e:
                logger.warning("bars %s: as-of модель не собралась (%s) — "
                               "прошлые дни сегодняшней моделью", isin, e)
    else:
        face_ref = None
    fallback_face = float(face_ref) if face_ref else _DEFAULT_FACE
    today_iso = date.today().isoformat()

    # Сегодняшний номинал ISS не отдаёт (см. _implied_face), поэтому цена
    # сегодняшних баров считается БЕЗ него: сначала по тикам архива (цена тика уже
    # в % номинала), затем по номиналу, выведенному из самих свечей дня.
    tick_vwap = await asyncio.to_thread(tick_vwap_hours, isin, today_iso)
    today_face = _implied_face([c for c in candles
                                if str(c.get("begin") or "")[:10] == today_iso])

    # reprice уровней — чистый CPU: в event loop он вставал бы на десятки мс на
    # бумагу × весь обход демона (то самое «сайт периодически подвисает»)
    def _crunch() -> list[dict]:
        memo: dict[tuple, dict] = {}
        bars: list[dict] = []
        for c in candles:
            vol, val, begin = c.get("volume"), c.get("value"), c.get("begin")
            if not begin:
                continue
            ts = str(begin)[:13] + ":00"          # 'YYYY-MM-DD HH:00'
            day = ts[:10]
            face = _face_for(faces, day, fallback_face)
            if day == today_iso and today_face:
                face = today_face
            tv = tick_vwap.get(ts) if day == today_iso else None
            vwap = (round(tv, 4) if tv
                    else round(val / vol / face * 100, 4) if vol and val and face
                    else c.get("close"))
            # прошлый день → честный as-of того дня; сегодня → живая модель
            use_asof = asof_fn is not None and day < today_iso

            def _metrics(price) -> dict:
                """Спред по одной цене. memo — (день, округлённая цена): внутри
                дня open/high/low/close часов повторяются, и 4 точки на бар почти
                не добавляют счёта поверх прежней одной."""
                if price is None or (metrics_fn is None and not use_asof):
                    return {}
                key = round(float(price), 3)
                mkey = (day if use_asof else "", key)
                m = memo.get(mkey)
                if m is None:
                    try:
                        m = (asof_fn(day, key) if use_asof else metrics_fn(key)) or {}
                    except Exception:
                        m = {}
                    memo[mkey] = m
                return m

            m = _metrics(vwap)
            # спред по каждой цене бара: без них свеча спреда собиралась из vwap
            # соседних часов и в малоликвидный день вырождалась в одну-две точки
            spread_key = "g_spread_bps" if kind == "fixed" else "y_idx_bps"
            o, h, l, cl = (c.get("open"), c.get("high"), c.get("low"), c.get("close"))
            bars.append({
                "isin": isin, "ts": ts, "kind": kind,
                "open": o, "high": h, "low": l, "close": cl,
                "vwap_pct": vwap, "volume": vol, "value": val, "face": face,
                "y_idx_bps": m.get("y_idx_bps"), "dm_bps": m.get("dm_bps"),
                "g_spread_bps": m.get("g_spread_bps"), "ytm": m.get("yield_pct"),
                "y_open_bps": _metrics(o).get(spread_key),
                "y_high_bps": _metrics(h).get(spread_key),
                "y_low_bps": _metrics(l).get(spread_key),
                "y_close_bps": _metrics(cl).get(spread_key),
                "metrics_ver": BARS_METRICS_VERSION,
            })
        return bars

    from services.heavy import run_heavy
    return await run_heavy(_crunch)


# Версия модели спреда в барах. Поднять при правке, меняющей цифру спреда бара:
# бары старых версий занулят спред и пересчитаются фоном при первом запросе.
#   NULL — «цена × модель дня записи» (candle-est, до 2026-08-11)
#   1    — прошлые дни честным as-of (asof_bar_metrics), сегодня живой моделью
#   2    — realized-гибрид якорится на первую архивную кривую (скачок на
#          границе архива котировок убран; HONEST_ENGINE_VERSION=4)
#   3    — 2026-08-12: бары ВЫХОДНОЙ СЕССИИ в день выплаты купона считались с
#          НКД старого периода почти в полный купон (history-строки за выходной
#          нет, _accrue_to_date при смене периода с неопубликованным купоном
#          оставлял факт пятницы) → dirty завышен на купон, спред улетал в минус
#          на сотни bps. 495 бумаг / 3152 бара в окне 95 дней. В ТОРГОВЫЙ день
#          выплаты тот же дефект был мягче — НКД на поставку не доначислялся
#          (accrue_to_settle, elapsed=0). HONEST_ENGINE_VERSION=5
#   4    — 2026-08-13: цена СЕГОДНЯШНЕГО бара считалась через FACEVALUE, которого
#          за сегодня у ISS ещё нет (брался вчерашний) → у бумаг с амортизацией
#          сегодня и с валютным/индексируемым номиналом средневзвес уезжал на
#          проценты мимо диапазона сделок дня (RU000A108C58: 89.44 при сделках
#          100.46–101.14). Теперь сегодня считается по тикам, номинал —
#          выведенный из свечей (_implied_face). Бампом версии перестраиваются
#          дни, записанные кривыми, пока они были «сегодня».
BARS_METRICS_VERSION = 4

_COLS = ("isin", "ts", "kind", "open", "high", "low", "close", "vwap_pct",
         "volume", "value", "face", "y_idx_bps", "dm_bps", "g_spread_bps", "ytm",
         "y_open_bps", "y_high_bps", "y_low_bps", "y_close_bps", "metrics_ver")


def upsert_bars(bars: list[dict]) -> int:
    """Пишет бары (src='candle'). Перезаписывает цену/спред, но НЕ трогает
    tick-поля (trades/buy_*/sell_*) существующей строки — их наполняет
    trades_archive и повторный прогон свечей не должен их стирать."""
    if not bars:
        return 0
    rows = [tuple(b.get(k) for k in _COLS) for b in bars]
    ph = ",".join("?" * len(_COLS))
    upd = ",".join(f"{k}=excluded.{k}" for k in _COLS[2:])
    with _lock, _connect() as c:
        c.executemany(
            f"INSERT INTO bar_hourly({','.join(_COLS)},src) VALUES({ph},'candle') "
            f"ON CONFLICT(isin,ts) DO UPDATE SET {upd}", rows)
    return len(rows)


def _null_stale_spreads(isin: str, frm: str, till_day: str) -> int:
    """Зануляет спред-поля баров ПРОШЛЫХ версий движка в окне [frm, till_day):
    candle-est мусор не должен рисоваться, пока фоновый honest-пересчёт не дошёл.
    Цена/объём остаются — слой средневзвеса живёт."""
    with _lock, _connect() as c:
        cur = c.execute(
            "UPDATE bar_hourly SET y_idx_bps=NULL, dm_bps=NULL, g_spread_bps=NULL, "
            "ytm=NULL, y_open_bps=NULL, y_high_bps=NULL, y_low_bps=NULL, "
            "y_close_bps=NULL WHERE isin=? AND ts>=? AND ts<? "
            "AND (metrics_ver IS NULL OR metrics_ver<?) "
            "AND (y_idx_bps IS NOT NULL OR g_spread_bps IS NOT NULL "
            "     OR y_close_bps IS NOT NULL)",
            (isin, frm, till_day, BARS_METRICS_VERSION))
        return cur.rowcount or 0


def _covered_from(isin: str) -> Optional[str]:
    """Самый ранний ts бара текущей версии метрик (None — таких нет)."""
    with _connect() as c:
        r = c.execute(
            "SELECT min(ts) FROM bar_hourly WHERE isin=? AND metrics_ver>=?",
            (isin, BARS_METRICS_VERSION)).fetchone()
    return r[0] if r and r[0] else None


_bg_backfill: set = set()               # isin'ы с уже запущенным фоновым пересчётом
_bg_sem = asyncio.Semaphore(2)          # честный as-of сетевой и тяжёлый — не флудим


def _day_covered(isin: str, day: str) -> bool:
    with _connect() as c:
        r = c.execute(
            "SELECT 1 FROM bar_hourly WHERE isin=? AND ts>=? AND ts<? "
            "AND metrics_ver>=? LIMIT 1",
            (isin, day, day + " 24", BARS_METRICS_VERSION)).fetchone()
    return r is not None


async def ensure_bars(isin: str, days: int = 30, kind: str = "floater",
                      board: Optional[str] = None, wait_past: bool = False) -> int:
    """Инкрементально держит бары окна свежими. Инлайн — только хвост
    (сегодня; вчера — если ещё не покрыт текущей версией метрик). Прошлые дни
    без метрик текущей версии — фоновым пересчётом (честный as-of, для длинного
    окна — минуты): роут не ждёт, спред-панель до его конца живёт на дневной
    honest-серии (стейл-спреды прошлых версий зануляются сразу). Идемпотентно."""
    frm = (date.today() - timedelta(days=days)).isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    stale = await asyncio.to_thread(_null_stale_spreads, isin, frm, yesterday)
    if stale:
        logger.info("bars %s: занулено %d стейл-спредов старой версии", isin, stale)

    # хвост инлайн: сегодня живой моделью; вчера (as-of) — только если не покрыт
    tail_days = 0 if await asyncio.to_thread(_day_covered, isin, yesterday) else 1
    tail = await build_bars(isin, min(days, tail_days), kind, board)
    n = await asyncio.to_thread(upsert_bars, tail)

    covered = await asyncio.to_thread(_covered_from, isin)
    need_past = days > 1 and (stale > 0 or covered is None or covered[:10] > frm)
    if need_past and isin not in _bg_backfill:
        _bg_backfill.add(isin)

        async def _past():
            try:
                async with _bg_sem:
                    bars = await build_bars(isin, days, kind, board)
                    m = await asyncio.to_thread(upsert_bars, bars)
                logger.info("bars honest backfill %s: %d строк (%d дн)", isin, m, days)
                return m
            except Exception as e:
                logger.warning("bars honest backfill %s: %s", isin, e)
                return 0
            finally:
                _bg_backfill.discard(isin)

        if wait_past:
            # разовый бэкфилл-скрипт: процесс не должен выйти раньше пересчёта
            n += await _past()
        else:
            asyncio.create_task(_past())
    return n


def read_bars(isin: str, frm: Optional[str] = None, till: Optional[str] = None,
              limit: int = 5000) -> list[dict]:
    """Бары по возрастанию времени. frm/till — 'YYYY-MM-DD' либо полный ts."""
    q = "SELECT * FROM bar_hourly WHERE isin=?"
    args: list = [isin]
    if frm:
        q += " AND ts >= ?"
        args.append(frm)
    if till:
        q += " AND ts <= ?"
        args.append(till + " 23:59" if len(till) == 10 else till)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _connect() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r) for r in reversed(rows)]


def last_bar_ts(isin: str) -> Optional[str]:
    with _connect() as c:
        r = c.execute("SELECT MAX(ts) t FROM bar_hourly WHERE isin=?", (isin,)).fetchone()
    return r["t"] if r and r["t"] else None


async def universe_targets(kinds: tuple = ("floater", "fixed")) -> list[tuple[str, str]]:
    """[(isin, kind)] всех бумаг, по которым имеет смысл держать бары."""
    out: list[tuple[str, str]] = []
    if "floater" in kinds:
        from services import instruments_registry
        try:
            uni = await instruments_registry.fetch_floater_universe()
            out += [(u["isin"], "floater") for u in uni if u.get("isin")]
        except Exception as e:
            logger.warning("universe_targets floaters: %s", e)
    if "fixed" in kinds:
        from services import fixed_income as fi
        from services.market_data import market_cache
        try:
            uni = market_cache.get("fixed_universe") or await fi.fetch_fixed_universe()
            out += [(u["isin"], "fixed") for u in uni if u.get("isin")]
        except Exception as e:
            logger.warning("universe_targets fixed: %s", e)
    seen: set = set()
    return [(i, k) for i, k in out if not (i in seen or seen.add(i))]


# ADV (средний дневной оборот) считается по всему рынку одним запросом и живёт
# в памяти: архив баров дописывается раз в час, а /api/bonds зовётся каждым
# рефрешем таблицы — пересчитывать на каждый вызов незачем.
_ADV_TTL_SEC = 900.0
_adv_cache: dict = {"key": None, "at": 0.0, "map": {}}


def adv_map(days: int = 30, kind: Optional[str] = None) -> dict:
    """ISIN → средний ДНЕВНОЙ оборот за окно, ₽ (из архива часовых баров).

    Знаменатель — число ТОРГОВЫХ дней РЫНКА в окне (даты, где есть хоть один
    бар по любой бумаге), а не дней, когда торговалась эта бумага: иначе
    неликвид с одной сделкой в месяц показывал бы оборот на уровне ОФЗ.
    Деньги — `value` бара (руб. без НКД, из свечей ISS), поэтому число
    сопоставимо с VALTODAY дня по порядку, но не обязано совпадать копейка
    в копейку."""
    import time
    key = (days, kind)
    now = time.monotonic()
    if _adv_cache["key"] == key and now - _adv_cache["at"] < _ADV_TTL_SEC:
        return _adv_cache["map"]

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    q_sum = "SELECT isin, SUM(value) v FROM bar_hourly WHERE ts >= ?"
    q_days = "SELECT COUNT(DISTINCT substr(ts,1,10)) d FROM bar_hourly WHERE ts >= ?"
    args: list = [cutoff]
    if kind:
        q_sum += " AND kind = ?"
        q_days += " AND kind = ?"
        args.append(kind)
    q_sum += " GROUP BY isin"
    with _connect() as c:
        rows = c.execute(q_sum, args).fetchall()
        d = c.execute(q_days, args).fetchone()
    tdays = (d["d"] if d else 0) or 0
    out = {r["isin"]: r["v"] / tdays for r in rows
           if r["v"] is not None} if tdays else {}
    _adv_cache.update(key=key, at=now, map=out)
    return out


# База спреда — тот же профиль обращения, что у ADV: считается по всему рынку
# одним запросом и живёт в памяти, потому что архив баров дописывается раз в час.
_SPREAD_AVG_TTL_SEC = 900.0
_spread_avg_cache: dict = {"key": None, "at": 0.0, "map": {}}


def spread_avg_map(days: int = 7, kind: Optional[str] = None) -> dict:
    """ISIN → средневзвешенный спред за ПРЕДЫДУЩИЕ `days` дней, bps.

    Взвешивание — оборотом бара (`value`), метрика — спред по средневзвешенной
    цене часа (y_idx_bps у флоатеров, g_spread_bps у фиксов). Так база — это
    «где бумага реально торговалась», а не среднее по часам, где одна сделка
    весит столько же, сколько миллиардный час.

    Окно ЗАКАНЧИВАЕТСЯ вчера: сегодняшние сделки сравниваются с историей, а не
    сами с собой. Спред прошлых дней в баре — честный as-of того дня
    (см. BARS_METRICS_VERSION), поэтому база не переоценивается сегодняшней
    кривой."""
    import time
    key = (days, kind)
    now = time.monotonic()
    if _spread_avg_cache["key"] == key and now - _spread_avg_cache["at"] < _SPREAD_AVG_TTL_SEC:
        return _spread_avg_cache["map"]

    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    q = ("SELECT isin, SUM(COALESCE(y_idx_bps, g_spread_bps) * value) n, SUM(value) d "
         "FROM bar_hourly WHERE ts >= ? AND ts < ? AND value > 0 "
         "AND COALESCE(y_idx_bps, g_spread_bps) IS NOT NULL")
    args: list = [cutoff, today]
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    q += " GROUP BY isin"
    with _connect() as c:
        rows = c.execute(q, args).fetchall()
    out = {r["isin"]: r["n"] / r["d"] for r in rows if r["d"]}
    _spread_avg_cache.update(key=key, at=now, map=out)
    return out


def active_isins(days: int = 7) -> set:
    """Бумаги, у которых за последние `days` дней есть хоть один бар — то есть
    по ним реально идут сделки. Остальные (погашенные, неторгуемые остатки
    реестра) незачем опрашивать каждый час."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _connect() as c:
        rows = c.execute("SELECT DISTINCT isin FROM bar_hourly WHERE ts >= ?",
                         (cutoff,)).fetchall()
    return {r["isin"] for r in rows}


async def refresh_universe(days: int = 3, limit: Optional[int] = None,
                           with_ticks: bool = True, concurrency: int = 4,
                           kinds: tuple = ("floater", "fixed"),
                           progress_every: int = 50, full: bool = True,
                           refetch_ticks: bool = False,
                           progress_key: str = "bars_refresh",
                           progress_label: Optional[str] = None) -> dict:
    """Наливает бары (и тики) по всему юниверсу. Используется и часовым демоном
    (days=2..3, дозалив хвоста), и бэкфилл-скриптом (days=365, разовый прогон).

    full=False — только торгующиеся бумаги (есть бар за неделю): проход вместо
    ~33 минут занимает единицы минут. Полный проход нужен реже — им подхватываются
    новые выпуски и вернувшаяся ликвидность.

    refetch_ticks=True снимает водяной знак инкрементального дрейна и качает окно
    сделок заново. Обычному проходу это не нужно (знак и так отдаёт всё новое) —
    флаг для ремонта: если в архиве подозревается дыра."""
    targets = await universe_targets(kinds)
    if not full:
        act = active_isins()
        if act:
            targets = [(i, k) for i, k in targets if i in act]
    if limit:
        targets = targets[:limit]
    sem = asyncio.Semaphore(concurrency)
    stat = {"papers": len(targets), "bars": 0, "ticks": 0, "failed": 0}
    done = 0

    from services import progress
    progress.start(progress_key, progress_label or "Налив часовых баров",
                   total=len(targets),
                   detail=f"окно {days} дн · {'весь юниверс' if full else 'только торгующиеся'}")

    async def one(isin: str, kind: str):
        nonlocal done
        async with sem:
            try:
                stat["bars"] += await ensure_bars(isin, days=days, kind=kind,
                                                  wait_past=days > 7)
                if with_ticks:
                    from services import trades_archive as ta
                    stat["ticks"] += await ta.drain(isin, days=min(days, ta.ALOR_HISTORY_DAYS),
                                                    full=refetch_ticks)
                    # GROUP BY по тикам бумаги — синхронный SQLite: в loop он
                    # подвешивал сервер на каждый шаг часового обхода
                    await asyncio.to_thread(
                        ta.enrich_bars_with_ticks,
                        isin, frm=(date.today() - timedelta(days=days)).isoformat())
            except Exception as e:
                stat["failed"] += 1
                logger.warning("refresh %s: %s", isin, e)
            done += 1
            progress.advance(progress_key,
                             detail=f"строк {stat['bars']} · тиков {stat['ticks']}"
                                    + (f" · ошибок {stat['failed']}" if stat["failed"] else ""))
            if progress_every and done % progress_every == 0:
                logger.info("бары %d/%d · строк %d · тиков %d · ошибок %d",
                            done, len(targets), stat["bars"], stat["ticks"], stat["failed"])

    try:
        await asyncio.gather(*(one(i, k) for i, k in targets))
    finally:
        progress.finish(progress_key,
                        detail=f"строк {stat['bars']} · тиков {stat['ticks']} · ошибок {stat['failed']}")
    return stat


def resample(bars: list[dict], hours: int) -> list[dict]:
    """Склейка часовых баров в N-часовые (VWAP взвешенный по объёму, спред —
    пересчитан не будет: берём взвешенный по объёму средний спред баров)."""
    if hours <= 1 or not bars:
        return bars
    out: list[dict] = []
    bucket: list[dict] = []

    def flush():
        if not bucket:
            return
        vol = sum(b["volume"] or 0 for b in bucket)
        val = sum(b["value"] or 0 for b in bucket)
        face = bucket[-1].get("face") or _DEFAULT_FACE
        agg = {
            "isin": bucket[0]["isin"], "ts": bucket[0]["ts"], "kind": bucket[0]["kind"],
            "open": bucket[0]["open"], "close": bucket[-1]["close"],
            "high": max((b["high"] for b in bucket if b["high"] is not None), default=None),
            "low": min((b["low"] for b in bucket if b["low"] is not None), default=None),
            "volume": vol, "value": val, "face": face,
            "vwap_pct": round(val / vol / face * 100, 4) if vol and val else None,
            "trades": sum(b.get("trades") or 0 for b in bucket) or None,
        }
        for k in ("y_idx_bps", "dm_bps", "g_spread_bps", "ytm"):
            num = sum((b[k] or 0) * (b["volume"] or 0) for b in bucket if b.get(k) is not None)
            den = sum((b["volume"] or 0) for b in bucket if b.get(k) is not None)
            agg[k] = round(num / den, 2) if den else None
        # спред по ценам бара склеивается как сам бар: open первого, close
        # последнего, экстремумы — по всем четырём полям (спред обратен цене,
        # поэтому y_high это минимальный спред, а не максимальный)
        agg["y_open_bps"] = bucket[0].get("y_open_bps")
        agg["y_close_bps"] = bucket[-1].get("y_close_bps")
        ys = [b[k] for b in bucket
              for k in ("y_open_bps", "y_high_bps", "y_low_bps", "y_close_bps")
              if b.get(k) is not None]
        agg["y_high_bps"] = min(ys) if ys else None   # по максимальной цене
        agg["y_low_bps"] = max(ys) if ys else None    # по минимальной цене
        out.append(agg)

    cur_key = None
    for b in bars:
        dt_ = datetime.fromisoformat(b["ts"])
        key = (dt_.date(), dt_.hour // hours)
        if key != cur_key:
            flush()
            bucket, cur_key = [], key
        bucket.append(b)
    flush()
    return out
