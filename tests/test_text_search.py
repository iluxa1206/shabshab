"""Поиск понимает чужую раскладку и латинские двойники кириллицы.

Имя выпуска набирают на бегу, и запрос регулярно приезжает не в той раскладке:
«Ufpgy» вместо «Газпн». Символ в символ это ровно то, что человек хотел
набрать, — «ничего не найдено» здесь враньё.
"""
import importlib

import pytest

from services import text_search as ts


# --- разбор запроса ---

def test_layout_swap_works_both_ways():
    assert ts.swap_layout("Ufpgy") == "газпн"
    assert ts.swap_layout("Газпн") == "ufpgy"
    # смешанный запрос разбирается посимвольно, цифры остаются собой
    assert ts.swap_layout("RU000A109B33") == "кг000ф109и33"


def test_homoglyphs_fold_to_cyrillic():
    """Латиница ПО НАЧЕРТАНИЮ — в кириллицу: в тикерах биржи «P» и «Р»
    перемешаны, и глазом разницы нет."""
    assert ts.fold_homoglyphs("PЖД") == "ржд"
    # сворачиваются только НЕОТЛИЧИМЫЕ буквы; транслитерации нет — «g» так и
    # остаётся «g», иначе любое английское слово превращалось бы в русское и
    # давало ложные совпадения
    assert ts.fold_homoglyphs("gaz").startswith("g")
    assert "г" not in ts.fold_homoglyphs("gaz")


def test_variants_keep_typed_query_first():
    """Набранное — первым: человек чаще всего набрал верно, и догадка не должна
    его опережать."""
    v = ts.query_variants("Ufpgy")
    assert v[0] == "Ufpgy" and "газпн" in v


def test_variants_drop_punctuation_junk():
    """«РЖД» в другой раскладке — «h;l»: точки с запятой в именах выпусков не
    бывает, гонять такую догадку по базе незачем."""
    assert ts.query_variants("РЖД") == ["РЖД"]


def test_variants_collapse_duplicates():
    """Дубликаты не плодим, набранное всегда первое, пустой запрос — пусто.

    Догадка по кириллическому слову («ставка» → «cnfdrf») выглядит мусором, но
    остаётся: ровно так набирается ЛАТИНСКИЙ тикер по-русски («кг000ф109и33» →
    «ru000a109b33»), и по форме эти два случая неотличимы. Стоит она дёшево:
    очередь останавливается на первом варианте, который что-то нашёл."""
    v = ts.query_variants("ставка")
    assert v[0] == "ставка" and len(v) == len(set(v))
    assert ts.query_variants("") == []


def test_first_hit_takes_the_first_non_empty_variant():
    """Побеждает первый вариант, давший хоть что-то: выдачи НЕ объединяются —
    иначе обычный запрос разбавлялся бы случайными попаданиями догадки."""
    seen = []

    def run(term):
        seen.append(term)
        return ["нашлось"] if term == "газпн" else []

    assert ts.first_hit("Ufpgy", run) == ["нашлось"]
    assert seen[0] == "Ufpgy", "сперва пробуем набранное"
    assert seen[-1] == "газпн"


def test_first_hit_is_honest_about_nothing():
    assert ts.first_hit("зюзюка", lambda term: []) == []


def test_contains_ignores_case_and_homoglyphs():
    assert ts.contains("Газпн3P13R", "газпн")
    assert ts.contains("Газпн3P13R", "3р13")      # кириллическая «р» в тикере
    assert not ts.contains("Газпн3P13R", "ржд")


# --- поиск по реестру ---

@pytest.fixture()
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTRUMENTS_DB", str(tmp_path / "instruments.db"))
    from services import instruments_registry
    importlib.reload(instruments_registry)
    for isin, name, emitter in (
            ("RU000A109B33", "Газпн3P13R", "Газпром капитал"),
            ("RU000A10AU99", "РЖД 1Р-52R", "РЖД"),
    ):
        instruments_registry.upsert(
            {"isin": isin, "short_name": name, "emitter_name": emitter,
             "base": "KEYRATE"}, source="test")
    yield instruments_registry
    monkeypatch.delenv("INSTRUMENTS_DB")
    importlib.reload(instruments_registry)


def _names(rows):
    return [r["name"] for r in rows]


def test_registry_search_understands_wrong_layout(reg):
    assert _names(reg.search("Ufpgy")) == ["Газпн3P13R"]
    # обратное направление: латинское имя набрали по-русски
    assert _names(reg.search("кг000ф109и33")) == ["Газпн3P13R"]


def test_registry_search_ignores_case_on_cyrillic(reg):
    """LIKE в SQLite регистронезависим ТОЛЬКО для ASCII — «газпн» не находил
    «Газпн3P13R», и поиск работал, лишь когда регистр совпал с базой."""
    assert _names(reg.search("газпн")) == ["Газпн3P13R"]
    assert _names(reg.search("ГАЗПН")) == ["Газпн3P13R"]


def test_registry_search_finds_by_emitter_and_isin(reg):
    assert _names(reg.search("ржд")) == ["РЖД 1Р-52R"]
    assert _names(reg.search("ru000a10au99")) == ["РЖД 1Р-52R"]


def test_registry_search_needs_two_chars(reg):
    assert reg.search("г") == []
