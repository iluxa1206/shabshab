"""Разбор ориентира купона первички: текст организатора → параметры прайсинга.

Сам расчёт здесь не проверяется — он общий с калькулятором
(services/custom_bond, tests/test_calc_custom) и требует живых кривых.
"""
from datetime import date

import pytest

from services.primary_pricing import maturity_for, parse_coupon_guide, parse_freq


@pytest.mark.parametrize("text,expect", [
    # флоатеры: маржа в бп, «не выше» = потолок
    ("ставка купона КС + не выше 300 бп",
     {"kind": "floater", "base": "KEYRATE", "margin_bps": 300.0, "bound": "max"}),
    ("ставка купона КС + 150 бп",
     {"kind": "floater", "base": "KEYRATE", "margin_bps": 150.0, "bound": "exact"}),
    ("ставка купона RUONIA + не выше 240 б.п.",
     {"kind": "floater", "base": "RUONIA", "margin_bps": 240.0, "bound": "max"}),
    # фиксы
    ("ставка купона не выше 17,5%", {"kind": "fixed", "rate_pct": 17.5, "bound": "max"}),
    ("ставка купона 24%", {"kind": "fixed", "rate_pct": 24.0, "bound": "exact"}),
])
def test_parse_guide(text, expect):
    assert parse_coupon_guide(text) == expect


def test_parse_range():
    # вилка: считаются обе границы, rate_pct — верхняя (широкий спред)
    got = parse_coupon_guide("ставка купона  24,00 - 25,50%")
    assert got == {"kind": "fixed", "rate_pct": 25.5, "rate_pct_low": 24.0, "bound": "range"}


@pytest.mark.parametrize("text", ["будет определен позднее", "", None, "ориентир объявят"])
def test_parse_no_rate(text):
    """Ставки в тексте нет — строка просто остаётся без спреда, не падает."""
    assert parse_coupon_guide(text) is None


def test_floater_base_wins_over_percent():
    # «КС + 3%» — это МАРЖА 300 бп, а не фиксированная ставка 3%
    assert parse_coupon_guide("ставка купона КС + 3%") == {
        "kind": "floater", "base": "KEYRATE", "margin_bps": 300.0, "bound": "exact"}


def test_freq_words():
    assert parse_freq("ежемесячный") == 12
    assert parse_freq("Ежеквартальный") == 4
    assert parse_freq("раз в полгода") == 2
    assert parse_freq("невнятица") is None


def test_maturity_from_issue_plus_term():
    row = {"issue_date": "2026-09-14", "book_date": "2026-09-09", "term_years": 3.0}
    assert maturity_for(row) == date(2029, 9, 14)


def test_maturity_fractional_years():
    # 1,5 года = 18 месяцев; срок в источнике — уже «до оферты», если она есть
    row = {"issue_date": "2026-09-11", "term_years": 1.5}
    assert maturity_for(row) == date(2028, 3, 11)


def test_maturity_falls_back_to_book_date():
    row = {"issue_date": None, "book_date": "2026-09-08", "term_years": 2.0}
    assert maturity_for(row) == date(2028, 9, 8)


@pytest.mark.parametrize("row", [
    {"issue_date": None, "book_date": None, "term_years": 3.0},   # дат нет
    {"issue_date": "2026-09-14", "term_years": None},             # срока нет
    {"issue_date": "не дата", "term_years": 3.0},                 # мусор в дате
])
def test_maturity_absent(row):
    assert maturity_for(row) is None


def test_maturity_month_end_clamped():
    # 31 августа + 6 месяцев: конца февраля 31-го не бывает
    assert maturity_for({"issue_date": "2026-08-31", "term_years": 0.5}) == date(2027, 2, 28)
