import os
import asyncio
from datetime import datetime, date, timezone
from typing import List, Optional
from fastapi import APIRouter, Query, Path


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
    build_ref_external, external_formula,
)
from services.cashflow import get_cashflow_items, build_cashflow_from_moex
from services.valuation import calculate_valuation_metrics
from services.zspread import compute_z_bps, project_cfs, solve_flat_y
from services.exceptions import NotFoundException, CalculationException
from services import nrd as nrd_service
from services import metrics, history
from cashflow import read_isins_from_file

router = APIRouter()


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
    """Фоновый расчёт полных метрик (dirty/DM/z_model/carry/next_coupon/delta) для
    бумаг юниверса вне watchlist. Данные MOEX батчатся (board snapshot одним запросом)
    и кэшируются на день (bondization, справочник). Возвращает {isin: {метрики}}.
    Логика идентична watch-ветке enrich(), но по всему юниверсу из кэшей."""
    want = {i for i in isins if i}
    uni_by = {u["isin"]: u for u in uni if u.get("isin") in want}
    ids = list(uni_by.keys())
    if not ids:
        return {}

    base_dir = get_base_dir()
    cache = MarketDataService.get_local_bond_cache(os.path.join(base_dir, "isins_cache.json"))
    external = [i for i in ids if i not in cache]

    prices = MarketDataService.cached_prices()
    board, curves, zctx, secs = await asyncio.gather(
        MarketDataService.fetch_board_snapshot(),
        MarketDataService.get_curves(),
        MarketDataService.get_zspread_ctx(),
        MarketDataService.fetch_moex_securities(external) if external else _aempty(),
    )
    ruonia_curve, keyrate_curve, cd, rd = curves
    exp_ks, exp_ru, g_curve = zctx
    calc_date = cd or rd or date.today()
    # bondization (купоны+амортизации) — из day-кэша, MOEX только на прогреве
    fulls = await asyncio.gather(*(MarketDataService.fetch_bond_schedule_full(i) for i in ids))
    full_by = dict(zip(ids, fulls))

    out: dict = {}
    for isin in ids:
        u = uni_by[isin]
        base = u.get("base_rate_type", "UNKNOWN")
        spread = u.get("spread_issue_bps") or 0
        snap = board.get(isin, {})
        last = prices.get(isin)
        prev = snap.get("prev")
        delta = round(last - float(prev), 4) if (last is not None and prev is not None) else None

        data = cache.get(isin)
        ref = create_bond_ref_data(data, isin) if data else build_ref_external(isin, secs.get(isin, {}), {
            "base_coupon_index": ("CBRATED" if base == "KEYRATE" else "RUONIARATED" if base == "RUONIA" else None),
            "nominal_margin_bps": spread,
        })
        curve = ruonia_curve if base == "RUONIA" else keyrate_curve
        full = full_by.get(isin) or {}
        coupons_full = full.get("coupons") or []
        periods = None
        if coupons_full:
            periods = []
            for c in coupons_full:
                try:
                    periods.append((date.fromisoformat(c["start"]), date.fromisoformat(c["end"]), c.get("value")))
                except Exception:
                    pass
        # цена для расчёта: live → prev-close → НРД (не зависим от момента WS-цены)
        price_calc = last if last is not None else (prev if prev is not None else u.get("nrd_price_pct"))

        dirty = dm = z_model = None
        if price_calc is not None and curve and base in ("RUONIA", "KEYRATE"):
            try:
                m = calculate_valuation_metrics(ref, price_calc, curve, calc_date,
                                                accrued_override=snap.get("accrued"), periods=periods)
                dirty, dm = m.get("dirty_price_rub"), m.get("dm_bps")
            except Exception:
                pass

        next_cpn = None
        if periods:
            future = [e for (_s, e, _v) in periods if e > calc_date]
            next_cpn = min(future) if future else None
        if next_cpn is None:
            try:
                next_cpn = next_coupon_after(ref, calc_date)
            except Exception:
                pass

        exp = exp_ru if base == "RUONIA" else exp_ks
        if exp and g_curve and price_calc is not None and base in ("RUONIA", "KEYRATE"):
            try:
                coupons = ([{"start": c.get("start"), "end": c.get("end"), "value": c.get("value")} for c in coupons_full]
                           if coupons_full else
                           [{"start": s.isoformat(), "end": e.isoformat(), "value": v} for (s, e, v) in (periods or [])])
                z_model = compute_z_bps(ref, exp, g_curve, calc_date, price_calc,
                                        snap.get("accrued") or ref.accrued_rub, coupons, full.get("amorts"))
            except Exception:
                pass

        carry = refix = cur_cpn = None
        try:
            cpns = coupons_full or [{"start": s.isoformat(), "end": e.isoformat(), "value": v} for (s, e, v) in (periods or [])]
            refix = metrics.days_to_refix(cpns, calc_date)
            cur_cpn = metrics.current_coupon_pct(cpns, calc_date)
            cp = metrics.current_period(cpns, calc_date)
            if cur_cpn is None and cp and cp.get("value") is not None:
                cdays = (cp["end"] - cp["start"]).days or 1
                cur_cpn = round(float(cp["value"]) / ref.face_value * 365.0 / cdays * 100.0, 3)
            coupon_yield = u.get("current_yield_pct") or cur_cpn
            carry = metrics.carry_bps(coupon_yield, metrics.base_level_pct(exp))
        except Exception:
            pass

        out[isin] = {"last": last, "dirty": dirty, "dm": dm, "delta": delta,
                     "next_coupon": next_cpn, "z_model": z_model, "carry": carry,
                     "refix": refix, "current_coupon": cur_cpn}
    return out


