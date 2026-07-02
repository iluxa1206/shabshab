"""
Проба КС-кривой по методике НРД (Прил.3): кубич.сплайн spot из свопов IRS KeyRate,
годовой дисконт D(t)=(1+r)^(-t). Вариант D в сверке для КЕЙРЕЙТ-бумаг.
Сравниваем средн|Δ| к НРД discount_margin с текущим кодом (A).
"""
import json
from datetime import date

from rates import get_rates_curves
from forwards import CurveBootstrapper
from services.bonds import create_bond_ref_data
from valuation import BondRefData, dirty_price_rub, build_cashflows_with_spread, solve_dm_bps

CACHE = json.load(open("isins_cache.json"))
NRD = json.load(open("nrd_cache.json")).get("isins", {})

TENOR_YEARS = {"3M": 0.25, "6M": 0.5, "9M": 0.75, "1Y": 1, "2Y": 2, "3Y": 3,
               "4Y": 4, "5Y": 5, "6Y": 6, "7Y": 7, "8Y": 8, "9Y": 9, "10Y": 10}
OMEGA_KS = 0.075  # долгосрочный прогноз ЦБ (нейтральная ставка ~7.5%); влияет только >10Y


def natural_cubic_spline(xs, ys):
    """Натуральный кубич.сплайн. Возвращает eval(x) с C2-непрерывностью через узлы."""
    n = len(xs)
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    alpha = [0.0] * n
    for i in range(1, n - 1):
        alpha[i] = 3 * ((ys[i + 1] - ys[i]) / h[i] - (ys[i] - ys[i - 1]) / h[i - 1])
    l = [1.0] + [0.0] * (n - 1)
    mu = [0.0] * n
    z = [0.0] * n
    for i in range(1, n - 1):
        l[i] = 2 * (xs[i + 1] - xs[i - 1]) - h[i - 1] * mu[i - 1]
        mu[i] = h[i] / l[i]
        z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / l[i]
    l[n - 1] = 1.0
    c = [0.0] * n
    b = [0.0] * n
    d = [0.0] * n
    for j in range(n - 2, -1, -1):
        c[j] = z[j] - mu[j] * c[j + 1]
        b[j] = (ys[j + 1] - ys[j]) / h[j] - h[j] * (c[j + 1] + 2 * c[j]) / 3
        d[j] = (c[j + 1] - c[j]) / (3 * h[j])

    def ev(x):
        if x <= xs[0]:
            return ys[0]                      # короткий конец — плоско
        if x >= xs[-1]:                        # экспон. экстраполяция к OMEGA
            last = ys[-1]
            k = 0.5
            return OMEGA_KS + (last - OMEGA_KS) * pow(2.718281828, -k * (x - xs[-1]))
        i = 0
        while i < n - 1 and xs[i + 1] < x:
            i += 1
        dx = x - xs[i]
        return ys[i] + b[i] * dx + c[i] * dx * dx + d[i] * dx ** 3
    return ev


class KSCurve:
    """КС-кривая: spot r(t) сплайном, годовой DF=(1+r)^(-t)."""
    def __init__(self, calc_date, quotes):
        self.calc_date = calc_date
        pts = sorted(((TENOR_YEARS[q.tenor.upper()], q.value / 100.0)
                      for q in quotes if q.tenor.upper() in TENOR_YEARS), key=lambda p: p[0])
        self.xs = [p[0] for p in pts]
        self.ys = [p[1] for p in pts]
        self.spot = natural_cubic_spline(self.xs, self.ys)

    def t(self, d):
        return (d - self.calc_date).days / 365.0

    def df(self, d):
        tt = self.t(d)
        if tt <= 0:
            return 1.0
        return (1.0 + self.spot(tt)) ** (-tt)

    def fwd_annual(self, d1, d2):
        """Годовой форвард между датами: (D1/D2)^(1/dt)-1."""
        t1, t2 = self.t(d1), self.t(d2)
        if t2 <= t1:
            return self.spot(max(t2, 0.0))
        D1 = (1.0 + self.spot(t1)) ** (-t1) if t1 > 0 else 1.0
        D2 = (1.0 + self.spot(t2)) ** (-t2)
        return (D1 / D2) ** (1.0 / (t2 - t1)) - 1.0


import math as _math


def solve_dm_ks(ref, curve, cfs, calc_date, dirty):
    """DM непрерывным дисконтом (конвенция НРД 4.9): df=exp(-(r(t)+dm)*t)."""
    def pv(dm):
        dmf = dm / 10000.0
        tot = 0.0
        for pay, amt in cfs:
            tt = curve.t(pay)
            if tt <= 0:
                continue
            tot += amt * _math.exp(-(curve.spot(tt) + dmf) * tt)
        return tot
    lo, hi = -5000, 20000
    flo, fhi = pv(lo) - dirty, pv(hi) - dirty
    if flo * fhi > 0:
        return None
    for _ in range(64):
        mid = (lo + hi) / 2
        fm = pv(mid) - dirty
        if abs(fm) < 1e-9:
            return round(mid)
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return round((lo + hi) / 2)


