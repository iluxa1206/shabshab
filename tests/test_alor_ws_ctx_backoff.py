"""Неудачная сборка контекста стакана не повторяется на каждом пуше книги.

_ensure_ctx обновлял отметку только на УСПЕХЕ. Если build_levels_fn бросил
(бумаги нет в реестре, MOEX её не отдаёт, кривая не собралась), ранний выход по
TTL не срабатывал, и полная пересборка (load_reprice_ctx: gather из шести
источников + create_bond_ref_data + reconcile_face) запускалась заново на каждом
пуше стакана — раз в 800 мс на подписанную бумагу, в event loop и молча.
"""
import asyncio

import pytest

from services import alor_ws


def _sub():
    return alor_ws._Sub("guid-1")


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(alor_ws, "_ctx_fp", lambda isin: ("fp", 1))
    monkeypatch.setattr(alor_ws, "_detect_kind", lambda isin: _ok("floater"))


async def _ok(v):
    return v


def test_failed_build_is_not_retried_on_every_push(monkeypatch):
    calls = []

    async def _boom(isin, kind):
        calls.append(isin)
        raise RuntimeError("нет в реестре")
    monkeypatch.setattr("services.orderbook_svc.build_levels_fn", _boom)

    sub = _sub()
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert len(calls) == 1               # вторая и третья попытки отсечены бэкоффом
    assert sub.ctx_fails == 1
    assert sub.ctx_try > 0               # попытка отмечена, хотя успеха не было


def test_backoff_expires_and_lets_the_next_try_through(monkeypatch):
    calls = []

    async def _boom(isin, kind):
        calls.append(isin)
        raise RuntimeError("нет в реестре")
    monkeypatch.setattr("services.orderbook_svc.build_levels_fn", _boom)

    sub = _sub()
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    sub.ctx_try -= alor_ws._CTX_RETRY_MIN + 1        # окно бэкоффа вышло
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert len(calls) == 2
    assert sub.ctx_fails == 2                        # бэкофф растёт со вторым отказом


def test_success_clears_the_backoff(monkeypatch):
    async def _boom(isin, kind):
        raise RuntimeError("нет в реестре")
    monkeypatch.setattr("services.orderbook_svc.build_levels_fn", _boom)
    sub = _sub()
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert sub.ctx_fails == 1

    async def _fine(isin, kind):
        return (lambda prices: {}), None, 1000.0
    monkeypatch.setattr("services.orderbook_svc.build_levels_fn", _fine)
    sub.ctx_try -= alor_ws._CTX_RETRY_MIN + 1
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert sub.ctx_fails == 0 and sub.levels_fn is not None


def test_new_curves_lift_the_backoff(monkeypatch):
    """Смена отпечатка (кривые пересобрались, бумагу завели в Справочнике) даёт
    попытку немедленно: отказ был свойством ПРЕЖНЕГО входа."""
    calls = []

    async def _boom(isin, kind):
        calls.append(isin)
        raise RuntimeError("нет в реестре")
    monkeypatch.setattr("services.orderbook_svc.build_levels_fn", _boom)

    sub = _sub()
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert len(calls) == 1                       # бэкофф держит

    monkeypatch.setattr(alor_ws, "_ctx_fp", lambda isin: ("fp-2", 2))
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert len(calls) == 2                       # новый вход — новая попытка


def test_healthy_ctx_is_not_rebuilt_within_ttl(monkeypatch):
    calls = []

    async def _fine(isin, kind):
        calls.append(isin)
        return (lambda prices: {}), None, 1000.0
    monkeypatch.setattr("services.orderbook_svc.build_levels_fn", _fine)
    sub = _sub()
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    _run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert len(calls) == 1
