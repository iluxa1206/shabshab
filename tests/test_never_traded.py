"""Бумага без единой сделки и Y-IDX за верхом шкалы — признаки строки витрины.

Кейс 29.09.2026: Самолёт2P4/2P5 с листинга ни одной сделки на TQCB. PRICE в
витрине — PREVWAPRICE MOEX (цена размещения: у 2P5 — 75, доразмещение на
PSAU), спред посчитан к ней, и строка выглядела как обычный неликвид. Сырой
PREVPRICE борда у таких пуст: сверка по истории ISS — 92 из 92 без сделок.
"""
from api.routes.bonds import _never_traded, _uni_item
from services.market_data import MarketDataService


def test_empty_prevprice_without_trade_today_is_never_traded():
    assert _never_traded({"prev_trade": None, "last": None},
                         {"last": 75.0, "price_stale": True}) is True
    # цены нет вовсе (PREVWAPRICE тоже пуст) — тоже ни одной сделки
    assert _never_traded({"prev_trade": None}, {"last": None}) is True


def test_any_trade_ever_or_today_clears_the_flag():
    assert _never_traded({"prev_trade": 98.5}, {"last": 98.5, "price_stale": True}) is False
    # первая сделка сегодня: в снимке ISS …
    assert _never_traded({"prev_trade": None, "last": 99.1}, {"last": None}) is False
    # … или уже в потоке движка (цена сделки без метки prev-close)
    assert _never_traded({"prev_trade": None}, {"last": 99.1, "price_stale": False}) is False


def test_unknown_board_row_says_nothing():
    """Нет строки борда или колонку не тянули (marketdata-заход) — не знаем, а
    не «сделок не было»."""
    assert _never_traded(None, {"last": None}) is None
    assert _never_traded({"prev": 100.0, "last": None}, {"last": None}) is None


def test_marketdata_pass_keeps_prev_trade():
    """Внутридневной заход не несёт PREVPRICE и не должен стирать признак,
    добытый дневным."""
    prev = {"X": {"prev": 75.0, "prev_trade": None, "last": None}}
    fresh = {"X": {"prev": None, "last": 76.0, "bid": 75.5}}
    merged = MarketDataService._merge_board_rows(prev, fresh, "marketdata", True)
    assert "prev_trade" in merged["X"] and merged["X"]["prev_trade"] is None
    assert merged["X"]["last"] == 76.0


def _u():
    return {"isin": "RU000A10FE11", "base_rate_type": "KEYRATE",
            "spread_issue_bps": 0, "maturity_date": None, "issue_date": None}


def test_list_item_carries_both_flags():
    it = _uni_item(_u(), "Самолет2P5", {"last": 75.0, "price_stale": True, "yoi": 1002},
                   board_row={"prev_trade": None, "last": None})
    assert it.never_traded is True and it.y_idx_oos is False
    it = _uni_item(_u(), "X", {"last": 1.5, "yoi": None, "yoi_oos": True},
                   board_row={"prev_trade": 1.4})
    assert it.never_traded is False and it.y_idx_oos is True
    # число есть — флаг «вне шкалы» не нужен, даже если прилетел устаревшим
    it = _uni_item(_u(), "X", {"last": 46.0, "yoi": 21164, "yoi_oos": True})
    assert it.y_idx_oos is False and it.never_traded is None
