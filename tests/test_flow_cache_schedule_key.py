"""Кэш потоков не должен отдавать поток, построенный на ДРУГОМ расписании.

Аудит конвейера 03.09, находка 1 (critical). Ключ кэша был ("main", spread) —
графика платежей в нём не было вовсе. Одна осечка выборки bondization (ISS
отдал пусто, слой расписаний пустое не кэширует и повторит через секунды)
роняла в кэш bullet-поток по СГЕНЕРИРОВАННОМУ расписанию: без амортизаций, без
реза по оферте. ISS восстанавливался, цена и номинал шли уже из настоящего
графика, а поток приходил хитом из отравленного кэша — и жил до конца дня.
"""
from datetime import date, timedelta

from services.valuation import calculate_valuation_metrics
from tests.conftest import make_bond, quarterly_periods


MATURITY = date(2030, 1, 12)
ISSUE = date(2024, 1, 12)


def _amorts_half():
    """Половина номинала гасится за год до погашения, остаток — на погашении.
    Bullet-поток вместо этого вернёт всю 1000 в конце."""
    return [{"date": (MATURITY - timedelta(days=365)).isoformat(), "value": 500.0},
            {"date": MATURITY.isoformat(), "value": 500.0}]


def _calc(bond, curve, calc_date, *, periods, amorts, flows_cache):
    return calculate_valuation_metrics(
        bond, 99.0, curve, calc_date,
        accrued_override=0.0, periods=periods, amorts=amorts, offers=None,
        with_margins=False, flows_cache=flows_cache,
    )


def test_schedule_change_does_not_hit_stale_flow(keyrate_curve, calc_date):
    """Осечка расписания, затем полный график: числа обязаны совпасть с расчётом
    без кэша, а не прийти хитом bullet-потока."""
    bond = make_bond(maturity=MATURITY, issue=ISSUE)
    periods = quarterly_periods(ISSUE, MATURITY)
    amorts = _amorts_half()

    cache = {}
    # 1) ISS молчит: ни расписания, ни амортизаций — поток генерится, bullet
    _calc(bond, keyrate_curve, calc_date,
          periods=None, amorts=None, flows_cache=cache)
    assert cache, "первый расчёт обязан наполнить кэш"

    # 2) ISS ответил: полный график с амортизацией — в ТОТ ЖЕ кэш бумаги
    poisoned = _calc(bond, keyrate_curve, calc_date,
                     periods=periods, amorts=amorts, flows_cache=cache)
    clean = _calc(bond, keyrate_curve, calc_date,
                  periods=periods, amorts=amorts, flows_cache=None)

    assert poisoned["yield_xirr_pct"] == clean["yield_xirr_pct"]
    assert poisoned["yield_over_index_bps"] == clean["yield_over_index_bps"]


def test_same_schedule_still_hits(keyrate_curve, calc_date):
    """Отпечаток не должен убивать сам кэш: тот же вход — то же попадание."""
    bond = make_bond(maturity=MATURITY, issue=ISSUE)
    periods = quarterly_periods(ISSUE, MATURITY)
    amorts = _amorts_half()

    cache = {}
    _calc(bond, keyrate_curve, calc_date,
          periods=periods, amorts=amorts, flows_cache=cache)
    keys_after_first = set(cache)
    _calc(bond, keyrate_curve, calc_date,
          periods=periods, amorts=amorts, flows_cache=cache)
    assert set(cache) == keys_after_first, "тот же график не должен плодить ключи"


def test_margin_change_does_not_hit_stale_flow_at_offer(keyrate_curve, calc_date):
    """Правка маржи в Справочнике: ключ ('cut', cut) горизонта оферты спреда
    выпуска не нёс вовсе — маржу правили, а поток к оферте приходил старый."""
    periods = quarterly_periods(ISSUE, MATURITY)
    offers = [{"date": date(2028, 1, 12).isoformat(), "type": "put", "price": 100.0}]

    def calc(bond, cache):
        return calculate_valuation_metrics(
            bond, 99.0, keyrate_curve, calc_date,
            accrued_override=0.0, periods=periods, amorts=None, offers=offers,
            with_margins=False, flows_cache=cache)

    cache = {}
    calc(make_bond(margin_bps=150, maturity=MATURITY, issue=ISSUE), cache)

    wide = make_bond(margin_bps=900, maturity=MATURITY, issue=ISSUE)
    poisoned = calc(wide, cache)
    clean = calc(wide, None)

    assert poisoned["yield_to_offer_pct"] == clean["yield_to_offer_pct"]
    assert poisoned["yield_xirr_pct"] == clean["yield_xirr_pct"]


def test_fingerprint_is_order_independent(keyrate_curve, calc_date):
    """Витрина строит расписание как пришло, движок — отсортированным
    (_periods_from_coupons). Пока источник отдаёт купоны по возрастанию даты,
    списки совпадают, но зависеть от этого нельзя: разошёлся порядок — разошёлся
    ключ, и общий кэш потоков превращается в два набора ключей на бумагу."""
    from services.valuation import _schedule_fp

    bond = make_bond(maturity=MATURITY, issue=ISSUE)
    periods = [(date.fromisoformat(s), date.fromisoformat(e), v)
               for (s, e, v) in quarterly_periods(ISSUE, MATURITY)]
    amorts = _amorts_half()

    assert _schedule_fp(bond, periods, amorts, None) == \
        _schedule_fp(bond, list(reversed(periods)), list(reversed(amorts)), None)
    # но другой график — всё ещё другой ключ
    assert _schedule_fp(bond, periods, amorts, None) != \
        _schedule_fp(bond, periods[:-1], amorts, None)


def test_fingerprint_survives_odd_input(keyrate_curve, calc_date):
    """Неожиданная форма входа не должна ронять расчёт строки: отпечаток
    остаётся хэшируемым, и ключ кэша строится без исключения."""
    from services.valuation import _schedule_fp

    bond = make_bond(maturity=MATURITY, issue=ISSUE)
    weird = [({"нежданный": "словарь"}, ["список"], None)]
    fp = _schedule_fp(bond, weird, [{"date": {"x": 1}, "value": None}], None)
    assert hash((fp, ("main", 150)))          # ключ кэша собирается
