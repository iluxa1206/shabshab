"""Класс бумаги в событиях сигналов: клик по событию открывает карточку своего
вида (docs/unified_monitor_tz.md, этап C). Класс берётся из универса фиксов, а
не из «нет базы в реестре»: у флоатера с неподтверждёнными параметрами базы
тоже нет, и такая бумага открылась бы карточкой фикса."""
import services.market_data as md
from services import signals
from services.screener_core import bond_kind


def _universe(monkeypatch, isins):
    monkeypatch.setitem(md.market_cache, "fixed_universe", [{"isin": i} for i in isins])


def test_kind_from_fixed_universe(monkeypatch):
    _universe(monkeypatch, ["SU26238RMFS4"])
    assert bond_kind("SU26238RMFS4") == "fixed"
    assert bond_kind("RU000A0FLT01") == "floater"


def test_empty_or_cold_universe_means_floater(monkeypatch):
    monkeypatch.delitem(md.market_cache, "fixed_universe", raising=False)
    assert bond_kind("SU26238RMFS4") == "floater"


def test_explicit_set_wins_for_loops():
    assert bond_kind("X", {"X"}) == "fixed"
    assert bond_kind("Y", {"X"}) == "floater"


def test_events_carry_kind(monkeypatch):
    _universe(monkeypatch, ["SU26238RMFS4"])
    import services.instruments_registry as reg
    monkeypatch.setattr(reg, "labels_map", lambda isins: {})
    rows = [{"isin": "SU26238RMFS4"}, {"isin": "RU000A0FLT01"}]
    out = {r["isin"]: r["kind"] for r in signals._with_maturity(rows)}
    assert out == {"SU26238RMFS4": "fixed", "RU000A0FLT01": "floater"}
