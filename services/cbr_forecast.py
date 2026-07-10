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
from datetime import date, timedelta
from typing import Optional, Dict, List, Tuple

_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cbr_forecast.json")
_cache: Optional[dict] = None

# График опорных заседаний Совета директоров ЦБ (даты решений по КС). ~8/год.
# 2026-2027 — известный график; далее экстраполяция (та же сетка). Обновлять.
_CB_MEETINGS = [
    "2026-07-24", "2026-09-12", "2026-10-24", "2026-12-19",
    "2027-02-13", "2027-03-20", "2027-04-24", "2027-06-05", "2027-07-24",
    "2027-09-11", "2027-10-23", "2027-12-18",
    "2028-02-12", "2028-03-24", "2028-04-28", "2028-06-09", "2028-07-28",
    "2028-09-15", "2028-10-27", "2028-12-22",
    "2029-02-16", "2029-03-23", "2029-04-27", "2029-06-08", "2029-07-27",
    "2029-09-14", "2029-10-26", "2029-12-21",
]


def meeting_dates(after: date, until: date) -> List[date]:
    out = [date.fromisoformat(d) for d in _CB_MEETINGS]
    return [d for d in out if after < d <= until]


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


def meeting_step_path(calc_date: date, current_ks: float,
                      horizon_years: int = 4) -> List[Tuple[date, float]]:
    """Аппроксимация траектории КС: ступеньки на датах заседаний ЦБ, подобранные
    так, чтобы ВРЕМЕННАЯ СРЕДНЯЯ за каждый год = прогнозной средней КС ЦБ.

    Модель: КС кусочно-постоянна между заседаниями (уровень фиксируется на
    заседании до следующего). Уровни на заседаниях лежат на кусочно-линейной
    кривой через год-якоря; якоря итеративно подгоняются под годовую среднюю
    (метод простой коррекции по невязке). Возвращает [(meeting_date, level%)] —
    точки изменения ставки; между ними КС держится.
    """
    avg = avg_ks_by_year()
    if not avg:
        return []
    neutral = neutral_pct()
    years = sorted(avg)
    horizon = date(calc_date.year + horizon_years, 12, 31)
    meets = meeting_dates(calc_date, horizon)
    if not meets:
        return []

    # год-якоря (значение КС на 30 июня года) — старт с прогнозных средних;
    # за пределами прогноза — нейтраль
    anchors: Dict[int, float] = {calc_date.year - 1: current_ks}
    for y in years:
        anchors[y] = avg[y]
    last_y = years[-1]
    for y in range(last_y + 1, horizon.year + 1):
        anchors[y] = neutral

    def _anchor_x(y):
        return date(y, 6, 30)

    def interp(d: date) -> float:
        xs = sorted(anchors)
        # до первого якоря — плоско текущей; после последнего — нейтраль
        if d <= _anchor_x(xs[0]):
            return anchors[xs[0]]
        if d >= _anchor_x(xs[-1]):
            return anchors[xs[-1]]
        for i in range(1, len(xs)):
            x0, x1 = _anchor_x(xs[i - 1]), _anchor_x(xs[i])
            if x0 <= d <= x1:
                w = (d - x0).days / ((x1 - x0).days or 1)
                return anchors[xs[i - 1]] + w * (anchors[xs[i]] - anchors[xs[i - 1]])
        return anchors[xs[-1]]

    def build_levels():
        lv = [interp(m) for m in meets]
        for i in range(1, len(lv)):          # монотонно не растёт
            lv[i] = min(lv[i], lv[i - 1])
        lv = [max(v, neutral) for v in lv]   # не ниже нейтрали
        if lv and lv[0] > current_ks:        # не прыгаем вверх с текущей
            lv[0] = current_ks
            for i in range(1, len(lv)):
                lv[i] = min(lv[i], lv[i - 1])
        return lv

    def level_at(lv, d):
        # уровень КС, активный на дату d: последнее заседание ≤ d, иначе текущая
        v = current_ks
        for md, x in zip(meets, lv):
            if md <= d:
                v = x
            else:
                break
        return v

    def year_avg(lv, y):
        y0, y1 = date(y, 1, 1), date(y + 1, 1, 1)
        start = max(calc_date, y0)
        if start >= y1:
            return None
        # временная средняя: сегменты [start→первое заседание года→…→y1]
        pts = [start] + [d for d in meets if start < d < y1] + [y1]
        tot = days = 0
        for i in range(len(pts) - 1):
            v = level_at(lv, pts[i])
            dd = (pts[i + 1] - pts[i]).days
            tot += v * dd
            days += dd
        return tot / days if days else None

    # подгоняем год-якоря под годовую среднюю (клампы монотонности внутри build_levels).
    # Текущий (частичный) год не целим — его полную среднюю из середины не восстановить.
    target_years = [y for y in years if y > calc_date.year]
    for _ in range(12):
        lv = build_levels()
        maxres = 0.0
        for y in target_years:
            ya = year_avg(lv, y)
            if ya is None:
                continue
            res = avg[y] - ya
            anchors[y] += res
            maxres = max(maxres, abs(res))
        if maxres < 0.03:
            break
    return [(m, round(v, 3)) for m, v in zip(meets, build_levels())]


def reload():
    global _cache
    _cache = None
