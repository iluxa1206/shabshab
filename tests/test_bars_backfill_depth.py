"""Фоновый пересчёт баров не должен ходить по кругу.

Баг (прод, 2026-08-14): «покрыто ли окно» выводилось из самих баров
(`_covered_from` — дата самого раннего бара текущей версии). У выпуска, чья
история короче запрошенного окна, эта дата НАВСЕГДА правее frm — условие
«не покрыто» выполнялось всегда, и каждый запрос графика гонял полный пересчёт
(в логах: 2975 строк × 226 дней каждые 40 секунд по одной бумаге).
"""
import asyncio
import importlib
from datetime import date, timedelta

import pytest


@pytest.fixture
def bars(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.bars as bars_mod
    importlib.reload(bars_mod)
    yield bars_mod
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)
    importlib.reload(bars_mod)


@pytest.fixture
def calls(bars, monkeypatch):
    """Считаем вызовы build_bars: (days, till) каждого прохода."""
    seen = []

    async def fake_build(isin, days=30, kind="floater", board=None,
                         with_metrics=True, till=None, on_chunk=None):
        seen.append((days, till))
        # бумага торгуется только последние 20 дней — левее данных НЕТ
        start = date.today() - timedelta(days=20)
        out = []
        for i in range(21):
            d = start + timedelta(days=i)
            if till and d.isoformat() > till:
                continue
            out.append({"isin": isin, "ts": f"{d.isoformat()} 12:00", "kind": kind,
                        "close": 100.0, "vwap_pct": 100.0, "volume": 1, "value": 1000,
                        "y_idx_bps": 50, "metrics_ver": bars.BARS_METRICS_VERSION})
        return out

    monkeypatch.setattr(bars, "build_bars", fake_build)
    return seen


def _run(bars, days):
    # wait_past=True — тот же расчёт, но синхронно: фоновая задача не успела бы
    # записать достигнутую глубину до закрытия loop (в проде её от повторного
    # старта прикрывает _bg_backfill, пока проход не закончится)
    return asyncio.run(bars.ensure_bars("RU000TEST0001", days=days, wait_past=True))


def test_second_request_same_window_does_not_recalc(bars, calls):
    """Повтор того же окна — ни одного полного прохода (был вечный цикл)."""
    _run(bars, 226)
    past = [c for c in calls if c[0] == 226]
    assert len(past) == 1                     # первый заход посчитал
    calls.clear()
    _run(bars, 226)
    assert [c for c in calls if c[0] == 226] == []   # второй — no-op


def test_narrower_window_does_not_recalc(bars, calls):
    """Сужение окна (3 месяца → 1 месяц) пересчёта не требует."""
    _run(bars, 90)
    calls.clear()
    _run(bars, 30)
    assert [c for c in calls if c[0] > 1] == []


def test_widening_window_counts_only_missing_part(bars, calls):
    """Расширение окна досчитывает ТОЛЬКО левый кусок: till = прежняя граница."""
    _run(bars, 90)
    calls.clear()
    _run(bars, 240)
    past = [c for c in calls if c[0] == 240]
    assert len(past) == 1
    till = past[0][1]
    assert till is not None, "расширение окна пересчитало весь диапазон заново"
    assert till == (date.today() - timedelta(days=90)).isoformat()


def test_version_bump_forces_full_recalc(bars, calls, monkeypatch):
    """Бамп версии движка — полный пересчёт окна (цифры несопоставимы)."""
    _run(bars, 90)
    calls.clear()
    monkeypatch.setattr(bars, "BARS_METRICS_VERSION", bars.BARS_METRICS_VERSION + 1)
    _run(bars, 90)
    past = [c for c in calls if c[0] == 90]
    assert len(past) == 1 and past[0][1] is None


def test_chunks_are_written_as_counted(bars, monkeypatch):
    """Потоковая выдача: посчитанные дни уходят в базу ПОРЦИЯМИ по ходу счёта,
    а не одним батчем в конце. Иначе на длинном окне (замер: 730 дней = 144 с
    на бумагу) линия спреда минутами выглядела мёртвой."""
    flushes = []

    async def fake_build(isin, days=30, kind="floater", board=None,
                         with_metrics=True, till=None, on_chunk=None):
        # эмулируем счёт: три порции по ходу, от свежих к старым
        out = []
        for i in range(3):
            part = [{"isin": isin, "ts": f"2026-08-{10 - i:02d} 12:00", "kind": kind,
                     "close": 100.0, "vwap_pct": 100.0, "volume": 1, "value": 1000,
                     "y_idx_bps": 50, "metrics_ver": bars.BARS_METRICS_VERSION}]
            if on_chunk:
                on_chunk(part)
                flushes.append(part[0]["ts"])
            out += part
        return out

    monkeypatch.setattr(bars, "build_bars", fake_build)
    asyncio.run(bars.ensure_bars("RU000TEST0003", days=90, wait_past=True))
    assert len(flushes) == 3, "порции не отдавались по ходу счёта"
    assert flushes == sorted(flushes, reverse=True), "порядок не от свежих к старым"
    # всё, что отдано порциями, лежит в базе
    import services.portfolio_db as pdb
    with pdb._connect() as c:
        n = c.execute("SELECT COUNT(*) FROM bar_hourly WHERE isin=?",
                      ("RU000TEST0003",)).fetchone()[0]
    assert n == 3


def test_hole_inside_window_triggers_recalc(bars, calls):
    """Дыра ВНУТРИ окна лечится. Одной даты «самого раннего посчитанного бара»
    мало: у ВЭБP-41 покрытие начиналось с февраля, а весь август был без спреда
    (оборванная выборка ISS) — пересчёт не запускался, линия не строилась."""
    import services.portfolio_db as pdb
    _run(bars, 90)                                  # окно посчитано
    calls.clear()
    # эмулируем дыру: у бара есть цена, но нет спреда и версии
    day = (date.today() - timedelta(days=5)).isoformat()
    with pdb._connect() as c:
        c.execute("INSERT OR REPLACE INTO bar_hourly(isin,ts,kind,vwap_pct,close,"
                  "y_idx_bps,metrics_ver) VALUES(?,?,'floater',100.0,100.0,NULL,NULL)",
                  ("RU000TEST0001", f"{day} 12:00"))
    # следующий заход (другой день / после рестарта): в тот же день повторять
    # проход по тем же данным смысла нет — это вернуло бы вечный пересчёт
    bars._past_depth.clear()
    _run(bars, 90)
    assert [c for c in calls if c[0] == 90], "дыра внутри окна не вызвала пересчёт"


def test_hole_not_retried_within_same_pass(bars, calls):
    """…и наоборот: сразу после прохода дыра пересчёт не перезапускает —
    данные те же, а вечный цикл мы уже чинили."""
    import services.portfolio_db as pdb
    _run(bars, 90)
    calls.clear()
    day = (date.today() - timedelta(days=5)).isoformat()
    with pdb._connect() as c:
        c.execute("INSERT OR REPLACE INTO bar_hourly(isin,ts,kind,vwap_pct,close,"
                  "y_idx_bps,metrics_ver) VALUES(?,?,'floater',100.0,100.0,NULL,NULL)",
                  ("RU000TEST0001", f"{day} 12:00"))
    _run(bars, 90)
    assert [c for c in calls if c[0] == 90] == []
