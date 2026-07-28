"""РАЗОВЫЙ стенд: ЧИСТАЯ расчётная часть DM/z vs НРД на ОДНОЙ цене И дате.

Изолируем математику полностью:
  - цена = НРД wa_price (их market price)
  - НКД = на НРД val_date, ЛИНЕЙНО из купона (value·прошло/период) — как считает
    НРД/MOEX, НЕ MOEX-сегодня (иначе рассинхрон дат цены и НКД)
  - calc_date = val_date НРД (дисконт с той же даты, что их цена)
Остаток = только разница движков (наш forward-CF vs их NSS/Kalman fair-value).
"""
import asyncio, json, httpx
from datetime import date

from core.rates import get_rates_curves
from core.forwards import CurveBootstrapper
from services.bonds import build_ref_external
from services.market_data import MarketDataService
from services.zspread import compute_z_bps
from services import nrd
from core.valuation import dirty_price_rub, build_cashflows_with_spread, solve_dm_bps

UNI = {u["isin"]: u for u in json.load(open("nrd_universe_cache.json")).get("items")}
N = 45


def _pd(s):
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def accrued_asof(coupons, d, face, moex_acc=None, cur_rate_pct=None):
    """НКД на дату d (руб). Приоритет:
    1) зафикс. купон (value есть) → линейно value·прошло/период;
    2) MOEX ACCRUEDINT (реальный, из snapshot) — для RUONIA current где value=None
       (сегодня, дрейф к val_date мал);
    3) valueprc/current-rate → face·rate·прошло/365 (если value=None но ставка известна)."""
    for c in coupons:
        s, e = _pd(c.get("start")), _pd(c.get("end"))
        v, vp = c.get("value"), c.get("valueprc")
        if s and e and s <= d < e:
            elapsed = (d - s).days
            per = (e - s).days or 1
            if v is not None:
                return float(v) * elapsed / per
            if moex_acc is not None:
                return moex_acc
            rate = vp if vp is not None else cur_rate_pct
            if rate is not None:
                return face * (float(rate) / 100.0) * elapsed / 365.0
    return moex_acc if moex_acc is not None else 0.0


