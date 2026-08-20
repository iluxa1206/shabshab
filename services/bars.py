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
from datetime import date, datetime, timedelta, timezone
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


def _settle_face_fn(faces: dict[str, float], fallback: float):
    """Функция «день бара → номинал, по которому биржа считала VALUE его сделок».

    Это номинал ДАТЫ РАСЧЁТОВ (Т+1), а не дня заключения. В дни перед
    амортизацией сделка исполняется уже после списания части номинала: VALUE
    приходит по новому номиналу, а FACEVALUE дневной истории меняется только в
    саму дату амортизации. Итог — vwap = value/volume/face занижался ровно на
    шаг амортизации, и слой средневзвеса проваливался отвесной ступенькой при
    неподвижных свечах (БалтЛизП10 RU000A108777: 10–12.07.2026 88.2 против close
    98.0 — считалось по 1000 вместо 900; 08–10.08.2026 87.7 против 98.6 — по 900
    вместо 800; обе «дыры» кончались ровно в дату амортизации).

    Два шага, потому что выходные сессии биржа относит к следующему торговому
    дню: сначала день бара сводится к своей СЕССИИ (для субботы и воскресенья
    это понедельник — первый день, который есть в дневной истории), затем берётся
    номинал СЛЕДУЮЩЕГО за сессией торгового дня. Проверено на обоих провалах:
    пятница 07.08 → расчёты 10.08 (900, цена верна), суббота 08.08 → сессия
    10.08 → расчёты 11.08 (800).

    В обычные дни номинал соседних дней одинаков и сдвиг ничего не меняет. На
    правом крае окна следующего дня ещё нет — остаётся номинал самой сессии;
    сегодняшние бары и так считаются по тикам либо по номиналу, выведенному из
    самих свечей (_implied_face)."""
    import bisect
    days = sorted(faces)

    def face_of(day: str) -> float:
        if not days:
            return fallback
        i = bisect.bisect_left(days, day)        # сессия дня (выходной → пн)
        if i >= len(days):
            # день правее всей истории (сегодня): ближайший известный номинал
            return faces[days[-1]]
        j = i + 1                                 # расчёты: следующий торговый день
        return faces[days[j]] if j < len(days) else faces[days[i]]

    return face_of


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

# Порция потоковой выдачи баров (см. on_chunk в build_bars): ~2 недели торгов.
# Мельче — лишние транзакции на запись, крупнее — линия появляется рывками.
_CHUNK_BARS = 150


