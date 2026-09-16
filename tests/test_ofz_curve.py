"""Своя кривая ОФЗ (services/ofz_curve): чистая подгонка NSS/NS без сети.

Синтетика NSS восстанавливается в 1 бп, выброс не тащит кривую (Хьюбер),
мало точек — исключение, на 6 точках — NS-фолбэк. Плюс ручка /ofz/curve на
подменённом кэше метрик (без сети, база — tmp).
"""
import asyncio

import numpy as np
import pytest

from services import ofz_curve as oc


TRUE = {"beta0": 12.5, "beta1": 4.0, "beta2": -6.0, "beta3": 3.0,
        "lambda1": 1.7, "lambda2": 9.0}
TAUS = [0.4, 0.7, 1.0, 1.4, 1.9, 2.5, 3.2, 4.0, 4.9, 5.8, 6.7, 7.5, 8.4, 9.3, 10.5, 12.0]


def _pts(params=TRUE, taus=TAUS, noise=0.0, seed=1):
    rng = np.random.default_rng(seed)
    out = []
    for i, t in enumerate(taus):
        y = float(oc.evaluate(params, t)) + (rng.normal(0, noise) if noise else 0.0)
        out.append(oc.FitPoint(f"RU{i:010d}", y, t, turnover=50e6))
    return out


def test_evaluate_matches_formula():
    t = 2.0
    l1, l2 = TRUE["lambda1"], TRUE["lambda2"]
    f1 = (1 - np.exp(-t / l1)) / (t / l1)
    f2 = f1 - np.exp(-t / l1)
    f3 = (1 - np.exp(-t / l2)) / (t / l2) - np.exp(-t / l2)
    exp = TRUE["beta0"] + TRUE["beta1"] * f1 + TRUE["beta2"] * f2 + TRUE["beta3"] * f3
    assert oc.evaluate(TRUE, t) == pytest.approx(exp, abs=1e-12)
    # массив — тем же путём
    assert oc.evaluate(TRUE, np.array([t, 5.0]))[0] == pytest.approx(exp, abs=1e-12)


def test_synthetic_nss_recovered_within_1bp():
    r = oc.fit(_pts())
    assert r.method == "NSS"
    assert r.n_used == len(TAUS) and r.n_total == len(TAUS)
    assert r.rmse_bps < 1.0
    # кривая, а не только точки: на промежуточных тенорах тоже в 1 бп
    for t in (0.5, 1.5, 3.0, 6.0, 11.0):
        assert abs(oc.evaluate(r.params, t) - oc.evaluate(TRUE, t)) * 100 < 1.0
    # параметры в экономических границах (коробка подгонки), не артефакт
    # коллинеарности: β0 — уровень, остальные — наклон/горбы в пп
    assert oc.BETA0_RANGE[0] <= r.params["beta0"] <= oc.BETA0_RANGE[1]
    assert all(abs(r.params[k]) <= oc.BETA_ABS_MAX for k in ("beta1", "beta2", "beta3"))
    assert r.params["lambda2"] >= oc.LAMBDA2_MIN_RATIO * r.params["lambda1"]
    assert r.domain == [min(TAUS), max(TAUS)]
    # samples: от 0.1 до τmax+0.5 шагом 0.05
    assert r.samples[0]["years"] == 0.1
    assert r.samples[-1]["years"] == pytest.approx(max(TAUS) + 0.5, abs=0.03)
    assert abs(r.samples[1]["years"] - r.samples[0]["years"] - 0.05) < 1e-9
    kt = {k["years"]: k for k in r.key_tenors}
    assert set(kt) == set(oc.KEY_TENORS)
    assert kt[15.0]["extrap"] is True and kt[5.0]["extrap"] is False
    assert all(r.residuals[p.isin]["used"] for p in _pts())


def test_synthetic_pronounced_svensson_hump():
    """Форма с выраженным вторым горбом (β3 = 8, λ2 = 6): NSS нужен по делу
    и восстанавливается в 1 бп, β — в границах."""
    t2 = dict(TRUE, beta3=8.0, lambda1=1.2, lambda2=6.0)
    r = oc.fit(_pts(params=t2, noise=0.01, seed=11))
    assert r.method == "NSS" and r.rmse_bps < 1.0
    for t in (0.5, 1.5, 3.0, 6.0, 11.0):
        assert abs(oc.evaluate(r.params, t) - oc.evaluate(t2, t)) * 100 < 1.0
    assert oc.BETA0_RANGE[0] <= r.params["beta0"] <= oc.BETA0_RANGE[1]
    assert all(abs(r.params[k]) <= oc.BETA_ABS_MAX for k in ("beta1", "beta2", "beta3"))


