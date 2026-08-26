"""История Y-IDX в сообщении бота: спарклайн в шапке сигнала."""
import re

from services import tg_notify as tn


def test_spark_scales_to_own_range():
    bars = tn._spark([100, 120, 140, 160, 180])
    assert bars[0] == "▁" and bars[-1] == "█"
    assert len(bars) == 5


def test_spark_flat_series_is_flat():
    # разброс меньше бп — ряд «стоял на месте», а не шум от деления на ноль
    assert tn._spark([180.0, 180.4, 179.9]) == "▅▅▅"


def test_spark_needs_three_points():
    assert tn._spark([100, 200]) == ""


def test_book_columns_do_not_drift(monkeypatch):
    m = {"price": 99.86,
         "book": {"asks": [{"price": 99.90, "qty": 24950, "y_idx": 178},
                           {"price": 99.86, "qty": 120000, "y_idx": 181}],
                  "bids": [{"price": 99.70, "qty": 3800, "y_idx": 195}]}}
    out = tn._book_pre(m, "ask")
    rows = [re.sub(r"<[^>]+>", "", ln) for ln in out.splitlines() if "─" not in ln]
    assert len({len(r.rstrip(" ←")) for r in rows}) == 1     # колонки ровные
    assert "←" in out                                        # свой уровень помечен


def test_history_survives_missing_db(monkeypatch):
    monkeypatch.setattr(tn, "_spark_cache", {})
    monkeypatch.setattr(tn, "_spark_points",
                        lambda isin: (_ for _ in ()).throw(RuntimeError("нет базы")))
    assert tn._history({"isin": "RU000A100000"}) == ([], "")
    assert tn._spark_line({"isin": "RU000A100000"}) == ""


def test_spark_line_shows_delta_and_window(monkeypatch):
    monkeypatch.setattr(tn, "_spark_cache", {})
    monkeypatch.setattr(
        tn, "_spark_points",
        lambda isin: ([(f"{10 + i}:00", v) for i, v in
                       enumerate([120, 128, 124, 140, 152])], "ч"))
    out = tn._spark_line({"isin": "X"})
    assert "+32" in out and "5ч" in out
    assert re.sub(r"<[^>]+>", "", out).startswith("▁")


def test_book_has_no_volume_bars(monkeypatch):
    m = {"price": 99.86,
         "book": {"asks": [{"price": 99.90, "qty": 24950, "y_idx": 178}],
                  "bids": [{"price": 99.70, "qty": 3800, "y_idx": 195}]}}
    out = re.sub(r"<[^>]+>", "", tn._book_pre(m, "ask"))
    assert "█" not in out and "▏" not in out
