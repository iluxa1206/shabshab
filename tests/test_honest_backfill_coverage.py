"""Дневная honest-серия не должна пересчитываться, когда окно уже покрыто.

Баг (найден 2026-08-14 при разборе тормозов графика): покрытие проверялось как
`have >= days` — число доверенных СТРОК против КАЛЕНДАРНЫХ дней окна. Торговых
дней всегда меньше (в 400-дневном окне ~270), поэтому условие не выполнялось
никогда: спасало только memo в памяти процесса, а после каждого рестарта первый
заход на график любой бумаги пересчитывал всю историю заново. Расширение окна
(6 месяцев → YTD) тоже считало весь диапазон, а не недостающий кусок.
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


ISIN = "RU000TEST0002"


def _seed(pdb, backdate, days_back: int, step: int = 3):
    """Готовая honest-история: строки текущей версии движка за окно."""
    with pdb._connect() as c:
        for i in range(0, days_back, step):          # «торговые» дни через один-два
            d = (date.today() - timedelta(days=i)).isoformat()
            c.execute(
                "INSERT OR REPLACE INTO spread_daily(isin,date,kind,price_pct,y_idx,"
                "src,engine_ver) VALUES(?,?,'floater',100.0,120.0,'honest',?)",
                (ISIN, d, backdate.HONEST_ENGINE_VERSION))


@pytest.fixture
def calls(bd, monkeypatch):
    backdate = bd[0]
    seen = []

    async def fake_series(isin, days=180, board=None, price_overrides=None, till=None):
        seen.append({"days": days, "till": till, "overrides": bool(price_overrides)})
        return {"isin": isin, "points": [], "warnings": []}

    monkeypatch.setattr(backdate, "honest_spread_series", fake_series)
    return seen


def test_covered_window_is_noop(bd, calls):
    """История уже левее начала окна — тяжёлый пересчёт не нужен."""
    backdate, _sh, pdb = bd
    _seed(pdb, backdate, days_back=200)
    asyncio.run(backdate.ensure_honest_backfill(ISIN, days=90))
    assert calls == [], "окно покрыто, а серия всё равно пересчиталась"


def test_covered_after_restart_is_noop(bd, calls):
    """Memo живёт в памяти процесса: после рестарта покрытие должно
    определяться по САМОЙ БАЗЕ, а не по счётчику вызовов."""
    backdate, _sh, pdb = bd
    _seed(pdb, backdate, days_back=200)
    backdate._backfill_done.clear()          # как после перезапуска контейнера
    asyncio.run(backdate.ensure_honest_backfill(ISIN, days=180))
    assert calls == []


def test_widening_window_counts_only_missing_part(bd, calls):
    """Расширение окна досчитывает только левый кусок: till = день перед
    самой ранней готовой датой."""
    backdate, _sh, pdb = bd
    _seed(pdb, backdate, days_back=100)
    earliest = min(
        (date.today() - timedelta(days=i)).isoformat() for i in range(0, 100, 3))
    asyncio.run(backdate.ensure_honest_backfill(ISIN, days=365))
    assert len(calls) == 1
    assert calls[0]["till"] == date.fromisoformat(earliest) - timedelta(days=1)
    # считаем именно недостающий отрезок, а не все 365 дней
    assert calls[0]["days"] < 365


def test_empty_history_counts_full_window(bd, calls):
    """Истории нет вовсе — считаем всё окно, till не задаём."""
    backdate, _sh, _pdb = bd
    asyncio.run(backdate.ensure_honest_backfill(ISIN, days=120))
    assert len(calls) == 1
    assert calls[0]["till"] is None and calls[0]["days"] == 120