async def build_bars(isin: str, days: int = 30, kind: str = "floater",
                     board: Optional[str] = None, with_metrics: bool = True,
                     till: Optional[str] = None, on_chunk=None) -> list[dict]:
    """Часовые бары бумаги за последние `days` календарных дней (без записи в БД).
    with_metrics=False — только цена/объём (быстро, без загрузки модели бумаги).
    till — правая граница окна ('YYYY-MM-DD', по умолчанию сегодня): расширение
    окна графика досчитывает ТОЛЬКО недостающий кусок слева, а не весь диапазон
    заново (см. ensure_bars).

    on_chunk(bars) — колбэк потоковой выдачи: вызывается по ходу счёта, порциями
    примерно по две недели торгов, из heavy-потока (значит пишет синхронно, как
    upsert_bars). Нужен, чтобы посчитанные дни появлялись на графике сразу, а не
    все разом в конце длинного окна."""
    from services.backdate import resolve_market

    secid, brd = await resolve_market(isin, board)
    secid, brd = secid or isin, brd or "TQCB"
    till = till or date.today().isoformat()
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
    # номинал сделок дня — на дату расчётов (Т+1), см. _settle_face_fn
    face_of = _settle_face_fn(faces, fallback_face)
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
        chunk: list[dict] = []
        chunk_day: Optional[str] = None
        # ОТ СВЕЖИХ К СТАРЫМ при потоковой выдаче: человек смотрит правый край
        # графика, и заполняться он должен первым. Порядок самого результата
        # восстанавливаем в конце — потребители ждут хронологию.
        for c in (reversed(candles) if on_chunk is not None else candles):
            vol, val, begin = c.get("volume"), c.get("value"), c.get("begin")
            if not begin:
                continue
            ts = str(begin)[:13] + ":00"          # 'YYYY-MM-DD HH:00'
            day = ts[:10]
            face = face_of(day)
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
            # HLC-режим панели спреда убран (2026-08-14): свеча спреда читалась
            # плохо, а стоила ЧЕТЫРЁХ reprice на бар. Но спред ПО ЗАКРЫТИЮ нужен
            # и без неё: панель считает базу по цене закрытия, когда слой
            # СРЕДНЕВЗВЕС выключен. Без y_close_bps фронт молча падал на
            # vwap-спред — переключатель менял подпись, а линия оставалась той
            # же. Считаем одну доп. цену вместо четырёх; o/h/l больше не нужны.
            o, h, l, cl = (c.get("open"), c.get("high"), c.get("low"), c.get("close"))
            spread_key = "g_spread_bps" if kind == "fixed" else "y_idx_bps"
            y_close = _metrics(cl).get(spread_key) if cl is not None else None
            bar = {
                "isin": isin, "ts": ts, "kind": kind,
                "open": o, "high": h, "low": l, "close": cl,
                "vwap_pct": vwap, "volume": vol, "value": val, "face": face,
                "y_idx_bps": m.get("y_idx_bps"), "dm_bps": m.get("dm_bps"),
                "g_spread_bps": m.get("g_spread_bps"), "ytm": m.get("yield_pct"),
                "y_open_bps": None, "y_high_bps": None, "y_low_bps": None,
                "y_close_bps": y_close,
                # ВЕРСИЮ ШТАМПУЕМ ТОЛЬКО НА ПОСЧИТАННЫЙ БАР. Строка без спреда,
                # помеченная текущей версией, считается «посчитанной» и больше
                # не пересчитывается никогда: при одном флаке ISS (пустая
                # история → as-of молча отвечает {}) бумага получала недели
                # пустой линии (ВЭБP-41, 01-13.08). Без штампа её подберёт
                # следующий заход, а от молотьбы в течение дня прикрывает
                # _past_depth.
                "metrics_ver": (BARS_METRICS_VERSION
                                if (not with_metrics
                                    or m.get("y_idx_bps") is not None
                                    or m.get("g_spread_bps") is not None)
                                else None),
                "horizon": m.get("horizon"),
                "y_idx_alt_bps": m.get("y_idx_alt_bps"),
                "alt_horizon": m.get("alt_horizon"),
            }
            bars.append(bar)
            # ПОРЦИОННАЯ ВЫДАЧА: посчитанные дни отдаются наружу сразу, не
            # дожидаясь конца окна. Без неё линия спреда на длинном окне
            # появлялась разом через минуты — человек не видел, считается
            # вообще что-нибудь или нет. Режем по границе дня и накопленному
            # объёму: писать каждый бар отдельно — лишние транзакции.
            if on_chunk is not None:
                if chunk_day is None:
                    chunk_day = day
                elif day != chunk_day:
                    if len(chunk) >= _CHUNK_BARS:
                        on_chunk(chunk)
                        chunk = []
                    chunk_day = day
                chunk.append(bar)
        if on_chunk is not None and chunk:
            on_chunk(chunk)
        if on_chunk is not None:
            bars.sort(key=lambda b: b["ts"])
        return bars

    def _warn_if_unpriced(bars: list[dict]) -> list[dict]:
        """Диагностика: окно посчитано, а спреда нет НИ У ОДНОГО бара — значит
        модель или as-of не отработали (флак сети, пустая история MOEX)."""
        if with_metrics and bars and not any(
                b.get("y_idx_bps") is not None or b.get("g_spread_bps") is not None
                for b in bars):
            logger.warning("bars %s: спред не посчитан ни по одному бару из %d — "
                           "версию не штампуем, пересчитаем позже", isin, len(bars))
        return bars

    from services.heavy import run_heavy
    return _warn_if_unpriced(await run_heavy(_crunch))


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
#   5    — 2026-08-13: спред баров считался ВСЕГДА к погашению, а шапка, стакан
#          и лента сделок — к горизонту по правилу цены (pick_horizon). У бумаг
#          с офертой это две разные метрики на одном экране: РЖД 1Р-52R — put
#          09.10.2029 против погашения 31.03.2036. Теперь горизонт общий.
#   6    — 2026-08-13: рядом со спредом выбранного горизонта пишется спред ко
#          ВТОРОМУ (погашение ↔ ближайшая оферта) — свитчер на графике
#          переключает готовые числа, без пересчёта года истории.
#   7    — 2026-08-14: эхо неопределённых купонов источника (см.
#          coupon_calib.strip_undetermined_values, HONEST_ENGINE_VERSION=7).
#          Спред баров считался с замороженной ставкой на всём хвосте купонов —
#          сдвиг до 200 bps, старые бары несопоставимы с новыми.
#   8    — 2026-08-20: база Y-IDX (роллирование RUONIA) на прошлую дату замирала
#          на уровне первого дня факт-сегмента гибрида as-of — см.
#          HONEST_ENGINE_VERSION=8. Спред баров тем сильнее занижен, чем дальше
#          дата: МБЭС 2P-02 год назад 48 bps вместо 239.
BARS_METRICS_VERSION = 8

