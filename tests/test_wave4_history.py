"""Регресс-щит волны 4 аудита 2026-08-26: as-of/историю.

Три дефекта: оферты брались на правый край окна (look-ahead), номинал дня
наследовался от предыдущего обработанного дня (недетерминизм), горизонт левого
досчёта выводился из своей последней строки (шов на линии)."""
import importlib
from datetime import date, timedelta

import pytest


@pytest.fixture
def bd(tmp_path, monkeypatch):
    """Временная portfolio.db — тот же приём, что в test_honest_backfill_coverage."""
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.spread_history as sh
    import services.backdate as backdate
    importlib.reload(sh)
    importlib.reload(backdate)
    yield backdate, sh, pdb
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)
    importlib.reload(sh)
    importlib.reload(backdate)


# ─── оферты на день точки, а не на край окна ────────────────────────────────

def test_call_offers_differ_by_asof_date():
    """Синтетическая call-запись corpbonds — это «ближайший колл ПОСЛЕ даты».

    ctx собирается на d_till, поэтому на прошлой точке серии из него приезжал
    колл из БУДУЩЕГО: у RU000A103QN7 на 2026-02-27 ближайшим был 2026-03-24
    (0,07 года), а не 2031-03-18 (5 лет) — график расходился с /reprice."""
    from services.market_data import (call_offers_asof, _call_dates_cached,
                                      CALL_OFFER_SOURCE)
    isin = "RU000A103QN7"
    calls = sorted(_call_dates_cached().get(isin) or [])
    if not calls:
        pytest.skip("нет колл-дат в кэше corpbonds")
    late = [o["date"] for o in (call_offers_asof(isin, None, date(2026, 8, 25)) or [])
            if o.get("source") == CALL_OFFER_SOURCE]
    early = [o["date"] for o in (call_offers_asof(isin, None, date(2026, 2, 27)) or [])
             if o.get("source") == CALL_OFFER_SOURCE]
    assert late and early and late != early


def test_offers_memo_keyed_by_call_not_day():
    """Мемо по ДНЮ дало бы 457 КБ на бумагу × _ASOF_MEMO_MAX=120 = +55 МБ при
    контейнере 768 МиБ. Ключ — сам ближайший колл: между двумя колл-датами
    список не меняется, записей единицы."""
    import inspect
    from services import backdate
    src = inspect.getsource(backdate.asof_bar_metrics)
    assert "_offers_memo[nearest]" in src, "мемо оферт не по ближайшему коллу"
    assert "_offers_at(day_iso)" in src, "оферты не пересобираются на день точки"


# ─── номинал дня: детерминизм ───────────────────────────────────────────────

def test_face_of_day_from_amort_schedule():
    """Строка MOEX без FACEVALUE не должна оставлять номинал от ПРЕДЫДУЩЕГО
    обработанного дня: ref — общий мутируемый объект, а порядок обхода зависит
    от режима (reversed при стриминге), плюс fn живёт в _asof_memo между
    запросами. Откат — на график амортизаций, как в load_backdate_ctx."""
    from services.bonds import amort_remaining_face
    amorts = [{"date": "2026-03-15", "value": 500.0},
              {"date": "2026-09-15", "value": 500.0}]
    # остаток = Σ БУДУЩИХ траншей (вкл. финальное погашение)
    assert amort_remaining_face(amorts, date(2026, 1, 1), 1000.0) == pytest.approx(1000.0)
    assert amort_remaining_face(amorts, date(2026, 6, 1), 1000.0) == pytest.approx(500.0)
    # график исчерпан → None, вызывающий обязан оставить прежний номинал
    assert amort_remaining_face(amorts, date(2026, 12, 1), 1000.0) is None
    # исчерпанный график → None, и вызывающий обязан оставить прежний номинал


def test_face_of_day_is_order_independent():
    """ГЛАВНОЕ СВОЙСТВО ВОЛНЫ: одна дата — одно число, независимо от порядка
    обхода. Цикл идёт reversed при стриминге и вперёд иначе, а fn живёт в
    _asof_memo между запросами — раньше номинал протекал от предыдущего дня."""
    from services.bonds import amort_remaining_face
    amorts = [{"date": "2026-03-15", "value": 500.0},
              {"date": "2026-09-15", "value": 500.0}]

    def face_of(day, base=1000.0):        # ровно формула из backdate
        return float(amort_remaining_face(amorts, day, base) or base)

    days = [date(2026, m, 1) for m in range(1, 13)]
    fwd = [face_of(d) for d in days]
    bwd = list(reversed([face_of(d) for d in reversed(days)]))
    assert fwd == bwd
    assert face_of(date(2026, 6, 1)) == 500.0    # мартовский транш уже прошёл


