"""Единый монитор, этап A (docs/unified_monitor_tz.md): A1 — kind в строке,
котировках и WS-патче; A3 — общий список /api/bonds?kinds=floater,fixed;
A4 — фиксы в дельта-журнале /api/bonds/quotes, число рядом с ценой расчёта."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import bonds as mod
from api.routes.bonds import (_QUOTE_LOG, _QUOTE_LOG_TS, _fixed_list_item,
                              _universe_bonds, get_quotes)
from api.schemas import BondListItem, BondListResponse
from services.universe_stream import _fixed_patch, _metrics_patch

FIXED_ROW = {
    "isin": "RU000A0FIX01", "kind": "fixed", "secid": "SU26238RMFS4",
    "name": "ОФЗ 26238", "issuer": "Минфин", "rating": "AAA", "cls": "ofz",
    "maturity_date": "2041-05-15", "perpetual": False, "face_unit": "RUB",
    "coupon_pct": 7.1, "last_price_pct": 60.5, "last": 60.5, "bid": 60.4,
    "ask": 60.6, "wap_pct": 60.45, "ytm": 14.2, "dirty": 612.3,
    "g_spread_bps": -12, "yield_over_index_bps": -150, "y_idx_bid_bps": -148,
    "mod_dur": 7.9, "put_date": None, "price_stale": None, "has_amort": False,
}


@pytest.fixture(autouse=True)
def _clean():
    _QUOTE_LOG.clear()
    _QUOTE_LOG_TS.clear()
    yield


# ── A1: WS-патчи ─────────────────────────────────────────────────────────────

def test_patches_carry_kind():
    assert _metrics_patch({"yoi": 210})["kind"] == "floater"
    assert _fixed_patch({"ytm": 14.2})["kind"] == "fixed"


def test_empty_patch_stays_empty():
    # пустой патч не шлётся вовсе — kind не должен делать его непустым
    assert _metrics_patch({"unrelated": 1}) == {}
    assert _fixed_patch({"unrelated": 1}) == {}


# ── A3: адаптер строки фикса ─────────────────────────────────────────────────

def test_fixed_item_common_names():
    it = _fixed_list_item(FIXED_ROW)
    assert it.kind == "fixed" and it.base_rate_type == "FIXED"
    assert it.short_name == "ОФЗ 26238" and it.emitter_name == "Минфин"
    assert it.bid_price_pct == 60.4 and it.ask_price_pct == 60.6
    assert it.wap_price_pct == 60.45 and it.yield_xirr_pct == 14.2
    assert it.current_coupon_pct == 7.1 and it.is_ofz is True
    assert it.formula == "фикс 7.1%"
    assert it.price_stale is False          # None из движка → bool схемы


def test_fixed_dirty_is_not_rub_dirty():
    # dirty фикса — в валюте номинала, dirty_price_rub флоатера — в рублях
    d = _fixed_list_item(FIXED_ROW).model_dump()
    assert d["dirty_price_rub"] is None and d["dirty"] == 612.3


def test_fixed_specific_fields_survive_response_model():
    """extra="allow" должен пройти сквозь response_model FastAPI, иначе
    g-спред/дюрация фикса молча пропадут из ответа."""
    app = FastAPI()

    @app.get("/x", response_model=BondListResponse)
    def x():
        return BondListResponse(items=[_fixed_list_item(FIXED_ROW)], total=1,
                                limit=1, offset=0)

    row = TestClient(app).get("/x").json()["items"][0]
    assert row["kind"] == "fixed"
    assert row["g_spread_bps"] == -12 and row["mod_dur"] == 7.9
    assert row["yield_over_index_bps"] == -150


def test_put_date_becomes_offer():
    it = _fixed_list_item({**FIXED_ROW, "put_date": "2027-03-01"})
    assert str(it.offer_date) == "2027-03-01" and it.offer_kind == "put"


# ── A3: склейка и пагинация ──────────────────────────────────────────────────

def _list(monkeypatch, kinds, limit=10, offset=0):
    async def floaters(*a, **k):
        return [BondListItem(isin=f"RU000A0FLT0{i}", short_name="F", base_rate_type="KEYRATE",
                             formula="КС", spread_issue_bps=100, maturity_date=None,
                             next_coupon_date=None, last_price_pct=100.0,
                             dirty_price_rub=None, dm_bps=None) for i in range(2)]

    async def fixed(*a, **k):
        return [FIXED_ROW]
    import api.routes.fixed as fixed_mod
    import services.feature_flags as ff
    monkeypatch.setattr(mod, "_floater_items", floaters)
    monkeypatch.setattr(fixed_mod, "fixed_rows", fixed)
    monkeypatch.setattr(ff, "fixed_enabled", lambda: True)
    return asyncio.run(_universe_bonds([], {}, limit, offset, kinds=kinds))


def test_default_is_floaters_only(monkeypatch):
    r = _list(monkeypatch, ("floater",))
    assert r.total == 2 and {i.kind for i in r.items} == {"floater"}


def test_both_kinds_glued_and_paginated(monkeypatch):
    r = _list(monkeypatch, {"floater", "fixed"})
    assert r.total == 3 and [i.kind for i in r.items] == ["floater", "floater", "fixed"]
    page = _list(monkeypatch, {"floater", "fixed"}, limit=1, offset=2)
    assert page.total == 3 and page.items[0].kind == "fixed"


# ── A4: котировки ────────────────────────────────────────────────────────────

def _quotes(monkeypatch, snap, um=None, fm=None, **kw):
    async def fake_snap(*a, **k):
        return snap
    monkeypatch.setattr(mod.MarketDataService, "fetch_board_snapshot", fake_snap)
    monkeypatch.setattr(mod.live_quotes, "get", lambda i: {})
    import services.market_data as md
    md.market_cache["universe_metrics"] = um or {}
    md.market_cache["fixed_metrics"] = fm or {}
    try:
        r = asyncio.run(get_quotes(vol_bid=kw.get("vol_bid"), vol_ask=None,
                                   since=None, epoch=None))
    finally:
        md.market_cache.pop("universe_metrics", None)
        md.market_cache.pop("fixed_metrics", None)
    return {i["isin"]: i for i in r["items"]}


def test_fixed_quote_pairs_numbers_with_their_price(monkeypatch):
    got = _quotes(monkeypatch,
                  {"F": {"last": 60.7, "bid": 60.3, "ask": 60.9, "waprice": 60.5, "vol": 1e6}},
                  fm={"F": {"last": 60.5, "bid": 60.4, "ask": 60.6, "wap_pct": 60.45,
                            "yield_over_index_bps": -150, "ytm": 14.2, "g_spread_bps": -12,
                            "y_idx_bid_bps": -148, "ytm_bid": 14.25,
                            "y_idx_wap_bps": -151, "ytm_wap": 14.21}})["F"]
    assert got["kind"] == "fixed"
    # last — свежий из снапшота, а число и его цена расчёта — из движка
    assert got["last"] == 60.7 and got["yoi"] == -150 and got["yoi_px"] == 60.5
    assert got["ytm"] == 14.2 and got["g"] == -12
    # стороны — из движка, парой со спредом
    assert got["bid"] == 60.4 and got["yoi_bid"] == -148 and got["yoi_bid_px"] == 60.4
    assert got["ytm_bid"] == 14.25
    assert got["yoi_wap"] == -151 and got["yoi_wap_px"] == 60.45
    assert got["wap"] == 60.5                # pick_wap: своего VWAP нет → биржевой


def test_fixed_quote_explicit_none_is_news(monkeypatch):
    got = _quotes(monkeypatch, {"F": {"last": 60.7, "bid": 60.3, "ask": 60.9}},
                  fm={"F": {"last": 60.7, "bid": None, "yield_over_index_bps": None}})["F"]
    assert got["bid"] is None                 # заявку сняли — не откатываемся на снапшот
    assert "yoi" in got and got["yoi"] is None
    assert got["ask"] == 60.9                 # ключа нет у движка — снапшот


def test_fixed_vol_fields_flat(monkeypatch):
    got = _quotes(monkeypatch, {"F": {"last": 60.7}},
                  fm={"F": {"vol_px": {"bid:5000000": 60.1}, "y_idx_vol": {"bid:5000000": -140},
                            "g_spread_vol": {}, "ytm_vol": {"bid:5000000": 14.4}}},
                  vol_bid=5e6)["F"]
    assert got["vol_bid_px"] == 60.1 and got["vol_bid_y"] == -140 and got["vol_bid_ytm"] == 14.4
    assert "vol_bid_g" not in got


def test_floater_and_unknown_quote_kind(monkeypatch):
    got = _quotes(monkeypatch, {"L": {"last": 100.0}, "Z": {"last": 99.0}},
                  um={"L": {"yoi": 210, "last": 100.0}})
    assert got["L"]["kind"] == "floater"
    assert "kind" not in got["Z"]             # вне обоих универсов — класс не знаем
