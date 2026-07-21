"""
Метрики флоатеров — то, что действительно нужно для решения по бумаге с плавающим
купоном (в отличие от фикс-купонных, где всё завязано на YTM/duration).

Ключевая специфика: между рефиксингами купон переставляется под рынок, поэтому
чувствительность цены к ПАРАЛЛЕЛЬНОМУ сдвигу ставки (rate duration) ≈ сроку до
следующего рефиксинга (недели/месяцы), а ВЕСЬ кредитный риск (spread duration)
живёт до погашения. Поэтому:
  - rate duration  ≈ дни до рефиксинга / 365  — малая, флоатер защищён от роста КС;
  - spread duration ≈ Macaulay потоков       — большая, тут сидит P&L при ΔDM/Δz.

Функции чистые: принимают уже посчитанные потоки/купоны/кривую, без сети.
"""
from __future__ import annotations
import math
from datetime import date
from typing import Optional


def years_to(d: Optional[date], calc_date: date) -> Optional[float]:
    """Срок до даты в годах ACT/365 (0, если дата в прошлом; None если нет даты)."""
    if not d:
        return None
    return max((d - calc_date).days, 0) / 365.0


def macaulay_years(cfs, calc_date: date, y: float) -> Optional[float]:
    """Macaulay-длительность потоков при непрерывной доходности y (годы).
    Для флоатера это spread duration — приближённая dP/P на 1% параллельного
    сдвига дисконт-кривой (кредитного спреда), т.к. будущие купоны уже прогнозные."""
    num = den = 0.0
    for pay, amt in cfs:
        tau = (pay - calc_date).days / 365.0
        if tau <= 0:
            continue
        df = amt * math.exp(-y * tau)
        num += tau * df
        den += df
    return num / den if den > 0 else None


def current_period(coupons, calc_date: date):
    """Купон, чей период накрывает calc_date (или ближайший будущий).
    Возвращает dict {start, end, value, valueprc} с date-объектами или None."""
    def _d(s):
        try:
            return date.fromisoformat(s) if s else None
        except (ValueError, TypeError):
            return None
    best = None
    for c in coupons or []:
        s, e = _d(c.get("start")), _d(c.get("end"))
        if not s or not e:
            continue
        if s <= calc_date < e:
            return {"start": s, "end": e, "value": c.get("value"), "valueprc": c.get("valueprc")}
        if e > calc_date and (best is None or e < best["end"]):
            best = {"start": s, "end": e, "value": c.get("value"), "valueprc": c.get("valueprc")}
    return best


def days_to_refix(coupons, calc_date: date) -> Optional[int]:
    """Дни до конца текущего купонного периода = дата следующей переустановки
    ставки. Малое значение → флоатер быстро поймает новую КС (важно на развороте)."""
    cp = current_period(coupons, calc_date)
    if not cp:
        return None
    return max((cp["end"] - calc_date).days, 0)


def current_coupon_pct(coupons, calc_date: date) -> Optional[float]:
    """Зафиксированная годовая ставка текущего купона (valueprc MOEX), %."""
    cp = current_period(coupons, calc_date)
    if not cp:
        return None
    vp = cp.get("valueprc")
    return float(vp) if vp is not None else None


def carry_bps(coupon_yield_pct: Optional[float], base_rate_pct: Optional[float]) -> Optional[int]:
    """Carry = текущая купонная доходность − базовая ставка (КС/RUONIA), bps.
    >0: флоатер платит больше стоимости фондирования по базе (есть смысл держать
    против LQDT/депозита); <0: тревога, спред выпуска не покрывает премию."""
    if coupon_yield_pct is None or base_rate_pct is None:
        return None
    return round((coupon_yield_pct - base_rate_pct) * 100)


