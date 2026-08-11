from datetime import date
from functools import partial
from typing import Dict, Any, Optional

from core.forwards import DiscountCurve
from core.valuation import (
    BondRefData,
    dirty_price_rub,
    build_cashflows_with_spread,
    xirr_yield_pct,
    solve_simple_margin_bps,
    solve_discount_margin_bps,
    current_index_pct,
    FlatForwardCurve,
    ruonia_rolling_yield_pct,
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
    ruonia_curve: DiscountCurve = None,
    alt_prices=None,
    accrued_basis: str = "settle",
) -> Dict[str, Any]:
    """
    Computes all valuation metrics for a given bond and price.
    accrued_override — НКД из MOEX (приоритет над стейл-кэшем).
    accrued_basis — НА КАКУЮ ДАТУ дан НКД (свой и в accrued_override, и в
              bond.accrued_rub): "settle" — уже на дату поставки T+1 (блок
              securities: ACCRUEDINT live-снапшота и isins_cache — так отдаёт
              MOEX, доначислять НЕЛЬЗЯ), "calc" — на дату расчёта (блок history:
              ACCINT прошлого дня в as-of движке, и НКД, посчитанный из графика
              купонов на calc_date) — такой доначисляем до поставки сами.
    ruonia_curve — OIS-кривая RUONIA на ту же дату: база сравнения Y-IDX для ВСЕХ
              флоатеров, включая КС-бумаги (см. ruonia_rolling_yield_pct). Для
              RUONIA-бумаги совпадает с curve и может не передаваться; для
              КС-бумаги без неё Y-IDX не считается (warning, не крэш).
    periods — реальное расписание купонов [(start,end,value),...] из MOEX;
              value (зафикс. рублёвая сумма купона) прокидывается в DM-cashflow,
              чтобы текущий/прошлый купон брался фактом, а не перепрогнозом.
    amorts — график амортизаций MOEX [{date, value},...] для DM амортизируемых бумаг.
    alt_prices — доп. чистые цены (напр. bid/ask верха стакана): для каждой считаем
              ТОЛЬКО Y-IDX (XIRR по УЖЕ построенному потоку − та же база RUONIA) →
              "y_idx_by_price": {price: bps}. Дёшево: поток и base leg не пересобираются.
    Returns a dictionary suitable for formatting by Pydantic.
    """
    # Бумага гасится не позже даты расчётов T+1: покупателю не достаётся ни одного
    # платежа (весь поток ex) — метрики бессмысленны, а стейл prev-цена давала
    # мусорные отрицательные SM (Магнит4P06 за 2 дня до погашения: SM −330).
    from core.valuation import settle_date as _sd
    if bond.maturity_date is not None and bond.maturity_date <= _sd(calc_date):
        return {
            "clean_price_pct": price, "dirty_price_rub": None,
            "dm_bps": None, "sm_bps": None, "disc_margin_bps": None, "dm_label": None,
            "yield_xirr_pct": None, "index_yield_pct": None, "yield_over_index_bps": None,
            "pricing_status": "MATURED", "warnings": ["Погашение ≤ T+1 — потоки покупателю не достаются"],
        }

    # База не распознана (кэш без FORMULA и реестр молчит) — построить поток
    # нечем. Раньше это долетало до build_cashflows_to_maturity и вылезало
    # ValueError'ом «Unknown base rate type» → 500 на /reprice (флуд в логах от
    # live-рефреша строки таблицы по WS-тику). Деградируем как MATURED/NO_MATURITY.
    if bond.base not in ("RUONIA", "KEYRATE"):
        return {
            "clean_price_pct": price, "dirty_price_rub": None,
            "dm_bps": None, "sm_bps": None, "disc_margin_bps": None, "dm_label": None,
            "yield_xirr_pct": None, "index_yield_pct": None, "yield_over_index_bps": None,
            "pricing_status": "UNKNOWN_BASE",
            "warnings": [f"База ставки не определена ({bond.base}) — метрики флоатера не считаются"],
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

    # I/O-граница: история индекса — один фетч на запрос, дальше только инжекция
    warnings: list = []

    # НКД НА ДАТУ ПОСТАВКИ. Покупатель платит на settle (T+1 раб) — туда же
    # якорятся XIRR/SM/DM (xirr_yield_pct, pv_cashflows_with_dm,
    # solve_discount_margin_bps), поэтому и НКД в dirty обязан быть на settle.
    #
    # ОТКУДА НКД — РАЗНЫЕ КОНВЕНЦИИ (сверено с MOEX 2026-08-04):
    #   • блок securities (ACCRUEDINT, live-снапшот и isins_cache) отдаёт НКД
    #     УЖЕ НА ДАТУ ПОСТАВКИ — доначислять нечего (basis="settle");
    #   • блок history (ACCINT на прошлую дату, as-of движок) отдаёт НКД НА ДАТУ
    #     ТОРГОВ — его доначисляем сами (basis="calc").
    # Пока basis не различался, live-ветка накидывала лишний день (три на
    # пятницу, больше на праздники): dirty завышен, YTM/Y-IDX/SM/DM занижены.
    # accrue_to_settle заодно закрывает ex-coupon (купон в окне (calc, settle]
    # уходит продавцу — НКД считается от начала нового периода); у settle-НКД
    # биржа это уже сделала сама.
    from core.valuation import (settle_date as _sd_ex, accrue_to_settle as _ats,
                                accrued_at as _acc_at)
    settle_dt = _sd_ex(calc_date)
    if accrued is not None and accrued_basis == "calc":
        accrued_calc_date = accrued
        accrued, _acc_note = _ats(accrued, calc_date, periods)
        if _acc_note:
            warnings.append(_acc_note)
    else:
        # НКД на calc_date — справочное поле карточки; из биржевого settle-НКД
        # его не вычесть, считаем из графика (None, если купон не опубликован)
        accrued_calc_date = _acc_at(periods, calc_date) if periods else None

    # T+1: амортизация в окне (calc, settle] — продавцу; цена котируется от остатка
    from core.valuation import face_for_pricing
    _pricing_face = face_for_pricing(bond.face_value, amorts, calc_date)
    dirty_rub = dirty_price_rub(_pricing_face, price, accrued)

    # маржа выпуска = 0/None у флоатера почти всегда = пробел данных (формула не
    # распарсилась / нет в Cbonds): SM/DM тогда занижены на всю маржу, молча.
    # Помечаем (не зануляем — иногда 0 реален для дисконт-маржа бумаг).
    if not bond.spread_issue_bps:
        warnings.append("маржа выпуска не определена (0) — SM/DM занижены на величину "
                        "маржи; формула купона не распарсилась или бумаги нет в справочнике")

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

    # ГАРАНТИРОВАННЫЙ НОМИНАЛЬНЫЙ УБЫТОК: dirty > Σ всех будущих потоков (даже без
    # дисконта). Держать до погашения = точно потерять деньги → цена явно битая
    # (стейл/тонкий принт неликвида), SM/DM/z экономически бессмысленны и лезут в
    # топ сортировки мусором (напр. DM −2221). Помечаем → чистим спред-метрики.
    _future_sum = sum(cf.amount_rub for cf in cfs if cf.pay_date > calc_date)
    price_implausible = bool(dirty_rub is not None and _future_sum > 0
                             and dirty_rub > _future_sum * 1.0005)  # 5bps допуск на округление

    try:
        impl_yield = xirr_yield_pct(dirty_rub, cfs, calc_date)
    except Exception as e:
        logger.warning(f"XIRR error for {bond.isin}: {e}")
        impl_yield = None

    # ДОХОДНОСТЬ БУМАГИ vs ДОХОДНОСТЬ БАЗЫ. Base leg — эффективная годовая
    # доходность роллирования RUONIA по OIS-кривой, ОДНА И ТА ЖЕ для КС- и
    # RUONIA-бумаг (решение 2026-08-04): альтернатива держателю — размещать
    # деньги o/n, а это RUONIA. Спред = IRR_бумаги − доходность_RUONIA.
    _ru_curve = ruonia_curve if ruonia_curve is not None else (
        curve if bond.base == "RUONIA" else None)
    index_yield = None
    if _ru_curve is None:
        warnings.append("RUONIA-кривая не передана — R-spread не посчитан "
                        "(база сравнения для всех флоатеров — роллирование RUONIA)")
    else:
        try:
            index_yield = ruonia_rolling_yield_pct(_ru_curve, calc_date, bond.maturity_date)
        except Exception as e:
            logger.warning(f"RUONIA rolling-yield error for {bond.isin}: {e}")

    yield_over_index_bps = None
    if impl_yield is not None and index_yield is not None:
        yield_over_index_bps = round((impl_yield - index_yield) * 100.0)

    # Y-IDX на альтернативных ценах (bid/ask): поток cfs и base leg от цены не
    # зависят — меняется только dirty на входе XIRR. Реюз, а не пересчёт модели.
    y_idx_by_price: Dict[float, Any] = {}
    for _p in (alt_prices or []):
        if _p is None or index_yield is None:
            continue
        try:
            _dirty = dirty_price_rub(_pricing_face, _p, accrued)
            if _dirty is None or _dirty <= 0 or _dirty > _future_sum * 1.0005:
                y_idx_by_price[_p] = None      # та же отсечка «гарант. убыток», что для mid
                continue
            _y = xirr_yield_pct(_dirty, cfs, calc_date)
            y_idx_by_price[_p] = (round((_y - index_yield) * 100.0)
                                  if _y is not None else None)
        except Exception as e:
            logger.warning(f"alt-price Y-IDX error {bond.isin} @{_p}: {e}")
            y_idx_by_price[_p] = None


    # SIMPLE MARGIN (наш sm_bps): дисконт по форвард-кривей+спред. Воспроизводит
    # НРД simple_margin (сверка: ликвид near-par med 0-2bps). Поле dm_bps сохранено
    # для обратной совместимости = то же значение (это простая маржа, не discount).
    sm_bps = None
    try:
        if curve and len(cfs) > 0:
            sm_bps = solve_simple_margin_bps(bond, curve, cfs, calc_date, dirty_rub)
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
    from core.valuation import first_offer_date as _fod, _offer_price_pct as _opp, settle_date as _sd2
    _settle = _sd2(calc_date)
    _put = _fod(offers, _settle) if offers else None

    # ПРОПУЩЕННАЯ ОФЕРТА: купон после оферты у reset-бумаг не определён (эмитент
    # переставит), но MOEX часто отдаёт пустой OFFERDATE → оферта не распознана,
    # поток проецируется старым спредом к погашению, а рынок торгует к оферте →
    # DM/z несопоставимы. Помечаем только когда ОФЕРТНЫХ ДАННЫХ НЕТ ВОВСЕ (not
    # offers): если offers непусты, но все в прошлом — оферта состоялась, не «пропущена».
    if not offers:
        try:
            from services.ref_data import cut_at_offer as _coa
            if _coa(bond.isin):
                warnings.append("var_type=пересмотр купона, но оферта не распознана "
                                "(пустой OFFERDATE у MOEX) — поток к погашению старым спредом; "
                                "DM/z могут быть несопоставимы с рынком (торгует к оферте)")
        except Exception:
            pass

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
                sm_to_offer = solve_simple_margin_bps(bond, curve, cfs_off, calc_date, dirty_rub)
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
    y_idx_by_price = {p: _sane_bps(v, warnings, "yield_over_index_alt")
                      for p, v in y_idx_by_price.items()}
    sm_to_offer = _sane_bps(sm_to_offer, warnings, "sm_to_offer")
    dm_to_offer = _sane_bps(dm_to_offer, warnings, "disc_margin_to_offer")
    impl_yield = _sane_pct(impl_yield, warnings, "yield")
    y_to_offer = _sane_pct(y_to_offer, warnings, "yield_to_offer")
    if dirty_rub is not None and dirty_rub <= 0:
        warnings.append("sanity: dirty_price ≤ 0")

    # гарант. номинальный убыток → цена битая, спред-метрики бессмысленны: чистим
    # (иначе −2221 DM лезет в топ). yield/dirty оставляем (факт от цены).
    if price_implausible:
        warnings.append(f"цена {price}% подразумевает номинальный убыток "
                        f"(dirty {dirty_rub:.0f}₽ > Σ будущих потоков {_future_sum:.0f}₽) — "
                        "вероятно стейл/тонкая цена неликвида; SM/DM/спред скрыты")
        sm_bps = disc_margin_bps = yield_over_index_bps = None
        sm_to_offer = dm_to_offer = None

    status = "SUCCESS" if sm_bps is not None else ("PRICE_IMPLAUSIBLE" if price_implausible else "DM_FAILED")
    if any(w.startswith("sanity:") for w in warnings):
        status = "SANITY_FLAG"

    return {
        "clean_price_pct": price,
        "dirty_price_rub": dirty_rub,
        # дата поставки и НКД на неё — то, из чего собран dirty (калькулятор их показывает)
        "settlement_date": settle_dt,
        "accrued_settle_rub": round(accrued, 4) if accrued is not None else None,
        "accrued_calc_rub": round(accrued_calc_date, 4) if accrued_calc_date is not None else None,
        "pricing_face_rub": _pricing_face,
        "dm_bps": sm_bps,                      # backward-compat (= simple margin)
        "sm_bps": sm_bps,                      # simple margin (наш) ≈ НРД simple_margin
        "disc_margin_bps": disc_margin_bps,    # discount margin (наш) ≈ НРД discount_margin
        "dm_label": "simple_margin" if sm_bps is not None else None,
        "yield_xirr_pct": round(impl_yield, 4) if impl_yield is not None else None,
        "index_yield_pct": round(index_yield, 4) if index_yield is not None else None,
        "yield_over_index_bps": yield_over_index_bps,
        "y_idx_by_price": y_idx_by_price,          # {alt-цена: Y-IDX bps} (bid/ask)
        "price_implausible": price_implausible,   # гарант. убыток → z тоже занулить
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
