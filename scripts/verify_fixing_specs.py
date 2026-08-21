"""Верификация спек фиксинга по ФАКТУ выплат: бэктест mode/lag/периода.

Для каждого прайсуемого флоатера:
  1) резолвим эффективную спеку (как прод: manual > парсер проспекта > калибратор,
     ref_data.coupon_formula) и фиксируем, какой слой дал mode/lag;
  2) по каждому ПРОШЛОМУ зафиксированному купону считаем предсказанную ставку
     индекса нашей спекой (projected_ks_pct на реальной истории КС/RUONIA) и
     сравниваем с наблюдённой (value·365/(days·face) − маржа, _past_rows);
  3) сверяем coupon_period_days реестра с фактическим шагом графика.

Ошибка ≈ 0 ⇒ лаг/режим/маржа согласованы с реальными выплатами. Систематика
>0.15 пп — подозрение на неверный лаг/режим; >0.5 пп — почти наверняка.

Запуск (реестр можно подменить прод-копией):
  INSTRUMENTS_DB=/path/to/instruments_prod.db python3 -m scripts.verify_fixing_specs
"""
import asyncio
import csv
import os
import sys
from datetime import date

TOL_OK_PP = 0.15
TOL_BAD_PP = 0.50
MAX_PAST = 6          # последних зафиксированных купонов на бумагу


def resolve_spec_with_source(isin: str, coupons, margin_pct, face, today, amorts):
    """Эффективная спека + провенанс mode/lag (какой слой дал значение)."""
    from services.ref_data import params, coupon_formula
    from services.coupon_calib import parse_prospectus_formula

    p = params(isin)
    spec = coupon_formula(isin, coupons=coupons, margin_pct=margin_pct,
                          face=face, calc_date=today, amorts=amorts)
    src_mode = src_lag = None
    if p.get("coupon_mode") is not None:
        src_mode = "manual/db"
    if p.get("fixing_lag") is not None:
        src_lag = "manual/db"
    if (src_mode is None or src_lag is None) and p.get("coupon_text"):
        ps = parse_prospectus_formula(p["coupon_text"]) or {}
        if src_mode is None and ps.get("mode") is not None:
            src_mode = "parser"
        if src_lag is None and ps.get("lag") is not None:
            src_lag = "parser"
    if src_mode is None and spec.get("coupon_mode") is not None:
        src_mode = "calibrator"
    if src_lag is None and spec.get("fixing_lag") is not None:
        src_lag = "calibrator"
    if spec.get("coupon_mode") is None:
        src_mode = "default(point,lag0)"
    return spec, src_mode or "none", src_lag or "none"


