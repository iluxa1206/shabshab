"""Среднесрочный прогноз Банка России (cbr_forecast.json) — редактируемый вручную.

ЦБ публикует прогноз средней КС по годам + долгосрочную нейтральную ставку в
Основных направлениях ЕГДКП / пресс-релизах (PDF, машинно ненадёжно) → ведём
руками, обновляя после опорных заседаний (~4×/год).

Используется:
  1) neutral_pct → эндпоинт затухания траектории КС (met_float Прил.3);
  2) avg_ks_pct  → линия «прогноз ЦБ» на графике (взгляд ЦБ vs рынок).
"""
from __future__ import annotations
import os
import json
from typing import Optional, Dict, List

_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cbr_forecast.json")
_cache: Optional[dict] = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _cache = {}
    return _cache


def _mid(rng) -> Optional[float]:
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        return (float(rng[0]) + float(rng[1])) / 2.0
    if isinstance(rng, (int, float)):
        return float(rng)
    return None


def neutral_pct(default: float = 8.0) -> float:
    """Долгосрочная нейтральная КС (середина диапазона прогноза ЦБ), %."""
    m = _mid(_load().get("neutral_pct"))
    return m if m is not None else default


def avg_ks_by_year() -> Dict[int, float]:
    """{год: средняя прогнозная КС ЦБ, %} (середина диапазонов)."""
    out: Dict[int, float] = {}
    for y, rng in (_load().get("avg_ks_pct") or {}).items():
        m = _mid(rng)
        if m is not None:
            try:
                out[int(y)] = m
            except (ValueError, TypeError):
                pass
    return out


def as_of() -> Optional[str]:
    return _load().get("as_of")


def reload():
    global _cache
    _cache = None
