"""
z-спред над КБД ОФЗ (методика НРД) — наш расчётный аналог.

Пайплайн: прогноз купонов на кривой ОЖИДАНИЙ индекса (bootstrap par-свопов) →
дальше по базе:
  RUONIA  — met_rub п.4.9: дисконт по КБД непрерывно exp(-(r_g(τ)+z)τ) → solve z.
  KEYRATE — реверс-инжиниринг публикуемого z НРД (2026-07-02): для флоатеров
    z_НРД ≈ ytm_eff − G(горизонт рефиксинга); их duration = длина купонного
    периода, G клэмпится к короткому концу КБД. Наш аналог:
    y — плоская непрерывная доходность потоков (solve_flat_y), z = (e^y−1) − G(τ_reset).

Валидация (scripts/nrd_pipeline_probe.py, 2026-07-02, цена НРД на входе):
RUONIA ±40bps (старый путь); KEYRATE mean|Δ|≈25bps, медиана −12 (узкая выборка
watch-качества), юниверс 127 бумаг (с амортизируемыми): AAA..A mean|Δ|≈86,
медиана −15 — остаток растёт к низким рейтингам (кросс-секция NSS+Kalman
эмитента у НРД, Прил.6, из публичных данных несводимо). Устойчив во времени
(12 дат: bias ≈ 0, std 17-32bps).

Амортизируемые (project_cfs с amorts, 2026-07-02): принципал по графику
амортизаций MOEX, купоны от остаточного номинала (= сумма будущих амортизаций
после начала периода; поле face строк купонов ненадёжно). IG-сегмент n=16:
mean|Δ|≈113, медиана +81; хвост низких рейтингов — та же кросс-секция НРД.
"""
from __future__ import annotations
import math
from datetime import date
from typing import List, Optional

TENOR_Y = {"1W": 1/52, "2W": 2/52, "1M": 1/12, "2M": 2/12, "3M": .25, "6M": .5,
           "9M": .75, "1Y": 1, "2Y": 2, "3Y": 3, "4Y": 4, "5Y": 5, "6Y": 6,
           "7Y": 7, "8Y": 8, "9Y": 9, "10Y": 10}


class ExpCurve:
    """Кривая ожиданий индекса поверх честного bootstrap par-свопов (forwards.py).

    fwd(d1,d2) — эквивалентная ставка индекса на период в его собственной конвенции:
      RUONIA  — daily-comp average (project_cfs начисляет (1+r/365)^days − 1 →
                factor DF воспроизводится точно);
      KEYRATE — quarterly-nominal = уровень КС (простое начисление (КС+m)·days/365).
    spot(t) — уровень индекса на горизонте t лет той же конвенцией
    (короткий конец ≈ текущий фиксинг, заложенный в свопы)."""
    def __init__(self, calc_date: date, quotes, base: str = "RUONIA"):
        from datetime import timedelta
        from forwards import CurveBootstrapper
        boot = (CurveBootstrapper.bootstrap_ruonia if base == "RUONIA"
                else CurveBootstrapper.bootstrap_keyrate)
        self.base = base
        self.calc_date = calc_date
        self._td = timedelta
        self._curve = boot(quotes, calc_date)

    def t(self, d: date) -> float:
        return (d - self.calc_date).days / 365.0

    def spot(self, t: float) -> float:
        d2 = self.calc_date + self._td(days=max(2, round(t * 365)))
        return self.fwd(self.calc_date, d2)

    def fwd(self, d1: date, d2: date) -> float:
        c = self._curve
        lo = max(d1, c.calc_date)          # кривая стартует с calc_date+1
        if d2 <= lo:
            d2 = lo + self._td(days=1)
        return c.forward(lo, d2)


class GCurve:
    """КБД ОФЗ: линейная интерполяция yearyields (%) по сроку τ (годы)."""
    def __init__(self, pts):  # [(period_years, yield_pct)]
        pts = sorted(pts)
        self.xs = [p[0] for p in pts]; self.ys = [p[1]/100.0 for p in pts]

    def ok(self) -> bool:
        return len(self.xs) >= 2

    def r(self, tau: float) -> float:
        xs, ys = self.xs, self.ys
        if not xs:
            return 0.0
        if tau <= xs[0]:
            return ys[0]
        if tau >= xs[-1]:
            return ys[-1]
        for i in range(1, len(xs)):
            if xs[i] >= tau:
                w = (tau-xs[i-1])/(xs[i]-xs[i-1])
                return ys[i-1]+w*(ys[i]-ys[i-1])
        return ys[-1]


