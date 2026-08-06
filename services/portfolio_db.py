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
  y_idx REAL,                   -- флоатер: IRR − роллирование RUONIA, bps — ПЕРВИЧНАЯ метрика
  src TEXT,                     -- 'snap' вечерний снапшот | 'honest' as-of бэкфилл (NULL=легаси snap)
  engine_ver INTEGER,           -- версия as-of движка для honest-строк (см. backdate.HONEST_ENGINE_VERSION)
  PRIMARY KEY(isin, date)
);
CREATE INDEX IF NOT EXISTS ix_spread_isin ON spread_daily(isin, date);

-- Часовые бары: средневзвешенная цена часа + спред по ней. Источник цены —
-- часовые свечи MOEX ISS (vwap = value/volume/face), метрики — reprice текущей
-- моделью бумаги (оценка формы динамики, не точный as-of; см. services/bars.py).
-- buy_*/sell_* дозаполняются из тикового архива (Alor даёт агрессора).
CREATE TABLE IF NOT EXISTS bar_hourly(
  isin TEXT NOT NULL,
  ts TEXT NOT NULL,             -- 'YYYY-MM-DD HH:00' МСК, начало часа
  kind TEXT NOT NULL DEFAULT 'floater',
  open REAL, high REAL, low REAL, close REAL,
  vwap_pct REAL,                -- средневзвешенная ЧИСТАЯ цена, % номинала
  volume REAL,                  -- бумаг
  value REAL,                   -- руб, без НКД
  face REAL,                    -- номинал на дату бара (амортизация)
  y_idx_bps REAL,               -- спред по vwap: ПЕРВИЧНАЯ метрика флоатера
  dm_bps REAL,
  g_spread_bps REAL,            -- фикс
  ytm REAL,
  -- Спред по КАЖДОЙ цене бара (тот же reprice, что y_idx_bps, но от open/high/
  -- low/close). Нужен, чтобы свеча спреда была полноценной: раньше OHLC собирался
  -- из vwap соседних часов, и в день с одной-двумя торговавшими часами свеча
  -- вырождалась в палку. Спред обратен цене: y_high — спред по МАКСИМАЛЬНОЙ цене
  -- (то есть минимальный спред дня), поэтому экстремумы берутся max/min по всем
  -- четырём, а не по именам полей.
  y_open_bps REAL, y_high_bps REAL, y_low_bps REAL, y_close_bps REAL,
  trades INTEGER,               -- число сделок в часе (из тиков)
  buy_volume REAL, sell_volume REAL, buy_vwap REAL, sell_vwap REAL,
  src TEXT,                     -- 'candle' | 'ticks'
  PRIMARY KEY(isin, ts)
);
CREATE INDEX IF NOT EXISTS ix_bar_isin ON bar_hourly(isin, ts);

-- Прогресс фоновых задач (см. services/progress.py). В базе, а не в памяти:
-- бэкфиллы запускаются отдельным процессом и иначе не были бы видны, плюс после
-- рестарта видно, что задача оборвалась, а не молча исчезла.
CREATE TABLE IF NOT EXISTS job_progress(
  key TEXT PRIMARY KEY,         -- 'bars_refresh', 'warmup', …
  label TEXT NOT NULL,          -- человекочитаемое имя для страницы СТАТУС
  done INTEGER DEFAULT 0,
  total INTEGER,                -- NULL — задача без известного объёма
  state TEXT,                   -- running | done | failed
  detail TEXT,
  started_at TEXT, updated_at TEXT, finished_at TEXT,
  pid INTEGER                   -- чей процесс: API или разовый скрипт
);

-- Тиковый архив сделок (Alor alltrades). Копится вперёд: у брокера глубина
-- ~30 календарных дней, дальше история существует только у нас.
CREATE TABLE IF NOT EXISTS trade_tick(
  isin TEXT NOT NULL,
  trade_id INTEGER NOT NULL,    -- id сделки Alor (== TRADENO MOEX)
  ts TEXT NOT NULL,             -- 'YYYY-MM-DD HH:MM:SS' МСК
  price REAL NOT NULL,          -- % номинала
  qty REAL NOT NULL,            -- бумаг
  value REAL,                   -- руб, без НКД
  side TEXT,                    -- buy|sell — агрессор
  board TEXT,
  PRIMARY KEY(isin, trade_id)
);
CREATE INDEX IF NOT EXISTS ix_tick_isin_ts ON trade_tick(isin, ts);
CREATE INDEX IF NOT EXISTS ix_tick_big ON trade_tick(isin, value);
-- Общерыночная лента (вкладка СДЕЛКИ) ходит по времени БЕЗ isin: без этого
-- индекса каждый запрос — фулскан миллионов строк с сортировкой.
CREATE INDEX IF NOT EXISTS ix_tick_ts ON trade_tick(ts);

-- Водяной знак инкрементального дрейна: до какого момента история сделок бумаги
-- уже вычитана из Alor ЦЕЛИКОМ. Живёт ОТДЕЛЬНО от trade_tick, потому что
-- ретеншен вычищает старые мелкие тики — вывести точку старта из самих тиков
-- значило бы каждый раз качать заново всё, что только что удалили.
CREATE TABLE IF NOT EXISTS tick_drain(
  isin TEXT PRIMARY KEY,
  last_ts TEXT NOT NULL,        -- 'YYYY-MM-DD HH:MM:SS' МСК, правая граница
  updated_at TEXT NOT NULL
);
"""

# аддитивные миграции для прод-базы, где таблица уже создана без новых колонок;
# «duplicate column name» на свежей базе — норма, глотаем
_MIGRATIONS = [
    "ALTER TABLE spread_daily ADD COLUMN y_idx REAL",
    "ALTER TABLE spread_daily ADD COLUMN src TEXT",
    "ALTER TABLE spread_daily ADD COLUMN engine_ver INTEGER",
    "ALTER TABLE bar_hourly ADD COLUMN y_open_bps REAL",
    "ALTER TABLE bar_hourly ADD COLUMN y_high_bps REAL",
    "ALTER TABLE bar_hourly ADD COLUMN y_low_bps REAL",
    "ALTER TABLE bar_hourly ADD COLUMN y_close_bps REAL",
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