_COLS = ("isin", "ts", "kind", "open", "high", "low", "close", "vwap_pct",
         "volume", "value", "face", "y_idx_bps", "dm_bps", "g_spread_bps", "ytm",
         "y_open_bps", "y_high_bps", "y_low_bps", "y_close_bps", "metrics_ver",
         # горизонт бара и спред ко ВТОРОМУ горизонту (свитчер погашение↔оферта)
         "horizon", "y_idx_alt_bps", "alt_horizon")


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
            "y_close_bps=NULL, y_idx_alt_bps=NULL, horizon=NULL, alt_horizon=NULL "
            "WHERE isin=? AND ts>=? AND ts<? "
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


# ГЛУБИНА, ДО КОТОРОЙ ОКНО УЖЕ СЧИТАЛИ. Без этой памяти «покрыто ли окно»
# выводилось из самих баров (_covered_from — дата самого раннего бара текущей
# версии), а это ВРЁТ, когда данных левее просто не существует: у выпуска с
# историей короче запрошенного окна (или за границей глубины ISS) covered
# навсегда правее frm, условие «не покрыто» не выполнялось никогда, и КАЖДЫЙ
# запрос графика запускал полный пересчёт всего окна. На проде это крутилось по
# кругу каждые 40 секунд (2975 строк × 226 дней на бумагу).
# Ключ по дню и версии метрик: новый день досчитывает хвост, бамп версии — всё.
_past_depth: dict[str, tuple] = {}      # isin → (день, версия, самая ранняя frm)


_bg_backfill: set = set()               # isin'ы с уже запущенным фоновым пересчётом
_bg_sem = asyncio.Semaphore(2)          # честный as-of сетевой и тяжёлый — не флудим


