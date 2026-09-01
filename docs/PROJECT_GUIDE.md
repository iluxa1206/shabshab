# Floaters Desk — инструкция к проекту

Аналитическая платформа по рублёвым облигациям: флоатеры (плавающий купон к КС/RUONIA),
фиксы (ОФЗ-ПД + корпораты), кривые свопов, лента сделок, стаканы, сигналы, Telegram-бот.

* Бэкенд: **FastAPI** (Python 3.12+), точка входа `api/main.py`.
* Фронт: **React 18 + Vite + react-router 7**, `frontend-react/`, раздаётся тем же FastAPI под `/app/`.
* Хранилища: **SQLite** — `data/instruments.db` (реестр бумаг), `data/portfolio.db` (алерты,
  бары, тики, блоки, сигналы, tg), `data/users.json` (аккаунты), `data/cache/*.json` (дисковые кэши).
* Прод: `assetallocator.ru/desk` (VPS 161.104.17.23), Docker + Caddy, редеплой `scripts/deploy.sh`.

---

## 1. Быстрый старт

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Бэкенд (dev):

```bash
.venv/bin/python -m uvicorn api.main:app --reload --port 8000
```

Фронт (dev, проксирует `/api` на :8000):

```bash
cd frontend-react && npm install && npm run dev
```

Дев-фронт открывается **только по `/app/`** (`http://localhost:5173/app/`) — на «/» ломается `<base>`.

Тесты:

```bash
.venv/bin/python -m pytest
npm --prefix frontend-react test
```

Прод-сборка и деплой:

```bash
./scripts/deploy.sh
```

`docker-compose.prod.yml` — стек `floaters-prod` в внешней сети `astra-prod_default`, лимит памяти 1.2 GB,
том `./data:/app/data`. Dockerfile — двухстадийный: Node собирает `frontend-react/dist`, Python-рантайм
запускает `uvicorn api.main:app`.

Вспомогательные dev-запуски на отдельных портах (скретч-юзеры, воркеры выключены):
`scripts/dev_api_tables.sh` (:8021), `dev_api_cal.sh`, `dev_api_tape.sh`, `dev_api_blocks.sh`.

---

## 2. Карта репозитория

| Путь | Что внутри |
|---|---|
| `api/main.py` | приложение FastAPI, монтирование роутеров, все фоновые демоны в `lifespan` |
| `api/routes/*.py` | HTTP/WS-эндпоинты по доменам |
| `api/schemas.py` | pydantic-схемы ответов |
| `core/` | мат-ядро: кэшфлоу, солверы (SM/DM/XIRR), кривые, фетч котировок Alor |
| `services/` | доменные сервисы: рыночные данные, реестр, метрики, история, сигналы, tg |
| `frontend-react/src/` | SPA: `components/` — экраны, `charts/` — свой SVG-каркас графиков |
| `scripts/` | CLI-утилиты: бэкфиллы, калибровка спек, деплой, разовые стенды |
| `tests/` | pytest, ~37 файлов |
| `data/` | БД, кэши, аккаунты (переживает редеплой через volume) |
| `docs/` | этот файл, code review, план tg-бота |

---

## 3. Мат-ядро (`core/`)

### `core/valuation.py` — оценка флоатера

Модели данных:
* `BondRefData` — справочные данные выпуска (номинал, база, маржа, даты, периоды).
* `MarketPrice` — котировки.
* `Cashflow` — спроектированный платёж по форвардам.

Даты и номинал:
* `settle_date(calc_date)` — T+1 с пропуском выходных и праздников MOEX.
* `face_for_pricing(face, amorts, calc_date)` — номинал, от которого котируется цена при T+1.
* `generate_coupon_dates(...)`, `generate_coupon_dates_by_period(...)`, `extend_periods_to_maturity(...)`.
* `period_at(periods, d)`, `accrued_at(periods, d)`, `accrue_to_settle(...)` — НКД на дату поставки.
* `dirty_price_rub(face, clean_pct, accrued)`.

Оферты:
* `offer_kind(type_str)` → `call`/`put`; `next_offer_info(...)`, `first_offer_date(...)` (пут),
  `first_call_date(...)`, `offer_price_pct(...)`.

Потоки и солверы:
* `build_cashflows_to_maturity(bond, curve, calc_date, ...)` — единый builder ожидаемых потоков.
* `build_cashflows_with_spread(...)` — то же со сдвигом базы на спред.
* `xnpv`, `xirr`, `xirr_yield_pct` — доходность по неравномерным потокам.
* `pv_cashflows_with_dm(...)` — PV рекурсивным дисконтированием F_i + DM.
* `solve_simple_margin_bps(...)` — **SM** (simple margin), бисекция.
* `solve_discount_margin_bps(...)` — **DM** (market-standard FRN, met_float/Fabozzi).
* `current_index_pct(...)` — текущий уровень базы (КС/RUONIA) для DM.
* `implied_yield_pct(...)`, `ruonia_rolling_yield_pct(...)`.
* `calculate_floater_metrics(bond, price, curve, calc_date)` — полный пайплайн по одной цене
  (используется и для Last/Bid/Ask, и для каждого уровня стакана).
* `FlatForwardCurve` — плоский форвард = текущий индекс; `_RuoniaCompoundPath` — компаундинг
  RUONIA только в рабочие дни.

