"""Верх стакана в котировках: цена и спред приезжают ОДНОЙ парой.

02.09.2026: цены сторон шли из борд-снапшота ISS, спред — из движка (книга
Alor). Источники с разной задержкой: пока бумага «живая», в строке стояла цена
Alor и спред к ней подходил; замолчала — снапшот откатывал цену назад и гасил
спред, а число движка к откаченной цене уже не подходило. Прочерк держался до
следующего движения книги."""
import asyncio

import pytest

from api.routes.bonds import get_quotes, _QUOTE_LOG, _QUOTE_LOG_TS


@pytest.fixture(autouse=True)
def _clean():
    _QUOTE_LOG.clear()
    _QUOTE_LOG_TS.clear()
    yield


def _run(monkeypatch, snap, metrics):
    from api.routes import bonds as mod

    async def fake_snap(*a, **k):
        return snap
    monkeypatch.setattr(mod.MarketDataService, "fetch_board_snapshot", fake_snap)
    # market_cache — обычный dict, роут импортирует его внутри функции: кладём
    # метрики прямо в него и убираем за собой
    import services.market_data as md
    md.market_cache["universe_metrics"] = metrics
    monkeypatch.setattr(mod.live_quotes, "get", lambda i: {})
    try:
        r = asyncio.run(get_quotes(vol_bid=None, vol_ask=None, since=None, epoch=None))
    finally:
        md.market_cache.pop("universe_metrics", None)
    return {i["isin"]: i for i in r["items"]}


def test_engine_price_wins_over_stale_snapshot(monkeypatch):
    """Ровно случай БалтЛизП10: снапшот 93,90/93,91, движок 93,98/94,12."""
    got = _run(monkeypatch,
               {"X": {"last": 93.9, "bid": 93.90, "ask": 93.91}},
               {"X": {"bid": 93.98, "ask": 94.12, "yoi_bid": 3038, "yoi_ask": 2964}})
    assert got["X"]["bid"] == 93.98 and got["X"]["ask"] == 94.12
    # пара согласована: спред относится ровно к той цене, что уехала клиенту
    assert got["X"]["yoi_bid_px"] == got["X"]["bid"]
    assert got["X"]["yoi_ask_px"] == got["X"]["ask"]


def test_snapshot_is_fallback_outside_engine_universe(monkeypatch):
    """В ответе бумаг вчетверо больше, чем движок считает: их цены — из ISS."""
    got = _run(monkeypatch, {"Y": {"last": 100.0, "bid": 99.9, "ask": 100.1}}, {})
    assert got["Y"]["bid"] == 99.9 and got["Y"]["ask"] == 100.1
    assert "yoi_bid" not in got["Y"]


def test_price_without_spread_still_comes_from_engine(monkeypatch):
    """Движок знает цену, но спред ещё не посчитал: цена всё равно его — иначе
    вернулись бы разные источники в одной строке."""
    got = _run(monkeypatch,
               {"Z": {"last": 100.0, "bid": 99.9, "ask": 100.1}},
               {"Z": {"bid": 99.95, "yoi_bid": None}})
    assert got["Z"]["bid"] == 99.95        # из движка, спреда к ней пока нет
    assert got["Z"]["ask"] == 100.1        # ключа стороны нет вовсе — снапшот
    assert "yoi_bid" not in got["Z"]


def test_side_gone_from_engine_falls_back_not_crashes(monkeypatch):
    got = _run(monkeypatch,
               {"W": {"last": 100.0, "bid": None, "ask": 100.1}},
               {"W": {"bid": None, "ask": 100.15, "yoi_ask": 210}})
    assert got["W"]["bid"] is None
    assert got["W"]["ask"] == 100.15 and got["W"]["yoi_ask_px"] == 100.15


def test_engine_knows_side_is_gone(monkeypatch):
    """Движок видит снятую заявку — его пустота побеждает снапшот, иначе в
    строку вернулась бы цена, которой на рынке уже нет."""
    got = _run(monkeypatch,
               {"V": {"last": 100.0, "bid": 99.9, "ask": 100.1}},
               {"V": {"bid": None, "ask": 100.1, "yoi_ask": 200}})
    assert got["V"]["bid"] is None
    assert "yoi_bid" not in got["V"]


def test_row_without_side_keys_keeps_snapshot(monkeypatch):
    """Строка движка чужой структуры (нет ключей сторон) — снапшот остаётся."""
    got = _run(monkeypatch,
               {"U": {"last": 100.0, "bid": 99.9, "ask": 100.1}},
               {"U": {"yoi": 150}})
    assert got["U"]["bid"] == 99.9 and got["U"]["ask"] == 100.1
