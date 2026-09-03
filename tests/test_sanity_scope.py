"""Санити-флаг: чужая цена не гасит строку, а погашенная строка молчит везде.

Аудит конвейера 03.09, находка 10 (high). Блок sanity снимал спред-метрики, но
до карт по ценам внутри горизонтов не доходил: horizons["maturity"]
["y_idx_by_price"] лежит там по ссылке, и переприсваивание локальной переменной
его не трогало. В колонке Y-IDX стоял прочерк, а витрина, лестница стакана и TG
брали из той же карты число, которое сторож и должен был спрятать.

Обратная сторона того же места: флаг взводила ЛЮБАЯ альт-цена, хотя каждая из
них проходит отсечку поштучно — одна мусорная сторона гасила Y-IDX по цене
сделки.
"""
from datetime import date

from services.valuation import calculate_valuation_metrics
from tests.conftest import make_bond, quarterly_periods


CD = date(2026, 1, 12)
MATURITY = date(2031, 1, 12)
ISSUE = date(2024, 1, 12)


def _periods():
    return [(date.fromisoformat(s), date.fromisoformat(e), v)
            for (s, e, v) in quarterly_periods(ISSUE, MATURITY)]


def _calc(price, alt_prices, curve):
    return calculate_valuation_metrics(
        make_bond(base="RUONIA", maturity=MATURITY, issue=ISSUE),
        price, curve, CD, accrued_override=0.0, periods=_periods(),
        amorts=None, offers=None, ruonia_curve=curve,
        with_margins=False, alt_prices=alt_prices)


def test_garbage_alt_price_does_not_blank_the_row(ruonia_curve):
    """Битая сторона (1,5 % номинала у дефолтного выпуска) гасит СЕБЯ, а не
    Y-IDX по цене сделки."""
    clean = _calc(99.0, None, ruonia_curve)
    with_junk = _calc(99.0, [1.5], ruonia_curve)

    assert clean["yield_over_index_bps"] is not None, "фикстура без числа"
    assert with_junk["yield_over_index_bps"] == clean["yield_over_index_bps"]
    assert with_junk["pricing_status"] != "SANITY_FLAG"
    # сама мусорная цена числа не даёт
    assert with_junk["y_idx_by_price"].get(1.5) is None


def test_row_sanity_silences_the_pricing_price_everywhere(ruonia_curve):
    """Взведён строчный sanity — молчит ЦЕНА РАСЧЁТА во всех путях: и в
    верхнеуровневых полях, и в картах по ценам внутри горизонтов (иначе на одном
    экране прочерк и число из одного расчёта).

    ЧУЖИЕ ЦЕНЫ БАТЧА ОСТАЮТСЯ. Санити — вердикт одной цены, а батчевые
    потребители (y_idx_many, лестница стакана) передают первым аргументом
    произвольный элемент набора: гасить по нему весь батч значит терять числа
    здоровых уровней."""
    m = _calc(1.5, [99.0, 99.5], ruonia_curve)
    assert m["yield_over_index_bps"] is None
    for name, h in (m.get("horizons") or {}).items():
        assert h.get("yield_over_index_bps") is None, name
        assert 1.5 not in (h.get("y_idx_by_price") or {}), name
        assert 1.5 not in (h.get("ytm_by_price") or {}), name
    assert 1.5 not in (m.get("y_idx_by_price") or {})


def test_row_sanity_keeps_healthy_prices_of_the_batch(ruonia_curve):
    """Регресс, найденный самопроверкой: батч из сетки цен приходит с
    произвольной первой ценой (нижний узел, лучший бид, нижний уровень книги).
    Безумие этой цены не должно стирать числа остальных — иначе у бумаги в
    последние дни жизни молчала бы вся сетка."""
    m = _calc(1.5, [99.0, 99.5], ruonia_curve)
    hz = (m.get("horizons") or {}).get("maturity") or {}
    alive = {p: v for p, v in (hz.get("y_idx_by_price") or {}).items()
             if v is not None}
    assert set(alive) == {99.0, 99.5}, hz.get("y_idx_by_price")
