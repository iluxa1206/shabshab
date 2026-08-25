"""Тесты парсера формулы купона corpbonds (чистый, без сети)."""
from services.enrich_corpbonds import _parse_formula, _to_iso, _num


def test_formula_keyrate_average():
    r = _parse_formula("∑КС + 2.3%")
    assert r["base"] == "KEYRATE" and r["margin_bps"] == 230
    assert r["coupon_mode"] == "average" and r.get("exotic") is None


def test_formula_greek_sigma_attached_base():
    """Регрессия: corpbonds шлёт Σ (U+03A3, БУКВА) приклеенной к базе — «ΣКС».
    \\bКС\\b не матчил (нет границы слова после буквы) → база=None → бумага ошибочно
    уходила в EXOTIC. Терялись обычные усреднённые КС-флоатеры (Атомэнергопром/Газпром)."""
    r = _parse_formula("ΣКС + 1.5%")               # Σ = U+03A3, не ∑ (U+2211)
    assert r["base"] == "KEYRATE" and r["margin_bps"] == 150
    assert r["coupon_mode"] == "average" and r.get("exotic") is None


def test_formula_sigma_space_base():
    r = _parse_formula("Σ КС + 1%")
    assert r["base"] == "KEYRATE" and r["margin_bps"] == 100 and r["coupon_mode"] == "average"


def test_formula_keyrate_point():
    r = _parse_formula("КС + 1.5%")
    assert r["base"] == "KEYRATE" and r["margin_bps"] == 150 and r["coupon_mode"] == "point"


def test_formula_ruonia():
    r = _parse_formula("∑RUONIA + 1.85%")
    assert r["base"] == "RUONIA" and r["margin_bps"] == 185


def test_formula_inverse_exotic():
    r = _parse_formula("MAX (25.9% - КС; 12.9%)")
    assert r["exotic"] == "inverse"          # инверсный — не моделируется


def test_formula_floored_capped():
    r = _parse_formula("MAX (KC + 3.75%; 9.5%)")
    assert r["base"] == "KEYRATE" and r["margin_bps"] == 375 and r["exotic"] == "capped"


def test_formula_cpi_not_ksruonia():
    r = _parse_formula("ИПЦ + 1%")
    assert r["base"] is None                 # CPI — не КС/RUONIA


def test_to_iso_and_num():
    assert _to_iso("08.04.2027") == "2027-04-08"
    assert _num("900 руб") == 900.0
    assert _num("8 000 000 шт") == 8000000.0


def test_inverse_floater_detected_by_short_index_name():
    """Инверсный купон ловится, как бы ни звали индекс в проспекте.

    АЛЬФАБ1Р11: «max (25.90% - R; 12.90%)» — ставка ПАДАЕТ при росте КС.
    Шаблон искал только «− КС» и пропускал одиночную R, поэтому бумага
    считалась как КС+25,9 % → 40 % годовых вместо реальных 12,9 %."""
    from services.enrich_corpbonds import _parse_formula
    p = _parse_formula("2-12 купоны: Сi = max (25.90% - R; 12.90%), где R — "
                       "ключевая ставка Банка России")
    assert p.get("exotic") == "inverse"


def test_normal_floater_not_marked_inverse():
    """Обычная формула не должна попадать в инверсные из-за буквы R рядом."""
    from services.enrich_corpbonds import _parse_formula
    p = _parse_formula("Купон = КС + 2%, где КС — ключевая ставка; "
                       "RUONIA - справочная ставка")
    assert p.get("exotic") != "inverse"
