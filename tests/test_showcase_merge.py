"""Проход витрины и строки движка живут в одном словаре — правила их встречи.

Десятиминутный universe_price_poller считает ~1300 бумаг десятки секунд, а
движок всё это время пересчитывает строки по сделкам. Раньше проход клал
`market_cache["universe_metrics"] = metrics` — замену ВСЕГО словаря: работа
движка за время прохода терялась, поля, которых у прохода нет вовсе
(vol_px / yoi_vol — цена набора на объём и её спред), обнулялись по всему рынку,
а пересчёт после отката никто не заказывал.
"""
import time

from services import universe_stream as us


def _mc(rows=None):
    return {"universe_metrics": dict(rows or {})}


def test_engine_row_newer_than_pass_survives():
    """Строка, посчитанная движком ПОСЛЕ старта прохода, побеждает целиком."""
    t0 = time.time()
    mc = _mc({"A": {"last": 99.8, "yoi": 350, "_calc_ts": t0 + 5}})
    kept, merged = us.merge_universe_metrics(mc, {"A": {"last": 99.2, "yoi": 300}}, t0)
    assert (kept, merged) == (1, 0)
    row = mc["universe_metrics"]["A"]
    assert row["last"] == 99.8 and row["yoi"] == 350


def test_engine_only_fields_survive_the_pass():
    """Строка прохода свежее, но поля, которых он не считает, остаются: их
    кладёт только движок (цена набора на объём и её спред)."""
    t0 = time.time()
    mc = _mc({"A": {"last": 99.2, "yoi": 300, "_calc_ts": t0 - 30,
                    "vol_px": {"bid:5000000": 99.1}, "yoi_vol": {"bid:5000000": 312}}})
    kept, merged = us.merge_universe_metrics(mc, {"A": {"last": 99.5, "yoi": 320}}, t0)
    assert (kept, merged) == (0, 1)
    row = mc["universe_metrics"]["A"]
    assert row["last"] == 99.5 and row["yoi"] == 320        # проход новее — его числа
    assert row["vol_px"] == {"bid:5000000": 99.1}           # поле движка цело
    assert row["yoi_vol"] == {"bid:5000000": 312}


def test_row_outside_the_pass_is_dropped():
    """Бумага, которой в проходе нет, уходит из витрины — проход идёт по всему
    универсу, значит её там больше нет (делистинг, смена типа)."""
    t0 = time.time()
    mc = _mc({"A": {"last": 99.2, "_calc_ts": t0 - 1}, "GONE": {"last": 50.0}})
    us.merge_universe_metrics(mc, {"A": {"last": 99.5}}, t0)
    assert set(mc["universe_metrics"]) == {"A"}


def test_merged_row_is_marked_by_pass_start():
    """Слитая строка помечена НАЧАЛОМ прохода: следующий пересчёт движка её
    честно перебьёт, даже если проход закончился позже."""
    t0 = time.time()
    mc = _mc({"A": {"last": 99.2, "_calc_ts": t0 - 30}})
    us.merge_universe_metrics(mc, {"A": {"last": 99.5}}, t0)
    assert mc["universe_metrics"]["A"]["_calc_ts"] == t0


def test_store_rows_stamps_calc_time():
    """Движок штампует строку временем расчёта — по нему проход и различает,
    чья строка новее."""
    mc = _mc()
    before = time.time()
    us._store_rows(mc, {"A": {"yoi": 111}})
    assert mc["universe_metrics"]["A"]["_calc_ts"] >= before


def test_price_change_by_exchange_queues_recalc():
    """Смена цены по бирже заказывает полный пересчёт: пуша Alor по этой бумаге
    может не быть вовсе (шард отвалился, неликвида нет в стриме)."""
    us._dirty.clear()
    try:
        assert us.queue_price_change(["A", "B"]) == 2
        assert us.queue_price_change(["A"]) == 0      # уже в очереди — не дубль
        assert us._dirty == {"A", "B"}
    finally:
        us._dirty.clear()
