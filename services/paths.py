"""Единая точка путей дисковых кэшей: data/cache/ (Docker-том data/ переживает
редеплой — раньше кэши жили в корне контейнера и сгорали при каждом деплое:
холодный старт = 453 bondization + Cbonds + ЦБ заново).

Оверрайд каталога: env CACHE_DIR. При первом обращении файл мигрирует из
легаси-локации (корень репо) переносом.
"""
from __future__ import annotations

import os
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.environ.get("CACHE_DIR") or os.path.join(_ROOT, "data", "cache")
# Логи — тоже в data/: docker logs обнуляется при каждом редеплое (контейнер
# пересоздаётся), и разбирать вчерашнее «сайт висел» было не по чему.
LOG_DIR = os.environ.get("LOG_DIR") or os.path.join(_ROOT, "data", "logs")


def log_path(name: str) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, name)


def atomic_write_json(path: str, obj) -> None:
    """tmp + os.replace: крэш на записи не оставляет битый JSON (раньше половина
    кэшей писалась голым open('w') — битый файл молча превращался в пустой кэш)."""
    import json
    # tmp УНИКАЛЕН НА ПИСАТЕЛЯ: фиксированное "{path}.tmp" два конкурентных
    # писателя одного кэша (daily_prewarm + запрос карточки) открывали
    # одновременно, их json.dump интерливился, и os.replace выкладывал
    # НАПОЛОВИНУ ПЕРЕЗАПИСАННЫЙ файл (наблюдалось на schedule_full_cache.json:
    # 7 КБ вместо 2.7 МБ).
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cache_path(name: str) -> str:
    """Абсолютный путь кэш-файла в data/cache/ (+ разовая миграция из корня).

    Заодно лечит cwd-зависимость: часть модулей открывала кэши по голому имени
    файла — запуск из другого каталога читал/писал мусор."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    new = os.path.join(CACHE_DIR, name)
    legacy = os.path.join(_ROOT, name)
    if not os.path.exists(new) and os.path.exists(legacy):
        try:
            os.replace(legacy, new)
        except OSError:
            return legacy  # не смогли перенести (права/гонка) — работаем со старым
    return new
