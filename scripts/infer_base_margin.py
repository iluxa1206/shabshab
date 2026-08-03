#!/usr/bin/env python
"""Восстановление базы и маржи флоатера из ФАКТИЧЕСКИ выплаченных купонов.

Для непрайсуемых бумаг (нет base/margin, corpbonds их не знает — ипотечные
агенты, СФО, свежие размещения) параметров нет ни в одном источнике. Но если
бумага — флоатер, её прошлые купоны обязаны воспроизводиться как
«индекс(лаг,окно) + константа»: перебираем (база × лаг × окно), для каждой
комбинации маржа = медиана (факт − индекс), качество = разброс этой маржи.

Фикс-купоны отсеиваются сами: у них разброс растёт вместе с движением ставки.

Применяет (--apply) только уверенные срабатывания: разброс < 0.1пп и не
меньше MIN_COUPONS выплаченных купонов. Маржа округляется до 5 bps —
эмитенты задают её круглой, а остаток разброса это шум округления MOEX.

Использование:
    .venv/bin/python scripts/infer_base_margin.py --limit 40      # dry-run
    .venv/bin/python scripts/infer_base_margin.py --limit 40 --apply
    .venv/bin/python scripts/infer_base_margin.py ISIN1 ISIN2 --apply
"""
import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOL_SPREAD_PP = 0.10       # разброс маржи по купонам — «константа» или нет
MIN_COUPONS = 3
LAGS = [0, 1, 2, 3, 5, 7, 14]


def _median(v):
    a = sorted(v)
    n = len(a)
    return None if not n else (a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2)


async def infer_one(isin: str, row: dict, today: date) -> dict | None:
    from services.market_data import MarketDataService
    from services.coupon_calib import _past_rows, _index, _realized, _rate_at

    full = await MarketDataService.fetch_bond_schedule_full(isin)
    coupons = (full or {}).get("coupons") or []
    amorts = (full or {}).get("amorts") or []
    face = row.get("face_value") or 1000.0
    # маржа неизвестна → снимаем «чистую» ставку купона (margin_pct=0)
    rows_past = [r for r in _past_rows(coupons, 0.0, face, today, amorts)[-8:] if r[2] > 1.0]
    if len(rows_past) < MIN_COUPONS:
        return None
    period = row.get("coupon_period_days")

    best = None
    for base in ("KEYRATE", "RUONIA"):
        idx = _index(base)
        if not idx or not idx[0]:
            continue
        for window in (1, None):
            for lag in LAGS:
                margins = []
                for s, e, obs in rows_past:
                    if window == 1:
                        obs_d = s - timedelta(days=lag)
                        if not _realized(idx, obs_d, today):
                            continue
                        r = _rate_at(idx, obs_d)
                    else:                      # среднее по дням дохода (s,e]
                        tot, n, cur = 0.0, 0, s + timedelta(days=1)
                        while cur <= e:
                            d = cur - timedelta(days=lag)
                            v = _rate_at(idx, d) if _realized(idx, d, today) else None
                            if v is not None:
                                tot += v
                                n += 1
                            cur += timedelta(days=1)
                        r = tot / n if n else None
                    if r is None:
                        continue
                    margins.append(obs - r)
                if len(margins) < MIN_COUPONS:
                    continue
                m = _median(margins)
                spread = _median([abs(x - m) for x in margins])
                if best is None or spread < best[0]:
                    best = (spread, base, lag, window, m, len(margins))
    if best is None:
        return None
    spread, base, lag, window, m, n = best
    return {"isin": isin, "name": row.get("short_name"), "base": base, "lag": lag,
            "window": window, "margin_bps": round(m * 100 / 5) * 5,
            "margin_raw": round(m * 100, 1), "spread_pp": round(spread, 4),
            "n": n, "period": period}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("isins", nargs="*")
    ap.add_argument("--limit", type=int, default=40, help="сколько ликвидных проверить")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from services import instruments_registry as reg
    from services.market_data import MarketDataService

    if args.isins:
        targets = [(i, reg.get(i) or {}) for i in args.isins]
    else:
        inc = reg.list_incomplete()
        snap = await MarketDataService.fetch_board_snapshot()
        with_vol = [(float((snap.get(r["isin"]) or {}).get("vol") or 0), r) for r in inc]
        with_vol.sort(reverse=True, key=lambda x: x[0])
        targets = [(r["isin"], reg.get(r["isin"]) or {}) for _, r in with_vol[:args.limit]]

    print(f"кандидатов: {len(targets)}\n")
    today = date.today()
    applied = 0
    for isin, row in targets:
        try:
            r = await infer_one(isin, row, today)
        except Exception as e:
            print(f"{isin}: ошибка {type(e).__name__}: {e}")
            continue
        if r is None:
            print(f"{isin} {str(row.get('short_name') or '')[:14]:14s}: мало выплаченных купонов")
            continue
        good = r["spread_pp"] < TOL_SPREAD_PP
        w = f"окно{r['window']}" if r["window"] else "окно=период"
        print(f"{isin} {str(r['name'] or '')[:14]:14s} n={r['n']}: "
              f"{r['base']}·лаг{r['lag']}·{w} маржа {r['margin_raw']}пп → {r['margin_bps']}bps, "
              f"разброс {r['spread_pp']}пп — {'ПРИМЕНИТЬ' if good else 'не флоатер / нестабильно'}")
        if args.apply and good:
            params = {"base": r["base"], "margin_bps": r["margin_bps"],
                      "coupon_mode": "average", "fixing_lag": r["lag"],
                      "fixing_lag_unit": "cal"}
            if r["window"]:
                params["avg_window_days"] = r["window"]
            reg.set_manual(isin, params, lock=True)
            applied += 1
    print(f"\nприменено: {applied}" + ("" if args.apply else " (dry-run: --apply)"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
