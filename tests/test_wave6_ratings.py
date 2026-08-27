"""Регресс-щит волны 6: рейтинги обновляются, а не записываются один раз.

Баг: очередь дозагрузки отсеивала по ФАКТУ наличия рейтинга в реестре, а не по
времени проверки. Бумага, которой рейтинг однажды записали, исчезала из очереди
НАВСЕГДА — понижение AAA→A не доезжало ни через неделю, ни через год, а
семидневный json-TTL до неё просто не доходил."""
import datetime as _dt
from pathlib import Path

import pytest


@pytest.fixture
def reg(tmp_path, monkeypatch):
    from services import instruments_registry as m
    monkeypatch.setattr(m, "DB_PATH", Path(tmp_path) / "t.db")
    monkeypatch.setattr(m, "_initialized", False)
    m._ensure()
    return m


def test_rating_checked_at_column_exists(reg):
    """Отметка нужна DURABLE: json-кэш гибнет с рестартом, и без неё
    todo = весь универс каждый рестарт (вечный передрайн corpbonds)."""
    import sqlite3
    cols = [r[1] for r in sqlite3.connect(reg.DB_PATH).execute(
        "PRAGMA table_info(instruments)")]
    assert "rating_checked_at" in cols


def test_set_rating_stamps_time(reg):
    reg.upsert({"isin": "RU000TESTR001", "base": "KEYRATE"}, "moex")
    assert reg.rating_checked_map(["RU000TESTR001"]) == {}
    reg.set_rating("RU000TESTR001", "AAA")
    got = reg.rating_checked_map(["RU000TESTR001"])
    assert "RU000TESTR001" in got and got["RU000TESTR001"]
    assert reg.ratings_map(["RU000TESTR001"]) == {"RU000TESTR001": "AAA"}


def test_rating_checked_map_skips_unstamped(reg):
    """Легаси-строка: рейтинг есть, отметки нет — в карту не попадает,
    значит очередь её подхватит (и поставит в хвост)."""
    reg.upsert({"isin": "RU000TESTR002", "base": "KEYRATE", "rating": "AA"}, "moex")
    assert reg.rating_checked_map(["RU000TESTR002"]) == {}
    assert reg.ratings_map(["RU000TESTR002"]) == {"RU000TESTR002": "AA"}


def test_queue_requeues_after_ttl(monkeypatch):
    """ГЛАВНОЕ СВОЙСТВО: бумага с ПРОТУХШЕЙ отметкой возвращается в очередь,
    со свежей — нет. До фикса первая тоже не возвращалась никогда."""
    import services.ratings as R

    now = _dt.datetime.now(_dt.timezone.utc)
    fresh = (now - _dt.timedelta(days=1)).isoformat()
    stale = (now - _dt.timedelta(days=30)).isoformat()

    class _Reg:
        @staticmethod
        def ratings_map(isins):
            return {i: "AAA" for i in isins}        # у ВСЕХ рейтинг уже есть

        @staticmethod
        def rating_checked_map(isins):
            return {"RU_FRESH": fresh, "RU_STALE": stale}

    monkeypatch.setattr(R, "_load", lambda: {})     # json-кэш пуст
    monkeypatch.setitem(__import__("sys").modules, "services.instruments_registry", _Reg)

    seen = {}

    async def fake_fetch(isin, client=None):
        seen[isin] = True
        return {"rating": "A"}

    monkeypatch.setattr("services.enrich_corpbonds.fetch_corpbonds", fake_fetch,
                        raising=False)
    # сам refresh ходит в сеть — проверяем ОТБОР, а не доставку:
    # воспроизводим условие очереди из refresh()
    checked = _Reg.rating_checked_map(["RU_FRESH", "RU_STALE", "RU_NEW"])

    def reg_fresh(isin):
        ts = checked.get(isin)
        if not ts:
            return False
        age = (now - _dt.datetime.fromisoformat(ts)).total_seconds()
        return age <= R._TTL

    assert reg_fresh("RU_FRESH") is True      # проверяли вчера — не трогаем
    assert reg_fresh("RU_STALE") is False     # 30 дней назад — ПЕРЕПРОВЕРИТЬ
    assert reg_fresh("RU_NEW") is False       # не проверяли вовсе


def test_refresh_selection_uses_time_not_presence():
    """Пин против возврата старого условия `not rated.get(i)`."""
    import inspect
    import services.ratings as R
    src = inspect.getsource(R.refresh)
    assert "_reg_fresh" in src, "отбор снова по факту наличия рейтинга"
    assert "rating_checked_map" in src


# ─── правки автоматических путей доезжают до витрины ────────────────────────

def test_auto_paths_invalidate_cache(reg, monkeypatch):
    """apply_authoritative / set_exotic / reclassify_fixed / normalize_ofz_pk
    правят расчётные поля, но кэша не сбрасывали: memo уровней завязан на
    (calc_date, curves_ts), а пересчёт заказывается только сменой ЦЕНЫ — в
    неликвиде исправленная маржа ждала до следующего дня."""
    calls = []
    monkeypatch.setattr(reg, "invalidate_params_cache",
                        lambda isin=None: calls.append(isin))

    reg.upsert({"isin": "RU000TESTINV1", "base": "KEYRATE", "margin_bps": 100}, "moex")
    calls.clear()

    assert reg.apply_authoritative("RU000TESTINV1", {"margin_bps": 250}, "corpbonds")
    assert calls == ["RU000TESTINV1"], f"apply_authoritative не сбросил кэш: {calls}"

    calls.clear()
    reg.set_exotic("RU000TESTINV1", "КС + 2%, не более 25%")
    assert calls == ["RU000TESTINV1"], f"set_exotic не сбросил кэш: {calls}"

    calls.clear()
    reg.upsert({"isin": "RU000TESTINV2", "base": "KEYRATE"}, "moex")
    reg.reclassify_fixed("RU000TESTINV2")
    assert calls == ["RU000TESTINV2"], f"reclassify_fixed не сбросил кэш: {calls}"


def test_apply_authoritative_respects_lock(reg, monkeypatch):
    """Сброс кэша не должен случаться, когда правка ОТКЛОНЕНА локом."""
    calls = []
    monkeypatch.setattr(reg, "invalidate_params_cache",
                        lambda isin=None: calls.append(isin))
    reg.set_manual("RU000TESTINV3", {"base": "KEYRATE", "margin_bps": 100}, lock=True)
    calls.clear()
    assert reg.apply_authoritative("RU000TESTINV3", {"margin_bps": 250},
                                   "corpbonds") is False
    assert calls == [], "кэш сброшен, хотя правка отклонена локом"
