"""Материализация спек фиксинга в реестр: по каждой прайсуемой бумаге резолвим
спеку (парсер проспекта > калибратор), бэктестим по прошлым купонам и ПИШЕМ
подтверждённые поля (coupon_mode/fixing_lag/fixing_lag_unit/cap_pct/floor_pct)
в реестр через set_manual(lock=True).

Критерий записи:
  - спека есть (mode от парсера или калибратора) И
  - бэктест mean|err| < TOL_WRITE_PP (0.15) на ≥1 купоне, ЛИБО прошлых купонов
    нет вовсе (новая бумага) и режим дал ПАРСЕР (авторитетный источник).
  Всё остальное (WARN/BAD/спеки нет) — пропуск, в отчёт.

КАВЕАТ (ловушка заморозки, см. scripts/unfreeze_fixing_spec.py): записанные поля
читаются ref_data.coupon_formula РАНЬШЕ парсера — будущие правки парсера на
записанные бумаги не действуют, пока не перегнать этот скрипт заново.

Запуск: dry-run по умолчанию; APPLY=1 — запись.
    python3 -m scripts.fill_fixing_specs
    APPLY=1 python3 -m scripts.fill_fixing_specs
"""
import asyncio
import csv
import os
import sys
from datetime import date

TOL_WRITE_PP = 0.15
MAX_PAST = 6


def resolve_and_backtest(row, full, today) -> dict:
    """Спека + бэктест одной бумаги (та же логика, что verify_fixing_specs)."""
    from services.ref_data import coupon_formula
    from services.coupon_calib import (_past_rows, _index, _realized,
                                       projected_ks_pct, fixing_probe_date,
                                       parse_prospectus_formula)
    from services.ref_data import params

    isin = row["isin"]
    base = row["base"]
    margin_pct = (row["margin_bps"] or 0) / 100.0
    face = row["face_value"] or 1000.0
    coupons = (full or {}).get("coupons") or []
    amorts = (full or {}).get("amorts") or []

    spec = coupon_formula(isin, coupons=coupons, margin_pct=margin_pct,
                          face=face, calc_date=today, amorts=amorts)
    p = params(isin)
    ps = parse_prospectus_formula(p.get("coupon_text") or "") or {}
    mode_src = ("parser" if ps.get("mode") is not None
                else "calibrator" if spec.get("coupon_mode") is not None else None)

    out = {"isin": isin, "name": row["short_name"], "base": base,
           "mode": spec.get("coupon_mode"), "lag": spec.get("fixing_lag"),
           "lag_unit": spec.get("fixing_lag_unit"),
           "cap_pct": spec.get("cap_pct"), "floor_pct": spec.get("floor_pct"),
           "mode_src": mode_src, "n_cpn": 0, "mean_err_pp": None,
           "action": "SKIP", "reason": ""}

    if spec.get("coupon_mode") is None:
        out["reason"] = "спеки нет (парсер молчит, калибратор не прошёл порог)"
        return out

    mode = spec["coupon_mode"]
    lag = spec.get("fixing_lag") if spec.get("fixing_lag") is not None else 0
    unit = spec.get("fixing_lag_unit") or "cal"

    # fix-to-float прелюдия — как в стенде
    prc = [c.get("valueprc") for c in coupons]
    lead = 0
    while lead + 1 < len(prc) and prc[lead] is not None and prc[lead] == prc[lead + 1]:
        v = prc[lead]
        while lead < len(prc) and prc[lead] == v:
            lead += 1
    cps_bt = coupons[lead:] if lead >= 1 else coupons

    # маржа-лесенка: как в verify_fixing_specs — бэктест только по плавающим
    # диапазонам, предсказание с маржой своей ступени
    ms = spec.get("margin_schedule")
    ord_by_start = {}
    if ms:
        def _ck(c):
            v = c.get("start") or ""
            return v if isinstance(v, str) else v.isoformat()
        for i, c in enumerate(sorted(coupons, key=_ck)):
            s0 = c.get("start")
            if isinstance(s0, str):
                try:
                    s0 = date.fromisoformat(s0)
                except ValueError:
                    continue
            ord_by_start[s0] = i + 1

    def _margin_for(s):
        o = ord_by_start.get(s)
        if o is not None:
            for st in ms or []:
                if st["from"] <= o <= st["to"]:
                    return st["bps"] / 100.0
        return margin_pct

    rows_past = _past_rows(cps_bt, margin_pct, face, today, amorts)[-MAX_PAST:]
    if ms:
        flo = {o for st in ms for o in range(st["from"], st["to"] + 1)}
        rows_past = [r for r in rows_past if ord_by_start.get(r[0]) in flo]
    idx = _index(base)
    pspec = {"mode": mode, "lag": lag, "lag_unit": unit, "base": base}
    errs = []
    if rows_past and idx and idx[0]:
        for s, e, obs in rows_past:
            probe = fixing_probe_date(pspec, s)
            if not _realized(idx, probe, today):
                continue
            pred = projected_ks_pct(pspec, s, e, today, fwd_pct=lambda d: None, idx=idx)
            if pred is None or (pred == 0.0 and mode == "point"):
                continue
            pred_full, obs_full = pred + _margin_for(s), obs + margin_pct
            cap, floor = spec.get("cap_pct"), spec.get("floor_pct")
            if cap is not None:
                pred_full = min(pred_full, float(cap))
            if floor is not None:
                pred_full = max(pred_full, float(floor))
            errs.append(pred_full - obs_full)

    if errs:
        mean_abs = sum(abs(x) for x in errs) / len(errs)
        out["n_cpn"] = len(errs)
        out["mean_err_pp"] = round(mean_abs, 3)
        if mean_abs < TOL_WRITE_PP:
            out["action"] = "WRITE"
        else:
            out["reason"] = f"бэктест не подтверждает (mean {mean_abs:.3f}пп)"
    else:
        # прошлых купонов нет (новая бумага / вся история — фикс-прелюдия)
        if mode_src == "parser":
            out["action"] = "WRITE"
            out["reason"] = "без бэктеста: купонов нет, режим от парсера"
        else:
            out["reason"] = "нет прошлых купонов и режим не от парсера"
    return out


