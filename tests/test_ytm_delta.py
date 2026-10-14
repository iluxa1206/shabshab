"""Δ YTM фиксов: база — последний ПОЛНЫЙ день из spread_daily, а не «вчера»."""
import importlib

import pytest


@pytest.fixture()
def sh(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "p.db"))
    from services import portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    from services import spread_history as sh
    importlib.reload(sh)
    return sh, pdb


def _put(pdb, date, n, ytm, src="snap", kind="fixed", prefix="RU000A0FX"):
    with pdb._connect() as c:
        c.executemany(
            "INSERT INTO spread_daily(isin,date,kind,price_pct,ytm,src) VALUES(?,?,?,?,?,?)",
            [(f"{prefix}{i:03d}", date, kind, 100.0, ytm + i * 0.0, src) for i in range(n)])


def test_skips_empty_and_weekend_slices(sh):
    mod, pdb = sh
    _put(pdb, "2026-10-09", 100, 14.0)            # пятница, полный день
    _put(pdb, "2026-10-10", 40, 99.0)             # суббота — усечённый срез
    _put(pdb, "2026-10-13", 2, 77.0)              # день сбоя — жалкий срез
    prev = mod.previous_day_ytm("2026-10-14")
    assert len(prev) == 100 and set(prev.values()) == {14.0}


def test_degraded_snapshot_is_not_a_base(sh):
    mod, pdb = sh
    _put(pdb, "2026-10-12", 100, 14.0)
    _put(pdb, "2026-10-13", 100, 55.0, src="snap_degraded")
    assert set(mod.previous_day_ytm("2026-10-14").values()) == {14.0}


def test_apply_delta_uses_prev_full_day(sh, monkeypatch):
    mod, pdb = sh
    _put(pdb, "2026-10-12", 5, 14.0)
    from services import fixed_income as fi
    m = {"RU000A0FX000": {"ytm": 14.25}, "RU000A0FX001": {"ytm": None},
         "RU000A0NEW00": {"ytm": 10.0}}
    fi.apply_ytm_delta(m, "2026-10-14")
    assert m["RU000A0FX000"]["delta_ytm"] == 0.25
    assert "delta_ytm" not in m["RU000A0FX001"]      # нет YTM сейчас
    assert "delta_ytm" not in m["RU000A0NEW00"]      # нет вчерашнего
