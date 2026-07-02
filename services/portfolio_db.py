"""SQLite-хранилище модуля «Фонды»: справочник фондов, снапшоты позиций, РЕПО-сделки.

data/portfolio.db — data/ монтируется Docker-томом в проде (docker-compose.prod.yml),
переживает редеплой. sqlite3 stdlib: соединение на вызов, WAL — нагрузка единицы
запросов в минуту, пул не нужен. Все записи в транзакции.

Модель (Ф1 — моделирование текущего портфеля):
  funds      — код (R5/D5/Y5), базовая валюта, капитал (для «×капитала»), спред фондирования
  positions  — снапшот позиций фонда: isin, qty (шт), дата снапшота; replace целиком
  repo_deals — явные РЕПО-сделки (фондирование): сумма, ставка, валюта, срок
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PORTFOLIO_DB", _ROOT / "data" / "portfolio.db"))

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS funds(
  code   TEXT PRIMARY KEY,
  name   TEXT NOT NULL,
  base_ccy TEXT NOT NULL DEFAULT 'RUB',
  capital REAL,                          -- капитал фонда в base_ccy; NULL => derived (NAV)
  funding_spread_bps REAL NOT NULL DEFAULT 0,
  created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions(
  fund  TEXT NOT NULL REFERENCES funds(code) ON DELETE CASCADE,
  isin  TEXT NOT NULL,
  qty   REAL NOT NULL,
  snap_date TEXT NOT NULL,
  PRIMARY KEY (fund, isin)
);
CREATE TABLE IF NOT EXISTS repo_deals(
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  fund  TEXT NOT NULL REFERENCES funds(code) ON DELETE CASCADE,
  amount REAL NOT NULL,                  -- тело займа в ccy
  rate_pct REAL NOT NULL,                -- годовая ставка, %
  ccy   TEXT NOT NULL DEFAULT 'RUB',
  open_date TEXT,
  term_days INTEGER,
  note  TEXT
);
CREATE TABLE IF NOT EXISTS nav_daily(
  fund  TEXT NOT NULL REFERENCES funds(code) ON DELETE CASCADE,
  date  TEXT NOT NULL,                   -- ISO календарный день
  nav_rub REAL,
  mv_rub REAL,
  repo_rub REAL,
  dv01_rub REAL,
  leverage REAL,
  net_carry_rub REAL,
  PRIMARY KEY (fund, date)               -- перезапись за день идемпотентна
);
CREATE TABLE IF NOT EXISTS pos_daily(
  fund  TEXT NOT NULL REFERENCES funds(code) ON DELETE CASCADE,
  date  TEXT NOT NULL,
  isin  TEXT NOT NULL,
  qty   REAL,
  price_pct REAL,
  fx    REAL,
  mv_rub REAL,
  PRIMARY KEY (fund, date, isin)         -- дневной позиционный срез (P&L-атрибуция Ф5)
);
"""

# добивка колонок к существующим таблицам (ALTER не умеет IF NOT EXISTS)
_MIGRATIONS = [
    "ALTER TABLE nav_daily ADD COLUMN fx_usd REAL",
    "ALTER TABLE nav_daily ADD COLUMN fx_cny REAL",
]

