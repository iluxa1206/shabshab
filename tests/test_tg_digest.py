"""Вечерний «разбор дня»: отбор данных и сборка альбома."""
import asyncio
from datetime import date

import pytest

from services import charts_png as ch
from services import tg_digest as dg

DAY, PREV, OLD = "2026-08-25", "2026-08-22", "2026-08-21"


def _rows(day, prev):
    return {day: [
        # чистое движение: имя есть, оборот выше порога
        {"isin": "A", "kind": "floater", "y_idx_close_bps": 200.0,
         "g_spread_close_bps": None, "close_pct": 99.0,
         "value": 50e6, "trades": 10},
        # оборот ниже порога — одна сделка не делает движения дня
        {"isin": "B", "kind": "floater", "y_idx_close_bps": 900.0,
         "g_spread_close_bps": None, "close_pct": 80.0, "value": 1e5, "trades": 1},
        # битый спред: в рейтинге он забрал бы весь масштаб
        {"isin": "C", "kind": "fixed", "y_idx_close_bps": None,
         "g_spread_close_bps": 999999.0, "close_pct": 10.0,
         "value": 90e6, "trades": 5},
        # вне реестра (имени нет) — дайджест про наш юниверс
        {"isin": "D", "kind": "fixed", "y_idx_close_bps": None,
         "g_spread_close_bps": 300.0, "close_pct": 95.0, "value": 80e6, "trades": 7},
    ], prev: [
        {"isin": "A", "kind": "floater", "y_idx_close_bps": 150.0,
         "g_spread_close_bps": None, "close_pct": 99.2, "value": 40e6, "trades": 8},
        {"isin": "B", "kind": "floater", "y_idx_close_bps": 100.0,
         "g_spread_close_bps": None, "close_pct": 81.0, "value": 2e5, "trades": 2},
        {"isin": "C", "kind": "fixed", "y_idx_close_bps": None,
         "g_spread_close_bps": 250.0, "close_pct": 10.5, "value": 70e6, "trades": 4},
        {"isin": "D", "kind": "fixed", "y_idx_close_bps": None,
         "g_spread_close_bps": 280.0, "close_pct": 95.1, "value": 60e6, "trades": 6},
    ], OLD: [
        # позавчера A был ещё уже — значит сегодня второй день расширения
        {"isin": "A", "kind": "floater", "y_idx_close_bps": 120.0,
         "g_spread_close_bps": None, "close_pct": 99.4, "value": 30e6, "trades": 5},
    ]}


@pytest.fixture
def fake_day(monkeypatch):
    data = _rows(DAY, PREV)
    monkeypatch.setattr(dg, "_last_days", lambda n: [DAY, PREV, OLD][:n])
    monkeypatch.setattr(dg, "_day_rows", lambda d: data.get(d, []))
    monkeypatch.setattr(dg, "_spread_history", lambda days: {
        isin: {d: (r.get("y_idx_close_bps")
                   if r.get("y_idx_close_bps") is not None
                   else r.get("g_spread_close_bps"))
               for d in days for r in data.get(d, []) if r["isin"] == isin}
        for isin in ("A", "B", "C", "D")})
    monkeypatch.setattr(dg, "_blocks", lambda days, kinds=None: [])
    monkeypatch.setattr(dg, "_new_issues", lambda days: [])
    import services.instruments_registry as reg
    monkeypatch.setattr(reg, "labels_map",
                        lambda isins=None: {"A": {"name": "Тест 1Р-01"},
                                            "B": {"name": "Тест 1Р-02"},
                                            "C": {"name": "Тест 1Р-03"}})
    monkeypatch.setattr(dg, "_curve_series", lambda d: {})
    monkeypatch.setattr(dg, "_moex_meta", lambda: {})
    monkeypatch.setattr(dg, "_hourly_profile", lambda day, scope="floater": [])
    monkeypatch.setattr(dg, "_daily_profile", lambda days, scope="floater": [])
    return DAY


def test_collect_filters_noise(fake_day):
    """Альбом флоатеров видит только флоатеров — фиксы идут своим альбомом."""
    d = dg.collect(scope="floater")
    assert [m["name"] for m in d["movers"]] == ["Тест 1Р-01"]
    # обороты — только флоатеры реестра: B по имени проходит, C/D — другой класс
    assert [t["name"] for t in d["turnover"]] == ["Тест 1Р-01", "Тест 1Р-02"]
    assert d["traded"] == 2


