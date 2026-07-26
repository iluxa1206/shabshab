"""РАЗОВЫЙ стенд: матрица гипотез о причине расхождения нашего DM с НРД.

Данные: СВЕЖИЕ сырые строки valuationnewadd (wa_price/ytm/dm на val_date),
MOEX расписания/НКД, наши кривые. Гипотезы:

  DM-варианты (наш солвер, что меняем):
    A  current    — прод-движок после фикса #1/#3 (recursive comp)
    B  cont       — дисконт df(τ)·exp(−dm·τ) (непрерывный add-on, конвенция НРД п.4.9)
    C  annual     — дисконт df(τ)·(1+dm)^(−τ) (годовой add-on)
    D  simple-cpn — KEYRATE купоны simple face·r·α (как z-движок) + дисконт A
    E  cd=val_date— как A, но calc_date = НРД val_date (вчера)

  Разложение Δ (главный инструмент):
    y_our (flat cont → eff) vs ytm_НРД  → CF/yield-часть расхождения
    b_НРД = ytm_НРД − dm_НРД («имплайд-база» НРД, %) → фит к кандидатам:
      IRS_eff(0.25/1.0/T), avgFwd(life), G(0.25)
    residual-корреляции: Δdm vs duration бумаги, vs (100−P).
"""
import asyncio, json, math
from datetime import date
from dataclasses import replace

import httpx

from rates import get_rates_curves, tenor_to_days
from forwards import CurveBootstrapper
from services.bonds import build_ref_external
from services.market_data import MarketDataService
from services import nrd
from services.zspread import solve_flat_y
from valuation import dirty_price_rub, build_cashflows_with_spread, solve_dm_bps

UNI = {u["isin"]: u for u in json.load(open("nrd_universe_cache.json")).get("items")}
N = 40  # бумаг в выборке


def _pd(s):
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def solve_dm_addon(cfs, curve, cd, dirty, mode):
    """DM add-on к DF кривой: mode='cont' → df·e^{−dm·τ}; 'annual' → df·(1+dm)^{−τ}."""
    tups = [(cf.pay_date, cf.amount_rub) for cf in cfs if cf.pay_date > cd]
    if not tups:
        return None

    def pv(dm):
        tot = 0.0
        for d, a in tups:
            tau = (d - cd).days / 365.0
            dfc = curve.df(d)
            if dfc <= 0:
                continue
            if mode == "cont":
                tot += a * dfc * math.exp(-dm * tau)
            else:
                base = 1.0 + dm
                if base <= 0:
                    return float("inf")
                tot += a * dfc * base ** (-tau)
        return tot

    lo, hi = -0.5, 2.0
    flo, fhi = pv(lo) - dirty, pv(hi) - dirty
    if flo * fhi > 0:
        return None
    for _ in range(80):
        m = (lo + hi) / 2
        fm = pv(m) - dirty
        if abs(fm) < 1e-8:
            break
        if flo * fm < 0:
            hi = m
        else:
            lo, flo = m, fm
    return round((lo + hi) / 2 * 10000)


def build_cfs_simple_keyrate(ref, curve, cd, triples, amorts):
    """KEYRATE купоны simple face·(f+sp)·α (как z-движок project_cfs), факт value если есть."""
    sp = (ref.spread_issue_bps or 0) / 10000.0
    fam = sorted((d, float(a["value"])) for a in (amorts or [])
                 if a.get("value") is not None and (d := _pd(a.get("date"))) and d > cd)
    amortizing = any(ref.maturity_date and d < ref.maturity_date for d, _ in fam)
    from valuation import Cashflow
    cfs = []
    for s, e, v in triples:
        if not e or e <= cd or (ref.maturity_date and e > ref.maturity_date):
            continue
        if v is not None:
            amt = float(v)
        else:
            face = ref.face_value
            if amortizing:
                face = sum(x for d, x in fam if d > s) or face
            fs = max(s, cd)
            f = curve.forward(fs, e) if fs < e else 0.0
            amt = face * (f + sp) * ((e - s).days / 365.0)
        cfs.append(Cashflow(pay_date=e, amount_rub=amt, type="COUPON"))
    if amortizing:
        cfs += [Cashflow(pay_date=d, amount_rub=v, type="REDEMPTION") for d, v in fam]
    elif ref.maturity_date and ref.maturity_date > cd:
        cfs.append(Cashflow(pay_date=ref.maturity_date, amount_rub=ref.face_value, type="REDEMPTION"))
    cfs.sort(key=lambda c: c.pay_date)
    return cfs