def _uni_item(u, name, last, dirty=None, dm=None, delta=None, next_coupon=None, z_model=None,
              spread_dur_yrs=None, z_pctile=None, delta_z_dod=None, delta_z_mom=None,
              carry_bps=None, days_to_refix=None, current_coupon_pct=None):
    base = u.get("base_rate_type", "UNKNOWN")
    spread = u.get("spread_issue_bps") or 0
    label = _BASE_LABEL.get(base, base)
    formula = f"{label} + {spread / 100:g}%" if spread else label
    nrd_price = u.get("nrd_price_pct")
    vs_nrd = round(last - nrd_price, 4) if (last is not None and nrd_price is not None) else None
    return BondListItem(
        isin=u["isin"], short_name=name, base_rate_type=base,
        formula=formula, spread_issue_bps=int(spread), maturity_date=u.get("maturity_date"),
        next_coupon_date=next_coupon, last_price_pct=last, dirty_price_rub=dirty, dm_bps=dm,
        delta_to_prev_close=delta, nrd_price_pct=nrd_price, price_vs_nrd_pct=vs_nrd,
        nrd_duration=u.get("nrd_duration"), discount_margin_bps=u.get("discount_margin_bps"),
        z_spread_bps=u.get("z_spread_bps"), rating=u.get("rating"), z_model_bps=z_model,
        spread_dur_yrs=spread_dur_yrs, z_pctile=z_pctile, delta_z_dod=delta_z_dod,
        delta_z_mom=delta_z_mom,
        carry_bps=carry_bps, days_to_refix=days_to_refix, current_coupon_pct=current_coupon_pct,
    )


_cross_cache = {"date": None, "map": {}}

def _cross_section_map(uni: list) -> dict:
    """{isin: (spread_dur_yrs, z_pctile, Δz_dod, Δz_mom)} по всему рынку.
    Записывает снапшот истории и считает кросс-секцию РАЗ В ДЕНЬ — зависит только
    от дневного НРД-юниверса, незачем повторять на каждый запрос дашборда."""
    today = date.today().isoformat()
    if _cross_cache["date"] == today and _cross_cache["map"]:
        return _cross_cache["map"]
    try:
        history.record_snapshot(uni)
    except Exception:
        pass
    dod_z = mom_z = {}
    try:
        dod_z = history.dod_map("z")
        mom_z = history.change_map("z", 30)
    except Exception:
        pass
    bucket_z: dict = {}
    for u in uni:
        bucket_z.setdefault(u.get("rating"), []).append(u.get("z_spread_bps"))
    today0 = date.today()
    out: dict = {}
    for u in uni:
        mat = u.get("maturity_date")
        sd = metrics.years_to(date.fromisoformat(mat), today0) if mat else None
        zp = metrics.rank_pct(u.get("z_spread_bps"), bucket_z.get(u.get("rating"), []))
        out[u.get("isin")] = (sd, zp, dod_z.get(u.get("isin")), mom_z.get(u.get("isin")))
    _cross_cache["date"] = today
    _cross_cache["map"] = out
    return out


