from datetime import date
from functools import partial
from typing import Dict, Any, Optional

from forwards import DiscountCurve
from valuation import (
    BondRefData,
    dirty_price_rub,
    build_cashflows_with_spread,
    xirr_yield_pct,
    solve_dm_bps,
    solve_discount_margin_bps,
    current_index_pct,
    FlatForwardCurve,
    index_rolling_yield_pct,
)

import logging

logger = logging.getLogger(__name__)


def _index_provider(base: str, warnings: list, calc_date: date = None):
    """I/O-граница: история индекса ЦБ фетчится ЗДЕСЬ (раз на запрос), ядро
    получает готовый провайдер. Сбой фетча → warning + провайдер-заглушка
    (ядро уходит на форвард-проекцию, но это видно в ответе), history-пары для
    current_index_pct → None (DM посчитается от back-out из купона или не
    посчитается — тоже видимо по disc_margin_bps=None).

    calc_date — для проверки СВЕЖЕСТИ истории: если последняя дата отстаёт от
    calc_date больше допуска, фиксинги начавшихся периодов частично уходят на
    форвард (см. projected_ks_pct._realized) — помечаем warning'ом, иначе
    стейл-ставка тихо утекала бы в купон (аудит F1)."""
    try:
        from services.coupon_calib import period_index_pct, index_history, _HIST_STALE_GRACE_DAYS
        idx = index_history(base)
        if not idx[0]:
            raise RuntimeError("пустая история индекса")
        if calc_date is not None:
            last = idx[0][-1]
            lag_days = (calc_date - last).days
            if lag_days > _HIST_STALE_GRACE_DAYS:
                warnings.append(
                    f"история {base} отстаёт на {lag_days} дн (последняя {last.isoformat()}) "
                    "— фиксинги начавшихся периодов за пределом покрытия спроецированы форвардом")
        return partial(period_index_pct, idx=idx), list(zip(idx[0], idx[1]))
    except Exception as e:
        warnings.append(f"история {base} недоступна ({type(e).__name__}) — "
                        "фиксинги начавшихся периодов спроецированы форвардом")
        return (lambda *a, **k: None), None

