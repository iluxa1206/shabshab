"""Конструктор портфеля: набор, кэпы диверсификации, раздача денег по стакану.

Всё на синтетическом рынке — ни сети, ни БД: build() чистая, снимок рынка
подаётся фикстурами (так же, как его подаёт build_live в проде).
"""
import inspect
from datetime import date, timedelta

import pytest

from services import portfolio_build as pb
from services import screener_core as sc
from services.screener_core import FilterError

TODAY = date(2026, 8, 27)
AMOUNT = 100_000_000.0
FACE = 1000.0
ACCRUED = 5.0
DEEP_QTY = 100_000          # уровень заведомо толще любого тикета теста


def _uni_row(i: int, **kw) -> dict:
    """Бумага универса: свой эмитент, срок i+1 лет, рейтинг по кругу."""
    row = {
        "isin": f"RU000TEST{i:04d}",
        "name": f"Тест {i}",
        "emitter_name": f"Эмитент {i}",
        "rating": ["AAA", "AA", "A"][i % 3],
        "base_rate_type": "KEYRATE" if i % 2 else "RUONIA",
        "spread_issue_bps": 100 + i,
        "coupons_per_year": 4,
        "maturity_date": (TODAY + timedelta(days=365 * (i + 1))).isoformat(),
        "has_call": False,
    }
    row.update(kw)
    return row


def _metrics_row(i: int, **kw) -> dict:
    """Метрики: цена 100, Y-IDX убывает с номером — так порядок жадного
    набора детерминирован и читаем."""
    row = {
        "ask": 100.0, "yoi_ask": 400.0 - 10.0 * i, "yoi_slope": -50.0,
        "yoi": 400.0 - 10.0 * i, "last": 100.0,
        "face_px": FACE, "accrued_settle": ACCRUED,
        "spread_dur": 2.0, "current_coupon": 18.0,
        "has_amort": False, "offer_date": None,
        "price_stale": False, "implausible": False, "price_thin": False,
    }
    row.update(kw)
    return row


def _ladder(price: float = 100.0, qty: int = DEEP_QTY, levels: int = 3) -> dict:
    return {"a": [[price + 0.05 * k, qty] for k in range(levels)],
            "b": [[price - 0.05 * (k + 1), qty] for k in range(levels)]}


@pytest.fixture
def market():
    """20 бумаг, у каждой свой эмитент и толстый стакан."""
    uni = [_uni_row(i) for i in range(20)]
    metrics = {u["isin"]: _metrics_row(i) for i, u in enumerate(uni)}
    depth = {u["isin"]: _ladder() for u in uni}
    return uni, metrics, depth


def _build(market, **raw):
    uni, metrics, depth = market
    p = pb.normalize({"amount_rub": AMOUNT, **raw})
    return pb.build(p, uni, metrics, depth, adv={}, events=[], today=TODAY)


# ─────────────────────────────── набор ───────────────────────────────

def test_basic_set(market):
    out = _build(market, n=15)
    assert len(out["positions"]) == 15
    emitters = [r["emitter"] for r in out["positions"]]
    assert len(set(emitters)) == 15, "кэп 1 бумага на эмитента"
    assert out["totals"]["money_rub"] <= AMOUNT + 0.01
    # взяты лучшие по спреду: Y-IDX убывает с номером бумаги
    assert out["positions"][0]["isin"] == "RU000TEST0000"
    # цена и лоты сходятся с суммой позиции
    r = out["positions"][0]
    assert r["money_rub"] == pytest.approx(r["qty"] * (FACE * r["price"] / 100 + ACCRUED), abs=0.01)


def test_few_emitters_reports_shortage(market):
    """Эмитентов меньше n → позиций меньше n, кэп НЕ ослабляется молча."""
    uni, metrics, depth = market
    for u in uni:
        u["emitter_name"] = "Один эмитент"
    out = _build((uni, metrics, depth), n=10)
    assert len(out["positions"]) == 1
    assert any("Набралось 1 из 10" in w for w in out["warnings"])


