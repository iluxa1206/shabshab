"""РАЗОВЫЙ стенд: воспроизвести НРД discount_margin (не simple margin).

Гипотеза: НРД считает classic FRN discount margin с ПЛОСКИМ индексом на текущем
уровне (не форвард-кривая). Текущая КС/RUONIA ~15% >> форвард ~13.6%, поэтому
проекция на плоский индекс даёт выше купоны → на дисконте выше DM.

Textbook FRN DM (market standard):
  L = текущий индекс (плоский), из текущего зафикс. купона (value·365/days − margin)
  C_i = value (зафикс) | face·(L+QM)·τ_i (прогноз, простой)   QM = nominal_margin
  dirty = Σ C_i·DF_i + R·DF_mat,  DF_i = Π_{k≤i} 1/(1+(L+DM)·τ_k)
  solve DM.

Варианты дисконта: simple per-period (1+(L+DM)τ), compounded, continuous.
Сверяем каждый с НРД discount_margin И simple_margin на off-par ликвиде.
"""
import asyncio, json, httpx, math
from datetime import date

from rates import get_rates_curves
from forwards import CurveBootstrapper
from services.bonds import build_ref_external
from services.market_data import MarketDataService
from services import nrd
from valuation import dirty_price_rub, build_cashflows_with_spread, solve_dm_bps

UNI = {u["isin"]: u for u in json.load(open("nrd_universe_cache.json")).get("items")}


def _pd(s):
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def accr(cps, d, face, moex):
    for c in cps:
        s, e = _pd(c.get("start")), _pd(c.get("end"))
        v = c.get("value")
        if s and e and s <= d < e:
            if v is not None:
                return float(v) * (d - s).days / ((e - s).days or 1)
            return moex if moex is not None else 0.0
    return moex or 0.0


def current_index(cps, d, margin_frac):
    """L = текущий индекс из зафикс. купона: value·365/days/face − margin."""
    fixed = [c for c in cps if c.get("value") is not None]
    cur = next((c for c in cps if _pd(c.get("start")) and _pd(c.get("end"))
                and _pd(c["start"]) <= d < _pd(c["end"])), None)
    c = cur if (cur and cur.get("value") is not None) else (fixed[-1] if fixed else None)
    if not c:
        return None
    s, e = _pd(c["start"]), _pd(c["end"])
    days = (e - s).days or 1
    return float(c["value"]) * 365 / (days * 1000.0) - margin_frac


def frn_cfs(ref, cps, amorts, cd, L, qm):
    """Потоки при ПЛОСКОМ индексе L: зафикс=value, прогноз=face·(L+qm)·τ. +принципал."""
    fam = sorted((d, float(a["value"])) for a in (amorts or [])
                 if a.get("value") is not None and (d := _pd(a.get("date"))) and d > cd)
    amortizing = any(ref.maturity_date and d < ref.maturity_date for d, _ in fam)
    cfs = []
    for c in cps:
        e = _pd(c.get("end"))
        if not e or e <= cd or (ref.maturity_date and e > ref.maturity_date):
            continue
        s = _pd(c.get("start")) or e
        tau = (e - s).days / 365.0
        v = c.get("value")
        if v is not None:
            amt = float(v)
        else:
            face = ref.face_value
            if amortizing:
                face = sum(x for d, x in fam if d > s) or face
            amt = face * (L + qm) * tau
        cfs.append((e, amt))
    if amortizing:
        cfs += fam
    elif ref.maturity_date and ref.maturity_date > cd:
        cfs.append((ref.maturity_date, ref.face_value))
    cfs.sort()
    return cfs


def solve_frn_dm(cfs, cd, L, dirty, mode="simple"):
    """DM над плоским индексом L: DF_i = Π 1/(1+(L+DM)·τ_k) (simple) / comp / cont."""
    grid = sorted(set(d for d, _ in cfs if d > cd))
    if not grid:
        return None
    amt_on = {}
    for d, a in cfs:
        if d > cd:
            amt_on[d] = amt_on.get(d, 0.0) + a

    def pv(dm):
        r = L + dm
        df = 1.0
        prev = cd
        tot = 0.0
        for d in grid:
            tau = (d - prev).days / 365.0
            if mode == "simple":
                df /= (1.0 + r * tau)
            elif mode == "comp":
                df /= (1.0 + r) ** tau
            else:  # cont
                df *= math.exp(-r * tau)
            tot += amt_on[d] * df
            prev = d
        return tot

    lo, hi = -0.5, 2.5
    flo, fhi = pv(lo) - dirty, pv(hi) - dirty
    if flo * fhi > 0:
        return None
    for _ in range(90):
        mid = (lo + hi) / 2
        fm = pv(mid) - dirty
        if abs(fm) < 1e-8:
            break
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return round((lo + hi) / 2 * 10000)


