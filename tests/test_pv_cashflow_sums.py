"""PV дисконтирует СУММУ платежей на дату и не зависит от длины потока квадратично.

Раньше на каждую дату сетки PV заново перебирал весь поток
(«sum(... for cf in cashflows if cf.pay_date == d)»), то есть был квадратичным
по числу купонов. У обычной бумаги (20–40 платежей) это незаметно, а у
тридцатилетнего ипотечного агента с ежемесячным купоном (ИАДОМ 1P62, 368
платежей) один PV стоил 135 тысяч сравнений — и солвер, зовущий PV полтора
десятка раз на каждую цену стакана, держал ядро 700 мс на одну бумагу.
"""
import time
from datetime import date, timedelta

from core.valuation import BondRefData, Cashflow, pv_cashflows_with_dm

CALC = date(2026, 9, 1)


class _Flat:
    """Плоская форвардная кривая: тест про суммирование потока, не про кривую."""
    rate_convention = "simple"

    def __init__(self, r):
        self.r = r

    def forward(self, t1, t2):
        return self.r


def _bond():
    return BondRefData(isin="TEST", base="KEYRATE", spread_issue_bps=200,
                       face_value=1000.0, accrued_rub=0.0,
                       maturity_date=date(2056, 12, 28),
                       first_coupon_date=date(2026, 10, 1),
                       coupons_per_year=12, coupon_period_days=30)


def _flows(n, coupon=14.0):
    out, d = [], CALC + timedelta(days=30)
    for _ in range(n):
        out.append(Cashflow(pay_date=d, amount_rub=coupon, type="COUPON"))
        d += timedelta(days=30)
    out.append(Cashflow(pay_date=d, amount_rub=1000.0, type="REDEMPTION"))
    return out


def test_several_cashflows_on_one_date_are_summed():
    """Купон, амортизация и погашение могут прийти в один день — PV обязан
    дисконтировать их сумму, а не первый попавшийся."""
    curve = _Flat(0.17)
    d = CALC + timedelta(days=30)
    one = [Cashflow(pay_date=d, amount_rub=1014.0, type="REDEMPTION")]
    split = [Cashflow(pay_date=d, amount_rub=14.0, type="COUPON"),
             Cashflow(pay_date=d, amount_rub=1000.0, type="REDEMPTION")]
    assert (pv_cashflows_with_dm(_bond(), curve, one, CALC, 250)
            == pv_cashflows_with_dm(_bond(), curve, split, CALC, 250))


def test_past_cashflows_are_ignored():
    """Платежи до даты поставки в PV не входят — их уже получил прошлый
    держатель."""
    curve = _Flat(0.17)
    live = _flows(4)
    past = [Cashflow(pay_date=CALC - timedelta(days=10), amount_rub=99.0,
                     type="COUPON")] + live
    assert (pv_cashflows_with_dm(_bond(), curve, past, CALC, 250)
            == pv_cashflows_with_dm(_bond(), curve, live, CALC, 250))


def test_long_bond_is_not_quadratic():
    """Длинный поток считается ЛИНЕЙНО.

    Порог с большим запасом — тест про порядок роста, а не про абсолютную
    скорость машины: при квадратичном PV поток из 368 платежей был в сотню раз
    дороже потока из 36, теперь разница около десятка."""
    curve = _Flat(0.17)
    bond = _bond()

    def ms(n):
        cfs = _flows(n)
        pv_cashflows_with_dm(bond, curve, cfs, CALC, 250)      # прогрев
        t0 = time.perf_counter()
        for _ in range(10):
            pv_cashflows_with_dm(bond, curve, cfs, CALC, 250)
        return (time.perf_counter() - t0) * 1000

    short, long = ms(36), ms(368)
    assert long < short * 30, f"рост {long / max(short, 1e-6):.0f}× — похоже на квадрат"


