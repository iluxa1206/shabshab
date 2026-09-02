"""Засев утреннего прохода переживает первую сверку версий.

02.09.2026, репетиция переката: прогрев кладёт в движок контексты и потоки, а
первый такт вызывает _check_version с пустой _memo_version и сносит всё «на
всякий случай». Передача работы движку не срабатывала ни разу — лог «движок
получил ctx=611» печатался за секунды до сноса, и рынок догревался заново по
десять бумаг за такт. На медленной выкачке расписаний движок просыпался ДО
конца прогрева, и в логе оставалось ctx=2."""
from datetime import date

import services.universe_stream as us


def _reset():
    us._eval_ctx.clear()
    us._flow_cache.clear()
    us._level_memo.clear()
    us._memo_version = ()
    us._seeded_version = None


def _seed(n=3):
    for i in range(n):
        us._eval_ctx[f"I{i}"] = {"isin": f"I{i}", "base": "KEYRATE"}
        us._flow_cache[f"I{i}"] = {("main", 0): ([], [])}


def test_seeded_context_survives_first_version_check(monkeypatch):
    _reset()
    _seed()
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_done({}, date(2026, 9, 2))
    us._check_version(("2026-09-02", "fp-1"))
    assert len(us._eval_ctx) == 3 and len(us._flow_cache) == 3


def test_unseeded_cache_is_still_dropped():
    """Без отметки происхождения кэш по-прежнему сносится: считать по ссылке на
    кривую, которой уже нет, хуже, чем собрать заново."""
    _reset()
    _seed()
    us._check_version(("2026-09-02", "fp-1"))
    assert not us._eval_ctx and not us._flow_cache


def test_other_version_drops_the_seed(monkeypatch):
    """Кривые пересобрались между засевом и тактом — засев недействителен."""
    _reset()
    _seed()
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_done({}, date(2026, 9, 2))
    us._check_version(("2026-09-02", "fp-2"))
    assert not us._eval_ctx and not us._flow_cache


def test_new_day_drops_the_seed(monkeypatch):
    _reset()
    _seed()
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_done({}, date(2026, 9, 2))
    us._check_version(("2026-09-03", "fp-1"))
    assert not us._eval_ctx


def test_seed_done_returns_real_count(monkeypatch):
    """Число в логе должно быть тем, что реально осталось у движка."""
    _reset()
    _seed(5)
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    assert us.seed_done({}, date(2026, 9, 2)) == 5


def test_level_memo_is_always_dropped(monkeypatch):
    """Кэш уровней — это ЦЕНЫ на конкретной кривой, он недействителен всегда."""
    _reset()
    _seed()
    us._level_memo[("I0", 100.0)] = {"yoi": 1}
    monkeypatch.setattr(us, "_curves_fp", lambda mc: "fp-1")
    us.seed_done({}, date(2026, 9, 2))
    us._check_version(("2026-09-02", "fp-1"))
    assert not us._level_memo
