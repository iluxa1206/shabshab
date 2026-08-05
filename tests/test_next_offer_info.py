"""next_offer_info: маркер оферты p/c у даты погашения в таблице.

Ближайшая БУДУЩАЯ оферта любого вида (put/call); состоявшиеся/исполненные
не считаются будущим событием, даже если дата распарсилась в будущее.
"""
from datetime import date

from core.valuation import next_offer_info

SETTLE = date(2026, 8, 5)


def test_nearest_future_any_kind():
    offers = [
        {"date": "2027-01-15", "type": "Оферта"},
        {"date": "2026-12-01", "type": "Оферта (колл) - опцион эмитента"},
    ]
    d, typ, kind = next_offer_info(offers, SETTLE)
    assert d == date(2026, 12, 1)
    assert kind == "call"


def test_put_default_kind():
    d, typ, kind = next_offer_info([{"date": "2027-06-01", "type": "Оферта"}], SETTLE)
    assert d == date(2027, 6, 1)
    assert kind == "put"


def test_executed_offers_skipped():
    offers = [{"date": "2027-06-01", "type": "Оферта (состоялось)"},
              {"date": "2027-07-01", "type": "Оферта (исполнено)"}]
    assert next_offer_info(offers, SETTLE) is None


def test_past_and_empty():
    assert next_offer_info([{"date": "2025-01-01", "type": "Оферта"}], SETTLE) is None
    assert next_offer_info([], SETTLE) is None
    assert next_offer_info(None, SETTLE) is None


def test_bad_date_ignored():
    assert next_offer_info([{"date": "мусор", "type": "Оферта"}, {"date": None}],
                           SETTLE) is None