async def main():
    from services import instruments_registry as reg
    from services.market_data import MarketDataService as M

    apply = os.environ.get("APPLY") == "1"
    today = date.today()
    rows = [reg.get(r["isin"]) for r in reg.universe_rows(only_priceable=True)]
    print(f"прайсуемых флоатеров: {len(rows)}; APPLY={'1' if apply else '0 (dry-run)'}")

    fulls = await asyncio.gather(
        *(M.fetch_bond_schedule_full(r["isin"]) for r in rows), return_exceptions=True)
    M.flush_schedule_cache()

    results = []
    for r, f in zip(rows, fulls):
        if isinstance(f, Exception):
            f = {}
        try:
            results.append(resolve_and_backtest(r, f, today))
        except Exception as e:
            results.append({"isin": r["isin"], "name": r["short_name"],
                            "base": r["base"], "mode": None, "lag": None,
                            "lag_unit": None, "cap_pct": None, "floor_pct": None,
                            "mode_src": None, "n_cpn": 0, "mean_err_pp": None,
                            "action": "ERROR", "reason": str(e)[:80]})

    to_write = [x for x in results if x["action"] == "WRITE"]
    skipped = [x for x in results if x["action"] == "SKIP"]
    errors = [x for x in results if x["action"] == "ERROR"]
    print(f"\nзапись: {len(to_write)}; пропуск: {len(skipped)}; ошибки: {len(errors)}")

    print("\n=== ПРОПУЩЕННЫЕ (спека не подтверждена — остаются на живом резолве) ===")
    for x in skipped:
        print(f"  {x['isin']} {str(x['name'])[:22]:22s} {x['base'] or '':7s} {x['reason']}")
    for x in errors:
        print(f"  ERROR {x['isin']} {str(x['name'])[:22]:22s} {x['reason']}")

    if apply:
        for x in to_write:
            fields = {"coupon_mode": x["mode"], "fixing_lag": x["lag"],
                      "fixing_lag_unit": x["lag_unit"]}
            if x["cap_pct"] is not None:
                fields["cap_pct"] = x["cap_pct"]
            if x["floor_pct"] is not None:
                fields["floor_pct"] = x["floor_pct"]
            reg.set_manual(x["isin"], fields, lock=True)
        print(f"\nзаписано в реестр (lock=True): {len(to_write)}")

    out_csv = os.environ.get("FILL_OUT", "/tmp/fill_fixing_specs.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    print(f"CSV: {out_csv}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
