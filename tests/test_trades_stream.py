"""Живая лента сделок из Alor WS: буфер пушей → trade_tick.

Проверяем ровно то, что ломается молча: рублёвый объём считается по номиналу
бумаги (у амортизируемых он не 1000), а повторный пуш той же сделки не плодит
дублей — ISS-копия того же TRADENO доедет позже своей лентой.
"""
import importlib

import pytest


@pytest.fixture()
def ts(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.trades_archive as ta
    importlib.reload(ta)
    import services.trades_stream as mod
    importlib.reload(mod)
    return pdb, mod


def _push(mod, tid, price=100.0, qty=10):
    mod._on_trade("RU000TEST01", {"id": tid, "price": price, "qty": qty,
                                  "time": "2026-08-12T09:15:00Z", "side": "buy",
                                  "board": "TQCB"})


def test_push_writes_tick_with_face(ts):
    pdb, mod = ts
    _push(mod, 1, price=99.0, qty=25)
    chunks = [(i, r) for i, r in mod._buf.items()]
    assert mod._flush_sync(chunks, {"RU000TEST01": 800.0}) == 1
    with pdb._connect() as c:
        r = dict(c.execute("SELECT * FROM trade_tick").fetchone())
    assert r["ts"] == "2026-08-12 12:15:00"          # UTC пуша → МСК архива
    assert r["side"] == "buy" and r["board"] == "TQCB"
    # 25 бумаг × 800 ₽ × 99% — номинал берётся амортизированный, не дефолтный
    assert r["value"] == pytest.approx(19800.0)


def test_same_trade_twice_is_one_row(ts):
    """Пуш повторился (реконнект) — INSERT OR IGNORE по (isin, trade_id)."""
    pdb, mod = ts
    _push(mod, 7)
    assert mod._flush_sync(list(mod._buf.items()), {}) == 1
    mod._buf.clear()
    _push(mod, 7)
    assert mod._flush_sync(list(mod._buf.items()), {}) == 0
    with pdb._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM trade_tick").fetchone()[0] == 1


def test_broken_push_ignored(ts):
    """Пуш без цены/объёма в архив не попадает и буфер не засоряет."""
    pdb, mod = ts
    mod._on_trade("RU000TEST01", {"id": 1, "price": None, "qty": 5})
    mod._on_trade("RU000TEST01", {"id": 2, "price": 100.0, "qty": 0})
    assert mod._buf == {}
