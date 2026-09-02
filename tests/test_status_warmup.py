"""Блок прогрева на вкладке СТАТУС.

Отвечает на «почему у бумаги прочерк»: идёт догрев или всё посчитано."""
from api.routes.status import _warmup_block, router


def test_route_is_bound_to_the_handler():
    """Декоратор должен висеть на get_status. Вставленная между декоратором и
    роутом функция забирает маршрут себе, и /api/status начинает требовать её
    аргументы как query-параметры — 422 на каждый запрос (02.09.2026)."""
    r = next(x for x in router.routes if getattr(x, "path", None) == "")
    assert r.endpoint.__name__ == "get_status"


def _us(**kw):
    base = {"ctx": 500, "flows": 480, "grids": 300, "grids_cold": 5, "memo": 900,
            "hits": 10, "misses": 20, "dirty": 3, "sides_queue": 40,
            "flow_items": 120_000, "blank_sides": 60, "ctx_no_accrued": 2,
            "rate": {"rows_per_min": 100, "row_ms": 60,
                     "sides_per_min": 300, "side_ms": 15}}
    return {**base, **kw}


def _um(n_bid=400, n_ask=350, n_yoi=500):
    um = {}
    for i in range(600):
        um[f"I{i}"] = {"yoi": 1 if i < n_yoi else None,
                       "yoi_bid": 1 if i < n_bid else None,
                       "yoi_ask": 1 if i < n_ask else None}
    return um


def test_coverage_counts_each_side_separately():
    """Стороны считает отдельная очередь: строка может быть готова, а bid/ask
    ещё нет — одной цифрой это не показать."""
    w = _warmup_block(_us(), _um(), 600)
    by = {r["key"]: r for r in w["coverage"]}
    assert by["Спред бида"]["n"] == 400 and by["Спред оффера"]["n"] == 350
    assert by["Спред по сделке"]["pct"] == 83
    assert by["Контексты расчёта"]["n"] == 500


def test_eta_from_current_rate():
    w = _warmup_block(_us(), _um(), 600)
    assert w["queues"]["blank_sides"] == 60
    assert w["queues"]["eta_sec"] == 12          # 60 сторон / 300 в минуту
def test_no_rate_yet_gives_no_eta():
    """Первая минута после рестарта: сводки ещё не было, врать оценкой нельзя."""
    w = _warmup_block(_us(rate={}), _um(), 600)
    assert w["queues"]["eta_sec"] is None and w["rate"] is None


def test_empty_universe_does_not_divide_by_zero():
    w = _warmup_block(_us(), {}, 0)
    assert all(r["pct"] == 0 for r in w["coverage"])
