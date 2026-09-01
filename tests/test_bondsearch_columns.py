"""Карта колонок bondsearch: чужие заголовки, оверрайд-файл, новые поля."""
import openpyxl
import pytest

from services import ref_data


def _xlsx(tmp_path, headers, rows, name="bondsearch_01_01_2030.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    p = tmp_path / name
    wb.save(p)
    return str(p)


def test_headers_match_ignoring_case_punctuation(tmp_path):
    """Заголовок ищется по канону: регистр/пунктуация/лишние пробелы не важны."""
    path = _xlsx(tmp_path,
                 ["isin", "БУМАГА", "базовая  ставка", "Мин торг лот / номинал"],
                 [["RU000TEST0001", "Тест", "Ключевая ставка ЦБ РФ", 1000]])
    cb = ref_data.load_cbonds(path)
    row = cb["RU000TEST0001"]
    assert row["base"] == "KEYRATE"
    assert row["name"] == "Тест"
    assert row["face_value"] == 1000


def test_column_override_file(tmp_path, monkeypatch):
    """Свой формат подключается файлом bondsearch_columns.json — без правки кода."""
    (tmp_path / "cols.json").write_text(
        '{"isin": "Код", "margin": ["Спред к КС, %"]}', encoding="utf-8")
    monkeypatch.setattr(ref_data, "_COL_OVERRIDE_FILE", str(tmp_path / "cols.json"))
    path = _xlsx(tmp_path, ["Код", "Спред к КС, %", "Базовая ставка"],
                 [["RU000TEST0002", 2.5, "RUONIA"]])
    cb = ref_data.load_cbonds(path)
    assert cb["RU000TEST0002"]["margin_bps"] == 250
    assert cb["RU000TEST0002"]["base"] == "RUONIA"


def test_extra_fields_rating_and_flags(tmp_path):
    """Рейтинг агентств сводится в одно поле, «Да/Нет» → bool."""
    path = _xlsx(tmp_path,
                 ["ISIN", "Рейтинг эмитента АКРА", "Рейтинг эмитента Эксперт РА",
                  "Амортизация", "Статус"],
                 [["RU000TEST0003", "", "ruA+", "Да", "В обращении"]])
    row = ref_data.load_cbonds(path)["RU000TEST0003"]
    assert row["rating"] == "A+"
    assert row["amort"] is True
    assert row["status"] == "В обращении"


def test_unknown_override_key_does_not_break(tmp_path, monkeypatch):
    (tmp_path / "cols.json").write_text('{"нет_такого": "X"}', encoding="utf-8")
    monkeypatch.setattr(ref_data, "_COL_OVERRIDE_FILE", str(tmp_path / "cols.json"))
    path = _xlsx(tmp_path, ["ISIN", "Базовая ставка"], [["RU000TEST0004", "RUONIA"]])
    assert ref_data.load_cbonds(path)["RU000TEST0004"]["base"] == "RUONIA"


def test_ambiguous_prefix_is_not_guessed(tmp_path):
    """«Купон привязан к инфляции» без колонки «Купон» — не формула купона.
    Неоднозначный префикс = не сопоставили, а не «взяли первое похожее»."""
    path = _xlsx(tmp_path, ["ISIN", "Купон привязан к инфляции", "Купон плавающий"],
                 [["RU000TEST0005", "Нет", "Да"]])
    assert ref_data.load_cbonds(path)["RU000TEST0005"]["coupon_text"] is None


def test_unique_prefix_still_matches(tmp_path):
    """Единственное совпадение по префиксу берём: выгрузка любит хвосты «, %»."""
    path = _xlsx(tmp_path, ["ISIN", "Базовая ставка купона"],
                 [["RU000TEST0006", "RUONIA"]])
    assert ref_data.load_cbonds(path)["RU000TEST0006"]["base"] == "RUONIA"


def test_isin_144a_does_not_shadow_isin(tmp_path):
    path = _xlsx(tmp_path, ["ISIN 144A", "ISIN", "Базовая ставка"],
                 [["", "RU000TEST0007", "RUONIA"]])
    assert "RU000TEST0007" in ref_data.load_cbonds(path)
