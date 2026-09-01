"""Очередь свежих выпусков: окно новизны, счётчик на кнопке, алиасы импорта."""
import os
import tempfile
from datetime import date, timedelta

import pytest


@pytest.fixture
def reg(monkeypatch):
    db = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("INSTRUMENTS_DB", db)
    import importlib
    import services.instruments_registry as m
    importlib.reload(m)
    yield m
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(db + suf)
        except OSError:
            pass


def _iso(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def test_new_issue_window_by_issue_date(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150,
                "maturity_date": "2030-01-01", "issue_date": _iso(2)}, "cbonds")
    reg.upsert({"isin": "RU2", "base": "KEYRATE", "margin_bps": 150,
                "maturity_date": "2030-01-01",
                "issue_date": _iso(reg.NEW_ISSUE_DAYS + 5)}, "cbonds")
    isins = [r["isin"] for r in reg.list_new_issues()]
    assert isins == ["RU1"]


def test_new_issue_falls_back_to_first_seen(reg):
    """У бумаги без даты размещения новизну определяет день появления в реестре."""
    reg.upsert({"isin": "RU3", "base": "KEYRATE", "margin_bps": 100,
                "maturity_date": "2030-01-01"}, "moex")
    rows = reg.list_new_issues()
    assert [r["isin"] for r in rows] == ["RU3"]
    assert rows[0]["priceable"] is True


def test_unchecked_only_drops_reviewed(reg):
    reg.upsert({"isin": "RU4", "base": "KEYRATE", "margin_bps": 100,
                "maturity_date": "2030-01-01", "issue_date": _iso(1)}, "cbonds")
    assert reg.count()["new_issues"] == 1
    reg.mark_reviewed("RU4")
    assert reg.count()["new_issues"] == 0
    assert len(reg.list_new_issues()) == 1          # из очереди не исчезает
    assert reg.list_new_issues(unchecked_only=True) == []


def test_blind_new_issue_visible(reg):
    """Свежий выпуск без базы/маржи — главный кандидат на ручную проверку."""
    reg.upsert({"isin": "RU5", "issue_date": _iso(1)}, "moex")
    row = reg.list_new_issues()[0]
    assert row["priceable"] is False


def test_catalog_import_accepts_russian_headers():
    from api.routes.instruments import _header_index
    idx = _header_index(["ISIN", "Название", "Маржа, бп", "Погашение", "Окно, дн"])
    assert idx["isin"] == 0 and idx["short_name"] == 1
    assert idx["margin_bps"] == 2 and idx["maturity_date"] == 3
    assert idx["avg_window_days"] == 4


def test_catalog_import_ignores_percent_margin_column():
    """«Маржа» в bondsearch — проценты; алиасом её брать нельзя (2.5 → 2 bps)."""
    from api.routes.instruments import _header_index
    assert "margin_bps" not in _header_index(["ISIN", "Маржа"])
