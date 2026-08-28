"""Вкладка ФИКСЫ — список облигаций с фиксированным купоном (ОФЗ-ПД + ликвидные
корпораты) с метриками к погашению. Универс и метрики прогреваются фоновым
поллером (market_cache), эндпоинт отдаёт кэш; при холодном кэше — быстрый фетч
универса (2 запроса), метрики появляются по мере прогрева."""
import re
import asyncio
import logging
from datetime import date
from fastapi import APIRouter, Path, Query, HTTPException

from services.market_data import market_cache, MarketDataService
from services.exceptions import NotFoundException

logger = logging.getLogger(__name__)
router = APIRouter()

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

# поля метрик, доливаемые в строку из market_cache['fixed_metrics']
_METRIC_KEYS = ("last", "prev", "price_stale", "dirty", "ytm", "delta_ytm", "cur_yield",
                "g_spread_bps", "z_spread_bps", "mod_dur", "mac_dur", "convexity",
                "dv01", "put_date",
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
            "cls": u.get("cls"), "maturity_date": u.get("maturity_date"),
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
async def get_fixed_quotes():
    """Котировки фиксов одним компактным ответом — витрина тянет их тактом 5с.

    Отдаёт только то, что двигается внутри дня: цену сделки, верх стакана,
    средневзвес и оборот. Метрики (YTM/g-спред) живут своим циклом в /api/fixed
    и здесь не дублируются: универс фиксов пересобирается раз в час, а цена
    обязана быть свежей — источник тот же board-снапшот MOEX, который держит
    свежим quotes_poller (сети на запрос нет).

    ОБЪЯВЛЕН ДО /{isin}: иначе путь съест роут карточки как ISIN.
    """
    from services import fixed_income as fi
    from services import live_quotes
    uni = market_cache.get("fixed_universe") or await fi.fetch_fixed_universe()
    snap = await MarketDataService.fetch_board_snapshot()
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


@router.get("/{isin}", tags=["Fixed"])
async def get_fixed_details(isin: str = Path(...)):
    """Карточка фикс-бумаги: справка + метрики к погашению + поток платежей."""
    isin = isin.strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="bad isin")
    from services import fixed_income as fi, ratings
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
            "coupon_pct": row.get("coupon_pct"), "face": row.get("face"),
            "issuer": row.get("issuer"), "rating": ratings.bucket_of_fixed(isin, row.get("cls")),
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
