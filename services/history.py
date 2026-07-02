"""
Ежедневный снапшот юниверса НРД для day-over-day аналитики (repricing-алерты).
Флоатеры переоцениваются ежедневно; НРД API отдаёт только последний val_date в
универсе, поэтому историю Δz/Δdm/Δцены копим сами.

Файл nrd_history.json: {"dates": {"YYYY-MM-DD": {isin: [z_bps, dm_bps, price_pct]}}}.
Хранит последние MAX_DAYS дат. Запись идемпотентна (перезапись за сегодня — норм).
"""
from __future__ import annotations
import json
import os
from datetime import date
from typing import Optional

HISTORY_FILE = "nrd_history.json"
MAX_DAYS = 40


def _path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, HISTORY_FILE)


def _load() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) and "dates" in d else {"dates": {}}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"dates": {}}


def _save(data: dict) -> None:
    try:
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, _path())
    except OSError:
        pass


def record_snapshot(items: list, snap_date: Optional[str] = None) -> None:
    """Записать снапшот юниверса за дату (по умолчанию сегодня). Идемпотентно."""
    snap_date = snap_date or date.today().isoformat()
    data = _load()
    dates = data.setdefault("dates", {})
    dates[snap_date] = {
        it["isin"]: [it.get("z_spread_bps"), it.get("discount_margin_bps"), it.get("nrd_price_pct")]
        for it in items if it.get("isin")
    }
    # обрезаем до последних MAX_DAYS дат
    for old in sorted(dates)[:-MAX_DAYS]:
        dates.pop(old, None)
    _save(data)


# индексы полей в снапшоте
_FIELD_IDX = {"z": 0, "dm": 1, "px": 2}


def _diff(cur: dict, prev: dict, idx: int) -> dict:
    out = {}
    for isin, cvals in cur.items():
        pv = prev.get(isin)
        if not pv:
            continue
        a = cvals[idx] if idx < len(cvals) else None
        b = pv[idx] if idx < len(pv) else None
        if a is not None and b is not None:
            out[isin] = round(a - b)
    return out


def dod_map(field: str = "z", as_of: Optional[str] = None) -> dict:
    """{isin: Δ(field)} — изменение поля latest-vs-предыдущая доступная дата.
    field: 'z'|'dm'|'px'. Пусто, если истории <2 дат."""
    idx = _FIELD_IDX.get(field, 0)
    dates = _load().get("dates", {})
    keys = sorted(dates)
    if as_of and as_of in dates:
        keys = [k for k in keys if k <= as_of]
    if len(keys) < 2:
        return {}
    return _diff(dates[keys[-1]], dates[keys[-2]], idx)


def change_map(field: str = "z", lookback_days: int = 30, as_of: Optional[str] = None) -> dict:
    """{isin: Δ(field)} — изменение latest-vs-снапшот ~lookback_days назад.
    Берёт снапшот, ближайший к (latest − lookback_days). Требует фактического
    разрыва ≥ lookback/2 дней (иначе «месяц» на 3-дневной истории — ложь) → пусто."""
    idx = _FIELD_IDX.get(field, 0)
    dates = _load().get("dates", {})
    keys = sorted(dates)
    if as_of and as_of in dates:
        keys = [k for k in keys if k <= as_of]
    if len(keys) < 2:
        return {}
    latest = date.fromisoformat(keys[-1])
    target = latest.toordinal() - lookback_days
    best = min(keys[:-1], key=lambda k: abs(date.fromisoformat(k).toordinal() - target))
    if (latest - date.fromisoformat(best)).days < max(1, lookback_days // 2):
        return {}
    return _diff(dates[keys[-1]], dates[best], idx)


def history_dates() -> list:
    return sorted(_load().get("dates", {}))
