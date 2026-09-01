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


# --- токены и опечатки ---

def test_tokenize_splits_on_letter_digit_border():
    """«ржд3» это «ржд» + «3»: в именах выпусков буквы и цифры слиты, а ищут
    их по отдельности."""
    assert ts.tokenize("ржд-2р3") == ["ржд", "2", "р", "3"]
    assert ts.tokenize("  ") == []


def test_loose_includes_forgives_one_letter():
    """Допуск — одна лишняя буква в токене от четырёх символов: так ловится и
    промах по соседней клавише, и лишний символ."""
    assert ts.loose_includes("газпн3р13r", "газпм")
    assert ts.loose_includes("газпн3р13r", "гаазпн")
    # короткий токен проверяется точно: на «ржд» допуск оставил бы «рж»
    assert not ts.loose_includes("ржд 1р-52r", "рxд")


def test_matcher_requires_every_token():
    """Токены складываются по «и» — иначе «ржд 3» вернул бы весь рынок."""
    ok = ts.make_matcher("ржд 52")
    assert ok("РЖД 1Р-52R", "RU000A10AU99")
    assert not ok("РЖД 1Р-40R", "RU000A10AU98")


def test_matcher_keeps_short_digits_out_of_isin():
    """Одиночная цифра в ISIN не считается: «3» совпадает с цифрами почти
    любого ISIN, и фильтр перестал бы фильтровать."""
    ok = ts.make_matcher("3")
    assert not ok("РЖД 1Б-01", "RU000A103333")
    assert ok("РЖД 3Р-01", "RU000A103333")


def test_ranked_puts_exact_before_typo():
    """Набравшему имя верно оно и достаётся первым: без ранжирования допуск
    опечатки вытеснял точное совпадение соседями по алфавиту."""
    rows = [("ГазпКап2P1", "Газпром капитал", "RU000A0000A1"),
            ("Газпн3P13R", "Газпром нефть", "RU000A109B33")]
    got = ts.ranked("газпн", rows, lambda r: r, limit=2)
    assert [r[0] for r in got] == ["Газпн3P13R", "ГазпКап2P1"]


def test_ranked_prefers_issue_name_over_emitter():
    """Имя выпуска важнее имени эмитента: набирают обычно выпуск, а эмитент —
    способ достать всю группу."""
    rows = [("ГПН005Р-07", "Газпром нефть", "RU000A0000A2"),
            ("ГазпКап2P1", "Газпром капитал", "RU000A0000A1")]
    got = ts.ranked("газп", rows, lambda r: r, limit=2)
    assert [r[0] for r in got] == ["ГазпКап2P1", "ГПН005Р-07"]


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
            {"isin": isin, "short_name": name, "base": "KEYRATE"}, source="test")
        # эмитент живёт отдельным полем: upsert его не пишет (белый список
        # расчётных параметров), а поиск по группе выпусков без него не проверить
        instruments_registry.set_emitter(isin, None, emitter)
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


def test_registry_search_forgives_a_typo(reg):
    """Опечатка в одну букву не должна ронять пикер в «ничего не найдено» —
    правило то же, что в таблице монитора."""
    assert _names(reg.search("газпм")) == ["Газпн3P13R"]


def test_registry_search_keeps_exact_hit_on_top(reg):
    """Точное совпадение выше приблизительного: «газпн» — это Газпн3P13R, а не
    сосед по эмитенту, подошедший через допуск."""
    assert _names(reg.search("газпн"))[0] == "Газпн3P13R"


def test_registry_search_finds_group_by_emitter(reg):
    """Эмитент — способ достать всю группу выпусков."""
    assert _names(reg.search("газпром")) == ["Газпн3P13R"]
