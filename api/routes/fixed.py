"""Вкладка ФИКСЫ — список облигаций с фиксированным купоном (ОФЗ-ПД + ликвидные
корпораты) с метриками к погашению. Универс и метрики прогреваются фоновым
поллером (market_cache), эндпоинт отдаёт кэш; при холодном кэше — быстрый фетч
универса (2 запроса), метрики появляются по мере прогрева."""
import re
import time
import asyncio
import logging
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Path, Query, HTTPException

from services.market_data import market_cache, MarketDataService
from services.exceptions import NotFoundException

logger = logging.getLogger(__name__)
router = APIRouter()

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

# поля метрик, доливаемые в строку из market_cache['fixed_metrics']
_METRIC_KEYS = ("last", "prev", "price_stale", "dirty", "ytm", "delta_ytm", "cur_yield",
                "g_spread_bps", "z_spread_bps", "mod_dur", "mac_dur", "convexity",
                "dv01", "put_date", "issue_date",
                # средневзвес дня и g-спред по нему — база графиков аналитики
                # (last price в неликвиде это один случайный принт)
                "wap_pct", "g_spread_wap_bps", "ytm_wap",
                # верх стакана и YTM/g-спред по сторонам: по ним торгуют, а last
                # это уже история (те же две колонки, что у флоатеров)
                "bid", "ask", "g_spread_bid_bps", "g_spread_ask_bps",
                "ytm_bid", "ytm_ask",
                # движение к вчерашнему закрытию и признаки выпуска для фильтров
                "delta_to_prev_close", "has_amort", "price_thin",
                # номинал на дату поставки и НКД — из них фронт считает ДЕНЬГИ
                # уровня стакана для фильтра по объёму тикета
                "face_value_rub", "accrued_rub")


def _vol_fields(m: dict, vol_bid, vol_ask) -> dict:
    """Цена набора тикета и метрики ПО НЕЙ — из чисел, посчитанных движком в его
    такте (universe_stream._crunch_fixed). Здесь ничего не считается: своя
    арифметика в ручке разъехалась бы с движком — то же правило, что у флоатеров
    (api/routes/bonds._vol_fields)."""
    out: dict = {}
    px_map = m.get("vol_px") or {}
    g_map = m.get("g_spread_vol") or {}
    y_map = m.get("ytm_vol") or {}
    for side, size in (("bid", vol_bid), ("ask", vol_ask)):
        if not size:
            continue
        key = f"{side}:{float(size):.0f}"
        out[f"vol_{side}_price_pct"] = px_map.get(key)
        out[f"g_spread_vol_{side}_bps"] = g_map.get(key)
        out[f"ytm_vol_{side}"] = y_map.get(key)
    return out


