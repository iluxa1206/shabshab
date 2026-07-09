"""РАЗОВЫЙ стенд: сверка DM/z/yield/duration с НРД ДО и ПОСЛЕ фикса #1(+#3).

Вход — цена НРД (wa_price) + MOEX accrued (изолирует математику от расхождения цен).
  before = build_cashflows_with_spread(пары, amorts=None)  → старое поведение
  after  = build_cashflows_with_spread(тройки value, amorts) → новое (#1 факт купона + #3 амортизация)
Выборка балансируется по base (KEYRATE/RUONIA) × amort/bullet.
Не прод-артефакт — запускать вручную для отчёта.
"""
import asyncio, json
from datetime import date

from rates import get_rates_curves
from forwards import CurveBootstrapper
from services.bonds import build_ref_external
from services.market_data import MarketDataService
from services.zspread import compute_z_bps, project_cfs, solve_flat_y
from valuation import dirty_price_rub, build_cashflows_with_spread, solve_dm_bps

_UNI = json.load(open("nrd_universe_cache.json"))
UNI = {u["isin"]: u for u in (_UNI.get("items") or _UNI)}

# Курированная выборка: KEYRATE bullet (AAA/AA), RUONIA bullet (AAA),
# KEYRATE амортизируемые (эффект #3). Детерминированно — без сканирования 456×ISS.
SAMPLE = [
    "RU000A0JQAM6", "RU000A0JXE06", "RU000A0ZZ1J8",          # KEYRATE bullet
    "RU000A1042W6", "RU000A105YH8", "RU000A106375",          # RUONIA bullet
    "RU000A1087G4", "RU000A1094W7", "RU000A1097C2", "RU000A109XR1",  # KEYRATE amortizing
]


def _ref(isin, mo, u):
    """BondRefData из MOEX securities + НРД-юниверса (база/спред)."""
    base_idx = "CBRATED" if u.get("base_rate_type") == "KEYRATE" else "RUONIARATED"
    ref = build_ref_external(isin, mo, {"base_coupon_index": base_idx,
                                        "nominal_margin_bps": u.get("spread_issue_bps") or 0})
    return ref


def _pd(s):
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def is_amortizing(full, maturity):
    for a in full.get("amorts") or []:
        d = _pd(a.get("date"))
        if d and maturity and d < maturity and a.get("value") is not None:
            return True
    return False


async def main():
    ois, irs = get_rates_curves(use_cache=True)
    cd = date.today()
    ruonia = CurveBootstrapper.bootstrap_ruonia(ois, cd)
    keyrate = CurveBootstrapper.bootstrap_keyrate(irs, cd)
    exp_ks, exp_ru, g_curve = await MarketDataService.get_zspread_ctx()

    sample = [i for i in SAMPLE if i in UNI]
    secs = await MarketDataService.fetch_moex_securities(sample)
    fulls = dict(zip(sample, await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(i) for i in sample))))
    snap = await MarketDataService.fetch_moex_snapshot(sample)

    hdr = (f"{'ISIN':14} {'base':4} {'am':2} {'rat':4} {'price':>7} "
           f"{'NRDdm':>6} {'before':>7} {'after':>7} {'Δbef':>6} {'Δaft':>6}  "
           f"{'NRDz':>6} {'ourZ':>6} {'NRDytm':>7} {'ourY':>7}")
    print(hdr)
    print("-" * len(hdr))
    rows = []
    for isin in sample:
        nm = UNI[isin]
        ref = _ref(isin, secs.get(isin, {}), nm)
        full = fulls.get(isin) or {}
        curve = ruonia if ref.base == "RUONIA" else keyrate
        exp = exp_ru if ref.base == "RUONIA" else exp_ks
        price = nm["nrd_price_pct"]
        nrd_dm = nm["discount_margin_bps"]
        nrd_z = nm.get("z_spread_bps")
        nrd_ytm = nm.get("ytm_pct") or nm.get("yield_maturity_pct") or nm.get("current_yield_pct")
        rating = (nm.get("rating") or "")[:4]
        am = is_amortizing(full, ref.maturity_date)
        acc = snap.get(isin, {}).get("accrued")
        if acc is not None:
            ref.accrued_rub = acc
        dirty = dirty_price_rub(ref.face_value, price, ref.accrued_rub)

        coupons_full = full.get("coupons") or []
        triples = [(_pd(c["start"]), _pd(c["end"]), c.get("value"))
                   for c in coupons_full if c.get("start") and c.get("end")]
        pairs = [(s, e) for (s, e, _v) in triples]
        amorts = full.get("amorts")

        # DM before: пары без value, без amorts (старое поведение)
        try:
            cfs_b = build_cashflows_with_spread(ref, curve, cd, ref.spread_issue_bps,
                                                explicit_periods=pairs or None, amorts=None)
            dm_before = solve_dm_bps(ref, curve, cfs_b, cd, dirty)
        except Exception:
            dm_before = None
        # DM after: тройки value + amorts (#1 + #3)
        try:
            cfs_a = build_cashflows_with_spread(ref, curve, cd, ref.spread_issue_bps,
                                                explicit_periods=triples or None, amorts=amorts)
            dm_after = solve_dm_bps(ref, curve, cfs_a, cd, dirty)
        except Exception:
            dm_after = None

        # наш z (не менялся фиксом — контекст) + наш flat-yield
        our_z = our_y = None
        try:
            cdicts = [{"start": c.get("start"), "end": c.get("end"), "value": c.get("value")}
                      for c in coupons_full]
            our_z = compute_z_bps(ref, exp, g_curve, cd, price, ref.accrued_rub, cdicts, amorts)
            zcfs = project_cfs(ref, exp, cd, cdicts, amorts)
            fy = solve_flat_y(zcfs, cd, dirty)
            our_y = round((pow(2.718281828, fy) - 1) * 100, 2) if fy is not None else None
        except Exception:
            pass

        db = (dm_before - nrd_dm) if dm_before is not None else None
        da = (dm_after - nrd_dm) if dm_after is not None else None
        rows.append((ref.base, am, db, da))
        fN = lambda x, w=7: (f"{x:>{w}}" if isinstance(x, int) else
                             (f"{x:>{w}.2f}" if isinstance(x, float) else f"{'—':>{w}}"))
        print(f"{isin:14} {ref.base[:4]:4} {'Y' if am else '.':2} {rating:4} {price:>7.2f} "
              f"{nrd_dm:>6} {fN(dm_before)} {fN(dm_after)} {fN(db,6)} {fN(da,6)}  "
              f"{fN(nrd_z,6)} {fN(our_z,6)} {fN(nrd_ytm)} {fN(our_y)}")

    # агрегаты
    def agg(sel):
        b = [abs(r[2]) for r in rows if sel(r) and r[2] is not None]
        a = [abs(r[3]) for r in rows if sel(r) and r[3] is not None]
        mb = sum(b) / len(b) if b else 0
        ma = sum(a) / len(a) if a else 0
        return len(b), mb, ma
    print("\nСредн|Δ DM| к НРД, bps (before → after):")
    for label, sel in [
        ("ВСЕ", lambda r: True),
        ("KEYRATE", lambda r: r[0] == "KEYRATE"),
        ("RUONIA", lambda r: r[0] == "RUONIA"),
        ("амортизируемые", lambda r: r[1]),
        ("bullet", lambda r: not r[1]),
    ]:
        n, mb, ma = agg(sel)
        arrow = "↓ лучше" if ma < mb else ("↑ хуже" if ma > mb else "=")
        print(f"  {label:16} n={n:2}  {mb:7.1f} → {ma:7.1f}   {arrow}")


asyncio.run(main())