### `core/forwards.py` — кривые

* `yf_act365`, `add_months`, `get_maturity_date(start, tenor)` (`ON/1W/3M/1Y`).
* `DiscountCurve` — базовый интерфейс: `df(d)`, `forward(t1,t2)`, `daily_forward(d)` (ступень между тенорами).
* `BootstrappedForwardCurve` — честный bootstrap par-свопов (NPV=0 на каждом теноре).
* `SheetForwardCurve` — методика трейдерского листа (используется вкладкой КРИВЫЕ и прайсингом).
* `CurveBootstrapper.bootstrap_ruonia(...)` (OIS, фикс-нога ≤1Y single / годовая),
  `.bootstrap_keyrate(...)` (IRS, фикс-нога квартальная).

> **ДВЕ КОНВЕНЦИИ KEYRATE — ЭТО НАМЕРЕННО** (решение 2026-08-26).
> `SheetForwardCurve` трактует фикс-ногу как **годовую** (`4·((1+par)^¼−1)`),
> `bootstrap_keyrate` — как **квартальную** (`months=3`, конвенция СПФИ МБ).
> Задачи разные: бутстрап даёт безарбитражные DF, лист воспроизводит
> утверждённую методику вкладки КРИВЫЕ, на которой и прайсятся купоны.
> На длинных тенорах кривые расходятся на 70-90 bps — это ожидаемо.
> Приведение sheet к квартальной ноге сдвинуло бы **Y-IDX всех KEYRATE-бумаг
> на +73…+91 bps** (SM/DM почти не двигаются, RUONIA не двигается вовсе) и
> потребовало бы пересчёта истории. Формула продублирована в
> `api/routes/curves.py::_avg_pct` — править только синхронно.

### `core/rates.py`, `core/cashflow.py`, `core/last_prices.py`, `core/orderbooks.py`

* `get_rates_curves()` — OIS RUONIA + IRS KEYRATE с Cbonds, кэш на день, `load_cache(allow_stale=True)`
  — last-known-good фолбэк.
* `coupon_period_from_coupons(...)` — длина купонного периода из **фактического** графика (не 365/freq).
* `parse_base_and_spread(formula, base_rate)`.
* `get_last_prices_dict(...)`, `get_orderbooks_dict(...)` — батч-снимки Alor одним WS-заходом.

---

## 4. Сервисный слой (`services/`)

### Рыночные данные и кривые

* **`market_data.MarketDataService`** — фасад MOEX ISS + Alor. Ключевое:
  `get_curves()` (bootstrap RUONIA/KEYRATE), `get_gcurve()` (КБД ОФЗ), `get_zspread_ctx()`,
  `fetch_board_snapshot(force)` (TQCB/TQOB/TQRD одним заходом — основной источник цен),
  `fetch_bond_schedule_full(isin)` (bondization: купоны/амортизации/оферты, day-кэш),
  `fetch_moex_securities`, `fetch_security_master`, `fetch_emitter_info`, `fetch_candles`,
  `session_prices()`, `cached_prices(max_age)`, `resolve_secid_board(isin)`, `search_bonds(q)`.
* `cbr.py` — живьём с ЦБ: `ks_history()`, `ruonia_history()`, `ruonia_index_history()`,
  `ks_rate_at(d)`, `current_ks()`, `current_ruonia()`.
* `cbr_forecast.py` — среднесрочный прогноз ЦБ (`cbr_forecast.json`): `meeting_step_path(...)`,
  `key_rate_decision(...)`, `avg_ks_by_year()`, `neutral_pct()`.
* `implied_curve.KsExpectationCurve` — ожидаемая КС(t) из IRS (НРД met_float Прил.3).
* `ks_path.build_path(...)` — факт ЦБ + рыночный форвард для вкладки КРИВЫЕ.
* `curve_history.py` — архив своп-котировок по датам: `save_snapshot`, `quotes_asof(base, d)`.
* `zspread.py` — `ExpCurve`, `GCurve`, `solve_z_bps`, `solve_z_discrete` (методика НРД),
  `compute_z_bps(...)` — наш z-спред флоатера над КБД.
* `fx.py` — курсы валют (TOM/ЦБ).

### Реестр инструментов — источник истины

**`instruments_registry.py`** (`data/instruments.db`, таблицы `instruments`, `discovery_seen`, `enrich_seen`):

* Запись: `upsert(row, source)`, `set_manual(isin, params, lock)`, `apply_authoritative(...)`,
  `reset_manual`, `mark_reviewed`, `set_emitter`, `set_rating`, `set_has_call`, `set_call_dates`,
  `set_exotic`, `reclassify_fixed`, `clear_base`, `set_br_spec(s_bulk)`, `set_spec_backtest`,
  `set_smartlab_type`, `set_margin_check`.
* Чтение: `get(isin)`, `search(q)`, `calc_params_map()` (все расчётные поля одним запросом, кэш),
  `labels_map`, `ratings_map`, `call_dates_map`, `br_specs_all`, `coupon_overrides_all`,
  `universe_rows(...)`, **`fetch_floater_universe()`** — весь юниверс рублёвых флоатеров.
* Очереди качества данных: `list_suspect`, `list_incomplete`, `list_no_spec`, `list_call_unknown`,
  `list_call_dates_missing`, `list_exotic`, `list_spec_mismatch`, `list_sl_mismatch`, `list_unreviewed`,
  `queue_stats()`.