async def _universe_bonds(extra_list, cache, limit, offset):
    """Весь рынок флоатеров из НРД (кэш на день). НРД-аналитика по всем;
    live-цена Alor + наш DM — только для watchlist (extra), т.к. их мало."""
    uni = await nrd_service.fetch_floater_universe()
    if not uni:
        return BondListResponse(items=[], total=0, limit=limit, offset=offset)

    cached_prices = MarketDataService.cached_prices()
    uni_metrics = MarketDataService.universe_metrics()  # полные метрики вне watchlist (фон)
    shortnames = await MarketDataService.fetch_moex_shortnames()
    watch = set(extra_list)

    # кросс-секция (z-перцентиль, spread duration, Δz d/d и m/m) + запись истории —
    # зависят только от дневного НРД-юниверса, считаем раз в день (не на каждый запрос)
    cross = _cross_section_map(uni)

    def _cross(u):
        return cross.get(u.get("isin"), (None, None, None, None))

    # live-данные + наш DM только для watchlist
    market_prices = snapshot = schedules = moex_ref = sched_full = {}
    ruonia_curve = keyrate_curve = None
    exp_ks = exp_ru = g_curve = None
    calc_date = date.today()
    if watch:
        live = list(watch)
        external_live = [i for i in live if i not in cache]
        # Цену watch берём из cached_prices (фон-поллер 10мин + WS-broadcaster 5с
        # держат её тёплой) — НЕ блокируем ответ свежим Alor WS на каждый запрос.
        market_prices = cached_prices
        # независимые сетевые вызовы — параллельно (MOEX ISS ~3.5с/запрос,
        # последовательно давало ~17с; gather сводит к самому долгому)
        snapshot, schedules, moex_ref, curves, zctx, fulls = await asyncio.gather(
            MarketDataService.fetch_moex_snapshot(live),
            MarketDataService.fetch_coupon_schedules(live),
            MarketDataService.fetch_moex_securities(external_live) if external_live else _aempty(),
            MarketDataService.get_curves(),
            MarketDataService.get_zspread_ctx(),
            asyncio.gather(*(MarketDataService.fetch_bond_schedule_full(i) for i in live)),
        )
        sched_full = dict(zip(live, fulls))
        ruonia_curve, keyrate_curve, cd, rd = curves
        exp_ks, exp_ru, g_curve = zctx
        calc_date = cd or rd or date.today()

    def enrich(u):
        isin = u["isin"]
        base = u.get("base_rate_type", "UNKNOWN")
        spread = u.get("spread_issue_bps") or 0
        name = shortnames.get(isin) or u.get("name") or isin
        sd, zp, dz, dzm = _cross(u)
        if isin not in watch:
            mx = uni_metrics.get(isin, {})
            return _uni_item(u, name, mx.get("last", cached_prices.get(isin)),
                             dirty=mx.get("dirty"), dm=mx.get("dm"), delta=mx.get("delta"),
                             next_coupon=mx.get("next_coupon"), z_model=mx.get("z_model"),
                             spread_dur_yrs=sd, z_pctile=zp, delta_z_dod=dz, delta_z_mom=dzm,
                             carry_bps=mx.get("carry"), days_to_refix=mx.get("refix"),
                             current_coupon_pct=mx.get("current_coupon"))
        last = market_prices.get(isin)
        prev = snapshot.get(isin, {}).get("prev") or (moex_ref.get(isin) or {}).get("prev")
        delta = round(last - float(prev), 4) if (last is not None and prev is not None) else None
        dirty = dm = None
        data = cache.get(isin)
        ref = create_bond_ref_data(data, isin) if data else build_ref_external(isin, moex_ref.get(isin, {}), {
            "base_coupon_index": ("CBRATED" if base == "KEYRATE" else "RUONIARATED" if base == "RUONIA" else None),
            "nominal_margin_bps": spread,
        })
        curve = ruonia_curve if base == "RUONIA" else keyrate_curve
        # цена для расчёта dirty/DM: live → prev-close → НРД (модель не должна
        # зависеть от момента прихода WS-цены; при холодном Alor не обнуляемся)
        price_calc = last if last is not None else (prev if prev is not None else u.get("nrd_price_pct"))
        if price_calc is not None and curve and base in ("RUONIA", "KEYRATE"):
            try:
                m = calculate_valuation_metrics(ref, price_calc, curve, calc_date,
                                                accrued_override=snapshot.get(isin, {}).get("accrued"),
                                                periods=schedules.get(isin))
                dirty, dm = m.get("dirty_price_rub"), m.get("dm_bps")
            except Exception:
                pass
        # ближайший купон: из реального расписания MOEX (end > calc_date),
        # иначе оценка по ref (для внешних без issue падает на maturity)
        next_cpn = None
        sched = schedules.get(isin)
        if sched:
            future = [e for (_s, e, _v) in sched if e > calc_date]
            next_cpn = min(future) if future else None
        if next_cpn is None:
            try:
                next_cpn = next_coupon_after(ref, calc_date)
            except Exception:
                pass
        # наш z-спред, сопоставимый с НРД z_spread (zspread.py): RUONIA ±40bps,
        # KEYRATE mean|Δ|≈43bps (ytm−G, сверка 2026-07-02). value зафиксированных
        # купонов передаём — иначе текущий купон перепрогнозируется моделью.
        # Амортизации (bondization) — z_model амортизируемых бумаг считается
        # от остаточного номинала, принципал гасится по датам амортизаций
        z_model = None
        exp = exp_ru if base == "RUONIA" else exp_ks
        if exp and g_curve and price_calc is not None and base in ("RUONIA", "KEYRATE"):
            try:
                full = sched_full.get(isin) or {}
                if full.get("coupons"):
                    coupons = [{"start": c.get("start"), "end": c.get("end"), "value": c.get("value")}
                               for c in full["coupons"]]
                else:
                    coupons = [{"start": s.isoformat(), "end": e.isoformat(), "value": v}
                               for (s, e, v) in (sched or [])]
                z_model = compute_z_bps(ref, exp, g_curve, calc_date, price_calc,
                                        snapshot.get(isin, {}).get("accrued") or ref.accrued_rub,
                                        coupons, full.get("amorts"))
            except Exception:
                pass
        # watch-метрики: carry vs база, срок до рефиксинга, текущая ставка купона
        carry = refix = cur_cpn = None
        try:
            full = sched_full.get(isin) or {}
            cpns = full.get("coupons") or [{"start": s.isoformat(), "end": e.isoformat(), "value": v}
                                           for (s, e, v) in (sched or [])]
            refix = metrics.days_to_refix(cpns, calc_date)
            cur_cpn = metrics.current_coupon_pct(cpns, calc_date)
            cp = metrics.current_period(cpns, calc_date)
            if cur_cpn is None and cp and cp.get("value") is not None:
                cdays = (cp["end"] - cp["start"]).days or 1
                cur_cpn = round(float(cp["value"]) / ref.face_value * 365.0 / cdays * 100.0, 3)
            coupon_yield = u.get("current_yield_pct") or cur_cpn
            carry = metrics.carry_bps(coupon_yield, metrics.base_level_pct(exp))
        except Exception:
            pass
        return _uni_item(u, name, last, dirty, dm, delta, next_cpn, z_model,
                         spread_dur_yrs=sd, z_pctile=zp, delta_z_dod=dz, delta_z_mom=dzm,
                         carry_bps=carry, days_to_refix=refix, current_coupon_pct=cur_cpn)

    items = [enrich(u) for u in uni]
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
            print(f"NRD list fetch error: {e}")

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

        nrd_price_pct = price_vs_nrd_pct = nrd_duration = nrd_dm_bps = nrd_z_bps = None
        nm = nrd_metrics.get(isin)
        if nm:
            nrd_price_pct = nm.get("fair_value_pct") or nm.get("nrd_price_pct")
            nrd_duration = nm.get("duration")
            nrd_dm_bps = nm.get("discount_margin_bps")
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
                z_spread_bps=nrd_z_bps,
            )
        )

    return BondListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/search", tags=["Bonds"])
