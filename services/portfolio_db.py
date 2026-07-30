"""Общее SQLite-хранилище приложения: алерты по стакану + дневные снапшоты
спред-метрик. (Раньше здесь жил модуль «Фонды» — удалён; файл остался как
шаред-инфра `_connect`/`_lock`/`init_db`, на которую опираются services.alerts
и services.spread_history.)

data/portfolio.db — data/ монтируется Docker-томом в проде (docker-compose.prod.yml),
переживает редеплой. sqlite3 stdlib: соединение на вызов, WAL — нагрузка единицы
запросов в минуту, пул не нужен. Все записи в транзакции.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PORTFOLIO_DB", _ROOT / "data" / "portfolio.db"))

_lock = threading.Lock()

# CREATE ... IF NOT EXISTS — идемпотентно и для чистой базы, и для прод-базы, где
# эти таблицы уже созданы прежними миграциями (funds-таблицы, если остались от
# старого модуля, не трогаем — безвредные сироты).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_email TEXT NOT NULL,
  isin TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'floater', -- floater|fixed (путь reprice)
  side TEXT NOT NULL,                    -- buy|sell
  metric TEXT NOT NULL,                  -- price|ytm|dm|gspread
  op TEXT NOT NULL,                      -- '<=' | '>='
  threshold REAL NOT NULL,
  min_volume REAL NOT NULL DEFAULT 0,    -- в volume_unit
  volume_unit TEXT NOT NULL DEFAULT 'bonds', -- bonds|rub
  note TEXT,
  status TEXT NOT NULL DEFAULT 'active', -- active|fired|cancelled
  created_at TEXT NOT NULL,
  fired_at TEXT,
  fired_price REAL,
  fired_volume REAL
);
CREATE INDEX IF NOT EXISTS ix_alerts_user ON alerts(user_email, status);
CREATE INDEX IF NOT EXISTS ix_alerts_active ON alerts(status, isin);

CREATE TABLE IF NOT EXISTS spread_daily(
  isin TEXT NOT NULL,
  date TEXT NOT NULL,
  kind TEXT NOT NULL,           -- floater|fixed
  price_pct REAL,
  dm_bps REAL,                  -- флоатер: disc margin (вспом.)
  g_spread_bps REAL,            -- фикс: g-спред
  z_bps REAL,                   -- z-спред/z-model
  ytm REAL,
  y_idx REAL,                   -- флоатер: IRR−индекс, bps — ПЕРВИЧНАЯ метрика
  src TEXT,                     -- 'snap' вечерний снапшот | 'honest' as-of бэкфилл (NULL=легаси snap)
  PRIMARY KEY(isin, date)
);
CREATE INDEX IF NOT EXISTS ix_spread_isin ON spread_daily(isin, date);
"""

# аддитивные миграции для прод-базы, где таблица уже создана без новых колонок;
# «duplicate column name» на свежей базе — норма, глотаем
_MIGRATIONS = [
    "ALTER TABLE spread_daily ADD COLUMN y_idx REAL",
    "ALTER TABLE spread_daily ADD COLUMN src TEXT",
]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Создаёт схему alerts/spread_daily (идемпотентно) + аддитивные миграции."""
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        for mig in _MIGRATIONS:
            try:
                conn.execute(mig)
            except sqlite3.OperationalError:
                pass    # колонка уже есть


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
