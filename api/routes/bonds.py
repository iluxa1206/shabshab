import os
import re
import logging
from datetime import date
from typing import Optional
from fastapi import APIRouter, Query, Path, HTTPException

from api.schemas import (
    BondListItem, BondListResponse, BondFiltersResponse,
    BondDetailsResponse, CashflowResponse, ValuationResponse, BondNrd,
)
from services.market_data import MarketDataService
from services.bonds import (
    create_bond_ref_data, build_ref_external, external_formula, next_coupon_after,
)
from services.cashflow import get_cashflow_items
from services.valuation import calculate_valuation_metrics
from services.exceptions import NotFoundException, CalculationException
from services import nrd as nrd_service
from cashflow import read_isins_from_file

logger = logging.getLogger(__name__)

router = APIRouter()

# ISO 6166; тот же паттерн, что в funds. Валидируем ВСЕ входные ISIN: они
# интерполируются в URL к MOEX/Alor f-строками — мусор/`..%2F` не должен уходить
# во внешние запросы.
_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


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
    return await _cum(uni, isins, os.path.join(get_base_dir(), "isins_cache.json"))


def _uni_item(u, name, mx, cross):
    """BondListItem: строка НРД-юниверса + наши метрики mx (universe.enrich_bond)
    + кросс-секция (spread_dur, z-перцентиль, Δz)."""
    base = u.get("base_rate_type", "UNKNOWN")
    spread = u.get("spread_issue_bps") or 0
    label = _BASE_LABEL.get(base, base)
    formula = f"{label} + {spread / 100:g}%" if spread else label
    last = mx.get("last")
    nrd_price = u.get("nrd_price_pct")
    vs_nrd = round(last - nrd_price, 4) if (last is not None and nrd_price is not None) else None
    sd, zp, dz, dzm = cross
    return BondListItem(
        isin=u["isin"], short_name=name, base_rate_type=base, formula=formula,
        spread_issue_bps=int(spread), maturity_date=u.get("maturity_date"),
        next_coupon_date=mx.get("next_coupon"), last_price_pct=last,
        dirty_price_rub=mx.get("dirty"), dm_bps=mx.get("dm"),
        delta_to_prev_close=mx.get("delta"), nrd_price_pct=nrd_price,
        price_vs_nrd_pct=vs_nrd, nrd_duration=u.get("nrd_duration"),
        discount_margin_bps=u.get("discount_margin_bps"),
        simple_margin_bps=u.get("simple_margin_bps"), disc_margin_bps=mx.get("disc_dm"),
        yield_over_index_bps=mx.get("yoi"),
        z_spread_bps=u.get("z_spread_bps"), rating=u.get("rating"),
        z_model_bps=mx.get("z_model"), spread_dur_yrs=sd, z_pctile=zp,
        delta_z_dod=dz, delta_z_mom=dzm, carry_bps=mx.get("carry"),
        days_to_refix=mx.get("refix"), current_coupon_pct=mx.get("current_coupon"),
        preferred_horizon=mx.get("horizon") or "maturity", offer_date=mx.get("offer_date"),
        sm_to_offer_bps=mx.get("sm_to_offer"), disc_margin_to_offer_bps=mx.get("dm_to_offer"),
    )


async def _universe_bonds(extra_list, cache, limit, offset):
    """Весь рынок флоатеров из НРД (кэш на день). НРД-аналитика по всем;
    live-метрики — только для watchlist (extra). Расчёты в services.universe."""
    from services import universe as universe_svc
    uni = await nrd_service.fetch_floater_universe()
    if not uni:
        return BondListResponse(items=[], total=0, limit=limit, offset=offset)

    cached_prices = MarketDataService.cached_prices()
    uni_metrics = MarketDataService.universe_metrics()  # фоновый поллер
    shortnames = await MarketDataService.fetch_moex_shortnames()
    watch = set(extra_list)
    cross = universe_svc.cross_section_map(uni)

    watch_rows = [u for u in uni if u.get("isin") in watch]
    watch_metrics = await universe_svc.compute_watch_metrics(watch_rows, cache) if watch_rows else {}

    items = []
    for u in uni:
        isin = u["isin"]
        name = shortnames.get(isin) or u.get("name") or isin
        mx = watch_metrics.get(isin) or uni_metrics.get(isin)
        if mx is None:
            mx = {"last": cached_prices.get(isin)}
        items.append(_uni_item(u, name, mx, cross.get(isin, (None, None, None, None))))
    return BondListResponse(items=items[offset:offset + limit], total=len(items), limit=limit, offset=offset)


