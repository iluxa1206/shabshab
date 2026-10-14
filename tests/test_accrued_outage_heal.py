"""Сбой сети в полночь (13→14.10.2026): контексты пересобрались на пустом
снапшоте и пять часов стояли «без НКД». Две защиты: читатель не получает пустой
снимок при сбое ISS, а контексты без НКД лечатся сами, как только снимок вернулся.
"""
import asyncio

from services import market_data as md
from services import universe_stream as us


def test_reader_gets_previous_snapshot_when_iss_fails(monkeypatch):
    good = {"RU1": {"accrued": 12.3, "last": 100.0}}
    monkeypatch.setattr(md.MarketDataService, "_board_snap", good)
    monkeypatch.setattr(md.MarketDataService, "_board_snap_ts", 0.0)   # TTL истёк

    async def dead(boards=None, only=None):
        return {}
    monkeypatch.setattr(md.MarketDataService, "_fetch_board_snapshot_inner",
                        staticmethod(dead))
    assert asyncio.run(md.MarketDataService.fetch_board_snapshot()) is good
    # поллер (force) свою семантику сохраняет: пусто значит «свежего нет»
    assert asyncio.run(md.MarketDataService.fetch_board_snapshot(force=True)) == {}


def test_heal_fills_accrued_without_quote_movement(monkeypatch):
    us._eval_ctx.clear()
    us._dirty.clear()
    us._last_quote.clear()
    monkeypatch.setattr(us, "_drop_price_caches", lambda isin: None)
    try:
        us._eval_ctx["RU1"] = {"accrued_missing": True, "accrued_live": None}
        us._eval_ctx["RU2"] = {"accrued_missing": True, "accrued_live": None}
        us._last_quote["RU1"] = {"last_price": 100.0}
        board = {"RU1": {"accrued": 12.3, "accrued_date": "2026-10-14"},
                 "RU2": {"accrued": None}}
        assert us.heal_missing_accrued(board) == 1
        assert us._eval_ctx["RU1"]["accrued_live"] == 12.3
        assert us._eval_ctx["RU1"]["accrued_missing"] is False
        assert us._eval_ctx["RU2"]["accrued_missing"] is True      # снимок НКД не принёс
        assert "RU1" in us._dirty                                   # строку на пересчёт
        assert us.heal_missing_accrued({}) == 0                     # пустой снимок не лечит
    finally:
        us._eval_ctx.clear()
        us._dirty.clear()
        us._last_quote.clear()
