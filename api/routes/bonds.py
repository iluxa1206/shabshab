import os
import re
import asyncio
from datetime import datetime, date, timezone
from typing import List, Optional
from fastapi import APIRouter, Query, Path, HTTPException


async def _aempty():
    return {}

from api.schemas import (
    BondListItem, BondListResponse, BondFiltersResponse,
    BondDetailsResponse, CashflowResponse, ValuationResponse,
    BondMarketData, BondValuation, BondNrd, FloaterRisk
)
from services.market_data import MarketDataService
from services.bonds import (
    create_bond_ref_data, extract_bond_reference_dict, next_coupon_after,
    build_ref_external, external_formula, reconcile_face,
)
from services.cashflow import get_cashflow_items, build_cashflow_from_moex
from services.valuation import calculate_valuation_metrics
from services.zspread import compute_z_bps, project_cfs, solve_flat_y
from services.exceptions import NotFoundException, CalculationException
from services import nrd as nrd_service
from services import metrics
from cashflow import read_isins_from_file
import logging

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


def build_bond_nrd(mapped: dict, last_price: Optional[float]) -> Optional[BondNrd]:
    """Собирает BondNrd, досчитывает price_vs_nrd (рынок vs справедливая/цена НРД)."""
    if not mapped:
        return None
    data = dict(mapped)
    ref = data.get("fair_value_pct") or data.get("nrd_price_pct")
    if last_price is not None and ref is not None:
        data["price_vs_nrd_pct"] = round(last_price - ref, 4)
    return BondNrd(**{k: v for k, v in data.items() if k in BondNrd.model_fields})

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
    base_dir = get_base_dir()
    cache = MarketDataService.get_local_bond_cache(os.path.join(base_dir, "isins_cache.json"))
    data = cache.get(isin)
    external = data is None

    # все независимые сетевые вызовы — одним gather (MOEX ISS ~3.5с/запрос,
    # последовательно карточка грузилась 10-17с)
    res = await asyncio.gather(
        nrd_service.fetch_nrd_metrics([isin]),                                        # 0
        MarketDataService.fetch_last_prices([isin]),                                  # 1
        MarketDataService.fetch_moex_snapshot([isin]),                                # 2
        MarketDataService.fetch_coupon_schedules([isin]),                             # 3
        MarketDataService.get_curves(),                                               # 4
        MarketDataService.fetch_bond_schedule_full(isin),                             # 5
        MarketDataService.fetch_moex_securities([isin]) if external else _aempty(),   # 6
        MarketDataService.fetch_moex_shortnames() if external else _aempty(),         # 7
        MarketDataService.get_zspread_ctx(),                                          # 8
        return_exceptions=True,
    )
    _ok = lambda x, d: d if isinstance(x, Exception) else x
    nrd_metrics = _ok(res[0], {})
    market_prices = _ok(res[1], {})
    snapshot = _ok(res[2], {})
    schedules = _ok(res[3], {})
    ruonia_curve, keyrate_curve, calc_date, rates_date = _ok(res[4], (None, None, None, None))
    sched_full = _ok(res[5], {"coupons": [], "amorts": []})
    mo_map = _ok(res[6], {})
    shortnames = _ok(res[7], {})
    exp_ks, exp_ru, g_curve = _ok(res[8], (None, None, None))

    if data:
        ref_obj = create_bond_ref_data(data, isin)
        ref_dict = extract_bond_reference_dict(isin, data, ref_obj)
    else:
        # любая бумага вне кэша — справочник MOEX + база/спред из НРД
        mo = mo_map.get(isin, {})
        if not mo and not nrd_metrics.get(isin):
            raise NotFoundException(f"Bond {isin} not found on MOEX/NRD", {"isin": isin})
        ref_obj = build_ref_external(isin, mo, nrd_metrics.get(isin))
        ref_dict = {
            "isin": isin,
            "short_name": shortnames.get(isin) or mo.get("name") or isin,
            "face_value": ref_obj.face_value,
            "face_unit": mo.get("face_unit") or "RUB",
            "base_rate_type": ref_obj.base,
            "spread_bps": ref_obj.spread_issue_bps,
            "formula": external_formula(ref_obj),
            "start_date": ref_obj.issue_date,
            "maturity_date": ref_obj.maturity_date,
            "coupon_period_days": ref_obj.coupon_period_days,
            "coupons_per_year": ref_obj.coupons_per_year,
            "next_coupon_date": None,
            "accrued_interest": ref_obj.accrued_rub,
        }

    last_price = market_prices.get(isin)
    prev_close_pct = snapshot.get(isin, {}).get("prev")
    accrued_live = snapshot.get(isin, {}).get("accrued")
    periods = schedules.get(isin)

    # Номинал: сверяем с фактом купона (value/valueprc); правит тихий фолбэк на 1000
    _cd_face = calc_date or date.today()
    if reconcile_face(ref_obj, (sched_full or {}).get("coupons"), _cd_face):
        ref_dict["face_value"] = ref_obj.face_value

    # НКД на calc_date из MOEX (приоритет над стейл-кэшем) — для dirty и карточки
    if accrued_live is not None:
        ref_obj.accrued_rub = accrued_live
        ref_dict["accrued_interest"] = accrued_live

    # ближайшая будущая оферта (bondization offers) — информационный флаг.
    # Оценку НЕ клэмпим: НРД dm тоже к погашению (сверка 2026-07-08 — клэмп
    # к оферте ухудшает совпадение на всех горизонтах), но цена бумаги
    # с близкой офертой может прайситься к ней → DM/z несопоставимы.
    next_offer = None
    try:
        future_offers = [(date.fromisoformat(o["date"]), o.get("type"))
                         for o in sched_full.get("offers", [])
                         if o.get("date") and date.fromisoformat(o["date"]) > date.today()]
        if future_offers:
            next_offer = min(future_offers)
            ref_dict["offer_date"] = next_offer[0]
            ref_dict["offer_type"] = next_offer[1]
    except (ValueError, TypeError):
        pass

    if not calc_date:
        calc_date = rates_date or date.today()
    if not rates_date:
        rates_date = date.today()

    # честный is_stale: ставки не сегодняшние (выходные/до обновления Cbonds)
    is_stale = rates_date < date.today()

    market_data = BondMarketData(
        last_price_pct=last_price,
        price_source="Alor WebSocket",
        calc_date=calc_date,
        rates_date=rates_date,
        market_timestamp=datetime.now(timezone.utc),
        is_stale=is_stale,
        prev_close_clean_pct=prev_close_pct,
        prev_close_dm_bps=None
    )
    
    curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    cfs = []

    # Cashflow по реальному расписанию MOEX: прошлые купоны = факт, будущие = прогноз
    formula = (data.get("FORMULA", "") if data else "") or external_formula(ref_obj)
    try:
        cfs, _ = build_cashflow_from_moex(
            ref_obj, curve, calc_date,
            sched_full.get("coupons", []), sched_full.get("amorts", []), formula,
        )
    except Exception as e:
        logger.warning(f"Cashflow error for {isin}: {e}")
        
    val_dict = {
        "clean_price_pct": last_price or 100.0,
        "dirty_price_rub": ref_obj.face_value + ref_obj.accrued_rub, # fallback
        "dm_bps": None, "dm_label": None, "yield_xirr_pct": None,
        "base_yield_pct": None, "spread_to_base_bps": None,
        "pricing_status": "NO_MARKET_DATA",
        "warnings": ["No market price available, using Par (100.00) for dirty calc where needed"]
    }
    
    if last_price is not None and curve:
        try:
            val_dict = calculate_valuation_metrics(
                ref_obj, last_price, curve, calc_date,
                accrued_override=accrued_live, periods=periods,
                amorts=sched_full.get("amorts"), offers=sched_full.get("offers"),
            )
        except Exception as e:
            val_dict["pricing_status"] = "CALCULATION_ERROR"
            val_dict["warnings"] = [str(e)]
            
    if prev_close_pct is not None and curve:
        try:
            prev_metrics = calculate_valuation_metrics(
                ref_obj, prev_close_pct, curve, calc_date,
                accrued_override=accrued_live, periods=periods,
                amorts=sched_full.get("amorts"), offers=sched_full.get("offers"),
            )
            market_data.prev_close_dm_bps = prev_metrics.get("dm_bps")
        except Exception:
            pass
            
    valuation_data = BondValuation(**val_dict)

    # блок флоатер-риска: spread duration (Macaulay проектных потоков), rate duration
    # (≈ до рефиксинга), carry vs база, breakeven. Считаем на нашей кривой ожиданий.
    floater_block = None
    if ref_obj.base in ("RUONIA", "KEYRATE"):
        try:
            nm = nrd_metrics.get(isin, {}) or {}
            exp = exp_ru if ref_obj.base == "RUONIA" else exp_ks
            coupons = sched_full.get("coupons", [])
            px = last_price or prev_close_pct or nm.get("nrd_price_pct")
            spread_dur = None
            if exp and px:
                # те же потоки и цена, что в z-модели: face_for_pricing (T+1
                # ex-амортизация) и offers (обрезка по оферте при пересмотре купона)
                from valuation import face_for_pricing
                dirty = (face_for_pricing(ref_obj.face_value, sched_full.get("amorts"), calc_date)
                         * px / 100.0 + (accrued_live or ref_obj.accrued_rub or 0.0))
                zcfs = project_cfs(ref_obj, exp, calc_date, coupons,
                                   sched_full.get("amorts"), sched_full.get("offers"))
                y = solve_flat_y(zcfs, calc_date, dirty)
                if y is not None:
                    spread_dur = metrics.macaulay_years(zcfs, calc_date, y)
            cb = metrics.carry_refix_block(coupons, sched_full.get("amorts"),
                                           ref_obj.face_value, px, exp,
                                           nm.get("current_yield_pct"), calc_date)
            refix = cb["days_to_refix"]
            floater_block = FloaterRisk(
                spread_duration_yrs=round(spread_dur, 3) if spread_dur is not None else None,
                rate_duration_yrs=round(refix / 365.0, 3) if refix is not None else None,
                days_to_refix=refix, current_coupon_pct=cb["current_coupon_pct"],
                base_rate_pct=cb["base_rate_pct"], carry_bps=cb["carry_bps"],
                breakeven_base_pct=metrics.breakeven_base_pct(
                    cb["coupon_yield_pct"], cb["base_rate_pct"], ref_obj.spread_issue_bps),
                mod_duration=nm.get("mod_duration"), convexity=nm.get("convexity"), pvbp=nm.get("pvbp"),
            )
        except Exception as e:
            logger.warning(f"Floater risk error for {isin}: {e}")

    nrd_block = None
    warnings = []
    if next_offer:
        warnings.append(
            f"Оферта {next_offer[0].isoformat()}: первостепенны метрики к оферте "
            "(sm/dm/yield_to_offer, yield-to-put); к погашению — вторичные "
            "(sm_bps/disc_margin_bps, сверка с НРД)")
    # nrd_metrics уже получен в начале хендлера — не дёргаем НРД повторно
    try:
        nrd_block = build_bond_nrd(nrd_metrics.get(isin, {}), last_price)
    except Exception as e:
        logger.warning(f"NRD details error for {isin}: {e}")
    if nrd_block is None and nrd_service.is_configured():
        warnings.append("NRD data unavailable for this bond")
    elif not nrd_service.is_configured():
        warnings.append("NRD source not configured (set NRD_LOGIN / NRD_APIKEY in .env)")

    return BondDetailsResponse(
        reference=ref_dict,
        market=market_data,
        valuation=valuation_data,
        cashflow=cfs,
        nrd=nrd_block,
        floater=floater_block,
        sources={"details": "MOEX", "market": "Alor", "nrd": "NRD Price Center"},
        warnings=warnings
    )


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
    block = build_bond_nrd(nrd_metrics.get(isin, {}), last_price)
    if block is None:
        raise NotFoundException(f"NRD data unavailable for {isin}", {"isin": isin})
    return block


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