@router.get("", tags=["Fixed"])
async def get_fixed(
    vol_bid: float = Query(None, description="Тикет на биде, ₽ — вернуть цену набора и её g-спред"),
    vol_ask: float = Query(None, description="Тикет на оффере, ₽"),
):
    """{items, total, calc_date} — фиксы с YTM/g-спред/z-спред/дюрацией."""
    from services.feature_flags import fixed_enabled
    if not fixed_enabled():
        # слой выключен (FIXED_TAB=0): прогрева нет, и ходить за универсом в
        # MOEX ради пустой витрины незачем — старая вкладка получит пустой ответ
        return {"items": [], "total": 0, "disabled": True,
                "calc_date": date.today().isoformat()}
    from services import fixed_income as fi
    # размеры тикета регистрируем В ДВИЖКЕ: он посчитает цену набора и спред по
    # ней в своём такте по методике, а ручка только выберет нужный размер
    if vol_bid or vol_ask:
        from services.universe_stream import register_vol_sizes
        register_vol_sizes([v for v in (vol_bid, vol_ask) if v])
    uni = market_cache.get("fixed_universe")
    if not uni:
        uni = await fi.fetch_fixed_universe()
    metrics = market_cache.get("fixed_metrics") or {}

    from services import ratings
    rmap = ratings.bucket_map_fixed([(u["isin"], u.get("cls")) for u in uni])  # батч (1 SQL)
    # рейтинги по агентствам (Эксперт/АКРА) — durable-кэш слоя, без сети
    try:
        from services import ratings_br
        ea = ratings_br.ea_map([u["isin"] for u in uni])
    except Exception:
        ea = {}
    # средний дневной оборот за месяц — из архива часовых баров (кэш в памяти на
    # 15 мин, SQLite синхронный → в поток). Зовём БЕЗ kind, тем же ключом, что и
    # /api/bonds: в bar_hourly у бумаги свой kind, поэтому ответ по всему рынку
    # содержит и фиксы, а кэш ADV — на ОДИН ключ (services/bars._adv_cache), и
    # два разных ключа выбивали бы друг друга полным сканом базы на каждый запрос.
    from services import bars as bars_svc
    try:
        adv = await asyncio.to_thread(bars_svc.adv_map, 30)
    except Exception as e:
        logger.warning("fixed adv_map failed: %s", e)
        adv = {}
    items = []
    for u in uni:
        m = metrics.get(u["isin"], {})
        item = {
            "isin": u["isin"], "secid": u.get("secid"), "name": u.get("name"),
            "issuer": u.get("issuer"), "rating": rmap.get(u["isin"]),
            "ratings_ea": ea.get(u["isin"]),
            "cls": u.get("cls"), "maturity_date": u.get("maturity_date"),
            # Валюта номинала — отдельна от валюты расчётов и нужна фильтру
            # витрины. Старые коды рубля приводим к одному значению на UI.
            "face_unit": "RUB" if (u.get("faceunit") or "RUB").upper() in ("", "RUB", "SUR", "RUR")
                         else (u.get("faceunit") or "RUB").upper(),
            "coupon_pct": u.get("coupon_pct"), "val_today": u.get("val_today"),
            "adv_1m_rub": adv.get(u["isin"]),
            # цена: из метрик (last→prev с флагом) иначе сырой board
            "last_price_pct": m.get("last", u.get("last") if u.get("last") is not None else u.get("prev")),
        }
        for k in _METRIC_KEYS:
            if k in m:
                item[k] = m[k]
        if vol_bid or vol_ask:
            item.update(_vol_fields(m, vol_bid, vol_ask))
        items.append(item)

    return {"items": items, "total": len(items),
            "calc_date": market_cache.get("fixed_calc_date") or date.today().isoformat()}


@router.get("/quotes", tags=["Fixed"])
async def get_fixed_quotes(
    vol_bid: float = Query(None, description="Тикет на биде, ₽ — вернуть цену набора и её g-спред"),
    vol_ask: float = Query(None, description="Тикет на оффере, ₽"),
):
    """Котировки фиксов одним компактным ответом — витрина тянет их тактом 5с.

    Отдаёт только то, что двигается внутри дня: цену сделки, верх стакана,
    средневзвес и оборот. Метрики (YTM/g-спред) живут своим циклом в /api/fixed
    и здесь не дублируются: универс фиксов пересобирается раз в час, а цена
    обязана быть свежей — источник тот же board-снапшот MOEX, который держит
    свежим quotes_poller (сети на запрос нет).

    vol_bid/vol_ask — размеры тикета из фильтра по объёму. Цена набора и её
    g-спред/YTM едут ЭТИМ ЖЕ ТАКТОМ: сам список витрины обновляется раз в
    минуту, а движок считает бумаги пачками, и прочерк в колонках бида и оффера
    висел до минуты после включения фильтра. Заодно продлевает регистрацию
    размера в движке.

    ОБЪЯВЛЕН ДО /{isin}: иначе путь съест роут карточки как ISIN.
    """
    from services.feature_flags import fixed_enabled
    if not fixed_enabled():
        return {"ts": None, "n": 0, "items": [], "disabled": True}
    from services import fixed_income as fi
    from services import live_quotes
    uni = market_cache.get("fixed_universe") or await fi.fetch_fixed_universe()
    snap = await MarketDataService.fetch_board_snapshot()
    if vol_bid or vol_ask:
        from services.universe_stream import register_vol_sizes
        register_vol_sizes([v for v in (vol_bid, vol_ask) if v])
    fm = market_cache.get("fixed_metrics") or {} if (vol_bid or vol_ask) else {}
    items = []
    for u in uni:
        v = snap.get(u["isin"])
        if not v:
            continue
        lv = live_quotes.get(u["isin"]) or {}
        items.append({"isin": u["isin"], "last": v.get("last"), "bid": v.get("bid"),
                      "ask": v.get("ask"),
                      # средневзвес выбираем ТЕМ ЖЕ правилом, что и расчёт метрик
                      # (fixed_income.pick_wap): свой тиковый — пока он покрывает
                      # дневной оборот, иначе биржевой WAPRICE
                      "wap": fi.pick_wap({"isin": u["isin"], "wap": v.get("waprice"),
                                         "val_today": v.get("vol")}),
                      # оборот — больший из двух: свой счёт полон только при живом
                      # стриме, биржевой VALTODAY отстаёт
                      "vol": max(v.get("vol") or 0, lv.get("val_today") or 0) or None})
        m = fm.get(u["isin"])
        if m:
            # плоскими ключами: размер известен из запроса (см. bonds.get_quotes)
            px_map = m.get("vol_px") or {}
            g_map = m.get("g_spread_vol") or {}
            y_map = m.get("ytm_vol") or {}
            for side, size in (("bid", vol_bid), ("ask", vol_ask)):
                if not size:
                    continue
                key = f"{side}:{float(size):.0f}"
                for suffix, src in (("px", px_map), ("g", g_map), ("ytm", y_map)):
                    if src.get(key) is not None:
                        items[-1][f"vol_{side}_{suffix}"] = src[key]
    return {"ts": market_cache.get("quotes_ts"), "n": len(items), "items": items}


