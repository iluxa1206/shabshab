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


# ── A5: эмитенты, флаг «структурная», A6: ступень рейтинга ────────────────────

class _FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return {"securities": {"data": self._rows}}


class _FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, timeout=None):
        page = self.pages[self.calls] if self.calls < len(self.pages) else []
        self.calls += 1
        return _FakeResp(page)


def _emitters(monkeypatch, tmp_path, doc, pages):
    import services.emitters as em
    import services.market_data as md
    monkeypatch.setattr(em.paths, "cache_path", lambda n: str(tmp_path / n))
    monkeypatch.setattr(em, "_mem", {"doc": doc})
    client = _FakeClient(pages)
    monkeypatch.setattr(md, "moex_client", lambda **k: client)
    return em, client


def test_emitters_refresh_on_missing_isin(monkeypatch, tmp_path):
    import time
    em, client = _emitters(monkeypatch, tmp_path,
                           {"ts": time.time() - 7 * 3600, "map": {"A": [1, "x"]}},
                           [[["A", 1, "x"], ["B", 2, "y"]]])
    asyncio.run(em.ensure(["A", "B"]))
    assert em.emitter_of("B") == [2, "y"]
    assert (tmp_path / "emitters.json").exists()


def test_emitters_fresh_map_no_network(monkeypatch, tmp_path):
    import time
    em, client = _emitters(monkeypatch, tmp_path,
                           {"ts": time.time() - 3600, "map": {"A": [1, "x"]}}, [])
    asyncio.run(em.ensure(["A", "NEW"]))       # свежий выпуск, но 6 ч не прошло
    assert client.calls == 0


def test_emitters_short_answer_keeps_map(monkeypatch, tmp_path):
    em, client = _emitters(monkeypatch, tmp_path,
                           {"ts": 0, "map": {f"I{i}": [i, "t"] for i in range(10)}},
                           [[["I0", 0, "t"]]])
    asyncio.run(em.ensure(["I0"]))
    assert len(em._doc()["map"]) == 10 and em._doc()["ts"] == 0


def _fixed_rows(monkeypatch, uni, steps=None, emit=None, canon=None):
    import api.routes.fixed as fx
    import services.market_data as md
    from services import bars, ratings, ratings_br, emitters
    md.market_cache["fixed_universe"] = uni
    md.market_cache["fixed_metrics"] = {}
    monkeypatch.setattr(ratings, "bucket_map_fixed",
                        lambda items: {i: ("AAA" if c == "ofz" else "AA") for i, c in items})
    monkeypatch.setattr(ratings_br, "ea_map", lambda isins: {})
    monkeypatch.setattr(ratings_br, "ratings_map",
                        lambda isins: {i: {"rating": r} for i, r in (steps or {}).items()})
    monkeypatch.setattr(bars, "adv_map", lambda *a, **k: {})

    async def no_vol(*a, **k):
        return {}
    monkeypatch.setattr(md.MarketDataService, "fetch_issue_volumes", no_vol)
    monkeypatch.setattr(emitters, "emitter_of", lambda i: (emit or {}).get(i))
    monkeypatch.setattr(fx, "_registry_emitter_names", lambda: canon or {})
    try:
        return {r["isin"]: r for r in asyncio.run(fx.fixed_rows())}
    finally:
        md.market_cache.pop("fixed_universe", None)
        md.market_cache.pop("fixed_metrics", None)


