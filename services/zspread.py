"""
z-спред над КБД ОФЗ (методика НРД) — наш расчётный аналог.

Пайплайн: прогноз купонов на кривой ОЖИДАНИЙ индекса (кубич.сплайн из свопов) →
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


def _natural_cubic_spline(xs, ys):
    n = len(xs)
    if n < 2:
        y0 = ys[0] if ys else 0.0
        return lambda x: y0
    h = [xs[i+1]-xs[i] for i in range(n-1)]
    a = [0.0]*n
    for i in range(1, n-1):
        a[i] = 3*((ys[i+1]-ys[i])/h[i] - (ys[i]-ys[i-1])/h[i-1])
    l = [1.0]+[0.0]*(n-1); mu = [0.0]*n; z = [0.0]*n
    for i in range(1, n-1):
        l[i] = 2*(xs[i+1]-xs[i-1])-h[i-1]*mu[i-1]; mu[i] = h[i]/l[i]
        z[i] = (a[i]-h[i-1]*z[i-1])/l[i]
    c = [0.0]*n; b = [0.0]*n; d = [0.0]*n
    for j in range(n-2, -1, -1):
        c[j] = z[j]-mu[j]*c[j+1]
        b[j] = (ys[j+1]-ys[j])/h[j]-h[j]*(c[j+1]+2*c[j])/3
        d[j] = (c[j+1]-c[j])/(3*h[j])

    def ev(x):
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        i = 0
        while i < n-1 and xs[i+1] < x:
            i += 1
        dx = x-xs[i]
        return ys[i]+b[i]*dx+c[i]*dx*dx+d[i]*dx**3
    return ev


class ExpCurve:
    """Кривая ожиданий индекса: spot сплайном из par-свопов. base='KEYRATE' →
    par-своп quarterly-nominal переводим в effective annual."""
    def __init__(self, calc_date: date, quotes, base: str = "RUONIA"):
        def conv(v):
            r = v/100.0
            return (1+r/4)**4 - 1 if base == "KEYRATE" else r
        pts = sorted((TENOR_Y[q.tenor.upper()], conv(q.value))
                     for q in quotes if q.tenor.upper() in TENOR_Y)
        self.calc_date = calc_date
        self.xs = [p[0] for p in pts]; self.ys = [p[1] for p in pts]
        self.spot = _natural_cubic_spline(self.xs, self.ys)

    def t(self, d: date) -> float:
        return (d - self.calc_date).days / 365.0

    def fwd(self, d1: date, d2: date) -> float:
        t1, t2 = self.t(d1), self.t(d2)
        if t2 <= t1:
            return self.spot(max(t2, 0.0))
        D1 = (1+self.spot(t1))**(-t1) if t1 > 0 else 1.0
        D2 = (1+self.spot(t2))**(-t2)
        return (D1/D2)**(1/(t2-t1)) - 1


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


def project_cfs(ref, exp: ExpCurve, calc_date: date, coupons: list, amorts: list = None):
    """Прогноз потоков: зафикс.купон = факт MOEX value; будущий = индекс(exp.fwd)+margin.
    KEYRATE простой (КС+m)·days/365; RUONIA daily-comp. + погашение номинала.

    amorts — MOEX bondization amortizations [{date, value}]. Если есть погашение
    принципала до maturity (амортизируемая бумага), принципал платится по датам
    амортизаций, а будущие купоны начисляются от остаточного номинала = сумма
    амортизаций после начала периода (поле face строк купонов MOEX ненадёжно:
    для будущих периодов не проецируется, бывает стейл). Иначе — прежний
    bullet-путь (номинал целиком на maturity_date)."""
    sp = (ref.spread_issue_bps or 0) / 10000.0
    future_am = sorted(
        (d, float(a["value"])) for a in amorts or []
        if a.get("value") is not None and (d := _d(a.get("date"))) and d > calc_date
    )
    amortizing = any(ref.maturity_date and d < ref.maturity_date for d, _ in future_am)
    cfs = []
    for c in coupons or []:
        end = _d(c.get("end"))
        if not end or end <= calc_date or (ref.maturity_date and end > ref.maturity_date):
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
                face = sum(v for d, v in future_am if d > start) or face
            f = exp.fwd(max(start, calc_date), end)
            r = f + sp
            if ref.base == "RUONIA":
                amt = face * ((1 + r/365.0)**days - 1)
            else:
                amt = face * r * alpha
        cfs.append((end, amt))
    if amortizing:
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
                  amorts: list = None) -> Optional[int]:
    """Высокоуровнево: наш z-спред для флоатера (bps), сопоставимый с НРД z_spread.
    RUONIA — п.4.9 (z по кривой КБД); KEYRATE — (e^y − 1) − G(τ_reset), см. докстринг модуля.
    Для амортизируемых бумаг ref.face_value = остаточный номинал на calc_date
    (цена в % котируется от него), amorts — график погашения принципала."""
    if ref.base not in ("RUONIA", "KEYRATE") or price_pct is None:
        return None
    dirty = ref.face_value * price_pct / 100.0 + (accrued_rub or 0.0)
    cfs = project_cfs(ref, exp, calc_date, coupons, amorts)
    if ref.base == "KEYRATE":
        if not g.ok():
            return None
        y = solve_flat_y(cfs, calc_date, dirty)
        if y is None:
            return None
        tau = current_period_len(coupons, calc_date)
        return round((math.exp(y) - 1 - g.r(tau)) * 10000)
    return solve_z_bps(g, cfs, calc_date, dirty)
