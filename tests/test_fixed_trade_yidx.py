"""Спред сделки у фиксов: подписи из универса и расчёт Y-IDX по цене принта."""
import asyncio

import pytest

from services import bond_labels as bl
from services import trade_yidx as ty
from services.market_data import market_cache

UNI = [
    {"isin": "RU000A0RUB01", "name": "РЖД 1Р-05", "issuer": "РЖД", "cls": "corp",
     "maturity_date": "2029-05-01", "faceunit": "SUR"},
    {"isin": "RU000A0USD01", "name": "ЛУКОЙЛ 30", "issuer": "ЛУКОЙЛ", "cls": "corp",
     "maturity_date": "2030-05-06", "faceunit": "USD"},
    {"isin": "RU000A0PRP01", "name": "ВТБСУБТ", "issuer": "ВТБ", "cls": "corp",
     "maturity_date": None, "faceunit": "SUR", "perpetual": True},
    {"isin": "SU26238RMFS4", "name": "ОФЗ 26238", "issuer": "Минфин", "cls": "ofz",
     "maturity_date": "2041-05-15", "faceunit": "SUR"},
]


@pytest.fixture()
def labels(monkeypatch):
    monkeypatch.setitem(market_cache, "fixed_universe", UNI)
    monkeypatch.setattr(bl, "_fx_cache", {"at": 0.0, "fp": None, "map": {}})
    from services import instruments_registry as reg
    monkeypatch.setattr(reg, "labels_map", lambda isins=None: {
        "RU000A0FLT01": {"name": "Флоатер", "emitter": "X", "base": "KEYRATE",
                         "rating": "AA", "maturity": "2028-01-01"}})
    from services import ratings, ratings_br
    monkeypatch.setattr(ratings, "bucket_map_fixed",
                        lambda items: {i: ("AAA" if c == "ofz" else "AA") for i, c in items})
    monkeypatch.setattr(ratings_br, "ratings_map", lambda ids: {})
    return bl.labels_all()


def test_fixed_labels_carry_rating_maturity_and_base(labels):
    rzd = labels["RU000A0RUB01"]
    assert rzd["base"] == "FIXED" and rzd["rating"] == "AA" and rzd["maturity"] == "2029-05-01"
    assert labels["SU26238RMFS4"]["rating"] == "AAA"
    assert labels["RU000A0FLT01"]["base"] == "KEYRATE"          # реестр не затёрт


def test_pricing_kind(labels):
    k = lambda i: bl.pricing_kind(i, labels)
    assert k("RU000A0FLT01") == "floater"
    assert k("RU000A0RUB01") == "fixed" and k("SU26238RMFS4") == "fixed"
    assert k("RU000A0USD01") is None           # валютный номинал: Y-IDX не определён
    assert k("RU000A0PRP01") is None           # бессрочная
    assert k("RU000A0NONE00") is None          # вне универсов


def test_enrich_prices_fixed_trades_not_foreign(labels, monkeypatch):
    async def fake_fn(isin, kind="floater"):
        assert kind == "fixed"
        return lambda px: {"y_idx_bps": (100.0 - px) * 100.0}
    monkeypatch.setattr(ty, "_metrics_fn", fake_fn)
    rows = [{"isin": "RU000A0RUB01", "price": 99.0},
            {"isin": "RU000A0USD01", "price": 90.0}]
    n = asyncio.run(ty.enrich(rows, labels))
    assert n == 1 and rows[0]["y_idx_bps"] == 100.0 and "y_idx_bps" not in rows[1]
