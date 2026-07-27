"""Тесты парсинга bondsearch-выгрузки (ref_data.load_cbonds) — купонные параметры."""
import datetime

from services import ref_data


def test_cell_iso_datetime_and_string():
    assert ref_data._cell_iso(datetime.datetime(2027, 12, 16)) == "2027-12-16"
    assert ref_data._cell_iso(datetime.date(2025, 1, 3)) == "2025-01-03"
    assert ref_data._cell_iso("16.12.2027") == "2027-12-16"
    assert ref_data._cell_iso("") is None
    assert ref_data._cell_iso(None) is None


def test_base_map_ruonia_index():
    # «RUONIA Индекс» (не голый RUONIA) раньше давал base=None → бумага теряла базу
    assert ref_data._BASE_MAP.get("ruonia индекс") == "RUONIA"
    assert ref_data._BASE_MAP.get("ключевая ставка цб рф") == "KEYRATE"
