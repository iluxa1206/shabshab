"""Мониторинг дрейфа наших метрик против публикаций НРД.

Зачем: KEYRATE-путь z-спреда — реверс-инжиниринг публикаций НРД (zspread.py),
SM/DM калиброваны сверками. НРД может молча поменять модель — без монитора наш
расчёт уедет и никто не заметит. Считаем по всему юниверсу медиану и mean|Δ|
(наша метрика − НРД) для SM/DM/z, складываем в market_cache['nrd_drift'],
алерт в лог + warning в /meta при превышении порогов.

Нюанс: наши метрики на live/prev цене, НРД — на их wa_price → часть Δ ценовая.
Медиана по юниверсу (сотни бумаг) устойчива к ценовому шуму; пороги щедрые —
ловим методический сдвиг, не тик."""
from __future__ import annotations
from datetime import datetime
from typing import Optional

# Пороги |медианы Δ|, bps: выше — WARNING (подобраны от текущих сверок:
# SM ≈ 0-30, DM ≈ 20-50, z ≈ 40-90 медианно; порог = «что-то сломалось»)
THRESHOLDS = {"sm": 75, "dm": 120, "z": 200}


def _med(vals: list) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _agg(deltas: list) -> Optional[dict]:
    if not deltas:
        return None
    return {"n": len(deltas),
            "median": round(_med(deltas), 1),
            "mean_abs": round(sum(abs(d) for d in deltas) / len(deltas), 1)}


def compute_nrd_drift(universe: list, uni_metrics: dict) -> dict:
    """universe — items НРД-юниверса (simple_margin_bps/discount_margin_bps/
    z_spread_bps); uni_metrics — {isin: {dm, disc_dm, z_model}} из
    compute_universe_metrics. Возвращает срез дрейфа + список алертов."""
    pairs = {"sm": [], "dm": [], "z": []}   # (Δ = наш − НРД), bps
    by_base = {"KEYRATE": {"sm": [], "dm": [], "z": []},
               "RUONIA": {"sm": [], "dm": [], "z": []}}
    for u in universe:
        isin = u.get("isin")
        m = uni_metrics.get(isin)
        if not m:
            continue
        base = u.get("base_rate_type")
        for key, ours_field, nrd_field in (("sm", "dm", "simple_margin_bps"),
                                           ("dm", "disc_dm", "discount_margin_bps"),
                                           ("z", "z_model", "z_spread_bps")):
            ours, nrd = m.get(ours_field), u.get(nrd_field)
            if ours is None or nrd is None:
                continue
            d = float(ours) - float(nrd)
            pairs[key].append(d)
            if base in by_base:
                by_base[base][key].append(d)

    alerts = []
    stats = {}
    for key in ("sm", "dm", "z"):
        stats[key] = _agg(pairs[key])
        stats[f"{key}_by_base"] = {b: _agg(v[key]) for b, v in by_base.items() if v[key]}
        a = stats[key]
        if a and abs(a["median"]) > THRESHOLDS[key]:
            alerts.append(f"NRD drift {key.upper()}: median Δ {a['median']:+.0f} bps "
                          f"(порог {THRESHOLDS[key]}, n={a['n']}) — проверь методику/кривые")
    for msg in alerts:
        print(f"WARNING: {msg}")
    return {"ts": datetime.now().isoformat(timespec="seconds"),
            "stats": stats, "alerts": alerts}
