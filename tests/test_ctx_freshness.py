"""Тёплые контексты стакана и ленты обязаны замечать смену кривой и правку
Справочника, а не жить фиксированные 5 минут.

Аудит конвейера 03.09, находки 17/20/23. У WS-подписки стакана (services/
alor_ws._Sub) и у ленты сделок (services/trade_yidx._ctx_cache) контекст
пересобирался только по таймеру _CTX_TTL=300 с. Правку маржи в Справочнике
строка монитора узнавала на ближайшем такте, а лестница той же бумаги в
карточке — через пять минут; в день восстановления ставок обе считали на снятой
кривой. Ровно это уже чинили в скринере (_sync_ctx_curves) и в движке
(_check_version → _rebind_curves).
"""
import asyncio

import pytest

from services import alor_ws, trade_yidx


def test_orderbook_sub_rebuilds_on_registry_edit(monkeypatch):
    sub = alor_ws._Sub("guid-1")
    built = []

    async def fake_build(isin, kind):
        built.append(isin)
        return (lambda prices: {}), None, 1000.0

    monkeypatch.setattr("services.orderbook_svc.build_levels_fn", fake_build)
    monkeypatch.setattr(alor_ws, "_detect_kind", lambda isin: _ret("floater"))
    monkeypatch.setattr(alor_ws, "_ctx_fp", lambda isin: ("fp-A", 7))

    asyncio.run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert built == ["RU000A100001"]
    sub.memo[100.0] = {"y_idx_bps": 250}

    # ничего не изменилось — контекст не трогаем (TTL ещё не вышел)
    asyncio.run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert built == ["RU000A100001"] and sub.memo

    # правка Справочника подняла data_version → пересборка вместе с memo
    monkeypatch.setattr(alor_ws, "_ctx_fp", lambda isin: ("fp-A", 8))
    asyncio.run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert built == ["RU000A100001", "RU000A100001"]
    assert sub.memo == {}

    # пересобрались кривые — тоже
    monkeypatch.setattr(alor_ws, "_ctx_fp", lambda isin: ("fp-B", 8))
    asyncio.run(alor_ws._ensure_ctx(sub, "RU000A100001"))
    assert len(built) == 3


async def _ret(v):
    return v


def test_tape_ctx_rebuilds_on_curve_change(monkeypatch):
    trade_yidx._ctx_cache.clear()
    built = []

    async def fake_build(isin, kind):
        built.append(isin)
        return (lambda price: {}), None, 1000.0

    monkeypatch.setattr("services.orderbook_svc.build_metrics_fn", fake_build)
    monkeypatch.setattr(trade_yidx, "_ctx_fp", lambda: ("fp-A", 1))

    asyncio.run(trade_yidx._metrics_fn("RU000A100001"))
    asyncio.run(trade_yidx._metrics_fn("RU000A100001"))
    assert len(built) == 1                      # тёплый контекст переиспользован

    monkeypatch.setattr(trade_yidx, "_ctx_fp", lambda: ("fp-B", 1))
    asyncio.run(trade_yidx._metrics_fn("RU000A100001"))
    assert len(built) == 2

    # точечный сброс из Справочника
    trade_yidx.drop_ctx_cache("RU000A100001")
    asyncio.run(trade_yidx._metrics_fn("RU000A100001"))
    assert len(built) == 3
    trade_yidx._ctx_cache.clear()


def test_registry_edit_drops_tape_ctx(monkeypatch):
    """invalidate_params_cache обязана пнуть ленту: её число уходит в архив
    навсегда, а не до истечения TTL."""
    from services import instruments_registry
    trade_yidx._ctx_cache["RU000A100001"] = (0.0, object(), ("fp", 1))
    instruments_registry.invalidate_params_cache("RU000A100001")
    assert "RU000A100001" not in trade_yidx._ctx_cache
