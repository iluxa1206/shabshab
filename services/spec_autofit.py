"""Автоподбор спеки фиксинга по ФАКТУ выплат — ядро, общее для CLI и синка.

Grid search lag × окно по прошлым купонам: ищем комбинацию с минимальной
медианной |ошибкой| пересчёта. Применяем ТОЛЬКО подтверждённый фит — сошёлся и
на подборе, и на отложенных купонах (hold-out), и данных хватило. Систематика,
которую не лечит ни один лаг, спекой не маскируется: это другая причина
(смещённая маржа, капитализация индекса, битые данные) — такую бумагу оставляем
помеченной для разбора руками.

Зовётся из ежедневного синка (instruments_sync, шаг 8b) и из CLI
scripts/fit_spec.py.
"""
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

FIT_TOL_PP = 0.15          # порог «сошлось» (= TOL_OK_PP вердикта)
MIN_GAIN_PP = 0.05         # минимальный выигрыш против текущей спеки
MIN_COUPONS = 5            # меньше — статистики нет, любой лаг «подберётся»
HOLDOUT = 2                # последние N купонов не участвуют в подборе
LAGS = list(range(0, 46))
EXTRA_WINDOWS = [1, 7, 30, 31, 91, 182]


def _median(vals):
    a = sorted(vals)
    n = len(a)
    if not n:
        return None
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2


async def fit_one(isin: str, row: dict, today: date) -> dict | None:
    from services.market_data import MarketDataService
    from services.coupon_calib import (_past_rows, _index, _realized,
                                       projected_ks_pct, fixing_probe_date)
    from services.ref_data import coupon_formula

    base = row.get("base")
    if base not in ("KEYRATE", "RUONIA"):
        return None
    full = await MarketDataService.fetch_bond_schedule_full(isin)
    coupons = (full or {}).get("coupons") or []
    amorts = (full or {}).get("amorts") or []
    if not coupons:
        return None
    margin_pct = (row.get("margin_bps") or 0) / 100.0
    face = row.get("face_value") or 1000.0
    period = row.get("coupon_period_days")

    # те же прошлые купоны, что видит вердикт (с отсечкой fix-to-float прелюдии)
    prc = [c.get("valueprc") for c in coupons]
    lead = 0
    while lead + 1 < len(prc) and prc[lead] is not None and prc[lead] == prc[lead + 1]:
        v = prc[lead]
        while lead < len(prc) and prc[lead] == v:
            lead += 1
    cps = coupons[lead:] if lead >= 1 else coupons
    rows_past = _past_rows(cps, margin_pct, face, today, amorts)[-8:]
    # битые строки MOEX (value ≈ 0 при живой базе) в подбор не берём
    rows_past = [(s, e, o) for s, e, o in rows_past if o + margin_pct > 1.0]
    if len(rows_past) < 2:
        return None
    idx = _index(base)
    if not idx or not idx[0]:
        return None

    # None = окно длиной в купонный период (дефолт average); остальные — явные
    windows = [None] + sorted({int(w) for w in EXTRA_WINDOWS + [period] if w})

    def err_on(sample, lag, window, min_pts=2):
        spec = {"mode": "average", "lag": lag, "lag_unit": "cal", "base": base,
                "avg_window_days": window}
        es = []
        for s, e, obs in sample:
            if not _realized(idx, fixing_probe_date(spec, s), today):
                continue
            p = projected_ks_pct(spec, s, e, today, fwd_pct=lambda d: None, idx=idx)
            if p is None or p == 0.0:
                continue
            es.append(abs(p - obs))
        if len(es) < min_pts:
            return None
        return _median(es)

    cur = coupon_formula(isin, coupons=coupons, margin_pct=margin_pct, face=face,
                         calc_date=today, amorts=amorts)
    cur_err = err_on(rows_past, cur.get("fixing_lag") or 0, cur.get("avg_window_days"))

    # HOLD-OUT: подбираем на ранних купонах, проверяем на последних HOLDOUT.
    # Без этого grid search «находит» лаг, который просто повторяет шум:
    # у серии ВЭБ2Р подбор давал 31/33/34/38 дней на 2-4 купонах — конвенция
    # выпуска одна, значит это подгонка, а не спека.
    train, test = rows_past[:-HOLDOUT], rows_past[-HOLDOUT:]
    best = None
    for w in windows:
        for lag in LAGS:
            e = err_on(train, lag, w)
            if e is None:
                continue
            if best is None or e < best[0]:
                best = (e, lag, w)
    if best is None:
        return None
    test_err = err_on(test, best[1], best[2], min_pts=1)
    full_err = err_on(rows_past, best[1], best[2])

    # Диагностика «не лечится лагом»: ЗНАКОВАЯ медиана ошибки при лучшем фите.
    # Если она ≈ модулю ошибки, промах систематический в одну сторону — это не
    # лаг, а смещённая МАРЖА выпуска (или кэп/капитализация), и её величина в пп
    # прямо даёт поправку margin_bps.
    spec_b = {"mode": "average", "lag": best[1], "lag_unit": "cal", "base": base,
              "avg_window_days": best[2]}
    signed = []
    for s, e, obs in rows_past:
        if not _realized(idx, fixing_probe_date(spec_b, s), today):
            continue
        p = projected_ks_pct(spec_b, s, e, today, fwd_pct=lambda d: None, idx=idx)
        if p is None or p == 0.0:
            continue
        signed.append(p - obs)
    med_signed = _median(signed) if signed else None
    return {"isin": isin, "name": row.get("short_name"), "base": base,
            "n": len(rows_past), "period": period,
            "cur_lag": cur.get("fixing_lag"), "cur_window": cur.get("avg_window_days"),
            "cur_err": None if cur_err is None else round(cur_err, 3),
            "fit_err": None if full_err is None else round(full_err, 3),
            "train_err": round(best[0], 3),
            "test_err": None if test_err is None else round(test_err, 3),
            "fit_lag": best[1], "fit_window": best[2],
            "med_signed": None if med_signed is None else round(med_signed, 3),
            "margin_bps": row.get("margin_bps")}