def test_thin_book_caps_position(market):
    """Тонкий стакан: позиция урезана, остаток раскидан, недобор в warnings."""
    uni, metrics, depth = market
    depth[uni[0]["isin"]] = _ladder(qty=1000, levels=1)   # ~1 млн ₽ вместо 10
    out = _build((uni, metrics, depth), n=10)
    thin = next(r for r in out["positions"] if r["isin"] == uni[0]["isin"])
    assert thin["capped"] is True
    assert thin["money_rub"] < AMOUNT / 10
    assert any("урезано стаканом" in w for w in out["warnings"])
    # высвободившееся ушло другим позициям, а не потерялось
    fat = next(r for r in out["positions"] if r["isin"] == uni[1]["isin"])
    assert fat["money_rub"] > AMOUNT / 10


def test_ladder_mode_spreads_across_buckets(market):
    """Лесенка: набор разложен по корзинам срока, а не собран из коротких."""
    out = _build(market, n=8, mode="ladder", buckets=[2, 4, 6])
    buckets = {r["bucket"] for r in out["positions"]}
    assert len(buckets) == 4, f"ожидались все четыре корзины, вышло {buckets}"


def test_ladder_empty_bucket_warns(market):
    """Пустая корзина отдаёт квоту соседям и говорит об этом."""
    out = _build(market, n=8, mode="ladder", buckets=[2, 4, 100])
    assert any("Корзины недобраны" in w for w in out["warnings"])
    assert len(out["positions"]) == 8


# ───────────────────────── ручная правка ─────────────────────────────

def test_manual_amount_is_exact(market):
    uni, _, _ = market
    isin = uni[5]["isin"]
    out = _build(market, n=10, manual={isin: 20_000_000})
    pos = next(r for r in out["positions"] if r["isin"] == isin)
    unit = FACE * pos["price"] / 100 + ACCRUED
    assert pos["money_rub"] == pytest.approx(20_000_000, abs=unit)
    assert pos["manual"] is True


def test_manual_over_book_marks_estimate(market):
    uni, metrics, depth = market
    isin = uni[5]["isin"]
    depth[isin] = _ladder(qty=1000, levels=1)             # ~1 млн ₽ в книге
    out = _build((uni, metrics, depth), n=10, manual={isin: 20_000_000})
    pos = next(r for r in out["positions"] if r["isin"] == isin)
    assert pos["price_estimated"] is True
    assert pos["money_rub"] > 19_000_000, "ручную сумму исполняем как просили"
    assert any("цена оценочная" in w for w in out["warnings"])


def test_manual_over_amount_is_error(market):
    with pytest.raises(FilterError):
        _build(market, n=10, manual={"RU000TEST0001": AMOUNT * 2})


def test_pin_takes_bond_past_filter(market):
    """Прикнопленная бумага входит, даже если фильтр её не пропускает."""
    uni, _, _ = market
    isin = uni[9]["isin"]                                  # рейтинг A, срок 10 лет
    out = _build(market, n=5, ratings=["AAA"], years_max=3, pin=[isin])
    assert isin in {r["isin"] for r in out["positions"]}
    assert next(r for r in out["positions"] if r["isin"] == isin)["pinned"] is True


def test_exclude_drops_bond(market):
    uni, _, _ = market
    isin = uni[0]["isin"]
    out = _build(market, n=5, exclude=[isin])
    assert isin not in {r["isin"] for r in out["positions"]}
    assert any(x["isin"] == isin and x["reason"] == "excluded" for x in out["rejected"])


# ────────────────────────── фильтры отбора ───────────────────────────

def test_portfolio_filters(market):
    uni, metrics, depth = market
    metrics[uni[0]["isin"]]["has_amort"] = True
    uni[1]["has_call"] = True
    out = _build((uni, metrics, depth), n=18, no_amort=True, no_call=True)
    got = {r["isin"] for r in out["positions"]}
    assert uni[0]["isin"] not in got and uni[1]["isin"] not in got
    reasons = {x["isin"]: x["reason"] for x in out["rejected"]}
    assert reasons[uni[0]["isin"]] == "amort"
    assert reasons[uni[1]["isin"]] == "call"


def test_base_and_adv_filters(market):
    uni, metrics, depth = market
    p = pb.normalize({"amount_rub": AMOUNT, "n": 20, "bases": ["KEYRATE"],
                      "min_adv_rub": 5_000_000})
    adv = {u["isin"]: 10_000_000 for u in uni[:6]}
    out = pb.build(p, uni, metrics, depth, adv=adv, events=[], today=TODAY)
    assert all(r["base"] == "KEYRATE" for r in out["positions"])
    assert all((r["adv_rub"] or 0) >= 5_000_000 for r in out["positions"])
    assert all(r["adv_days"] is not None for r in out["positions"])


