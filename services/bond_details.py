"""Сборка карточки бумаги (вынесена из api/routes/bonds.py — оркестрация
9 источников + расчёты не место в route-хендлере). Возвращает plain dicts,
Pydantic-модели (BondDetailsResponse и вложенные) коэрсятся на route-слое.
"""
import asyncio
import logging
from datetime import datetime, date, timezone
from typing import Optional

from services.market_data import MarketDataService
from services import nrd as nrd_service
from services import metrics
from services.bonds import (
    create_bond_ref_data, extract_bond_reference_dict,
    build_ref_external, external_formula, reconcile_face,
)
from services.valuation import calculate_valuation_metrics
from services.cashflow import build_cashflow_from_moex
from services.zspread import project_cfs, solve_flat_y
from services.exceptions import NotFoundException

logger = logging.getLogger(__name__)


async def _aempty():
    return {}


def nrd_view(mapped: dict, last_price: Optional[float]) -> Optional[dict]:
    """НРД-блок карточки + price_vs_nrd (рынок vs справедливая/цена НРД)."""
    if not mapped:
        return None
    data = dict(mapped)
    ref = data.get("fair_value_pct") or data.get("nrd_price_pct")
    if last_price is not None and ref is not None:
        data["price_vs_nrd_pct"] = round(last_price - ref, 4)
    return data


async def build_bond_details(isin: str, cache: dict) -> dict:
    """Полная карточка: reference/market/valuation/cashflow/nrd/floater/warnings."""
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
    market_data = {
        "last_price_pct": last_price,
        "price_source": "Alor WebSocket",
        "calc_date": calc_date,
        "rates_date": rates_date,
        "market_timestamp": datetime.now(timezone.utc),
        "is_stale": rates_date < date.today(),
        "prev_close_clean_pct": prev_close_pct,
        "prev_close_dm_bps": None,
    }

    curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    cfs = []

    # Cashflow по реальному расписанию MOEX: прошлые купоны = факт, будущие = прогноз
    formula = (data.get("FORMULA", "") if data else "") or external_formula(ref_obj)
    try:
        cfs, _ = build_cashflow_from_moex(
            ref_obj, curve, calc_date,
            sched_full.get("coupons", []), sched_full.get("amorts", []), formula,
            offers=sched_full.get("offers"),
        )
    except Exception as e:
        logger.warning(f"Cashflow error for {isin}: {e}")

    val_dict = {
        "clean_price_pct": last_price or 100.0,
        "dirty_price_rub": ref_obj.face_value + ref_obj.accrued_rub,  # fallback
        "dm_bps": None, "dm_label": None, "yield_xirr_pct": None,
        "index_yield_pct": None, "yield_over_index_bps": None,
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
            market_data["prev_close_dm_bps"] = prev_metrics.get("dm_bps")
        except Exception:
            pass

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
            floater_block = {
                "spread_duration_yrs": round(spread_dur, 3) if spread_dur is not None else None,
                "rate_duration_yrs": round(refix / 365.0, 3) if refix is not None else None,
                "days_to_refix": refix, "current_coupon_pct": cb["current_coupon_pct"],
                "base_rate_pct": cb["base_rate_pct"], "carry_bps": cb["carry_bps"],
                "breakeven_base_pct": metrics.breakeven_base_pct(
                    cb["coupon_yield_pct"], cb["base_rate_pct"], ref_obj.spread_issue_bps),
                "mod_duration": nm.get("mod_duration"), "convexity": nm.get("convexity"),
                "pvbp": nm.get("pvbp"),
            }
        except Exception as e:
            logger.warning(f"Floater risk error for {isin}: {e}")

    warnings = []
    if next_offer:
        warnings.append(
            f"Оферта {next_offer[0].isoformat()}: первостепенны метрики к оферте "
            "(sm/dm/yield_to_offer, yield-to-put); к погашению — вторичные "
            "(sm_bps/disc_margin_bps, сверка с НРД)")
    nrd_block = None
    try:
        nrd_block = nrd_view(nrd_metrics.get(isin, {}), last_price)
    except Exception as e:
        logger.warning(f"NRD details error for {isin}: {e}")
    if nrd_block is None and nrd_service.is_active():
        warnings.append("NRD data unavailable for this bond")
    elif not nrd_service.is_active():
        # НРД-слой выключен (базовый режим) — метрики считаются по нашим кривым,
        # NRD-обогащение (цена/fair-value/duration) недоступно
        warnings.append("NRD source disabled")

    return {
        "reference": ref_dict,
        "market": market_data,
        "valuation": val_dict,
        "cashflow": cfs,
        "nrd": nrd_block,
        "floater": floater_block,
        "sources": {"details": "MOEX", "market": "Alor", "nrd": "NRD Price Center"},
        "warnings": warnings,
    }
