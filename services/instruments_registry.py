"""Реестр инструментов — НРД-независимый источник универса.

Ранее список бумаг был на 100% из НРД (fetch_floater_universe). Реестр хранит все
известные ISIN + ключевые расчётные параметры (база/маржа/погашение/частота/...)
в SQLite (data/instruments.db, Docker-том — переживает редеплой). Наполняется
ежедневным sync из Cbonds-выгрузки + замороженного NRD-кэша + MOEX; ручной слой
(source='manual', manual_locked=1) sync не затирает.

Расчётная часть (SM/DM/z по нашим кривым) работает поверх реестра без НРД.
НРД — опциональный слой обогащения (цена/fair-value), см. services.nrd_config.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("INSTRUMENTS_DB", _ROOT / "data" / "instruments.db"))

_lock = threading.Lock()

# Поля, которые sync НЕ трогает у записи с manual_locked=1 (ручной приоритет).
_MANUAL_FIELDS = ("base", "margin_bps", "maturity_date", "issue_date",
                  "coupon_period_days", "coupons_per_year", "day_count",
                  "face_value", "fixing_lag", "fixing_lag_unit", "coupon_mode",
                  "short_name", "var_type")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments(
  isin              TEXT PRIMARY KEY,
  short_name        TEXT,
  base              TEXT,              -- KEYRATE | RUONIA | FIXED | ...
  margin_bps        INTEGER,
  maturity_date     TEXT,             -- ISO
  issue_date        TEXT,
  coupon_period_days INTEGER,
  coupons_per_year  INTEGER,
  day_count         TEXT,
  face_value        REAL,
  var_type          TEXT,
  fixing_lag        INTEGER,
  fixing_lag_unit   TEXT,
  coupon_mode       TEXT,             -- point | average
  rating            TEXT,
  source            TEXT,             -- cbonds | nrd_frozen | moex | manual
  first_seen        TEXT,
  updated_at        TEXT,
  active            INTEGER NOT NULL DEFAULT 1,
  manual_locked     INTEGER NOT NULL DEFAULT 0,
  reviewed          INTEGER NOT NULL DEFAULT 0   -- новая бумага, ждёт ревью параметров
);
CREATE INDEX IF NOT EXISTS ix_instruments_active ON instruments(active);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


_initialized = False


def _ensure() -> None:
    global _initialized
    if _initialized:
        return
    with _lock, _conn() as c:
        c.executescript(_SCHEMA)
    _initialized = True


# Колонки, которые может нести входная запись (upsert). isin обязателен.
_COLS = ("short_name", "base", "margin_bps", "maturity_date", "issue_date",
         "coupon_period_days", "coupons_per_year", "day_count", "face_value",
         "var_type", "fixing_lag", "fixing_lag_unit", "coupon_mode", "rating")


def upsert(row: dict, source: str, mark_new: bool = True) -> str:
    """Вставить/обновить одну бумагу. Возвращает 'new' | 'updated' | 'skipped_locked'.
    У записи с manual_locked=1 обновляются только НЕ-manual поля (rating и т.п.).
    None-значения во входе НЕ затирают уже известные (COALESCE-семантика)."""
    _ensure()
    isin = (row.get("isin") or "").strip()
    if not isin:
        return "skipped_locked"
    with _lock, _conn() as c:
        cur = c.execute("SELECT * FROM instruments WHERE isin=?", (isin,))
        existing = cur.fetchone()
        now = _now()
        if existing is None:
            fields = {k: row.get(k) for k in _COLS}
            cols = ["isin", "source", "first_seen", "updated_at", "reviewed"] + list(_COLS)
            vals = [isin, source, now, now, 0 if mark_new else 1] + [fields[k] for k in _COLS]
            ph = ",".join("?" * len(cols))
            c.execute(f"INSERT INTO instruments({','.join(cols)}) VALUES({ph})", vals)
            return "new"
        locked = bool(existing["manual_locked"])
        updatable = [k for k in _COLS if not (locked and k in _MANUAL_FIELDS)]
        sets, vals = [], []
        for k in updatable:
            v = row.get(k)
            if v is None:
                continue  # не затираем известное None-ом
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return "skipped_locked"
        sets.append("updated_at=?")
        vals.append(now)
        # source обновляем только если не ручная запись
        if not locked:
            sets.append("source=?")
            vals.append(source)
        vals.append(isin)
        c.execute(f"UPDATE instruments SET {','.join(sets)} WHERE isin=?", vals)
        return "updated"


def set_manual(isin: str, params: dict, lock: bool = True) -> None:
    """Ручной ввод/правка параметров бумаги (admin). lock=True → sync не затрёт."""
    _ensure()
    isin = (isin or "").strip()
    if not isin:
        raise ValueError("isin обязателен")
    with _lock, _conn() as c:
        exists = c.execute("SELECT 1 FROM instruments WHERE isin=?", (isin,)).fetchone()
        now = _now()
        fields = {k: params.get(k) for k in _COLS if k in params}
        if exists is None:
            cols = ["isin", "source", "first_seen", "updated_at", "reviewed",
                    "manual_locked"] + list(fields.keys())
            vals = [isin, "manual", now, now, 1, 1 if lock else 0] + list(fields.values())
            ph = ",".join("?" * len(cols))
            c.execute(f"INSERT INTO instruments({','.join(cols)}) VALUES({ph})", vals)
        else:
            sets = [f"{k}=?" for k in fields]
            vals = list(fields.values())
            sets += ["updated_at=?", "reviewed=?"]
            vals += [now, 1]
            if lock:
                sets.append("manual_locked=?")
                vals.append(1)
            vals.append(isin)
            c.execute(f"UPDATE instruments SET {','.join(sets)} WHERE isin=?", vals)


def mark_reviewed(isin: str) -> None:
    _ensure()
    with _lock, _conn() as c:
        c.execute("UPDATE instruments SET reviewed=1, updated_at=? WHERE isin=?", (_now(), isin))


def get(isin: str) -> Optional[dict]:
    _ensure()
    with _conn() as c:
        r = c.execute("SELECT * FROM instruments WHERE isin=?", ((isin or "").strip(),)).fetchone()
        return dict(r) if r else None


_BASE_LABEL = {"KEYRATE": "Ключевая ставка", "RUONIA": "RUONIA"}


def universe_rows(only_floaters: bool = True) -> list[dict]:
    """Список бумаг в форме universe-строки (совместимо с fetch_floater_universe):
    NRD-поля (nrd_price_pct/discount_margin_bps/...) = None — их наполняет NRD-слой,
    когда включён. Расчётные поля (base/margin/maturity) — из реестра."""
    _ensure()
    with _conn() as c:
        rows = c.execute("SELECT * FROM instruments WHERE active=1").fetchall()
    out = []
    for r in rows:
        base = r["base"]
        if only_floaters and base not in ("KEYRATE", "RUONIA"):
            continue
        out.append({
            "isin": r["isin"],
            "name": r["short_name"] or r["isin"],
            "base_rate_type": base or "UNKNOWN",
            "spread_issue_bps": r["margin_bps"],
            "maturity_date": r["maturity_date"],
            "rating": r["rating"],
            # NRD-слой (пусто без НРД):
            "nrd_price_pct": None, "discount_margin_bps": None,
            "simple_margin_bps": None, "z_spread_bps": None,
            "nrd_duration": None, "current_yield_pct": None, "nrd_calc_date": None,
        })
    return out


def list_unreviewed() -> list[dict]:
    """Новые бумаги, у которых параметры ещё не подтверждены (для admin-ревью)."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, base, margin_bps, maturity_date, source, first_seen "
            "FROM instruments WHERE reviewed=0 AND active=1 ORDER BY first_seen DESC").fetchall()
    return [dict(r) for r in rows]