def _unpriced_in_window(isin: str, frm: str, till: str) -> int:
    """Сколько баров окна имеют цену, но НЕ имеют спреда текущей версии.

    Одной даты «самого раннего посчитанного бара» (_covered_from) мало: она
    ничего не знает о ДЫРАХ в середине. ВЭБP-41 — покрытие «есть с февраля»,
    а весь август пустой (одна оборванная выборка ISS), и пересчёт не
    запускался, потому что covered левее начала окна."""
    with _connect() as c:
        r = c.execute(
            "SELECT COUNT(*) FROM bar_hourly WHERE isin=? AND ts>=? AND ts<? "
            "AND vwap_pct IS NOT NULL AND (metrics_ver IS NULL OR metrics_ver<? "
            # спред по закрытию вернулся отдельным полем: бары, налитые без него,
            # тоже досчитываем — иначе панель в режиме «по цене закрытия» молча
            # показывала бы vwap-спред. Бамп версии тут не годится: он обнулил
            # бы ВЕСЬ спред окна и оставил пустую линию до конца пересчёта
            "     OR (y_close_bps IS NULL AND close IS NOT NULL AND kind='floater')) "
            "AND (y_idx_bps IS NULL OR y_close_bps IS NULL)",
            (isin, frm, till, BARS_METRICS_VERSION)).fetchone()
    return r[0] if r else 0


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
    # Дыры ВНУТРИ окна: бары с ценой, но без спреда (см. _unpriced_in_window)
    holes = await asyncio.to_thread(_unpriced_in_window, isin, frm, yesterday)
    # Считали ли это окно сегодня на текущей версии метрик (см. _past_depth).
    seen = _past_depth.get(isin)
    done_frm = (seen[2] if seen and seen[0] == date.today().isoformat()
                and seen[1] == BARS_METRICS_VERSION else None)
    need_past = days > 1 and (
        stale > 0                                  # в окне нашлись стейл-строки
        or (done_frm is None and holes > 0)        # дыры внутри окна
        or (done_frm is None and (covered is None or covered[:10] > frm))
        or (done_frm is not None and frm < done_frm))   # окно расширили влево
    if need_past and isin not in _bg_backfill:
        _bg_backfill.add(isin)
        if holes:
            logger.info("bars %s: %d баров окна без спреда — досчитываем", isin, holes)
        # Инкремент: если стейла и дыр нет, а часть окна посчитана — считаем
        # только НЕДОСТАЮЩИЙ кусок слева [frm, граница). Раньше расширение окна
        # (6 месяцев → YTD) пересчитывало весь диапазон, включая готовые дни.
        # При дырах внутри окна резать нельзя — они где угодно.
        edge = None
        if stale == 0 and holes == 0:
            known = [d for d in (done_frm, covered[:10] if covered else None) if d]
            edge = min(known) if known else None
        till_day = edge if edge and edge > frm else None

        async def _past():
            try:
                async with _bg_sem:
                    # пишем ПОРЦИЯМИ по ходу счёта: посчитанные дни видны на
                    # графике сразу (фронт переспрашивает, пока покрытие
                    # неполное), а не все разом через минуты
                    written = [0]

                    def _flush(part):
                        written[0] += upsert_bars(part)

                    bars = await build_bars(isin, days, kind, board, till=till_day,
                                            on_chunk=_flush)
                    m = written[0] or await asyncio.to_thread(upsert_bars, bars)
                logger.info("bars honest backfill %s: %d строк (%d дн%s)",
                            isin, m, days, f", досчёт до {till_day}" if till_day else "")
                # глубину помним, даже если строк не приехало: данных левее может
                # не быть вовсе, и повторять этот проход бессмысленно
                prev = _past_depth.get(isin)
                keep = (prev[2] if prev and prev[0] == date.today().isoformat()
                        and prev[1] == BARS_METRICS_VERSION else None)
                _past_depth[isin] = (date.today().isoformat(), BARS_METRICS_VERSION,
                                     min(frm, keep) if keep else frm)
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


# ── Дневная свёртка (bar_daily) ────────────────────────────────────────────
# Цена и спред каждого ЧАСА уже посчитаны и проштампованы metrics_ver, поэтому
# день — чистая агрегация: ни сети, ни солвера. Считается один раз и лежит в
# базе; повторный прогон трогает только новые дни, дни прошлой версии движка и
# дни, где с прошлой свёртки прибавилось оборота (дозалив хвоста, тики).
#
# Вес — ОБОРОТ ЧАСА В РУБЛЯХ: так средневзвес дня совпадает с определением
# средневзвеса внутри часа (value/volume/face), и час с одной сделкой не тянет
# цену дня наравне с часом на сто миллионов.

