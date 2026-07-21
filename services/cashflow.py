from datetime import date, timedelta
from typing import List, Optional, Tuple, Dict, Any

from cashflow import (
    adjust_following,
    build_coupon_schedule,
    parse_base_and_spread
)
from api.schemas import CashflowItem
from services.exceptions import CalculationException


def _d(s):
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def build_cashflow_from_moex(
    ref, curve, calc_date: date, coupons: list, amorts: list, formula: str,
) -> Tuple[List[CashflowItem], float]:
    """Cashflow по реальному расписанию MOEX:
    прошлые/зафиксированные купоны = фактическая сумма (value/valueprc),
    будущие плавающие = прогноз (forward + spread). Погашение из амортизаций."""
    sp = (ref.spread_issue_bps or 0) / 10000.0
    items: List[CashflowItem] = []
    n = 0
    # I/O-граница: история индекса — один фетч на вызов (не в цикле по купонам)
    _index_pct_fn = None
    if ref.base in ("KEYRATE", "RUONIA"):
        try:
            from functools import partial
            from services.coupon_calib import period_index_pct as _pip, index_history
            _index_pct_fn = partial(_pip, idx=index_history(ref.base))
        except Exception:
            _index_pct_fn = lambda *a, **k: None
    # амортизируемая: остаточный номинал на начало периода. Считаем сами (поле face
    # строк купонов MOEX ненадёжно, всегда 1000). Берём ПОЛНЫЙ график погашений
    # (не только будущие: погашение ~сегодня уже уменьшило остаток) относительно
    # исходного номинала = текущий + уже погашенное.
    _am_all = sorted(
        (_d(a.get("date")), float(a.get("value") or 0))
        for a in (amorts or []) if _d(a.get("date"))
    )
    _amortizing = any(ref.maturity_date and d < ref.maturity_date for d, _ in _am_all)
    _orig_face = ref.face_value + sum(v for d, v in _am_all if d <= calc_date)

    # Достройка хвоста купонов до погашения: bondization обрывается на 100-м
    # купоне (пагинация ISS) — у длинных бумаг display недосчитывает годы купонов.
    if coupons and ref.maturity_date:
        from valuation import extend_periods_to_maturity
        triples = [(c.get("start"), c.get("end"), c.get("value")) for c in coupons]
        ext = extend_periods_to_maturity(triples, ref.maturity_date)
        if len(ext) > len(triples):
            coupons = [{"start": s.isoformat(), "end": e.isoformat(),
                        "value": v, "valueprc": None, "face": None} for s, e, v in ext]

    for c in coupons:
        end = _d(c.get("end"))
        if not end:
            continue
        start = _d(c.get("start")) or end
        if _amortizing:
            paid = sum(v for d, v in _am_all if d <= start)
            face = max(_orig_face - paid, 0.0)
        else:
            try:
                face = float(c.get("face")) if c.get("face") is not None else ref.face_value
            except (ValueError, TypeError):
                face = ref.face_value
        days = (end - start).days or 1
        alpha = days / 365.0
        val = c.get("value")
        vp = c.get("valueprc")

        if val is not None:
            # фактический / зафиксированный купон из MOEX
            amount = float(val)
            base_pct = 0.0
            rate_pct = float(vp) if vp is not None else (amount / face * 365.0 / days * 100 if face else 0.0)
        else:
            # НАЧАВШИЙСЯ период (start ≤ calc): индекс (частично) определён по
            # факту — coupon_calib.period_index_pct (спека point/average+лаг,
            # фолбэк — точечная КС на start). Общая точка с pricing (valuation,
            # zspread): display показывает тот же купон, что заложен в SM/z.
            idx_pct = None
            if _index_pct_fn is not None and start <= calc_date < end:
                try:
                    ks_fwd = lambda dt: (curve.forward(max(dt, calc_date), end) * 100.0) if curve else 0.0
                    # ref.face_value (текущий остаток) + amorts, а НЕ face периода:
                    # калибратор откатывает номинал сам, и все три пайплайна
                    # (valuation, zspread, display) обязаны кормить его одним и тем же.
                    idx_pct = _index_pct_fn(ref.isin, ref.base, coupons, ref.face_value,
                                            start, end, calc_date, ks_fwd, amorts=amorts)
                except Exception:
                    idx_pct = None
            if idx_pct is not None:
                r = idx_pct / 100.0 + sp
                factor = r * alpha
                amount = face * factor
                base_pct = round(idx_pct, 4)
                rate_pct = round(r * 100, 4)
            else:
                # будущий плавающий — прогноз по forward + spread (конвенция выпуска:
                # маржа simple; RUONIA-индекс капитализируется дневно).
                # анкер форварда клэмпим к calc_date (прошлый стаб не форвардим — мусор)
                fstart = start if start > calc_date else calc_date
                f = curve.forward(fstart, end) if (curve and ref.base in ("RUONIA", "KEYRATE") and fstart < end) else 0.0
                if ref.base == "RUONIA":
                    factor = (1.0 + f / 365.0) ** days - 1.0 + sp * alpha
                elif ref.base == "KEYRATE":
                    factor = (f + sp) * alpha
                else:
                    factor = 0.0
                r = factor / alpha if alpha > 0 else 0.0   # эффективная ставка периода
                amount = face * factor
                base_pct = round(f * 100, 4)
                rate_pct = round(r * 100, 4)

        n += 1
        items.append(CashflowItem(
            number=n, period_start=start, period_end=end, payment_date=end,
            coupon_formula=formula, base_rate_pct=base_pct, spread_bps=ref.spread_issue_bps or 0,
            coupon_rate_pct=round(rate_pct, 4), amount_rub=round(amount, 2), type="COUPON",
        ))

    # погашение принципала (амортизации). Если нет — номинал на дату погашения.
    redemption_total = 0.0
    if amorts:
        for a in amorts:
            d = _d(a.get("date"))
            if not d:
                continue
            amt = float(a.get("value") or 0)
            redemption_total += amt
            n += 1
            items.append(CashflowItem(
                number=n, period_start=d, period_end=d, payment_date=d,
                coupon_formula="", base_rate_pct=0.0, spread_bps=0,
                coupon_rate_pct=0.0, amount_rub=round(amt, 2), type="REDEMPTION",
            ))
    elif ref.maturity_date:
        redemption_total = ref.face_value
        n += 1
        items.append(CashflowItem(
            number=n, period_start=ref.maturity_date, period_end=ref.maturity_date,
            payment_date=ref.maturity_date, coupon_formula="", base_rate_pct=0.0,
            spread_bps=0, coupon_rate_pct=0.0, amount_rub=round(ref.face_value, 2), type="REDEMPTION",
        ))

    items.sort(key=lambda x: x.payment_date)
    for idx, it in enumerate(items, start=1):
        it.number = idx
    return items, redemption_total