# 31 реальная точка ОФЗ-ПД (dev-универс 15.09.2026, база wap): (τ Маколея,
# YTM %, оборот млн ₽). Домен узкий — 0.38…6.93 лет: при 16 % даже у
# двадцатилетних дюрация ≤ 7. Именно здесь голый lstsq давал β0 = −670.
REAL_PTS = [
    (0.38, 13.26, 271.8), (1.29, 14.0, 71.3), (3.85, 15.46, 320.0), (4.71, 15.72, 3.7),
    (2.42, 14.74, 4.1), (5.32, 16.05, 11.4), (0.06, 13.61, 15.2), (3.02, 15.05, 13.6),
    (6.37, 16.12, 82.6), (1.01, 13.57, 14.9), (6.11, 15.86, 215.5), (3.9, 15.31, 278.3),
    (1.58, 14.61, 494.2), (2.32, 14.87, 107.9), (6.93, 15.92, 1899.3), (4.03, 15.26, 9.3),
    (6.3, 15.88, 45.4), (4.44, 15.79, 401.1), (2.63, 14.71, 10.3), (6.03, 16.21, 85.9),
    (4.67, 16.25, 1675.1), (5.08, 16.24, 2932.4), (5.17, 16.23, 2316.1), (5.87, 16.24, 2000.9),
    (6.02, 16.21, 893.0), (4.15, 15.92, 183.6), (5.64, 16.24, 733.8), (3.31, 15.3, 71.0),
    (4.47, 16.14, 724.5), (5.63, 16.24, 385.1), (5.9, 16.22, 891.5),
]


def test_real_points_short_domain_stays_sane():
    """На реальном облаке 0.4–7 лет: β0 в [5, 25], RMSE в рабочем диапазоне,
    экстраполяция к 10–15 годам не улетает (хвост около уровня длинного края),
    короткая бумага (τ = 0.06) — вне подгонки."""
    pts = [oc.FitPoint(f"R{i}", y, t, v * 1e6) for i, (t, y, v) in enumerate(REAL_PTS)]
    r = oc.fit(pts)
    assert r.n_used == 30 and r.n_total == 31
    assert 5.0 <= r.params["beta0"] <= 25.0
    assert all(abs(r.params[k]) <= oc.BETA_ABS_MAX for k in ("beta1", "beta2", "beta3"))
    assert 5.0 < r.rmse_bps < 30.0
    kt = {k["years"]: k for k in r.key_tenors}
    assert kt[7.0]["extrap"] is True and kt[5.0]["extrap"] is False
    for t in (7.0, 10.0, 15.0):
        assert 14.0 < kt[t]["yield_pct"] < 18.5
    # внутри домена кривая проходит сквозь облако: 1Y ≈ 13.6–14.2, 5Y ≈ 15.9–16.4
    assert 13.4 < kt[1.0]["yield_pct"] < 14.3
    assert 15.8 < kt[5.0]["yield_pct"] < 16.5
    assert r.residuals["R6"]["used"] is False and r.residuals["R6"]["bps"] is not None


def test_outlier_does_not_drag_curve():
    pts = _pts(noise=0.01, seed=7)          # 1 бп шума — как живой рынок
    victim = pts[7]                          # τ = 4.0, середина облака
    pts[7] = oc.FitPoint(victim.isin, victim.ytm + 1.5, victim.tau, turnover=victim.turnover)
    r = oc.fit(pts)
    shift = abs(oc.evaluate(r.params, victim.tau) - oc.evaluate(TRUE, victim.tau)) * 100
    assert shift < 15.0
    res = r.residuals[victim.isin]
    assert res["used"] is True and res["bps"] > 100     # выброс виден в остатке
    assert res["weight"] < r.residuals[pts[6].isin]["weight"]   # и придавлен весом


def test_too_few_points_raises():
    with pytest.raises(ValueError):
        oc.fit(_pts(taus=[1.0, 2.0, 3.0]))
    # короткие и мусорные не считаются
    pts = _pts(taus=[1.0, 2.0, 3.0]) + [oc.FitPoint("X1", 12.0, 0.1), oc.FitPoint("X2", 55.0, 4.0),
                                        oc.FitPoint("X3", None, 4.0), oc.FitPoint("X4", 12.0, None)]
    with pytest.raises(ValueError):
        oc.fit(pts)


def test_ns_fallback_on_six_points():
    r = oc.fit(_pts(taus=[0.5, 1.5, 3.0, 5.0, 7.0, 10.0]))
    assert r.method == "NS"
    assert r.params["beta3"] == 0.0 and r.params["lambda2"] is None
    assert r.n_used == 6


def test_ns_when_nss_gain_is_small():
    """Данные из чистого NS: NSS не выигрывает ≥5 % — остаёмся на NS."""
    ns = dict(TRUE, beta3=0.0, lambda2=None)
    r = oc.fit(_pts(params=ns, noise=0.02, seed=3))
    assert r.method == "NS"
    assert r.rmse_bps < 4.0


def test_excluded_points_get_residual_but_not_used():
    pts = _pts() + [oc.FitPoint("SHORT", 12.0, 0.15), oc.FitPoint("NOYTM", None, 3.0)]
    r = oc.fit(pts)
    assert r.n_used == len(TAUS) and r.n_total == len(TAUS) + 2
    assert r.residuals["SHORT"]["used"] is False and r.residuals["SHORT"]["bps"] is not None
    assert r.residuals["NOYTM"]["used"] is False and r.residuals["NOYTM"]["bps"] is None