def solve_dm_forward_disc(ref, curve, cfs, cd, dirty):
    """Гибрид: потоки cfs (спроектированы на плоском L), дисконт по ФОРВАРД-кривой+DM
    (recursive comp как прод). Форвард ниже тек.индекса → дальний redemption весит
    больше → выше ценочувствительность DM (ближе к discount_margin)."""
    grid = sorted(set(d for d, _ in cfs if d > cd))
    if not grid:
        return None
    amt_on = {}
    for d, a in cfs:
        if d > cd:
            amt_on[d] = amt_on.get(d, 0.0) + a

    def pv(dm):
        df = 1.0
        prev = cd
        tot = 0.0
        for d in grid:
            days = (d - prev).days
            f = curve.forward(prev, d) if prev < d else 0.0
            r = f + dm
            if ref.base == "RUONIA":
                df /= (1.0 + r / 365.0) ** days
            else:
                df /= (1.0 + r / 4.0) ** (4.0 * days / 365.0)
            tot += amt_on[d] * df
            prev = d
        return tot

    lo, hi = -0.5, 2.5
    flo, fhi = pv(lo) - dirty, pv(hi) - dirty
    if flo * fhi > 0:
        return None
    for _ in range(90):
        mid = (lo + hi) / 2
        fm = pv(mid) - dirty
        if abs(fm) < 1e-8:
            break
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return round((lo + hi) / 2 * 10000)


async def main():
    ois, irs = get_rates_curves(use_cache=True)
    cand = [i for i, u in UNI.items()
            if u.get("nrd_price_pct") and abs(u["nrd_price_pct"] - 100) > 2
            and u.get("discount_margin_bps") is not None][:60]
    async with httpx.AsyncClient(timeout=60) as client:
        raw = await nrd._fetch_method(client, nrd.PATH_VALUATION_ADD, cand)
    secs = await MarketDataService.fetch_moex_securities(cand)
    snap = await MarketDataService.fetch_moex_snapshot(cand)
    fulls = dict(zip(cand, await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(i) for i in cand))))

    print(f"{'ISIN':14} {'bs':2} {'price':>6} {'L%':>5} {'NRDsimp':>7} {'NRDdisc':>7} "
          f"{'ourFwd':>6} {'frnS':>6} {'frnHyb':>6}")
    rows = []
    for i in cand:
        r = raw.get(i) or {}
        u = UNI[i]
        vol = float(r.get("trade_volume_rub") or 0)
        if vol < 3e6:
            continue
        price, vd = r.get("wa_price"), _pd(r.get("val_date"))
        if price is None or vd is None:
            continue
        price = float(price)
        simp = round(float(r["simple_margin"]) * 100) if r.get("simple_margin") is not None else None
        disc = round(float(r["discount_margin"]) * 100) if r.get("discount_margin") is not None else None
        bidx = "CBRATED" if u["base_rate_type"] == "KEYRATE" else "RUONIARATED"
        ref = build_ref_external(i, secs.get(i, {}),
                                 {"base_coupon_index": bidx, "nominal_margin_bps": u.get("spread_issue_bps") or 0})
        if not ref.maturity_date:
            continue
        cd = vd
        curve = (CurveBootstrapper.bootstrap_ruonia(ois, cd) if ref.base == "RUONIA"
                 else CurveBootstrapper.bootstrap_keyrate(irs, cd))
        full = fulls.get(i) or {}
        cps = full.get("coupons") or []
        trip = [(_pd(c["start"]), _pd(c["end"]), c.get("value")) for c in cps if c.get("start") and c.get("end")]
        if not trip:
            continue
        qm = (ref.spread_issue_bps or 0) / 10000.0
        ref.accrued_rub = accr(cps, cd, ref.face_value, snap.get(i, {}).get("accrued"))
        dirty = dirty_price_rub(ref.face_value, price, ref.accrued_rub)
        L = current_index(cps, cd, qm)
        if L is None:
            continue

        try:
            cfs_f = build_cashflows_with_spread(ref, curve, cd, ref.spread_issue_bps,
                                                explicit_periods=trip, amorts=full.get("amorts"))
            our_fwd = solve_dm_bps(ref, curve, cfs_f, cd, dirty)
        except Exception:
            our_fwd = None
        cfs = frn_cfs(ref, cps, full.get("amorts"), cd, L, qm)
        frn_s = solve_frn_dm(cfs, cd, L, dirty, "simple")
        # гибрид: купоны на плоском L (высокий тек.индекс), дисконт на форвард-кривой+DM
        frn_h = solve_dm_forward_disc(ref, curve, cfs, cd, dirty)

        rows.append(dict(base=ref.base, simp=simp, disc=disc, our_fwd=our_fwd,
                         frn_s=frn_s, frn_h=frn_h))
        g = lambda x, w=6: (f"{x:>{w}}" if isinstance(x, int) else f"{'—':>{w}}")
        print(f"{i:14} {ref.base[:2]:2} {price:>6.2f} {L*100:>5.1f} {g(simp,7)} {g(disc,7)} "
              f"{g(our_fwd)} {g(frn_s)} {g(frn_h)}")

    def stat(key, tgt):
        ds = [r[key] - r[tgt] for r in rows if r[key] is not None and r[tgt] is not None]
        if not ds:
            return None
        ds.sort()
        n = len(ds)
        m = sum(ds) / n
        return n, m, ds[n//2], (sum((d-m)**2 for d in ds)/n)**0.5, sum(abs(d) for d in ds)/n

    print("\nСверка вариантов с целями (bps):")
    for key, label in [("our_fwd", "ourFwd (текущий)"), ("frn_s", "FRN simple(flat)"),
                       ("frn_h", "FRN hybrid(fwd-disc)")]:
        for tgt, tname in [("disc", "vs НРД discount"), ("simp", "vs НРД simple")]:
            s = stat(key, tgt)
            if s:
                print(f"  {label:18} {tname:16} n={s[0]:2} mean={s[1]:+7.1f} med={s[2]:+6.0f} std={s[3]:6.1f} m|Δ|={s[4]:6.1f}")


asyncio.run(main())