def _daily_rows(isin: str, frm: Optional[str] = None) -> list[dict]:
    """Свёртка часов бумаги в дни (без записи). frm — 'YYYY-MM-DD'."""
    q = ("SELECT substr(ts,1,10) d, kind, ts, vwap_pct, close, y_idx_bps, "
         "g_spread_bps, y_close_bps, volume, value, trades, metrics_ver "
         "FROM bar_hourly WHERE isin=?")
    args: list = [isin]
    if frm:
        q += " AND ts >= ?"
        args.append(frm)
    q += " ORDER BY ts"
    with _connect() as c:
        rows = c.execute(q, args).fetchall()

    acc: dict[str, dict] = {}
    for r in rows:
        d = r["d"]
        a = acc.get(d)
        if a is None:
            a = acc[d] = {"isin": isin, "date": d, "kind": r["kind"] or "floater",
                          "pw": 0.0, "w": 0.0, "yw": 0.0, "yws": 0.0,
                          "close_pct": None, "y_idx_close_bps": None,
                          "volume": 0.0, "value": 0.0, "trades": 0, "hours": 0,
                          "ver": None}
        w = r["value"] or 0.0
        # ВЕРСИЯ ДНЯ — МИНИМУМ ПО ЧАСАМ (NULL = 0, легаси до штампа). День,
        # собранный из часов старого движка, обязан выглядеть старым: иначе
        # свёртка навсегда застревает на «уже посчитано текущей версией» и не
        # пересобирается после пересчёта самих часов.
        hv = r["metrics_ver"] or 0
        a["ver"] = hv if a["ver"] is None else min(a["ver"], hv)
        a["volume"] += r["volume"] or 0.0
        a["value"] += w
        a["trades"] += r["trades"] or 0
        # ЗАКРЫТИЕ ДНЯ — последний час, где была цена (часы идут по возрастанию).
        # Спред закрытия берём из того же часа: y_close_bps посчитан ровно по
        # этой цене, а не по средневзвесу.
        if r["close"] is not None:
            a["close_pct"] = r["close"]
            a["y_idx_close_bps"] = r["y_close_bps"]
        # у ФИКСОВ в тех же часовых полях лежит g-спред (см. spread_key в
        # build_bars): y_idx_bps там пуст, а y_close_bps — это спред к погашению
        # по цене закрытия. Разводим по своим колонкам на свёртке.
        if w <= 0 or r["vwap_pct"] is None:
            continue          # час без оборота в средневзвес не идёт
        a["hours"] += 1
        a["pw"] += r["vwap_pct"] * w
        a["w"] += w
        sp = r["g_spread_bps"] if a["kind"] == "fixed" else r["y_idx_bps"]
        if sp is not None:
            a["yw"] += sp * w
            a["yws"] += w

    out = []
    for d, a in sorted(acc.items()):
        wap = a["pw"] / a["w"] if a["w"] > 0 else None
        ywap = a["yw"] / a["yws"] if a["yws"] > 0 else None
        ycl = a["y_idx_close_bps"]
        fix = a["kind"] == "fixed"
        out.append({
            "isin": isin, "date": d, "kind": a["kind"],
            "wap_pct": round(wap, 4) if wap is not None else None,
            "close_pct": a["close_pct"],
            "y_idx_wap_bps": None if fix or ywap is None else round(ywap, 1),
            "y_idx_close_bps": None if fix or ycl is None else round(ycl, 1),
            "g_spread_wap_bps": round(ywap, 1) if fix and ywap is not None else None,
            "g_spread_close_bps": round(ycl, 1) if fix and ycl is not None else None,
            "volume": a["volume"] or None, "value": a["value"] or None,
            "trades": a["trades"] or None, "hours": a["hours"],
            "ver": a["ver"] or 0,
        })
    return out


_DAILY_COLS = ("isin", "date", "kind", "wap_pct", "close_pct", "y_idx_wap_bps",
               "y_idx_close_bps", "g_spread_wap_bps", "g_spread_close_bps",
               "volume", "value", "trades", "hours")


