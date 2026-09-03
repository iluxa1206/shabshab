"""Дисциплина записи в витрину: у строки несколько писателей.

Полный пересчёт, очередь сторон и волна размера тикета работают по схеме
«скопировать строку из universe_metrics в heavy-потоке → вернуться в петлю →
записать». Копия и запись разделены await'ом, поэтому две последовательности
переплетаются, и вернувшаяся позже кладёт строку, прочитанную РАНЬШЕ. Бумага
после этого ни в одну очередь не встаёт — событий на неё больше нет.

Лечение двустороннее: писать СЛИЯНИЕМ по полям и возвращать ТОЛЬКО свои поля.
"""
import time

import pytest

from services import universe_stream as us


def _mc(rows=None):
    return {"universe_metrics": dict(rows or {})}


def test_store_rows_merges_instead_of_replacing():
    """Поля, которых нет в новой строке, переживают запись."""
    mc = _mc({"A": {"yoi": 300, "vol_px": {"bid:5000000": 99.1}, "face_px": 1000.0}})
    us._store_rows(mc, {"A": {"yoi": 320}})
    row = mc["universe_metrics"]["A"]
    assert row["yoi"] == 320                       # своё поле обновлено
    assert row["vol_px"] == {"bid:5000000": 99.1}  # чужое цело
    assert row["face_px"] == 1000.0


def test_cheap_branch_returns_only_its_own_fields(monkeypatch):
    """recrunch_sides отдаёт стороны, средневзвес и цены наборов — и ничего из
    того, что принадлежит полному пересчёту."""
    import services.yidx_exact as ye
    from services.market_data import market_cache

    isin = "RU000A100001"
    monkeypatch.setattr(ye, "y_idx_many",
                        lambda ctx, prices: {round(float(p), 4): 111 for p in prices})
    market_cache["universe_metrics"] = {
        isin: {"yoi": 300, "dm": 250, "preferred_horizon": "put", "face_px": 1000.0},
    }
    us._eval_ctx[isin] = {"isin": isin}
    us._last_quote[isin] = {"bid": 99.8, "ask": 100.1, "_ts": time.time()}
    try:
        out = us.recrunch_sides([isin], {})
    finally:
        us._eval_ctx.pop(isin, None)
        us._last_quote.pop(isin, None)
        market_cache.pop("universe_metrics", None)

    row = out[isin]
    assert row["bid"] == 99.8 and row["yoi_bid"] == 111
    # числа полного пересчёта дешёвая ветка не возит: её копия строки была
    # прочитана ДО await'а, и записью она затёрла бы работу движка
    for k in ("yoi", "dm", "preferred_horizon", "face_px"):
        assert k not in row


def test_engine_work_survives_a_late_cheap_write():
    """Сценарий гонки целиком: волна размера тикета прочитала строку, движок за
    это время пересчитал стороны, волна записала свою копию последней."""
    mc = _mc({"A": {"yoi_bid": 200, "yoi_ask": 205}})
    # T0: волна сняла копию (в новой схеме — только свои поля)
    wave = {"A": {"vol_px": {"bid:5000000": 99.1}, "yoi_vol": {"bid:5000000": 212}}}
    # T1: движок положил свежие стороны
    us._store_rows(mc, {"A": {"yoi_bid": 240, "yoi_ask": 246}})
    # T2: волна проснулась и записала своё
    us._store_rows(mc, wave)
    row = mc["universe_metrics"]["A"]
    assert row["yoi_bid"] == 240 and row["yoi_ask"] == 246   # работа движка цела
    assert row["yoi_vol"] == {"bid:5000000": 212}            # и числа волны тоже


def test_dirty_batch_takes_visible_first(monkeypatch):
    """Пачка полного пересчёта: видимые вперёд, остальное — повторяемым порядком."""
    us._dirty.clear()
    us._visible.clear()
    try:
        us._dirty.update({"A", "B", "C", "D"})
        us._visible["C"] = time.monotonic()
        monkeypatch.setattr(us, "_MAX_BATCH", 2)
        take = us._take_dirty_batch()
        assert take[0] == "C"                  # её смотрят прямо сейчас
        assert len(take) == 2
        assert not (set(take) & us._dirty)     # снятое из очереди убрано
    finally:
        us._dirty.clear()
        us._visible.clear()


def test_dirty_batch_order_is_stable():
    """Без видимых порядок задаётся ISIN, а не хешами строк."""
    us._dirty.clear()
    us._visible.clear()
    try:
        us._dirty.update({"RU3", "RU1", "RU2"})
        assert us._take_dirty_batch() == ["RU1", "RU2", "RU3"]
    finally:
        us._dirty.clear()
