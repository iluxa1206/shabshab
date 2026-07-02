"""
Изоляционный стенд сверки DM с НРД.
Подаём в наш солвер САМУ цену НРД (wa_price) → расхождение = чисто наша математика.
Варианты дисконтирования:
  A) current  — наш код как есть (daily/quarterly forward-comp)
  B) fixed-cpn — текущий/зафиксированный купон берём из MOEX value (BUG-1 fix)
  C) annual   — B + дисконт годовым (1+y+dm)^(-t) по zero-курсу (BUG-2, конвенция НРД)
"""
import asyncio, json, math
from datetime import date

from rates import get_rates_curves
from forwards import CurveBootstrapper
from services.bonds import create_bond_ref_data
from services.market_data import MarketDataService
from valuation import (
    BondRefData, dirty_price_rub, build_cashflows_with_spread, Cashflow,
    pv_cashflows_with_dm, solve_dm_bps,
)

CACHE = json.load(open("isins_cache.json"))
NRD = json.load(open("nrd_cache.json")).get("isins", {})


def solve_dm_annual(bond, curve, cfs, calc_date, dirty_target):
    """DM годовым компаундированием на zero-курсе: df(t)=(1+y(t)+dm)^(-t)."""
    def pv(dm):
        dmf = dm / 10000.0
        tot = 0.0
        for cf in cfs:
            if cf.pay_date <= calc_date:
                continue
            t = (cf.pay_date - calc_date).days / 365.0
            if t <= 0:
                continue
            dfc = curve.df(cf.pay_date)          # базовый DF кривой
            if dfc <= 0:
                continue
            y = dfc ** (-1.0 / t) - 1.0          # annual-comp zero rate
            base = 1.0 + y + dmf
            if base <= 0:
                return float("inf")
            tot += cf.amount_rub * base ** (-t)
        return tot
    lo, hi = -5000, 20000
    flo, fhi = pv(lo) - dirty_target, pv(hi) - dirty_target
    if flo * fhi > 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        fm = pv(mid) - dirty_target
        if abs(fm) < 1e-9:
            return round(mid)
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return round((lo + hi) / 2)


def build_cfs_fixed(bond, curve, calc_date, sched_full):
    """CF как build_cashflows_with_spread, но фиксированные/текущие купоны берём
    из MOEX value (BUG-1). Будущие неизвестные — прогноз forward+spread."""
    sp = (bond.spread_issue_bps or 0) / 10000.0
    cfs = []
    for c in sched_full.get("coupons", []):
        end = c.get("end")
        end = date.fromisoformat(end) if end else None
        if not end or end <= calc_date:
            continue
        start = c.get("start")
        start = date.fromisoformat(start) if start else end
        face = bond.face_value
        days = (end - start).days or 1
        alpha = days / 365.0
        val = c.get("value")
        if val is not None:                      # зафиксированный купон — факт
            amount = float(val)
        else:                                    # будущий — прогноз
            fstart = max(start, calc_date)       # НЕ форвардим прошлое
            f = curve.forward(fstart, end) if fstart < end else 0.0
            r = f + sp
            if bond.base == "RUONIA":
                amount = face * ((1 + r / 365.0) ** days - 1)
            else:
                amount = face * ((1 + r / 4.0) ** (4 * alpha) - 1)
        cfs.append(Cashflow(pay_date=end, amount_rub=amount, type="COUPON"))
    if bond.maturity_date and bond.maturity_date > calc_date:
        cfs.append(Cashflow(pay_date=bond.maturity_date, amount_rub=bond.face_value, type="REDEMPTION"))
    cfs.sort(key=lambda x: x.pay_date)
    return cfs


async def main():
    ois, irs = get_rates_curves(use_cache=True)
    cd = date.today()
    ruonia = CurveBootstrapper.bootstrap_ruonia(ois, cd)
    keyrate = CurveBootstrapper.bootstrap_keyrate(irs, cd)

    isins = [i for i in CACHE if NRD.get(i, {}).get("discount_margin_bps") is not None
             and NRD.get(i, {}).get("nrd_price_pct")]
    snap = await MarketDataService.fetch_moex_snapshot(isins)
    scheds = await MarketDataService.fetch_coupon_schedules(isins)
    # fetch_coupon_schedules теперь отдаёт тройки (start, end, value) — тут нужны пары
    scheds = {i: [(s, e) for (s, e, _v) in v] for i, v in scheds.items()}

    print(f"{'ISIN':14} {'base':7} {'price':>7} {'NRD_dm':>7} "
          f"{'A_cur':>7} {'B_fix':>7} {'C_ann':>7}  {'dA':>6} {'dB':>6} {'dC':>6}")
    rows = []
    for isin in isins:
        nm = NRD[isin]
        ref = create_bond_ref_data(CACHE[isin], isin)
        if ref.base not in ("RUONIA", "KEYRATE"):
            continue
        curve = ruonia if ref.base == "RUONIA" else keyrate
        price = nm["nrd_price_pct"]
        nrd_dm = nm["discount_margin_bps"]
        acc = snap.get(isin, {}).get("accrued")
        if acc is not None:
            ref.accrued_rub = acc
        dirty = dirty_price_rub(ref.face_value, price, ref.accrued_rub)
        periods = scheds.get(isin)
        sched_full = await MarketDataService.fetch_bond_schedule_full(isin)

        # A: текущий код
        try:
            cfs_a = build_cashflows_with_spread(ref, curve, cd, ref.spread_issue_bps, explicit_periods=periods)
            dm_a = solve_dm_bps(ref, curve, cfs_a, cd, dirty)
        except Exception:
            dm_a = None
        # B: фикс-купон + наш форвард-дисконт
        try:
            cfs_b = build_cfs_fixed(ref, curve, cd, sched_full)
            dm_b = solve_dm_bps(ref, curve, cfs_b, cd, dirty)
        except Exception:
            dm_b = None
        # C: фикс-купон + годовой дисконт
        try:
            dm_c = solve_dm_annual(ref, curve, cfs_b, cd, dirty)
        except Exception:
            dm_c = None

        def d(x):
            return (x - nrd_dm) if x is not None else None
        rows.append((d(dm_a), d(dm_b), d(dm_c)))
        f = lambda x: f"{x:>7}" if x is not None else f"{'—':>7}"
        g = lambda x: f"{x:>6}" if x is not None else f"{'—':>6}"
        print(f"{isin:14} {ref.base:7} {price:>7.2f} {nrd_dm:>7} "
              f"{f(dm_a)} {f(dm_b)} {f(dm_c)}  {g(d(dm_a))} {g(d(dm_b))} {g(d(dm_c))}")

    def stats(idx):
        vals = [abs(r[idx]) for r in rows if r[idx] is not None]
        return (sum(vals) / len(vals), max(vals), len(vals)) if vals else (0, 0, 0)
    print("\nСредн|Δ| к НРД (bps):")
    for i, name in enumerate(["A current", "B fixed-cpn", "C annual"]):
        m, mx, n = stats(i)
        print(f"  {name:14} mean={m:7.1f}  max={mx:7.0f}  n={n}")


asyncio.run(main())