def _display_cashflow(full: dict, calc_date: date) -> list:
    """Будущие потоки для карточки: купоны (ставка+₽) + амортизации/погашение."""
    from services.zspread import _d
    out = []
    for c in full.get("coupons") or []:
        e, v = _d(c.get("end")), c.get("value")
        if e and e > calc_date and v is not None:
            out.append({"date": e.isoformat(), "type": "COUPON",
                        "amount": round(float(v), 2), "rate_pct": c.get("valueprc")})
    amorts = [(_d(a.get("date")), a.get("value")) for a in (full.get("amorts") or [])]
    amorts = sorted((d, v) for d, v in amorts if d and v is not None and d > calc_date)
    for i, (d, v) in enumerate(amorts):
        out.append({"date": d.isoformat(),
                    "type": "MATURITY" if i == len(amorts) - 1 else "AMORT",
                    "amount": round(float(v), 2), "rate_pct": None})
    out.sort(key=lambda x: (x["date"], x["type"] == "COUPON"))
    return out


# ───────────────────────── витрина ОФЗ: объёмы и as-of ─────────────────────────
#
# Ручки страницы /fixed/ofz (docs/ofz_desk_tz.md). Своих расчётов у страницы
# нет — YTM/дюрация только из движка фиксов (compute_fixed_row), объёмы —
# из тех же таблиц, что лента сделок. Стоят ВЫШЕ /{isin}: путь /ofz/... с
# двумя сегментами в /{isin} не попадает, но пусть порядок это гарантирует.

_MSK = timezone(timedelta(hours=3))
_STEP_BACK_DAYS = 7          # «вчера» = предыдущий торговый день: выходные + праздники
_ASOF_MEMO_MAX = 30          # дат в памяти — витрина листает вчера/дату, не год
_ASOF_MEMO_TTL = 3600.0      # с: вечерний снапшот и бэкфилл bond_day приходят позже
_asof_memo: "OrderedDict[str, tuple]" = OrderedDict()   # date → (ts, payload)


def _msk_today() -> date:
    return datetime.now(_MSK).date()


def _parse_day(s: Optional[str], *, required: bool) -> Optional[date]:
    """Дата запроса: ISO, не в будущем. None — «сегодня» (там, где допустимо)."""
    if s is None:
        if required:
            raise HTTPException(status_code=400, detail="date обязателен: YYYY-MM-DD")
        return None
    try:
        d = date.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=400, detail="date: YYYY-MM-DD")
    if d > _msk_today():
        raise HTTPException(status_code=400, detail="date в будущем")
    return d


async def _ofz_rows() -> list:
    """Строки ОФЗ-ПД из универса фиксов (кэш поллера, при холодном — фетч)."""
    from services import fixed_income as fi
    uni = market_cache.get("fixed_universe") or await fi.fetch_fixed_universe()
    return [u for u in uni if u.get("cls") == "ofz" and u.get("isin")]


