"""Обе ленты ISS читаются всегда — проверенная на проде необходимость.

11.09.2026 попробовали при живом стриме Alor читать только адресную ленту
(ndm), добирая безадресную раз в 15 минут ради поля yld. Через час прод
прислал «ЛЕНТА ОТСТАЁТ: живьём поймано 27 из 247 крупных сделок».

Причина: в block_trade из стрима попадают только сделки ВЫШЕ alert_floor
(порога активных получателей). Всё, что между 1 млн и этим порогом, живёт
лентой ISS — и с тактом раз в 15 минут доезжало за полчаса вместо пятнадцати
минут. Экономия составляла 408 запросов в сутки из 2 728; полнота ленты
этого не стоит.

Тест закрепляет откат: адресная И безадресная ленты читаются в любом
состоянии стрима.
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
    return _set


def test_both_feeds_are_read_whatever_the_stream_state(stream):
    """Ни одно состояние стрима не должно отключать безадресную ленту."""
    for up, total, quiet in ((13, 13, 0.5), (2, 13, 0.5), (13, 13, 30), (0, 0, 99)):
        stream(shards_up=up, shards_total=total, quiet_min=quiet,
               streamed=bool(total))
        assert bt.markets_to_sweep() == bt.MARKETS, (
            f"при шардах {up}/{total} и тишине {quiet} мин лента урезана")


def test_ndm_is_present():
    """Адресные сделки брокер не отдаёт вообще — только ISS."""
    assert "ndm" in bt.markets_to_sweep()


def test_bonds_is_present():
    """Безадресные нужны в block_trade быстрее, чем раз в 15 минут:
    сделки ниже alert_floor стрим туда не пишет."""
    assert "bonds" in bt.markets_to_sweep()


# --- covers_market остаётся: им пользуется диагностика состояния стрима ---

def test_covers_market_needs_both_live_shards_and_fresh_ticks(stream):
    """Сокет может висеть открытым и молчать — одного числа шардов мало."""
    stream(shards_up=13, shards_total=13, quiet_min=0.5)
    assert ts.covers_market() is True
    stream(shards_up=13, shards_total=13, quiet_min=30)
    assert ts.covers_market() is False, "молчащие сокеты — не покрытие"
    stream(shards_up=2, shards_total=13, quiet_min=0.5)
    assert ts.covers_market() is False
    stream(shards_up=0, shards_total=0, quiet_min=0.5, streamed=False)
    assert ts.covers_market() is False
