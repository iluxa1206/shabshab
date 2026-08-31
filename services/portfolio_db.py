"""Общее SQLite-хранилище приложения: сигналы, бары, тиковый архив, дневные
снапшоты спред-метрик. (Раньше здесь жили модуль «Фонды» и алерты по стакану —
удалены; файл остался как шаред-инфра `_connect`/`_lock`/`init_db`.)

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
-- Таблица alerts (алерты по стакану) удалена вместе со слоем 2026-08-20:
-- её роль забрали фильтры вкладки СИГНАЛЫ. На прод-базе строки остаются
-- безвредной сиротой — DROP не делаем, чтобы не терять историю пользователя.

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

-- Дневная свёртка часовых баров: средневзвешенная цена дня и спред по ней,
-- плюс закрытие дня и спред по закрытию. ЧИСТАЯ АГРЕГАЦИЯ bar_hourly — ни сети,
-- ни солвера: цена и спред каждого часа уже посчитаны и проштампованы версией
-- движка. Отдельная таблица, а не GROUP BY на чтении, чтобы витрины (СРАВНЕНИЕ)
-- брали готовые числа: свёртка сотни бумаг × 400 дней на каждый запрос — это
-- миллион строк bar_hourly через сортировку.
-- Идемпотентно: день пересобирается, только если его нет, если он посчитан
-- прошлой версией метрик или если в часах прибавилось оборота (дозалив хвоста).
CREATE TABLE IF NOT EXISTS bar_daily(
  isin TEXT NOT NULL,
  date TEXT NOT NULL,           -- 'YYYY-MM-DD' МСК
  kind TEXT NOT NULL DEFAULT 'floater',
  wap_pct REAL,                 -- средневзвешенная чистая цена дня (вес — оборот часа)
  close_pct REAL,               -- цена закрытия дня (последний час с ценой)
  y_idx_wap_bps REAL,           -- Y-IDX по средневзвесу дня (флоатеры)
  y_idx_close_bps REAL,         -- Y-IDX по закрытию дня (флоатеры)
  -- у фиксов метрика к погашению другая — g-спред; держим её отдельными
  -- колонками, а не в y_idx_*: имя должно говорить, что внутри
  g_spread_wap_bps REAL,
  g_spread_close_bps REAL,
  volume REAL, value REAL,      -- бумаг / рублей за день
  trades INTEGER,
  hours INTEGER,                -- сколько часов с оборотом свёрнуто (диагностика)
  -- горизонт дня и спред ко ВТОРОМУ горизонту: без них медианную линию по
  -- средневзвесу строить нельзя (см. _one_horizon в api/routes/history)
  horizon TEXT,
  y_idx_alt_wap_bps REAL,
  alt_horizon TEXT,
  metrics_ver INTEGER,          -- версия движка спреда (см. bars.BARS_METRICS_VERSION)
  built_at TEXT,
  PRIMARY KEY(isin, date)
);
CREATE INDEX IF NOT EXISTS ix_bard_isin ON bar_daily(isin, date);
CREATE INDEX IF NOT EXISTS ix_bard_date ON bar_daily(date);

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

-- Крупные сделки по ВСЕМ облигациям MOEX (не только по юниверсу): безадресные
-- режимы (market=bonds) и адресные/РПС (market=ndm — PSOB/PTOB/PSAU/PSBB/...).
-- Источник — ISS, сквозная лента всего рынка: 271k безадресных сделок за день
-- отфильтровываются порогом BLOCK_MIN_VALUE_RUB и в базу попадают сотни строк.
-- Отдельно от trade_tick: тот льётся из Alor по одной бумаге, знает только
-- безадресные борды (TQCB/TQOB/TQRD) и подчищается ретеншеном.
CREATE TABLE IF NOT EXISTS block_trade(
  trade_id INTEGER PRIMARY KEY,   -- TRADENO MOEX, сквозной по всем рынкам
  isin TEXT NOT NULL,             -- по SECID из справочника; для ОФЗ ≠ secid
  secid TEXT NOT NULL,
  ts TEXT NOT NULL,               -- 'YYYY-MM-DD HH:MM:SS' МСК
  market TEXT NOT NULL,           -- bonds (безадресные) | ndm (адресные/РПС)
  board TEXT,
  price REAL,                     -- % номинала
  qty REAL,                       -- бумаг
  value REAL NOT NULL,            -- руб, без НКД
  yld REAL,                       -- доходность сделки (YIELD ISS)
  side TEXT,                      -- buy|sell — агрессор; у адресных сделок NULL
  face REAL,
  cur TEXT,                       -- валюта расчётов (SUR/CNY/USD): VALUE в НЕЙ,
                                  -- поэтому суммы в статистике считаем по SUR
  -- Спред по ЦЕНЕ СДЕЛКИ (только флоатеры). Считается один раз, когда сделка
  -- приезжает в архив: модель тогда актуальна, а считать на лету при чтении
  -- ленты нельзя — прогрев контекстов сотни выпусков занимает минуту.
  y_idx_bps REAL,
  dm_bps REAL,
  metrics_at TEXT,                -- когда посчитали (NULL = ещё не считали)
  -- Очередь колокольчика. Раньше её держал водяной знак по TRADENO, но с
  -- приходом живых тиков Alor (market=bonds приезжает СРАЗУ, ndm — из ISS
  -- через 15 минут) сквозной номер перестал быть монотонным по времени
  -- записи: знак, сдвинутый живой сделкой, навсегда перепрыгивал бы через
  -- ISS-строки с меньшим TRADENO. Поэтому флаг на строке.
  alerted INTEGER NOT NULL DEFAULT 0,
  ins_at INTEGER                  -- unixtime записи (НЕ время сделки): по нему
                                  -- отсекается бэкфилл, у которого ts старый
);
CREATE INDEX IF NOT EXISTS ix_block_ts ON block_trade(ts);
CREATE INDEX IF NOT EXISTS ix_block_isin_ts ON block_trade(isin, ts);
CREATE INDEX IF NOT EXISTS ix_block_value ON block_trade(value);
-- Крупные сделки за период: value первым, ts вторым. Лента с порогом («весь
-- рынок от 10 млн за год») по (isin, ts) читала ~1,3 млн строк и фильтровала
-- сумму построчно — 5с на запрос; здесь порог сразу отсекает всё лишнее
-- (сделок ≥10 млн во всём архиве 2695), а ts внутри уже упорядочен.
CREATE INDEX IF NOT EXISTS ix_block_value_ts ON block_trade(value, ts);

-- Отмеченные сделки («красный флажок» в ленте), per-user. Со СНИМКОМ строки:
-- trade_tick подчищается ретеншеном (мелочь старше TICK_RAW_DAYS удаляется), и
-- ссылка по одному trade_id через месяц указывала бы в пустоту. Снимок делает
-- список флагов самодостаточным — отмеченная сделка не исчезает никогда.
CREATE TABLE IF NOT EXISTS trade_flag(
  user_email TEXT NOT NULL,
  trade_id INTEGER NOT NULL,      -- TRADENO MOEX (== id сделки Alor)
  isin TEXT NOT NULL,
  ts TEXT NOT NULL,               -- 'YYYY-MM-DD HH:MM:SS' МСК
  price REAL, qty REAL, value REAL,
  side TEXT, board TEXT, market TEXT, cur TEXT,
  y_idx_bps REAL, yld REAL,
  note TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(user_email, trade_id)
);
CREATE INDEX IF NOT EXISTS ix_trade_flag_user ON trade_flag(user_email, ts DESC);

-- Дневные агрегаты РПС (ISS history market=ndm). Поштучных адресных сделок за
-- прошлые дни ISS не отдаёт вообще — только «бумага/борд/день: оборот, число
-- сделок, средневзвес». Нужны, чтобы видеть блоки ДО запуска поштучного сбора.
CREATE TABLE IF NOT EXISTS block_day(
  isin TEXT NOT NULL,
  date TEXT NOT NULL,             -- 'YYYY-MM-DD'
  board TEXT NOT NULL,
  secid TEXT,
  numtrades INTEGER,
  value REAL,                     -- руб за день по этому борду
  waprice REAL,
  close REAL,
  volume REAL,                    -- бумаг
  face REAL,
  PRIMARY KEY(isin, date, board)
);
CREATE INDEX IF NOT EXISTS ix_block_day_date ON block_day(date);

-- Дневной итог БЕЗАДРЕСНЫХ торгов по бумаге и борду (ISS history, весь рынок).
-- Зачем отдельно от поштучных сделок: живой поток пишет тик по каждой бумаге
-- юниверса, а по остальному рынку — только от порога TRADES_STREAM_MIN_RUB, и
-- оборот таких выпусков в ленте занижен (замер 2026-08-28: 20,6 против 22,4
-- млрд ₽ по рынку вне витрин; у RU000A10FNA0 0,2 млн вместо 46,3 — весь его
-- оборот набран сделками мельче порога). Полный поток всего рынка в тиках —
-- это кратный рост архива на тесном диске VPS, а дневной итог биржи стоит
-- десятки строк на бумагу в день и даёт ровно недостающее число.
-- value — В РУБЛЯХ: на валютных бордах биржа отдаёт объём в валюте расчётов,
-- домножаем на курс дня (services/fx), исходную валюту храним в cur.
CREATE TABLE IF NOT EXISTS bond_day(
  isin TEXT NOT NULL,
  date TEXT NOT NULL,             -- 'YYYY-MM-DD'
  board TEXT NOT NULL,
  secid TEXT,
  numtrades INTEGER,
  value REAL,                     -- руб за день по этому борду
  waprice REAL,
  close REAL,
  volume REAL,                    -- бумаг
  face REAL,
  cur TEXT,                       -- валюта расчётов борда (SUR — рублёвый)
  PRIMARY KEY(isin, date, board)
);
CREATE INDEX IF NOT EXISTS ix_bond_day_date ON bond_day(date);

-- Курсор сквозной ленты: последний вычитанный TRADENO по рынку. Протухший
-- курсор (вчерашний номер) безопасен — ISS на неизвестный tradeno отдаёт ленту
-- с начала сессии, то есть деградирует до полного прохода, а не до дыры.
CREATE TABLE IF NOT EXISTS block_cursor(
  market TEXT PRIMARY KEY,        -- bonds | ndm
  last_tradeno INTEGER NOT NULL,
  session_date TEXT,
  updated_at TEXT NOT NULL
);

-- Курс валюты к рублю ПО ДНЯМ. Нужен там, где считается рублёвая величина за
-- прошлую дату: объём сделки по бумаге с валютным номиналом (цена — процент от
-- FACEVALUE, а тот у замещаек в долларах/юанях). Сегодняшний курс для этого не
-- годится: исторический дрейн им завышал объём на всё движение курса с даты
-- сделки (замер 2026-08-31: пересчёт окна 11–31.08 биржевым VALUE отнял
-- 1,59 млрд ₽). Живой срез (services/fx) хранит только «сейчас» — здесь его
-- дневная фиксация плюс история ЦБ.
CREATE TABLE IF NOT EXISTS fx_rate(
  date TEXT NOT NULL,             -- 'YYYY-MM-DD'
  ccy TEXT NOT NULL,              -- USD | EUR | CNY
  rate REAL NOT NULL,             -- рублей за единицу валюты
  source TEXT,                    -- tom (MOEX) | cbr
  updated_at TEXT NOT NULL,
  PRIMARY KEY(date, ccy)
);

-- Телеграм-чаты, привязанные к веб-аккаунтам. Своей настройки у бота нет:
-- он лишь дублирует алерты и сигналы владельца email в чат. /start заводит
-- строку в статусе pending, привязку делает админ на сайте (status=approved,
-- email заполнен). Один аккаунт — сколько угодно чатов (телефон + десктоп).
CREATE TABLE IF NOT EXISTS tg_users(
  tg_user_id INTEGER PRIMARY KEY,
  chat_id    INTEGER NOT NULL,
  username   TEXT,
  muted      INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  email       TEXT,                              -- веб-аккаунт (NULL до одобрения)
  status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
  approved_at TEXT,
  approved_by TEXT,
  emoji       TEXT                               -- свои маркеры строк, JSON (см. tg_users.icons)
);
-- индекс по email — в _MIGRATIONS: на старой базе колонки ещё нет, а
-- CREATE TABLE IF NOT EXISTS её не добавит (упало бы прямо здесь)

-- Вкладка СИГНАЛЫ: те же фильтры скринера, но владелец — веб-аккаунт, а
-- доставка идёт в браузер (WS-пуш + тост/уведомление). Условия фильтра
-- общие с ботом, см. services/screener_core.py.
CREATE TABLE IF NOT EXISTS signal_filters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_email TEXT NOT NULL,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  params_json TEXT NOT NULL,
  cooldown_min INTEGER NOT NULL DEFAULT 60,   -- ЛЕГАСИ: заменён на change_pct
  sound INTEGER NOT NULL DEFAULT 1,      -- звук при срабатывании
  desktop INTEGER NOT NULL DEFAULT 1,    -- системное уведомление браузера
  change_pct REAL NOT NULL DEFAULT 10,   -- порог «шевеления» метрики, %
  kind TEXT NOT NULL DEFAULT 'book',     -- book — стакан | block — крупная сделка
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_signal_filters_user ON signal_filters(user_email);

-- Куда доставлять сигналы КРОМЕ личных чатов: группы и каналы, куда добавлен
-- бот. Отдельная таблица от tg_users: там идентичность человека (заявка,
-- одобрение админом, mute), здесь — просто адрес доставки, принадлежащий
-- аккаунту. Фильтр ссылается сюда через signal_filters.tg_target_id: «Р5» шлём
-- в канал «Р5», «Ф5» — в другой, а без ссылки всё идёт в личку, как раньше.
CREATE TABLE IF NOT EXISTS tg_targets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_email TEXT NOT NULL,
  chat_id INTEGER NOT NULL,      -- у групп и каналов отрицательный
  title TEXT NOT NULL,
  kind TEXT NOT NULL,            -- channel | group | private
  created_at TEXT NOT NULL,
  UNIQUE(user_email, chat_id)
);
CREATE INDEX IF NOT EXISTS ix_tg_targets_user ON tg_targets(user_email);

-- Текущее состояние набора: последние метрики бумаги, попадающей под фильтр.
-- Ровно с ним сравнивается каждый тик, чтобы поймать шевеление метрики.
-- Строка ПЕРЕЖИВАЕТ выход бумаги из набора (см. last_seen_at): стакан
-- подрагивает вокруг границы фильтра, и мгновенное забывание превращало
-- каждое возвращение в новое событие «заявка».
CREATE TABLE IF NOT EXISTS signal_state(
  filter_id INTEGER NOT NULL,
  isin TEXT NOT NULL,
  val_bps REAL,
  price REAL,
  money_rub REAL,
  money_ok_rub REAL,      -- деньги стороны В ГРАНИЦАХ спреда фильтра
  last_seen_at TEXT,      -- когда бумага последний раз была В НАБОРЕ
  last_event_at TEXT,     -- когда по ней последний раз звонило (кулдаун)
  last_reason TEXT,       -- по какой причине звонило: кулдаун гасит ПОВТОР ТОЙ ЖЕ
  updated_at TEXT NOT NULL,
  PRIMARY KEY(filter_id, isin)
);

-- Лента событий: история срабатываний, много записей на одну бумагу
-- (появилась → цена ушла на 10% → объём вырос). Отдельно от состояния:
-- состояние забывается при выходе из набора, история остаётся.
CREATE TABLE IF NOT EXISTS signal_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filter_id INTEGER NOT NULL,
  user_email TEXT NOT NULL,
  isin TEXT NOT NULL,
  name TEXT,
  side TEXT,
  val_bps REAL,                          -- спред по средневзвесу набора
  price REAL,                            -- средневзвешенная цена набора
  money_rub REAL,                        -- фактически набранные деньги
  want_money_rub REAL,                   -- сколько просили набрать (для подсветки)
  levels INTEGER,                        -- уровней стакана в наборе
  single_px REAL,                        -- цена крупной заявки (режим single)
  reason TEXT,                           -- new | price | spread | money
  prev_val_bps REAL, prev_price REAL, prev_money_rub REAL,
  fired_at TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_signal_events_user ON signal_events(user_email, fired_at);
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
    "ALTER TABLE bar_hourly ADD COLUMN metrics_ver INTEGER",
    # горизонт бара и спред ко второму горизонту: свитчер «погашение ↔ оферта»
    # на графике переключает готовые числа (см. bars.BARS_METRICS_VERSION=6)
    "ALTER TABLE bar_hourly ADD COLUMN horizon TEXT",
    "ALTER TABLE bar_hourly ADD COLUMN y_idx_alt_bps REAL",
    "ALTER TABLE bar_hourly ADD COLUMN alt_horizon TEXT",
    "ALTER TABLE spread_daily ADD COLUMN horizon TEXT",
    "ALTER TABLE spread_daily ADD COLUMN y_idx_alt REAL",
    "ALTER TABLE spread_daily ADD COLUMN alt_horizon TEXT",
    "ALTER TABLE signal_filters ADD COLUMN change_pct REAL NOT NULL DEFAULT 10",
    # signal_hits (лента+анти-спам одной таблицей) заменена парой
    # signal_state/signal_events; старая остаётся безвредной сиротой
    "DROP TABLE IF EXISTS signal_hits",
    "ALTER TABLE signal_events ADD COLUMN single_px REAL",
    # спред сделки: считается демоном при приходе, не при чтении ленты
    "ALTER TABLE block_trade ADD COLUMN y_idx_bps REAL",
    "ALTER TABLE block_trade ADD COLUMN dm_bps REAL",
    "ALTER TABLE block_trade ADD COLUMN metrics_at TEXT",
    # тип фильтра: book — условия по стакану (исторический, потому DEFAULT),
    # block — крупная сделка в ленте (см. services/block_trades.notify_blocks)
    "ALTER TABLE signal_filters ADD COLUMN kind TEXT NOT NULL DEFAULT 'book'",
    "ALTER TABLE bar_daily ADD COLUMN g_spread_wap_bps REAL",
    "ALTER TABLE bar_daily ADD COLUMN g_spread_close_bps REAL",
    # спред у ТИКА Alor: раньше считался только для block_trade (ISS), из-за чего
    # свежая безадресная сделка висела с прочерком ~15 минут — ровно до того, как
    # та же сделка приедет из ISS. Тик приходит сразу, поэтому считаем и по нему.
    "ALTER TABLE trade_tick ADD COLUMN y_idx_bps REAL",
    "ALTER TABLE trade_tick ADD COLUMN dm_bps REAL",
    "ALTER TABLE trade_tick ADD COLUMN metrics_at TEXT",
    # очередь расчёта ходит по (metrics_at, value) — без индекса это фулскан
    # многомиллионной таблицы на каждом такте демона
    "CREATE INDEX IF NOT EXISTS ix_tick_unpriced ON trade_tick(metrics_at, value)",
    # привязка телеграм-чата к веб-аккаунту вместо автономной идентичности бота
    # (см. services/tg_users.py). Прод-строки поднимаются как pending — админ
    # одобряет и выбирает email, тогда же старые алерты 'tg:<id>' переезжают.
    "ALTER TABLE tg_users ADD COLUMN email TEXT",
    "ALTER TABLE tg_users ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE tg_users ADD COLUMN approved_at TEXT",
    "ALTER TABLE tg_users ADD COLUMN approved_by TEXT",
    "CREATE INDEX IF NOT EXISTS ix_tg_users_email ON tg_users(email)",
    # свои эмодзи-маркеры чата (команда бота /custom): JSON {слот: эмодзи}
    "ALTER TABLE tg_users ADD COLUMN emoji TEXT",
    # адресат доставки фильтра: канал/группа из tg_targets (NULL — личные чаты)
    "ALTER TABLE signal_filters ADD COLUMN tg_target_id INTEGER",
    # свой скринер бота удалён: фильтры живут на сайте (вкладка СИГНАЛЫ),
    # бот получает их события копией
    "DROP TABLE IF EXISTS tg_filters",
    "DROP TABLE IF EXISTS tg_filter_hits",
    # режим торгов у события-сделки: в ленте «блок на 600 млн» без пометки
    # адресный/биржевой читается как принт по стакану, а это разные новости
    "ALTER TABLE signal_events ADD COLUMN board TEXT",
    "ALTER TABLE signal_events ADD COLUMN negotiated INTEGER",
    # антидребезг сигналов стакана: состояние переживает выход из набора,
    # объём меряется деньгами в границах спреда фильтра (см. services/signals)
    "ALTER TABLE signal_state ADD COLUMN money_ok_rub REAL",
    "ALTER TABLE signal_state ADD COLUMN last_seen_at TEXT",
    "ALTER TABLE signal_state ADD COLUMN last_event_at TEXT",
    "ALTER TABLE signal_state ADD COLUMN last_reason TEXT",
    "ALTER TABLE signal_events ADD COLUMN money_ok_rub REAL",
    "ALTER TABLE signal_events ADD COLUMN prev_money_ok_rub REAL",
    # деньги ПО ЦЕНЕ СИГНАЛА (весь уровень стакана, а не набранный лимит и не
    # сумма стороны) — то, что показывается человеку; см. screener_core
    "ALTER TABLE signal_events ADD COLUMN level_money_rub REAL",
    # заявка стоит ПЕРВОЙ в очереди своей стороны: в уведомлении вместо счёта
    # уровней набора (см. screener_core: best)
    "ALTER TABLE signal_events ADD COLUMN best INTEGER",
    "CREATE INDEX IF NOT EXISTS ix_block_value_ts ON block_trade(value, ts)",
    # очередь колокольчика флагом вместо водяного знака по TRADENO (см. схему
    # block_trade). Старым строкам ins_at остаётся NULL — они в очередь не
    # попадают, то есть миграция не вываливает архив в уведомления.
    "ALTER TABLE block_trade ADD COLUMN alerted INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE block_trade ADD COLUMN ins_at INTEGER",
    "CREATE INDEX IF NOT EXISTS ix_block_alert ON block_trade(ins_at) "
    "WHERE alerted = 0",
    # ГОРИЗОНТ в дневной свёртке баров. Часы его несут с BARS_METRICS_VERSION=6,
    # а свёртка теряла — и медианные линии аналитики нельзя было строить по
    # средневзвесу: без горизонта спред к оферте и спред к погашению сложились
    # бы в одну линию (обвал на 220 б.п. без движения цены, см. _one_horizon).
    "ALTER TABLE bar_daily ADD COLUMN horizon TEXT",
    "ALTER TABLE bar_daily ADD COLUMN y_idx_alt_wap_bps REAL",
    "ALTER TABLE bar_daily ADD COLUMN alt_horizon TEXT",
]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Создаёт схему (идемпотентно) + аддитивные миграции."""
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        for mig in _MIGRATIONS:
            try:
                conn.execute(mig)
            except sqlite3.OperationalError:
                pass    # колонка уже есть


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