async def search_bonds(q: str = Query(..., min_length=2)):
    """Поиск облигаций на MOEX по названию/ISIN для добавления в список."""
    import httpx
    url = ("https://iss.moex.com/iss/securities.json"
           "?iss.meta=off&limit=50&securities.columns=secid,isin,shortname,type,is_traded")
    out = []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params={"q": q}, timeout=8)
        sec = resp.json().get("securities", {})
        cols, rows = sec.get("columns", []), sec.get("data", [])
        g = lambda row, n: row[cols.index(n)] if n in cols else None
        seen = set()
        for row in rows:
            isin = g(row, "isin") or g(row, "secid")
            typ = (g(row, "type") or "")
            if not isin or not str(isin).startswith("RU") or "bond" not in typ:
                continue
            if isin in seen:
                continue
            seen.add(isin)
            out.append({"isin": isin, "name": g(row, "shortname"), "type": typ, "traded": bool(g(row, "is_traded"))})
    except Exception as e:
        print(f"MOEX search error: {e}")
    return {"items": out[:20]}


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

    # НКД на calc_date из MOEX (приоритет над стейл-кэшем) — для dirty и карточки
    if accrued_live is not None:
        ref_obj.accrued_rub = accrued_live
        ref_dict["accrued_interest"] = accrued_live

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
        print(f"Cashflow error for {isin}: {e}")
        
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
            )
        except Exception as e:
            val_dict["pricing_status"] = "CALCULATION_ERROR"
            val_dict["warnings"] = [str(e)]
            
    if prev_close_pct is not None and curve:
        try:
            prev_metrics = calculate_valuation_metrics(
                ref_obj, prev_close_pct, curve, calc_date,
                accrued_override=accrued_live, periods=periods,
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
            spread_dur = None
            if exp and (last_price or prev_close_pct or nm.get("nrd_price_pct")):
                px = last_price or prev_close_pct or nm.get("nrd_price_pct")
                dirty = ref_obj.face_value * px / 100.0 + (accrued_live or ref_obj.accrued_rub or 0.0)
                zcfs = project_cfs(ref_obj, exp, calc_date, coupons, sched_full.get("amorts"))
                y = solve_flat_y(zcfs, calc_date, dirty)
                if y is not None:
                    spread_dur = metrics.macaulay_years(zcfs, calc_date, y)
            refix = metrics.days_to_refix(coupons, calc_date)
            cur_cpn = metrics.current_coupon_pct(coupons, calc_date)
            cp = metrics.current_period(coupons, calc_date)
            if cur_cpn is None and cp and cp.get("value") is not None:
                cdays = (cp["end"] - cp["start"]).days or 1
                cur_cpn = round(float(cp["value"]) / ref_obj.face_value * 365.0 / cdays * 100.0, 3)
            base_lvl = metrics.base_level_pct(exp)
            coupon_yield = nm.get("current_yield_pct") or cur_cpn
            carry = metrics.carry_bps(coupon_yield, base_lvl)
            floater_block = FloaterRisk(
                spread_duration_yrs=round(spread_dur, 3) if spread_dur is not None else None,
                rate_duration_yrs=round(refix / 365.0, 3) if refix is not None else None,
                days_to_refix=refix, current_coupon_pct=cur_cpn, base_rate_pct=base_lvl,
                carry_bps=carry,
                breakeven_base_pct=metrics.breakeven_base_pct(coupon_yield, base_lvl, ref_obj.spread_issue_bps),
                mod_duration=nm.get("mod_duration"), convexity=nm.get("convexity"), pvbp=nm.get("pvbp"),
            )
        except Exception as e:
            print(f"Floater risk error for {isin}: {e}")

    nrd_block = None
    warnings = []
    # nrd_metrics уже получен в начале хендлера — не дёргаем НРД повторно
    try:
        nrd_block = build_bond_nrd(nrd_metrics.get(isin, {}), last_price)
    except Exception as e:
        print(f"NRD details error for {isin}: {e}")
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