def verdict(r: dict) -> tuple:
    """(применять ли, причина) по результату fit_one — общая для CLI и автофита."""
    gain = (r["cur_err"] or 99) - (r["fit_err"] if r["fit_err"] is not None else 99)
    ok_fit = r["fit_err"] is not None and r["fit_err"] < FIT_TOL_PP
    ok_test = r["test_err"] is not None and r["test_err"] < FIT_TOL_PP
    enough = r["n"] >= MIN_COUPONS
    good = ok_fit and ok_test and enough and gain >= MIN_GAIN_PP
    why = ("ПРИМЕНИТЬ" if good
           else "мало купонов" if not enough
           else "не сошлось" if not ok_fit
           else "подгонка (hold-out хуже)" if not ok_test
           else "выигрыш мал")
    return good, why


def margin_hint(r: dict) -> Optional[int]:
    """Систематический знаковый сдвиг во ВСЕХ купонах, который не лечится лагом,
    выдаёт смещённую МАРЖУ — возвращает предполагаемое значение в bps."""
    if (r.get("med_signed") is None or r.get("fit_err") is None
            or abs(r["med_signed"]) <= 0.1
            or abs(abs(r["med_signed"]) - r["fit_err"]) >= 0.05):
        return None
    return (r.get("margin_bps") or 0) - round(r["med_signed"] * 100)


async def autofit(isins: Optional[list] = None, apply: bool = False,
                  limit: Optional[int] = None) -> dict:
    """Подобрать и (при apply) применить спеку для бумаг с расходящимся вердиктом.

    Возвращает {"checked", "applied", "isins": [...], "suspect_margin": [...]}:
    список применённых нужен вызывающему, чтобы пересчитать их историю — без
    этого правка меняет только витрину, а график остаётся на старых числах.
    """
    from services import instruments_registry as reg

    if isins:
        targets = [(i, reg.get(i) or {}) for i in isins]
    else:
        targets = [(r["isin"], reg.get(r["isin"]) or {}) for r in reg.list_spec_mismatch()]
    if limit:
        targets = targets[:limit]

    today = date.today()
    out = {"checked": 0, "applied": 0, "isins": [], "suspect_margin": []}
    for isin, row in targets:
        try:
            r = await fit_one(isin, row, today)
        except Exception as e:
            logger.debug("fit_one %s: %s", isin, e)
            continue
        if r is None:
            continue
        out["checked"] += 1
        good, why = verdict(r)
        if good:
            logger.info("spec autofit %s %s: err %s → %s (hold-out %s) — %s",
                        isin, r["name"], r["cur_err"], r["fit_err"], r["test_err"],
                        "применено" if apply else "dry-run")
            if apply:
                params = {"coupon_mode": "average", "fixing_lag": r["fit_lag"],
                          "fixing_lag_unit": "cal"}
                if r["fit_window"]:
                    params["avg_window_days"] = r["fit_window"]
                reg.set_manual(isin, params, lock=True)
                out["applied"] += 1
                out["isins"].append(isin)
        else:
            hint = margin_hint(r)
            if hint is not None:
                out["suspect_margin"].append((isin, r["name"], r["margin_bps"], hint))
                logger.info("spec autofit %s %s: лагом не лечится, похоже смещена "
                            "МАРЖА %s → ~%s bps (%s)", isin, r["name"],
                            r["margin_bps"], hint, why)
    return out
