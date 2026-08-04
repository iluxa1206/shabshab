"""Кэш глубины стакана (services.depth) — сырьё фильтра по объёму в таблице."""
import time

import pytest

from services import depth as depth_svc
from services.market_data import market_cache
from core.orderbooks import _levels


@pytest.fixture(autouse=True)
def _clean_cache():
    old, old_ts = market_cache.get("depth"), market_cache.get("depth_ts")
    yield
    market_cache["depth"], market_cache["depth_ts"] = old, old_ts


def test_levels_drops_incomplete():
    raw = [{"price": 100.1, "volume": 50}, {"price": None, "volume": 10},
           {"price": 99.9}, {"price": 99.8, "volume": 0}]
    assert _levels(raw) == [[100.1, 50.0], [99.8, 0.0]]
    assert _levels(None) == []


def test_get_depth_empty_without_snapshot():
    market_cache["depth"], market_cache["depth_ts"] = {}, 0.0
    assert depth_svc.get_depth() == {}


def test_get_depth_returns_fresh_snapshot():
    snap = {"RU000A0TEST1": {"b": [[100.0, 10]], "a": [[100.2, 5]]}}
    market_cache["depth"], market_cache["depth_ts"] = snap, time.time()
    assert depth_svc.get_depth() == snap


def test_get_depth_hides_stale_snapshot():
    """Протухший снимок наружу не отдаём: фильтр по объёму на стаканах получасовой
    давности хуже выключенного фильтра — трейдер увидит глубину, которой нет."""
    snap = {"RU000A0TEST1": {"b": [[100.0, 10]], "a": []}}
    market_cache["depth"] = snap
    market_cache["depth_ts"] = time.time() - depth_svc._STALE_SEC - 1
    assert depth_svc.get_depth() == {}
