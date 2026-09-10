"""Предохранитель ISS: при мёртвом iss.moex.com запросы должны отваливаться
мгновенно, а не занимать слот семафора весь таймаут (авария 10.09.2026)."""
import asyncio

import httpx
import pytest

import services.market_data as md


@pytest.fixture(autouse=True)
def reset_breaker():
    md._cb_fails = 0
    md._cb_open_until = 0.0
    md._cb_cooldown = md._CB_COOLDOWN_MIN
    md._cb_probing = False
    yield
    md._cb_fails = 0
    md._cb_open_until = 0.0
    md._cb_cooldown = md._CB_COOLDOWN_MIN
    md._cb_probing = False


class _Client:
    """Клиент, который либо всегда падает по транспорту, либо всегда отвечает."""

    def __init__(self, alive=False):
        self.alive = alive
        self.calls = 0

    async def get(self, url, params=None, timeout=None):
        self.calls += 1
        if not self.alive:
            raise httpx.ConnectTimeout("")
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))


def test_breaker_opens_and_stops_calling():
    client = _Client(alive=False)

    async def run():
        for _ in range(md._CB_FAILS):
            assert await md._moex_get(client, "u") is None
        opened = client.calls
        # цепь разомкнута: следующие вызовы не должны трогать сеть вообще
        for _ in range(20):
            assert await md._moex_get(client, "u") is None
        return opened, client.calls

    opened, after = asyncio.run(run())
    assert opened == md._CB_FAILS
    assert after == opened, "разомкнутая цепь всё ещё ходит в ISS"
    assert md.iss_breaker_state()["open"] is True


def test_probe_closes_breaker_when_iss_returns():
    client = _Client(alive=False)

    async def run():
        for _ in range(md._CB_FAILS):
            await md._moex_get(client, "u")
        # cooldown вышел → пропускаем ровно одну пробу
        md._cb_open_until = md.time.monotonic() - 1
        client.alive = True
        before = client.calls
        resp = await md._moex_get(client, "u")
        return resp, client.calls - before

    resp, probes = asyncio.run(run())
    assert resp is not None and probes == 1
    assert md.iss_breaker_state()["open"] is False
    assert md._cb_fails == 0


def test_probe_failure_extends_cooldown():
    client = _Client(alive=False)

    async def run():
        for _ in range(md._CB_FAILS):
            await md._moex_get(client, "u")
        first = md._cb_cooldown
        md._cb_open_until = md.time.monotonic() - 1
        await md._moex_get(client, "u")          # проба провалилась
        return first, md._cb_cooldown

    first, second = asyncio.run(run())
    assert second == min(first * 2, md._CB_COOLDOWN_MAX)
    assert md._cb_probing is False, "флаг пробы завис — цепь больше не замкнётся"


def test_healthy_iss_never_opens():
    client = _Client(alive=True)

    async def run():
        for _ in range(50):
            assert await md._moex_get(client, "u") is not None

    asyncio.run(run())
    assert md.iss_breaker_state()["open"] is False
    assert client.calls == 50