def test_collect_fixed_scope_takes_only_fixed(fake_day):
    d = dg.collect(scope="fixed")
    # C отсеян санитаром премии, D — без имени; движений у фиксов не осталось
    assert d["movers"] == [] and d["scope"] == "fixed"
    assert [t["name"] for t in d["turnover"]] == ["Тест 1Р-03"]
    # кривой и выплат у фикс-альбома нет: своп-кривая КС — про флоатеры
    assert d["curve"] == {}


def test_streak_counts_same_direction_days(fake_day):
    """Серия: 120 → 150 → 200, все дни в одну сторону — две дельты подряд."""
    d = dg.collect()
    assert d["movers"][0]["streak"] == 2


def test_streak_breaks_on_gap_and_turn():
    days = ["d3", "d2", "d1", "d0"]
    hist = {"d3": 200.0, "d2": 150.0, "d1": 160.0}        # разворот на d1
    assert dg._streak(hist, days, +1) == 1
    assert dg._streak({"d3": 200.0}, days, +1) == 0       # дырка в истории
    assert dg._streak(hist, days, -1) == 0                # знак не тот


def test_week_mode_sums_turnover(fake_day, monkeypatch):
    """Недельное окно: база сравнения дальше, обороты складываются."""
    monkeypatch.setattr(dg, "WEEK_SESSIONS", 2)
    d = dg.collect("week", "floater")
    assert d["mode"] == "week" and d["prev"] == OLD
    # A: 50 млн сегодня + 40 млн в prev = 90 млн за окно
    top = {t["name"]: t["value"] for t in d["turnover"]}
    assert top["Тест 1Р-01"] == pytest.approx(90e6)
    assert d["movers"][0]["delta_bps"] == pytest.approx(80.0)


def test_build_album_makes_all_pngs(fake_day, monkeypatch):
    async def _no_payments(day, horizon=dg.PAYMENT_DAYS):
        return [], date.fromisoformat(fake_day)
    monkeypatch.setattr(dg, "_payment_days", _no_payments)
    items, caption, ctx = asyncio.run(dg.build_album())
    assert [n for n, _p, _c in items] == [
        "movers.png", "breadth.png", "turnover.png", "blocks.png", "map.png",
        "ratings.png", "profile.png"]     # без кривой и выплат: их тут нет
    assert all(png[:4] == b"\x89PNG" for _n, png, _c in items)
    # подпись — только у первой картинки: Telegram показывает её под альбомом
    assert items[0][2] == caption and all(c is None for _n, _p, c in items[1:])
    assert "Разбор дня · Флоатеры" in caption
    # ссылка на график и копируемый ISIN — из чата уходят в дашборд
    assert "/app/chart/A" in caption and "<code>A</code>" in caption
    assert "Y-IDX" in caption          # метрика класса названа явно
    assert ctx["data"]["day"] == fake_day


def test_album_silent_without_data(monkeypatch):
    monkeypatch.setattr(dg, "collect",
                        lambda mode="day", scope="floater": {"day": None})
    items, caption, ctx = asyncio.run(dg.build_album())
    assert items == [] and caption == "" and ctx == {}


def test_signals_line_counts_only_this_day(monkeypatch):
    import services.signals as sg
    monkeypatch.setattr(sg, "events_for_user", lambda email, limit=500: [
        {"name": "Тест 1Р-01", "fired_at": "2026-08-25T10:00:00+00:00"},
        {"name": "Тест 1Р-01", "fired_at": "2026-08-25T11:00:00+00:00"},
        {"name": "Тест 1Р-02", "fired_at": "2026-08-25T12:00:00+00:00"},
        {"name": "Тест 1Р-03", "fired_at": "2026-08-24T12:00:00+00:00"},   # не тот день
    ])
    line = dg._signals_line("a@b.c", "2026-08-25")
    assert "3" in line and "Тест 1Р-01 ×2" in line
    assert dg._signals_line(None, "2026-08-25") is None


def test_buttons_are_url_only():
    """callback_query никто не разбирает — кнопки только ссылками."""
    rows = dg._buttons()["inline_keyboard"]
    assert all(b.get("url", "").startswith("http") for row in rows for b in row)


def test_charts_survive_empty_input():
    assert ch.movers([], "x")[:4] == b"\x89PNG"
    assert ch.turnover([], "x")[:4] == b"\x89PNG"
    assert ch.blocks([], "x")[:4] == b"\x89PNG"
    assert ch.breadth([], "x")[:4] == b"\x89PNG"
    assert ch.scatter([], "x")[:4] == b"\x89PNG"
    assert ch.grouped([], [], "x")[:4] == b"\x89PNG"
    assert ch.profile([], "x")[:4] == b"\x89PNG"
    assert ch.curve([], [], "x")[:4] == b"\x89PNG"
    assert ch.payments([], "x")[:4] == b"\x89PNG"