_SEED_FUNDS = [
    ("R5", "R5 · Рублёвый", "RUB"),
    ("D5", "D5 · Долларовый", "USD"),
    ("Y5", "Y5 · Юаневый", "CNY"),
]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Схема + сид трёх фондов (идемпотентно). Зовётся на старте приложения."""
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        for mig in _MIGRATIONS:
            try:
                conn.execute(mig)
            except sqlite3.OperationalError:
                pass  # колонка уже есть
        now = datetime.now(timezone.utc).isoformat()
        for code, name, ccy in _SEED_FUNDS:
            conn.execute(
                "INSERT OR IGNORE INTO funds(code,name,base_ccy,created) VALUES(?,?,?,?)",
                (code, name, ccy, now))


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ---------- funds ----------

def list_funds() -> list[dict]:
    with _connect() as conn:
        return _rows(conn.execute("SELECT * FROM funds ORDER BY code"))


def get_fund(code: str) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM funds WHERE code=?", (code,)).fetchone()
        return dict(r) if r else None


def create_fund(code: str, name: str, base_ccy: str = "RUB") -> dict:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO funds(code,name,base_ccy,created) VALUES(?,?,?,?)",
            (code.upper(), name, base_ccy.upper(),
             datetime.now(timezone.utc).isoformat()))
    return get_fund(code.upper())


def update_fund(code: str, **fields: Any) -> Optional[dict]:
    """Частичное обновление: name / base_ccy / capital / funding_spread_bps."""
    allowed = {"name", "base_ccy", "capital", "funding_spread_bps"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if sets:
        with _lock, _connect() as conn:
            conn.execute(
                f"UPDATE funds SET {', '.join(f'{k}=?' for k in sets)} WHERE code=?",
                (*sets.values(), code))
    return get_fund(code)


def delete_fund(code: str) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM funds WHERE code=?", (code,))
        return cur.rowcount > 0


# ---------- positions (снапшот) ----------

def get_positions(fund: str) -> list[dict]:
    with _connect() as conn:
        return _rows(conn.execute(
            "SELECT isin, qty, snap_date FROM positions WHERE fund=? ORDER BY isin", (fund,)))


def replace_snapshot(fund: str, rows: list[tuple[str, float]], snap_date: str) -> int:
    """Заменяет снапшот фонда целиком. rows = [(isin, qty), ...]. Возвращает кол-во позиций."""
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM positions WHERE fund=?", (fund,))
        conn.executemany(
            "INSERT INTO positions(fund,isin,qty,snap_date) VALUES(?,?,?,?)",
            [(fund, isin, qty, snap_date) for isin, qty in rows])
        return len(rows)


def upsert_position(fund: str, isin: str, qty: float, snap_date: str) -> None:
    """Точечное редактирование позиции; qty<=0 => удаление."""
    with _lock, _connect() as conn:
        if qty <= 0:
            conn.execute("DELETE FROM positions WHERE fund=? AND isin=?", (fund, isin))
        else:
            conn.execute(
                "INSERT INTO positions(fund,isin,qty,snap_date) VALUES(?,?,?,?) "
                "ON CONFLICT(fund,isin) DO UPDATE SET qty=excluded.qty, snap_date=excluded.snap_date",
                (fund, isin, qty, snap_date))


# ---------- repo ----------

def list_repos(fund: str) -> list[dict]:
    with _connect() as conn:
        return _rows(conn.execute("SELECT * FROM repo_deals WHERE fund=? ORDER BY id", (fund,)))


def add_repo(fund: str, amount: float, rate_pct: float, ccy: str = "RUB",
             open_date: Optional[str] = None, term_days: Optional[int] = None,
             note: Optional[str] = None) -> dict:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO repo_deals(fund,amount,rate_pct,ccy,open_date,term_days,note) "
            "VALUES(?,?,?,?,?,?,?)",
            (fund, amount, rate_pct, ccy.upper(), open_date, term_days, note))
        r = conn.execute("SELECT * FROM repo_deals WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(r)


def delete_repo(fund: str, repo_id: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM repo_deals WHERE fund=? AND id=?", (fund, repo_id))
        return cur.rowcount > 0


# ---------- nav history ----------

def save_nav(fund: str, day: str, m: dict) -> None:
    """Дневной снапшот метрик фонда (перезапись за день — идемпотентно)."""
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO nav_daily"
            "(fund,date,nav_rub,mv_rub,repo_rub,dv01_rub,leverage,net_carry_rub,fx_usd,fx_cny) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (fund, day, m.get("nav_rub"), m.get("mv_rub"), m.get("repo_rub"),
             m.get("dv01_rub"), m.get("leverage_gross"), m.get("net_carry_rub"),
             m.get("fx_usd"), m.get("fx_cny")))


def save_positions_snapshot(fund: str, day: str, rows: list[dict]) -> None:
    """Дневной позиционный срез с ценами (перезапись за день)."""
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM pos_daily WHERE fund=? AND date=?", (fund, day))
        conn.executemany(
            "INSERT INTO pos_daily(fund,date,isin,qty,price_pct,fx,mv_rub) VALUES(?,?,?,?,?,?,?)",
            [(fund, day, r["isin"], r.get("qty"), r.get("price_pct"),
              r.get("fx"), r.get("mv_rub")) for r in rows])


def get_nav_history(fund: str, days: int = 120) -> list[dict]:
    with _connect() as conn:
        return _rows(conn.execute(
            "SELECT * FROM nav_daily WHERE fund=? ORDER BY date DESC LIMIT ?",
            (fund, days)))[::-1]