def calculate_valuation_metrics(
    bond: BondRefData,
    price: float,
    curve: DiscountCurve,
    calc_date: date,
    accrued_override: float = None,
    periods=None,
    amorts=None,
    offers=None,
) -> Dict[str, Any]:
    """
    Computes all valuation metrics for a given bond and price.
    accrued_override — НКД на calc_date из MOEX (приоритет над стейл-кэшем).
    periods — реальное расписание купонов [(start,end,value),...] из MOEX;
              value (зафикс. рублёвая сумма купона) прокидывается в DM-cashflow,
              чтобы текущий/прошлый купон брался фактом, а не перепрогнозом.
    amorts — график амортизаций MOEX [{date, value},...] для DM амортизируемых бумаг.
    Returns a dictionary suitable for formatting by Pydantic.
    """
    # Бумага гасится не позже даты расчётов T+1: покупателю не достаётся ни одного
    # платежа (весь поток ex) — метрики бессмысленны, а стейл prev-цена давала
    # мусорные отрицательные SM (Магнит4P06 за 2 дня до погашения: SM −330).
    from valuation import settle_date as _sd
    if bond.maturity_date is not None and bond.maturity_date <= _sd(calc_date):
        return {
            "clean_price_pct": price, "dirty_price_rub": None,
            "dm_bps": None, "sm_bps": None, "disc_margin_bps": None, "dm_label": None,
            "yield_xirr_pct": None, "index_yield_pct": None, "yield_over_index_bps": None,
            "pricing_status": "MATURED", "warnings": ["Погашение ≤ T+1 — потоки покупателю не достаются"],
        }

    # Перпы/суборды без даты погашения: поток не терминируется — флоатер-метрики
    # (SM/DM к погашению) не определены, выходим без крэша.
    if bond.maturity_date is None:
        return {
            "clean_price_pct": price, "dirty_price_rub": None,
            "dm_bps": None, "sm_bps": None, "disc_margin_bps": None, "dm_label": None,
            "yield_xirr_pct": None, "index_yield_pct": None, "yield_over_index_bps": None,
            "pricing_status": "NO_MATURITY", "warnings": ["Нет даты погашения (перп/суборд)"],
        }

    accrued = accrued_override if accrued_override is not None else bond.accrued_rub
    # T+1: амортизация в окне (calc, settle] — продавцу; цена котируется от остатка
    from valuation import face_for_pricing
    _pricing_face = face_for_pricing(bond.face_value, amorts, calc_date)
    dirty_rub = dirty_price_rub(_pricing_face, price, accrued)

    # I/O-граница: история индекса — один фетч на запрос, дальше только инжекция
    warnings: list = []
    index_pct_fn, hist_pairs = _index_provider(bond.base, warnings, calc_date)

    # кэп/флор купона: если число распарсилось — прогноз клэмпится в
    # build_cashflows (потолок/пол ставки учтён). Если capped, но числа нет —
    # проекция линейна, помечаем (при высокой базе DM/SM/YTM могут завышать).
    try:
        from services.ref_data import coupon_formula as _cf
        _cfs = _cf(bond.isin)
        if _cfs.get("capped"):
            _cap, _flr = _cfs.get("cap_pct"), _cfs.get("floor_pct")
            if _cap is not None or _flr is not None:
                parts = []
                if _cap is not None:
                    parts.append(f"кэп {_cap}%")
                if _flr is not None:
                    parts.append(f"флор {_flr}%")
                warnings.append(f"купон с ограничением ставки ({', '.join(parts)}) — учтён в проекции")
            else:
                warnings.append("купон с кэпом/флором (число не распарсилось) — проекция линейна, "
                                "ограничение ставки НЕ учтено: DM/SM/YTM могут завышать")
    except Exception:
        pass

    # DM считается по cfs с реальным спредом: value зафикс. купонов сохраняем
    # (факт MOEX), амортизации учитываем.
    cfs = build_cashflows_with_spread(bond, curve, calc_date, bond.spread_issue_bps,
                                      explicit_periods=periods, amorts=amorts, offers=offers,
                                      index_pct_fn=index_pct_fn, warnings_out=warnings)

    try:
        impl_yield = xirr_yield_pct(dirty_rub, cfs, calc_date)
    except Exception as e:
        logger.warning(f"XIRR error for {bond.isin}: {e}")
        impl_yield = None

    # ДОХОДНОСТЬ БУМАГИ vs ДОХОДНОСТЬ ИНДЕКСА (заменяет прежний spread_to_base_bps
    # = разность двух XIRR, которая как нелинейная разность систематически врала
    # off-par). Теперь base leg — эффективная годовая доходность роллирования
    # самого индекса по ожидаемым форвардным ставкам (RUONIA daily-comp / КС
    # quarterly-comp), а спред = IRR_бумаги − доходность_индекса.
    try:
        index_yield = index_rolling_yield_pct(bond.base, curve, calc_date, bond.maturity_date)
    except Exception as e:
        logger.warning(f"Index rolling-yield error for {bond.isin}: {e}")
        index_yield = None

    yield_over_index_bps = None
    if impl_yield is not None and index_yield is not None:
        yield_over_index_bps = round((impl_yield - index_yield) * 100.0)
        
    # SIMPLE MARGIN (наш sm_bps): дисконт по форвард-кривей+спред. Воспроизводит
    # НРД simple_margin (сверка: ликвид near-par med 0-2bps). Поле dm_bps сохранено
    # для обратной совместимости = то же значение (это простая маржа, не discount).
    sm_bps = None
    try:
        if curve and len(cfs) > 0:
            sm_bps = solve_dm_bps(bond, curve, cfs, calc_date, dirty_rub)
    except Exception as e:
        logger.warning(f"SM calculation error for {bond.isin}: {e}")

    # DISCOUNT MARGIN (наш disc_margin_bps): настоящий FRN DM — индекс плоский на
    # ТЕКУЩЕМ уровне (из зафикс. купона), money-market дисконт (L+DM). Воспроизводит
    # НРД discount_margin (med −20, m|Δ|≈47bps; остаток — их проприетарная машина).
    disc_margin_bps = None
    try:
        L = current_index_pct(periods, calc_date, bond.spread_issue_bps, bond.face_value,
                              amorts=amorts, base=bond.base, hist=hist_pairs)
        if L is not None:
            flat = FlatForwardCurve(calc_date, L)
            flat_cfs = build_cashflows_with_spread(bond, flat, calc_date, bond.spread_issue_bps,
                                                   explicit_periods=periods, amorts=amorts,
                                                   offers=offers,
                                                   index_pct_fn=index_pct_fn, warnings_out=warnings)
            disc_margin_bps = solve_discount_margin_bps(flat_cfs, calc_date, dirty_rub, L)
    except Exception as e:
        logger.warning(f"Discount margin error for {bond.isin}: {e}")

    # К ОФЕРТЕ (yield-to-put): для бумаг с будущей офертой это первостепенная
    # цифра — рынок торгует к ближайшей оферте. Режем поток к оферте безусловно
    # (выкуп остатка по цене оферты). Основные поля выше — к погашению (сверка НРД).
    # preferred_horizon — только ПОДСКАЗКА UI, что показать первым; на то, как
    # посчитаны sm_bps/disc_margin_bps/yield_xirr_pct, он не влияет.
    horizon = "maturity"
    offer_date = offer_price_pct = None
    sm_to_offer = dm_to_offer = y_to_offer = None
    from valuation import first_offer_date as _fod, _offer_price_pct as _opp, settle_date as _sd2
    _settle = _sd2(calc_date)
    _put = _fod(offers, _settle) if offers else None
    if _put is not None and (bond.maturity_date is None or _put < bond.maturity_date):
        horizon = "offer"
        offer_date = _put
        offer_price_pct = _opp(offers, _put)
        try:
            cfs_off = build_cashflows_with_spread(bond, curve, calc_date, bond.spread_issue_bps,
                                                  explicit_periods=periods, amorts=amorts,
                                                  offers=offers, to_offer=True,
                                                  index_pct_fn=index_pct_fn, warnings_out=warnings)
            y_off = xirr_yield_pct(dirty_rub, cfs_off, calc_date)
            y_to_offer = round(y_off, 4) if y_off is not None else None
            if curve and len(cfs_off) > 0:
                sm_to_offer = solve_dm_bps(bond, curve, cfs_off, calc_date, dirty_rub)
            L2 = current_index_pct(periods, calc_date, bond.spread_issue_bps, bond.face_value,
                                   amorts=amorts, base=bond.base, hist=hist_pairs)
            if L2 is not None:
                flat2 = FlatForwardCurve(calc_date, L2)
                flat_cfs_off = build_cashflows_with_spread(bond, flat2, calc_date, bond.spread_issue_bps,
                                                           explicit_periods=periods, amorts=amorts,
                                                           offers=offers, to_offer=True,
                                                           index_pct_fn=index_pct_fn, warnings_out=warnings)
                dm_to_offer = solve_discount_margin_bps(flat_cfs_off, calc_date, dirty_rub, L2)
        except Exception as e:
            logger.warning(f"to-offer valuation error for {bond.isin}: {e}")

    # SANITY-GUARD (C6): вывод вне разумных границ = плохой вход (кривая/параметры/
    # цена) → чистим метрику в None + помечаем, а не выдаём мусор в таблицу. Границы
    # широкие: ловят только явную дичь (SM −30000bps, ytm 900%), не режут дистресс.
    sm_bps = _sane_bps(sm_bps, warnings, "sm")
    disc_margin_bps = _sane_bps(disc_margin_bps, warnings, "disc_margin")
    yield_over_index_bps = _sane_bps(yield_over_index_bps, warnings, "yield_over_index")
    sm_to_offer = _sane_bps(sm_to_offer, warnings, "sm_to_offer")
    dm_to_offer = _sane_bps(dm_to_offer, warnings, "disc_margin_to_offer")
    impl_yield = _sane_pct(impl_yield, warnings, "yield")
    y_to_offer = _sane_pct(y_to_offer, warnings, "yield_to_offer")
    if dirty_rub is not None and dirty_rub <= 0:
        warnings.append("sanity: dirty_price ≤ 0")

    status = "SUCCESS" if sm_bps is not None else "DM_FAILED"
    if any(w.startswith("sanity:") for w in warnings):
        status = "SANITY_FLAG"

    return {
        "clean_price_pct": price,
        "dirty_price_rub": dirty_rub,
        "dm_bps": sm_bps,                      # backward-compat (= simple margin)
        "sm_bps": sm_bps,                      # simple margin (наш) ≈ НРД simple_margin
        "disc_margin_bps": disc_margin_bps,    # discount margin (наш) ≈ НРД discount_margin
        "dm_label": "simple_margin" if sm_bps is not None else None,
        "yield_xirr_pct": round(impl_yield, 4) if impl_yield is not None else None,
        "index_yield_pct": round(index_yield, 4) if index_yield is not None else None,
        "yield_over_index_bps": yield_over_index_bps,
        "pricing_status": status,
        "warnings": sorted(set(warnings)),
        "preferred_horizon": horizon,
        "offer_date": offer_date,
        "offer_price_pct": offer_price_pct,
        "sm_to_offer_bps": sm_to_offer,
        "disc_margin_to_offer_bps": dm_to_offer,
        "yield_to_offer_pct": y_to_offer,
    }


# Разумные границы вывода — ловят data-driven регрессии (плохой параметр → дичь),
# не режут реальный дистресс. Спред флоатера ±10000bps, доходность 0..150%.
_SANE_BPS = (-5000, 15000)
_SANE_PCT = (-5.0, 150.0)


def _sane_bps(v, warnings: list, name: str):
    if v is None:
        return None
    if not (_SANE_BPS[0] <= v <= _SANE_BPS[1]):
        warnings.append(f"sanity: {name}={v}bps вне [{_SANE_BPS[0]},{_SANE_BPS[1]}]")
        return None
    return v


def _sane_pct(v, warnings: list, name: str):
    if v is None:
        return None
    if not (_SANE_PCT[0] <= v <= _SANE_PCT[1]):
        warnings.append(f"sanity: {name}={v}% вне [{_SANE_PCT[0]},{_SANE_PCT[1]}]")
        return None
    return v
