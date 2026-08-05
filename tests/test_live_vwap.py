"""Живой средневзвес дня: накопитель Σ(price·qty)/Σ(qty) по потоку сделок.

Цифра в таблице обязана сходиться со слоем «Средневзвес» на графике, поэтому
VWAP считается по тем же тикам Alor, что лежат в архиве, а не берётся из
биржевого WAPRICE. Здесь проверяется арифметика, стык «архив ↔ поток» (Alor при
подписке отдаёт последние сделки, часть уже в архиве) и смена торгового дня.
"""
import asyncio

import pytest

from services import live_quotes as lq


@pytest.fixture(autouse=True)
def clean_state():
    lq._state.clear()
    yield
    lq._state.clear()


def test_vwap_weighted_by_quantity():
    lq.add_trade("RU000A100001", 100.0, 10, tid=1)
    lq.add_trade("RU000A100001", 101.0, 30, tid=2)
    v = lq.get("RU000A100001")
    # (100·10 + 101·30) / 40 = 100.75 — взвешивание по объёму, не среднее цен
    assert v["vwap_pct"] == pytest.approx(100.75)
    assert v["volume"] == 40
    assert v["trades"] == 2


def test_duplicate_trade_id_ignored():
    """Стык архива и потока: одна сделка не должна попасть в агрегат дважды."""
    lq.add_trade("RU000A100001", 100.0, 10, tid=7)
    lq.add_trade("RU000A100001", 100.0, 10, tid=7)
    lq.add_trade("RU000A100001", 200.0, 10, tid=8)
    v = lq.get("RU000A100001")
    assert v["volume"] == 20
    assert v["vwap_pct"] == pytest.approx(150.0)


def test_no_trades_returns_none():
    assert lq.get("RU000A100001") is None
    lq.add_trade("RU000A100001", 100.0, 0, tid=1)      # нулевой объём — не сделка
    assert lq.get("RU000A100001") is None


def test_new_day_resets_accumulator(monkeypatch):
    lq.add_trade("RU000A100001", 100.0, 10, tid=1)
    assert lq.get("RU000A100001")["volume"] == 10

    monkeypatch.setattr(lq, "_today", lambda: "2099-01-01")
    assert lq.get("RU000A100001") is None                # вчерашний агрегат не отдаём
    lq.add_trade("RU000A100001", 50.0, 4, tid=1)         # тот же id, но новый день
    assert lq.get("RU000A100001") == {"vwap_pct": 50.0, "volume": 4, "trades": 1}


def test_drop_clears_state():
    lq.add_trade("RU000A100001", 100.0, 10, tid=1)
    lq.drop("RU000A100001")
    assert lq.get("RU000A100001") is None
    assert "RU000A100001" not in lq.active()


def test_ensure_day_seeds_from_archive(monkeypatch):
    """Агрегат поднимается из архива, а поток дополняет его без двойного счёта."""
    day = lq._today()
    archive = [(1, 100.0, 10, f"{day} 10:00:00"), (2, 102.0, 10, f"{day} 10:05:00")]
    monkeypatch.setattr(lq, "read_day_ticks", lambda isin, d: archive)

    asyncio.run(lq.ensure_day("RU000A100001", drain=False))
    assert lq.get("RU000A100001")["vwap_pct"] == pytest.approx(101.0)

    lq.add_trade("RU000A100001", 102.0, 10, tid=2)       # уже в архиве — дубль
    lq.add_trade("RU000A100001", 106.0, 10, tid=3)       # новая из потока
    v = lq.get("RU000A100001")
    assert v["volume"] == 30
    assert v["vwap_pct"] == pytest.approx((100.0 * 10 + 102.0 * 10 + 106.0 * 10) / 30)


def test_ensure_day_runs_once(monkeypatch):
    """Повторная подписка не должна задваивать дневной объём."""
    day = lq._today()
    calls = []

    def fake_read(isin, d):
        calls.append(isin)
        return [(1, 100.0, 10, f"{day} 10:00:00")]

    monkeypatch.setattr(lq, "read_day_ticks", fake_read)
    asyncio.run(lq.ensure_day("RU000A100001", drain=False))
    asyncio.run(lq.ensure_day("RU000A100001", drain=False))
    assert len(calls) == 1
    assert lq.get("RU000A100001")["volume"] == 10