def get_cashflow_items(
    isin: str,
    start_date: Optional[date],
    end_date: Optional[date],
    coupon_period_days: Optional[int],
    face_value: float,
    formula: str,
    base_rate: Optional[str],
    ruonia_curve: Any,
    keyrate_curve: Any,
    calc_date: date,
    coupon_percent: Optional[float],
    next_coupon_date: Optional[date],
) -> Tuple[List[CashflowItem], float]:
    
    if not end_date or not coupon_period_days:
        raise CalculationException("Недостаточно данных для построения графика (нет end_date или coupon_period_days)", {"isin": isin})

    if not start_date:
        if next_coupon_date:
            start_date = next_coupon_date - timedelta(days=coupon_period_days)
        else:
            raise CalculationException("Нет даты начала размещения и NEXTCOUPON", {"isin": isin})

    schedule = build_coupon_schedule(start_date, end_date, coupon_period_days)
    base, spread_bps = parse_base_and_spread(formula, base_rate)

    items = []
    prev_date = start_date
    for i, coup_date in enumerate(schedule, start=1):
        days = (coup_date - prev_date).days
        alpha = days / 365.0 if days > 0 else 0.0

        computed_rate = 0.0
        factor = 0.0
        base_rate_pct = 0.0
        
        # Only compute for future coupons
        if coup_date > calc_date:
            start_fwd = max(prev_date, calc_date)
            if start_fwd < coup_date:
                if base == "RUONIA" and ruonia_curve:
                    fwd = ruonia_curve.forward(start_fwd, coup_date)
                    base_rate_pct = fwd * 100
                    computed_rate = fwd + spread_bps / 10000.0
                    # индекс daily-comp, маржа simple (конвенция выпуска)
                    factor = (1.0 + fwd / 365.0) ** days - 1.0 + spread_bps / 10000.0 * alpha
                elif base == "KEYRATE" and keyrate_curve:
                    fwd = keyrate_curve.forward(start_fwd, coup_date)
                    base_rate_pct = fwd * 100
                    computed_rate = fwd + spread_bps / 10000.0
                    factor = computed_rate * alpha  # (КС+m) simple ACT/365
                elif coupon_percent is not None:
                    computed_rate = coupon_percent / 100.0
                    factor = computed_rate * alpha
        else:
            # Past coupons - we don't recalculate payout in standard mode, returning zero or basic info
             if coupon_percent is not None:
                computed_rate = coupon_percent / 100.0
                factor = computed_rate * alpha

        payout = face_value * factor
        
        item = CashflowItem(
            number=i,
            period_start=prev_date,
            period_end=coup_date,
            payment_date=coup_date,
            coupon_formula=formula,
            base_rate_pct=round(base_rate_pct, 4) if base_rate_pct else 0.0,
            spread_bps=spread_bps,
            coupon_rate_pct=round(computed_rate * 100, 4) if computed_rate > 0 else 0.0,
            amount_rub=round(payout, 2),
            type="COUPON"
        )
        items.append(item)
        prev_date = coup_date

    redemption_date = adjust_following(end_date)
    redemption_item = CashflowItem(
        number=len(items) + 1,
        period_start=prev_date, # arbitrary for redemption
        period_end=redemption_date,
        payment_date=redemption_date,
        coupon_formula="",
        base_rate_pct=0.0,
        spread_bps=0,
        coupon_rate_pct=0.0,
        amount_rub=round(face_value, 2),
        type="REDEMPTION"
    )
    items.append(redemption_item)

    return items, face_value