def test_fixed_rows_emitter_structured_rating(monkeypatch):
    uni = [{"isin": "OFZ1", "name": "ОФЗ 26238", "issuer": "ОФЗ", "cls": "ofz", "coupon_pct": 0.5},
           {"isin": "IOS1", "name": "СберИОС819", "issuer": "СберИОС", "cls": "corp",
            "coupon_pct": 0.01},
           {"isin": "RZD1", "name": "РЖД 1Р-05R", "issuer": "РЖД", "cls": "corp",
            "coupon_pct": 15.5}]
    got = _fixed_rows(monkeypatch, uni, steps={"RZD1": "AA+", "OFZ1": "AA"},
                      emit={"IOS1": [484, "ПАО Сбербанк"], "RZD1": [712, "ОАО РЖД"]},
                      canon={712: "РЖД БО"})
    # ОФЗ: синтетический эмитент реестра, AAA правилом, низкий купон ≠ структурная
    assert got["OFZ1"]["emitter_id"] == -1 and got["OFZ1"]["emitter_name"] == "Минфин России"
    assert got["OFZ1"]["rating"] == "AAA" and got["OFZ1"]["structured"] is False
    # имя эмитента — реестра по id; ступень рейтинга вместо грейда
    assert got["RZD1"]["emitter_id"] == 712 and got["RZD1"]["emitter_name"] == "РЖД БО"
    assert got["RZD1"]["rating"] == "AA+" and got["RZD1"]["structured"] is False
    # нет эмитента в реестре — официальное название MOEX (а не имя серии выпуска)
    assert got["IOS1"]["emitter_id"] == 484 and got["IOS1"]["emitter_name"] == "ПАО Сбербанк"
    assert got["IOS1"]["rating"] == "AA" and got["IOS1"]["structured"] is True


def test_list_item_carries_structured_and_emitter():
    it = _fixed_list_item({**FIXED_ROW, "emitter_id": 712, "emitter_name": "РЖД БО",
                           "structured": True})
    assert it.emitter_id == 712 and it.emitter_name == "РЖД БО" and it.structured is True


# ── only: строка одной бумаги для страницы графика ────────────────────────────

def _list_only(monkeypatch, only, kinds):
    async def floaters(*a, **k):
        return [BondListItem(isin="RU000A0FLT00", short_name="F", base_rate_type="KEYRATE",
                             formula="КС", spread_issue_bps=100, maturity_date=None,
                             next_coupon_date=None, last_price_pct=100.0,
                             dirty_price_rub=None, dm_bps=None, rating="AA-",
                             emitter_name="РЖД БО")]

    async def fixed(*a, **k):
        return [FIXED_ROW, {**FIXED_ROW, "isin": "RU000A0FIX02"}]
    import api.routes.fixed as fixed_mod
    import services.feature_flags as ff
    monkeypatch.setattr(mod, "_floater_items", floaters)
    monkeypatch.setattr(fixed_mod, "fixed_rows", fixed)
    monkeypatch.setattr(ff, "fixed_enabled", lambda: True)
    return asyncio.run(_universe_bonds([], {}, 1, 0, kinds=kinds, only=only))


def test_only_floater_row_has_rating_and_emitter(monkeypatch):
    r = _list_only(monkeypatch, "RU000A0FLT00", {"floater", "fixed"})
    assert r.total == 1 and r.items[0].rating == "AA-" and r.items[0].emitter_name == "РЖД БО"


def test_only_finds_fixed_when_not_floater(monkeypatch):
    r = _list_only(monkeypatch, "RU000A0FIX02", {"floater", "fixed"})
    assert r.total == 1 and r.items[0].isin == "RU000A0FIX02" and r.items[0].kind == "fixed"


def test_only_unknown_is_empty_not_whole_market(monkeypatch):
    assert _list_only(monkeypatch, "-", {"floater", "fixed"}).total == 0


def test_emitter_name_order_registry_then_moex_then_issue(monkeypatch):
    import services.emitters as em
    monkeypatch.setattr(em, "emitter_of", lambda i: {"A": (1, "ООО Эмитент"), "B": (2, "ПАО Банк")}.get(i))
    canon = {2: "Банк БО"}
    assert em.fixed_emitter({"isin": "A", "issuer": "Серия 02"}, canon) == (1, "ООО Эмитент")
    assert em.fixed_emitter({"isin": "B", "issuer": "Серия 03"}, canon) == (2, "Банк БО")
    assert em.fixed_emitter({"isin": "Z", "issuer": "Серия 04"}, canon) == (None, "Серия 04")
    assert em.fixed_emitter({"isin": "O", "cls": "ofz"}, canon) == (-1, "Минфин России")