def build_cfs_ks(ref, curve, calc_date, periods, coupon_mode):
    """CF для КС-флоатера. coupon_mode: 'simple' | 'quarterly'.
    Купон = f(fwd+spread) на реальном расписании. Будущие только (end>calc_date)."""
    sp = (ref.spread_issue_bps or 0) / 10000.0
    cfs = []
    for (start, end) in (periods or []):
        if end <= calc_date or end > ref.maturity_date:
            continue
        days = (end - start).days or 1
        fstart = max(start, calc_date)
        f = curve.fwd_annual(fstart, end)
        r = f + sp
        if coupon_mode == "simple":
            amt = ref.face_value * r * days / 365.0
        else:
            alpha = days / 365.0
            amt = ref.face_value * ((1 + r / 4.0) ** (4 * alpha) - 1)
        cfs.append((end, amt))
    if ref.maturity_date > calc_date:
        cfs.append((ref.maturity_date, ref.face_value))
    cfs.sort(key=lambda x: x[0])
    return cfs


def main():
    import asyncio
    from services.market_data import MarketDataService

    async def run():
        ois, irs = get_rates_curves(use_cache=True)
        cd = date.today()
        keyrate_old = CurveBootstrapper.bootstrap_keyrate(irs, cd)
        ks = KSCurve(cd, irs)

        isins = [i for i in CACHE if NRD.get(i, {}).get("discount_margin_bps") is not None
                 and NRD.get(i, {}).get("nrd_price_pct")
                 and NRD[i].get("base_coupon_index") in ("CBRATED", "KEYRATE")]
        snap = await MarketDataService.fetch_moex_snapshot(isins)
        scheds = await MarketDataService.fetch_coupon_schedules(isins)
        # fetch_coupon_schedules теперь отдаёт тройки (start, end, value) — тут нужны пары
        scheds = {i: [(s, e) for (s, e, _v) in v] for i, v in scheds.items()}

        print(f"{'ISIN':14} {'price':>7} {'NRD_dm':>7} {'A_old':>7} {'D_smpl':>7} {'D_quar':>7}  "
              f"{'dA':>6} {'dDs':>6} {'dDq':>6}")
        rows = []
        for isin in isins:
            nm = NRD[isin]
            ref = create_bond_ref_data(CACHE[isin], isin)
            if ref.base != "KEYRATE":
                continue
            price = nm["nrd_price_pct"]
            nrd_dm = nm["discount_margin_bps"]
            acc = snap.get(isin, {}).get("accrued")
            if acc is not None:
                ref.accrued_rub = acc
            dirty = dirty_price_rub(ref.face_value, price, ref.accrued_rub)
            periods = scheds.get(isin)

            try:
                cfs_a = build_cashflows_with_spread(ref, keyrate_old, cd, ref.spread_issue_bps, explicit_periods=periods)
                dm_a = solve_dm_bps(ref, keyrate_old, cfs_a, cd, dirty)
            except Exception:
                dm_a = None
            cfs_s = build_cfs_ks(ref, ks, cd, periods, "simple")
            cfs_q = build_cfs_ks(ref, ks, cd, periods, "quarterly")
            dm_s = solve_dm_ks(ref, ks, cfs_s, cd, dirty)
            dm_q = solve_dm_ks(ref, ks, cfs_q, cd, dirty)

            dd = lambda x: (x - nrd_dm) if x is not None else None
            rows.append((dd(dm_a), dd(dm_s), dd(dm_q)))
            f = lambda x: f"{x:>7}" if x is not None else f"{'—':>7}"
            g = lambda x: f"{x:>6}" if x is not None else f"{'—':>6}"
            print(f"{isin:14} {price:>7.2f} {nrd_dm:>7} {f(dm_a)} {f(dm_s)} {f(dm_q)}  "
                  f"{g(dd(dm_a))} {g(dd(dm_s))} {g(dd(dm_q))}")

        def stats(idx, drop_outlier=True):
            vals = [abs(r[idx]) for r in rows if r[idx] is not None]
            if drop_outlier and vals:
                vals = sorted(vals)[:-1]  # убрать 1 макс. выброс
            return (sum(vals) / len(vals), max(vals), len(vals)) if vals else (0, 0, 0)
        print("\nСредн|Δ| к НРД (bps), без 1 макс.выброса:")
        for i, name in enumerate(["A old-curve", "D spline simple", "D spline quarterly"]):
            m, mx, n = stats(i)
            print(f"  {name:20} mean={m:7.1f}  max={mx:7.0f}  n={n}")

    asyncio.run(run())


main()
