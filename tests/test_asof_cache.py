"""Кэши подготовки as-of: история MOEX и собранная фабрика.

Сборка as-of — это сеть (дневная история пагинацией) плюс контексты по дням;
на холодном длинном окне доходило до сотни секунд, и её просят три места сразу
(бары, маркеры сделок, honest-серия). Прошлые торговые дни не меняются, значит
и то, и другое живёт день.
"""
import asyncio
import importlib
from datetime import date, timedelta

import pytest


@pytest.fixture
def bd(monkeypatch):
    import services.backdate as backdate
    importlib.reload(backdate)
    yield backdate
    importlib.reload(backdate)


def _rows(d_from, d_till):
    out, d = [], d_from
    while d <= d_till:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "close": 100.0, "legalclose": 100.0,
                        "accint": 1.0, "facevalue": 1000.0})
        d += timedelta(days=1)
    return out


def test_history_cached_within_day(bd, monkeypatch):
    calls = []

    async def fake_get(*a, **kw):
        raise AssertionError("сеть не должна дёргаться при попадании в кэш")

    d_till = date.today() - timedelta(days=1)
    d_from = d_till - timedelta(days=100)
    bd._hist_memo_put("SEC", "TQCB", d_from, d_till, _rows(d_from, d_till))
    monkeypatch.setattr(bd.httpx, "AsyncClient", fake_get)
    rows = asyncio.run(bd.fetch_history_range("SEC", d_from, d_till, "TQCB"))
    assert rows and all(d_from.isoformat() <= r["date"] <= d_till.isoformat() for r in rows)
    assert calls == []


def test_history_narrower_window_served_from_cache(bd):
    """Окно поуже отдаётся СРЕЗОМ широкого кэша, без похода в сеть."""
    d_till = date.today() - timedelta(days=1)
    wide_from = d_till - timedelta(days=300)
    bd._hist_memo_put("SEC", "TQCB", wide_from, d_till, _rows(wide_from, d_till))
    narrow_from = d_till - timedelta(days=30)
    got = bd._hist_memo_get("SEC", "TQCB", narrow_from, d_till)
    assert got is not None
    assert min(r["date"] for r in got) >= narrow_from.isoformat()


def test_history_wider_window_is_a_miss(bd):
    """Окно ШИРЕ кэша обслужить нечем — честный промах, пойдём в сеть."""
    d_till = date.today() - timedelta(days=1)
    bd._hist_memo_put("SEC", "TQCB", d_till - timedelta(days=30), d_till,
                      _rows(d_till - timedelta(days=30), d_till))
    assert bd._hist_memo_get("SEC", "TQCB", d_till - timedelta(days=300), d_till) is None


def test_history_empty_not_cached(bd, monkeypatch):
    """Пустой ответ — сбой сети, а не «истории нет»: кэшировать нельзя, иначе
    один флак ISS заморозил бы бумагу без спреда на весь день (ВЭБP-41)."""
    class _Resp:
        status_code = 200
        def json(self):
            return {"history": {"columns": [], "data": []}}

    class _Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(bd.httpx, "AsyncClient", lambda *a, **kw: _Client())
    d_till = date.today() - timedelta(days=1)
    d_from = d_till - timedelta(days=10)
    assert asyncio.run(bd.fetch_history_range("EMPTY", d_from, d_till, "TQCB")) == []
    assert bd._hist_memo_get("EMPTY", "TQCB", d_from, d_till) is None


def test_asof_factory_reused_for_narrower_window(bd):
    """Фабрика умеет любую дату внутри своего окна: запрос поуже переиспользует
    уже построенную, а шире — строится заново."""
    sentinel = lambda day, px: {"y_idx_bps": 42}
    bd._asof_memo[("RU000TEST", None)] = (date.today(), 400, sentinel)
    got = asyncio.run(bd.asof_bar_metrics("RU000TEST", 95, None))
    assert got is sentinel
    bd._asof_memo[("RU000TEST", None)] = (date.today() - timedelta(days=1), 400, sentinel)
    with pytest.raises(Exception):        # вчерашний кэш не годится → сборка (без сети упадёт)
        asyncio.run(bd.asof_bar_metrics("RU000TEST", 95, None))
