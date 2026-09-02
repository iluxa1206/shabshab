"""Батч br-спек не трогает кэши, когда данные не изменились.

Дневной синк bondresearch перезаписывает те же ~450 спек. Пока rowcount считал
их изменёнными, invalidate_params_cache сносила кэш уровней, потоки и КОНТЕКСТЫ
расчёта движка; на старте синк попадает в середину прогрева и съедал его засев
целиком (репетиция переката 02.09: посчитано 611, движок получил 0)."""
import services.instruments_registry as reg


def _spec(lag=2, mode="average", win=None):
    return {"fixing_lag": lag, "coupon_mode": mode, "avg_window_days": win}


def test_rewriting_same_specs_changes_nothing(monkeypatch):
    isin = "RU000TESTBR01"
    reg.upsert({"isin": isin, "base": "KEYRATE"}, source="test", mark_new=False)
    # база живёт между прогонами — приводим строку к известному состоянию, а не
    # полагаемся на то, что спеки записываются впервые
    reg.set_br_specs_bulk({isin: _spec(lag=1)})
    assert reg.set_br_specs_bulk({isin: _spec()}) == 1      # значения другие — правка
    hits = []
    monkeypatch.setattr(reg, "invalidate_params_cache", lambda *a: hits.append(a))
    assert reg.set_br_specs_bulk({isin: _spec()}) == 0      # те же значения
    assert hits == []                                        # кэши не тронуты


def test_real_change_still_invalidates(monkeypatch):
    isin = "RU000TESTBR02"
    reg.upsert({"isin": isin, "base": "KEYRATE"}, source="test", mark_new=False)
    reg.set_br_specs_bulk({isin: _spec(lag=2)})   # известное состояние
    hits = []
    monkeypatch.setattr(reg, "invalidate_params_cache", lambda *a: hits.append(a))
    assert reg.set_br_specs_bulk({isin: _spec(lag=5)}) == 1
    assert len(hits) == 1


def test_null_field_is_not_a_change(monkeypatch):
    """Спека без avg_window_days: NULL против NULL — не правка (IS NOT)."""
    isin = "RU000TESTBR03"
    reg.upsert({"isin": isin, "base": "KEYRATE"}, source="test", mark_new=False)
    reg.set_br_specs_bulk({isin: _spec(win=None)})
    hits = []
    monkeypatch.setattr(reg, "invalidate_params_cache", lambda *a: hits.append(a))
    assert reg.set_br_specs_bulk({isin: _spec(win=None)}) == 0
    assert hits == []
