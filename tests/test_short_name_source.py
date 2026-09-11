"""Имя выпуска: биржевое короткое, а не полное из Cbonds.

10.09.2026 ISS лежал весь день, биржевой синк не отработал — и обогащение из
Cbonds записало в short_name полные названия («Газпром Капитал, БО-002P-04»
вместо «ГазпКап2P4»). В телеграм-уведомления, ленту и таблицу они поехали
такими же: 557 бумаг. Имя выпуска не меняется со временем, поэтому у известной
бумаги его переписывать незачем — а у новой другого источника может и не быть.
"""
import sqlite3

import pytest

from services import instruments_registry as reg


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "instruments.db"
    monkeypatch.setattr(reg, "DB_PATH", path, raising=False)
    monkeypatch.setattr(reg, "_initialized", False, raising=False)
    reg._ensure()
    return path


def test_cbonds_does_not_overwrite_known_name(db):
    """Ядро регресса: у бумаги с известным именем cbonds его не трогает."""
    reg.upsert({"isin": "RU000A107UW1", "short_name": "Газпн3P10R",
                "base": "KEYRATE"}, source="moex")
    reg.sync_from_sources(
        nrd_items=[{"isin": "RU000A107UW1"}],
        cbonds={"RU000A107UW1": {"name": "Газпром нефть, БО-003P-10R",
                                 "base": "KEYRATE", "margin_bps": 130}})
    assert reg.get("RU000A107UW1")["short_name"] == "Газпн3P10R"


def test_new_bond_takes_cbonds_name(db):
    """У новой бумаги биржевого имени ещё нет — берём что дают, иначе она
    осталась бы без названия вовсе."""
    reg.sync_from_sources(
        nrd_items=[{"isin": "RU000NEWBOND"}],
        cbonds={"RU000NEWBOND": {"name": "Новый Эмитент, БО-01",
                                 "base": "KEYRATE", "margin_bps": 200}})
    assert reg.get("RU000NEWBOND")["short_name"] == "Новый Эмитент, БО-01"


def test_empty_existing_name_is_filled(db):
    """Пустое имя — не «известное»: его заполнить можно и нужно."""
    reg.upsert({"isin": "RU000EMPTY01", "short_name": None, "base": "KEYRATE"},
               source="moex")
    reg.sync_from_sources(
        nrd_items=[{"isin": "RU000EMPTY01"}],
        cbonds={"RU000EMPTY01": {"name": "Эмитент, БО-02", "base": "KEYRATE"}})
    assert reg.get("RU000EMPTY01")["short_name"] == "Эмитент, БО-02"


def test_other_cbonds_fields_still_update(db):
    """Защита касается ТОЛЬКО имени: маржа, база и прочее обязаны обновляться."""
    reg.upsert({"isin": "RU000A107UW1", "short_name": "Газпн3P10R",
                "base": "KEYRATE", "margin_bps": 100}, source="moex")
    reg.sync_from_sources(
        nrd_items=[{"isin": "RU000A107UW1"}],
        cbonds={"RU000A107UW1": {"name": "Газпром нефть, БО-003P-10R",
                                 "base": "KEYRATE", "margin_bps": 130}})
    row = reg.get("RU000A107UW1")
    assert row["short_name"] == "Газпн3P10R"
    assert row["margin_bps"] == 130, "маржа из cbonds должна была обновиться"