def test_face_assigned_before_accrued():
    """НКД считается ОТ НОМИНАЛА, поэтому блок номинала обязан идти выше:
    стояло наоборот, и НКД считался по номиналу предыдущего дня."""
    import inspect
    from services import backdate
    src = inspect.getsource(backdate.asof_bar_metrics)
    i_face = src.index("ref.face_value = float(_face)")
    i_acc = src.index("accint = row.get(\"accint\")")
    assert i_face < i_acc, "номинал присваивается ПОСЛЕ расчёта НКД"


# ─── шов горизонта при расширении окна ──────────────────────────────────────

def test_left_chunk_gets_horizon_of_full_window(bd, monkeypatch):
    """Левый досчёт (till=earliest-1) выводил hz_key из СВОЕЙ последней строки
    и получал другой горизонт — линия склеивалась из двух."""
    import asyncio
    from datetime import timedelta
    backdate, _sh, pdb = bd
    isin = "RU000TESTHZ01"
    seen = []

    async def fake_series(isin_, days=180, board=None, price_overrides=None,
                          till=None, on_chunk=None, hz_keys=None):
        seen.append(hz_keys)
        return {"isin": isin_, "points": [], "warnings": []}

    monkeypatch.setattr(backdate, "honest_spread_series", fake_series)
    monkeypatch.setattr(backdate, "_backfill_done", {})
    # правая часть окна уже посчитана и лежит с горизонтом put
    with pdb._connect() as c:
        for i in range(0, 100, 3):
            c.execute(
                "INSERT OR REPLACE INTO spread_daily(isin,date,kind,price_pct,"
                "y_idx,src,engine_ver,horizon,alt_horizon) "
                "VALUES(?,?,'floater',100.0,120.0,'honest',?,'put','maturity')",
                (isin, (date.today() - timedelta(days=i)).isoformat(),
                 backdate.HONEST_ENGINE_VERSION))
    asyncio.run(backdate.ensure_honest_backfill(isin, days=365))
    assert seen == [("put", "maturity")], f"левый досчёт вывел горизонт сам: {seen}"


def test_full_run_probes_horizon_itself(bd, monkeypatch):
    """Полный прогон (till=None) обязан считать горизонт сам, а не брать чужой."""
    import asyncio
    backdate, _sh, _pdb = bd
    seen = []

    async def fake_series(isin_, days=180, board=None, price_overrides=None,
                          till=None, on_chunk=None, hz_keys=None):
        seen.append(hz_keys)
        return {"isin": isin_, "points": [], "warnings": []}

    monkeypatch.setattr(backdate, "honest_spread_series", fake_series)
    monkeypatch.setattr(backdate, "_backfill_done", {})
    asyncio.run(backdate.ensure_honest_backfill("RU000TESTHZ02", days=120))
    assert seen == [None], f"полный прогон взял чужой горизонт: {seen}"


# ─── дыра чтения: bar_daily без фильтра горизонта ───────────────────────────

def test_one_horizon_keeps_price_when_horizon_differs():
    """Чужой горизонт гасит СПРЕД, но не выбрасывает точку: по этим же точкам
    во вкладке СРАВНЕНИЕ строятся линии «цена» и «изменение», и выброс стирал
    ценовую историю левее переключения горизонта."""
    from api.routes.history import _one_horizon
    rows = [
        {"isin": "X", "date": "2026-01-10", "y_idx": 200.0, "horizon": "maturity",
         "alt_horizon": None, "y_idx_alt": None, "price_pct": 99.0},
        {"isin": "X", "date": "2026-02-10", "y_idx": 150.0, "horizon": "put",
         "alt_horizon": "maturity", "y_idx_alt": 205.0, "price_pct": 100.0},
    ]
    out = _one_horizon(rows, -5000.0, 5000.0)
    assert len(out) == 2, "точка с чужим горизонтом выброшена вместе с ценой"
    # последний известный горизонт — put; у первой строки его нет и alt нет
    assert out[0]["y_idx"] is None and out[0]["price"] == 99.0
    assert out[1]["y_idx"] == 150.0


def test_one_horizon_translates_via_alt():
    """Есть второй горизонт — берём его значение, а не гасим точку."""
    from api.routes.history import _one_horizon
    rows = [
        {"isin": "X", "date": "2026-01-10", "y_idx": 200.0, "horizon": "maturity",
         "alt_horizon": "put", "y_idx_alt": 145.0, "price_pct": 99.0},
        {"isin": "X", "date": "2026-02-10", "y_idx": 150.0, "horizon": "put",
         "alt_horizon": "maturity", "y_idx_alt": 205.0, "price_pct": 100.0},
    ]
    out = _one_horizon(rows, -5000.0, 5000.0)
    assert [r["y_idx"] for r in out] == [145.0, 150.0]
