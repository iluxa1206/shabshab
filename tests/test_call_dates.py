"""Даты call-опциона: парсер календаря corpbonds → реестр → оферты MOEX → горизонт.

Дыра, которую эти тесты закрывают: has_call давал только маркер «c», а горизонт
прайсинга оставался погашением — сравнивать цену было не с чем (СибурХ1Р04: колл
ежемесячный с 14.12.2026, спред считался к 2032 году).
"""
from datetime import date

from core.valuation import first_call_date, next_offer_info
from services.enrich_corpbonds import _parse_call_dates, parse_corpbonds_html

# Календарь выплат corpbonds: строки «call-опцион» + методологический абзац с тем
# же словом (из него даты браться НЕ должны) + обычный купон.
_PAGE = """
<html><body>
<table><tbody>
<tr><td>YTM от CorpBonds ?</td><td>Call-опционы обрабатываются так: если цена выше
102% от номинала, доходность считается к дате опциона 01.01.2001</td></tr>
</tbody></table>
<table><tbody>
<tr><td></td><td>16.08.2026</td><td>купон</td><td>13,66</td></tr>
<tr><td></td><td>14.12.2026</td><td>call-опцион</td><td></td></tr>
<tr><td></td><td>13.01.2027</td><td>call-опцион</td><td></td></tr>
</tbody></table>
</body></html>
"""


def _soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def test_parse_call_dates_from_calendar():
    assert _parse_call_dates(_soup(_PAGE)) == ["2026-12-14", "2027-01-13"]


def test_methodology_text_gives_no_dates():
    """Длинный абзац со словом call — не строка календаря: 01.01.2001 не берём."""
    only_text = _PAGE.split("</table>")[0] + "</table></body></html>"
    assert _parse_call_dates(_soup(only_text)) == []


def test_dates_imply_has_call():
    """Даты нашлись → колл есть, даже если строки «Наличие сall-опциона» на
    странице не было (факт сильнее отсутствия поля)."""
    out = parse_corpbonds_html(_PAGE)
    assert out["call_dates"] == ["2026-12-14", "2027-01-13"]
    assert out["has_call"] is True


def test_injected_offer_becomes_call_horizon():
    """Синтетическая запись из реестра проходит штатной дорогой оценки."""
    from services.market_data import _with_call_offers
    import services.market_data as md

    md._CALL_DATES = {"XX": ["2026-12-14", "2027-01-13"]}
    md._CALL_DATES_AT = float("inf")   # не ходить в реестр
    try:
        sched = {"coupons": [], "amorts": [], "offers": []}
        out = _with_call_offers("XX", sched)
        assert sched["offers"] == [], "исходное расписание (кэш MOEX) мутировать нельзя"
        assert [o["date"] for o in out["offers"]] == ["2026-12-14"], "только ближайшая"

        settle = date(2026, 8, 14)
        assert first_call_date(out["offers"], settle) == date(2026, 12, 14)
        assert next_offer_info(out["offers"], settle, date(2032, 3, 17))[2] == "call"
    finally:
        md._CALL_DATES, md._CALL_DATES_AT = {}, 0.0


def test_call_offers_asof_uses_backdated_reference():
    """Бэкдейт: ближайшая будущая считается от РАСЧЁТНОЙ даты, не от сегодня.
    У бермудского колла даты каждый месяц — иначе горизонт был бы из будущего."""
    import services.market_data as md

    md._CALL_DATES = {"XX": ["2026-01-20", "2026-02-20", "2026-12-14"]}
    md._CALL_DATES_AT = float("inf")
    try:
        today = md._with_call_offers("XX", {"offers": []})["offers"]
        assert [o["date"] for o in today] == ["2026-12-14"]
        # своя вчерашняя запись выбрасывается, ставится корректная на as-of
        asof = md.call_offers_asof("XX", today, date(2026, 1, 15))
        assert [o["date"] for o in asof] == ["2026-01-20"]
        assert [o["date"] for o in md.call_offers_asof("XX", today, date(2026, 2, 1))] \
            == ["2026-02-20"]
        # записи MOEX не трогаем
        mixed = [{"date": "2027-05-05", "type": "Оферта", "price": 100.0}] + today
        assert md.call_offers_asof("XX", mixed, date(2026, 1, 15))[0]["type"] == "Оферта"
    finally:
        md._CALL_DATES, md._CALL_DATES_AT = {}, 0.0


def test_moex_offer_wins_over_injected():
    """Своя запись MOEX на ту же дату авторитетнее — дубля не создаём."""
    import services.market_data as md

    md._CALL_DATES = {"XX": ["2026-12-14"]}
    md._CALL_DATES_AT = float("inf")
    try:
        sched = {"offers": [{"date": "2026-12-14", "type": "Оферта", "price": 100.0}]}
        assert md._with_call_offers("XX", sched) is sched
    finally:
        md._CALL_DATES, md._CALL_DATES_AT = {}, 0.0