def test_point_weight_and_fingerprint():
    assert oc.point_weight(0) == 0.25
    assert oc.point_weight(None) == 0.25
    assert 0.9 < oc.point_weight(30e6) < 1.1
    assert oc.point_weight(1e12) == 2.0
    a = oc.input_fingerprint(_pts())
    assert a == oc.input_fingerprint(reversed(_pts()))          # порядок не важен
    moved = _pts()
    moved[0] = oc.FitPoint(moved[0].isin, moved[0].ytm + 0.01, moved[0].tau)
    assert a != oc.input_fingerprint(moved)                     # цена — важна


# ───────────────────────────── ручка /ofz/curve ─────────────────────────────

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    import services.portfolio_db as pdb
    monkeypatch.setattr(pdb, "DB_PATH", tmp_path / "t.db")
    pdb.init_db()
    return pdb


@pytest.fixture()
def live_cache(monkeypatch):
    from services import market_data as md
    uni, metrics = [], {}
    for i, p in enumerate(_pts(noise=0.01, seed=5)):
        uni.append({"isin": p.isin, "secid": f"SU{i}", "cls": "ofz", "val_today": 20e6 * (i + 1)})
        metrics[p.isin] = {"ytm_wap": p.ytm, "ytm": p.ytm + 0.02, "ytm_bid": None,
                           "mac_dur": p.tau, "mod_dur": p.tau * 0.9}
    uni.append({"isin": "RU000A10CRP1", "cls": "corp", "val_today": 1e9})
    monkeypatch.setitem(md.market_cache, "fixed_universe", uni)
    monkeypatch.setitem(md.market_cache, "fixed_metrics", metrics)
    from api.routes import fixed as fx
    fx._curve_memo.clear()
    yield uni
    fx._curve_memo.clear()


def test_curve_route_live_fits_and_archives(tmp_db, live_cache):
    from api.routes import fixed as fx
    out = asyncio.run(fx.get_ofz_curve(base="wap", date_=None))
    assert out["base"] == "wap" and out["stale"] is False and out["date"] == out["requested"]
    assert out["method"] in ("NSS", "NS") and out["n_used"] == len(TAUS)
    assert out["n_total"] == len(TAUS)          # корп в точки не попал
    assert out["rmse_bps"] < 3.0
    assert set(out["residuals"]) == {u["isin"] for u in live_cache if u["cls"] == "ofz"}
    # архив: одна строка на (дата, база), последний расчёт побеждает
    with tmp_db._connect() as c:
        rows = c.execute("SELECT date, base, method, n_used FROM ofz_curve_daily").fetchall()
    assert len(rows) == 1 and rows[0]["base"] == "wap" and rows[0]["n_used"] == len(TAUS)
    # второй запрос при том же входе — из памяти
    assert asyncio.run(fx.get_ofz_curve(base="wap", date_=None)) is out
    # другая база — другой вход (ytm по last), другой ответ, вторая строка архива
    out2 = asyncio.run(fx.get_ofz_curve(base="last", date_=None))
    assert out2 is not out and out2["base"] == "last"
    with tmp_db._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM ofz_curve_daily").fetchone()[0] == 2


def test_curve_route_422_when_too_few(tmp_db, live_cache):
    from fastapi import HTTPException
    from api.routes import fixed as fx
    with pytest.raises(HTTPException) as e:
        asyncio.run(fx.get_ofz_curve(base="bid", date_=None))     # ytm_bid нет ни у кого
    assert e.value.status_code == 422


def test_curve_route_asof_keeps_requested_date(tmp_db, monkeypatch):
    """Суббота и пятница шагают к одной фактической дате с теми же точками —
    но requested/stale в ответе свои у каждой (ключ мемо включает requested)."""
    from api.routes import fixed as fx
    items = {p.isin: {"ytm": p.ytm, "tau": p.tau, "px": 90.0, "val": 1e7, "src": "snap"}
             for p in _pts(noise=0.01, seed=9)}

    async def _asof(req):
        return {"date": "2026-09-11", "requested": req.isoformat(), "items": items}

    monkeypatch.setattr(fx, "_ofz_asof_payload", _asof)
    fx._curve_memo.clear()
    fri = asyncio.run(fx.get_ofz_curve(base="wap", date_="2026-09-11"))
    sat = asyncio.run(fx.get_ofz_curve(base="wap", date_="2026-09-12"))
    assert fri["base"] == "asof" and fri["requested"] == "2026-09-11" and fri["stale"] is False
    assert sat["date"] == "2026-09-11" and sat["requested"] == "2026-09-12" and sat["stale"] is True
    assert sat["params"] == fri["params"]          # подгонка одна и та же
    # as-of в архив «сегодня» не пишется
    with tmp_db._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM ofz_curve_daily").fetchone()[0] == 0
    fx._curve_memo.clear()
