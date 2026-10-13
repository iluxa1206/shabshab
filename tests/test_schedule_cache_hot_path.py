"""fetch_coupon_schedules не читает диск (json.load ~1 МБ в event loop), пока
всё нужное уже лежит в памяти процесса."""
import asyncio

from services import market_data as md


def test_memory_hit_does_not_touch_disk(monkeypatch):
    md.MarketDataService._schedule_mem_date = md._trading_day()
    md.MarketDataService._schedule_mem = {"RU1": [("a", "b", 1.0)]}
    monkeypatch.setattr(md, "_load_schedule_cache",
                        lambda: (_ for _ in ()).throw(AssertionError("диск прочитан")))
    monkeypatch.setattr(md, "_strip_echo_triples", lambda i, s: s)
    got = asyncio.run(md.MarketDataService.fetch_coupon_schedules(["RU1"]))
    assert got == {"RU1": [("a", "b", 1.0)]}