* Дискавери: `discovery_pending`, `mark_discovery_seen`, `enrich_pending`, `mark_enrich_attempt`.
* Жизненный цикл: `sync_active_set(traded)`, `retire_matured(today)`, `normalize_ofz_pk()`.

**`instruments_sync.py`** — ежедневное наполнение: `sync_instruments()`, `discover_floaters(cap)`
(поиск новых флоатеров среди торгуемых MOEX), `infer_missing_params(cap)` (база+маржа из истории купонов).

Обогащение: `enrich_corpbonds.py` (`fetch_corpbonds`, `parse_corpbonds_html`, `enrich_registry`),
`enrich_smartlab.py`, `bondresearch.py` (спека фиксинга: `fetch_specs`, `apply_specs`),
`ratings.py` (`refresh(isins, cap)`, `bucket_of`, `bucket_map`, `rating_to_bucket`),
`ref_data.py` (`params(isin)` — слияние слоёв; `coupon_formula(...)`; приоритет: реестр-ручное > парсер > Cbonds),
`instruments_validate.validate_priceable()`, `smartlab_audit.run()`, `spec_backtest.run()`.

### Купонный фиксинг

**`coupon_calib.py`** — самый чувствительный модуль:
* `parse_prospectus_formula(text)` → `{mode, lag, lag_unit, capped}`.
* `parse_margin_schedule(text)` → лесенка маржи по номерам купонов.
* `fixing_probe_date(spec, start)` — единая точка «на какую дату смотрим индекс».
* `calibrate(...)` — подбор спеки по прошлым купонам; `infer_base_margin(...)`; `looks_fixed_coupons(...)`.
* `period_index_pct(...)` — индекс-компонента начавшегося периода; `projected_ks_pct(...)` —
  факт ЦБ на прошлые дни + форвард на будущие; `compounded_index_bounds(...)` — уровни официального индекса RUONIA.

### Расчётные конвейеры

* `universe.py` — `build_universe_ref`, `enrich_bond(...)` (все метрики по одной бумаге),
  **`compute_universe_metrics(uni, isins, cache_path)`** (фоновый прогон по всему рынку),
  `compute_watch_metrics(...)`, `cross_section_map(...)`.
* `valuation.py` — `calculate_valuation_metrics(...)` (обёртка ядра), `pick_horizon(m, horizon)` —
  выбор горизонта (погашение / пут / колл), см. правило горизонта.
* `bonds.py` — сборка `BondRefData`: `apply_registry_params`, `build_ref_external`,
  `reconcile_face`, `amort_remaining_face`, `next_coupon_after`, `coupons_per_year`.
* `cashflow.py` — `build_cashflow_from_moex(...)`: форматтер таблицы потока карточки.
* `bond_details.py` — `build_bond_details(isin, cache)`, `load_reprice_ctx`, `reprice_at_price`,
  `reprice_bond`, `solve_price_for_yidx(...)` (обратная задача калькулятора).
* `bond_audit.py` — паспорт бумаги: `build_bond_audit`, `coupon_day_rates` (дневная раскладка фиксинга).
* `fixed_income.py` — фиксы: `fetch_fixed_universe()`, `fixed_metrics(...)` (YTM/мод.дюрация/DV01/G-спред),
  `compute_fixed_metrics_all(...)`, `apply_ytm_delta(...)`.
* `metrics.py` — `duration_metrics`, `days_to_refix`, `current_coupon_pct`, `carry_bps`,
  `breakeven_base_pct`, `carry_refix_block`, `rank_pct`.
* `backdate.py` — расчёт **на прошлую дату**: `SplicedAsofCurve`, `build_hybrid_curve`,
  `curve_asof`, `load_backdate_ctx`, `reprice_asof`, `honest_spread_series`, `ensure_honest_backfill`.
* `payments_calendar.build_payments_calendar()`.
* `instruments.classify(isins)` — corp_fix / corp_float / ofz_pd / ofz_pk / fx_*.

### Рыночные потоки и история

* `alor_ws.alor_orderbook_ws()` — персистентный WS Alor по бумагам, которые смотрит фронт.
* `universe_stream.py` — пул сокетов по **всему** юниверсу: `universe_stream_pool()`,
  `metrics_worker()` (событийный пересчёт метрик, такт 5с), `live_isins()`, `depth_stream_covers(n)`.
* `trades_stream.trades_stream_pool()` — безадресные сделки пушем (Alor без задержки).
* `depth.py` — `refresh_depth(isins, chunk)`, `get_depth()` — лестницы стаканов.
* `orderbook_svc.build_metrics_fn(isin, kind)` — `metrics_fn(price)` для per-level метрик стакана.
* `live_quotes.py` — живой дневной VWAP по подписанным бумагам.
* `bars.py` — часовые бары VWAP+спред: `build_bars`, `ensure_bars`, `read_bars`,
  `refresh_universe(days, full, concurrency)`, `resample(bars, hours)`, `adv_map`, `spread_avg_map`.