def breakeven_base_pct(coupon_yield_pct: Optional[float], base_rate_pct: Optional[float],
                       spread_issue_bps: Optional[int]) -> Optional[float]:
    """Уровень базовой ставки, ниже которого купон флоатера после рефиксинга
    станет меньше текущей купонной доходности (carry-эдж исчезает):
    купон(B) = B + spread < coupon_yield  ⇔  B < coupon_yield − spread.
    Показывает, сколько «пространства снижения КС» есть до потери привлекательности."""
    if coupon_yield_pct is None:
        return None
    return round(coupon_yield_pct - (spread_issue_bps or 0) / 100.0, 2)


def face_on_date(face_value: float, amorts, start: date, calc_date: date) -> float:
    """Номинал, действовавший на дату start ≤ calc_date: остаток на calc_date +
    амортизации, выплаченные в (start, calc_date]. Нужен, чтобы ставку купона
    восстанавливать из рублёвой суммы по номиналу ЕГО периода, а не текущему."""
    def _d(s):
        if isinstance(s, date):
            return s
        try:
            return date.fromisoformat(s) if s else None
        except (ValueError, TypeError):
            return None
    add = 0.0
    for a in amorts or []:
        d = _d(a.get("date"))
        if d and a.get("value") is not None and start < d <= calc_date:
            add += float(a["value"])
    return face_value + add


def base_level_pct(exp_curve) -> Optional[float]:
    """Текущий уровень базовой ставки (КС/RUONIA) с короткого конца кривой
    ожиданий (spot на ~недельном сроке), %, в конвенции самого индекса
    (KEYRATE — quarterly-nominal ≈ уровень КС; RUONIA — daily-comp avg)."""
    if exp_curve is None:
        return None
    try:
        return round(exp_curve.spot(0.02) * 100.0, 4)
    except Exception:
        return None


def carry_refix_block(coupons, amorts, face_value: float, price_pct: Optional[float],
                      exp_curve, nrd_current_yield: Optional[float],
                      calc_date: date) -> dict:
    """{carry_bps, days_to_refix, current_coupon_pct, coupon_yield_pct,
    base_rate_pct} — единый блок carry/refix.

    Раньше был скопирован ТРИЖДЫ (universe-фон, universe-watch, карточка) и копии
    начинали разъезжаться. Логика: ставка текущего купона из valueprc, фолбэк —
    восстановление из рублёвой суммы по номиналу периода (face_on_date);
    carry = купонная доходность на вложенное (купон/цена, как НРД current_yield;
    фолбэк из ставки-на-номинал приводится к цене) − уровень базы с короткого
    конца кривой ожиданий."""
    refix = days_to_refix(coupons, calc_date)
    cur_cpn = current_coupon_pct(coupons, calc_date)
    cp = current_period(coupons, calc_date)
    if cur_cpn is None and cp and cp.get("value") is not None:
        cdays = (cp["end"] - cp["start"]).days or 1
        face_p = face_on_date(face_value, amorts, cp["start"], calc_date)
        if face_p > 0:
            cur_cpn = round(float(cp["value"]) / face_p * 365.0 / cdays * 100.0, 3)
    coupon_yield = nrd_current_yield
    if coupon_yield is None and cur_cpn is not None:
        coupon_yield = round(cur_cpn / (price_pct / 100.0), 3) if price_pct else cur_cpn
    base_lvl = base_level_pct(exp_curve)
    return {
        "carry_bps": carry_bps(coupon_yield, base_lvl),
        "days_to_refix": refix,
        "current_coupon_pct": cur_cpn,
        "coupon_yield_pct": coupon_yield,
        "base_rate_pct": base_lvl,
    }


def rank_pct(value: Optional[float], peers: list) -> Optional[int]:
    """Перцентиль value среди peers (доля peers со значением ≤ value), 0..100.
    Для z-спреда внутри рейтингового бакета: высокий перцентиль = бумага дороже
    отдаёт спред, чем группа (потенциально дёшево/rich-cheap-скрин)."""
    vals = [v for v in peers if v is not None]
    if value is None or len(vals) < 3:
        return None
    below = sum(1 for v in vals if v <= value)
    return round(100 * below / len(vals))
