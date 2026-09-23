"""Свежесть котировочного пуша судится по ШАРДУ, а не по бумаге.

Alor шлёт котировку только на изменение: у тихой бумаги пуш стареет законно.
Прежний порог по возрасту пуша самой бумаги через две минуты тишины подменял
живой верх книги снапшотом ISS (задержка 15 минут) — см. _push_usable."""
import time

import pytest

from services import universe_stream as us


@pytest.fixture(autouse=True)
def clean():
    us._isin_shard.clear(); us._shards.clear()
    yield
    us._isin_shard.clear(); us._shards.clear()


def _shard(sid, up=True, last=None):
    us._shards[sid] = {**us._SHARD0, "up": up, "last": time.time() if last is None else last}


def test_fresh_push_is_usable_without_shard():
    assert us._push_usable("X", {"bid": 99.0, "_ts": time.time()})


def test_stale_push_of_live_shard_is_the_book():
    """Книга не двигалась десять минут, но сокет шарда живой — пуш и есть верх книги."""
    us._isin_shard["X"] = 7; _shard(7)
    q = {"bid": 99.0, "ask": 99.5, "_ts": time.time() - 600}
    assert us._push_usable("X", q)
    sides = us._sides_from(q, {"bid": 98.0, "ask": 100.0}, "X")
    assert (sides["bid"], sides["ask"]) == (99.0, 99.5)


def test_stale_push_of_dead_shard_falls_back_to_snapshot():
    us._isin_shard["X"] = 7; _shard(7, up=False, last=time.time() - 600)
    q = {"bid": 99.0, "ask": 99.5, "_ts": time.time() - 600}
    assert not us._push_usable("X", q)
    sides = us._sides_from(q, {"bid": 98.0, "ask": 100.0}, "X")
    assert (sides["bid"], sides["ask"]) == (98.0, 100.0)


def test_stale_push_of_silent_shard_falls_back():
    """Сокет числится поднятым, но сообщений давно нет — это и есть мёртвый шард."""
    us._isin_shard["X"] = 7; _shard(7, up=True, last=time.time() - 600)
    assert not us._push_usable("X", {"bid": 99.0, "_ts": time.time() - 600})


def test_stale_push_without_shard_keeps_old_rule():
    assert not us._push_usable("X", {"bid": 99.0, "_ts": time.time() - 600})
    assert us._push_usable("X", {"bid": 99.0})            # синтетика без метки


def test_live_sides_respects_shard(monkeypatch):
    us._isin_shard["X"] = 7; _shard(7)
    monkeypatch.setattr(us, "_last_quote", {"X": {"bid": 99.0, "_ts": time.time() - 600}})
    monkeypatch.setattr(us, "yoi_at", lambda isin, px: 150)
    assert us.live_sides("X") == {"bid": (99.0, 150)}
    _shard(7, up=False)
    assert us.live_sides("X") == {}