* `trades_archive.py` — тиковый архив: `drain(isin)`, `read_trades`, `read_tape`, `tape_stats`,
  `enrich_bars_with_ticks`, `prune`, `vacuum`, `db_stats`, `repair_values`, `repair_fx_values`.
  Alor отдаёт только 30 дней — копим сами.
  Объём тика — В РУБЛЯХ: цена идёт в % от номинала, поэтому у замещаек номинал домножается на курс
  валюты номинала (`face_fx_days` — курс ДНЯ СДЕЛКИ из архива `fx_rate`), иначе объём занижался
  в 11–86 раз, а на историческом окне уезжал на движение валюты (USD 03.08 — 80,24 против 85,84
  на 31.08). Промахи по номиналу (амортизация в день события) сверяются с биржевым VALUE по
  TRADENO — `repair_values`, ночным тактом `block_trades_worker` и разово
  `scripts/fix_tick_values.py --all`; где биржевого двойника нет (тиковый архив глубже ISS-ленты) —
  полный пересчёт по номиналу и курсу дня: `repair_fx_values`, `scripts/fx_history.py --repair`.
* `fx.py` — курсы валют: живой срез (MOEX TOM + ЦБ) и АРХИВ ПО ДНЯМ (`fx_rate`): `save_rates`,
  `rate_on(ccy, day)`, `rates_by_day`, `backfill_history(days)` (история MOEX, недостающие валюты —
  ЦБ), `archive_stats`. День фиксируется автоматически при каждом обновлении курса (дебаунс 10 мин),
  дыры за нерабочие дни сервиса добирает ночной такт.
* `block_trades.py` — крупные сделки всего рынка (безадресные + РПС из ISS `market=ndm`):
  `sweep()`, `backfill(days)`, `price_new_trades()`, `read_blocks`, `read_days`, `notify_blocks`, `prune`,
  `board_ccy_map` (валюта расчётов по борду), `backfill_bond_days(days)` — дневные итоги безадресных
  торгов всего рынка в `bond_day`, объём в рублях (валютные борды по курсу дня).
* `tape.py` — единая лента: тики Alor + блоки ISS (`read_tape`, `tape_stats`, `market_turnover`).
  `tape_stats` рядом с оборотом показанных сделок отдаёт `market_value` — ПОЛНЫЙ оборот тех же
  бумаг по дневным итогам биржи (`bond_day`): вне витрин тик пишется от порога потока, и сумма
  сделок там заведомо неполна. Под фильтрами по сумме/стороне/режиму поле пустое — сравнивать
  отфильтрованное с полным нельзя.
* `trade_yidx.py` — Y-IDX сделки: `enrich(rows)`, `for_price(isin, price)`.
* `spread_history.py` — дневные снапшоты спред-метрик: `write_snapshot()`, `read_history(isin, days)`,
  `upsert_honest(...)`, `drop_stale_honest(...)`.

### Пользовательский слой

* `auth_users.py` + `api/routes/auth.py` — bcrypt-хеши в `data/users.json`, JWT в httpOnly-cookie,
  роли `user`/`admin`; зависимости `require_user`, `require_admin`, `user_from_websocket`.
* `alerts.py` — алерты по стакану: `create`, `update`, `cancel`, `active_all`, `evaluate(alert, levels, face)`,
  `mark_fired`.
* `screener_core.py` — **общее ядро скринера** для вкладки СИГНАЛЫ и Telegram-бота:
  `normalize_params`, `static_candidates`, `evaluate_candidates`, `evaluate`, `market_snapshot()`,
  `vwap_for(levels, want_rub, face)`, `y_idx_at(row, px, side)`, `block_matches(trade, meta, params)`.
* `signals.py` — фильтры веб-аккаунта + лента событий: `create`, `update`, `run_cycle()`,
  `detect_events(...)`, `preview`, `events_for_user`, `unseen_count`.
* `telegram.py` / `tg_users.py` / `tg_notify.py` / `tg_render.py` — бот `@deskdeskdesk_bot`
  как канал доставки: клиент Bot API, привязка чата к веб-аккаунту (заявка на `/start` →
  одобрение админом на сайте), очередь алертов + буфер сигналов, PNG-рендер стакана (Pillow).
  Своей настройки у бота нет — алерты и фильтры заводятся на сайте.
