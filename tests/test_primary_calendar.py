"""Парсер анонсов первички: позиционный массив bondresearch → строки витрины."""
from services.primary_calendar import _merge, _num, parse_rows

# реальная форма строки источника (14 полей), фикс и флоатер
ROW_FIX = ["2026-09-14", "2026-09-09", "Заслон", "BBB+/A-/-/-", "RUB", "3",
           "2'000", "ежемесячный", "ставка купона не выше 17,5%", None,
           "002Р-01", None, "18.97", "2.3"]
ROW_FRN = ["2026-09-11", "2026-09-08", "АЛРОСА", "AAA/AAA/-/-", "RUB", "1,5",
           "≥ 20'000", "ежемесячный", "ставка купона КС + не выше 160 бп", "1",
           "002Р-03", "https://example.test/1", None, None]


def test_parse_fixed_row():
    r = parse_rows([ROW_FIX])[0]
    assert r["issuer"] == "Заслон"
    assert r["ratings"] == ["BBB+", "A-"]          # прочерки агентств отброшены
    assert r["volume_mln"] == 2000 and r["term_years"] == 3
    assert r["is_floater"] is False
    assert r["ytm_pct"] == 18.97 and r["duration_years"] == 2.3


def test_parse_floater_row():
    r = parse_rows([ROW_FRN])[0]
    assert r["is_floater"] is True                 # их поле frn_technical == "1"
    assert r["volume_mln"] == 20000                # «≥ 20'000» → нижняя граница
    assert r["volume_raw"].startswith("≥")         # сырое поле сохранено для UI
    assert r["ytm_pct"] is None                    # у флоатеров источник YTM не даёт
    assert r["url"] == "https://example.test/1"


def test_parse_skips_garbage():
    assert parse_rows([[], None, ["короткая", "строка"], [""] * 14]) == []


def test_num_variants():
    assert _num("1,5") == 1.5
    assert _num("10'000") == 10000
    assert _num("26,83 - 28,71") == 26.83         # диапазон — по нижней границе
    assert _num("будет определен позднее") is None


def test_cyrillic_rating_normalized():
    # сайт пишет рейтинги смешанной раскладкой: «ВВ-» кириллицей
    row = list(ROW_FIX)
    row[3] = "-/-/-/ВВ-"
    assert parse_rows([row])[0]["ratings"] == ["BB-"]


def test_merge_seeds_without_first_seen():
    """Первый посев не красит всю таблицу «новым»."""
    fresh = parse_rows([ROW_FIX, ROW_FRN])
    assert all(r["first_seen"] is None for r in _merge(fresh, [], "2026-09-01"))


def test_merge_keeps_first_seen_when_dates_shift():
    prev = _merge(parse_rows([ROW_FIX]), [{"issuer": "x", "comment": "y"}], "2026-08-20")
    shifted = list(ROW_FIX)
    shifted[0], shifted[1] = "2026-09-20", "2026-09-15"   # анонс переехал
    merged = _merge(parse_rows([shifted, ROW_FRN]), prev, "2026-09-01")
    assert merged[0]["first_seen"] == "2026-08-20"        # та же серия — не «новое»
    assert merged[1]["first_seen"] == "2026-09-01"        # АЛРОСА появилась сегодня