def sync_from_sources(nrd_items: list[dict] | None = None,
                      cbonds: dict | None = None,
                      manual: dict | None = None) -> dict:
    """Наполнить/обновить реестр из готовых источников (чистая, без сети — сеть
    собирает вызывающий, см. sync_instruments в poller). Приоритет расчётных полей:
    manual > Cbonds > замороженный NRD. Возвращает статистику {new, updated, total}.

    nrd_items — строки замороженного nrd_universe_cache (isin/name/base_rate_type/
                spread_issue_bps/maturity_date/rating);
    cbonds     — {isin: {base, margin_bps, name, freq, var_type, day_count}} (ref_data.load_cbonds);
    manual     — {isin: {...оверрайды}} (ref_data.load_manual)."""
    _ensure()
    cbonds = cbonds or {}
    manual = manual or {}
    # ручной слой: только реальные записи (dict), служебные ключи (_README) — вон
    manual = {k: v for k, v in manual.items()
              if not k.startswith("_") and isinstance(v, dict) and v}
    # объединённое множество ISIN из всех источников
    isins: set[str] = set()
    nrd_by = {}
    for it in nrd_items or []:
        i = (it.get("isin") or "").strip()
        if i:
            isins.add(i)
            nrd_by[i] = it
    isins.update(k.strip() for k in cbonds if k and not k.startswith("_"))
    isins.update(k.strip() for k in manual if k)

    stats = {"new": 0, "updated": 0, "skipped_locked": 0, "total": len(isins)}
    for isin in isins:
        n = nrd_by.get(isin, {})
        cb = cbonds.get(isin, {})
        # база/маржа: Cbonds авторитетнее (сверено 317/326 ±5бп), NRD — фолбэк
        base = cb.get("base") or n.get("base_rate_type")
        margin = cb.get("margin_bps")
        if margin is None:
            margin = n.get("spread_issue_bps")
        freq = cb.get("freq")
        row = {
            "isin": isin,
            "short_name": cb.get("name") or n.get("name"),
            "base": base,
            "margin_bps": int(margin) if margin is not None else None,
            "maturity_date": n.get("maturity_date"),   # NRD/MOEX; Cbonds без maturity
            "coupons_per_year": int(freq) if freq else None,
            "coupon_period_days": round(365 / freq) if freq else None,
            "day_count": cb.get("day_count"),
            "var_type": cb.get("var_type"),
            "rating": n.get("rating"),
        }
        res = upsert(row, source="cbonds" if cb else "nrd_frozen")
        stats[res if res in stats else "updated"] = stats.get(res, 0) + 1
    # ручной слой поверх (lock=True — sync впредь не затрёт); manual уже отфильтрован
    for isin, p in manual.items():
        set_manual(isin.strip(), p, lock=True)
    return stats


def count() -> dict:
    _ensure()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM instruments WHERE active=1").fetchone()[0]
        floaters = c.execute(
            "SELECT COUNT(*) FROM instruments WHERE active=1 AND base IN ('KEYRATE','RUONIA')"
        ).fetchone()[0]
        unrev = c.execute("SELECT COUNT(*) FROM instruments WHERE reviewed=0 AND active=1").fetchone()[0]
    return {"total": total, "floaters": floaters, "unreviewed": unrev}