@router.get("/ofz/volumes", tags=["Fixed"])
async def get_ofz_volumes(
    date_: str = Query(None, alias="date", description="День YYYY-MM-DD; без него — сегодня (живые данные)"),
):
    """Объём торгов ОФЗ за день по бумагам и режимам (стакан / адресные / прочее)
    плюс раскладка по бордам — столбики под точками графика ОФЗ.

    Сегодня — живые данные: val_today универса (TQOB, это уже весь стакан дня)
    + адресные сделки из ленты block_trade (market=ndm) суммой по борду.
    Прошлая дата — дневные итоги биржи: bond_day (безадресные борды) и
    block_day (адресные). Классификация борда — block_trades.classify_board."""
    from services import block_trades as bt
    from services.pools import run_bg
    d = _parse_day(date_, required=False)
    today = _msk_today()
    live = d is None or d == today
    d = d or today
    rows = await _ofz_rows()
    isins = [u["isin"] for u in rows]
    # {isin: {board: (руб, market|None)}}
    acc: dict = {}

    def _add(isin: str, board: Optional[str], value, market: Optional[str] = None):
        if not isin or value is None:
            return
        b = (board or "?").upper()
        by = acc.setdefault(isin, {})
        cur = by.get(b)
        by[b] = (float(value) + (cur[0] if cur else 0.0), market or (cur[1] if cur else None))

    if live:
        for u in rows:
            _add(u["isin"], u.get("board") or "TQOB", u.get("val_today") or 0.0, "bonds")
        for r in await run_bg(bt.read_ndm_day_values, d.isoformat(), isins):
            _add(r["isin"], r["board"], r["value"], "ndm")
    else:
        ds = d.isoformat()
        for r in await run_bg(bt.read_bond_days, ds, isins):
            _add(r["isin"], r["board"], r["value"], "bonds")
        for r in await run_bg(bt.read_block_days, ds, isins):
            _add(r["isin"], r["board"], r["value"], "ndm")

    items = {}
    for isin, by in acc.items():
        it = {"total": 0.0, "book": 0.0, "rps": 0.0, "other": 0.0, "boards": {}}
        for b, (v, market) in by.items():
            v = round(v, 2)
            it["boards"][b] = v
            it[bt.classify_board(b, market)] += v
            it["total"] += v
        for k in ("total", "book", "rps", "other"):
            it[k] = round(it[k], 2)
        items[isin] = it
    return {"date": d.isoformat(), "live": live, "items": items,
            "board_labels": {b: bt.BOARD_LABELS.get(b, b)
                             for it in items.values() for b in it["boards"]}}


def _accrued_on(full: dict, d: date, fallback: float) -> float:
    """НКД на дату поставки d из расписания купонов (купон ОФЗ-ПД всегда
    опубликован). Биржевой ACCRUEDINT в строке универса — на СЕГОДНЯ, для
    прошлой даты он врал бы на всё начисление между датами."""
    from core.valuation import accrued_at, accrued_estimate
    coupons = full.get("coupons") or []
    a = accrued_at(coupons, d)
    if a is None:
        a = accrued_estimate(coupons, d)
    return float(a) if a is not None else float(fallback or 0.0)


def _asof_rows_for(d: str, isins: list) -> tuple:
    """(snap: {isin: {ytm, price_pct, horizon}}, px: {isin: цена}) на дату —
    в одном потоке, чтобы не дёргать пул дважды."""
    from services import block_trades as bt
    from services.portfolio_db import _connect
    snap: dict = {}
    with _connect() as c:
        q = ("SELECT isin, ytm, price_pct, horizon FROM spread_daily "
             "WHERE date = ? AND kind = 'fixed' AND price_pct IS NOT NULL")
        args: list = [d]
        if isins:
            q += f" AND isin IN ({','.join('?' * len(isins))})"
            args.extend(isins)
        for r in c.execute(q, args):
            snap[r["isin"]] = {"ytm": r["ytm"], "price_pct": r["price_pct"],
                               "horizon": r["horizon"]}
    px: dict = {}
    val: dict = {}
    # bond_day: стакан впереди прочих бордов, средневзвес впереди закрытия
    # (last в неликвиде — один случайный принт; у ОФЗ TQOB это редкость, но
    # правило общее с витриной)
    for r in bt.read_bond_days(d, isins):
        # оборот дня по всем бордам — вес точки в подгонке своей кривой
        val[r["isin"]] = val.get(r["isin"], 0.0) + float(r.get("value") or 0.0)
        p = r.get("waprice") or r.get("close")
        if p is None:
            continue
        rank = (0 if bt.classify_board(r.get("board")) == "book" else 1, -(r.get("value") or 0.0))
        cur = px.get(r["isin"])
        if cur is None or rank < cur[0]:
            px[r["isin"]] = (rank, float(p))
    return snap, {k: v[1] for k, v in px.items()}, val