@router.get("", response_model=BondListResponse, tags=["Bonds"])
async def get_bonds(
    limit: int = Query(50, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    with_market: bool = Query(True),
    with_valuation: bool = Query(False),
    with_nrd: bool = Query(False),
    universe: bool = Query(False, description="Весь юниверс флоатеров из НРД"),
    extra: Optional[str] = Query(None, description="Доп. ISIN'ы (через запятую) — любые бумаги вне списка"),
    fields: Optional[str] = Query(None)
):
    base_dir = get_base_dir()
    isins_path = os.path.join(base_dir, "isins.txt")
    cache_path = os.path.join(base_dir, "isins_cache.json")

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
    nrd_metrics = {}
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

    if with_nrd or external:
        try:
            nrd_metrics = await nrd_service.fetch_nrd_metrics(paginated_isins if with_nrd else external)
        except Exception as e:
            logger.warning(f"NRD list fetch error: {e}")

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
            # внешняя бумага: справочник MOEX + база/спред из НРД
            ref_obj = build_ref_external(isin, moex_ref.get(isin, {}), nrd_metrics.get(isin))
            short_name = (moex_ref.get(isin) or {}).get("name") or isin
            formula = external_formula(ref_obj)

        last_price_pct = prev_close_pct = dirty_price_rub = dm_bps = delta_to_prev_close = None

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
            except Exception:
                pass

        nrd_price_pct = price_vs_nrd_pct = nrd_duration = nrd_dm_bps = nrd_z_bps = nrd_sm_bps = None
        nm = nrd_metrics.get(isin)
        if nm:
            nrd_price_pct = nm.get("fair_value_pct") or nm.get("nrd_price_pct")
            nrd_duration = nm.get("duration")
            nrd_dm_bps = nm.get("discount_margin_bps")
            nrd_sm_bps = nm.get("simple_margin_bps")
            nrd_z_bps = nm.get("z_spread_bps")
            if last_price_pct is not None and nrd_price_pct is not None:
                price_vs_nrd_pct = round(last_price_pct - nrd_price_pct, 4)

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
                nrd_price_pct=nrd_price_pct,
                price_vs_nrd_pct=price_vs_nrd_pct,
                nrd_duration=nrd_duration,
                discount_margin_bps=nrd_dm_bps,
                simple_margin_bps=nrd_sm_bps,
                z_spread_bps=nrd_z_bps,
            )
        )

    return BondListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/search", tags=["Bonds"])
async def search_bonds(q: str = Query(..., min_length=2)):
    """Поиск облигаций на MOEX по названию/ISIN для добавления в список."""
    return {"items": await MarketDataService.search_bonds(q)}


@router.get("/filters", response_model=BondFiltersResponse, tags=["Bonds"])
async def get_bond_filters():
    base_dir = get_base_dir()
    cache = MarketDataService.get_local_bond_cache(os.path.join(base_dir, "isins_cache.json"))
    
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
        os.path.join(get_base_dir(), "isins_cache.json"))
    from services.bond_details import build_bond_details
    return BondDetailsResponse(**await build_bond_details(isin, cache))



@router.get("/{isin}/nrd", response_model=BondNrd, tags=["Bonds"])
async def get_bond_nrd(isin: str = Path(...)):
    isin = _require_isin(isin)
    base_dir = get_base_dir()
    cache = MarketDataService.get_local_bond_cache(os.path.join(base_dir, "isins_cache.json"))
    if isin not in cache:
        raise NotFoundException(f"Bond {isin} not found in cache", {"isin": isin})

    market_prices = await MarketDataService.fetch_last_prices([isin])
    last_price = market_prices.get(isin)

    nrd_metrics = await nrd_service.fetch_nrd_metrics([isin])
    from services.bond_details import nrd_view
    block = nrd_view(nrd_metrics.get(isin, {}), last_price)
    if block is None:
        raise NotFoundException(f"NRD data unavailable for {isin}", {"isin": isin})
    return BondNrd(**{k: v for k, v in block.items() if k in BondNrd.model_fields})


@router.get("/{isin}/cashflow", response_model=CashflowResponse, tags=["Bonds"])
async def get_bond_cashflow(isin: str = Path(...)):
    # Re-use logic from get_bond_details internally to stay DRY in a real app
    # Here extending it directly for clarity
    isin = _require_isin(isin)
    base_dir = get_base_dir()
    cache = MarketDataService.get_local_bond_cache(os.path.join(base_dir, "isins_cache.json"))
    data = cache.get(isin)
    
    if not data:
        raise NotFoundException(f"Bond {isin} not found in cache", {"isin": isin})
        
    ref_obj = create_bond_ref_data(data, isin)
    ref_dict = extract_bond_reference_dict(isin, data, ref_obj)

    ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()
    if not calc_date:
        calc_date = rates_date or date.today()

    cfs, fv = get_cashflow_items(
        isin=isin,
        start_date=ref_obj.issue_date,
        end_date=ref_obj.maturity_date,
        coupon_period_days=ref_obj.coupon_period_days,
        face_value=ref_obj.face_value,
        formula=data.get("FORMULA", ""),
        base_rate=ref_obj.base,
        ruonia_curve=ruonia_curve,
        keyrate_curve=keyrate_curve,
        calc_date=calc_date,
        coupon_percent=data.get("COUPONPERCENT") and float(data.get("COUPONPERCENT")) or None,
        next_coupon_date=ref_dict.get("next_coupon_date")
    )
    
    return CashflowResponse(
        isin=isin,
        calc_date=calc_date,
        items=cfs,
        redemption_amount=fv
    )

@router.get("/{isin}/valuation", response_model=ValuationResponse, tags=["Bonds"])
async def get_bond_valuation(isin: str = Path(...)):
    isin = _require_isin(isin)
    base_dir = get_base_dir()
    cache = MarketDataService.get_local_bond_cache(os.path.join(base_dir, "isins_cache.json"))
    data = cache.get(isin)
    
    if not data:
        raise NotFoundException(f"Bond {isin} not found in cache", {"isin": isin})
        
    ref_obj = create_bond_ref_data(data, isin)
    ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()
    if not calc_date:
        calc_date = rates_date or date.today()

    market_prices = await MarketDataService.fetch_last_prices([isin])
    last_price = market_prices.get(isin)

    if last_price is None:
        raise CalculationException("No market price available to compute valuation", {"isin": isin})
        
    curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    metrics = calculate_valuation_metrics(ref_obj, last_price, curve, calc_date)
    
    return ValuationResponse(
        isin=isin,
        calc_date=calc_date,
        **metrics
    )
