"""Runtime-переключатель источника НРД (ценовой центр).

НРД — ОПЦИОНАЛЬНЫЙ слой обогащения (цена/fair-value/duration/маржа-бенчмарк).
Базово ВЫКЛЮЧЕН: расчётная часть (SM/DM/z по нашим кривым) и универс из реестра
инструментов работают без него. Включается админом «по кнопке», когда/если
доступ к API НРД выдан (сейчас 403 — пробный доступ отозван).

Состояние персистится в data/nrd_config.json (переживает редеплой). Гейт активности:
    активен ⇔ enabled(runtime-флаг) И configured(креды в .env присутствуют).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_FILE = Path(os.getenv("NRD_CONFIG_FILE", _ROOT / "data" / "nrd_config.json"))

# Дефолт OFF намеренно: продукт НРД-независим, включение — осознанное действие
# админа. ENV NRD_ENABLED_DEFAULT=1 позволяет перекрыть дефолт при первом старте
# (например, если доступ уже есть и не хочется кликать).
_DEFAULT_ENABLED = os.getenv("NRD_ENABLED_DEFAULT", "0") in ("1", "true", "True")

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _cache = {"enabled": _DEFAULT_ENABLED}
    if "enabled" not in _cache:
        _cache["enabled"] = _DEFAULT_ENABLED
    return _cache


def is_enabled() -> bool:
    """Включён ли НРД-слой (runtime-флаг). НЕ проверяет наличие кред —
    это делает nrd.is_active()."""
    return bool(_load().get("enabled"))


def set_enabled(value: bool) -> dict:
    """Переключить НРД-слой (админ-кнопка). Возвращает новое состояние.
    Сбрасывает in-memory кэши универса/метрик НРД, чтобы смена подхватилась сразу."""
    global _cache
    with _lock:
        cfg = dict(_load())
        cfg["enabled"] = bool(value)
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        os.replace(tmp, _CONFIG_FILE)  # атомарно
        _cache = cfg
    # инвалидация NRD in-memory слоёв (universe/metrics), чтобы OFF→ON и обратно
    # подхватилось без рестарта; импорт локальный — избегаем цикла на старте
    try:
        from services import nrd
        nrd.invalidate_memory()
    except Exception:
        pass
    return {"enabled": cfg["enabled"]}
