"""Тесты парсера формулы купона corpbonds (чистый, без сети)."""
from services.enrich_corpbonds import _parse_formula, _to_iso, _num


def test_formula_keyrate_average():
    r = _parse_formula("∑КС + 2.3%")
    assert r["base"] == "KEYRATE" and r["margin_bps"] == 230
    assert r["coupon_mode"] == "average" and r.get("exotic") is None


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
