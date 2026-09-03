"""Цена набора и спред по ней не ждут очереди движка.

Оба попадали в строку, только когда движок доберётся до бумаги в очереди
сторон — круг по универсу две-три минуты, и всё это время фильтр по объёму
показывал прочерк. Между тем цена набора считается по кэшу глубины
арифметикой, а спред на любой цене берётся из готовой сетки."""
import asyncio

import pytest

from api.routes.bonds import get_quotes, _QUOTE_LOG, _QUOTE_LOG_TS


@pytest.fixture(autouse=True)
def _clean():
    _QUOTE_LOG.clear()
    _QUOTE_LOG_TS.clear()
    yield


def _run(monkeypatch, metrics, *, vol_px=None, grid=None):
    from api.routes import bonds as mod
    import services.market_data as md

    async def fake_snap(*a, **k):
        return {"X": {"last": 100.0, "bid": 99.9, "ask": 100.1}}
    monkeypatch.setattr(mod.MarketDataService, "fetch_board_snapshot", fake_snap)
    monkeypatch.setattr(mod.live_quotes, "get", lambda i: {})
    # роут импортирует их внутри функции — патчим источник
    import services.universe_stream as us
    monkeypatch.setattr(us, "_vol_prices", lambda isin, **kw: vol_px or {})
    monkeypatch.setattr(us, "yoi_at", lambda isin, px: (grid or {}).get(round(px, 4)))
    monkeypatch.setattr(us, "register_vol_sizes", lambda sizes: None)
    md.market_cache["universe_metrics"] = metrics
    try:
        r = asyncio.run(get_quotes(vol_bid=5_000_000, vol_ask=None,
                                   since=None, epoch=None))
    finally:
        md.market_cache.pop("universe_metrics", None)
    return {i["isin"]: i for i in r["items"]}


def test_ready_numbers_are_used_as_is(monkeypatch):
    got = _run(monkeypatch,
               {"X": {"bid": 99.9, "ask": 100.1,
                      "vol_px": {"bid:5000000": 99.8},
                      "yoi_vol": {"bid:5000000": 240}}})
    assert got["X"]["vol_bid_px"] == 99.8 and got["X"]["vol_bid_y"] == 240


def test_price_computed_now_when_row_has_none(monkeypatch):
    """Движок бумагу ещё не трогал: цену набора считаем по глубине сразу."""
    got = _run(monkeypatch, {"X": {"bid": 99.9, "ask": 100.1}},
               vol_px={"bid:5000000": 99.75}, grid={99.75: 235})
    assert got["X"]["vol_bid_px"] == 99.75
    assert got["X"]["vol_bid_y"] == 235          # из сетки, без очереди


def test_spread_from_grid_when_only_price_is_ready(monkeypatch):
    """Движок посчитал цену набора, но спред к ней ещё нет: сетка отвечает по
    ТОЙ ЖЕ цене, поэтому пара остаётся согласованной."""
    got = _run(monkeypatch,
               {"X": {"bid": 99.9, "ask": 100.1, "vol_px": {"bid:5000000": 99.8}}},
               grid={99.8: 250})
    assert got["X"]["vol_bid_px"] == 99.8 and got["X"]["vol_bid_y"] == 250


def test_live_price_without_spread_is_not_sent(monkeypatch):
    """Сетки нет — живую цену набора придержим. Она новее всего в строке, и
    рядом со спредом от прошлого прохода дала бы рассинхрон 27.08.2026: пара
    выглядит согласованной и врёт."""
    got = _run(monkeypatch, {"X": {"bid": 99.9, "ask": 100.1}},
               vol_px={"bid:5000000": 99.75}, grid={})
    assert "vol_bid_px" not in got["X"] and "vol_bid_y" not in got["X"]


def test_engine_price_without_spread_goes_with_explicit_null(monkeypatch):
    """Цену ОТ ДВИЖКА отдаём (она уже была в строке — придержать значило бы
    отнять у витрины показанное), но спред едет рядом ЯВНЫМ null.

    Отсутствующее поле на клиенте прежнее число не стирает: присвоение идёт по
    наличию ключа. Без явного null в ячейке вставала новая цена набора со
    спредом от ПРЕДЫДУЩЕЙ цены — пара выглядела согласованной и врала."""
    got = _run(monkeypatch,
               {"X": {"bid": 99.9, "ask": 100.1, "vol_px": {"bid:5000000": 99.8}}},
               grid={})
    assert got["X"]["vol_bid_px"] == 99.8
    assert "vol_bid_y" in got["X"] and got["X"]["vol_bid_y"] is None


def test_set_not_collected_gives_nothing(monkeypatch):
    """Книги не хватило на тикет — ни цены, ни спреда."""
    got = _run(monkeypatch, {"X": {"bid": 99.9, "ask": 100.1}},
               vol_px={"bid:5000000": None}, grid={99.75: 235})
    assert "vol_bid_px" not in got["X"] and "vol_bid_y" not in got["X"]