async def main():
    ois, irs = get_rates_curves(use_cache=True)
    exp_ks, exp_ru, g = await MarketDataService.get_zspread_ctx()

    isins = [i for i, u in UNI.items()
             if u.get("discount_margin_bps") is not None and u.get("nrd_price_pct")
             and u.get("base_rate_type") in ("KEYRATE", "RUONIA")]
    kk = [i for i in isins if UNI[i]["base_rate_type"] == "KEYRATE"][:N - 13]
    rr = [i for i in isins if UNI[i]["base_rate_type"] == "RUONIA"][:13]
    sample = kk + rr

    async with httpx.AsyncClient(timeout=60) as client:
        raw = await nrd._fetch_method(client, nrd.PATH_VALUATION_ADD, sample)
    secs = await MarketDataService.fetch_moex_securities(sample)
    snap = await MarketDataService.fetch_moex_snapshot(sample)
    fulls = dict(zip(sample, await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(i) for i in sample))))

    rows = []
    for isin in sample:
        r = raw.get(isin) or {}
        u = UNI[isin]
        dm_nrd, price, z_nrd = r.get("discount_margin"), r.get("wa_price"), r.get("z_spread")
        sm_nrd = r.get("simple_margin")  # НРД simple margin (%→bps), альт-таргет
        vdate = _pd(r.get("val_date"))
        if dm_nrd is None or price is None or vdate is None:
            continue
        dm_nrd = round(float(dm_nrd) * 100)
        sm_nrd = round(float(sm_nrd) * 100) if sm_nrd is not None else None
        z_nrd = round(float(z_nrd) * 10000) if z_nrd is not None else None
        price = float(price)

        cd = vdate  # calc_date = дата цены НРД
        ru = CurveBootstrapper.bootstrap_ruonia(ois, cd)
        kr = CurveBootstrapper.bootstrap_keyrate(irs, cd)

        bidx = "CBRATED" if u["base_rate_type"] == "KEYRATE" else "RUONIARATED"
        ref = build_ref_external(isin, secs.get(isin, {}),
                                 {"base_coupon_index": bidx, "nominal_margin_bps": u.get("spread_issue_bps") or 0})
        if not ref.maturity_date:
            continue
        curve = ru if ref.base == "RUONIA" else kr
        exp = exp_ru if ref.base == "RUONIA" else exp_ks
        full = fulls.get(isin) or {}
        coupons_full = full.get("coupons") or []
        trip = [(_pd(c["start"]), _pd(c["end"]), c.get("value"))
                for c in coupons_full if c.get("start") and c.get("end")]
        if not trip:
            continue
        amorts = full.get("amorts")
        # НКД: зафикс.купон → из value; RUONIA current (value=None) → реальный MOEX
        moex_acc = snap.get(isin, {}).get("accrued")
        ref.accrued_rub = accrued_asof(coupons_full, cd, ref.face_value, moex_acc=moex_acc)
        dirty = dirty_price_rub(ref.face_value, price, ref.accrued_rub)

        try:
            cfs = build_cashflows_with_spread(ref, curve, cd, ref.spread_issue_bps,
                                              explicit_periods=trip, amorts=amorts)
            dm_our = solve_dm_bps(ref, curve, cfs, cd, dirty)
        except Exception:
            dm_our = None
        z_our = None
        try:
            cdicts = [{"start": c.get("start"), "end": c.get("end"), "value": c.get("value")}
                      for c in coupons_full]
            z_our = compute_z_bps(ref, exp, g, cd, price, ref.accrued_rub, cdicts, amorts)
        except Exception:
            pass

        # имплайд-цена НРД: при какой чистой цене наша модель даёт ИХ dm?
        # если далеко от wa_price → НРД считал от fair value (valuationnew), не от рынка
        impl_price = None
        if dm_our is not None:
            lo, hi = 60.0, 140.0
            def gap(p):
                d = dirty_price_rub(ref.face_value, p, ref.accrued_rub)
                cc = build_cashflows_with_spread(ref, curve, cd, ref.spread_issue_bps,
                                                 explicit_periods=trip, amorts=amorts)
                dd = solve_dm_bps(ref, curve, cc, cd, d)
                return (dd - dm_nrd) if dd is not None else None
            glo, ghi = gap(lo), gap(hi)
            if glo is not None and ghi is not None and glo * ghi < 0:
                for _ in range(40):
                    mid = (lo + hi) / 2
                    gm = gap(mid)
                    if gm is None:
                        break
                    if glo * gm <= 0:
                        hi = mid
                    else:
                        lo, glo = mid, gm
                impl_price = (lo + hi) / 2

        # НЕЗАВИСИМЫЙ признак ликвидности из сырой строки НРД (не циркулярный с ΔDM)
        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        vol_rub = _f(r.get("trade_volume_rub"))
        vol_qty = _f(r.get("trade_volume_qty"))
        rows.append(dict(isin=isin, base=ref.base, price=price, acc=ref.accrued_rub,
                         dm_nrd=dm_nrd, sm_nrd=sm_nrd, dm_our=dm_our, z_nrd=z_nrd, z_our=z_our,
                         impl_price=impl_price, vol_rub=vol_rub, vol_qty=vol_qty,
                         vmethod=r.get("valuation_method"),
                         offer_soon=any(_pd(o.get("date")) and (_pd(o["date"]) - cd).days <= 90
                                        for o in (full.get("offers") or []) if o.get("date")),
                         T=(ref.maturity_date - cd).days / 365.0))

    print(f"{'ISIN':14} {'bs':2} {'T':>4} {'price':>7} {'acc':>6} "
          f"{'NRDdm':>6} {'ourDM':>6} {'ΔDM':>6}  {'NRDz':>6} {'ourZ':>6} {'Δz':>6}")
    for r in rows:
        f = lambda x, w=6: (f"{x:>{w}}" if isinstance(x, int) else (f"{x:>{w}.2f}" if isinstance(x, float) else f"{'—':>{w}}"))
        ddm = (r["dm_our"] - r["dm_nrd"]) if r["dm_our"] is not None else None
        dz = (r["z_our"] - r["z_nrd"]) if (r["z_our"] is not None and r["z_nrd"] is not None) else None
        print(f"{r['isin']:14} {r['base'][:2]:2} {r['T']:>4.1f} {f(r['price'],7)} {f(r['acc'])} "
              f"{f(r['dm_nrd'])} {f(r['dm_our'])} {f(ddm)}  {f(r['z_nrd'])} {f(r['z_our'])} {f(dz)}")

    def stats(key_our, key_nrd, base=None):
        ds = [(r[key_our] - r[key_nrd]) for r in rows
              if r[key_our] is not None and r[key_nrd] is not None and (base is None or r["base"] == base)]
        if not ds:
            return None
        ds.sort()
        n = len(ds)
        m = sum(ds) / n
        med = ds[n // 2]
        sd = (sum((d - m) ** 2 for d in ds) / n) ** 0.5
        ma = sum(abs(d) for d in ds) / n
        p10, p90 = ds[max(0, n // 10)], ds[min(n - 1, 9 * n // 10)]
        return n, m, med, sd, ma, p10, p90

    print("\nРАЗБРОС на цене+дате НРД (чистая математика):")
    for label, ko, kn in [("DM vs НРДdm", "dm_our", "dm_nrd"),
                          ("DM vs НРДsimpleМ", "dm_our", "sm_nrd"),
                          ("z", "z_our", "z_nrd")]:
        for b in ("KEYRATE", "RUONIA"):
            s = stats(ko, kn, b)
            if s:
                print(f"  {label:16} {b[:4]:5} n={s[0]:2}  mean={s[1]:+7.1f}  med={s[2]:+6.0f}  "
                      f"std={s[3]:6.1f}  m|Δ|={s[4]:6.1f}  p10..p90=[{s[5]:+.0f}..{s[6]:+.0f}]")

    # гистограмма ΔDM
    print("\nГистограмма ΔDM (наш−НРД), bps:")
    buckets = [(-1e9, -100), (-100, -50), (-50, -25), (-25, -10), (-10, 10),
               (10, 25), (25, 50), (50, 100), (100, 1e9)]
    dall = [(r["dm_our"] - r["dm_nrd"]) for r in rows if r["dm_our"] is not None]
    for lo, hi in buckets:
        c = sum(1 for d in dall if lo <= d < hi)
        lbl = f"[{int(lo) if lo>-1e9 else '−∞':>4}..{int(hi) if hi<1e9 else '+∞':>4})"
        print(f"  {lbl:16} {'█'*c} {c}")
    med_all = sorted(dall)[len(dall)//2]
    within = sum(1 for d in dall if abs(d) <= 25)
    print(f"\n  всего n={len(dall)}  медиана={med_all:+.0f}bps  |Δ|≤25bps: {within}/{len(dall)} ({100*within//len(dall)}%)")

    # ── разделение: ЧИСТОЕ ЯДРО vs DATA-хвосты (НЕЗАВИСИМЫЙ признак ликвидности) ──
    VOL_MIN = 1_000_000  # < 1 млн ₽ дневного оборота → НРД wa ненадёжна (fair value)
    print(f"\nКЛАССИФИКАЦИЯ (независимо: оборот НРД, оферта). Порог ликвидности {VOL_MIN:,}₽:")
    core, tails = [], []
    for r in rows:
        if r["dm_our"] is None:
            continue
        d = r["dm_our"] - r["dm_nrd"]
        reasons = []
        if r["offer_soon"]:
            reasons.append("оферта≤90д")
        if r["vol_rub"] is not None and r["vol_rub"] < VOL_MIN:
            reasons.append(f"неликвид({r['vol_rub']/1e6:.2f}млн₽)")
        elif r["vol_rub"] is None:
            reasons.append("оборот=None")
        if reasons:
            tails.append((r, d, reasons))
        else:
            core.append(d)
    for r, d, reasons in sorted(tails, key=lambda x: abs(x[1]), reverse=True):
        print(f"  {r['isin']} {r['base'][:2]} ΔDM={d:+5.0f} impl={r['impl_price'] and round(r['impl_price'],1)} → {', '.join(reasons)}")

    def _st(xs, label):
        if not xs:
            return
        xs = sorted(xs)
        n = len(xs)
        m = sum(xs) / n
        sd = (sum((x - m) ** 2 for x in xs) / n) ** 0.5
        w25 = sum(1 for x in xs if abs(x) <= 25)
        w50 = sum(1 for x in xs if abs(x) <= 50)
        print(f"  {label}: n={n}  mean={m:+.1f}  med={xs[n//2]:+.0f}  std={sd:.1f}  "
              f"min={xs[0]:+.0f} max={xs[-1]:+.0f}  |Δ|≤25:{w25}/{n} ≤50:{w50}/{n}")

    print("\n★ РЕЗУЛЬТАТ ПО СЕГМЕНТАМ:")
    _st(core, "ЧИСТОЕ ЯДРО (ликвид >1млн₽, без оферты)")
    _st([d for r, d, _ in tails], "DATA-хвосты (неликвид/оферта)")
    core_k = [r["dm_our"] - r["dm_nrd"] for r in rows if r["dm_our"] is not None
              and r["base"] == "KEYRATE" and not r["offer_soon"]
              and (r["vol_rub"] or 0) >= VOL_MIN]
    core_r = [r["dm_our"] - r["dm_nrd"] for r in rows if r["dm_our"] is not None
              and r["base"] == "RUONIA" and not r["offer_soon"]
              and (r["vol_rub"] or 0) >= VOL_MIN]
    _st(core_k, "  ядро KEYRATE")
    _st(core_r, "  ядро RUONIA")


asyncio.run(main())
