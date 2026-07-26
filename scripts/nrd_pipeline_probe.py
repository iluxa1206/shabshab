"""
Матрица NRD-style пайплайна z-спреда (met_rub п.4.9 + met_float Прил.2-4).

Оси (все комбинации за один запуск, сетевые входы грузятся один раз):
  A curve_kind: 'spline'     — прод ExpCurve (natural spline по свопам)
                'nrd_spline' — сплайн + exp-экстраполяция к ω (Прил.3)
  B rate_mode:  'fwd'        — годовой форвард из DF (текущий прод)
                'spot_start' — значение кривой на дату фиксинга (начало периода;
                               Прил.3/4 НРД: «форвард = спот»)
                'spot_end'   — значение на конец периода
                'avg'        — среднее значение кривой по периоду
  C fixing_lag_days: сдвиг даты фиксинга назад (конвенция эмитента)

Вход: цена НРД (wa_price) + as-of выравнивание на nrd_calc_date (КБД zcyc?date=,
AI пропорцией из value текущего купона) — изолирует чистую математику.
Ground truth: НРД z_spread_bps.

Запуск из корня репо: .venv/bin/python scripts/nrd_pipeline_probe.py [--csv probe.csv]
"""
import argparse
import asyncio
import csv
import json
import math
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from rates import get_rates_curves
from services.bonds import create_bond_ref_data
from services.market_data import MarketDataService
from services.zspread import ExpCurve, GCurve, compute_z_bps, solve_z_bps, TENOR_Y

OMEGA_KS = 0.075   # долгосрочный прогноз ЦБ (нейтральная КС ~7.5%)
SCHED_MEMO = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".probe_sched_cache.json")


# ---------- кривые ----------
def _spline_ext(xs, ys, omega=None, k=0.5):
    """Натуральный кубич.сплайн; за последним узлом — exp-экстраполяция к omega
    (если задана), иначе плоско. Короткий конец — плоско."""
    n = len(xs)
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
            if omega is None:
                return ys[-1]
            return omega + (ys[-1]-omega)*math.exp(-k*(x-xs[-1]))
        i = 0
        while i < n-1 and xs[i+1] < x:
            i += 1
        dx = x-xs[i]
        return ys[i]+b[i]*dx+c[i]*dx*dx+d[i]*dx**3
    return ev


class NrdSplineCurve:
    """Сплайн по свопам с exp-экстраполяцией к ω (met_float Прил.3)."""
    def __init__(self, cd, quotes, eff_conv=True, omega=OMEGA_KS, k=0.5):
        def conv(v):
            r = v/100.0
            return (1+r/4)**4 - 1 if eff_conv else r
        pts = sorted((TENOR_Y[q.tenor.upper()], conv(q.value))
                     for q in quotes if q.tenor.upper() in TENOR_Y)
        self.cd = cd
        self.xs = [p[0] for p in pts]; self.ys = [p[1] for p in pts]
        self.spot = _spline_ext(self.xs, self.ys, omega, k)

    def t(self, d): return (d-self.cd).days/365.0

    def fwd(self, d1, d2):
        t1, t2 = self.t(d1), self.t(d2)
        if t2 <= t1:
            return self.spot(max(t2, 0.0))
        D1 = (1+self.spot(t1))**(-t1) if t1 > 0 else 1.0
        D2 = (1+self.spot(t2))**(-t2)
        return (D1/D2)**(1/(t2-t1)) - 1


def make_curve(kind, cd, inputs, eff_conv):
    if kind == "spline":
        # ExpCurve применяет (1+r/4)^4-1 только при base='KEYRATE' — трюк для eff_conv=off
        return ExpCurve(cd, inputs.irs, "KEYRATE" if eff_conv else "RUONIA")
    if kind == "nrd_spline":
        return NrdSplineCurve(cd, inputs.irs, eff_conv=eff_conv)
    raise ValueError(kind)


# ---------- ставка периода / потоки ----------
def period_rate(curve, rate_mode, cd, start, end, lag_days=0):
    anchor = max(start, cd)
    if rate_mode == "fwd":
        return curve.fwd(anchor, end)
    if rate_mode == "spot_start":
        fix = max(start - timedelta(days=lag_days), cd) if lag_days else anchor
        return curve.spot(max(curve.t(fix), 0.0))
    if rate_mode == "spot_end":
        return curve.spot(curve.t(end))
    if rate_mode == "avg":
        t1, t2 = max(curve.t(anchor), 0.0), curve.t(end)
        n = 5
        return sum(curve.spot(t1+(t2-t1)*i/(n-1)) for i in range(n))/n
    raise ValueError(rate_mode)


