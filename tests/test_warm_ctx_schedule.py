"""Контекст расчёта, который собирает warm_ctx, обязан быть таким же, как у
_crunch: с поправленным номиналом и с настоящим расписанием.

Аудит конвейера 03.09:
  - находка 3 (high): warm_ctx клал в _eval_ctx сырой ref из isins_cache, без
    reconcile_face/amort_remaining_face. На этом контексте считаются спреды
    сторон, вся сетка цен и спред набора на объём — у амортизируемой бумаги от
    завышенного номинала, тогда как yoi в той же строке считался от верного.
  - находка 6 (high): контекст, собранный на ПУСТОМ расписании (штатный ответ
    при осечке ISS — пустое там не кэшируется), жил до следующего переката:
    перестраивать его было некому.
"""
from datetime import date

import pytest

from services import universe_stream as us
from core.valuation import BondRefData


ISIN = "RU000A_WARM01"
CD = date(2026, 9, 3)


@pytest.fixture(autouse=True)
def clean():
    us._eval_ctx.clear()
    us._ctx_no_sched.clear()
    yield
    us._eval_ctx.clear()
    us._ctx_no_sched.clear()


def _ref(face=1000.0):
    return BondRefData(isin=ISIN, base="KEYRATE", spread_issue_bps=150,
                       face_value=face, accrued_rub=0.0,
                       maturity_date=date(2030, 1, 12),
                       first_coupon_date=date(2024, 4, 12), coupons_per_year=4,
                       issue_date=date(2024, 1, 12), coupon_period_days=91)


def _full(with_sched=True, amorts=None):
    if not with_sched:
        return {"coupons": [], "amorts": [], "offers": []}
    return {"coupons": [{"start": "2026-07-12", "end": "2026-10-12", "value": 30.0}],
            "amorts": amorts, "offers": []}


def _ctx(full):
    return {"uni_by": {ISIN: {"isin": ISIN, "base_rate_type": "KEYRATE"}},
            "cache": {}, "secs": {}, "board": {}, "ruonia_curve": None,
            "keyrate_curve": None, "calc_date": CD, "full_by": {ISIN: full}}


def test_warm_ctx_fixes_stale_face(monkeypatch):
    """Номинал из isins_cache стейлится у амортизируемых бумаг: остаток по
    графику авторитетнее (БалтЛизП10 — кэш 1000 ₽ при остатке 900 ₽)."""
    # график несёт ВСЕ транши, включая выплаченный: Σ = исходные 1000 ₽ (иначе
    # amort_remaining_face решит, что график огрызок, и доверится бирже),
    # Σ будущих = 900 ₽ — это и есть остаток, от которого котируется цена
    amorts = [{"date": "2026-01-12", "value": 100.0},
              {"date": "2027-01-12", "value": 300.0},
              {"date": "2030-01-12", "value": 600.0}]
    ctx = _ctx(_full(amorts=amorts))
    monkeypatch.setattr("services.universe.build_universe_ref",
                        lambda u, isin, cache, secs: _ref(1000.0))

    assert us.warm_ctx([ISIN], ctx) == 1
    assert us._eval_ctx[ISIN]["ref_obj"].face_value == pytest.approx(900.0)


def test_empty_schedule_ctx_is_marked_and_rebuilt(monkeypatch):
    """Пустое расписание — осечка источника, а не факт: контекст помечается и
    встаёт на пересборку, а не живёт до конца дня."""
    monkeypatch.setattr("services.universe.build_universe_ref",
                        lambda u, isin, cache, secs: _ref())

    assert us.warm_ctx([ISIN], _ctx(_full(with_sched=False))) == 1
    assert ISIN in us._ctx_no_sched
    assert us._eval_ctx[ISIN]["periods"] == []
    assert us.stats()["ctx_no_sched"] == 1

    # пауза ещё не вышла — источник не долбим
    assert us._ctx_needs_rebuild(ISIN) is False
    assert us.warm_ctx([ISIN], _ctx(_full())) == 0

    # пауза вышла: ISS ответил, контекст пересобран настоящим расписанием
    us._ctx_no_sched[ISIN] -= us._CTX_SCHED_RETRY_SEC + 1
    assert us._ctx_needs_rebuild(ISIN) is True
    assert us.warm_ctx([ISIN], _ctx(_full())) == 1
    assert us._eval_ctx[ISIN]["periods"]
    assert ISIN not in us._ctx_no_sched
    assert us.stats()["ctx_no_sched"] == 0