def _d(s):
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def project_cfs(ref, exp: ExpCurve, calc_date: date, coupons: list, amorts: list = None,
                offers: list = None):
    """Прогноз потоков: зафикс.купон = факт MOEX value; будущий = индекс(exp.fwd)+margin.
    KEYRATE простой (КС+m)·days/365; RUONIA daily-comp. + погашение номинала.

    amorts — MOEX bondization amortizations [{date, value}]. Если есть погашение
    принципала до maturity (амортизируемая бумага), принципал платится по датам
    амортизаций, а будущие купоны начисляются от остаточного номинала = сумма
    амортизаций после начала периода (поле face строк купонов MOEX ненадёжно:
    для будущих периодов не проецируется, бывает стейл). Иначе — прежний
    bullet-путь (номинал целиком на maturity_date).

    T+1: платежи с датой <= settle (след. рабочий день) покупателю не достаются
    (ex-coupon, MOEX НКД уже 0) — исключаем, иначе накануне выплаты PV завышен
    на целый купон."""
    from valuation import settle_date, first_offer_date, _offer_price_pct
    settle = settle_date(calc_date)
    sp = (ref.spread_issue_bps or 0) / 10000.0

    # Оферта: режем поток (та же логика, что valuation.build_cashflows_to_maturity —
    # спред после оферты не гарантирован, купоны за ней фикция)
    put = first_offer_date(offers, settle)
    eff_maturity = ref.maturity_date
    if put and (eff_maturity is None or put < eff_maturity):
        eff_maturity = put
    else:
        put = None

    future_am = sorted(
        (d, float(a["value"])) for a in amorts or []
        if a.get("value") is not None and (d := _d(a.get("date"))) and d > settle
        and (eff_maturity is None or d <= eff_maturity)
    )
    amortizing = any(eff_maturity and d < eff_maturity for d, _ in future_am)
    cfs = []
    for c in coupons or []:
        end = _d(c.get("end"))
        if not end or end <= settle or (eff_maturity and end > eff_maturity):
            continue
        start = _d(c.get("start")) or end
        days = (end - start).days or 1
        alpha = days / 365.0
        val = c.get("value")
        if val is not None:
            amt = float(val)
        else:
            face = ref.face_value
            if amortizing:
                paid_by_start = sum(v for d, v in future_am if d <= start)
                face = (ref.face_value - paid_by_start) or face
            f = exp.fwd(max(start, calc_date), end)
            r = f + sp
            if ref.base == "RUONIA":
                amt = face * ((1 + r/365.0)**days - 1)
            else:
                amt = face * r * alpha
        cfs.append((end, amt))
    if put:
        cfs.extend(future_am)
        residual = ref.face_value - sum(v for _d2, v in future_am)
        if residual > 1e-9:
            cfs.append((put, residual * _offer_price_pct(offers, put) / 100.0))
    elif amortizing:
        cfs.extend(future_am)
    elif ref.maturity_date and ref.maturity_date > calc_date:
        cfs.append((ref.maturity_date, ref.face_value))
    cfs.sort()
    return cfs


def solve_z_bps(g: GCurve, cfs, calc_date: date, dirty_target: float) -> Optional[int]:
    """z-спред (bps) над КБД: PV = Σ CF·exp(-(r_g(τ)+z)·τ) = dirty."""
    if not cfs or not g.ok():
        return None

    def pv(z):
        tot = 0.0
        for pay, amt in cfs:
            tau = (pay - calc_date).days / 365.0
            if tau <= 0:
                continue
            tot += amt * math.exp(-(g.r(tau) + z) * tau)
        return tot

    lo, hi = -0.5, 0.5
    flo, fhi = pv(lo) - dirty_target, pv(hi) - dirty_target
    if flo * fhi > 0:
        return None
    for _ in range(80):
        m = (lo + hi) / 2
        fm = pv(m) - dirty_target
        if abs(fm) < 1e-7:
            return round(m * 10000)
        if flo * fm < 0:
            hi = m
        else:
            lo, flo = m, fm
    return round((lo + hi) / 2 * 10000)


