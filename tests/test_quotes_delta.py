"""Дельта котировок: ответ несёт только изменившиеся строки.

Такт опроса секундный (App.jsx QUOTES_POLL_MS), а за секунду из трёх тысяч
строк меняется десяток — полный ответ был бы почти целиком повтором."""
from api.routes.bonds import _quote_delta, _QUOTE_LOG, _QUOTE_LOG_TS


def _reset():
    _QUOTE_LOG.clear()
    _QUOTE_LOG_TS.clear()


def test_first_call_returns_all():
    _reset()
    items = [{"isin": "A", "last": 100.0}, {"isin": "B", "last": 99.0}]
    out, stamp = _quote_delta(items, "0:0", None)
    assert len(out) == 2 and stamp > 0


def test_unchanged_rows_drop_out():
    _reset()
    items = [{"isin": "A", "last": 100.0}, {"isin": "B", "last": 99.0}]
    _, stamp = _quote_delta(items, "0:0", None)
    out, _ = _quote_delta(items, "0:0", stamp)
    assert out == []


def test_changed_row_comes_back():
    _reset()
    items = [{"isin": "A", "last": 100.0}, {"isin": "B", "last": 99.0}]
    _, stamp = _quote_delta(items, "0:0", None)
    items[0]["last"] = 100.5
    out, _ = _quote_delta(items, "0:0", stamp)
    assert [r["isin"] for r in out] == ["A"]


def test_null_field_is_a_change():
    """Сторона исчезла из книги (px=None) — строка обязана доехать, иначе в
    таблице осталась бы цена заявки, которой на рынке уже нет."""
    _reset()
    items = [{"isin": "A", "bid": 100.0}]
    _, stamp = _quote_delta(items, "0:0", None)
    items[0]["bid"] = None
    out, _ = _quote_delta(items, "0:0", stamp)
    assert len(out) == 1 and out[0]["bid"] is None


def test_late_client_gets_everything_since_its_stamp():
    """Клиент пропустил такт: since у него старый, значит приедут ВСЕ
    изменения с той отметки, а не только последние."""
    _reset()
    items = [{"isin": "A", "last": 1.0}, {"isin": "B", "last": 2.0}]
    _, s0 = _quote_delta(items, "0:0", None)
    items[0]["last"] = 1.5
    _quote_delta(items, "0:0", s0)          # такт, который клиент пропустил
    items[1]["last"] = 2.5
    out, _ = _quote_delta(items, "0:0", s0)
    assert sorted(r["isin"] for r in out) == ["A", "B"]


def test_ticket_sizes_have_separate_logs():
    """Со сменой размера тикета состав строки другой — журнал общим быть не
    может, иначе клиент недосчитается полей набора."""
    _reset()
    items = [{"isin": "A", "last": 1.0}]
    _, stamp = _quote_delta(items, "0:0", None)
    out, _ = _quote_delta([{"isin": "A", "last": 1.0, "vol_bid_y": 250}],
                          "5000000:0", stamp)
    assert len(out) == 1


def test_old_ticket_logs_are_dropped():
    _reset()
    _quote_delta([{"isin": "A", "last": 1.0}], "5000000:0", None)
    _QUOTE_LOG_TS["5000000:0"] -= 10_000     # давно никто не спрашивал
    _quote_delta([{"isin": "A", "last": 1.0}], "0:0", None)
    assert "5000000:0" not in _QUOTE_LOG


def test_foreign_epoch_forces_full_answer():
    """since из прошлой жизни процесса (рестарт бэка, второй воркер) не должен
    молча проглатывать строки, которых клиент не видел."""
    _reset()
    items = [{"isin": "A", "last": 1.0}, {"isin": "B", "last": 2.0}]
    _, stamp = _quote_delta(items, "0:0", None)
    out, _ = _quote_delta(items, "0:0", stamp, "чужой-процесс")
    assert len(out) == 2


def test_own_epoch_keeps_delta():
    _reset()
    from api.routes.bonds import _QUOTE_EPOCH
    items = [{"isin": "A", "last": 1.0}]
    _, stamp = _quote_delta(items, "0:0", None)
    out, _ = _quote_delta(items, "0:0", stamp, _QUOTE_EPOCH)
    assert out == []