* `tg_digest.py` / `charts_png.py` / `tg_links.py` — вечерний «разбор дня» (19:30 МСК,
  `DIGEST_AT`): по альбому НА КЛАСС рынка (`DIGEST_SCOPES=floater,fixed`), один
  за другим, каждый — свой `sendMediaGroup` из 7–9 PNG (движения премии, широта
  движения, обороты, крупные сделки, карта «срок × премия», премия по
  рейтингам, профиль дня; у флоатеров ещё своп-кривая КС со сдвигом в бп и
  выплаты вперёд — календарь строится по универсу флоатеров, а КС к фиксам
  отношения не имеет). В конце одно сообщение с url-кнопками на страницы
  дашборда (у медиагруппы своей клавиатуры не бывает). Валютных бумаг
  (FACEUNIT ≠ SUR) в свёртке нет вовсе — их не собирает стрим, третий альбом
  ждёт отдельной задачи. Картинки рисуются примитивами Pillow (`Canvas`, `movers_split`,
  `breadth_split`, `turnover`, `blocks`, `scatter`, `grouped`, `profile`,
  `curve`, `payments`) — без matplotlib и браузера; в контейнере нужен
  `fonts-dejavu-core`. **Рынки не смешиваются**: Y-IDX флоатера и g-спред фикса
  меряют премию по-разному, поэтому каждый класс получает собственный альбом со
  своими шкалами, своим топом движений и своей метрикой в подписи; лента блоков
  фильтруется по классу через свёртку дня (у `block_trade` своего `kind` нет). Имена бумаг резолвятся реестр → суточный справочник ISS
  (`block_trades.secid_map`, прогревается перед сборкой): реестр знает только
  флоатеры, и без второго источника весь слой ФИКСОВ выпадал из альбома.
  Рейтинговые бакеты — из кэша `ratings` (реестр проставляет рейтинг лишь
  флоатерам). Санитары премии `DIGEST_SANE_MIN`…`DIGEST_SANE_SPREAD` отсекают
  структурные ноты с мусорным g-спредом. В подписи — ссылки на график выпуска (`tg_links.bond`),
  копируемые ISIN, серия движения («3-й день подряд») и ЛИЧНАЯ строка сигналов
  чата за день (картинки общие, подпись пересобирается на каждый чат).
  По пятницам (`DIGEST_WEEKLY_DAY=4`) вместо дневного уходит недельный разбор:
  движения к прошлой неделе (`DIGEST_WEEK_SESSIONS` торговых сессий), обороты
  суммой за неделю, свежие выпуски в подписи. Первичка из рейтинга сделок
  выброшена (`DIGEST_SKIP_PLACEMENT`) — РПС по 100,00 в день размещения не
  новость. Ручной вызов — команды бота `/digest` и `/week` (только в свой чат).
* `progress.py` — реестр фоновых задач для страницы СТАТУС: `start/advance/finish/snapshot`.
* `portfolio_db.init_db()` — схема `data/portfolio.db` (идемпотентно + аддитивные миграции).
* `paths.py` — `cache_path(name)`, `atomic_write_json(path, obj)`.
* `heavy.run_heavy(fn, ...)` — однопоточный executor для тяжёлого CPU-счёта (чтобы не душить event loop).
* `exceptions.py` — `APIException` и потомки (`NotFoundException`, `CalculationException`, …),
  единый обработчик в `api/main.py`.

---

## 5. HTTP API

Все роутеры под `/api`, закрыты `Depends(require_user)`, кроме `/api/health`, `/api/auth/*`
(логин) и `/api/tg/webhook` (защищён секрет-заголовком). WS проверяет cookie внутри хендлера.

### Системные
| Метод | Путь | Что |
|---|---|---|
| GET | `/api/health` | живость |
| GET | `/api/meta` | версии/мета |
| GET | `/api/status` | связность источников + очереди + прогресс задач |

### Авторизация
`POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`, `POST /api/auth/password`;
админ: `GET|POST /api/auth/users`, `POST /api/auth/users/{email}/reset-password`, `DELETE /api/auth/users/{email}`.

### Флоатеры
| Метод | Путь | Что |
|---|---|---|
| GET | `/api/bonds` | список/юниверс с метриками (`universe`, `with_market`, `with_valuation`) |
| GET | `/api/bonds/search?q=` | поиск по MOEX |
| GET | `/api/bonds/filters` | значения фильтров |
| GET | `/api/bonds/calendar` | календарь выплат |
| GET | `/api/bonds/quotes` | котировки всего рынка одним ответом (фронт тянет тактом 5с) |
| GET | `/api/bonds/{isin}` | карточка |
| GET | `/api/bonds/{isin}/audit` | паспорт бумаги (провенанс + бэктест спеки) |
| GET | `/api/bonds/{isin}/coupon-days` | дневная раскладка фиксинга |
| GET | `/api/bonds/{isin}/candles?tf=` | OHLCV MOEX |
| GET | `/api/bonds/{isin}/cashflow` | поток платежей |
| GET | `/api/bonds/{isin}/reprice?price=` | метрики под произвольную цену |
| GET | `/api/bonds/{isin}/price_from_spread?y_idx=` | обратная задача: спред → цена |

### Фиксы
`GET /api/fixed`, `GET /api/fixed/{isin}`, `GET /api/fixed/{isin}/reprice?price=`.

### Кривые
`GET /api/curves?type=ruonia|keyrate`, `/api/curves/plot`, `/api/curves/forwards`,
`/api/curves/ks-path?series=ks|ruonia`, `/api/curves/ruonia-index?days=`.

### Стакан
`GET /api/orderbook/{isin}?depth=&full=&kind=`, `GET /api/orderbook/depth/all`.

### История
`GET /api/history/{isin}/spread`, `/bars?days=&hours=`, `/trades`, `/reprice?date=&price=`,
`/spread_honest?days=`; `POST /api/history/aggregate/yidx` (медианный Y-IDX по бакетам/эмитентам).

### Сделки и блоки
`GET /api/trades` (+`/boards`, `/ratings`, `/issuers`), `GET /api/blocks` (+`/boards`, `/days`, `/{isin}`).

### Калькулятор
`GET /api/calc/custom` (кастомный фикс), `GET /api/calc/custom_floater` (кастомный флоатер).

### Алерты и сигналы
`GET|POST /api/alerts`, `DELETE /api/alerts/{aid}`;
`GET|POST /api/signals`, `DELETE /api/signals/{fid}`, `DELETE /api/signals/all`,
`POST /api/signals/preview`, `/preview-block`, `GET /api/signals/events`, `POST /api/signals/events/seen`,
`GET /api/signals/search`, `/emitters`.

