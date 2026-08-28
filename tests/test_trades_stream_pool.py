"""Битый ответ ISS не должен схлопывать пул сокетов.

Листинг MOEX на своём сбое возвращается пустым словарём — молча. Владелец пула
сравнивает список бумаг с прошлым, и «пусто» читалось как «рынок ужался»: все
шарды убивались, поднимался один юниверс, остальной рынок слеп до следующего
такта (замер 2026-08-28: по разу в час, каждый раз на 5 минут).
"""
import asyncio

import pytest

from services import trades_stream as ts


@pytest.fixture()
def uni(monkeypatch):
    core = [f"RU{i:010d}" for i in range(10)]

    async def fake_uni():
        return [{"isin": i} for i in core]

    monkeypatch.setattr("services.instruments_registry.fetch_floater_universe", fake_uni)
    ts._last_rest.clear()
    return core


def _listing(monkeypatch, isins):
    async def fake():
        return {i: {} for i in isins}
    monkeypatch.setattr("services.market_data.MarketDataService.fetch_bond_listing",
                        staticmethod(fake))


def test_healthy_listing_sets_the_tail(uni, monkeypatch):
    _listing(monkeypatch, [f"XX{i:08d}" for i in range(100)])
    got = asyncio.run(ts.subscription_isins())
    assert len(got) == 110 and len(ts._last_rest) == 100


def test_empty_listing_keeps_previous_tail(uni, monkeypatch):
    _listing(monkeypatch, [f"XX{i:08d}" for i in range(100)])
    asyncio.run(ts.subscription_isins())
    _listing(monkeypatch, [])           # сбой ISS
    got = asyncio.run(ts.subscription_isins())
    assert len(got) == 110, "пул не должен схлопнуться до одного юниверса"


def test_shrunken_listing_is_rejected(uni, monkeypatch):
    _listing(monkeypatch, [f"XX{i:08d}" for i in range(100)])
    asyncio.run(ts.subscription_isins())
    _listing(monkeypatch, [f"XX{i:08d}" for i in range(10)])   # куцый ответ
    assert len(asyncio.run(ts.subscription_isins())) == 110


def test_real_shrink_is_accepted(uni, monkeypatch):
    """Настоящее сжатие рынка (больше половины) проходит: порог не запрещает
    пулу уменьшаться, он отсекает обвал источника."""
    _listing(monkeypatch, [f"XX{i:08d}" for i in range(100)])
    asyncio.run(ts.subscription_isins())
    _listing(monkeypatch, [f"XX{i:08d}" for i in range(80)])
    assert len(asyncio.run(ts.subscription_isins())) == 90
