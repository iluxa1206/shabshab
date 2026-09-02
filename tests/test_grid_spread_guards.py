"""Защиты вокруг «спред из сетки» — по итогам ревью коммита b067448.

Сетка даёт спред на любой цене без солвера, но у этой дешевизны есть границы:
её числа округлены, она переживает пересборку контекста, а котировочный пуш
может быть мёртвым. Каждый тест — про одну такую границу."""
from datetime import date

import pytest

import services.universe_stream as us


@pytest.fixture(autouse=True)
def _clean():
    us._last_quote.clear()
    us._yoi_grid.clear()
    us._eval_ctx.clear()
    yield
    us._last_quote.clear()
    us._yoi_grid.clear()
    us._eval_ctx.clear()


def _grid(isin, nodes_vals):
    us._yoi_grid[isin] = (us._yoi_cache_epoch, sorted(nodes_vals), dict(nodes_vals))


def _quote(isin, *, bid=None, ask=None, age=0.0):
    import time
    us._last_quote[isin] = {"bid": bid, "ask": ask, "_ts": time.time() - age}


# ── свежесть пуша ────────────────────────────────────────────────────────────

def test_live_side_uses_fresh_push():
    _grid("X", {100.0: 200, 100.5: 210})
    _quote("X", ask=100.2)
    assert us.live_sides("X")["ask"][0] == 100.2


def test_dead_stream_does_not_override_engine():
    """Сокет умер: _last_quote больше не обновляется. Без порога его последняя
    котировка навсегда выигрывала бы у снапшота ISS — замороженный верх стакана
    выдавался бы за текущий."""
    _grid("X", {100.0: 200, 100.5: 210})
    _quote("X", ask=100.2, age=us._LIVE_SIDE_MAX_AGE_SEC + 5)
    assert us.live_sides("X") == {}


def test_no_push_at_all_is_empty():
    _grid("X", {100.0: 200, 100.5: 210})
    assert us.live_sides("X") == {}


# ── сетка только там, где точного числа нет ──────────────────────────────────

def test_engine_number_wins_at_the_same_price():
    """Узлы сетки хранят числа, округлённые до целых б.п., и интерполяция между
    ними расходится с точным расчётом. Там, где движок считал по ТОЙ ЖЕ цене,
    его число точнее."""
    _grid("X", {100.0: 200, 100.5: 210})
    _quote("X", ask=100.2)
    assert "ask" not in us.live_sides("X", {"ask": 100.2, "yoi_ask": 204})


def test_grid_used_when_price_moved_away():
    _grid("X", {100.0: 200, 100.5: 210})
    _quote("X", ask=100.2)
    got = us.live_sides("X", {"ask": 100.05, "yoi_ask": 201})
    assert got["ask"][0] == 100.2


def test_zero_side_is_not_a_price():
    _grid("X", {100.0: 200, 100.5: 210})
    _quote("X", ask=0)
    assert us.live_sides("X") == {}


# ── разрыв правила горизонта ─────────────────────────────────────────────────

def test_interpolation_skips_horizon_break():
    """На цене перелома «держу до погашения ↔ сдам на оферту» соседние узлы
    принадлежат разным горизонтам, и прямая между ними даёт число, которого нет
    ни у одного из них."""
    _grid("X", {99.0: 300, 99.5: 305, 100.0: 310, 100.5: 900, 101.0: 905})
    assert us.yoi_at("X", 100.25) is None          # интервал перелома
    assert us.yoi_at("X", 99.25) == 302            # ровный участок считается


def test_smooth_grid_interpolates():
    _grid("X", {100.0: 200, 100.5: 210, 101.0: 220})
    assert us.yoi_at("X", 100.25) == 205


def test_price_outside_grid_is_none():
    _grid("X", {100.0: 200, 100.5: 210})
    assert us.yoi_at("X", 105.0) is None


# ── сетка не переживает пересборку контекста ─────────────────────────────────

def test_ctx_rebuild_drops_the_grid(monkeypatch):
    """Узлы считались по прежнему графику платежей и прежнему НКД — после
    пересборки контекста они отдавали бы числа, которых точный расчёт уже не
    даёт."""
    _grid("X", {100.0: 200, 100.5: 210})

    class _Ref:
        pass
    ctx = {"ruonia_curve": object(), "keyrate_curve": object(),
           "calc_date": date(2026, 9, 2), "full_by": {"X": {}}}
    us._store_eval_ctx("X", {"base_rate_type": "KEYRATE"}, _Ref(), ctx, {"accrued": 10.0})
    assert "X" not in us._yoi_grid


def test_grid_survives_untouched_context():
    """Обратная сторона: пока контекст не пересобирали, сетка живёт — иначе
    каждый такт платил бы за неё заново (150 мс против 26)."""
    _grid("X", {100.0: 200, 100.5: 210})
    _quote("X", ask=100.2)
    assert us.live_sides("X")["ask"] == (100.2, 204)
