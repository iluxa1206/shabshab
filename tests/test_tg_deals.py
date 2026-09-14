"""/deals ISIN [порог] — лента сделок бумаги в боте."""
import time

import pytest

from api.routes.tg import _deals_text, _parse_threshold


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from services import portfolio_db, trades_archive
    monkeypatch.setattr(portfolio_db, "DB_PATH", tmp_path / "portfolio.db")
    portfolio_db.init_db()
    monkeypatch.setattr(trades_archive, "DB_PATH", tmp_path / "portfolio.db", raising=False)
    yield


def _seed(isin, rows):
    from services import trades_archive
    trades_archive._insert_ticks([
        (isin, i, ts, price, qty, value, side, "TQCB")
        for i, (ts, price, qty, value, side) in enumerate(rows, 1)])


def test_parse_threshold():
    assert _parse_threshold("1m") == 1e6
    assert _parse_threshold("10м") == 10e6
    assert _parse_threshold("500k") == 500e3
    assert _parse_threshold("2,5") == 2.5e6
    assert _parse_threshold("abc") is None


def test_deals_today_with_threshold(db, monkeypatch):
    from services import instruments_registry as reg
    monkeypatch.setattr(reg, "get", lambda isin: {"short_name": "Тест1Р1"} if isin == "RU000TEST0001" else None)
    day = time.strftime("%Y-%m-%d")
    _seed("RU000TEST0001", [
        (f"{day} 10:00:00", 100.0, 100, 100_000, "buy"),
        (f"{day} 11:00:00", 101.0, 2000, 2_020_000, "sell"),
        (f"{day} 12:00:00", 100.5, 15000, 15_075_000, None),
    ])
    out = _deals_text("RU000TEST0001", "1m")
    assert "Тест1Р1" in out and "сегодня" in out
    assert "3 сделок" in out
    assert "от 1,0 млн ₽: 2 шт" in out
    assert "🔴 11:00" in out and "⚪ 12:00" in out and "10:00" not in out


def test_deals_falls_back_to_last_day(db, monkeypatch):
    from services import instruments_registry as reg
    monkeypatch.setattr(reg, "get", lambda isin: {"short_name": "Тест1Р1"})
    _seed("RU000TEST0001", [("2026-09-10 10:00:00", 99.0, 10, 10_000, "buy")])
    out = _deals_text("RU000TEST0001")
    assert "2026-09-10" in out and "🟢 10:00" in out


def test_deals_unknown(db, monkeypatch):
    from services import instruments_registry as reg
    monkeypatch.setattr(reg, "get", lambda isin: None)
    monkeypatch.setattr(reg, "search", lambda q, limit=5: [])
    assert "не нашёл" in _deals_text("XXX")
    assert "Нужен ISIN" in _deals_text("")