@router.get("/ofz/asof", tags=["Fixed"])
async def get_ofz_asof(
    date_: str = Query(..., alias="date", description="Дата сравнения YYYY-MM-DD"),
):
    """Доходность и дюрация ОФЗ НА ПРОШЛУЮ ДАТУ — «тени» точек и колонка ΔYTM.

    Источник по приоритету: 1) вечерний снапшот spread_daily (kind=fixed) —
    YTM/цена, как их видел движок в тот день; 2) иначе цена дня биржи
    (bond_day.waprice → close) и пересчёт compute_fixed_row тем же движком
    без КБД (calc_date=дата, price_override=цена); 3) без цены бумаги в
    ответе нет. Дюрация (tau, Маколей — та же ось X, что сегодня) в снапшоте
    не хранится, поэтому считается пересчётом по цене снапшота в обоих случаях.
    На дату без единой строки шагаем назад до 7 дней (выходные/праздники) и
    возвращаем фактическую дату. Кэш в памяти по дате: пересчёт ~60 бумаг
    дорогой только первый раз."""
    req = _parse_day(date_, required=True)
    return await _ofz_asof_payload(req)


async def _ofz_asof_payload(req: date) -> dict:
    """Тело /ofz/asof: as-of точки {isin: {ytm, tau, px, val, src}} на дату с
    шагом назад и кэшем. Общее с /ofz/curve?date= — своя кривая на прошлую
    дату подгоняется по ТЕМ ЖЕ точкам, что рисуются тенями, а не по копии
    логики."""
    from services import fixed_income as fi
    now = time.time()
    hit = _asof_memo.get(req.isoformat())
    if hit and now - hit[0] < _ASOF_MEMO_TTL:
        _asof_memo.move_to_end(req.isoformat())
        return hit[1]

    rows = await _ofz_rows()
    isins = [u["isin"] for u in rows]
    found = None
    for k in range(_STEP_BACK_DAYS + 1):
        d = req - timedelta(days=k)
        snap, px, val = await asyncio.to_thread(_asof_rows_for, d.isoformat(), isins)
        if snap or px:
            found = (d, snap, px, val)
            break
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=f"Нет ни снапшота, ни дневных цен ОФЗ за {_STEP_BACK_DAYS} дн до {req.isoformat()}")
    d, snap, px, val = found

    # расписание MOEX тянем по SECID — у ОФЗ ISIN в bondization не резолвится
    # (то же правило, что в compute_fixed_metrics_all)
    fulls = await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(u.get("secid") or u["isin"]) for u in rows),
        return_exceptions=True)

    def _crunch() -> dict:
        from core.valuation import settle_date
        out: dict = {}
        settle = settle_date(d)
        for u, full in zip(rows, fulls):
            full = {} if isinstance(full, Exception) else (full or {})
            isin = u["isin"]
            sn = snap.get(isin)
            price = sn["price_pct"] if sn else px.get(isin)
            if price is None or not full.get("coupons"):
                continue
            row = dict(u)
            row["accrued"] = _accrued_on(full, settle, u.get("accrued"))
            try:
                m = fi.compute_fixed_row(row, full, None, d, price_override=float(price))
            except Exception as e:
                logger.warning(f"ofz asof {isin} {d}: {e}")
                continue
            ytm = sn["ytm"] if sn and sn.get("ytm") is not None else m.get("ytm")
            if ytm is None:
                continue
            out[isin] = {"ytm": ytm, "tau": m.get("mac_dur"), "px": float(price),
                         "val": val.get(isin, 0.0),
                         "src": "snap" if sn else "reprice"}
        return out

    items = await asyncio.to_thread(_crunch)
    payload = {"date": d.isoformat(), "requested": req.isoformat(), "items": items}
    _asof_memo[req.isoformat()] = (now, payload)
    while len(_asof_memo) > _ASOF_MEMO_MAX:
        _asof_memo.popitem(last=False)
    return payload


# ── СВОЯ кривая ОФЗ (services/ofz_curve): NSS/NS по точкам выпусков ──
# YTM по базе — та же карта, что byBase во фронте витрины: точка на графике и
# остаток к кривой обязаны быть от одной цены.
_CURVE_BASES = {"wap": "ytm_wap", "last": "ytm", "bid": "ytm_bid", "ask": "ytm_ask"}
_CURVE_MEMO_TTL_TODAY = 60.0     # с: сегодняшняя кривая ходит с ценами
_CURVE_MEMO_MAX = 30
# ключ — (база, дата, отпечаток ВСЕГО входа): при тех же точках подгонка та же,
# при новой цене хоть одной бумаги ключ другой — кэш от прошлых цен не отдаём
_curve_memo: "OrderedDict[str, tuple]" = OrderedDict()   # key → (ts, payload)