def future_amorts(sched_full, cd):
    """Будущие амортизации (date, value), отсортированы по дате."""
    return sorted((date.fromisoformat(a["date"]), float(a["value"]))
                  for a in sched_full.get("amorts", [])
                  if a.get("date") and a.get("value") is not None
                  and date.fromisoformat(a["date"]) > cd)


def is_amortizing(future_am, maturity):
    """Есть погашение принципала до maturity."""
    return any(maturity and d < maturity for d, _ in future_am)


def project_cfs_v(ref, curve, cd, sched_full, rate_mode, lag_days=0):
    """Потоки: зафикс.купон = факт MOEX value (НЕ перезаписывать!); будущий =
    (индекс+margin), KEYRATE простой, RUONIA daily-comp. + погашение.
    Амортизируемые (принципал до maturity): принципал по датам амортизаций,
    будущие купоны от остаточного номинала (зеркало prod project_cfs)."""
    sp = (ref.spread_issue_bps or 0)/10000.0
    future_am = future_amorts(sched_full, cd)
    amortizing = is_amortizing(future_am, ref.maturity_date)
    cfs = []
    for c in sched_full.get("coupons", []):
        end = c.get("end"); end = date.fromisoformat(end) if end else None
        if not end or end <= cd or (ref.maturity_date and end > ref.maturity_date):
            continue
        start = c.get("start"); start = date.fromisoformat(start) if start else end
        days = (end-start).days or 1
        val = c.get("value")
        if val is not None:
            amt = float(val)
        else:
            face = ref.face_value
            if amortizing:
                face = sum(v for d, v in future_am if d > start) or face
            r = period_rate(curve, rate_mode, cd, start, end, lag_days) + sp
            if ref.base == "RUONIA":
                amt = face*((1+r/365.0)**days - 1)
            else:
                amt = face*r*(days/365.0)
        cfs.append((end, amt))
    if amortizing:
        cfs.extend(future_am)
    elif ref.maturity_date and ref.maturity_date > cd:
        cfs.append((ref.maturity_date, ref.face_value))
    cfs.sort()
    return cfs


def accrued_asof(sched_full, as_of, fallback):
    """AI на as_of: пропорция от value купона, чей период накрывает as_of."""
    for c in sched_full.get("coupons", []):
        s, e, v = c.get("start"), c.get("end"), c.get("value")
        if not s or not e or v is None:
            continue
        s, e = date.fromisoformat(s), date.fromisoformat(e)
        if s <= as_of < e and (e-s).days > 0:
            return float(v)*(as_of-s).days/(e-s).days
    return fallback


def current_period_len(sched_full, as_of):
    """Длина текущего (или ближайшего) купонного периода в годах — конвенция
    duration НРД для флоатеров (горизонт рефиксинга)."""
    best = None
    for c in sched_full.get("coupons", []):
        s, e = c.get("start"), c.get("end")
        if not s or not e:
            continue
        s, e = date.fromisoformat(s), date.fromisoformat(e)
        if s <= as_of < e:
            return (e-s).days/365.0
        if e > as_of and (best is None or e < best[1]):
            best = (s, e)
    return (best[1]-best[0]).days/365.0 if best else 0.25


def solve_flat_y(cfs, cd, dirty):
    """Плоская непрерывная доходность y: Σ CF·exp(−y·τ)=dirty."""
    def pv(y):
        return sum(a*math.exp(-y*(p-cd).days/365.0) for p, a in cfs if (p-cd).days > 0)
    lo, hi = -0.5, 5.0
    if (pv(lo)-dirty)*(pv(hi)-dirty) > 0:
        return None
    for _ in range(100):
        m = (lo+hi)/2
        if (pv(lo)-dirty)*(pv(m)-dirty) <= 0:
            hi = m
        else:
            lo = m
    return (lo+hi)/2


# ---------- входы ----------
@dataclass
class Inputs:
    cd: date
    nrd: dict
    refs: dict
    scheds: dict
    accrued: dict
    g: GCurve
    ois: list
    irs: list


async def fetch_gcurve(as_of=None):
    params = {"iss.meta": "off", "iss.only": "yearyields"}
    if as_of:
        params["date"] = as_of.isoformat()
    async with httpx.AsyncClient() as c:
        r = await c.get("https://iss.moex.com/iss/engines/stock/zcyc.json",
                        params=params, timeout=15)
    yy = r.json()["yearyields"]
    cols, data = yy["columns"], yy["data"]
    pi, vi = cols.index("period"), cols.index("value")
    return [(float(row[pi]), float(row[vi])) for row in data
            if row[pi] is not None and row[vi] is not None]