def backtest_bond(row, full, today) -> dict:
    """Бэктест одной бумаги. Возвращает dict для отчёта."""
    from services.coupon_calib import (_past_rows, _index, _realized,
                                       projected_ks_pct, fixing_probe_date)
    from core.cashflow import coupon_period_from_coupons

    isin = row["isin"]
    base = row["base"]
    margin_pct = (row["margin_bps"] or 0) / 100.0
    face = row["face_value"] or 1000.0
    coupons = (full or {}).get("coupons") or []
    amorts = (full or {}).get("amorts") or []

    out = {"isin": isin, "name": row["short_name"], "base": base,
           "margin_bps": row["margin_bps"], "n_cpn": 0, "mean_err_pp": None,
           "max_err_pp": None, "mode": None, "lag": None, "lag_unit": None,
           "mode_src": None, "lag_src": None, "period_db": row["coupon_period_days"],
           "period_fact": None, "period_mismatch": False, "verdict": "NO_DATA",
           "fix_prelude": 0}

    # период: реестр vs факт графика
    pf = coupon_period_from_coupons(coupons, issue_date=row["issue_date"], today=today)
    out["period_fact"] = pf
    pdb = row["coupon_period_days"]
    if pf and pdb:
        out["period_mismatch"] = abs(pf - pdb) > 3   # 3 дня — бизнес-сдвиги дат

    spec, src_mode, src_lag = resolve_spec_with_source(
        isin, coupons, margin_pct, face, today, amorts)
    mode = spec.get("coupon_mode") or "point"
    lag = spec.get("fixing_lag") if spec.get("fixing_lag") is not None else 0
    unit = spec.get("fixing_lag_unit") or "cal"
    out.update({"mode": mode, "lag": lag, "lag_unit": unit,
                "mode_src": src_mode, "lag_src": src_lag})

    # fix-to-float: ведущий блок купонов с ОДИНАКОВОЙ фиксированной ставкой
    # («1-6 купоны — 12.8% годовых») — не флоатер-режим, спека к ним не
    # относится; бэктест по ним дал бы ложную ошибку. Срезаем прелюдию.
    # обобщение на лесенку («1-2 — 12.75%, 3-4 — 9.85%, …»): срезаем ведущие
    # БЛОКИ из ≥2 подряд одинаковых ставок, сколько бы блоков ни было
    prc = [c.get("valueprc") for c in coupons]
    lead = 0
    while lead + 1 < len(prc) and prc[lead] is not None and prc[lead] == prc[lead + 1]:
        v = prc[lead]
        while lead < len(prc) and prc[lead] == v:
            lead += 1
    cps_bt = coupons[lead:] if lead >= 1 else coupons
    if lead >= 1:
        out["fix_prelude"] = lead

    # RUONIA без спеки: прод не применяет point-фолбэк (форвард-проекция);
    # ближайший офлайн-прокси — average lag 0
    if base == "RUONIA" and spec.get("coupon_mode") is None:
        mode, lag, unit = "average", 0, "cal"
        out.update({"mode": mode, "lag": lag, "lag_unit": unit,
                    "mode_src": "default(fwd~avg)"})

    # маржа-лесенка: надбавка периода по № купона (полный график MOEX, 1-based);
    # вне диапазонов — скаляр реестра
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

    rows_past = _past_rows(cps_bt, margin_pct, face, today, amorts)[-MAX_PAST:]
    # лесенка задаёт, КАКИЕ купоны плавающие: фикс-ступени вне диапазонов
    # («1-16 купоны - X% годовых» ТрансФин-М) — не флоатер, из бэктеста вон
    if ms:
        flo = {o for st in ms for o in range(st["from"], st["to"] + 1)}
        rows_past = [r for r in rows_past if ord_by_start.get(r[0]) in flo]
    if not rows_past:
        return out
    idx = _index(base)
    if not idx or not idx[0]:
        return out

    # compounded ОБЯЗАН ехать в спеку бэктеста: у индексных RUONIA-купонов
    # (ВЭБ.РФ, Роснефть, ОФЗ 29026+) ставка это отношение накопленных индексов,
    # а не среднее арифметическое дневных. Без флага прибор мерил их простым
    # средним и рисовал ложные WARN 0.28–0.39 пп там, где прод считает верно —
    # то есть звал чинить работающее.
    pspec = {"mode": mode, "lag": lag, "lag_unit": unit, "base": base,
             "avg_window_days": spec.get("avg_window_days"),
             "compounded": spec.get("compounded")}

    def _margin_for(s):
        o = ord_by_start.get(s)
        if o is not None:
            for st in ms or []:
                if st["from"] <= o <= st["to"]:
                    return st["bps"] / 100.0
        return margin_pct

    errs = []
    for s, e, obs in rows_past:
        # период считаем только если окно фиксинга целиком покрыто историей —
        # fwd-протечка сделала бы «ошибку» из дыры данных
        probe = fixing_probe_date(pspec, s)
        if not _realized(idx, probe, today):
            continue
        pred = projected_ks_pct(pspec, s, e, today, fwd_pct=lambda d: None, idx=idx)
        if pred is None or pred == 0.0 and mode == "point":
            continue
        # кэп/флор применяются к ПОЛНОЙ ставке купона (индекс+маржа): сравниваем
        # клэмпнутые полные ставки, иначе капнутая бумага даёт ложную ошибку.
        # obs + margin_pct = фактическая полная ставка купона (обе на скаляре);
        # предсказание — с маржой лесенки своего периода.
        pred_full, obs_full = pred + _margin_for(s), obs + margin_pct
        cap, floor = spec.get("cap_pct"), spec.get("floor_pct")
        if cap is not None:
            pred_full = min(pred_full, float(cap))
        if floor is not None:
            pred_full = max(pred_full, float(floor))
        errs.append(pred_full - obs_full)
    if not errs:
        return out
    out["n_cpn"] = len(errs)
    mean_abs = sum(abs(x) for x in errs) / len(errs)
    out["mean_err_pp"] = round(mean_abs, 3)
    out["max_err_pp"] = round(max(abs(x) for x in errs), 3)
    out["verdict"] = ("OK" if mean_abs < TOL_OK_PP
                      else "WARN" if mean_abs < TOL_BAD_PP else "BAD")
    return out


