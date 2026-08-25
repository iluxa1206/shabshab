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

def bond_periods_or_none(periods):
    """Периоды как их ждёт services/accrued: [(start, end, value)] либо None."""
    return periods or None


def _face_for_accrued(bond, amorts, calc_date) -> float:
    """Номинал, от которого начисляется НКД: остаток на дату расчёта."""
    from core.valuation import face_for_pricing
    try:
        return face_for_pricing(bond.face_value, amorts, calc_date) or bond.face_value
    except Exception:
        return bond.face_value or 1000.0


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
    accrued_date=None,
) -> Dict[str, Any]:
    """
    Computes all valuation metrics for a given bond and price.
    accrued_override — НКД из MOEX (приоритет над стейл-кэшем).
    accrued_date — ДАТА БИРЖИ, на которую посчитан НКД (ISS SETTLEDATE блока
              securities). Задан — верим ей и приводим НКД к НАШЕЙ дате
              поставки; это точнее accrued_basis, который лишь угадывает
              конвенцию источника.
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

    # НУЛЕВОЙ НКД ОТ ИСТОЧНИКА — ПРОВЕРЯЕМ ПО ГРАФИКУ. ISS изредка отдаёт
    # ACCRUEDINT=0 посреди купонного периода; цена тогда считается «чистой»
    # без накопленного купона, доходность взлетает, и число уезжает на сотню
    # bps (РостелP21R 24.08: сигнал 233 bps против 120 верных — ровно разница
    # НКД 11,46 ₽). Ноль законен только в день выплаты, когда период начался
    # сегодня; в остальных случаях верим расписанию, а не снапшоту.
    if accrued is not None and abs(accrued) < 0.005:
        # Ноль от биржи посреди купонного периода — почти всегда сбой источника
        # (ISS отдаёт ACCRUEDINT=0), и цена тогда считается «чистой» без
        # накопленного купона: доходность улетает на сотню bps. Считаем сами
        # ОБЩЕЙ лестницей (services/accrued): купон опубликован → спека фиксинга
        # → прошлый купон → индекс+маржа. Та же лестница у as-of движка, чтобы
        # история и живой расчёт не расходились.
        from services.accrued import accrued_for
        _own, _how = accrued_for(bond_periods_or_none(periods), settle_dt,
                                 face=_face_for_accrued(bond, amorts, calc_date),
                                 base=bond.base, margin_bps=bond.spread_issue_bps,
                                 isin=bond.isin, calc_date=calc_date)
        if _own is None and curve is not None:
            # расписания нет вовсе — сетка купонных дат из параметров выпуска
            from core.valuation import accrued_from_grid as _acc_grid
            _own, _how = _acc_grid(bond, curve, settle_dt), "параметры выпуска"
        if _own and _own > 0.01:
            warnings.append(f"НКД источника 0 — посчитан сам ({_how}): "
                            f"{_own:.2f} ₽ на {settle_dt.isoformat()}")
            accrued, accrued_date = _own, settle_dt
        elif not periods:
            # ни расписания, ни параметров — считать цену «чистой» нельзя
            warnings.append("sanity: НКД источника 0, посчитать его нечем")
    # Биржа публикует НКД ВМЕСТЕ со своей датой расчётов (SETTLEDATE). Наша
    # settle считается сама (T+1 раб) и с биржевой расходится — в пятницу и
    # перед праздниками на 3 дня. Пока даты не сверялись, НКД мог быть на одну
    # дату, а срок до погашения на другую: РусГид2Р01 21.08.2026 — НКД на 21.08
    # (6,58) против поставки 24.08, и Y-IDX выходил 181 bps вместо 103. У
    # короткой бумаги день рассогласования стоит десятков bps.
    if (accrued is not None and accrued_date is not None
            and accrued_date != settle_dt):
        accrued, _acc_note = _ats(accrued, accrued_date, periods, to_date=settle_dt)
        if _acc_note:
            warnings.append(_acc_note)
        accrued_calc_date = _acc_at(periods, calc_date) if periods else None
    elif accrued is not None and accrued_basis == "calc":
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
        # База считается до ФАКТИЧЕСКОГО конца потока, а не до заявленного
        # погашения. Поток флоатера режется офертой, когда ставка после неё
        # неизвестна (ref_data.cut_at_offer: эмитент пересматривает купон), —
        # тогда числитель Y-IDX относится к оферте, и база обязана относиться
        # туда же. Раньше знаменатель брался до maturity_date: доходность к
        # оферте через год делилась на роллирование до погашения через восемь.
        # На проде так считались 19 бумаг из 161 с офертами, спред занижался
        # почти вдвое (Россети1Р8: 56 bps против верных 152, Славнеф2Р5 89/180).
        # У необрезанных бумаг конец потока и есть погашение — для них ничего
        # не меняется.
        _flow_end = max((cf.pay_date for cf in cfs), default=None) or bond.maturity_date
        try:
            index_yield = ruonia_rolling_yield_pct(_ru_curve, calc_date, _flow_end)
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

    # ГОРИЗОНТЫ ОЦЕНКИ. Полный набор метрик считается к каждому доступному
    # горизонту (погашение / пут-оферта / call-оферта), а preferred_horizon
    # выбирается ПРАВИЛОМ ЦЕНЫ (см. _preferred_horizon). Базовые поля ответа
    # (sm_bps/disc_margin_bps/yield_xirr_pct/yield_over_index_bps) остаются К
    # ПОГАШЕНИЮ — это сверочная база с НРД и обратная совместимость; UI и
    # universe берут цифры выбранного горизонта из блока "horizons".
    horizon = "maturity"
    offer_date = offer_price_pct = None
    sm_to_offer = dm_to_offer = y_to_offer = None
    from core.valuation import (first_offer_date as _fod, first_call_date as _fcd,
                                _offer_price_pct as _opp, settle_date as _sd2)
    _settle = _sd2(calc_date)
    _put = _fod(offers, _settle) if offers else None
    _call = _fcd(offers, _settle) if offers else None

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

    # SANITY-GUARD (C6): вывод вне разумных границ = плохой вход (кривая/параметры/
    # цена) → чистим метрику в None + помечаем, а не выдаём мусор в таблицу. Границы
    # широкие: ловят только явную дичь (SM −30000bps, ytm 900%), не режут дистресс.
    # Стоит ДО сборки горизонтов: в horizons["maturity"] должны лечь уже чистые цифры.
    sm_bps = _sane_bps(sm_bps, warnings, "sm")
    disc_margin_bps = _sane_bps(disc_margin_bps, warnings, "disc_margin")
    yield_over_index_bps = _sane_bps(yield_over_index_bps, warnings, "yield_over_index")
    y_idx_by_price = {p: _sane_bps(v, warnings, "yield_over_index_alt")
                      for p, v in y_idx_by_price.items()}
    impl_yield = _sane_pct(impl_yield, warnings, "yield")
    if dirty_rub is not None and dirty_rub <= 0:
        warnings.append("sanity: dirty_price ≤ 0")

    def _metrics_at(cut: date) -> Optional[dict]:
        """Полный набор метрик к произвольному горизонту cut (дата оферты):
        поток режется к cut с выкупом остатка по цене оферты, база Y-IDX
        (роллирование RUONIA) — тоже до cut, иначе спред сравнивал бы бумагу с
        депозитом другого срока."""
        cfs_h = build_cashflows_with_spread(bond, curve, calc_date, bond.spread_issue_bps,
                                            explicit_periods=periods, amorts=amorts,
                                            offers=offers, cut_date=cut,
                                            index_pct_fn=index_pct_fn, warnings_out=warnings)
        if not cfs_h:
            return None
        y_h = xirr_yield_pct(dirty_rub, cfs_h, calc_date)
        sm_h = (solve_simple_margin_bps(bond, curve, cfs_h, calc_date, dirty_rub)
                if curve else None)
        dm_h = None
        L_h = current_index_pct(periods, calc_date, bond.spread_issue_bps, bond.face_value,
                               amorts=amorts, base=bond.base, hist=hist_pairs)
        if L_h is not None:
            flat_h = FlatForwardCurve(calc_date, L_h)
            flat_cfs_h = build_cashflows_with_spread(bond, flat_h, calc_date, bond.spread_issue_bps,
                                                     explicit_periods=periods, amorts=amorts,
                                                     offers=offers, cut_date=cut,
                                                     index_pct_fn=index_pct_fn, warnings_out=warnings)
            dm_h = solve_discount_margin_bps(flat_cfs_h, calc_date, dirty_rub, L_h)
        idx_y_h = None
        if _ru_curve is not None:
            try:
                idx_y_h = ruonia_rolling_yield_pct(_ru_curve, calc_date, cut)
            except Exception as e:
                logger.warning(f"RUONIA rolling-yield to {cut} error for {bond.isin}: {e}")
        # Y-IDX горизонта на альтернативных ценах (bid/ask/проба наклона): поток и
        # base leg от цены не зависят — меняется только dirty на входе XIRR.
        y_idx_alt_h = {}
        _fut_h = sum(cf.amount_rub for cf in cfs_h if cf.pay_date > calc_date)
        for _p in (alt_prices or []):
            if _p is None or idx_y_h is None:
                continue
            try:
                _d = dirty_price_rub(_pricing_face, _p, accrued)
                if _d is None or _d <= 0 or _d > _fut_h * 1.0005:
                    y_idx_alt_h[_p] = None
                    continue
                _y = xirr_yield_pct(_d, cfs_h, calc_date)
                y_idx_alt_h[_p] = (_sane_bps(round((_y - idx_y_h) * 100.0), warnings,
                                             "yield_over_index_alt_horizon")
                                   if _y is not None else None)
            except Exception as e:
                logger.warning(f"alt-price horizon Y-IDX error {bond.isin} @{_p}: {e}")
                y_idx_alt_h[_p] = None
        return {
            "y_idx_by_price": y_idx_alt_h,
            "date": cut,
            "price_pct": _opp(offers, cut),
            "sm_bps": _sane_bps(sm_h, warnings, "sm_horizon"),
            "disc_margin_bps": _sane_bps(dm_h, warnings, "disc_margin_horizon"),
            "yield_xirr_pct": _sane_pct(round(y_h, 4) if y_h is not None else None,
                                        warnings, "yield_horizon"),
            "index_yield_pct": round(idx_y_h, 4) if idx_y_h is not None else None,
            "yield_over_index_bps": (_sane_bps(round((y_h - idx_y_h) * 100.0), warnings,
                                               "yield_over_index_horizon")
                                     if (y_h is not None and idx_y_h is not None) else None),
        }

    _mat_end = max((cf.pay_date for cf in cfs), default=None) or bond.maturity_date
    horizons: Dict[str, Any] = {"maturity": {
        # дата ФАКТИЧЕСКОГО конца потока: у бумаги с обрезкой по оферте метка
        # «до погашения» обещала 2038 год, а число относилось к 2028-му
        "date": _mat_end, "price_pct": 100.0,
        "y_idx_by_price": y_idx_by_price,
        "sm_bps": sm_bps, "disc_margin_bps": disc_margin_bps,
        "yield_xirr_pct": round(impl_yield, 4) if impl_yield is not None else None,
        "index_yield_pct": round(index_yield, 4) if index_yield is not None else None,
        "yield_over_index_bps": yield_over_index_bps,
    }}
    for _key, _dt in (("put", _put), ("call", _call)):
        # Горизонт за КОНЦОМ ПОТОКА не существует: если поток режется пут-офертой
        # (после неё эмитент пересматривает ставку), то колл-опцион, назначенный
        # позже, ничего не дисконтирует — до него платежей уже нет. Раньше здесь
        # стояло сравнение с bond.maturity_date, и такой горизонт не только
        # считался, но и выигрывал по правилу цены: СИМПЛСК1Р1 — поток до
        # 03.02.2027, а call 30.12.2027 перехватывал витрину при цене выше 100.5,
        # и Y-IDX РОС с ценой (165 → 291), что экономически невозможно.
        if _dt is None or (_mat_end is not None and _dt >= _mat_end):
            continue
        try:
            _m = _metrics_at(_dt)
            if _m:
                horizons[_key] = _m
        except Exception as e:
            logger.warning(f"{_key}-horizon valuation error for {bond.isin}: {e}")

    # ПРАВИЛО ЦЕНЫ: держатель предъявит пут, только если бумага торгуется НИЖЕ
    # цены выкупа (сдать по 100 выгоднее, чем держать дешёвый актив); при цене
    # выше выкупа он не сдаст — горизонт остаётся погашение. Call зеркально:
    # эмитент выкупит дорогой для себя долг (цена ВЫШЕ выкупа), дешёвый оставит.
    horizon = _preferred_horizon(price, horizons)
    _sel = horizons.get(horizon if horizon != "maturity" else "", {})
    if horizon != "maturity":
        offer_date = _sel.get("date")
        offer_price_pct = _sel.get("price_pct")
    elif "put" in horizons:      # горизонт не выбран правилом, но оферта есть — показать в карточке
        offer_date = horizons["put"]["date"]
        offer_price_pct = horizons["put"]["price_pct"]
    elif "call" in horizons:
        offer_date = horizons["call"]["date"]
        offer_price_pct = horizons["call"]["price_pct"]

    # legacy-поля *_to_offer = пут-горизонт (их формат не меняем: universe/UI/
    # алерты на них завязаны; call живёт только в блоке horizons)
    if "put" in horizons:
        sm_to_offer = horizons["put"]["sm_bps"]
        dm_to_offer = horizons["put"]["disc_margin_bps"]
        y_to_offer = horizons["put"]["yield_xirr_pct"]

    # гарант. номинальный убыток → цена битая, спред-метрики бессмысленны: чистим
    # (иначе −2221 DM лезет в топ). yield/dirty оставляем (факт от цены).
    if price_implausible:
        warnings.append(f"цена {price}% подразумевает номинальный убыток "
                        f"(dirty {dirty_rub:.0f}₽ > Σ будущих потоков {_future_sum:.0f}₽) — "
                        "вероятно стейл/тонкая цена неликвида; SM/DM/спред скрыты")
        sm_bps = disc_margin_bps = yield_over_index_bps = None
        sm_to_offer = dm_to_offer = None
        for _h in horizons.values():
            _h["sm_bps"] = _h["disc_margin_bps"] = _h["yield_over_index_bps"] = None

    # САНИТИ — ВСЕЙ СТРОКОЙ, А НЕ ПОМЕТРИЧНО. Пороги ловят метрики по одной, и
    # строка выходила противоречивой: Y-IDX скрыт как безумный, а DM из того же
    # расчёта остаётся (24.08: dm 14 824 bps при пустом Y-IDX — цена 8 % от
    # номинала, дефолтный неликвид). Спред-метрики одного расчёта либо все
    # осмысленны, либо все нет; yield и dirty оставляем — это факт от цены.
    _sanity = any(w.startswith("sanity:") for w in warnings)
    if _sanity:
        sm_bps = disc_margin_bps = yield_over_index_bps = None
        sm_to_offer = dm_to_offer = None
        y_idx_by_price = {}
        for _h in horizons.values():
            _h["sm_bps"] = _h["disc_margin_bps"] = _h["yield_over_index_bps"] = None

    # Y-IDX пуст, а статус «успех» — противоречие: так выглядела недоступная
    # RUONIA-кривая (база сравнения), и потребитель считал число просто
    # отсутствующим, а не сбойным. Первичная метрика решает статус наравне с SM.
    _ok = sm_bps is not None and yield_over_index_bps is not None
    status = "SUCCESS" if _ok else ("PRICE_IMPLAUSIBLE" if price_implausible else "DM_FAILED")
    if _sanity:
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
        "horizons": horizons,                  # {maturity|put|call: полный набор метрик}
        "offer_date": offer_date,
        "offer_price_pct": offer_price_pct,
        "sm_to_offer_bps": sm_to_offer,
        "disc_margin_to_offer_bps": dm_to_offer,
        "yield_to_offer_pct": y_to_offer,
    }


def alt_horizon(hz_key: str, horizons: Dict[str, Any]) -> Optional[str]:
    """Второй горизонт: к погашению ↔ к ближайшей оферте. None, если у бумаги
    оферт нет и переключать не на что. Им живут свитчер графика и дневной снимок
    спреда (там второй горизонт держит линию истории сопоставимой, когда горизонт
    бумаги меняется во времени)."""
    if hz_key != "maturity":
        return "maturity"
    for k in ("put", "call"):
        if k in (horizons or {}):
            return k
    return None


def pick_horizon(m: Dict[str, Any], horizon: str = "auto") -> Dict[str, Any]:
    """Метрики ВЫБРАННОГО горизонта из ответа calculate_valuation_metrics.

    horizon: "auto" — тот, что выбрало правило цены (preferred_horizon),
    иначе явный ключ "maturity" | "put" | "call" (свитчер карточки). Если
    запрошенного горизонта у бумаги нет — молча падаем на погашение.
    Возвращает плоский dict метрик + "horizon" (что реально выбрано)."""
    hzs = m.get("horizons") or {}
    key = m.get("preferred_horizon", "maturity") if horizon in (None, "", "auto") else horizon
    if key not in hzs:
        key = "maturity"
    sel = dict(hzs.get(key) or {})
    sel["horizon"] = key
    return sel


def horizon_pair(m: Dict[str, Any], horizon: str = "auto") -> tuple:
    """(метрики выбранного горизонта, метрики ВТОРОГО, ключ второго).

    Один и тот же приём — «взять числа выбранного горизонта, рядом положить
    числа альтернативного» — нужен пяти потребителям: витрине, стакану, ленте
    as-of, честной истории и скринеру. Каждый писал его сам, и копии разъезжались:
    universe вообще доставал horizons вручную мимо pick_horizon, без отката на
    погашение, когда выбранного ключа в ответе нет. Правки правила горизонта
    (21.08.2026 — выбор по доходности, отсечение горизонтов за концом потока)
    должны доезжать до всех сразу, поэтому извлечение тоже одно.

    Второй горизонт нужен графику (свитчер «погашение ↔ оферта») и дневному
    снимку: горизонт бумаги меняется во времени, и без пары линия истории
    склеивает несопоставимые числа."""
    sel = pick_horizon(m, horizon)
    alt_key = alt_horizon(sel.get("horizon") or "maturity", m.get("horizons") or {})
    alt = dict((m.get("horizons") or {}).get(alt_key) or {}) if alt_key else {}
    if alt and alt_key:
        alt["horizon"] = alt_key
    return sel, alt, alt_key


def horizon_value(m: Dict[str, Any], field: str, horizon: str = "auto"):
    """Одно поле выбранного горизонта с откатом на верхнеуровневое значение.

    Верхнеуровневые поля ответа всегда посчитаны К ПОГАШЕНИЮ (сверочная база с
    НРД), поэтому откат допустим только как последний шаг — когда горизонта в
    ответе нет вовсе."""
    sel = pick_horizon(m, horizon)
    return sel.get(field, m.get(field))


# Мёртвая зона вокруг цены выкупа, пп. Цена в пределах буфера = «практически по
# номиналу»: дисконт в считанные копейки не окупает поход на оферту
# (транзакционка + купон после оферты эмитент переставляет), а премия в копейки
# не заставит эмитента отзывать выпуск. Без буфера бумага у номинала (МТС 3Р-02,
# bid/ask 99.95/100.00) прыгала между горизонтами от тика к тику: last к оферте,
# ask к погашению — в одной строке таблицы две несопоставимые цифры.
_PAR_BUFFER_PCT = 0.5
# Мёртвая зона выбора горизонта в базисных пунктах ДОХОДНОСТИ: опцион исполняют
# ради выгоды, а не ради шума, и 10 bps годовых — заведомо меньше типичного
# спреда bid/ask, внутри которого горизонт не должен скакать.
_HORIZON_BUFFER_BPS = 10.0


def _preferred_horizon(price: Optional[float], horizons: Dict[str, Any]) -> str:
    """К ЧЕМУ ПРАЙСИТСЯ БУМАГА — по ДОХОДНОСТИ, а не по цене выкупа.

    Опцион исполняют ради выгоды, и меряется она доходностью:

      ПУТ — право ДЕРЖАТЕЛЯ. Предъявит, если доходность к оферте выше, чем к
      погашению: сдать по 100 выгоднее, чем держать дальше.
      CALL — право ЭМИТЕНТА, зеркально: отзовёт долг, если для держателя это
      ХУЖЕ (доходность к коллу ниже), то есть занял дороже нынешнего рынка.

    Раньше решала ЦЕНА: пут выбирался при цене ниже выкупа на _PAR_BUFFER_PCT.
    Цена — прокси доходности, и на близкой оферте прокси врёт: 0.5 пп цены за
    неделю до выкупа это не «копейки», а десятки процентов годовых. Отсюда два
    дефекта. Во-первых, систематический промах: ГПБФин1Р10 при цене 100.00 даёт
    14.96% к оферте против 14.30% к погашению — держатель предъявит, а витрина
    считала к погашению (на проде так шли 11 бумаг из 523, до 75 bps ошибки).
    Во-вторых, разрыв метрики: на границе буфера Y-IDX прыгал с 4701 на 61 bps
    от четверти пункта цены. Сравнение доходностей чинит оба — в точке
    безразличия доходности равны, поэтому переключение непрерывно.

    Буфер остаётся, но в БАЗИСНЫХ ПУНКТАХ доходности (_HORIZON_BUFFER_BPS):
    без него горизонт дребезжал бы внутри одного спреда bid/ask.

    Оба опциона сработали (редкая конфигурация put+call) → ближайшее по дате
    событие. Доходности неизвестны (солвер не сошёлся) → откат на правило цены,
    оно грубее, но лучше, чем ничего.
    """
    mat = horizons.get("maturity") or {}
    y_mat = mat.get("yield_xirr_pct")
    put, call = horizons.get("put") or {}, horizons.get("call") or {}
    buf = _HORIZON_BUFFER_BPS / 100.0          # bps → проценты годовых

    if y_mat is not None:
        cands = []
        y_put, y_call = put.get("yield_xirr_pct"), call.get("yield_xirr_pct")
        if put.get("date") and y_put is not None and y_put > y_mat + buf:
            cands.append(("put", put["date"]))
        if call.get("date") and y_call is not None and y_call < y_mat - buf:
            cands.append(("call", call["date"]))
        if cands:
            return min(cands, key=lambda c: c[1])[0]
        # доходность хотя бы одного опциона не посчиталась — дорешаем ценой
        if not ((put.get("date") and y_put is None)
                or (call.get("date") and y_call is None)):
            return "maturity"

    if price is None:
        return "maturity"
    cands = []
    if put.get("date") and price < (put.get("price_pct") or 100.0) - _PAR_BUFFER_PCT:
        cands.append(("put", put["date"]))
    if call.get("date") and price > (call.get("price_pct") or 100.0) + _PAR_BUFFER_PCT:
        cands.append(("call", call["date"]))
    if not cands:
        return "maturity"
    return min(cands, key=lambda c: c[1])[0]


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
