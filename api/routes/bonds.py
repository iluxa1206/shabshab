import os
import re
import logging
from datetime import date
from typing import Optional, Literal
from fastapi import APIRouter, Query, Path, HTTPException

from api.schemas import (
    BondListItem, BondListResponse, BondFiltersResponse,
    BondDetailsResponse, CashflowResponse,
    RepriceResponse,
)
from services.market_data import MarketDataService
from services.bonds import (
    create_bond_ref_data, build_ref_external, external_formula, next_coupon_after,
)
from services.valuation import calculate_valuation_metrics
from services.exceptions import NotFoundException
from services import instruments_registry
from services.paths import cache_path as _cache_path
from core.cashflow import read_isins_from_file

logger = logging.getLogger(__name__)

router = APIRouter()

# ISO 6166; тот же паттерн, что в funds. Валидируем ВСЕ входные ISIN: они
# интерполируются в URL к MOEX/Alor f-строками — мусор/`..%2F` не должен уходить
# во внешние запросы.
_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_SECID_RE = re.compile(r"[A-Z0-9]{4,20}")


def _require_isin(isin: str) -> str:
    v = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(v):
        raise HTTPException(status_code=422, detail=f"Невалидный ISIN: {isin!r}")
    return v


def get_base_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE_LABEL = {"KEYRATE": "Ключевая ставка", "RUONIA": "RUONIA"}


async def compute_universe_metrics(uni: list, isins: list) -> dict:
    """Прокси в services.universe (конвейер вынесен из route-слоя)."""
    from services.universe import compute_universe_metrics as _cum
    return await _cum(uni, isins, _cache_path("isins_cache.json"))


def _uni_item(u, name, mx, spread_dur):
    """BondListItem: строка универса реестра + наши метрики mx (universe.enrich_bond)
    + spread duration (кросс-секция)."""
    base = u.get("base_rate_type", "UNKNOWN")
    spread = u.get("spread_issue_bps") or 0
    label = _BASE_LABEL.get(base, base)
    formula = f"{label} + {spread / 100:g}%" if spread else label
    last = mx.get("last")
    return BondListItem(
        isin=u["isin"], short_name=name, base_rate_type=base, formula=formula,
        spread_issue_bps=int(spread), maturity_date=u.get("maturity_date"),
        next_coupon_date=mx.get("next_coupon"), last_price_pct=last,
        dirty_price_rub=mx.get("dirty"), dm_bps=mx.get("dm"),
        delta_to_prev_close=mx.get("delta"), disc_margin_bps=mx.get("disc_dm"),
        yield_xirr_pct=mx.get("ytm"), index_yield_pct=mx.get("base_ytm"),
        yield_over_index_bps=mx.get("yoi"), price_implausible=mx.get("implausible") or False,
        price_thin=mx.get("price_thin") or False, price_stale=mx.get("price_stale") or False,
        emitter_id=u.get("emitter_id"), emitter_name=u.get("emitter_name"),
        rating=u.get("rating"),
        z_model_bps=mx.get("z_model"), spread_dur_yrs=spread_dur, carry_bps=mx.get("carry"),
        days_to_refix=mx.get("refix"), current_coupon_pct=mx.get("current_coupon"),
        preferred_horizon=mx.get("horizon") or "maturity", offer_date=mx.get("offer_date"),
        sm_to_offer_bps=mx.get("sm_to_offer"), disc_margin_to_offer_bps=mx.get("dm_to_offer"),
    )


async def _universe_bonds(extra_list, cache, limit, offset):
    """Весь рынок флоатеров из реестра инструментов. Аналитика по всем;
    live-метрики — только для watchlist (extra). Расчёты в services.universe."""
    from services import universe as universe_svc
    uni = await instruments_registry.fetch_floater_universe()
    if not uni:
        return BondListResponse(items=[], total=0, limit=limit, offset=offset)

    cached_prices = MarketDataService.cached_prices()
    uni_metrics = MarketDataService.universe_metrics()  # фоновый поллер
    shortnames = await MarketDataService.fetch_moex_shortnames()
    watch = set(extra_list)
    spread_dur = universe_svc.cross_section_map(uni)

    watch_rows = [u for u in uni if u.get("isin") in watch]
    watch_metrics = await universe_svc.compute_watch_metrics(watch_rows, cache) if watch_rows else {}

    items = []
    for u in uni:
        isin = u["isin"]
        name = shortnames.get(isin) or u.get("name") or isin
        mx = watch_metrics.get(isin) or uni_metrics.get(isin)
        if mx is None:
            mx = {"last": cached_prices.get(isin)}
        items.append(_uni_item(u, name, mx, spread_dur.get(isin)))
    return BondListResponse(items=items[offset:offset + limit], total=len(items), limit=limit, offset=offset)