def _load_sched_memo():
    try:
        with open(SCHED_MEMO, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == date.today().isoformat():
            return d.get("items", {})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _save_sched_memo(items):
    try:
        with open(SCHED_MEMO, "w", encoding="utf-8") as f:
            json.dump({"date": date.today().isoformat(), "items": items}, f, ensure_ascii=False)
    except OSError:
        pass


async def load_inputs(as_of=None):
    cache = json.load(open("isins_cache.json"))
    nrd = json.load(open("nrd_cache.json")).get("isins", {})
    ids = [i for i in cache
           if nrd.get(i, {}).get("z_spread_bps") is not None
           and nrd.get(i, {}).get("nrd_price_pct")
           and nrd.get(i, {}).get("base_coupon_index") in ("CBRATED", "KEYRATE", "RUONIARATED", "RUONIA")]
    # as-of: мода nrd_calc_date по выборке (НРД считает на T-1 от кэша)
    if as_of is None:
        dates = Counter(nrd[i].get("nrd_calc_date") for i in ids if nrd[i].get("nrd_calc_date"))
        as_of = date.fromisoformat(dates.most_common(1)[0][0]) if dates else date.today()

    refs = {}
    for i in ids:
        try:
            r = create_bond_ref_data(cache[i], i)
            if r.base in ("RUONIA", "KEYRATE"):
                refs[i] = r
        except Exception:
            pass
    ids = [i for i in ids if i in refs]

    memo = _load_sched_memo()
    missing = [i for i in ids if i not in memo]
    if missing:
        scheds = await asyncio.gather(*(MarketDataService.fetch_bond_schedule_full(i) for i in missing))
        for i, s in zip(missing, scheds):
            memo[i] = s
        _save_sched_memo(memo)

    snap = await MarketDataService.fetch_moex_snapshot(ids)
    accrued = {}
    for i in ids:
        fb = snap.get(i, {}).get("accrued")
        accrued[i] = accrued_asof(memo[i], as_of, fb if fb is not None else refs[i].accrued_rub)

    ois, irs = get_rates_curves(use_cache=True)
    g = GCurve(await fetch_gcurve(as_of))
    return Inputs(as_of, {i: nrd[i] for i in ids}, refs, memo, accrued, g, ois, irs)


# ---------- варианты ----------
@dataclass(frozen=True)
class VariantSpec:
    key: str
    curve_kind: str
    eff_conv: bool
    rate_mode: str
    lag_days: int = 0
    bases: tuple = ("KEYRATE",)
    solver: str = "curve_z"     # 'curve_z' (п.4.9 по кривой) | 'ytm_minus_g' (z=y_eff−G(reset))
    g_tenor: str = "period"     # для ytm_minus_g: 'period' | 'nrd_dur'


SPECS = [
    VariantSpec("V0   spline conv fwd (прод)", "spline", True, "fwd", bases=("KEYRATE", "RUONIA")),
    VariantSpec("V0n  spline raw  fwd",         "spline", False, "fwd"),
    VariantSpec("V1a  spline conv spot_start",  "spline", True, "spot_start"),
    VariantSpec("V1e  spline conv spot_end",    "spline", True, "spot_end"),
    VariantSpec("V3f  nrdspl conv fwd",         "nrd_spline", True, "fwd"),
    VariantSpec("V7   ytm−G spline conv fwd",   "spline", True, "fwd", solver="ytm_minus_g"),
    VariantSpec("V7s  ytm−G spline conv spot",  "spline", True, "spot_start", solver="ytm_minus_g"),
    VariantSpec("V7n  ytm−G spline raw  fwd",   "spline", False, "fwd", solver="ytm_minus_g"),
    VariantSpec("V7d  ytm−G G(nrd_dur)",        "spline", True, "fwd", solver="ytm_minus_g", g_tenor="nrd_dur"),
    VariantSpec("V7r  ytm−G RUONIA (контроль)", "spline", True, "fwd", solver="ytm_minus_g", bases=("RUONIA",)),
]


@dataclass
class BondResult:
    variant: str
    isin: str
    base: str
    nrd_z: int
    our_z: object
    delta: object
    duration: object
    margin_bps: object
    rating: object
    z_minus_g: object


def run_variant(spec, inputs, curve_ru):
    curve_ks = make_curve(spec.curve_kind, inputs.cd, inputs, spec.eff_conv)
    rows = []
    for isin, ref in inputs.refs.items():
        if ref.base not in spec.bases:
            continue
        nm = inputs.nrd[isin]
        curve = curve_ru if ref.base == "RUONIA" else curve_ks
        rate_mode = "fwd" if ref.base == "RUONIA" else spec.rate_mode
        dirty = ref.face_value*nm["nrd_price_pct"]/100.0 + (inputs.accrued.get(isin) or 0.0)
        cfs = project_cfs_v(ref, curve, inputs.cd, inputs.scheds[isin],
                            rate_mode, spec.lag_days)
        if spec.solver == "ytm_minus_g":
            y = solve_flat_y(cfs, inputs.cd, dirty)
            if y is None:
                oz = None
            else:
                if spec.g_tenor == "nrd_dur" and nm.get("duration") is not None:
                    tau = nm["duration"]
                else:
                    tau = current_period_len(inputs.scheds[isin], inputs.cd)
                oz = round(((math.exp(y)-1) - inputs.g.r(tau))*10000)
        else:
            oz = solve_z_bps(inputs.g, cfs, inputs.cd, dirty)
        delta = (oz-nm["z_spread_bps"]) if oz is not None else None
        ratings = nm.get("ratings") or {}
        zg = None
        if nm.get("g_spread_bps") is not None:
            zg = nm["z_spread_bps"] - nm["g_spread_bps"]
        rows.append(BondResult(spec.key, isin, ref.base, nm["z_spread_bps"], oz, delta,
                               nm.get("duration"), ref.spread_issue_bps,
                               next(iter(ratings.values()), None), zg))
    return rows


def stats(rows, base):
    ds = [r.delta for r in rows if r.base == base and r.delta is not None]
    if not ds:
        return None
    s = sorted(ds)
    med = s[len(s)//2]
    ab = [abs(x) for x in ds]
    return dict(n=len(ds), mean_abs=sum(ab)/len(ab), median_signed=med,
                mean_signed=sum(ds)/len(ds), max_abs=max(ab))


async def load_universe_inputs(as_of=None):
    """Широкая выборка из nrd_universe_cache (456 флоатеров): pseudo-ref из полей
    юниверса, face из bondization (остаточный номинал — от него котируется цена),
    AI пропорцией. Амортизируемые считаются: принципал по графику амортизаций,
    купоны от остаточного face. as_of по умолчанию = calc_date кэша − 1 день."""
    from types import SimpleNamespace
    uni = json.load(open("nrd_universe_cache.json"))
    if as_of is None:
        as_of = date.fromisoformat(uni["calc_date"]) - timedelta(days=1)
    items = [it for it in uni["items"]
             if it.get("base_rate_type") in ("KEYRATE", "RUONIA")
             and it.get("z_spread_bps") is not None and it.get("nrd_price_pct")
             and it.get("spread_issue_bps") is not None and it.get("maturity_date")]

    memo = _load_sched_memo()
    missing = [it["isin"] for it in items if it["isin"] not in memo]
    if missing:
        print(f"bondization: догружаю {len(missing)} расписаний (memo-кэш)...")
        scheds = await asyncio.gather(*(MarketDataService.fetch_bond_schedule_full(i) for i in missing))
        for i, s in zip(missing, scheds):
            memo[i] = s
        _save_sched_memo(memo)

    refs, nrd, accrued = {}, {}, {}
    skipped = Counter()
    for it in items:
        isin = it["isin"]
        sched = memo.get(isin) or {}
        coupons = sched.get("coupons") or []
        if not coupons:
            skipped["нет расписания"] += 1
            continue
        mat = date.fromisoformat(it["maturity_date"])
        ai = accrued_asof(sched, as_of, None)
        if ai is None:
            skipped["нет зафикс. купона (AI)"] += 1
            continue
        # остаточный номинал на as_of (цена в % котируется от него): сумма будущих
        # амортизаций (авторитетно) → face строки купона → 1000
        face = (sum(v for _, v in future_amorts(sched, as_of))
                or next((c.get("face") for c in coupons
                         if c.get("end") and date.fromisoformat(c["end"]) > as_of and c.get("face")), None)
                or 1000.0)
        refs[isin] = SimpleNamespace(base=it["base_rate_type"],
                                     spread_issue_bps=it["spread_issue_bps"],
                                     face_value=float(face), maturity_date=mat,
                                     accrued_rub=ai)
        nrd[isin] = {"z_spread_bps": it["z_spread_bps"], "nrd_price_pct": it["nrd_price_pct"],
                     "duration": it.get("nrd_duration"), "g_spread_bps": None,
                     "ratings": {"": it.get("rating")}}
        accrued[isin] = ai
    print(f"universe as_of={as_of}: годных {len(refs)}, пропущено {dict(skipped)}")

    ois, irs = get_rates_curves(use_cache=True)
    g = GCurve(await fetch_gcurve(as_of))
    return Inputs(as_of, nrd, refs, memo, accrued, g, ois, irs)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", type=str, default=None)
    ap.add_argument("--csv", type=str, default=None)
    ap.add_argument("--universe", action="store_true")
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    inputs = await (load_universe_inputs(as_of) if args.universe else load_inputs(as_of))
    print(f"as_of={inputs.cd}  bonds={len(inputs.refs)} "
          f"(KEYRATE={sum(1 for r in inputs.refs.values() if r.base=='KEYRATE')}, "
          f"RUONIA={sum(1 for r in inputs.refs.values() if r.base=='RUONIA')})")
    print(f"G-curve pts={len(inputs.g.xs)}: {[(x, round(y*100,2)) for x,y in zip(inputs.g.xs[:3], inputs.g.ys[:3])]}...")

    curve_ru = ExpCurve(inputs.cd, inputs.ois, "RUONIA")
    all_rows = []
    print(f"\n{'вариант':34}{'base':8}{'n':>4}{'mean|Δ|':>9}{'med(Δ)':>8}{'mean(Δ)':>9}{'max|Δ|':>8}")
    for spec in SPECS:
        rows = run_variant(spec, inputs, curve_ru)
        all_rows.extend(rows)
        for base in spec.bases:
            st = stats(rows, base)
            if st:
                print(f"{spec.key:34}{base:8}{st['n']:>4}{st['mean_abs']:>9.1f}"
                      f"{st['median_signed']:>8}{st['mean_signed']:>9.1f}{st['max_abs']:>8}")

    # срез: только амортизируемые (принципал до maturity) — валидация CF-модели
    am = {i for i, r in inputs.refs.items()
          if is_amortizing(future_amorts(inputs.scheds[i], inputs.cd), r.maturity_date)}
    if am:
        print(f"\nтолько амортизируемые ({len(am)}):")
        for spec in SPECS:
            rows = [r for r in all_rows if r.variant == spec.key and r.isin in am]
            for base in spec.bases:
                st = stats(rows, base)
                if st:
                    print(f"{spec.key:34}{base:8}{st['n']:>4}{st['mean_abs']:>9.1f}"
                          f"{st['median_signed']:>8}{st['mean_signed']:>9.1f}{st['max_abs']:>8}")

    # паритет прод-модуля: compute_z_bps на тех же входах должен совпасть
    # с V7 (KEYRATE) / V0 (RUONIA) бит-в-бит
    ref_rows = {(r.base, r.isin): r.our_z for r in all_rows
                if (r.variant.startswith("V7 ") and r.base == "KEYRATE")
                or (r.variant.startswith("V0 ") and r.base == "RUONIA")}
    ok = bad = 0
    for isin, ref in inputs.refs.items():
        nm = inputs.nrd[isin]
        exp = curve_ru if ref.base == "RUONIA" else make_curve("spline", inputs.cd, inputs, True)
        coupons = [{"start": c.get("start"), "end": c.get("end"), "value": c.get("value")}
                   for c in inputs.scheds[isin].get("coupons", [])]
        z = compute_z_bps(ref, exp, inputs.g, inputs.cd, nm["nrd_price_pct"],
                          inputs.accrued.get(isin), coupons, inputs.scheds[isin].get("amorts"))
        expect = ref_rows.get((ref.base, isin))
        if z == expect:
            ok += 1
        else:
            bad += 1
            print(f"  ПАРИТЕТ ✗ {isin} {ref.base}: стенд={expect} prod={z}")
    print(f"\nпаритет prod compute_z_bps: {ok} ✓ / {bad} ✗")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["variant", "isin", "base", "nrd_z", "our_z", "delta",
                        "duration", "margin_bps", "rating", "z_minus_g"])
            for r in all_rows:
                w.writerow([r.variant, r.isin, r.base, r.nrd_z, r.our_z, r.delta,
                            r.duration, r.margin_bps, r.rating, r.z_minus_g])
        print(f"\nCSV: {args.csv}")


asyncio.run(main())