def _curve_archive_save(d: str, base: str, res) -> None:
    """ofz_curve_daily: одна строка на (дата, база), последний расчёт дня
    побеждает — история своей кривой для аукционов и динамики теноров."""
    import json
    from services import portfolio_db as pdb
    with pdb._lock, pdb._connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO ofz_curve_daily(date,base,method,params,rmse_bps,n_used,at) "
            "VALUES(?,?,?,?,?,?,?)",
            (d, base, res.method, json.dumps(res.params), res.rmse_bps, res.n_used,
             datetime.now(_MSK).isoformat(timespec="seconds")))


@router.get("/ofz/curve", tags=["Fixed"])
async def get_ofz_curve(
    base: str = Query("wap", description="База цены YTM: wap|last|bid|ask (без date)"),
    date_: str = Query(None, alias="date",
                       description="As-of дата YYYY-MM-DD: точки как у /ofz/asof, база одна"),
):
    """Своя кривая ОФЗ — Нельсон–Сигел–Свенссон по точкам выпусков
    (YTM × дюрация Маколея), подгонка на бэке (services/ofz_curve).

    Без date — точки те же, что у /api/fixed cls=ofz: YTM по базе цены, τ —
    Маколей (фолбэк модифицированная), вес — оборот дня val_today. С date —
    as-of точки той же логики, что /ofz/asof (одна цена дня → base="asof",
    date/requested/stale как там). В ответе линия (samples), ключевые теноры
    для стрипа Δ и остатки по ISIN — фронт ничего не считает. < 4 пригодных
    точек — 422. Сегодняшний расчёт ложится в архив ofz_curve_daily."""
    from services import ofz_curve as oc
    if base not in _CURVE_BASES:
        raise HTTPException(status_code=400, detail="base: wap|last|bid|ask")
    req = _parse_day(date_, required=False)
    if req is None:
        rows = await _ofz_rows()
        metrics = market_cache.get("fixed_metrics") or {}
        ykey = _CURVE_BASES[base]
        pts = []
        for u in rows:
            m = metrics.get(u["isin"]) or {}
            tau = m.get("mac_dur") if m.get("mac_dur") is not None else m.get("mod_dur")
            pts.append(oc.FitPoint(u["isin"], m.get(ykey), tau, u.get("val_today") or 0.0))
        d_iso = str(market_cache.get("fixed_calc_date") or _msk_today().isoformat())
        head = {"date": d_iso, "requested": d_iso, "stale": False, "base": base}
        ttl = _CURVE_MEMO_TTL_TODAY
    else:
        asof = await _ofz_asof_payload(req)
        pts = [oc.FitPoint(isin, it.get("ytm"), it.get("tau"), it.get("val") or 0.0)
               for isin, it in asof["items"].items()]
        head = {"date": asof["date"], "requested": asof["requested"],
                "stale": asof["date"] != asof["requested"], "base": "asof"}
        ttl = _ASOF_MEMO_TTL

    # requested — тоже в ключе: суббота и пятница дают одну фактическую дату и
    # одни точки, но ответ обязан честно сказать, что просили субботу (stale)
    key = f"{head['base']}|{head['date']}|{head['requested']}|{oc.input_fingerprint(pts)}"
    now = time.time()
    hit = _curve_memo.get(key)
    if hit and now - hit[0] < ttl:
        _curve_memo.move_to_end(key)
        return hit[1]
    try:
        # ~0.1 с numpy на 60 точках — не в event loop
        res = await asyncio.to_thread(oc.fit, pts)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"кривая ОФЗ: {e}")
    payload = {**head, **res.to_dict()}
    if req is None:
        try:
            await asyncio.to_thread(_curve_archive_save, head["date"], base, res)
        except Exception as e:
            logger.warning(f"ofz_curve_daily save {head['date']}/{base}: {e}")
    _curve_memo[key] = (now, payload)
    while len(_curve_memo) > _CURVE_MEMO_MAX:
        _curve_memo.popitem(last=False)
    return payload


