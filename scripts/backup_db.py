#!/usr/bin/env python3
"""Резервные копии баз дашборда.

Зачем: тиковый архив и история спредов существуют ТОЛЬКО в data/portfolio.db.
У Alor глубина сделок 30 дней, у MOEX ISS — своё окно; всё, что глубже,
восстановить неоткуда. Один упавший диск или кривая миграция — и год
накопления исчезает.

Копия снимается sqlite3.backup() — штатным онлайн-бэкапом SQLite: он
консистентен на ЖИВОЙ базе (в отличие от `cp`, который поймает файл в
середине транзакции и оставит битую копию) и идёт порциями, не запирая
писателей надолго.

Запуск (в проде — внутри контейнера, база лежит на томе data/):
    docker compose -f docker-compose.prod.yml exec -T floaters \\
        python scripts/backup_db.py

Ретеншен: RETAIN_DAILY последних ежедневных + RETAIN_WEEKLY воскресных.
Проверка: копия открывается и в ней считаются строки контрольных таблиц —
битый или обрезанный файл до ротации не доживёт.
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

DATA_DIR = Path(os.environ.get("DATA_DIR") or _ROOT / "data")
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or DATA_DIR / "backups")

# Сколько копий держим. Место на VPS считаное (30 ГБ на всё, из них ~2,4 ГБ
# сама база), а VACUUM тикового архива требует свободного места ещё в размер
# базы — поэтому копий немного, а зеркало снаружи снимает scripts/pull_backup.sh.
RETAIN_DAILY = int(os.environ.get("BACKUP_RETAIN_DAILY", "2"))
RETAIN_WEEKLY = int(os.environ.get("BACKUP_RETAIN_WEEKLY", "2"))

# Что бэкапим и чем проверяем копию (таблица → её ждём непустой).
DATABASES = {
    "portfolio.db": ("trade_tick", "bar_hourly", "spread_daily"),
    "instruments.db": ("instruments",),
}
# Мелкие, но критичные файлы: без users.json на сайт не войти никто.
PLAIN_FILES = ("users.json",)


def _log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} backup: {msg}", flush=True)


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _snapshot(src: Path, tmp: Path) -> None:
    """Онлайн-копия SQLite: порциями, с уступкой писателям между шагами."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(tmp)
    try:
        # pages=2000 (~8 МБ за шаг) + sleep между шагами: на живой базе бэкап
        # не должен держать писателя (демоны пишут тики и бары непрерывно)
        s.backup(d, pages=2000, sleep=0.05)
    finally:
        d.close()
        s.close()


def _verify(snap: Path, tables: tuple[str, ...]) -> str:
    """Копия открывается и контрольные таблицы читаются — иначе это не бэкап."""
    c = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        counts = []
        for t in tables:
            n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if n <= 0:
                raise RuntimeError(f"таблица {t} пуста в копии")
            counts.append(f"{t}={n}")
        return ", ".join(counts)
    finally:
        c.close()


def _gzip(src: Path, dst: Path) -> None:
    with open(src, "rb") as fi, gzip.open(dst, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, length=8 * 1024 * 1024)


def _rotate(prefix: str, suffix: str = ".gz") -> list[str]:
    """Оставляем RETAIN_DAILY свежих и RETAIN_WEEKLY воскресных, остальное сносим."""
    files = sorted(BACKUP_DIR.glob(f"{prefix}-*{suffix}"), reverse=True)
    keep: set[Path] = set(files[:RETAIN_DAILY])
    weekly = 0
    for f in files:
        try:
            d = date.fromisoformat(f.name.split("-")[1][:4] + "-"
                                   + f.name.split("-")[1][4:6] + "-"
                                   + f.name.split("-")[1][6:8])
        except (IndexError, ValueError):
            continue
        if d.weekday() == 6 and weekly < RETAIN_WEEKLY:   # воскресенье
            keep.add(f)
            weekly += 1
    removed = []
    for f in files:
        if f not in keep:
            f.unlink(missing_ok=True)
            removed.append(f.name)
    return removed


def backup_one(name: str, tables: tuple[str, ...], stamp: str) -> dict:
    src = DATA_DIR / name
    if not src.exists():
        return {"db": name, "skipped": "нет файла"}
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    size = src.stat().st_size
    # копия + её сжатая версия живут на диске одновременно
    need = int(size * 1.4)
    if _free_bytes(BACKUP_DIR) < need:
        return {"db": name, "skipped": "мало места",
                "need_mb": round(need / 1e6), "free_mb": round(_free_bytes(BACKUP_DIR) / 1e6)}

    prefix = src.stem
    tmp = BACKUP_DIR / f".{prefix}-{stamp}.tmp"
    out = BACKUP_DIR / f"{prefix}-{stamp}.db.gz"
    t0 = time.monotonic()
    try:
        _snapshot(src, tmp)
        checks = _verify(tmp, tables)
        _gzip(tmp, out)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        raise RuntimeError(f"{name}: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
    return {"db": name, "file": out.name,
            "src_mb": round(size / 1e6, 1), "gz_mb": round(out.stat().st_size / 1e6, 1),
            "secs": round(time.monotonic() - t0, 1), "checks": checks,
            "rotated": _rotate(prefix)}


def main() -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    rc = 0
    for name, tables in DATABASES.items():
        try:
            _log(str(backup_one(name, tables, stamp)))
        except Exception as e:
            rc = 1
            _log(f"ОШИБКА {e}")
    for name in PLAIN_FILES:
        src = DATA_DIR / name
        if not src.exists():
            continue
        dst = BACKUP_DIR / f"{src.stem}-{stamp}{src.suffix}"
        shutil.copy2(src, dst)
        _rotate(src.stem, src.suffix)
        _log(f"{{'file': '{dst.name}', 'kb': {round(dst.stat().st_size / 1e3, 1)}}}")
    _log(f"свободно после: {round(_free_bytes(BACKUP_DIR) / 1e9, 1)} ГБ")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