@router.get("", response_model=BondListResponse, tags=["Bonds"])
async def get_bonds(
    limit: int = Query(50, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    with_market: bool = Query(True),
    with_valuation: bool = Query(False),
    universe: bool = Query(False, description="Весь юниверс флоатеров из реестра"),
    extra: Optional[str] = Query(None, description="Доп. ISIN'ы (через запятую) — любые бумаги вне списка"),
    fields: Optional[str] = Query(None)
):
    base_dir = get_base_dir()
    isins_path = os.path.join(base_dir, "isins.txt")
    cache_path = _cache_path("isins_cache.json")

    try:
        isins = read_isins_from_file(isins_path)
    except Exception:
        isins = []

    extra_list = [x.strip().upper() for x in (extra.split(",") if extra else []) if x.strip()]
    extra_list = [x for x in extra_list if _ISIN_RE.fullmatch(x)]  # мусор молча отбрасываем
    cache = MarketDataService.get_local_bond_cache(cache_path)

    if universe:
        return await _universe_bonds(extra_list, cache, limit, offset)

    # добавленные пользователем бумаги (watchlist) — в начало, чтобы были видны
    base_set = set(isins)
    all_isins = [e for e in extra_list if e not in base_set] + isins

    total = len(all_isins)
    paginated_isins = all_isins[offset:offset + limit]

    external = [i for i in paginated_isins if i not in cache]

    market_prices = {}
    prev_close_prices = {}
    ruonia_curve = keyrate_curve = calc_date = rates_date = None
    moex_snapshot = {}
    moex_ref = {}
    schedules = {}

    if with_market or with_valuation:
        market_prices = await MarketDataService.fetch_last_prices(paginated_isins)
        moex_snapshot = await MarketDataService.fetch_moex_snapshot(paginated_isins)
        prev_close_prices = {i: v["prev"] for i, v in moex_snapshot.items() if v.get("prev") is not None}
        ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()

    if with_valuation:
        schedules = await MarketDataService.fetch_coupon_schedules(paginated_isins)

    if external:
        moex_ref = await MarketDataService.fetch_moex_securities(external)

    if not calc_date:
        calc_date = rates_date or date.today()

    items = []

    for isin in paginated_isins:
        data = cache.get(isin)
        if data:
            ref_obj = create_bond_ref_data(data, isin)
            short_name = data.get("SHORTNAME", "")
            formula = data.get("FORMULA", "")
        else:
            # внешняя бумага: справочник MOEX + база/спред из Cbonds-справки
            ref_obj = build_ref_external(isin, moex_ref.get(isin, {}))
            short_name = (moex_ref.get(isin) or {}).get("name") or isin
            formula = external_formula(ref_obj)

        last_price_pct = prev_close_pct = dirty_price_rub = dm_bps = delta_to_prev_close = None
        yield_xirr_pct = index_yield_pct = None

        if with_market:
            last_price_pct = market_prices.get(isin)
            prev_close_pct = prev_close_prices.get(isin) or (moex_ref.get(isin) or {}).get("prev")
            if last_price_pct is not None and prev_close_pct is not None:
                delta_to_prev_close = round(last_price_pct - float(prev_close_pct), 4)

        if with_valuation and last_price_pct is not None and (ruonia_curve or keyrate_curve) and ref_obj.base in ("RUONIA", "KEYRATE"):
            curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
            try:
                metrics = calculate_valuation_metrics(
                    ref_obj, last_price_pct, curve, calc_date,
                    accrued_override=moex_snapshot.get(isin, {}).get("accrued"),
                    periods=schedules.get(isin),
                )
                dirty_price_rub = metrics.get("dirty_price_rub")
                dm_bps = metrics.get("dm_bps")
                yield_xirr_pct = metrics.get("yield_xirr_pct")
                index_yield_pct = metrics.get("index_yield_pct")
            except Exception:
                pass

        items.append(
            BondListItem(
                isin=isin,
                short_name=short_name,
                base_rate_type=ref_obj.base,
                formula=formula,
                spread_issue_bps=ref_obj.spread_issue_bps,
                maturity_date=ref_obj.maturity_date,
                next_coupon_date=next_coupon_after(ref_obj, calc_date),
                last_price_pct=last_price_pct,
                dirty_price_rub=dirty_price_rub,
                dm_bps=dm_bps,
                delta_to_prev_close=delta_to_prev_close,
                yield_xirr_pct=yield_xirr_pct,
                index_yield_pct=index_yield_pct,
            )
        )

    return BondListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/search", tags=["Bonds"])
async def search_bonds(q: str = Query(..., min_length=2)):
    """Поиск облигаций на MOEX по названию/ISIN для добавления в список."""
    return {"items": await MarketDataService.search_bonds(q)}


@router.get("/filters", response_model=BondFiltersResponse, tags=["Bonds"])
async def get_bond_filters():
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    
    bases = set()
    for isin, data in cache.items():
        ref = create_bond_ref_data(data, isin)
        bases.add(ref.base)
        
    return BondFiltersResponse(
        issuers=["MOEX Issuers"], # Placeholder, would extract from actual data if available
        classes=["Floater"],
        base_rates=sorted(list(bases - {"UNKNOWN"})),
        maturities=["1Y", "3Y", "5Y", "10Y"] # Placeholder, can be generated dynamically
    )


@router.get("/{isin}", response_model=BondDetailsResponse, tags=["Bonds"])
async def get_bond_details(isin: str = Path(...)):
    isin = _require_isin(isin)
    cache = MarketDataService.get_local_bond_cache(
        _cache_path("isins_cache.json"))
    from services.bond_details import build_bond_details
    return BondDetailsResponse(**await build_bond_details(isin, cache))


@router.get("/{isin}/candles", tags=["Bonds"])
async def get_bond_candles(
    isin: str = Path(...),
    tf: Literal["5m", "1h", "1d", "1w"] = Query("1d", description="Таймфрейм свечи"),
    board: str = Query("TQCB", description="Борд MOEX (TQCB корп / TQOB ОФЗ)"),
    secid: Optional[str] = Query(None, description="SECID (для ОФЗ ≠ ISIN)"),
):
    """OHLCV-свечи MOEX для графика карточки. 5m — агрегация 1-мин. Для ОФЗ
    передавать board=TQOB и secid (SU26…), т.к. по ISIN candles не резолвятся."""
    isin = _require_isin(isin)
    sec = secid or isin
    if not _SECID_RE.fullmatch(sec) or board not in ("TQCB", "TQOB"):
        raise HTTPException(status_code=400, detail="bad secid/board")
    return {"isin": isin, "tf": tf, "candles": await MarketDataService.fetch_candles(sec, tf, board)}



@router.get("/{isin}/cashflow", response_model=CashflowResponse, tags=["Bonds"])
async def get_bond_cashflow(isin: str = Path(...)):
    # Re-use logic from get_bond_details internally to stay DRY in a real app
    # Here extending it directly for clarity
    isin = _require_isin(isin)
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    data = cache.get(isin)
    
    if not data:
        raise NotFoundException(f"Bond {isin} not found in cache", {"isin": isin})
        
    ref_obj = create_bond_ref_data(data, isin)

    ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()
    if not calc_date:
        calc_date = rates_date or date.today()

    # Amort/offer-aware builder (тот же, что карточка) — прежний get_cashflow_items
    # игнорировал амортизацию: купоны на полный номинал + принципал одним бул-платежом.
    sched_full = await MarketDataService.fetch_bond_schedule_full(isin)
    curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    from services.cashflow import build_cashflow_from_moex
    from services.bonds import external_formula
    formula = data.get("FORMULA", "") or external_formula(ref_obj)
    cfs, fv = build_cashflow_from_moex(
        ref_obj, curve, calc_date,
        sched_full.get("coupons", []), sched_full.get("amorts", []), formula,
        offers=sched_full.get("offers"),
    )

    return CashflowResponse(
        isin=isin,
        calc_date=calc_date,
        items=cfs,
        redemption_amount=fv
    )

@router.get("/{isin}/reprice", response_model=RepriceResponse, tags=["Bonds"])
async def reprice_bond_valuation(
    isin: str = Path(...),
    price: float = Query(..., gt=0, le=1000, description="Чистая цена, % от номинала"),
):
    """Пересчёт цена-зависимых метрик (SM/DM/YTM/dirty/Y-IDX/z_model/carry) под
    произвольную чистую цену. Использует калькулятор карточки И live-рефреш строки
    таблицы по WS-тику. Тёплые кэши → мгновенно."""
    isin = _require_isin(isin)
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    from services.bond_details import reprice_bond
    metrics = await reprice_bond(isin, price, cache)
    return RepriceResponse(**metrics)
