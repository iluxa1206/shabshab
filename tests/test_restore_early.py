"""Ранний подъём снимка метрик фиксов: только в пустую витрину, только свежий."""
import json
import time

from services import metrics_persist as mp


def _snap(tmp_path, monkeypatch, ts, fixed=None):
    path = tmp_path / "metrics_snapshot.json"
    path.write_text(json.dumps({"v": mp.SNAPSHOT_VERSION, "ts": ts, "calc_date": "2026-10-14",
                                "curves_fp": "x", "universe": {"F": {"yoi": 1.0}},
                                "fixed": fixed if fixed is not None else {"A": {"ytm": 13.0}}}))
    import services.paths as paths
    monkeypatch.setattr(paths, "cache_path", lambda name: str(tmp_path / name))


def test_restores_fixed_only_with_zero_calc_ts(tmp_path, monkeypatch):
    _snap(tmp_path, monkeypatch, time.time() - 60)
    mc: dict = {}
    assert mp.restore_early(mc) == 1
    assert mc["fixed_metrics"]["A"] == {"ytm": 13.0, "_calc_ts": 0.0}
    assert "universe_metrics" not in mc            # флоатеры считает прогрев старта


def test_skips_old_snapshot_and_busy_cache(tmp_path, monkeypatch):
    _snap(tmp_path, monkeypatch, time.time() - mp.MAX_AGE_SEC - 5)
    assert mp.restore_early({}) == 0
    _snap(tmp_path, monkeypatch, time.time() - 60)
    busy = {"fixed_metrics": {"Z": {"ytm": 1.0}}}
    assert mp.restore_early(busy) == 0 and busy["fixed_metrics"] == {"Z": {"ytm": 1.0}}


def test_warm_replaces_restored_row_whole():
    from services import universe_stream as us
    mc = {"fixed_metrics": {"A": {"_calc_ts": 0.0, "bid": 99.0, "y_idx_bid_bps": -50,
                                  "ytm": 13.0}}}
    kept, merged = us.merge_fixed_metrics(mc, {"A": {"last": 100.0, "bid": 99.5, "ytm": 12.9}}, 1.0)
    assert (kept, merged) == (0, 1)
    assert mc["fixed_metrics"]["A"] == {"last": 100.0, "bid": 99.5, "ytm": 12.9}
