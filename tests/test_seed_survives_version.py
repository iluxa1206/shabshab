"""Засев утреннего прохода переживает первую сверку версий.

02.09.2026, репетиция переката: прогрев кладёт в движок контексты и потоки, а
первый такт вызывает _check_version с пустой _memo_version и сносит всё «на
всякий случай». Передача работы движку не срабатывала ни разу — лог «движок
получил ctx=611» печатался за секунды до сноса, и рынок догревался заново по
десять бумаг за такт. На медленной выкачке расписаний движок просыпался ДО
конца прогрева, и в логе оставалось ctx=2."""
from datetime import date

import pytest

import services.universe_stream as us


@pytest.fixture(autouse=True)
def _clean_seed():
    """Отметка засева — глобальная: оставленная после теста, она меняет
    поведение _check_version в чужих тестах (падал
    test_curves_rebuild_keeps_eval_ctx)."""
    yield
    us._seeded_version = None
    us._memo_version = ()


def _reset():
    us._eval_ctx.clear()
    us._flow_cache.clear()
    us._level_memo.clear()
    us._memo_version = ()
    us._seeded_version = None


def _ver(day, fp):
    """Версия кэшей — тройка (календарный день, день расписаний, кривые):
    купоны перекатываются в 09:00, а calc_date в полночь."""
    return (day, us._trading_day(), fp)


def _seed(n=3):
    for i in range(n):
        us._eval_ctx[f"I{i}"] = {"isin": f"I{i}", "base": "KEYRATE"}
        us._flow_cache[f"I{i}"] = {("main", 0): ([], [])}


def test_seeded_context_survives_first_version_check(monkeypatch):
    _reset()
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    # ПОРЯДОК КАК В ПРОДЕ (api/main.daily_prewarm): день объявляется ДО прохода,
    # и seed_begin сносит потоки прошлого дня — контексты и потоки кладёт уже
    # сам проход, после отметки.
    us.seed_begin({}, date(2026, 9, 2))
    _seed()
    us._check_version(_ver("2026-09-02", "fp-1"))
    assert len(us._eval_ctx) == 3 and len(us._flow_cache) == 3


def test_unseeded_cache_is_still_dropped():
    """Без отметки происхождения кэш по-прежнему сносится: считать по ссылке на
    кривую, которой уже нет, хуже, чем собрать заново."""
    _reset()
    _seed()
    us._check_version(_ver("2026-09-02", "fp-1"))
    assert not us._eval_ctx and not us._flow_cache


def test_other_version_drops_the_seed(monkeypatch):
    """Кривые пересобрались между засевом и тактом — засев недействителен."""
    _reset()
    _seed()
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_begin({}, date(2026, 9, 2))
    _seed()
    us._check_version(_ver("2026-09-02", "fp-2"))
    assert not us._eval_ctx and not us._flow_cache


def test_new_day_drops_the_seed(monkeypatch):
    _reset()
    _seed()
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_begin({}, date(2026, 9, 2))
    _seed()
    us._check_version(_ver("2026-09-03", "fp-1"))
    assert not us._eval_ctx


def test_seed_count_is_what_engine_really_has(monkeypatch):
    """Число в логе должно быть тем, что реально осталось у движка."""
    _reset()
    _seed(5)
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_begin({}, date(2026, 9, 2))
    assert us.seed_count() == 5


def test_version_marked_before_the_pass_protects_the_middle(monkeypatch):
    """ГЛАВНОЕ: движок просыпается ПОСРЕДИ прохода. Отметка, поставленная в
    конце, спасала только хвост — репетиция переката 02.09 дала 151 контекст из
    611. Версия объявляется до засева, поэтому такт в середине ничего не сносит."""
    _reset()
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_begin({}, date(2026, 9, 2))          # проход только начался
    _seed(2)                                      # успели две бумаги
    us._check_version(("2026-09-02", "fp-1"))     # ← движок проснулся здесь
    _seed(5)                                      # проход продолжается
    assert len(us._eval_ctx) == 5 and len(us._flow_cache) == 5


def test_level_memo_is_always_dropped(monkeypatch):
    """Кэш уровней — это ЦЕНЫ на конкретной кривой, он недействителен всегда."""
    _reset()
    _seed()
    us._level_memo[("I0", 100.0)] = {"yoi": 1}
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_begin({}, date(2026, 9, 2))
    us._check_version(("2026-09-02", "fp-1"))
    assert not us._level_memo


def test_curves_rebuilt_mid_pass_rebinds_instead_of_dropping(monkeypatch):
    """Кривые пересобрались ПОСРЕДИ прогрева. Контекст кривой не принадлежит —
    от неё в нём одна ссылка, поэтому его перепривязывают, а не сносят. На
    старте «тот же день» определить было нечем (_memo_version пуста), и половина
    засева терялась: репетиция 02.09 — посчитано 611, у движка 315."""
    _reset()
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_begin({}, date(2026, 9, 2))
    _seed(4)
    class _C:  # ссылки на новые кривые
        pass
    ru, kr = _C(), _C()
    us._check_version(("2026-09-02", "fp-2"),
                      {"ruonia_curve": ru, "keyrate_curve": kr,
                       "calc_date": date(2026, 9, 2)})
    assert len(us._eval_ctx) == 4                     # контексты живы
    assert all(v["curve"] is kr for v in us._eval_ctx.values())   # и на новой кривой
    assert not us._flow_cache                         # потоки строятся НА кривой — снесены


def test_new_day_still_drops_everything(monkeypatch):
    """Смена дня — другие НКД, графики и потоки: перепривязка не спасает."""
    _reset()
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_begin({}, date(2026, 9, 2))
    _seed(4)
    us._check_version(("2026-09-03", "fp-1"),
                      {"ruonia_curve": object(), "keyrate_curve": object(),
                       "calc_date": date(2026, 9, 3)})
    assert not us._eval_ctx and not us._flow_cache


def test_early_tick_without_curves_keeps_the_seed(monkeypatch):
    """Ранний такт после старта кривых ещё не видит. Снос защищает от ссылки на
    кривую, которой нет в кэше, — но засев собран минуту назад на живых кривых.
    Репетиция 02.09: посчитано 611, у движка оставалось 256."""
    _reset()
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_begin({}, date(2026, 9, 2))
    _seed(4)
    us._check_version(("2026-09-02", "fp-2"), {"calc_date": date(2026, 9, 2)})
    assert len(us._eval_ctx) == 4


def test_without_seed_missing_curves_still_drop_the_cache():
    """Без засева происхождение контекстов неизвестно — снос остаётся."""
    _reset()
    _seed(4)
    assert us._rebind_curves(None) == 4
    assert not us._eval_ctx
