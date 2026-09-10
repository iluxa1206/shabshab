"""Такт котировок и разделение board-снапшота по времени жизни полей.

Пятисекундный такт стоил 36 700 запросов к ISS в сутки — три полных дампа
бордов каждые 5 секунд, 17 часов подряд. 10.09.2026 биржа перестала пускать наш
адрес. Столько котировок через ISS не нужно: цены, bid и ask по всему рынку
приходят push-стримом Alor. У ISS остаётся НКД, вчерашнее закрытие и дата
поставки (дневные) плюс средневзвес и оборот (внутридневные) — у них разное
время жизни, и тянуть их одним тактом незачем.
"""
from datetime import datetime, timedelta, timezone

import pytest

import api.main as m
from services.market_data import MarketDataService as MDS

_MSK = timezone(timedelta(hours=3))


class _Manager:
    def __init__(self, watching):
        self.client_subs = {"sock": {}} if watching else {}


@pytest.fixture
def watchers(monkeypatch):
    def _set(watching):
        from api.routes import ws as wsmod
        monkeypatch.setattr(wsmod, "manager", _Manager(watching))
    return _set


def test_interval_is_the_same_for_everyone(watchers):
    """Особого такта «никто не смотрит» быть не должно: за витриной живут
    сигналы и телеграм-алерты, они работают как раз при закрытых вкладках."""
    watchers(True)
    assert m._quotes_interval() == m.QUOTES_POLL_INTERVAL
    watchers(False)
    assert m._quotes_interval() == m.QUOTES_POLL_INTERVAL


def test_daily_block_is_rare_enough():
    """НКД появляется на начало дня, prev — после закрытия. Полчаса с запасом."""
    assert m.QUOTES_DAILY_REFRESH_SEC >= 900


def test_budget_dropped_at_least_tenfold():
    """Смысл правки — в числе запросов к ISS, поэтому считаем его прямо здесь."""
    old = 3 * (17 * 3600 / 5)                     # три борда, такт 5 с
    boards = len(MDS._SNAP_BOARDS)
    new = boards * (17 * 3600 / m.QUOTES_POLL_INTERVAL)          # marketdata
    new += boards * (17 * 3600 / m.QUOTES_DAILY_REFRESH_SEC)     # securities
    assert old > 36000
    assert new < old / 15, f"экономия меньше пятнадцатикратной: {old:.0f} → {new:.0f}"


def test_intraday_merge_keeps_daily_fields():
    """marketdata-заход НЕ должен стирать НКД, добытый дневным заходом —
    иначе спред снова поедет на суррогате, как 10.09."""
    prev = {"RU000TEST0001": {"accrued": 10.48, "prev": 100.16,
                              "prev_date": "2026-09-09", "accrued_date": "2026-09-11",
                              "last": 100.2, "waprice": 100.18, "vol": 5e6,
                              "bid": 100.1, "ask": 100.3}}
    fresh = {"RU000TEST0001": {"accrued": None, "prev": None, "prev_date": None,
                               "accrued_date": None,
                               "last": 100.5, "waprice": 100.4, "vol": 9e6,
                               "bid": 100.4, "ask": 100.6}}
    got = MDS._merge_board_rows(prev, fresh, "marketdata", full_boards=True)["RU000TEST0001"]
    assert got["accrued"] == 10.48, "НКД дневного захода затёрт внутридневным"
    assert got["prev"] == 100.16
    assert got["last"] == 100.5 and got["vol"] == 9e6, "внутридневные не обновились"


def test_daily_merge_keeps_intraday_fields():
    """И симметрично: дневной заход не должен откатывать оборот и средневзвес."""
    prev = {"X": {"accrued": 1.0, "prev": 99.0, "prev_date": "a", "accrued_date": "b",
                  "last": 100.5, "waprice": 100.4, "vol": 9e6, "bid": None, "ask": None}}
    fresh = {"X": {"accrued": 2.0, "prev": 99.5, "prev_date": "c", "accrued_date": "d",
                   "last": None, "waprice": None, "vol": None, "bid": None, "ask": None}}
    got = MDS._merge_board_rows(prev, fresh, "securities", full_boards=True)["X"]
    assert got["accrued"] == 2.0 and got["prev"] == 99.5
    assert got["last"] == 100.5 and got["vol"] == 9e6


def test_full_pass_replaces_snapshot():
    """Полный заход заменяет снимок целиком: делистингованная бумага обязана
    из него исчезнуть, а не жить вечно."""
    prev = {"OLD": {"accrued": 1.0}, "KEEP": {"accrued": 2.0}}
    fresh = {"KEEP": {"accrued": 3.0}}
    got = MDS._merge_board_rows(prev, fresh, None, full_boards=True)
    assert got == fresh and "OLD" not in got


def test_partial_boards_do_not_drop_others():
    """Опрос части бордов не должен стирать бумаги остальных."""
    prev = {"TQOB_BOND": {"accrued": 1.0}}
    fresh = {"TQCB_BOND": {"accrued": 2.0}}
    got = MDS._merge_board_rows(prev, fresh, None, full_boards=False)
    assert set(got) == {"TQOB_BOND", "TQCB_BOND"}


def test_field_sets_do_not_overlap():
    """Поля разведены строго: пересечение означало бы, что один заход молча
    перетирает результат другого."""
    assert not set(MDS._DAILY_FIELDS) & set(MDS._INTRADAY_FIELDS)


# --- лента сделок ---------------------------------------------------------

def test_trades_feed_tick_is_relaxed():
    """После разгрузки котировок лента стала самым тяжёлым потребителем ISS:
    две сквозные ленты (bonds и ndm) на каждый такт."""
    assert m.BLOCK_POLL_INTERVAL >= 120


def test_trades_feed_budget():
    """Два запроса на такт (по ленте на рынок), только в торговые часы."""
    per_tick = 2
    old = per_tick * (17 * 3600 / 60)
    new = per_tick * (17 * 3600 / m.BLOCK_POLL_INTERVAL)
    assert old - new > 1000, f"экономия мельче тысячи запросов: {old:.0f} → {new:.0f}"


def test_page_size_still_covers_a_tick():
    """Такт можно растягивать, только пока сделки за него влезают в одну
    страницу — иначе выигрыш съедается лишними страницами."""
    from services import block_trades as bt
    assert bt._PAGE >= 5000
