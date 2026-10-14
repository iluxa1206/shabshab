"""/api/trades?part=… — строки и итоги по отдельности (14.10.2026).

Итоги по широкому окну без порога суммы стоят 4–9 с, строки — сотые доли.
Фронт просит их двумя запросами, чтобы лента появлялась сразу."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.trades as tr
from services import tape as tape_svc


@pytest.fixture
def client(monkeypatch):
    calls = {"rows": 0, "stats": 0}

    def read_tape(**k):
        calls["rows"] += 1
        return [{"trade_id": 1, "isin": "RU000A0000A1", "ts": "2026-10-14 10:00:00",
                 "value": 5e6, "board": "TQCB", "market": "bonds"}]

    def tape_stats(**k):
        calls["stats"] += 1
        return {"n": 1, "value": 5e6, "buy_value": 0, "sell_value": 0,
                "by_market": {}, "top": [], "archive_till": None}

    monkeypatch.setattr(tape_svc, "read_tape", read_tape)
    monkeypatch.setattr(tape_svc, "tape_stats", tape_stats)
    monkeypatch.setattr(tr, "_labels", lambda: {"RU000A0000A1": {"name": "X", "emitter": "E"}})
    monkeypatch.setattr(tr, "_moex_names", lambda: {})

    async def bases():
        return {}, "2026-10-07"
    monkeypatch.setattr(tr, "_bases", bases)
    monkeypatch.setattr(tr, "_scope_isins", lambda scope, labels: None)
    import services.trade_flags as tf
    monkeypatch.setattr(tf, "ids", lambda email: set())

    app = FastAPI()
    app.include_router(tr.router, prefix="/api/trades")
    app.dependency_overrides[tr.require_user] = lambda: {"email": "u@x.ru"}
    c = TestClient(app)
    c.calls = calls
    return c


def test_all_is_default_and_unchanged(client):
    j = client.get("/api/trades?days=1&min_value=1000000").json()
    assert client.calls == {"rows": 1, "stats": 1}
    assert len(j["trades"]) == 1 and j["summary"]["n"] == 1


def test_rows_part_skips_the_aggregate(client):
    j = client.get("/api/trades?days=1&part=rows").json()
    assert client.calls == {"rows": 1, "stats": 0}
    assert len(j["trades"]) == 1 and j["summary"] == {}


def test_summary_part_skips_the_rows(client):
    j = client.get("/api/trades?days=1&part=summary").json()
    assert client.calls == {"rows": 0, "stats": 1}
    assert j["trades"] == [] and j["summary"]["n"] == 1


def test_unknown_part_is_400(client):
    assert client.get("/api/trades?part=bogus").status_code == 400