### Реестр (admin)
`GET /api/instruments/unreviewed`, `/catalog`, `/catalog/export` (xlsx-шаблон),
`POST /api/instruments/catalog/import`, `/parse-formula`, `/{isin}`, `/{isin}/reset-manual`,
`/{isin}/reviewed`, `/{isin}/recheck-spec`.

### Telegram
`POST /api/tg/webhook`; Mini App: `/api/tg/me`, `/alerts`, `/filters`, `/mute`, `/search`, `/emitters`.

### WebSocket
`/api/ws/market` — `ConnectionManager`: `subscribe/unsubscribe` по каналам (`market`, `orderbook`,
`signals`), `broadcast_market_data`, `broadcast_orderbook`, `broadcast_signal`.

---

## 6. Фоновые демоны (`api/main.py`, `lifespan`)

| Задача | Такт | Что делает |
|---|---|---|
| `warmup_caches` | старт | ставки ЦБ → кривые → z-контекст → метрики флоатеров → метрики фиксов (6 шагов, видно на СТАТУСЕ) |
| `quotes_poller` | 5с (торги) | board-снапшот MOEX (3 запроса на ~540 бумаг) → `market_cache['last_prices']` |
| `universe_price_poller` | 600с | синк реестра, бэкфилл эмитентов, дискавери флоатеров, метрики юниверса, прогрев фиксов, драйн рейтингов (24/7) |
| `ws_market_data_broadcaster` | 5с | пуш изменившихся цен подписчикам WS (heartbeat 60с) |
| `universe_stream_pool` + `metrics_worker` | push / 5с | живой стрим котировок и стаканов по всему рынку + событийный пересчёт |
| `trades_stream_pool` | push | безадресные сделки Alor → `trade_tick` (объём в рублях: номинал × курс FACEUNIT) |
| `alor_orderbook_ws` | push | стаканы избранного |
| `depth_poller` | 120с | HTTP-фолбэк батч-снимка стаканов, если стрим не покрывает юниверс |
| `alerts_monitor` | 12с | активные алерты против стакана → fired + Telegram |
| `signals_worker` | 3с | фильтры вкладки СИГНАЛЫ по снапшоту в памяти |
| `tg_screener_worker` | 180с | скринер-фильтры бота |
| `block_trades_worker` | 60с | лента крупных сделок ISS, ночной backfill+prune, уведомления |
| `hourly_bars_worker` | час :07 | налив `bar_hourly` + тиков (полный обход раз в 6ч) |
| `spread_snapshotter` | 19:00 МСК | дневной снапшот спред-метрик |
| `daily_prewarm` | 09:00 МСК | тяжёлый прогрев дня (расписания bondization + метрики) |
| `archive_maintenance` | 03:30 МСК | prune тикового архива + VACUUM |
| `loop_lag_watchdog` | 1с | лаг event loop > 0.5с → warning, > 2с → стеки потоков |

Все демоны гейтятся `_in_moex_trading_hours()` (пн–пт 07:00–23:50 МСК), кроме реестровых драйнов.
`sys.setswitchinterval(0.001)` в шапке `main.py` — против GIL-конвоя на двухъядерном хосте.

---

## 7. Фронтенд

Роутинг (`App.jsx`, basename учитывает префикс `/desk`):

| Путь | Компонент |
|---|---|
| `/floaters` | монитор флоатеров (`BondTable`, `Toolbar`, `FiltersMenu`, `ColumnsMenu`, `Kpis`, `AnalyticsPanel`) |
| `/fixed` | `FixedModule` / `FixedCard` / `FixedAnalytics` |
| `/trades`, `/blocks` | `TradesTape` |
| `/signals` | `SignalsModule` + `SignalsBell`/`SignalsWatcher` |
| `/payments` | `PaymentsCalendar` |
| `/calc`, `/calc/float` | `CalcModule` |
| `/curves`, `/curves/:view` | `CurvesModule` |
| `/reference` | `Catalog` (admin: правка параметров + импорт/экспорт xlsx) |
| `/status` | `StatusPage` |
| `/audit/:isin` | `BondAudit` — паспорт бумаги |
| `/chart/:isin` | `ChartPage` — полноэкранный график (lightweight-charts, lazy) |

Навигация: `Topbar` — тип бумаг (Флоатеры / Фиксы / Кривые / Справочник / Статус) + суб-вкладки.
Карточка выпуска открывается в `Drawer` (`Orderbook`, `CashflowChart`, `SpreadHistory`, `PriceChart`,
`CouponFormula`, `OrderbookAlerts`).

Графики: **два движка** — свой SVG-каркас `src/charts/` (`ChartFrame`, `Axis`, `Brush`, `Legend`,
`MeasuredSvg`, `scales/paths/ticks/hover`, `useChartSize`) везде и `lightweight-charts` только на
`/chart/:isin`. Правило: **размер по замеру контейнера**, не фиксированный viewBox.

Утилиты: `api.js` (префикс/`APP_BASENAME`), `auth.jsx` (глобальный 401), `format.js` (ru-разделители),
`search.js` (умный поиск «РЖД 3» → все выпуски), `vwap.js` (VWAP по лестнице + Y-IDX),
`clipboard.js`, `pageStatus.jsx` (единая нижняя полоса статуса).

