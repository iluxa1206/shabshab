"""Снимок метрик на диск: рестарт не начинается с пустой витрины.

Полный пересчёт рынка после рестарта — 612 бумаг по 200–800 мс на одном ядре,
две-три минуты прочерков в таблице. Считать заново нечего: метрики зависят от
цены, дня и кривой, и все три переживают рестарт. Отсюда снимок — и жёсткая
проверка, что он ПРО ЭТОТ день и ЭТУ кривую.
"""
import json
import os
import time

import pytest

from services import metrics_persist as mp


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("services.paths.CACHE_DIR", str(tmp_path))
    return tmp_path


ROWS = {"RU000A100001": {"yoi": 210, "yoi_bid": 205, "vol_px": {"bid:5000000": 99.4}}}


def test_roundtrip(cache_dir):
    """Сохранили — подняли теми же числами."""
    assert mp.save(calc_date="2026-09-01", curves_fp="fp1", universe=ROWS, fixed={}) == 1
    got = mp.load(calc_date="2026-09-01", curves_fp="fp1")
    assert got["universe"] == ROWS
    assert got["fixed"] == {}


def test_other_day_and_other_curve_are_ignored(cache_dir):
    """Чужой день или чужая кривая — снимок мимо целиком: то же число цены даёт
    на другой кривой другой спред, а назавтра другие поток и НКД."""
    mp.save(calc_date="2026-09-01", curves_fp="fp1", universe=ROWS, fixed={})
    assert mp.load(calc_date="2026-09-02", curves_fp="fp1") is None
    assert mp.load(calc_date="2026-09-01", curves_fp="fp2") is None


def test_stale_snapshot_ignored(cache_dir, monkeypatch):
    """День совпал, но снимок утренний — не поднимаем: устаревшее число хуже
    прочерка, который заполнится через минуту."""
    mp.save(calc_date="2026-09-01", curves_fp="fp1", universe=ROWS, fixed={})
    path = os.path.join(str(cache_dir), "metrics_snapshot.json")
    d = json.load(open(path, encoding="utf-8"))
    d["ts"] = time.time() - mp.MAX_AGE_SEC - 1
    json.dump(d, open(path, "w", encoding="utf-8"))
    assert mp.load(calc_date="2026-09-01", curves_fp="fp1") is None


def test_schema_version_bump_invalidates(cache_dir, monkeypatch):
    """Схема строки поменялась — вчерашний файл не оживает."""
    mp.save(calc_date="2026-09-01", curves_fp="fp1", universe=ROWS, fixed={})
    monkeypatch.setattr(mp, "SNAPSHOT_VERSION", mp.SNAPSHOT_VERSION + 1)
    assert mp.load(calc_date="2026-09-01", curves_fp="fp1") is None


def test_non_serializable_field_dropped_not_fatal(cache_dir):
    """Объект в строке выпадает из снимка целиком, а не ломает его и не
    подсовывает витрине строку вместо даты: потребитель прочтёт row.get(...) →
    None, то есть «числа нет», и движок посчитает его заново."""
    from datetime import date
    rows = {"RU000A100001": {"yoi": 210, "when": date(2026, 9, 1)}}
    assert mp.save(calc_date="2026-09-01", curves_fp="fp1", universe=rows, fixed={}) == 1
    got = mp.load(calc_date="2026-09-01", curves_fp="fp1")
    assert got["universe"]["RU000A100001"]["yoi"] == 210
    assert "when" not in got["universe"]["RU000A100001"]   # поля просто нет


def test_empty_state_writes_nothing(cache_dir):
    assert mp.save(calc_date="2026-09-01", curves_fp="fp1", universe={}, fixed={}) == 0
    assert mp.load(calc_date="2026-09-01", curves_fp="fp1") is None


def test_broken_file_is_not_fatal(cache_dir):
    """Битый файл (обрыв записи, чужая рука) — просто нет снимка."""
    path = os.path.join(str(cache_dir), "metrics_snapshot.json")
    open(path, "w", encoding="utf-8").write("{не json")
    assert mp.load(calc_date="2026-09-01", curves_fp="fp1") is None


def test_snapshot_key_is_curves_fingerprint_not_trading_day(monkeypatch):
    """Аудит 03.09: версия контекста расширилась с пары до тройки (день
    расписаний вставлен вторым элементом), а три вызова metrics_persist остались
    на позиционном [1] — снимок витрины писался и читался под ДАТОЙ ТОРГОВОГО
    ДНЯ вместо отпечатка кривых. Ключ внутри дня переставал меняться вовсе, и
    после рестарта в таблицу поднимались числа снятой кривой."""
    import asyncio
    from services import universe_stream as us

    ctx = {"calc_date": "2026-09-03",
           "version": ("2026-09-03", "2026-09-03", "CURVES-FP")}
    assert us._curves_fp_of(ctx) == "CURVES-FP"

    seen = {}

    def fake_save(*, calc_date, curves_fp, universe, fixed):
        seen["curves_fp"] = curves_fp
        return 0

    monkeypatch.setattr("services.metrics_persist.save", fake_save)
    monkeypatch.setattr(us, "_last_ctx", ctx, raising=False)
    asyncio.run(us.save_snapshot_now())
    assert seen["curves_fp"] == "CURVES-FP"
