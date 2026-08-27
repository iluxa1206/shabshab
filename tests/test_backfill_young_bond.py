"""Молодая бумага + окно длиннее её жизни: бэкфилл не должен падать.

Баг доэкзистирующий, найден 2026-08-27. ПЕРВЫЙ заход проходит (existing пуст →
earliest None → считается полное окно), а ВТОРОЙ берёт till = earliest−1, что у
бумаги моложе окна оказывается ДО ДАТЫ РАЗМЕЩЕНИЯ, и load_backdate_ctx честно
кидает «размещена после». Воспроизведено на проде: 3 из 4 бумаг, размещённых за
неделю до проверки, падали со второго захода.
"""
import asyncio
import importlib
from datetime import date, timedelta

import pytest


@pytest.fixture
def bd(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.spread_history as sh
    import services.backdate as backdate
    importlib.reload(sh)
    importlib.reload(backdate)
    yield backdate, sh, pdb
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)
    importlib.reload(sh)
    importlib.reload(backdate)


def _seed(pdb, isin, days_ago_list, ver):
    from services.spread_history import upsert_honest
    pts = [{"date": (date.today() - timedelta(days=d)).isoformat(),
            "price_pct": 100.0, "y_idx_bps": 200, "dm_bps": 180,
            "yield_pct": 16.0} for d in days_ago_list]
    upsert_honest(isin, pts, set(), ver)


def test_left_extension_stops_at_issue_date(bd, monkeypatch):
    """Окно 400 дней, бумага размещена 10 дней назад: левее размещения считать
    нечего, и заход обязан тихо завершиться, а не упасть."""
    backdate, _sh, pdb = bd
    isin = "RU000TESTYNG1"
    # размещение = самая ранняя строка: till = earliest−1 уйдёт ДО него
    issue = (date.today() - timedelta(days=9)).isoformat()
    _seed(pdb, isin, [9, 8, 7], backdate.HONEST_ENGINE_VERSION)

    called = []

    async def boom(*a, **kw):
        called.append(kw.get("till"))
        raise AssertionError("серия не должна считаться левее размещения")

    monkeypatch.setattr(backdate, "honest_spread_series", boom)
    monkeypatch.setattr(backdate, "_backfill_done", {})

    class _Reg:
        @staticmethod
        def get(_isin):
            return {"issue_date": issue}

    monkeypatch.setitem(__import__("sys").modules,
                        "services.instruments_registry", _Reg)

    n = asyncio.run(backdate.ensure_honest_backfill(isin, days=400))
    assert n == 0
    assert called == [], "полезли считать левее даты размещения"


def test_left_extension_runs_when_room_exists(bd, monkeypatch):
    """А если левее earliest есть место ПОСЛЕ размещения — считаем как обычно."""
    backdate, _sh, pdb = bd
    isin = "RU000TESTYNG2"
    issue = (date.today() - timedelta(days=300)).isoformat()
    _seed(pdb, isin, [40, 39, 38], backdate.HONEST_ENGINE_VERSION)

    seen = []

    async def fake(isin_, days=180, board=None, price_overrides=None,
                   till=None, on_chunk=None, hz_keys=None):
        seen.append(till)
        return {"isin": isin_, "points": [], "warnings": []}

    monkeypatch.setattr(backdate, "honest_spread_series", fake)
    monkeypatch.setattr(backdate, "_backfill_done", {})

    class _Reg:
        @staticmethod
        def get(_isin):
            return {"issue_date": issue}

    monkeypatch.setitem(__import__("sys").modules,
                        "services.instruments_registry", _Reg)

    asyncio.run(backdate.ensure_honest_backfill(isin, days=200))
    assert seen and seen[0] is not None, "левый досчёт не запустился"


def test_no_issue_date_keeps_old_behaviour(bd, monkeypatch):
    """Дата размещения неизвестна — ведём себя как раньше (пробуем считать)."""
    backdate, _sh, pdb = bd
    isin = "RU000TESTYNG3"
    _seed(pdb, isin, [9, 8, 7], backdate.HONEST_ENGINE_VERSION)
    seen = []

    async def fake(isin_, days=180, board=None, price_overrides=None,
                   till=None, on_chunk=None, hz_keys=None):
        seen.append(till)
        return {"isin": isin_, "points": [], "warnings": []}

    monkeypatch.setattr(backdate, "honest_spread_series", fake)
    monkeypatch.setattr(backdate, "_backfill_done", {})

    class _Reg:
        @staticmethod
        def get(_isin):
            return {}

    monkeypatch.setitem(__import__("sys").modules,
                        "services.instruments_registry", _Reg)

    asyncio.run(backdate.ensure_honest_backfill(isin, days=400))
    assert seen, "без issue_date поведение не должно меняться"