def solve_z_moex(exp: ExpCurve, cfs, calc_date: date, dirty_target: float) -> Optional[int]:
    """z-спред RUONIA-флоатера по ОФИЦИАЛЬНОЙ методике MOEX (met_rub):

        P+A = Σ CF_i / (1 + R_i + Z)^(t_i/B),  B=365

    где R_i — безрисковая zero-ставка RUONIA-КРИВОЙ на срок t_i (годовой
    компаундинг), Z — искомый спред. Отличие от solve_z_bps (наш старый путь):
    дисконт по САМОЙ RUONIA-кривой (не по КБД ОФЗ) и ДИСКРЕТНЫЙ годовой
    компаундинг (не непрерывный exp). R_i берём из DF нашего bootstrap RUONIA:
    R_i = DF(t_i)^(-365/t_i) − 1."""
    curve = getattr(exp, "_curve", None)
    if curve is None or not cfs:
        return None

    def zero(pay: date, t_days: int) -> float:
        df = curve.df(pay)
        if df <= 0:
            return 0.0
        return df ** (-365.0 / t_days) - 1.0

    def pv(z: float) -> float:
        tot = 0.0
        for pay, amt in cfs:
            t = (pay - calc_date).days
            if t <= 0:
                continue
            base = 1.0 + zero(pay, t) + z
            if base <= 0:
                return float("inf")
            tot += amt / base ** (t / 365.0)
        return tot

    lo, hi = -0.5, 0.5
    flo, fhi = pv(lo) - dirty_target, pv(hi) - dirty_target
    if flo != flo or fhi != fhi or flo * fhi > 0:
        return None
    for _ in range(90):
        m = (lo + hi) / 2
        fm = pv(m) - dirty_target
        if abs(fm) < 1e-7:
            return round(m * 10000)
        if flo * fm < 0:
            hi = m
        else:
            lo, flo = m, fm
    return round((lo + hi) / 2 * 10000)


def solve_flat_y(cfs, calc_date: date, dirty_target: float) -> Optional[float]:
    """Плоская непрерывная доходность y: Σ CF·exp(−y·τ) = dirty."""
    if not cfs:
        return None

    def pv(y):
        return sum(a * math.exp(-y * (p - calc_date).days / 365.0)
                   for p, a in cfs if (p - calc_date).days > 0)

    lo, hi = -0.5, 5.0
    if (pv(lo) - dirty_target) * (pv(hi) - dirty_target) > 0:
        return None
    for _ in range(100):
        m = (lo + hi) / 2
        if (pv(lo) - dirty_target) * (pv(m) - dirty_target) <= 0:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2


def current_period_len(coupons: list, calc_date: date) -> float:
    """Длина текущего (или ближайшего будущего) купонного периода в годах —
    горизонт рефиксинга флоатера (конвенция duration НРД)."""
    best = None
    for c in coupons or []:
        s, e = _d(c.get("start")), _d(c.get("end"))
        if not s or not e:
            continue
        if s <= calc_date < e:
            return (e - s).days / 365.0
        if e > calc_date and (best is None or e < best[1]):
            best = (s, e)
    return (best[1] - best[0]).days / 365.0 if best else 0.25


def compute_z_bps(ref, exp: ExpCurve, g: GCurve, calc_date: date,
                  price_pct: float, accrued_rub: float, coupons: list,
                  amorts: list = None, offers: list = None) -> Optional[int]:
    """Высокоуровнево: наш z-спред для флоатера (bps), сопоставимый с НРД z_spread.
    RUONIA — п.4.9 (z по кривой КБД); KEYRATE — (e^y − 1) − G(τ_reset), см. докстринг модуля.
    Для амортизируемых бумаг ref.face_value = остаточный номинал на calc_date
    (цена в % котируется от него), amorts — график погашения принципала."""
    if ref.base not in ("RUONIA", "KEYRATE") or price_pct is None:
        return None
    dirty = ref.face_value * price_pct / 100.0 + (accrued_rub or 0.0)
    cfs = project_cfs(ref, exp, calc_date, coupons, amorts, offers)
    if ref.base == "KEYRATE":
        if not g.ok():
            return None
        y = solve_flat_y(cfs, calc_date, dirty)
        if y is None:
            return None
        tau = current_period_len(coupons, calc_date)
        return round((math.exp(y) - 1 - g.r(tau)) * 10000)
    # RUONIA — официальная методика MOEX (дисконт по RUONIA-кривой, годовой комп.)
    return solve_z_moex(exp, cfs, calc_date, dirty)
