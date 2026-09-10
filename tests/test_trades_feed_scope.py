"""Безадресные сделки не тянем из ISS, пока их даёт стрим Alor.

Сквозная лента market=bonds дублирует ровно то, что уже пришло пушем, только с
задержкой 15 минут — склейка по TRADENO дублей не создаёт, но и новой
информации не приносит. Адресные (ndm) стрим не покрывает никогда: подписки на
них у брокера нет.

Отдельно важен фолбэк: стрим встаёт (прод 28.08.2026 — сокеты открыты, данных
нет), и тогда лента ISS обязана включиться сама, иначе крупные сделки просто
пропадут из ленты.
"""
import time

import pytest

from services import block_trades as bt
from services import trades_stream as ts


@pytest.fixture
def stream(monkeypatch):
    def _set(shards_up, shards_total, quiet_min, streamed=True):
        now = time.time()
        shards = {}
        for i in range(shards_total):
            shards[i] = {"up": i < shards_up, "last": now - quiet_min * 60,
                         "isins": 10, "ticks": 1, "resubs": 0, "errors": 0}
        monkeypatch.setattr(ts, "_shards", shards, raising=False)
        monkeypatch.setattr(ts, "_streamed", {"RU000TEST"} if streamed else set(),
                            raising=False)
        # «yld только что добирали» — иначе каждый тест ловил бы редкий проход
        # по bonds и проверял не то, что хотел
        monkeypatch.setattr(bt, "_bonds_at", time.monotonic(), raising=False)
    return _set


def test_healthy_stream_means_ndm_only(stream):
    stream(shards_up=5, shards_total=5, quiet_min=0.5)
    assert ts.covers_market() is True
    assert bt.markets_to_sweep() == ("ndm",)


def test_silent_sockets_are_not_coverage(stream):
    """Сокет может висеть открытым и молчать — на этом уже обжигались."""
    stream(shards_up=5, shards_total=5, quiet_min=30)
    assert ts.covers_market() is False
    assert bt.markets_to_sweep() == bt.MARKETS


def test_half_dead_pool_falls_back(stream):
    stream(shards_up=2, shards_total=5, quiet_min=0.5)
    assert ts.covers_market() is False
    assert "bonds" in bt.markets_to_sweep()


def test_empty_pool_falls_back(stream):
    stream(shards_up=0, shards_total=0, quiet_min=0.5, streamed=False)
    assert ts.covers_market() is False
    assert bt.markets_to_sweep() == bt.MARKETS


def test_ndm_is_never_dropped(stream):
    """Адресные сделки брокер не отдаёт вообще — их лента обязана читаться
    в любом состоянии стрима."""
    for up, quiet in ((5, 0.5), (0, 99)):
        stream(shards_up=up, shards_total=5, quiet_min=quiet)
        assert "ndm" in bt.markets_to_sweep()


def test_broken_stream_module_does_not_break_sweep(monkeypatch):
    """Сторож, падающий вместе с системой, бесполезен: не смогли спросить —
    читаем обе ленты, как раньше."""
    def boom():
        raise RuntimeError("стрим сломался")
    monkeypatch.setattr(ts, "covers_market", boom)
    assert bt.markets_to_sweep() == bt.MARKETS


def test_yld_backfill_runs_rarely_but_runs(stream, monkeypatch):
    """Колонка «ДОХ-ТЬ» приходит только из ISS. При живом стриме читаем bonds
    редко — но читаем, иначе поле станет прочерком навсегда."""
    stream(shards_up=5, shards_total=5, quiet_min=0.5)
    monkeypatch.setattr(bt, "_bonds_at", float("-inf"), raising=False)
    assert bt.markets_to_sweep() == bt.MARKETS, "первый проход обязан добрать yld"
    assert bt.markets_to_sweep() == ("ndm",), "следующий такт — уже без bonds"


def test_yld_backfill_interval_is_sane():
    assert bt._BONDS_BACKFILL_SEC >= 600
