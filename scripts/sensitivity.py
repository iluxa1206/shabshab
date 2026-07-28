"""РАЗОВЫЙ sensitivity-стенд: что двигает DM (и z, yield) и насколько.
Берём репрезентативные бумаги, шевелим каждый вход по одному, меряем Δметрики.
Не прод-артефакт.
"""
import asyncio, json
from datetime import date
from dataclasses import replace

from core.rates import get_rates_curves
from core.forwards import CurveBootstrapper, DiscountCurve
from services.bonds import build_ref_external
from services.market_data import MarketDataService
from services.zspread import compute_z_bps
from core.valuation import dirty_price_rub, build_cashflows_with_spread, solve_dm_bps, xirr_yield_pct

UNI = {u["isin"]: u for u in json.load(open("nrd_universe_cache.json")).get("items")}

# KEYRATE bullet, RUONIA bullet, KEYRATE amortizing
BONDS = ["RU000A0JXE06", "RU000A1042W6", "RU000A1087G4"]


def _pd(s):
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


class ShiftedCurve(DiscountCurve):
    """Обёртка: параллельный сдвиг форварда на delta (в долях). Проекция И дисконт
    используют forward() → сдвигает обе стороны (демонстрирует curve-относительность DM)."""
    def __init__(self, base, delta):
        self.base = base
        self.delta = delta
        self.calc_date = base.calc_date

    def forward(self, t1, t2):
        return self.base.forward(t1, t2) + self.delta

    def df(self, d):
        return self.base.df(d)


async def main():
    ois, irs = get_rates_curves(use_cache=True)
    cd = date.today()
    ru = CurveBootstrapper.bootstrap_ruonia(ois, cd)
    kr = CurveBootstrapper.bootstrap_keyrate(irs, cd)
    exp_ks, exp_ru, g = await MarketDataService.get_zspread_ctx()

    secs = await MarketDataService.fetch_moex_securities(BONDS)
    fulls = dict(zip(BONDS, await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(i) for i in BONDS))))
    snap = await MarketDataService.fetch_moex_snapshot(BONDS)

    for isin in BONDS:
        u = UNI[isin]
        bidx = "CBRATED" if u["base_rate_type"] == "KEYRATE" else "RUONIARATED"
        ref = build_ref_external(isin, secs.get(isin, {}),
                                 {"base_coupon_index": bidx, "nominal_margin_bps": u.get("spread_issue_bps") or 0})
        curve = ru if ref.base == "RUONIA" else kr
        exp = exp_ru if ref.base == "RUONIA" else exp_ks
        full = fulls.get(isin) or {}
        trip = [(_pd(c["start"]), _pd(c["end"]), c.get("value"))
                for c in (full.get("coupons") or []) if c.get("start") and c.get("end")]
        pairs = [(s, e) for (s, e, _v) in trip]
        amorts = full.get("amorts")
        cdicts = [{"start": c.get("start"), "end": c.get("end"), "value": c.get("value")}
                  for c in (full.get("coupons") or [])]
        acc = snap.get(isin, {}).get("accrued")
        if acc is not None:
            ref.accrued_rub = acc
        price = u["nrd_price_pct"]

        def dm_of(ref_, curve_, price_, trip_, amorts_, accr=None):
            a = ref_.accrued_rub if accr is None else accr
            dirty = dirty_price_rub(ref_.face_value, price_, a)
            cfs = build_cashflows_with_spread(ref_, curve_, cd, ref_.spread_issue_bps,
                                              explicit_periods=trip_, amorts=amorts_)
            return solve_dm_bps(ref_, curve_, cfs, cd, dirty)

        def y_of(price_):
            dirty = dirty_price_rub(ref.face_value, price_, ref.accrued_rub)
            cfs = build_cashflows_with_spread(ref, curve, cd, ref.spread_issue_bps,
                                              explicit_periods=trip, amorts=amorts)
            return xirr_yield_pct(dirty, cfs, cd)

        def z_of(price_):
            return compute_z_bps(ref, exp, g, cd, price_, ref.accrued_rub, cdicts, amorts)

        base_dm = dm_of(ref, curve, price, trip, amorts)
        base_y = y_of(price)
        base_z = z_of(price)
        am = any(_pd(a.get("date")) and ref.maturity_date and _pd(a["date"]) < ref.maturity_date
                 for a in (amorts or []) if a.get("value") is not None)

        print(f"\n=== {isin}  {ref.base}{' amort' if am else ' bullet'}  "
              f"price={price:.2f} spread={ref.spread_issue_bps}bp  mat={ref.maturity_date} ===")
        print(f"  BASE:  DM={base_dm}  z={base_z}  yield={base_y:.2f}%")

        def line(label, dm, z=None, y=None):
            dd = f"{dm-base_dm:+d}" if (dm is not None and base_dm is not None) else "—"
            zz = f"  Δz={z-base_z:+d}" if (z is not None and base_z is not None) else ""
            yy = f"  Δy={y-base_y:+.2f}" if (y is not None and base_y is not None) else ""
            print(f"    {label:26} ΔDM={dd:>6}{zz}{yy}")

        # 1. цена ±1 пункт
        line("price +1.0пт", dm_of(ref, curve, price + 1, trip, amorts), z_of(price + 1), y_of(price + 1))
        line("price -1.0пт", dm_of(ref, curve, price - 1, trip, amorts), z_of(price - 1), y_of(price - 1))
        # 2. НКД ±5 руб
        line("accrued +5руб", dm_of(ref, curve, price, trip, amorts, accr=ref.accrued_rub + 5))
        # 3. спред выпуска ±50бп
        line("spread +50бп", dm_of(replace(ref, spread_issue_bps=ref.spread_issue_bps + 50), curve, price, trip, amorts))
        # 4. параллельный сдвиг кривой ±100бп (проекция И дисконт)
        line("curve +100бп (parallel)", dm_of(ref, ShiftedCurve(curve, 0.01), price, trip, amorts), z_of(price))
        line("curve -100бп (parallel)", dm_of(ref, ShiftedCurve(curve, -0.01), price, trip, amorts))
        # 5. #1 выкл: тройки→пары (перепрогноз зафикс. купона)
        line("#1 off (пары, перепрогноз)", dm_of(ref, curve, price, pairs, amorts))
        # 6. #3 выкл: amorts=None (bullet вместо графика)
        if am:
            line("#3 off (bullet вместо amort)", dm_of(ref, curve, price, trip, None))


asyncio.run(main())