def build_daily(isin: str, days: Optional[int] = None, force: bool = False) -> int:
    """Свернуть часы бумаги в bar_daily. Возвращает число ЗАПИСАННЫХ дней.

    Готовые дни не трогаются: пишем только те, которых нет, те, что посчитаны
    прошлой версией движка, и те, где оборот дня изменился (в часы дозалились
    сделки). Поэтому прогон по всему универсу безопасно гонять хоть каждую ночь —
    второй раз он ничего не считает."""
    frm = (date.today() - timedelta(days=days)).isoformat() if days else None
    rows = _daily_rows(isin, frm)
    if not rows:
        return 0
    with _connect() as c:
        q = "SELECT date, value, metrics_ver FROM bar_daily WHERE isin=?"
        args: list = [isin]
        if frm:
            q += " AND date >= ?"
            args.append(frm)
        have = {r["date"]: (r["value"], r["metrics_ver"]) for r in c.execute(q, args)}

    fresh = []
    for r in rows:
        prev = have.get(r["date"])
        if not force and prev is not None:
            old_val, ver = prev
            same_val = (old_val or 0) == (r["value"] or 0)
            # версия строки дня = версия его часов: совпала и оборот не менялся —
            # пересобирать нечего (в т.ч. у дней на легаси-часах: они пересоберутся
            # ровно тогда, когда пересчитают сами часы)
            if same_val and (ver or 0) == r["ver"]:
                continue
        fresh.append(r)
    if not fresh:
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ph = ",".join("?" * (len(_DAILY_COLS) + 2))
    upd = ",".join(f"{k}=excluded.{k}" for k in _DAILY_COLS[2:])
    data = [tuple(r.get(k) for k in _DAILY_COLS) + (r["ver"], now) for r in fresh]
    with _lock, _connect() as c:
        c.executemany(
            f"INSERT INTO bar_daily({','.join(_DAILY_COLS)},metrics_ver,built_at) "
            f"VALUES({ph}) ON CONFLICT(isin,date) DO UPDATE SET {upd}, "
            "metrics_ver=excluded.metrics_ver, built_at=excluded.built_at", data)
    return len(fresh)


def read_daily(isin: str, frm: Optional[str] = None) -> list[dict]:
    """Дневные строки бумаги по возрастанию даты — как есть, БЕЗ пересчёта."""
    q = "SELECT * FROM bar_daily WHERE isin=?"
    args: list = [isin]
    if frm:
        q += " AND date >= ?"
        args.append(frm)
    q += " ORDER BY date"
    with _connect() as c:
        return [dict(r) for r in c.execute(q, args)]


async def build_daily_universe(days: Optional[int] = None, limit: Optional[int] = None,
                               kinds: tuple = ("floater", "fixed"),
                               force: bool = False) -> dict:
    """Свёртка дней по всему юниверсу. Чистый SQLite — гоним последовательно в
    отдельном потоке, чтобы не держать loop."""
    targets = await universe_targets(kinds)
    if limit:
        targets = targets[:limit]
    stat = {"papers": len(targets), "days": 0, "failed": 0}

    from services import progress
    progress.start("bars_daily", "Свёртка дневных баров", total=len(targets),
                   detail=f"окно {days} дн" if days else "вся глубина")

    def _run():
        for n, (isin, _kind) in enumerate(targets, 1):
            try:
                stat["days"] += build_daily(isin, days=days, force=force)
            except Exception as e:
                stat["failed"] += 1
                logger.warning("build_daily %s: %s", isin, e)
            if n % 50 == 0:
                progress.set_done("bars_daily", n)
                logger.info("build_daily: %d/%d, дней %d", n, len(targets), stat["days"])

    await asyncio.to_thread(_run)
    progress.finish("bars_daily",
                    detail=f"{stat['days']} дней, ошибок {stat['failed']}")
    logger.info("build_daily_universe готово: %s", stat)
    return stat


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