def test_healthy_ctx_is_not_rebuilt(monkeypatch):
    """Здоровый контекст догрев не трогает — иначе рынок пересобирался бы каждым
    тактом."""
    monkeypatch.setattr("services.universe.build_universe_ref",
                        lambda u, isin, cache, secs: _ref())
    ctx = _ctx(_full())
    assert us.warm_ctx([ISIN], ctx) == 1
    assert us._ctx_needs_rebuild(ISIN) is False
    assert us.warm_ctx([ISIN], ctx) == 0


def test_depth_push_orders_recompute(monkeypatch):
    """Аудит 03.09, находка 5: смена ГЛУБИНЫ книги не заказывала пересчёт —
    цена набора на тикет собирается по лестнице, а очередь будили только цена
    сделки и цена верха книги. Срезали объём при неизменном лучшем оффере — и в
    строке жила цена набора, которой в стакане уже нет."""
    # register_visible принимает только валидный 12-символьный isin
    isin = "RU000AWARM01"
    us._sides_dirty.clear()
    us._depth_queued.clear()
    us._visible.clear()
    us._vol_sizes.clear()
    try:
        # фильтра по объёму нет — цену набора никто не считает, заказ не нужен
        us.register_visible([isin])
        us._on_depth(isin)
        assert isin not in us._sides_dirty

        us._vol_sizes[5_000_000.0] = us.time.monotonic()
        # бумагу никто не смотрит — тоже не заказываем
        us._visible.clear()
        us._on_depth(isin)
        assert isin not in us._sides_dirty

        us.register_visible([isin])
        us._on_depth(isin)
        assert us._sides_dirty.get(isin) == us._SIDES_PRIO_WAVE

        # дебаунс: тысячи пушей в минуту не должны заливать очередь
        us._sides_dirty.clear()
        us._on_depth(isin)
        assert isin not in us._sides_dirty

        us._depth_queued[isin] -= us._DEPTH_REQUEUE_SEC + 1
        us._on_depth(isin)
        assert isin in us._sides_dirty
    finally:
        us._sides_dirty.clear()
        us._depth_queued.clear()
        us._visible.clear()
        us._vol_sizes.clear()


def test_grid_survives_price_change(monkeypatch):
    """Аудит 03.09, находка 13: _store_eval_ctx сносил сетку цен безусловно, а
    зовётся она на КАЖДЫЙ промах кэша уровней — то есть на каждую новую цену
    сделки, от которой узлы сетки не зависят. Прод: 593 сетки на месте и 25–59
    перестроек в минуту — 4–9 с в минуту единственного счётного потока на снос
    и восстановление того же самого."""
    monkeypatch.setattr("services.universe.build_universe_ref",
                        lambda u, isin, cache, secs: _ref())
    ctx = _ctx(_full())
    us._yoi_grid.clear()
    try:
        assert us.warm_ctx([ISIN], ctx) == 1
        us._yoi_grid[ISIN] = (us._yoi_cache_epoch, [], [])

        # тот же контекст пересобран (промах уровня на новой цене) — сетка жива
        us._store_eval_ctx(ISIN, ctx["uni_by"][ISIN], _ref(), ctx, {})
        assert ISIN in us._yoi_grid

        # НКД приехал биржевой — узлы посчитаны по другому начислению, сносим
        us._store_eval_ctx(ISIN, ctx["uni_by"][ISIN], _ref(), ctx, {"accrued": 12.3})
        assert ISIN not in us._yoi_grid

        # сменился номинал — тоже
        us._yoi_grid[ISIN] = (us._yoi_cache_epoch, [], [])
        us._store_eval_ctx(ISIN, ctx["uni_by"][ISIN], _ref(900.0), ctx, {"accrued": 12.3})
        assert ISIN not in us._yoi_grid
    finally:
        us._yoi_grid.clear()