def test_bad_price_rows_rejected(market):
    uni, metrics, depth = market
    metrics[uni[0]["isin"]]["price_stale"] = True
    metrics[uni[1]["isin"]]["implausible"] = True
    metrics[uni[2]["isin"]]["ask"] = 0
    depth[uni[3]["isin"]] = {"a": [], "b": []}
    out = _build((uni, metrics, depth), n=16)
    reasons = {x["isin"]: x["reason"] for x in out["rejected"]}
    assert reasons[uni[0]["isin"]] == "stale"
    assert reasons[uni[1]["isin"]] == "implausible"
    assert reasons[uni[2]["isin"]] == "no_ask"
    assert reasons[uni[3]["isin"]] == "no_depth"


def test_emitter_and_rating_share_caps(market):
    uni, metrics, depth = market
    for u in uni[:4]:
        u["emitter_name"] = "Крупный эмитент"
    out = _build((uni, metrics, depth), n=10, max_per_emitter=4,
                 max_emitter_share=0.2, max_rating_share=0.5)
    big = [r for r in out["positions"] if r["emitter"] == "Крупный эмитент"]
    assert len(big) == 2, "кэп по деньгам режет раньше, чем кэп по штукам"
    for share in out["totals"]["by_rating"].values():
        assert share <= 0.5 + 1e-6


# ───────────────────────── агрегаты и календарь ──────────────────────

def test_totals(market):
    out = _build(market, n=10)
    t = out["totals"]
    assert t["count"] == 10 and t["emitters"] == 10
    assert t["dur_w"] == pytest.approx(2.0)
    assert t["pnl_100bp_rub"] == pytest.approx(t["money_rub"] * 2.0 / 100.0, rel=1e-6)
    assert t["rating_avg"] in pb.RATING_SCALE
    assert sum(t["by_base"].values()) == pytest.approx(1.0, abs=1e-3)
    assert t["hhi_emitter"] == pytest.approx(10 * (0.1 ** 2), rel=0.05)


def test_calendar_scales_by_qty(market):
    """Календарь = события канонического билдера × количество бумаг."""
    uni, metrics, depth = market
    p = pb.normalize({"amount_rub": AMOUNT, "n": 1, "isins": [uni[0]["isin"]]})
    events = [
        {"isin": uni[0]["isin"], "date": TODAY + timedelta(days=30),
         "type": "COUPON", "amount_rub": 45.0},
        {"isin": uni[0]["isin"], "date": TODAY + timedelta(days=120),
         "type": "REDEMPTION", "amount_rub": 1000.0},
        {"isin": uni[0]["isin"], "date": TODAY - timedelta(days=5),
         "type": "COUPON", "amount_rub": 45.0},          # прошлое — не берём
        {"isin": "RU000OTHER000", "date": TODAY + timedelta(days=30),
         "type": "COUPON", "amount_rub": 999.0},         # чужая бумага
    ]
    out = pb.build(p, uni, metrics, depth, adv={}, events=events, today=TODAY)
    qty = out["positions"][0]["qty"]
    months = {row["month"]: row for row in out["calendar"]}
    assert len(months) == 2
    first = (TODAY + timedelta(days=30)).strftime("%Y-%m")
    assert months[first]["coupon_rub"] == pytest.approx(45.0 * qty)
    assert out["totals"]["coupon_12m_rub"] == pytest.approx(45.0 * qty)


# ─────────────────────── дубли и дрейф расчёта ───────────────────────

def test_no_selection_logic_copy():
    """Отбор по рейтингу/сроку/суборду живёт ТОЛЬКО в screener_core.
    Копия здесь означала бы, что портфель и сигналы разойдутся в понимании
    одного и того же фильтра."""
    src = inspect.getsource(pb)
    assert "static_candidates" in src
    for marker in ("_SUBORD_RE", "is_subord(", "def selected(", "def years_left"):
        assert marker not in src, f"похоже на копию отбора из скринера: {marker}"


