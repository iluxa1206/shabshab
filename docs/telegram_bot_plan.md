# Телеграм-бот: оповещения со стаканами + Mini App

Статус: план (2026-08-11). Решения пользователя: все три типа оповещений
(пер-ISIN алерты, скринер по рынку, дайджесты), рендер картинок Pillow на бэке,
Mini App — отдельная лёгкая страница, транспорт — webhook через Caddy.
**Идентичность — только Telegram**: алерты бота свои, пользователь заводит их
в боте/Mini App, привязки к веб-сессии дашборда нет (пока). Ключ пользователя
везде `tg_user_id`; в общей таблице alerts — `user_email = 'tg:<tg_user_id>'`
(движок alerts_monitor одинаково обрабатывает всех активных, разделение
только на уровне CRUD).

## Что уже есть (опора)

| Слой | Где | Что даёт боту |
|---|---|---|
| Алерты per-user | `services/alerts.py`, воркер `alerts_monitor` (`api/main.py:396`) | Событие fired с price/volume — точка врезки нотификатора |
| Стаканы live | `market_cache["ob_live"]` (WS-пуш) + `fetch_alor_orderbook_snapshot` | Данные для картинки в момент срабатывания |
| Глубина универса | `services/depth.py` (20 уровней, батч-поллер) | Данные для скринера по объёму в стакане |
| Per-level метрики | `services/orderbook_svc.build_metrics_fn` | Y-IDX/DM/YTM на уровнях — те же цифры, что на панели стакана |
| Auth по email | `auth.py`, `api/routes/auth.py`, `require_user` | Аккаунт, к которому привязывается Telegram |
| SQLite-инфра | `services/portfolio_db.py` (`_connect`/`_lock`/`init_db`) | Новые таблицы туда же, аддитивной миграцией |
| Деплой | VPS + Docker + Caddy path-route (см. vacation-tracker) | Роут `/tg/webhook` без нового TLS |

## Архитектура

```
Telegram ──POST──▶ /api/tg/webhook (проверка secret-token) ──▶ handlers
                                                                 /start → регистрация chat_id
                                                                 кнопка Mini App

alerts_monitor ──fired──▶ tg_notify.enqueue(alert, snap, metrics_fn)
screener_worker ──match──▶      │  (asyncio.Queue, отдельный consumer —
digest_worker  ──cron───▶       │   рендер+HTTP не тормозят мониторы)
                                ▼
                     services/tg_render.py (Pillow → PNG)
                                ▼
                     Bot API sendPhoto (httpx, ретраи, rate-limit)

Mini App (t.me кнопка) ──▶ /tg-app (лёгкая страница)
    initData ──▶ POST /api/tg/auth (HMAC-валидация) ──▶ tg_user_id → сессия
    далее /api/tg/alerts (CRUD поверх services.alerts, юзер 'tg:<id>'),
    /api/tg/filters, /api/tg/digest
```

Бот «тонкий»: без aiogram — команд мало (`/start`, `/mute`, `/status`), вся
настройка в Mini App. Клиент Bot API — httpx-обёртка `services/telegram.py`.

## Схема БД (portfolio.db, аддитивно в `init_db`)

```sql
CREATE TABLE IF NOT EXISTS tg_users(         -- регистрируется на /start
  tg_user_id INTEGER PRIMARY KEY,
  chat_id    INTEGER NOT NULL,
  username   TEXT,
  muted      INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS tg_filters(       -- скринер-фильтры
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_user_id INTEGER NOT NULL,
  name TEXT, enabled INTEGER DEFAULT 1,
  params_json TEXT NOT NULL,     -- {y_idx_min, rating_min, vol_rub_min, mat_window, ...}
  cooldown_min INTEGER DEFAULT 240,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS tg_filter_hits(   -- анти-спам: что уже слали
  filter_id INTEGER, isin TEXT, fired_at TEXT,
  PRIMARY KEY(filter_id, isin)
);
CREATE TABLE IF NOT EXISTS tg_digests(
  tg_user_id INTEGER PRIMARY KEY,
  schedule TEXT,                 -- 'morning' | 'evening' | 'both'
  params_json TEXT               -- фильтр топа (те же поля, что tg_filters)
);
```

## Компоненты (новые файлы)

- `services/telegram.py` — httpx-клиент Bot API: `send_message`, `send_photo`,
  `set_webhook`; ретраи на 429/5xx, уважение `retry_after`.
- `services/tg_render.py` — Pillow-рендер стакана: шапка (имя, ISIN, время МСК),
  две лесенки bid/ask с барами объёма, per-level Y-IDX (первичная) + DM/YTM,
  строка срабатывания подсвечена. Стиль под тёмную панель стакана дашборда.