def test_level_dm_is_off_by_default(monkeypatch):
    """DM на каждый уровень стакана — по флагу, не по умолчанию.

    Замер 01.09.2026: discount margin на цену стоит столько же, сколько сам
    Y-IDX с доходностью, то есть удваивает цену лестницы — а показывается одной
    подсказкой при наведении. Лестница пересчитывается на каждый пуш книги, так
    что цена постоянная, а польза разовая."""
    import importlib
    from services import orderbook_svc

    monkeypatch.delenv("ORDERBOOK_LEVEL_DM", raising=False)
    importlib.reload(orderbook_svc)
    assert orderbook_svc.LEVEL_DM is False

    monkeypatch.setenv("ORDERBOOK_LEVEL_DM", "1")
    importlib.reload(orderbook_svc)
    assert orderbook_svc.LEVEL_DM is True

    monkeypatch.delenv("ORDERBOOK_LEVEL_DM")
    importlib.reload(orderbook_svc)


def test_snapshot_is_written_in_chunks(tmp_path, monkeypatch):
    """Снимок спредов пишется ПАЧКАМИ и не переписывается при каждом старте.

    Две тысячи строк одной транзакцией держали поток на секунды (сторож лага
    01.09.2026 — 4,8 с со стеком write_snapshot), причём стартовый снимок падал
    ровно на прогрев: деплоев за день несколько, а снимок за дату один.
    """
    import services.portfolio_db as pdb
    monkeypatch.setattr(pdb, "DB_PATH", tmp_path / "p.db")
    pdb.init_db()

    from services import spread_history as sh
    from services.market_data import market_cache

    monkeypatch.setattr(sh, "_SNAP_CHUNK", 3)
    market_cache["universe_metrics"] = {
        f"RU000A10{i:04d}": {"last": 100.0, "yoi": 150 + i, "horizon": "maturity"}
        for i in range(7)
    }
    market_cache["fixed_metrics"] = {}
    try:
        assert sh.has_snapshot() is False
        assert sh.write_snapshot() == 7          # пачками по 3 — строки все на месте
        assert sh.has_snapshot() is True
    finally:
        market_cache.pop("universe_metrics", None)
        market_cache.pop("fixed_metrics", None)


def test_margins_can_be_switched_off(keyrate_curve, ruonia_curve, calc_date,
                                     flat_index_15, monkeypatch):
    """SM и DM выключаются, Y-IDX остаётся.

    Замер 01.09.2026: на маржи уходит 78–92 % расчёта бумаги — каждая это
    солвер, а DM вдобавок ПЕРЕСОБИРАЕТ поток на плоской кривой, и всё это для
    шестисот бумаг на каждом движении цены. Витрина живёт Y-IDX, поэтому
    считает без марж; карточка и лента продолжают их считать."""
    from conftest import make_bond, quarterly_periods
    from core.valuation import settle_date
    import services.valuation as sv

    monkeypatch.setattr(
        "services.valuation._index_provider",
        lambda base, warnings, calc_date=None: (flat_index_15[0],
                                                list(zip(*flat_index_15[1]))))
    bond = make_bond(margin_bps=150, accrued=0.0)
    periods = quarterly_periods(settle_date(calc_date), bond.maturity_date)
    kw = dict(accrued_override=0.0, periods=periods, ruonia_curve=ruonia_curve)

    full = sv.calculate_valuation_metrics(bond, 100.0, keyrate_curve, calc_date, **kw)
    lean = sv.calculate_valuation_metrics(bond, 100.0, keyrate_curve, calc_date,
                                          with_margins=False, **kw)

    assert full["sm_bps"] is not None and full["disc_margin_bps"] is not None
    assert lean["sm_bps"] is None and lean["disc_margin_bps"] is None
    # первичная метрика витрины не зависит от марж — считается тем же путём
    assert lean["yield_over_index_bps"] == full["yield_over_index_bps"]
    assert lean["yield_xirr_pct"] == full["yield_xirr_pct"]
    assert lean["dirty_price_rub"] == full["dirty_price_rub"]