async def main():
    from services import instruments_registry as reg
    from services.market_data import MarketDataService as M

    today = date.today()
    rows = [reg.get(r["isin"]) for r in reg.universe_rows(only_priceable=True)]
    print(f"прайсуемых флоатеров: {len(rows)}; реестр: {os.environ.get('INSTRUMENTS_DB', 'data/instruments.db')}")

    fulls = await asyncio.gather(
        *(M.fetch_bond_schedule_full(r["isin"]) for r in rows), return_exceptions=True)
    M.flush_schedule_cache()

    results = []
    for r, f in zip(rows, fulls):
        if isinstance(f, Exception):
            f = {}
        try:
            results.append(backtest_bond(r, f, today))
        except Exception as e:
            results.append({"isin": r["isin"], "name": r["short_name"], "verdict": "ERROR",
                            "mean_err_pp": None, "max_err_pp": None, "n_cpn": 0,
                            "mode": None, "lag": None, "lag_unit": None, "base": r["base"],
                            "margin_bps": r["margin_bps"], "mode_src": str(e)[:60],
                            "lag_src": "", "period_db": None, "period_fact": None,
                            "period_mismatch": False})

    by_verdict = {}
    for x in results:
        by_verdict.setdefault(x["verdict"], []).append(x)
    print("\n=== ВЕРДИКТЫ ===")
    for v in ("OK", "WARN", "BAD", "NO_DATA", "ERROR"):
        print(f"  {v:8s} {len(by_verdict.get(v, [])):4d}")

    print("\n=== ИСТОЧНИК mode (по бумагам с бэктестом) ===")
    src_stat = {}
    for x in results:
        if x["n_cpn"]:
            k = (x["mode_src"], x["verdict"])
            src_stat[k] = src_stat.get(k, 0) + 1
    for (src, v), n in sorted(src_stat.items()):
        print(f"  {src:22s} {v:6s} {n:4d}")

    pm = [x for x in results if x.get("period_mismatch")]
    print(f"\n=== ПЕРИОД: реестр vs факт — расхождений {len(pm)} ===")
    for x in pm[:15]:
        print(f"  {x['isin']} {str(x['name'])[:24]:24s} db={x['period_db']} fact={x['period_fact']}")

    bad = sorted((x for x in results if x["verdict"] in ("BAD", "WARN")),
                 key=lambda x: -(x["mean_err_pp"] or 0))
    print(f"\n=== ХУДШИЕ (WARN+BAD) — топ 25 из {len(bad)} ===")
    print(f"  {'ISIN':14s} {'имя':20s} {'база':7s} {'mode':8s} lag  {'src':18s} n  mean  max")
    for x in bad[:25]:
        print(f"  {x['isin']:14s} {str(x['name'])[:20]:20s} {x['base']:7s} "
              f"{x['mode']:8s} {x['lag']:>2}{x['lag_unit'][0]} {x['mode_src']:18s} "
              f"{x['n_cpn']}  {x['mean_err_pp']}  {x['max_err_pp']}")

    out_csv = os.environ.get("VERIFY_OUT", "/tmp/verify_fixing_specs.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    print(f"\nCSV: {out_csv}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
