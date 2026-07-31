"""Сборка карточки бумаги (вынесена из api/routes/bonds.py — оркестрация
9 источников + расчёты не место в route-хендлере). Возвращает plain dicts,
Pydantic-модели (BondDetailsResponse и вложенные) коэрсятся на route-слое.
"""
import asyncio
import logging
from datetime import datetime, date, timezone
from typing import Optional

from services.market_data import MarketDataService
from services import metrics
from services.bonds import (
    create_bond_ref_data, extract_bond_reference_dict,
    build_ref_external, external_formula, reconcile_face, amort_remaining_face,
)
from services.valuation import calculate_valuation_metrics
from services.cashflow import build_cashflow_from_moex
from services.zspread import project_cfs, solve_flat_y
from services.exceptions import NotFoundException, CalculationException

logger = logging.getLogger(__name__)


async def _aempty():
    return {}


async def build_bond_details(isin: str, cache: dict) -> dict:
    """Полная карточка: reference/market/valuation/cashflow/floater/warnings."""
    data = cache.get(isin)
    external = data is None

    # все независимые сетевые вызовы — одним gather (MOEX ISS ~3.5с/запрос,
    # последовательно карточка грузилась 10-17с)
    res = await asyncio.gather(
        MarketDataService.fetch_last_prices([isin]),                                  # 0
        MarketDataService.fetch_moex_snapshot([isin]),                                # 1
        MarketDataService.fetch_coupon_schedules([isin]),                             # 2
        MarketDataService.get_curves(),                                               # 3
        MarketDataService.fetch_bond_schedule_full(isin),                             # 4
        MarketDataService.fetch_moex_securities([isin]) if external else _aempty(),   # 5
        MarketDataService.fetch_moex_shortnames() if external else _aempty(),         # 6
        MarketDataService.get_zspread_ctx(),                                          # 7
        return_exceptions=True,
    )
    _ok = lambda x, d: d if isinstance(x, Exception) else x
    market_prices = _ok(res[0], {})
    snapshot = _ok(res[1], {})
    schedules = _ok(res[2], {})
    ruonia_curve, keyrate_curve, calc_date, rates_date = _ok(res[3], (None, None, None, None))
    sched_full = _ok(res[4], {"coupons": [], "amorts": []})
    mo_map = _ok(res[5], {})
    shortnames = _ok(res[6], {})
    exp_ks, exp_ru, g_curve = _ok(res[7], (None, None, None))

    if data:
        ref_obj = create_bond_ref_data(data, isin)
        ref_dict = extract_bond_reference_dict(isin, data, ref_obj)
    else:
        # любая бумага вне кэша — справочник MOEX + база/спред из Cbonds-справки
        mo = mo_map.get(isin, {})
        if not mo:
            raise NotFoundException(f"Bond {isin} not found on MOEX", {"isin": isin})
        ref_obj = build_ref_external(isin, mo)
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
    # остаток из графика амортизаций авторитетнее кэша: стейл-кэш у
    # амортизируемых бумаг завышал dirty/SM/DM (БалтЛизП10: 1000 vs 900)
    _rem = amort_remaining_face((sched_full or {}).get("amorts"), _cd_face)
    if _rem is not None and abs(_rem - ref_obj.face_value) > 0.5:
        ref_obj.face_value = _rem
        ref_dict["face_value"] = _rem

    # НКД на calc_date из MOEX (приоритет над стейл-кэшем) — для dirty и карточки
    if accrued_live is not None:
        ref_obj.accrued_rub = accrued_live
        ref_dict["accrued_interest"] = accrued_live

    # ближайшая будущая оферта (bondization offers) — информационный флаг.
    # Оценку НЕ клэмпим: НРД dm тоже к погашению (сверка 2026-07-08 — клэмп
    # к оферте ухудшает совпадение на всех горизонтах), но цена бумаги
    # с близкой офертой может прайситься к ней → DM/z несопоставимы.
    # горизонт оферты — от settle (как pricing), не от today; состоявшиеся оферты
    # отфильтрованы (не будущее событие). Показываем и call, и put, но вид (kind)
    # различаем: только put — гарантированный горизонт держателя (см. offer_kind).
    from core.valuation import settle_date as _settle, offer_kind
    _off_ref = _settle(calc_date) if calc_date else date.today()
    next_offer = None       # (date, type_str, kind) ближайшей будущей оферты
    try:
        future_offers = []
        for o in sched_full.get("offers", []):
            typ = (o.get("type") or "").lower()
            if "состоя" in typ or "исполн" in typ:
                continue
            if o.get("date") and date.fromisoformat(o["date"]) > _off_ref:
                future_offers.append((date.fromisoformat(o["date"]), o.get("type"),
                                      offer_kind(o.get("type"))))
        if future_offers:
            next_offer = min(future_offers, key=lambda x: x[0])
            ref_dict["offer_date"] = next_offer[0]
            ref_dict["offer_type"] = next_offer[1]
            ref_dict["offer_kind"] = next_offer[2]
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
    cf_warnings: list = []
    try:
        cfs, _ = build_cashflow_from_moex(
            ref_obj, curve, calc_date,
            sched_full.get("coupons", []), sched_full.get("amorts", []), formula,
            offers=sched_full.get("offers"), warnings_out=cf_warnings,
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
    # (≈ до рефиксинга), текущий купон/база. Считаем на нашей кривой ожиданий.
    floater_block = None
    if ref_obj.base in ("RUONIA", "KEYRATE"):
        try:
            exp = exp_ru if ref_obj.base == "RUONIA" else exp_ks
            coupons = sched_full.get("coupons", [])
            px = last_price or prev_close_pct
            spread_dur = None
            mod_dur = convexity = pvbp = None
            if exp and px:
                # те же потоки и цена, что в z-модели: face_for_pricing (T+1
                # ex-амортизация) и offers (обрезка по оферте при пересмотре купона)
                from core.valuation import face_for_pricing
                dirty = (face_for_pricing(ref_obj.face_value, sched_full.get("amorts"), calc_date)
                         * px / 100.0 + (accrued_live or ref_obj.accrued_rub or 0.0))
                zcfs = project_cfs(ref_obj, exp, calc_date, coupons,
                                   sched_full.get("amorts"), sched_full.get("offers"))
                y = solve_flat_y(zcfs, calc_date, dirty)
                if y is not None:
                    spread_dur = metrics.macaulay_years(zcfs, calc_date, y)
                    # mod dur / convexity / PVBP — наш расчёт (НРД-доступ отключён)
                    mod_dur, convexity, pvbp = metrics.duration_metrics(zcfs, calc_date, y, dirty)
            # ставка начавшегося периода из модельного cashflow — фолбэк текущего
            # купона для RUONIA-average (MOEX не даёт valueprc/value до конца периода)
            cur_cpn_model = None
            for it in cfs:
                if (it.get("type") == "COUPON" and it.get("period_start")
                        and it["period_start"] <= calc_date < it["period_end"]):
                    cur_cpn_model = it.get("coupon_rate_pct")
                    break
            cb = metrics.carry_refix_block(coupons, sched_full.get("amorts"),
                                           ref_obj.face_value, px, exp,
                                           None, calc_date,
                                           current_coupon_override=cur_cpn_model)
            refix = cb["days_to_refix"]
            floater_block = {
                "spread_duration_yrs": round(spread_dur, 3) if spread_dur is not None else None,
                "rate_duration_yrs": round(refix / 365.0, 3) if refix is not None else None,
                "days_to_refix": refix, "current_coupon_pct": cb["current_coupon_pct"],
                "base_rate_pct": cb["base_rate_pct"],
                "mod_duration": mod_dur, "convexity": convexity, "pvbp": pvbp,
            }
        except Exception as e:
            logger.warning(f"Floater risk error for {isin}: {e}")

    warnings = list(cf_warnings)   # деградация cashflow-таблицы (фолбэк на факты)
    if next_offer and next_offer[2] == "put":
        warnings.append(
            f"Пут-оферта {next_offer[0].isoformat()}: первостепенны метрики к оферте "
            "(sm/dm/yield_to_offer, yield-to-put); к погашению — вторичные "
            "(sm_bps/disc_margin_bps)")
    elif next_offer and next_offer[2] == "call":
        warnings.append(
            f"Call-оферта {next_offer[0].isoformat()} (опцион эмитента): держатель "
            "её не форсирует — первостепенны метрики к ПОГАШЕНИЮ, к оферте лишь справочно")

    return {
        "reference": ref_dict,
        "market": market_data,
        "valuation": val_dict,
        "cashflow": cfs,
        "floater": floater_block,
        "sources": {"details": "MOEX", "market": "Alor"},
        "warnings": warnings,
    }


async def load_reprice_ctx(isin: str, cache: dict) -> dict:
    """Тёплый контекст для пересчёта под произвольную цену: ref_obj, кривая,
    calc_date, НКД, amorts/offers/periods/coupons и z-spread ctx. Один сетевой
    gather на isin — далее reprice_at_price(ctx, price) работает БЕЗ I/O, что
    позволяет батчить десятки уровней стакана по одной бумаге.

    Тот же богатый путь, что build_bond_details (periods/amorts/offers/НКД), но
    без cashflow-сборки. Переиспользует тёплые кэши (кривые в памяти, расписание
    на диске с day-TTL, снапшот)."""
    data = cache.get(isin)
    external = data is None
    res = await asyncio.gather(
        MarketDataService.fetch_coupon_schedules([isin]),                             # 0
        MarketDataService.get_curves(),                                               # 1
        MarketDataService.fetch_bond_schedule_full(isin),                             # 2
        MarketDataService.fetch_moex_snapshot([isin]),                                # 3
        MarketDataService.fetch_moex_securities([isin]) if external else _aempty(),   # 4
        MarketDataService.get_zspread_ctx(),                                          # 5
        return_exceptions=True,
    )
    _ok = lambda x, d: d if isinstance(x, Exception) else x
    schedules = _ok(res[0], {})
    ruonia_curve, keyrate_curve, calc_date, rates_date = _ok(res[1], (None, None, None, None))
    sched_full = _ok(res[2], {"coupons": [], "amorts": []})
    snapshot = _ok(res[3], {})
    mo_map = _ok(res[4], {})
    exp_ks, exp_ru, g_curve = _ok(res[5], (None, None, None))

    if data:
        ref_obj = create_bond_ref_data(data, isin)
    else:
        mo = mo_map.get(isin, {})
        if not mo:
            raise NotFoundException(f"Bond {isin} not found on MOEX", {"isin": isin})
        ref_obj = build_ref_external(isin, mo)

    if not calc_date:
        calc_date = rates_date or date.today()

    # номинал и НКД — как в карточке (иначе dirty/ставка расходятся)
    reconcile_face(ref_obj, (sched_full or {}).get("coupons"), calc_date)
    _rem = amort_remaining_face((sched_full or {}).get("amorts"), calc_date)
    if _rem is not None and abs(_rem - ref_obj.face_value) > 0.5:
        ref_obj.face_value = _rem
    accrued_live = snapshot.get(isin, {}).get("accrued")
    if accrued_live is not None:
        ref_obj.accrued_rub = accrued_live

    curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    if curve is None:
        raise CalculationException("Curve unavailable to reprice", {"isin": isin})

    return {
        "isin": isin,
        "ref_obj": ref_obj,
        "curve": curve,
        "calc_date": calc_date,
        "accrued_live": accrued_live,
        "periods": schedules.get(isin),
        "coupons": sched_full.get("coupons", []),
        "amorts": sched_full.get("amorts"),
        "offers": sched_full.get("offers"),
        "exp": exp_ru if ref_obj.base == "RUONIA" else exp_ks,
        "g_curve": g_curve,
    }


def reprice_at_price(ctx: dict, price: float) -> dict:
    """Чистая цена → цена-зависимые метрики оценки (SM/DM/YTM/dirty/Y-IDX) на
    тёплом ctx (load_reprice_ctx) — БЕЗ сетевых вызовов и БЕЗ z/carry. Батчится
    по уровням стакана (тот же расчёт, что калькулятор карточки, с полными
    amorts/offers/periods/НКД → результаты совпадают с карточкой поштучно)."""
    return calculate_valuation_metrics(
        ctx["ref_obj"], price, ctx["curve"], ctx["calc_date"],
        accrued_override=ctx["accrued_live"], periods=ctx["periods"],
        amorts=ctx["amorts"], offers=ctx["offers"],
    )


def _reprice_z(ctx: dict, price: float) -> dict:
    """z_model (тоже цена-зависим) поверх reprice_at_price. Отдельно от core:
    нужен live-рефрешу строки таблицы по WS-тику, но НЕ уровням стакана."""
    ref_obj = ctx["ref_obj"]
    calc_date = ctx["calc_date"]
    amorts, offers = ctx["amorts"], ctx["offers"]
    accrued_live, exp, g_curve = ctx["accrued_live"], ctx["exp"], ctx["g_curve"]
    coupons = ctx["coupons"]
    isin = ctx["isin"]

    z_model = None
    if exp and g_curve and ref_obj.base in ("RUONIA", "KEYRATE"):
        try:
            from services.zspread import compute_z_bps
            cpn_dicts = [{"start": c.get("start"), "end": c.get("end"), "value": c.get("value")}
                         for c in coupons]
            z_model = compute_z_bps(
                ref_obj, exp, g_curve, calc_date, price,
                accrued_live if accrued_live is not None else ref_obj.accrued_rub,
                cpn_dicts, amorts, offers)
        except Exception as e:
            logger.warning(f"reprice z_model error {isin}: {e}")
    return {"z_model_bps": z_model}


async def reprice_bond(isin: str, price: float, cache: dict) -> dict:
    """Пересчёт всех метрик оценки под ПРОИЗВОЛЬНУЮ чистую цену (калькулятор в
    карточке И live-рефреш строки таблицы по WS-тику). Тёплые кэши → мгновенно."""
    ctx = await load_reprice_ctx(isin, cache)
    out = dict(reprice_at_price(ctx, price))
    out.update(_reprice_z(ctx, price))
    return out