@router.get("/{isin}", tags=["Fixed"])
async def get_fixed_details(isin: str = Path(...)):
    """Карточка фикс-бумаги: справка + метрики к погашению + поток платежей."""
    isin = isin.strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="bad isin")
    from services import fixed_income as fi, ratings, ratings_br
    uni = market_cache.get("fixed_universe") or await fi.fetch_fixed_universe()
    row = next((u for u in uni if u.get("isin") == isin), None)
    if row is None:
        raise NotFoundException(f"{isin} не найден в универсе фиксов", {"isin": isin})

    secid = row.get("secid") or isin
    board = "TQOB" if row.get("cls") == "ofz" else "TQCB"
    full = await MarketDataService.fetch_bond_schedule_full(secid)
    _r, _k, cd, rd = await MarketDataService.get_curves()
    _ek, _eu, g = await MarketDataService.get_zspread_ctx()
    calc_date = cd or rd or date.today()
    m = fi.compute_fixed_row(row, full, g, calc_date)

    return {
        "reference": {
            "isin": isin, "secid": secid, "name": row.get("name"), "cls": row.get("cls"),
            "board": board, "maturity_date": row.get("maturity_date"),
            "face_unit": "RUB" if (row.get("faceunit") or "RUB").upper() in ("", "RUB", "SUR", "RUR")
                         else (row.get("faceunit") or "RUB").upper(),
            "coupon_pct": row.get("coupon_pct"), "face": row.get("face"),
            "issuer": row.get("issuer"), "rating": ratings.bucket_of_fixed(isin, row.get("cls")),
            "ratings_ea": ratings_br.ea_map([isin]).get(isin),
            "linked": row.get("linked", False),   # номинал индексирован (RUONIA/инфл.)
        },
        "market": {
            "last_price_pct": m.get("last"), "prev_close_pct": row.get("prev"),
            "price_stale": m.get("price_stale", False), "dirty_rub": m.get("dirty"),
            "accrued_rub": row.get("accrued"), "val_today": row.get("val_today"),
            "wap_price_pct": m.get("wap_pct"),
        },
        "metrics": {
            "ytm_pct": m.get("ytm"), "cur_yield_pct": m.get("cur_yield"),
            "g_spread_bps": m.get("g_spread_bps"), "z_spread_bps": m.get("z_spread_bps"),
            "g_spread_wap_bps": m.get("g_spread_wap_bps"), "ytm_wap_pct": m.get("ytm_wap"),
            "mod_dur": m.get("mod_dur"), "mac_dur": m.get("mac_dur"),
            "convexity": m.get("convexity"), "dv01": m.get("dv01"),
            "put_date": m.get("put_date"),
        },
        "cashflow": _display_cashflow(full, calc_date),
        "calc_date": calc_date.isoformat(),
    }


@router.get("/{isin}/reprice", tags=["Fixed"])
async def reprice_fixed(
    isin: str = Path(...),
    price: float = Query(..., gt=0, le=1000, description="Чистая цена, % от номинала"),
):
    """Калькулятор карточки фикса: пересчёт YTM/g-спред/z-спред/дюрации/dirty под
    произвольную чистую цену. Тот же путь, что строка таблицы (compute_fixed_row),
    но с price_override."""
    isin = isin.strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="bad isin")
    from services import fixed_income as fi
    uni = market_cache.get("fixed_universe") or await fi.fetch_fixed_universe()
    row = next((u for u in uni if u.get("isin") == isin), None)
    if row is None:
        raise NotFoundException(f"{isin} не найден в универсе фиксов", {"isin": isin})

    secid = row.get("secid") or isin
    full = await MarketDataService.fetch_bond_schedule_full(secid)
    _r, _k, cd, rd = await MarketDataService.get_curves()
    _ek, _eu, g = await MarketDataService.get_zspread_ctx()
    calc_date = cd or rd or date.today()
    m = fi.compute_fixed_row(row, full, g, calc_date, price_override=price)

    return {
        "clean_price_pct": price,
        "dirty_rub": m.get("dirty"),
        "ytm_pct": m.get("ytm"), "cur_yield_pct": m.get("cur_yield"),
        "g_spread_bps": m.get("g_spread_bps"), "z_spread_bps": m.get("z_spread_bps"),
        "mod_dur": m.get("mod_dur"), "mac_dur": m.get("mac_dur"),
        "convexity": m.get("convexity"), "dv01": m.get("dv01"),
        "calc_date": calc_date.isoformat(),
    }