def test_no_pricing_drift(market):
    """Там, где глубины хватает, наша цена и Y-IDX совпадают со скринерными.
    Это единственная страховка от расхождения витрин: код ценообразования
    намеренно продублирован (см. модульный docstring)."""
    uni, metrics, depth = market
    ticket = AMOUNT / 10
    params = {"ratings": [], "emitters": [], "isins": [], "issuer": "all",
              "side": "ask", "spread_min": None, "spread_max": None,
              "min_money_rub": ticket, "money_mode": "book",
              "years_min": None, "years_max": None, "hide_subord": False}
    ref = sc.evaluate_candidates(params, sc.static_candidates(params, uni, TODAY),
                                 metrics, depth)
    ours = {r["isin"]: r for r in
            pb._priced(sc.static_candidates(params, uni, TODAY), metrics, depth, {},
                       pb.normalize({"amount_rub": AMOUNT, "n": 10}), ticket)[0]}
    assert ref, "фикстура должна проходить и скринерный фильтр"
    for m in ref:
        mine = ours[m["isin"]]
        assert mine["price"] == pytest.approx(m["price"], abs=1e-4)
        assert mine["y_idx_bps"] == pytest.approx(m["val_bps"], abs=0.1)


# ─────────────────── находки самопроверки (регрессии) ────────────────

def test_zero_n_is_error():
    """n=0 не должно молча превращаться в умолчание 15."""
    with pytest.raises(FilterError):
        pb.normalize({"n": 0, "amount_rub": AMOUNT})


def test_totals_survive_missing_metrics(market):
    """Ни у одной бумаги нет дюрации и Y-IDX → агрегаты None, а не падение."""
    uni, metrics, depth = market
    for row in metrics.values():
        row["spread_dur"] = None
        row["current_coupon"] = None
        row["yoi_ask"] = None
        row["yoi_slope"] = None
    out = _build((uni, metrics, depth), n=5)
    t = out["totals"]
    assert t["y_idx_w"] is None and t["dur_w"] is None
    assert t["current_coupon_w"] is None
    assert t["pnl_100bp_rub"] == 0.0
    assert len(out["positions"]) == 5


def test_coupon_12m_uses_calendar_horizon(market):
    """Редкие выплаты: срез по 12 СТРОКАМ календаря затянул бы в годовой купон
    платежи второго года. Считаем по календарному горизонту."""
    uni, metrics, depth = market
    p = pb.normalize({"amount_rub": AMOUNT, "n": 1, "isins": [uni[0]["isin"]]})
    events = [{"isin": uni[0]["isin"], "date": TODAY + timedelta(days=180),
               "type": "COUPON", "amount_rub": 50.0},
              {"isin": uni[0]["isin"], "date": TODAY + timedelta(days=560),
               "type": "COUPON", "amount_rub": 50.0}]   # второй год — не в счёт
    out = pb.build(p, uni, metrics, depth, adv={}, events=events, today=TODAY)
    qty = out["positions"][0]["qty"]
    assert len(out["calendar"]) == 2
    assert out["totals"]["coupon_12m_rub"] == pytest.approx(50.0 * qty)


def test_lost_pin_is_reported(market):
    """Прикнопленная бумага без стакана не должна исчезать молча."""
    uni, metrics, depth = market
    isin = uni[3]["isin"]
    depth[isin] = {"a": [], "b": []}
    out = _build((uni, metrics, depth), n=5, pin=[isin])
    assert isin not in {r["isin"] for r in out["positions"]}
    assert any(isin in w and "в набор не попали" in w for w in out["warnings"])
    assert any(x["isin"] == isin and x["reason_txt"] == "пустой стакан"
               for x in out["rejected"])


def test_pin_outside_universe_reported(market):
    out = _build(market, n=5, pin=["RU000NOSUCH0"])
    assert any(x["reason"] == "not_in_universe" for x in out["rejected"])
    assert any("RU000NOSUCH0" in w for w in out["warnings"])


def test_manual_position_not_marked_capped(market):
    """Ручную сумму мы не режем — флаг «урезано стаканом» на ней врал бы."""
    uni, metrics, depth = market
    isin = uni[2]["isin"]
    depth[isin] = _ladder(qty=1000, levels=1)
    out = _build((uni, metrics, depth), n=6, manual={isin: 20_000_000})
    pos = next(r for r in out["positions"] if r["isin"] == isin)
    assert pos["capped"] is False and pos["price_estimated"] is True
