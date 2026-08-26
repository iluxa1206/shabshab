import logging
from datetime import date
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Сервис отдаёт plain dicts (ключи = поля api.schemas.CashflowItem) — Pydantic
# коэрсит их на route-слое. Раньше сервис импортировал api.schemas: домен
# зависел от API-контракта (инверсия слоёв).


def _item(**kw) -> dict:
    return kw


def _d(s):
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def build_cashflow_from_moex(
    ref, curve, calc_date: date, coupons: list, amorts: list, formula: str,
    offers: Optional[list] = None, warnings_out: Optional[list] = None,
    cut_date: Optional[date] = None,
) -> Tuple[List[dict], float]:
    """Cashflow-таблица карточки. ФОРМАТТЕР, не самостоятельный расчёт:

    - БУДУЩИЙ поток (купоны с end > settle + погашение) берём из КАНОНИЧЕСКОГО
      build_cashflows_to_maturity (через build_cashflows_with_spread). Вся рисковая
      математика (проекция форвардом, кэп/флор, резка к оферте, амортизация,
      residual) — в ОДНОМ месте → таблица не расходится с SM/DM/z.
    - ПРОШЛЫЕ купоны/амортизации (end/дата ≤ settle) — факт MOEX (value/valueprc),
      простым эхом: они не прайсятся, проекц-математики нет → расхождение невозможно.

    Раньше это был ручной форк ~180 строк, дублировавший проекцию: каждый фикс
    приходилось вносить дважды и копии разъезжались."""
    from core.valuation import build_cashflows_with_spread, settle_date
    settle = settle_date(calc_date)

    # индекс-провайдер для фиксинга начавшегося периода (как в pricing)
    index_pct_fn = None
    if ref.base in ("KEYRATE", "RUONIA"):
        try:
            from functools import partial
            from services.coupon_calib import period_index_pct as _pip, index_history
            index_pct_fn = partial(_pip, idx=index_history(ref.base))
        except Exception as e:
            # Витрина потоков (карточка, waterfall паспорта) обязана считать купон
            # ТЕМ ЖЕ провайдером, что прайсинг. Молчаливый None разводит их на
            # разные методики: в таблице одна ставка купона, в метриках другая.
            logger.warning("%s: провайдер индекса для витрины потоков недоступен "
                           "(%s) — купоны начавшихся периодов разойдутся с прайсингом",
                           ref.isin, e)
            index_pct_fn = None

    _am_all = sorted(
        (_d(a.get("date")), float(a.get("value") or 0))
        for a in (amorts or []) if _d(a.get("date"))
    )
    _amortizing = any(ref.maturity_date and d < ref.maturity_date for d, _ in _am_all)
    _orig_face = ref.face_value + sum(v for d, v in _am_all if d <= calc_date)

    items: List[dict] = []

    # 1) ПРОШЛЫЕ купоны — факт MOEX (эхо value/valueprc), не прайсятся
    for c in (coupons or []):
        end = _d(c.get("end"))
        if not end or end > settle:
            continue
        start = _d(c.get("start")) or end
        days = (end - start).days or 1
        face = (max(_orig_face - sum(v for d, v in _am_all if d <= start), 0.0)
                if _amortizing else ref.face_value)
        val, vp = c.get("value"), c.get("valueprc")
        amount = float(val) if val is not None else 0.0
        rate = (float(vp) if vp is not None
                else (amount / face * 365.0 / days * 100.0 if face else 0.0))
        items.append(_item(
            number=0, period_start=start, period_end=end, payment_date=end,
            coupon_formula=formula, base_rate_pct=0.0, spread_bps=ref.spread_issue_bps or 0,
            coupon_rate_pct=round(rate, 4), amount_rub=round(amount, 2), type="COUPON",
        ))

    # 2) ПРОШЛЫЕ амортизации — факт (эхо)
    for d, amt in _am_all:
        if d <= settle:
            items.append(_item(
                number=0, period_start=d, period_end=d, payment_date=d,
                coupon_formula="", base_rate_pct=0.0, spread_bps=0,
                coupon_rate_pct=0.0, amount_rub=round(amt, 2), type="REDEMPTION",
            ))

    # 3) БУДУЩИЙ поток — из канонического builder (единый источник).
    # Сбой билдера (экзотика/битые данные) раньше ронял всю карточку 500 —
    # теперь фолбэк: только зафиксированные факты MOEX (как в календаре выплат)
    # + warning о деградации (без лесенки маржи/кэпа/прогноза).
    periods = [(c.get("start"), c.get("end"), c.get("value")) for c in (coupons or [])]
    try:
        cfs = build_cashflows_with_spread(
            ref, curve, calc_date, ref.spread_issue_bps or 0,
            explicit_periods=periods or None, amorts=amorts, offers=offers,
            index_pct_fn=index_pct_fn, cut_date=cut_date,
        )
    except Exception as e:
        if warnings_out is not None:
            warnings_out.append(
                f"поток деградирован: {type(e).__name__} — только зафиксированные "
                "купоны MOEX, без прогноза/кэпа/лесенки маржи")
        for c in (coupons or []):
            end = _d(c.get("end"))
            if not end or end <= settle or c.get("value") is None:
                continue
            start = _d(c.get("start")) or end
            days = (end - start).days or 1
            vp = c.get("valueprc")
            rate = (float(vp) if vp is not None
                    else (float(c["value"]) / ref.face_value * 365.0 / days * 100.0
                          if ref.face_value else 0.0))
            items.append(_item(
                number=0, period_start=start, period_end=end, payment_date=end,
                coupon_formula=formula, base_rate_pct=0.0,
                spread_bps=ref.spread_issue_bps or 0,
                coupon_rate_pct=round(rate, 4),
                amount_rub=round(float(c["value"]), 2), type="COUPON",
            ))
        for d, amt in _am_all:
            if d > settle:
                items.append(_item(
                    number=0, period_start=d, period_end=d, payment_date=d,
                    coupon_formula="", base_rate_pct=0.0, spread_bps=0,
                    coupon_rate_pct=0.0, amount_rub=round(amt, 2), type="REDEMPTION",
                ))
        # ОСТАТОК, А НЕ ПОЛНЫЙ НОМИНАЛ: будущие транши уже выплачены выше, и
        # ref.face_value после amort_remaining_face равен их сумме. Проверка «нет
        # транша ровно на maturity» от двойного счёта не спасала — у амортизируемых
        # последний транш сдвинут business-day adjustment'ом либо (у ABS) лежит за
        # горизонтом пагинации ISS. Считаем как канонический билдер (residual).
        _future_am = sum(amt for d, amt in _am_all if d > settle)
        # База — pricing_face, РОВНО как в каноне (core/valuation.py:906): транш
        # из окна (calc_date, settle] достаётся продавцу и уже эмитится выше
        # как прошлая амортизация. Без вычета он попадал в поток дважды —
        # накануне амортизации принципал завышался на весь транш.
        from core.valuation import face_for_pricing as _ffp
        residual = _ffp(ref.face_value or 0.0, amorts, calc_date) - _future_am
        if ref.maturity_date and ref.maturity_date > settle and residual > 1e-9:
            items.append(_item(
                number=0, period_start=ref.maturity_date, period_end=ref.maturity_date,
                payment_date=ref.maturity_date, coupon_formula="", base_rate_pct=0.0,
                spread_bps=0, coupon_rate_pct=0.0,
                amount_rub=round(residual, 2), type="REDEMPTION",
            ))
        cfs = []
    for cf in cfs:
        is_cpn = cf.type == "COUPON"
        items.append(_item(
            number=0,
            period_start=cf.period_start or cf.pay_date,
            period_end=cf.period_end or cf.pay_date,
            payment_date=cf.pay_date,
            coupon_formula=formula if is_cpn else "",
            base_rate_pct=cf.base_rate_pct if cf.base_rate_pct is not None else 0.0,
            spread_bps=cf.spread_bps if is_cpn else 0,
            coupon_rate_pct=cf.coupon_rate_pct if cf.coupon_rate_pct is not None else 0.0,
            amount_rub=round(cf.amount_rub, 2), type=cf.type,
        ))

    items.sort(key=lambda x: x["payment_date"])
    for i, it in enumerate(items, start=1):
        it["number"] = i
    redemption_total = sum(it["amount_rub"] for it in items if it["type"] == "REDEMPTION")
    return items, redemption_total