Тест-харнессы без бэкенда (отдельные Vite-entry, не входят в прод-бандл):
`chart-test`, `analytics-test`, `tape-test`, `daytable-test`, `ruonia-test`, `valcards-test`, `voltable-test`.

---

## 8. Схема данных

`data/instruments.db`: `instruments` (все параметры выпуска + слои br_*/manual/authoritative),
`discovery_seen`, `enrich_seen` (negative-кэши, чтобы дискавери не крутил один и тот же хвост).

`data/portfolio.db`:

| Таблица | Что |
|---|---|
| `alerts` | алерты по стакану, per-user |
| `spread_daily` | дневные снапшоты спред-метрик (+ honest-строки as-of движка) |
| `bar_hourly` | часовые бары: VWAP, спред OHLC, объёмы сторон |
| `trade_tick` | тиковый архив Alor |
| `tick_drain` | водяные знаки дрейна тиков |
| `block_trade`, `block_day`, `block_cursor` | крупные сделки, дневные РПС-агрегаты, курсор ленты |
| `signal_filters`, `signal_state`, `signal_events` | вкладка СИГНАЛЫ |
| `tg_users`, `tg_filters`, `tg_filter_hits` | Telegram-бот |
| `job_progress` | прогресс фоновых задач |

---

## 9. Переменные окружения

`.env` (не коммитится, исключён из rsync деплоя).

* **Alor**: `ALOR_REFRESH_TOKEN`, `ALOR_ENV`, `ALOR_ALLOWED_PORTFOLIOS`, `ALOR_POOL_SHARD` (150),
  `ALOR_TRADES_SHARD`, `ALOR_LIVE_CAP`.
* **Авторизация**: `AUTH_SECRET`, `AUTH_COOKIE_SECURE`, `AUTH_USERS_FILE`.
* **Telegram**: `TG_BOT_TOKEN`, `TG_WEBHOOK_SECRET`, `TG_WEBHOOK_URL`, `TG_SITE_URL`,
  `TG_SIGNAL_FLUSH_SEC` (30 — период слива буфера сигналов),
  `TG_API_IP` (обход IPv6-only DNS на VPS).
* **Дайджест**: `DIGEST_ENABLED`, `DIGEST_AT` (19:30 МСК), `DIGEST_SCOPES`
  (`floater,fixed` — по альбому на класс), `DIGEST_WEEKLY`(1),
  `DIGEST_WEEKLY_DAY` (4 — пятница), `DIGEST_WEEK_SESSIONS` (5),
  `DIGEST_MIN_VALUE`, `DIGEST_MOVERS`, `DIGEST_TURNOVER_TOP`, `DIGEST_BLOCKS_TOP`,
  `DIGEST_BLOCKS_PER_ISIN`, `DIGEST_SKIP_PLACEMENT`, `DIGEST_STREAK_DAYS`,
  `DIGEST_PAYMENT_DAYS`, `DIGEST_PAYMENT_DAYS_WEEK`, `DIGEST_NEW_ISSUES`,
  `DIGEST_CURVE_BACK`, `DIGEST_SANE_SPREAD`, `DIGEST_SANE_MIN`,
  `DIGEST_SANE_DELTA`, `DIGEST_THIN_SHARE` (доля максимума, ниже которой
  интервал профиля теряет медиану премии).
* **Такты воркеров**: `QUOTES_POLL_INTERVAL`, `SIGNALS_INTERVAL`, `DEPTH_POLL_INTERVAL`,
  `BLOCK_POLL_INTERVAL`, `BARS_WORKER`(0/1), `BARS_WORKER_DAYS`, `BLOCK_WORKER`, `TRADES_STREAM`,
  `DEPTH_STREAM`, `TRADES_STREAM_FLUSH`.
* **Пороги и ретеншен**: `BLOCK_MIN_VALUE_RUB`, `BLOCK_ARCHIVE_MIN_RUB`, `BLOCK_RAW_DAYS`,
  `BLOCK_BACKFILL_DAYS`, `BLOCK_MAX_PAGES`, `BLOCK_ALERTS`, `BLOCK_ALERT_MIN_RUB`,
  `BLOCK_ALERT_FLOATERS_ONLY`, `BLOCK_YIDX_MIN_RUB`, `TICK_RAW_DAYS`, `TICK_BIG_VALUE_RUB`,
  `TICK_DRAIN_OVERLAP_HOURS`, `TICK_YIDX_DAYS`, `ARCHIVE_VACUUM_MIN_ROWS`,
  `TAPE_YIDX_MAX_ISINS`, `TAPE_YIDX_CTX_TTL`.

---

## 10. CLI-утилиты (`scripts/`)

**Деплой и доступ**: `deploy.sh` (rsync + docker compose + healthcheck; `DEPLOY_SSH_BIND=en0` в обход VPN),
`useradd.py` (аккаунты дашборда), `tg_set_webhook.py`.

**Бэкфиллы**: `backfill_emitters.py`, `backfill_has_call.py`, `backfill_call_dates.py`,
`backfill_coupon_period.py`, `backfill_infer_params.py`, `backfill_hourly_bars.py`,
`backfill_spread_ohlc.py`, `backfill_yidx_history.py`, `reset_yidx_methodology.py`.

