"""Токен Alor: async-путь не ходит в пул потоков и не логинится дважды.

Регресс: девять корутин звали get_access_token через asyncio.to_thread, пул
to_thread один на приложение (6 воркеров), и при недоступном oauth все шесть
вставали в блокирующий requests на 15с — вместе с ними замирали чтения SQLite
под запросы API. В проде это ловилось сторожем: лаг 4.4с, «потоки в момент
лага: 6× auth.py:get_access_token».
"""
import asyncio
import time

import pytest

import auth


@pytest.fixture(autouse=True)
def clean_token(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "TOKEN_CACHE_FILE", tmp_path / "token.json")
    monkeypatch.setattr(auth, "_mem", (None, 0.0))
    monkeypatch.setattr(auth, "_alock", None)
    monkeypatch.setattr(auth, "REFRESH_TOKEN", "refresh-xxx")
    yield


class _FakeResp:
    def __init__(self, calls):
        self._calls = calls

    async def __aenter__(self):
        self._calls.append(1)
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return {"AccessToken": "tok-1"}


class _FakeSession:
    def __init__(self, calls, timeout=None):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, params=None):
        return _FakeResp(self._calls)


def _patch_net(monkeypatch):
    calls = []
    monkeypatch.setattr(auth.aiohttp, "ClientSession",
                        lambda *a, **kw: _FakeSession(calls))
    return calls


def test_async_token_caches_in_memory(monkeypatch):
    calls = _patch_net(monkeypatch)
    assert asyncio.run(auth.alor_token()) == "tok-1"
    assert asyncio.run(auth.alor_token()) == "tok-1"
    assert len(calls) == 1          # второй раз — из памяти, без сети


def test_async_token_single_flight(monkeypatch):
    """Девять одновременных запросов токена = один логин."""
    calls = _patch_net(monkeypatch)

    async def main():
        return await asyncio.gather(*(auth.alor_token() for _ in range(9)))

    assert asyncio.run(main()) == ["tok-1"] * 9
    assert len(calls) == 1


def test_sync_path_uses_memory_cache(monkeypatch):
    """Синхронный вызов после async-обновления в сеть не идёт: иначе скрипты и
    легаси-код снова занимали бы поток пула на HTTP_TIMEOUT."""
    _patch_net(monkeypatch)
    asyncio.run(auth.alor_token())

    def boom(*a, **kw):
        raise AssertionError("сеть не должна дёргаться при живом кэше")

    monkeypatch.setattr(auth.requests, "post", boom)
    assert auth.get_access_token("refresh-xxx") == "tok-1"


def test_expired_memory_falls_back_to_file(monkeypatch):
    calls = _patch_net(monkeypatch)
    asyncio.run(auth.alor_token())
    # память протухла, файл ещё валиден → перелогина нет
    monkeypatch.setattr(auth, "_mem", ("tok-1", time.time() - 1))
    assert asyncio.run(auth.alor_token()) == "tok-1"
    assert len(calls) == 1