def test_thin_intervals_lose_median_but_keep_volume():
    """Вечерний час с двумя сделками не должен дёргать линию премии."""
    rows = dg._thin_out([
        {"label": "12:00", "v_float": 1e9, "v_fixed": 1e9,
         "y_float": 300.0, "y_fixed": 400.0},
        {"label": "21:00", "v_float": 1e6, "v_fixed": 1e9,
         "y_float": 900.0, "y_fixed": 410.0}])
    assert rows[0]["y_float"] == 300.0
    # порог считается по каждому рынку отдельно: фиксы в этот час торговались
    assert rows[1]["y_float"] is None and rows[1]["v_float"] == 1e6
    assert rows[1]["y_fixed"] == 410.0


def test_rating_medians_split_floaters_and_fixed(monkeypatch):
    """Y-IDX и g-спред — разные метрики: в один столбик их складывать нельзя."""
    monkeypatch.setattr(dg, "_rating_buckets", lambda isins: {
        "A": "AA", "B": "AA", "C": "A", "D": "AA"})
    today = {
        "A": {"kind": "floater", "y_idx_close_bps": 200.0, "value": 50e6},
        "B": {"kind": "floater", "y_idx_close_bps": 300.0, "value": 50e6},
        "C": {"kind": "fixed", "g_spread_close_bps": 500.0, "value": 50e6},
        # тонкая бумага в расчёт медианы не идёт
        "D": {"kind": "floater", "y_idx_close_bps": 9000.0, "value": 1e5},
    }
    out = dg._rating_medians(today, {}, lambda i: "имя")
    assert out["cats"] == ["AA", "A"]
    assert out["floater"] == [250.0, None]
    assert out["fixed"] == [None, 500.0]


def test_profile_takes_only_its_scope(monkeypatch):
    """Профиль альбома считает оборот и медиану только своего класса."""
    monkeypatch.setattr(dg, "_day_rows", lambda d: [
        {"isin": "A", "kind": "floater", "y_idx_close_bps": 200.0, "value": 60e6},
        {"isin": "B", "kind": "floater", "y_idx_close_bps": 300.0, "value": 40e6},
        {"isin": "C", "kind": "fixed", "g_spread_close_bps": 500.0, "value": 50e6},
    ])
    row = dg._daily_profile(["2026-08-25"], "floater")[0]
    assert row["v_float"] == 100e6 and row["v_fixed"] == 0.0
    assert row["y_float"] == 250.0 and row["y_fixed"] is None
    row = dg._daily_profile(["2026-08-25"], "fixed")[0]
    assert row["v_fixed"] == 50e6 and row["y_fixed"] == 500.0


def test_map_skips_matured_and_insane(monkeypatch):
    today = {
        "A": {"kind": "floater", "y_idx_close_bps": 250.0, "value": 50e6},
        "B": {"kind": "fixed", "g_spread_close_bps": -5000.0, "value": 50e6},
        "C": {"kind": "fixed", "g_spread_close_bps": 300.0, "value": 50e6},
    }
    mats = {"A": "2030-01-01", "B": "2030-01-01", "C": "1999-01-01"}
    pts = dg._map_points(today, lambda i: "имя", lambda i: mats.get(i))
    assert [p["y"] for p in pts] == [250.0]     # B — санитар, C — уже погашена


def test_payment_days_use_issue_totals(monkeypatch):
    """Суммируем total_rub (платёж по выпуску), а не amount_rub (на бумагу)."""
    base = date(2026, 8, 26)

    async def _cal():
        return {"calc_date": base, "events": [
            {"date": date(2026, 8, 27), "type": "COUPON",
             "amount_rub": 48.17, "total_rub": 3_965_258.06, "paid": False},
            {"date": date(2026, 8, 27), "type": "REDEMPTION",
             "amount_rub": 80.0, "total_rub": 6_585_440.0, "paid": False},
            {"date": date(2026, 8, 20), "type": "COUPON",       # уже прошло
             "amount_rub": 10.0, "total_rub": 1e9, "paid": True},
            {"date": date(2027, 1, 1), "type": "COUPON",        # вне окна
             "amount_rub": 10.0, "total_rub": 1e9, "paid": False},
        ]}

    import services.payments_calendar as pc
    monkeypatch.setattr(pc, "build_payments_calendar", _cal)
    days, frm = asyncio.run(dg._payment_days(date(2026, 8, 25)))
    assert frm == base
    assert len(days) == 1
    assert days[0]["coupon"] == pytest.approx(3_965_258.06)
    assert days[0]["redemption"] == pytest.approx(6_585_440.0)
