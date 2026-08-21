"""Резервные копии баз (scripts/backup_db.py).

Данные тикового архива невосстановимы: у Alor глубина 30 дней. Поэтому бэкап
обязан быть (а) консистентным на живой базе, (б) проверенным — битая копия,
дожившая до ротации, хуже отсутствия копии, потому что создаёт ложное чувство
защищённости.
"""
import gzip
import importlib
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def bk(tmp_path, monkeypatch):
    """backup_db на временном каталоге данных."""
    data = tmp_path / "data"
    data.mkdir()
    c = sqlite3.connect(data / "portfolio.db")
    c.executescript(
        "CREATE TABLE trade_tick(a); CREATE TABLE bar_hourly(a);"
        "CREATE TABLE spread_daily(a);"
        "INSERT INTO trade_tick VALUES(1); INSERT INTO bar_hourly VALUES(1);"
        "INSERT INTO spread_daily VALUES(1);")
    c.commit()
    c.close()
    (data / "users.json").write_text('{"u": 1}')
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    import scripts.backup_db as mod
    importlib.reload(mod)
    yield mod, data


def test_backup_creates_verified_gzip(bk):
    mod, data = bk
    assert mod.main() == 0
    files = sorted((data / "backups").glob("portfolio-*.db.gz"))
    assert len(files) == 1
    # копия распаковывается и это рабочая база, а не обрезок
    raw = gzip.decompress(files[0].read_bytes())
    restored = data / "restored.db"
    restored.write_bytes(raw)
    c = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
    assert c.execute("SELECT COUNT(*) FROM trade_tick").fetchone()[0] == 1
    c.close()
    # мелкие критичные файлы тоже уехали
    assert list((data / "backups").glob("users-*.json"))


def test_verify_rejects_empty_copy(bk, monkeypatch):
    """Пустая контрольная таблица = копия негодна; файл .gz не создаётся."""
    mod, data = bk
    c = sqlite3.connect(data / "portfolio.db")
    c.execute("DELETE FROM trade_tick")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError):
        mod.backup_one("portfolio.db", ("trade_tick",), "20260101-0000")
    assert not list((data / "backups").glob("portfolio-*.db.gz"))


def test_rotation_keeps_daily_and_weekly(bk, monkeypatch):
    """Держим RETAIN_DAILY свежих + RETAIN_WEEKLY воскресных, остальное сносим."""
    mod, data = bk
    monkeypatch.setattr(mod, "RETAIN_DAILY", 2)
    monkeypatch.setattr(mod, "RETAIN_WEEKLY", 1)
    bdir = data / "backups"
    bdir.mkdir(exist_ok=True)
    # десять дней подряд, включая воскресенья
    d0 = date(2026, 8, 3)          # понедельник
    made = []
    for i in range(10):
        d = d0 + timedelta(days=i)
        f = bdir / f"portfolio-{d.strftime('%Y%m%d')}-0400.db.gz"
        f.write_bytes(b"x")
        made.append((d, f))
    mod._rotate("portfolio")

    left = sorted(p.name for p in bdir.glob("portfolio-*.db.gz"))
    assert len(left) == 3                      # 2 свежих + 1 воскресная
    assert left[-1].startswith("portfolio-20260812")     # самая свежая
    sundays = [f.name for d, f in made if d.weekday() == 6]
    assert any(s in left for s in sundays)


def test_skips_when_disk_is_tight(bk, monkeypatch):
    """Места меньше, чем нужно копии + её архиву — не начинаем вовсе: оборванный
    бэкап на тесном диске добьёт то, что мы пытаемся спасти."""
    mod, data = bk
    monkeypatch.setattr(mod, "_free_bytes", lambda p: 1)
    res = mod.backup_one("portfolio.db", ("trade_tick",), "20260101-0000")
    assert res["skipped"] == "мало места"


def test_manual_backups_rotate_only_with_replacement(bk):
    """Ручные копии data/*.bak-* ротацией не покрывались и копились незамеченными.

    Один такой файл (portfolio.db.bak-echo-20260814, 1.9 ГБ) держал диск прода на
    80%. Сносим старые — но только когда по этой базе есть свежая штатная копия:
    страховка перед миграцией не должна исчезать раньше, чем появится замена.
    """
    import os
    import time
    mod, data = bk

    old = time.time() - (mod.MANUAL_KEEP_DAYS + 5) * 86400
    have_replacement = data / "portfolio.db.bak-echo-20260814"
    no_replacement = data / "orphan.db.bak-20260101"
    fresh = data / "portfolio.db.bak-yesterday"
    for f in (have_replacement, no_replacement, fresh):
        f.write_bytes(b"x" * 100)
    for f in (have_replacement, no_replacement):
        os.utime(f, (old, old))

    assert mod.main() == 0          # создаёт штатную копию portfolio + ротацию

    assert not have_replacement.exists(), "старая копия при наличии штатной не снесена"
    assert no_replacement.exists(), "копия без штатной замены снесена — так нельзя"
    assert fresh.exists(), "свежая ручная копия снесена"


def test_orphan_tmp_files_are_cleaned(bk):
    """Спутники SQLite от прерванных прогонов: сам .tmp удаляется в finally, а
    .tmp-shm/.tmp-wal оставались лежать вечно."""
    mod, data = bk
    backups = data / "backups"
    backups.mkdir(exist_ok=True)
    (backups / ".portfolio-20260820-2230.tmp-shm").write_bytes(b"s")
    (backups / ".portfolio-20260820-2230.tmp-wal").write_bytes(b"")

    assert mod.main() == 0
    assert not list(backups.glob(".*tmp-shm"))
    assert not list(backups.glob(".*tmp-wal"))