def hot_isins(top: int = 200, window_days: int = 30) -> list[str]:
    """Самые торгуемые бумаги по обороту за окно — их графики открывают чаще
    всего, и именно им стоит держать спред досчитанным заранее."""
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    with _connect() as c:
        rows = c.execute(
            "SELECT isin, SUM(COALESCE(value,0)) v FROM bar_hourly WHERE ts >= ? "
            "GROUP BY isin ORDER BY v DESC LIMIT ?", (cutoff, top)).fetchall()
    return [r["isin"] for r in rows]


async def warm_hot(days: int = 150, top: int = 200, concurrency: int = 2) -> dict:
    """Ночной прогрев спреда по самым торгуемым бумагам.

    Полный проход по универсу на 400 дней нереален: честный as-of строит на
    КАЖДЫЙ день свою кривую/НКД/номинал, выходит порядка десяти минут на бумагу.
    Поэтому греем не всё и не на всю глубину, а топ по обороту на окно, которое
    реально смотрят (1М/3М/6М). Остальное догревается лениво при открытии
    графика — ensure_bars уже так работает, и результат ложится в базу
    (metrics_ver), так что второй раз бумага не пересчитывается."""
    targets = dict(await universe_targets())
    isins = [i for i in await asyncio.to_thread(hot_isins, top) if i in targets]
    sem = asyncio.Semaphore(concurrency)
    stat = {"papers": len(isins), "done": 0, "failed": 0}

    async def one(isin: str):
        async with sem:
            try:
                await ensure_bars(isin, days=days, kind=targets[isin], wait_past=True)
                stat["done"] += 1
            except Exception as e:
                stat["failed"] += 1
                logger.warning("warm_hot %s: %s", isin, e)
            if (stat["done"] + stat["failed"]) % 25 == 0:
                logger.info("warm_hot: %d/%d", stat["done"] + stat["failed"], len(isins))

    await asyncio.gather(*(one(i) for i in isins))
    logger.info("warm_hot готово: %s", stat)
    return stat


async def refresh_universe(days: int = 3, limit: Optional[int] = None,
                           with_ticks: bool = True, concurrency: int = 4,
                           kinds: tuple = ("floater", "fixed"),
                           progress_every: int = 50, full: bool = True,
                           refetch_ticks: bool = False,
                           progress_key: str = "bars_refresh",
                           progress_label: Optional[str] = None,
                           offset: int = 0) -> dict:
    """Наливает бары (и тики) по всему юниверсу. Используется и часовым демоном
    (days=2..3, дозалив хвоста), и бэкфилл-скриптом (days=365, разовый прогон).

    full=False — только торгующиеся бумаги (есть бар за неделю): проход вместо
    ~33 минут занимает единицы минут. Полный проход нужен реже — им подхватываются
    новые выпуски и вернувшаяся ликвидность.

    refetch_ticks=True снимает водяной знак инкрементального дрейна и качает окно
    сделок заново. Обычному проходу это не нужно (знак и так отдаёт всё новое) —
    флаг для ремонта: если в архиве подозревается дыра.

    offset — СКОЛЬКО БУМАГ ПРОПУСТИТЬ с начала списка, окно [offset, offset+limit).
    Нужен разовым бэкфиллам на глубоком окне: контексты as-of копятся по бумагам
    внутри процесса (~5 МБ на бумагу) и не отдаются, поэтому проход по всему
    юниверсу на days=365 упирается в mem_limit и ловит OOM. Дробить одним limit
    нельзя — он режет ХВОСТ, а не окно: каждая следующая пачка тянет за собой все
    предыдущие бумаги и падает ровно там же. С offset пачка живёт своим процессом
    и своей памятью."""
    targets = await universe_targets(kinds)
    if not full:
        act = active_isins()
        if act:
            targets = [(i, k) for i, k in targets if i in act]
    if offset:
        targets = targets[offset:]
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