def test_flows_cache_reuses_stream_and_keeps_numbers(keyrate_curve, ruonia_curve,
                                                     calc_date, flat_index_15,
                                                     monkeypatch):
    """Поток строится ОДИН раз на бумагу, а не на каждую цену.

    Цена входит в расчёт только через dirty и солверы — сам график платежей от
    неё не зависит. Пересборка стоила 35 мс у тридцатилетнего ипотечного агента
    и повторялась на каждом движении цены; числа при этом обязаны остаться теми
    же до бита."""
    from conftest import make_bond, quarterly_periods
    from core.valuation import settle_date
    import services.valuation as sv

    monkeypatch.setattr(
        "services.valuation._index_provider",
        lambda base, warnings, calc_date=None: (flat_index_15[0],
                                                list(zip(*flat_index_15[1]))))
    bond = make_bond(margin_bps=150, accrued=0.0)
    periods = quarterly_periods(settle_date(calc_date), bond.maturity_date)

    builds = []
    real_build = sv.build_cashflows_with_spread

    def counting_build(*a, **kw):
        builds.append(1)
        return real_build(*a, **kw)

    monkeypatch.setattr(sv, "build_cashflows_with_spread", counting_build)
    kw = dict(accrued_override=0.0, periods=periods, ruonia_curve=ruonia_curve)

    cache: dict = {}
    a = sv.calculate_valuation_metrics(bond, 100.0, keyrate_curve, calc_date,
                                       flows_cache=cache, **kw)
    first = len(builds)
    assert first > 0 and cache, "первый расчёт строит поток и кладёт его в кэш"

    # ВТОРАЯ ЦЕНА той же бумаги — поток из кэша, ни одной пересборки
    b = sv.calculate_valuation_metrics(bond, 99.5, keyrate_curve, calc_date,
                                       flows_cache=cache, **kw)
    assert len(builds) == first, "поток пересобрался на новой цене"

    # без кэша числа те же — кэш не меняет расчёт, только его цену
    c = sv.calculate_valuation_metrics(bond, 99.5, keyrate_curve, calc_date, **kw)
    for k in ("yield_over_index_bps", "yield_xirr_pct", "dirty_price_rub",
              "sm_bps", "disc_margin_bps"):
        assert b[k] == c[k], k
    assert a["yield_over_index_bps"] != b["yield_over_index_bps"], "цена всё же влияет"
    # предупреждения сборки не теряются на попадании в кэш
    assert set(c["warnings"]) <= set(b["warnings"])


def test_orderbook_levels_reuse_the_flow(monkeypatch):
    """Лестница стакана строит поток РАЗ НА КОНТЕКСТ, а не на каждый пуш книги.

    Уровни считаются батчем, но сам график платежей собирался заново при каждом
    вызове levels_fn — 35 мс у тридцатилетнего ипотечного агента, дважды
    (погашение и оферта), и так на каждом движении стакана."""
    import asyncio
    from services import orderbook_svc as ob
    import services.valuation as sv

    seen = []

    def fake_metrics(*a, **kw):
        seen.append(kw.get("flows_cache"))
        return {"horizons": {}, "y_idx_by_price": {}}

    monkeypatch.setattr(sv, "calculate_valuation_metrics", fake_metrics)
    monkeypatch.setattr(ob, "load_reprice_ctx",
                        lambda isin, cache: _async({"ref_obj": None, "curve": None,
                                                    "calc_date": None, "periods": None,
                                                    "amorts": None, "offers": None,
                                                    "accrued_live": None}),
                        raising=False)
    monkeypatch.setattr(ob.MarketDataService, "get_local_bond_cache",
                        staticmethod(lambda p: {}))
    monkeypatch.setattr("services.universe_stream.request_bond", lambda isin: None)

    levels_fn, _cd, _face = asyncio.run(ob.build_levels_fn("RU000A109B33"))
    levels_fn([100.0])
    levels_fn([99.5])
    assert len(seen) == 2, "оба вызова дошли до расчёта"
    # ОДИН И ТОТ ЖЕ словарь кэша — значит поток из него переиспользуется
    assert seen[0] is seen[1] and seen[0] is not None


async def _async(value):
    return value
