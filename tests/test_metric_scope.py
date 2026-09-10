"""Движок не считает то, чего никто не видит.

Спреды к биду и офферу считает отдельная очередь: по логам прода 240-790 строк
в минуту по ~11 мс на строку. При выключенных колонках эта работа уходила в
пустоту, забирая такт у того, что на экране есть.

Главная опасность правки — тихо выключить чужие оповещения: фильтры СТАКАНА
сравнивают спред по биду и офферу и работают как раз тогда, когда ни одной
вкладки не открыто.
"""
import time

import pytest

from services import universe_stream as us


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(us, "_scope", {}, raising=False)
    monkeypatch.setattr(us, "_sides_filter", {"at": 0.0, "need": False}, raising=False)
    monkeypatch.setattr(us, "_sides_dirty", {}, raising=False)
    monkeypatch.setattr("services.signals.list_enabled", lambda: [])


def test_no_orders_yet_means_compute():
    """Свежий старт, никто ещё не подключался — молчание клиента не означает,
    что стороны не нужны."""
    assert us.sides_needed() is True


def test_visible_side_column_orders_compute():
    us.register_metric_scope(["y_idx_ask_bps", "dm_bps"])
    assert us.sides_needed() is True


def test_hidden_side_columns_stop_compute():
    us.register_metric_scope(["dm_bps", "short_name"])
    assert us.sides_needed() is False


def test_book_filters_win_over_hidden_columns(monkeypatch):
    """Колонки выключены, но у кого-то включён фильтр стакана — считаем.
    Иначе сигнал в телеграм тихо перестал бы приходить."""
    monkeypatch.setattr("services.signals.list_enabled", lambda: [{"id": 1}])
    us.register_metric_scope(["dm_bps"])
    assert us.sides_needed() is True


def test_unreadable_filters_mean_compute(monkeypatch):
    """Не смогли спросить базу — считаем: тихо погасить сигналы хуже, чем
    потратить такт."""
    def boom():
        raise RuntimeError("база занята")
    monkeypatch.setattr("services.signals.list_enabled", boom)
    us.register_metric_scope(["dm_bps"])
    assert us.sides_needed() is True


def test_queue_is_cleared_not_piled_when_unwanted(monkeypatch):
    """Очередь при выключенных колонках ЧИСТИМ: иначе к моменту включения
    накопится хвост в тысячи бумаг и первый такт уйдёт в него целиком."""
    us.register_metric_scope(["dm_bps"])
    us._sides_dirty.update({"A": 0.0, "B": 1.0})
    take, prio = us._take_sides_batch()
    assert take == [] and prio == {}
    assert not us._sides_dirty


def test_turning_column_back_on_queues_a_wave(monkeypatch):
    """Пока колонку не смотрели, очередь не копилась. Без волны числа ждали бы
    движения книги, а у застывшего неликвида повода может не быть весь день."""
    monkeypatch.setattr(us, "_streamed", {"RU000A1", "RU000A2"}, raising=False)
    monkeypatch.setattr(us, "_fixed_isins", set(), raising=False)
    us.register_metric_scope(["dm_bps"])
    assert not us._sides_dirty
    us.register_metric_scope(["y_idx_bid_bps"])
    assert set(us._sides_dirty) == {"RU000A1", "RU000A2"}


def test_scope_expires(monkeypatch):
    """Вкладку закрыли — заказ истекает по общему TTL."""
    us.register_metric_scope(["y_idx_ask_bps"])
    assert us.sides_needed() is True
    stale = {k: t - us._VOL_TTL_SEC - 1 for k, t in us._scope.items()}
    monkeypatch.setattr(us, "_scope", stale, raising=False)
    us.register_metric_scope([])          # любой заход подчищает протухшее
    assert us._scope == {}