**Спека фиксинга**: `fill_fixing_specs.py` (материализация), `fit_spec.py` (подбор лаг×окно по факту),
`set_spec.py` (ручная правка), `verify_fixing_specs.py` (бэктест по факту выплат),
`import_bondresearch_specs.py`, `migrate_point_specs.py`, `unfreeze_fixing_spec.py`,
`clear_conflicting_fixing_fields.py`, `unlock_auto_imported.py`, `prod_verify_coupon_convergence.py`.

**Данные**: `enrich_from_bondsearch.py` (bondsearch-xlsx → шаблон СПРАВОЧНИКа), `infer_base_margin.py`.

**Разовые исследовательские стенды** (сверка с НРД, чувствительность): `calc_isolation.py`,
`dm_calibrate.py`, `reconcile_dm.py`, `reconcile_after.py`, `sensitivity.py`, `hypothesis_matrix.py`,
`nrd_pipeline_probe.py`, `ks_spline_probe.py`.

---

## 11. Понятия и правила расчёта

* **Y-IDX** — первичная метрика спреда во всех витринах (таблица, стакан, график, алерты).
  SM и DM — вспомогательные.
* **SM** (simple margin) — маржа, приравнивающая PV к dirty при плоской проекции.
  **DM** (discount margin) — market-standard FRN-спред (met_float / Fabozzi).
* **z-спред** — над КБД ОФЗ, две реализации: непрерывная (`solve_z_bps`) и дискретная по НРД
  (`solve_z_discrete`).
* **G-спред** — для фиксов, над КБД.
* **Правило горизонта**: если цена бумаги выше цены выкупа по пут/колл — метрики считаются
  к оферте, а не к погашению (`pick_horizon`, блок `horizons` в карточке, переключатель в UI).
* **Спека фиксинга** — из проспекта приоритетнее калибровки. Режимы: `point`, `average`
  (окно `avg_window_days`), лаг в днях/рабочих днях. Расхождения ловит бэктест по факту выплат
  (вердикты OK/WARN/BAD/NO_DATA).
* **Купонный период** берётся из фактического графика купонов, а не как `round(365/freq)`.
* **Приоритет источников параметров**: ручная правка реестра (`manual_locked`) > авторитетный
  источник (corpbonds, формула купона) > парсер/калибровка > Cbonds-выгрузка > MOEX.
* **Ловушка xlsx-импорта**: импорт справочника замораживает вывод парсера как manual-оверрайд
  и глушит последующие автоправки — лечится `scripts/unfreeze_fixing_spec.py`.
* **Данные MOEX bondization** кэшируются на торговый день (перекат 09:00 МСК), поэтому первый
  прогон дня тяжёлый — его специально делает `daily_prewarm`.
* Лента `/api/trades` из ISS отстаёт на ~15 минут; поток Alor (`trades_stream`) — без задержки.
* ISS не отдаёт `FACEVALUE` за текущий день — цена сегодняшнего бара считается по тикам.

---

## 12. Тесты

`pytest.ini`: `testpaths=tests`, `-q`. Покрыты: солверы и кэшфлоу (`test_solvers`, `test_cashflows`,
`test_zspread`, `test_settlement`), спека фиксинга (`test_coupon_formula_parser`, `test_calc_custom`),
реестр и источники (`test_registry_source_of_truth`, `test_instruments_registry`,
`test_ref_data_bondsearch`, `test_enrich_corpbonds`, `test_has_call`, `test_call_dates`),
рыночные потоки (`test_ws_subscriptions`, `test_universe_stream`, `test_trades_stream`,
`test_live_vwap`, `test_depth_cache`, `test_market_tape`, `test_tape_merge`, `test_block_alerts`,
`test_block_prune`, `test_tick_retention`, `test_tick_drain_incremental`, `test_today_vwap_face`),
кривые (`test_curves`), сигналы и бот (`test_signals`, `test_tg_bot`), горизонты (`test_pricing_horizon`,
`test_next_offer_info`), регрессии аудита (`test_audit_fixes`, `test_alt_price_yidx`,
`test_index_grow_cache`).

**Фронт: smoke-проверка монтирования** (`npm --prefix frontend-react test`, vitest + jsdom,
`frontend-react/src/App.test.jsx`). Рендерит приложение целиком — гостем (форма входа) и с
сессией (монитор со строкой таблицы). Сеть заглушена на уровне `fetch`/`WebSocket`, а не
подменой `api.js`: так проверяется настоящий клиентский слой, и тест не надо править при
каждом новом вызове.

Зачем он есть: `vite build` проверяет, что код СОБИРАЕТСЯ, и молчит про ошибки, которые
случаются только при рендере. 27.08.2026 монитор ушёл в белый экран — массив зависимостей
хука обращался к `const loadBonds` выше его объявления (мёртвая зона, `ReferenceError` на
первом рендере). Сборка была зелёной, `pytest` тоже, поймал пользователь. Признак в логах
бэка — ни одного запроса `/api/bonds?universe=true`; тест проверяет ровно это плюс саму
отрисовку строки. Стартовый URL окружения — `/app/floaters`: с «/» роутер с `basename="/app"`
молча рендерит пустоту (та же ловушка, что у дев-сервера).

---

## 13. Прочие документы

* `STATUS.md` — журнал состояния и решений.
* `TODO.md` — бэклог.
* `API_TZ.md` — ТЗ по API.
* `docs/code_review_2026-08-12.md`, `docs/telegram_bot_plan.md`.