- `services/tg_notify.py` — очередь + consumer; формирует caption
  (`⚡ RU000A10XXXX Имя — buy Y-IDX ≥ 250: 262 бп, объём 1.2 млн ₽`),
  зовёт рендер, шлёт фото; ошибки логирует, алерт-механику не ломает.
  Доставляет только алерты с `user_email` вида `tg:<id>` (chat_id из tg_users);
  веб-алерты живут как раньше, без Telegram.
- `services/tg_screener.py` — воркер: раз в N минут прогоняет enabled-фильтры
  по снапшоту универса (метрики уже в `market_cache`) + `depth.get_depth()`
  для объёмного порога; анти-спам через `tg_filter_hits` + cooldown; повторно
  шлёт, только если бумага вышла из условия и вернулась (гистерезис).
- `services/tg_digest.py` — cron-воркер (мск-время, торговые дни): топ-N по
  фильтру пользователя, движения за день; одно сообщение, без картинок стаканов
  (опционально мини-таблица картинкой позже).
- `api/routes/tg.py` — `/api/tg/webhook` (проверка
  `X-Telegram-Bot-Api-Secret-Token`), `/api/tg/auth` (initData HMAC →
  tg_user_id → сессия), CRUD `/api/tg/alerts` (тонкая обёртка над
  `services.alerts` с `user_email='tg:<id>'`), `/api/tg/filters`,
  `/api/tg/digest`. Эти роуты вне `require_user` — своя initData-зависимость.
- Mini App: `frontend-react` — маленькая страница `/tg-app` (свой entrypoint по
  образцу `*-test-main.jsx`): список алертов и фильтров, создание, вкл/выкл,
  mute. Telegram WebApp JS SDK, тема из `themeParams`.

## Регистрация и auth

1. `/start` в боте → upsert `tg_users(tg_user_id, chat_id)` → приветствие +
   кнопка Mini App. Никакой привязки к веб-аккаунту.
2. Mini App auth: `initData` → HMAC-SHA256 с ключом из bot token (валидация по
   спеке Telegram, проверка свежести `auth_date`) → `tg_user_id`. Каждый запрос
   Mini App несёт initData в заголовке (`Authorization: tma <initData>`),
   бэкенд валидирует — серверная сессия не обязательна.
3. Доступ к боту ограничить allowlist'ом tg_user_id (env `TG_ALLOWED_IDS`) —
   дашборд приватный, бот тоже; чужой /start получает отказ.
4. Привязка tg ↔ веб-аккаунт — позже, отдельной фазой, если понадобится
   (миграция тривиальна: `user_email 'tg:<id>' → email` одним UPDATE).

## Врезка в alerts_monitor

В `api/main.py` после `mark_fired(...)`: неблокирующий
`tg_notify.enqueue(a, snap, metrics_fn, face)` — снапшот и metrics_fn в этот
момент уже на руках, рендер получает ровно тот стакан, на котором сработало.
Никакого повторного I/O.

## Деплой

- Env: `TG_BOT_TOKEN`, `TG_WEBHOOK_SECRET` (в `.env` прода).
- Caddy: маршрут `assetallocator.ru/desk/api/tg/webhook` уже покрыт существующим
  path-route `/desk/*` — отдельного не нужно; проверить только, что webhook-URL
  в setWebhook указывает на полный путь.
- `scripts/tg_set_webhook.py` — разовый вызов setWebhook с secret_token.
- Зависимости: `Pillow` в requirements.txt (httpx уже есть).

## Фазы

1. **Ядро доставки**: telegram.py + tg_users + webhook (/start) + врезка в
   alerts_monitor + tg_render + sendPhoto. Алерты 'tg:<id>' приходят в Telegram
   с картинкой стакана (заводить в фазе 1 можно и командой боту:
   `/alert RU000A10XXXX buy yidx >= 250 vol 1e6`).
2. **Mini App**: страница /tg-app, initData-auth, CRUD алертов.
3. **Скринер**: tg_filters + tg_screener + UI фильтров в Mini App.
4. **Дайджесты**: tg_digests + cron-воркер + настройка расписания в Mini App.

## Открытые вопросы (решить по ходу)

- Rate-limit Telegram: 30 msg/sec глобально, 1 msg/sec на чат — при одном
  пользователе неактуально, но очередь всё равно троттлит.
- Скринер на всём универсе: где брать снапшот метрик — `market_cache` держит
  расчётные строки универса; уточнить ключ при реализации фазы 3.
- Картинка в дайджесте (таблица топа как PNG) — опционально, фаза 4+.
- `/mute` на время торговой сессии vs навсегда — решить в фазе 1 (просто флаг).