def test_recrunch_sides_respects_deadline(monkeypatch):
    """Аудит 03.09, находка 12: очередь сторон была единственным неразрезанным
    заходом в тяжёлый пул. Пачку выбирает среднее время бумаги, но среднее —
    прошлое: 100 дорогих бумаг это 12 секунд, пока event loop не просыпается."""
    from services.market_data import market_cache
    isins = [f"RU000A10000{i}" for i in range(5)]
    prev_um = market_cache.get("universe_metrics")
    market_cache["universe_metrics"] = {i: {"last": 100.0} for i in isins}
    us._eval_ctx.update({i: {"ref_obj": None} for i in isins})
    monkeypatch.setattr(us, "_fill_side_metrics",
                        lambda row, isin, sides, snap, book=None: None)
    monkeypatch.setattr(us, "_sides_from", lambda q, snap: {})
    try:
        pend = []
        # дедлайн уже позади: первая бумага считается всегда (иначе очередь
        # встала бы намертво), остальные уезжают в pending
        out = us.recrunch_sides(isins, {}, us.time.monotonic() - 1, pend)
        assert len(out) == 1
        assert pend == isins[1:]

        pend = []
        out = us.recrunch_sides(isins, {}, us.time.monotonic() + 60, pend)
        assert len(out) == 5 and pend == []
    finally:
        for i in isins:
            us._eval_ctx.pop(i, None)
        if prev_um is None:
            market_cache.pop("universe_metrics", None)
        else:
            market_cache["universe_metrics"] = prev_um


def test_sides_tail_keeps_its_priority():
    """Хвост очереди сторон, не влезший в дедлайн, возвращается СВОИМ
    приоритетом. Возврат общей меткой волны понижал живое движение книги
    (_SIDES_PRIO_LIVE=0) до 1.0, и бумага, по которой только что двигали заявку,
    уезжала в конец очереди."""
    us._sides_dirty.clear()
    try:
        us._queue_sides("RU000A100002", us._SIDES_PRIO_WAVE)
        us._queue_sides("RU000A100001", us._SIDES_PRIO_LIVE)

        take, prio = us._take_sides_batch()
        assert take == ["RU000A100001", "RU000A100002"]   # живое событие впереди
        assert not us._sides_dirty                        # пачка снята целиком

        # заход не успел ни одной — возвращаем, как это делает такт движка
        for i in take:
            us._queue_sides(i, prio.get(i, us._SIDES_PRIO_WAVE))
        assert us._sides_dirty["RU000A100001"] == us._SIDES_PRIO_LIVE
        assert us._sides_dirty["RU000A100002"] == us._SIDES_PRIO_WAVE

        # и на следующем такте живая бумага снова первая, а не в конце
        take2, _ = us._take_sides_batch()
        assert take2[0] == "RU000A100001"
    finally:
        us._sides_dirty.clear()


def test_level_memo_survives_its_own_context_store(monkeypatch):
    """Регресс самопроверки: снятие кэшей цен при смене контекста сносило
    строку, которую _crunch только что положил в _level_memo, — кэш уровней
    переставал попадать вовсе."""
    from services.market_data import market_cache
    isin = "RU000A100001"
    us._level_memo.clear()
    us._eval_ctx.clear()
    calls = []

    def enrich(u, ref, full, *, last, **kw):
        calls.append(last)
        return {"yoi": 100, "last": last}

    ctx = {"uni_by": {isin: {"isin": isin, "base_rate_type": "KEYRATE"}},
           "cache": {}, "secs": {}, "board": {}, "fixed_by": {},
           "ruonia_curve": None, "keyrate_curve": None, "exp_ks": None,
           "exp_ru": None, "g_curve": None, "calc_date": CD, "full_by": {}}
    monkeypatch.setattr(us, "build_ref", lambda u, i, c, s: _ref(), raising=False)
    monkeypatch.setattr(us, "_fill_side_metrics",
                        lambda row, isin, sides, snap, book=None: None)
    monkeypatch.setattr(us, "_sides_from", lambda q, snap: {})
    try:
        q = [(isin, {"last_price": 100.0})]
        us._crunch(q, ctx, enrich=enrich)
        assert (isin, 100.0) in us._level_memo, "строка не попала в кэш уровней"

        # вторая волна по той же цене обязана прийти из кэша, а не считаться снова
        us._crunch(q, ctx, enrich=enrich)
        assert calls == [100.0], f"enrich позван повторно: {calls}"
    finally:
        us._level_memo.clear()
        us._eval_ctx.clear()
        market_cache.pop("universe_metrics", None)