async def main():
    ois, irs = get_rates_curves(use_cache=True)
    cd = date.today()
    ru_c = CurveBootstrapper.bootstrap_ruonia(ois, cd)
    kr_c = CurveBootstrapper.bootstrap_keyrate(irs, cd)
    exp_ks, exp_ru, g = await MarketDataService.get_zspread_ctx()

    # выборка: KEYRATE + RUONIA с dm/price
    isins = [i for i, u in UNI.items()
             if u.get("discount_margin_bps") is not None and u.get("nrd_price_pct")
             and u.get("base_rate_type") in ("KEYRATE", "RUONIA")]
    kk = [i for i in isins if UNI[i]["base_rate_type"] == "KEYRATE"][:N - 12]
    rr = [i for i in isins if UNI[i]["base_rate_type"] == "RUONIA"][:12]
    sample = kk + rr

    # СВЕЖИЕ сырые строки НРД (wa_price/ytm/dm консистентны на val_date)
    async with httpx.AsyncClient(timeout=60) as client:
        raw = await nrd._fetch_method(client, nrd.PATH_VALUATION_ADD, sample)

    secs = await MarketDataService.fetch_moex_securities(sample)
    fulls = dict(zip(sample, await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(i) for i in sample))))
    snap = await MarketDataService.fetch_moex_snapshot(sample)

    rows = []
    for isin in sample:
        r = raw.get(isin) or {}
        u = UNI[isin]
        dm_nrd = r.get("discount_margin")
        price = r.get("wa_price")
        ytm = r.get("yield_maturity")
        vdate = _pd(r.get("val_date"))
        if dm_nrd is None or price is None or ytm is None:
            continue
        dm_nrd_bps = round(float(dm_nrd) * 100)
        ytm_pct = float(ytm) * 100.0
        price = float(price)

        bidx = "CBRATED" if u["base_rate_type"] == "KEYRATE" else "RUONIARATED"
        ref = build_ref_external(isin, secs.get(isin, {}),
                                 {"base_coupon_index": bidx, "nominal_margin_bps": u.get("spread_issue_bps") or 0})
        if not ref.maturity_date:
            continue
        curve = ru_c if ref.base == "RUONIA" else kr_c
        exp = exp_ru if ref.base == "RUONIA" else exp_ks
        full = fulls.get(isin) or {}
        trip = [(_pd(c["start"]), _pd(c["end"]), c.get("value"))
                for c in (full.get("coupons") or []) if c.get("start") and c.get("end")]
        if not trip:
            continue
        amorts = full.get("amorts")
        acc = snap.get(isin, {}).get("accrued")
        if acc is not None:
            ref.accrued_rub = acc
        dirty = dirty_price_rub(ref.face_value, price, ref.accrued_rub)

        try:
            cfs = build_cashflows_with_spread(ref, curve, cd, ref.spread_issue_bps,
                                              explicit_periods=trip, amorts=amorts)
        except Exception:
            continue
        dm_a = solve_dm_bps(ref, curve, cfs, cd, dirty)
        dm_b = solve_dm_addon(cfs, curve, cd, dirty, "cont")
        dm_c = solve_dm_addon(cfs, curve, cd, dirty, "annual")
        dm_d = None
        if ref.base == "KEYRATE":
            try:
                cfs_d = build_cfs_simple_keyrate(ref, curve, cd, trip, amorts)
                dm_d = solve_dm_bps(ref, curve, cfs_d, cd, dirty)
            except Exception:
                pass
        dm_e = None
        if vdate and vdate != cd:
            try:
                cfs_e = build_cashflows_with_spread(ref, curve, vdate, ref.spread_issue_bps,
                                                    explicit_periods=trip, amorts=amorts)
                dm_e = solve_dm_bps(ref, curve, cfs_e, vdate, dirty)
            except Exception:
                pass

        # наш flat-yield (eff) на тех же CF → разложение
        tups = [(c.pay_date, c.amount_rub) for c in cfs]
        fy = solve_flat_y(tups, cd, dirty)
        y_our_eff = (math.exp(fy) - 1) * 100.0 if fy is not None else None

        T = (ref.maturity_date - cd).days / 365.0
        b_nrd = ytm_pct - dm_nrd_bps / 100.0  # имплайд-база НРД, %

        # кандидаты базы (в %, effective для KEYRATE через ExpCurve)
        cands = {}
        try:
            cands["exp(0.25)"] = exp.spot(0.25) * 100
            cands["exp(1.0)"] = exp.spot(1.0) * 100
            cands["exp(T)"] = exp.spot(T) * 100
            # средний форвард по жизни (месячная сетка)
            steps = max(int(T * 12), 1)
            avg = sum(exp.spot((k + 0.5) * T / steps) for k in range(steps)) / steps
            cands["avgSpot(life)"] = avg * 100
        except Exception:
            pass
        try:
            cands["G(0.25)"] = g.r(0.25) * 100
        except Exception:
            pass

        rows.append(dict(isin=isin, base=ref.base, T=T, price=price, dm_nrd=dm_nrd_bps,
                         ytm_nrd=ytm_pct, y_our=y_our_eff, b_nrd=b_nrd,
                         dm_a=dm_a, dm_b=dm_b, dm_c=dm_c, dm_d=dm_d, dm_e=dm_e,
                         cands=cands, dur_nrd=r.get("duration")))

    # ── таблица per-bond (кратко) ──
    print(f"{'ISIN':14} {'bs':2} {'T':>4} {'price':>7} {'NRDdm':>6} {'A':>6} {'B':>6} {'C':>6} {'D':>6} {'E':>6}  {'ytmN':>6} {'yOur':>6} {'bN':>6}")
    for r in rows:
        f = lambda x, w=6: (f"{x:>{w}}" if isinstance(x, int) else (f"{x:>{w}.2f}" if isinstance(x, float) else f"{'—':>{w}}"))
        print(f"{r['isin']:14} {r['base'][:2]:2} {r['T']:>4.1f} {r['price']:>7.2f} {r['dm_nrd']:>6} "
              f"{f(r['dm_a'])} {f(r['dm_b'])} {f(r['dm_c'])} {f(r['dm_d'])} {f(r['dm_e'])}  "
              f"{f(r['ytm_nrd'])} {f(r['y_our'])} {f(r['b_nrd'])}")

    # ── агрегаты по вариантам ──
    def agg(key, base=None):
        ds = [(r[key] - r["dm_nrd"]) for r in rows
              if r[key] is not None and (base is None or r["base"] == base)]
        if not ds:
            return None
        m = sum(ds) / len(ds)
        med = sorted(ds)[len(ds) // 2]
        sd = (sum((d - m) ** 2 for d in ds) / len(ds)) ** 0.5
        ma = sum(abs(d) for d in ds) / len(ds)
        return len(ds), m, med, sd, ma

    print("\nΔ(вариант − НРДdm), bps:  n  mean  med  std  mean|Δ|")
    for key, label in [("dm_a", "A current"), ("dm_b", "B cont-addon"), ("dm_c", "C annual-addon"),
                       ("dm_d", "D simple-cpn(K)"), ("dm_e", "E cd=val_date")]:
        for b in ("KEYRATE", "RUONIA"):
            a = agg(key, b)
            if a:
                print(f"  {label:16} {b[:4]:5} n={a[0]:2} mean={a[1]:+7.1f} med={a[2]:+6.0f} std={a[3]:6.1f} m|Δ|={a[4]:6.1f}")

    # ── yield-часть vs база-часть ──
    dy = [(r["y_our"] - r["ytm_nrd"]) * 100 for r in rows if r["y_our"] is not None]
    if dy:
        m = sum(dy) / len(dy)
        print(f"\nΔyield (наш flat-eff − НРД ytm), bps: n={len(dy)} mean={m:+.1f} med={sorted(dy)[len(dy)//2]:+.0f} "
              f"std={(sum((d-m)**2 for d in dy)/len(dy))**0.5:.1f}")

    # ── фит имплайд-базы НРД к кандидатам ──
    print("\nb_НРД − кандидат (%, mean±std | n) — чья кривая у НРД под dm:")
    keys = sorted({k for r in rows for k in r["cands"]})
    for b in ("KEYRATE", "RUONIA"):
        print(f"  {b}:")
        for k in keys:
            ds = [r["b_nrd"] - r["cands"][k] for r in rows
                  if r["base"] == b and r["cands"].get(k) is not None]
            if len(ds) >= 3:
                m = sum(ds) / len(ds)
                sd = (sum((d - m) ** 2 for d in ds) / len(ds)) ** 0.5
                print(f"    {k:14} {m:+6.2f} ± {sd:5.2f}  (n={len(ds)})")

    # ── корреляции остатка A ──
    def corr(xs, ys):
        n = len(xs)
        if n < 4:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = sum((x - mx) ** 2 for x in xs) ** 0.5
        vy = sum((y - my) ** 2 for y in ys) ** 0.5
        return cov / (vx * vy) if vx > 0 and vy > 0 else None

    sub = [r for r in rows if r["dm_a"] is not None]
    d_a = [r["dm_a"] - r["dm_nrd"] for r in sub]
    print("\nКорреляции остатка (A − НРДdm):")
    print(f"  vs T (лет до погаш.):  {corr([r['T'] for r in sub], d_a):+.2f}")
    print(f"  vs (100−price):        {corr([100 - r['price'] for r in sub], d_a):+.2f}")
    print(f"  vs dm_НРД:             {corr([r['dm_nrd'] for r in sub], d_a):+.2f}")


asyncio.run(main())
