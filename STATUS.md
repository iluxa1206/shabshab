# FLOATERS DESK — статус проекта

_Обновлено: 2026-07-02. Контекст для новых сессий: прочитай этот файл первым._

## Что это
Дашборд аналитики **облигаций с плавающим купоном (флоатеров, RUB)**. FastAPI-бэкенд + React-фронт (Vite). Показывает весь рынок флоатеров с оценкой: discount margin (DM), спред, доходности, рейтинги, cashflow.

## Как запустить
```bash
cd /Users/ishabaev/python_projects/shabshab
.venv/bin/python -m uvicorn api.main:app --port 8000
# фронт: cd frontend-react && npm run build  (FastAPI отдаёт dist на /app)
# открыть http://127.0.0.1:8000/app/
```
Превью (Claude): `.claude/launch.json` server name `api`. React-правки требуют `npm run build` (FastAPI отдаёт статику из `frontend-react/dist`, не live src).

## Прод / деплой (deskdeskdesk.ru)
Живёт на **https://deskdeskdesk.ru/app/** (VPS 161.104.17.23, Ubuntu 24.04).

**Деплой одной командой** (с этой машины): `./scripts/deploy.sh`
— rsync кода в `/root/floaters` → `docker compose -f docker-compose.prod.yml --env-file .env up -d --build` → healthcheck. Фронт (`npm run build`) собирается ВНУТРИ Docker (multi-stage `Dockerfile`), пересборка вручную не нужна.

**Инфра:** сервер НЕ выделенный — на нём уже стек `astra-prod` (`/root/catalog/`, сайт assetallocator.ru: Next.js+Postgres+**Caddy** на 80/443). Наш проект — отдельный compose-проект `floaters-prod`:
- один контейнер `floaters` (uvicorn :8000), подключён к внешней docker-сети `astra-prod_default`, лимит `mem_limit: 768m`.
- Caddy (чужой, `/root/catalog/Caddyfile`) проксирует наш домен: блок `deskdeskdesk.ru, www.deskdeskdesk.ru { reverse_proxy floaters:8000 }`, TLS Let's Encrypt авто. WS (`/api/ws/market`) проксируется как wss.

**Доступ:** `ssh root@161.104.17.23` по ключу `~/.ssh/id_ed25519` (уже установлен).
**Секреты:** `.env` (Alor/НРД/`AUTH_SECRET` креды) лежит в `/root/floaters/.env`, прокинут через `env_file` (в git/образ НЕ попадает). `deploy.sh` **исключает `.env` и `data/` из rsync** (`--exclude`) — прод-секреты и БД юзеров не затираются локальными; правятся вручную на сервере.
**Правка Caddy** (после ручного изменения Caddyfile): `docker exec astra-prod-caddy-1 caddy reload --config /etc/caddy/Caddyfile`.
**DNS:** A-записи `@` и `www` deskdeskdesk.ru → 161.104.17.23.

## Авторизация (доступ по аккаунту)
Весь сайт закрыт логином — самостоятельной регистрации НЕТ, аккаунты заводит только админ (CLI). Чужие аккаунты попасть не могут.

**Как устроено:**
- Гейт: зависимость `require_user` на роутерах данных (`meta`/`bonds`/`curves`/`orderbook`) в `api/main.py`. Открыты только `/api/health` и `/api/auth/*`. WS проверяет cookie на хендшейке (`api/routes/ws.py`).
- Сессия: JWT (HS256, подпись `AUTH_SECRET`) в httpOnly-cookie `session`, срок 7 дней (`api/routes/auth.py`). Удаление юзера сразу лишает доступа (токен сверяется с хранилищем).
- Хранилище: bcrypt-хеши в `data/users.json` (атомарная запись, `services/auth_users.py`). В проде — Docker-том `./data:/app/data` (переживает редеплой). Файл gitignored.
- Фронт: `Login.jsx` + гейт в `App.jsx` (проверяет `/api/auth/me`), кнопка «Выход» в `Topbar`. 401 в любом запросе → редирект на логин.

**Env (`.env`):** `AUTH_SECRET` — обязателен (без него все запросы 401, fail-closed). `AUTH_COOKIE_SECURE` — `1` в проде (HTTPS), `0` локально (HTTP). Генерация секрета: `openssl rand -hex 32`.

**Управление доступом (CLI `scripts/useradd.py`):**
```bash
# локально
python scripts/useradd.py add user@example.com --role admin   # спросит пароль
python scripts/useradd.py list
python scripts/useradd.py remove user@example.com
# в проде — внутри контейнера (пишет в том data/):
docker compose -f docker-compose.prod.yml exec floaters python scripts/useradd.py add user@example.com
```

**Первый деплой авторизации:** (1) добавить `AUTH_SECRET` (+ убрать/выставить `AUTH_COOKIE_SECURE=1`) в `/root/floaters/.env`; (2) `./scripts/deploy.sh`; (3) завести первого юзера через `docker compose ... exec`. Без юзеров сайт закрыт для всех.

Файлы деплоя (в репо): `Dockerfile`, `.dockerignore`, `docker-compose.prod.yml`, `scripts/deploy.sh`.

## Архитектура
```
api/           FastAPI: main.py, schemas.py, routes/{health,meta,bonds,curves,orderbook,ws}
services/      bonds, cashflow, valuation, market_data, nrd, exceptions
root .py       auth(Alor token), cashflow, forwards(кривые), valuation(DM/XIRR), rates(Cbonds), last_prices(Alor WS)
frontend-react/ Vite+React+framer-motion: src/{App,api,format,main}.jsx + components/ + styles.css
tests/         только test_nrd.py (pytest)
```

## Источники данных (контракты)
- **НРД Ценовой центр** (NSDDATA API v2, base `https://nsddata.ru`). Auth: POST `/api/auth/login` {login, password=apikey} → {access_token, refresh_token}; refresh на 401; Bearer. Данные: GET `/api/get/valuationnewadd?filter=<json>&limit&skip` (доп.параметры). **Подписка даёт ТОЛЬКО valuationnewadd** — `valuationnew` (fair value) = 403. `filter` = JSON Mongo-стиль `{"isin":{"$in":[...]}}`. Спека: `https://nsddata.ru/ru/products/api/scheme`. Креды в `.env`: `NRD_LOGIN`, `NRD_APIKEY`.
  - **Масштабы полей (доли/проценты!)**: yields ×100→%; z_spread/g_spread/nominal_margin ×10000 (доли→bps); discount_margin/simple_margin ×100 (проценты→bps). `base_coupon_index`: CBRATED→KEYRATE, RUONIARATED→RUONIA. `nominal_margin`=спред выпуска (совпал 16/16).
  - Юниверс: пагинация valuationnewadd (1000/стр), фильтр `coupon_type=float` + база, **дедуп по isin (max val_date — НРД отдаёт дубли!)**, кэш `nrd_universe_cache.json` на день. ~453 рублёвых флоатера.
- **MOEX ISS**: цены prev/accrued (`fetch_moex_snapshot`, борд TQCB), справочник (`fetch_moex_securities`), расписание купонов с реальными суммами (`fetch_bond_schedule_full` — bondization value/valueprc), короткие имена выпусков (`fetch_moex_shortnames`, один запрос всех бондов), поиск (`/api/bonds/search`).
- **Alor**: live-цены по WebSocket (`last_prices.py`), refresh-токен в `.env` `ALOR_REFRESH_TOKEN`.
- **Cbonds**: кривые ставок RUONIA OIS / KEYRATE IRS (`rates.py`, парсинг HTML).

## Расчёты
- Наш `dm_bps` = `solve_dm_bps` (valuation.py) — правильный discount margin (PV cashflow при forward+DM = dirty price), бисекция. Та же метрика что НРД discount_margin.
- **DM-cashflow починен (2026-07-08)**: `build_cashflows_to_maturity` принимает тройки (start,end,value) — зафиксированный купон берётся ФАКТОМ MOEX (не перепрогнозируется форвардом, как в z-движке), + параметр `amorts` — принципал по графику амортизаций, прогнозные купоны от остаточного номинала. `services/valuation.calculate_valuation_metrics` больше НЕ срезает value; `amorts=` прокинут из bonds.py (universe/detail, из уже загруженного schedule_full). Эффект: KEYRATE +2…+48bps (факт купона), амортизируемые до ±56bps. Гипотеза «рассинхрон day-count сетки начисления/дисконта» ПРОВЕРЕНА И ОТВЕРГНУТА (купон платится за полный период, покупатель компенсирует НКД; par-смоук 152≈150bps ✓).
- **Ex-coupon T+1 (2026-07-09)**: `valuation.settle_date` — купон с pay_date <= T+1 (скип выходных) покупателю НЕ достаётся (MOEX НКД уже 0 под эту конвенцию); исключён из cashflow во всех трёх движках (DM `build_cashflows_to_maturity`, z `project_cfs`, фикс `build_fixed_cashflows`). До фикса накануне каждой купонной выплаты PV завышался на целый купон → DM/z +сотни bps мусора (пойман на RU000A106JX4: 731 → 237 vs НРД 200).
- Cashflow (`services/cashflow.build_cashflow_from_moex`): прошлые купоны = факт MOEX (value/valueprc), будущие = прогноз (forward+spread), погашение из амортизаций/номинал. dirty = clean + live accrued (MOEX, не стейл-кэш).
- Кривые: bootstrap из Cbonds (`forwards.py`), KEYRATE quarterly comp, RUONIA daily, ACT/365.
- **Расхождение наш DM vs НРД — РАЗОБРАНО ПО МЕТОДИКАМ (2026-07-01).** Документы НРД скачаны в `docs/nrd/`: `met_float_10112025.pdf` (флоатеры) + `met_rub_21_05_2026.pdf` (базовая). Изоляц. стенды: `scripts/reconcile_dm.py`, `scripts/ks_spline_probe.py` (подают цену НРД → расхождение = чистая математика).
  - **Конвенция дисконта НРД (базовая, п.4.9): `P=Σ CF_i·exp(−(r(τ_i)+z)·τ_i)−AI` — НЕПРЕРЫВНОЕ компаундирование.** Наш daily `(1+r/365)^days` ≈ непрерывному → конвенция уже почти верна (годовое `(1+r)^−t` — НЕВЕРНО, переоценивает DM +120bps, проверено).
  - **`r(τ)` — с кривой КБД ОФЗ (G-curve) МосБиржи**, `z` — z-спред эмитента по кривой **Нельсона-Сигеля-Свенсона**, калиброванной **фильтром Калмана** (Прил.6). То есть НРД дисконтирует по БЕЗРИСКОВОЙ ОФЗ + КРЕДИТНЫЙ z-спред, а своп-кривые КС(кубич.сплайн Прил.3)/RUONIA(Smith-Wilson UFR 7.14% Прил.2) — только для ПРОГНОЗА купонов.
  - **Эмпирика:** near-par качественные флоатеры сходятся ±10bps уже сейчас; бумаги с большим НРД DM (дисконт/ниже рейтинг) недобираем на 100–250bps — ровно там, где играет кредитный z-спред над ОФЗ. `discount_margin` НРД — **производный аналитик, точная формула НЕ опубликована** (методики задают справедливую цену и z-спред).
  - **РЕШЕНИЕ (пивот):** НРД `discount_margin` — авторитетный (есть из API, колонка «NRD DM»). Наш DM — intraday model-оценка для live-цены watchlist, помечен «DM MODEL». Полный матч <10bps = воспроизвести весь NSS/Kalman-движок НРД (G-curve+z-спред) — отклонён (недели, новые данные, аналитик проприетарен). BUG-1 (форвард прошлого стаба) исправлен клэмпом анкера к calc_date.

## 📐 ДВЕ МЕТРИКИ СПРЕДА: SM + DM (2026-07-09)
НРД публикует 5 спред-метрик флоатера: `nominal_margin` (спред выпуска), `simple_margin`, `discount_margin`, `z_spread`, `g_spread`. Мы считаем **две** и привязываем каждую к своему полю НРД:
- **`sm_bps` (simple margin)** = наш старый DM-солвер (`solve_dm_bps`, дисконт по форвард-кривой+спред). Воспроизводит НРД **`simple_margin`** (ликвид near-par med 0-2, off-par med +19, m\|Δ\|49). Поле `dm_bps` СОХРАНЕНО = `sm_bps` (обратная совместимость).
- **`disc_margin_bps` (discount margin)** = НОВЫЙ настоящий FRN DM (`solve_discount_margin_bps` + `FlatForwardCurve`): индекс держим ПЛОСКИМ на текущем уровне (`current_index_pct` из зафикс. купона), money-market дисконт `Π 1/(1+(L+DM)·τ)`. Правильно дисконтирует pull-to-par → DM выше simple на дисконте, ниже на премии (как НРД). Воспроизводит НРД **`discount_margin`** (off-par ликвид med −20, m\|Δ\|46). Остаток — их проприетарная fair-value машина (NSS/Kalman), несводимо.
- Стенд калибровки: `scripts/dm_calibrate.py` (перебор конвенций: flat/forward проекция × simple/comp/cont дисконт; FRN-simple-flat выиграл).
- **Прокинуто**: `sm_bps`/`disc_margin_bps` в `calculate_valuation_metrics`, `BondValuation` (detail), `BondListItem` + `_uni_item` + watchlist enrich (список). НРД `simple_margin_bps` уже в юниверсе (v4). UI: колонка «наш SM» ↔ НРД simple, «наш DM» ↔ НРД discount.

## 🎯 КОРНЕВАЯ ПРИЧИНА DM-РАСХОЖДЕНИЯ — НАЙДЕНА (2026-07-09)
**Мы сверялись НЕ с тем полем НРД.** Наш `dm_bps` (простой спред над проекцией при PV=цена) = НРД **`simple_margin`**, а НЕ `discount_margin`. Стенд `scripts/calc_isolation.py` (одна цена+дата+реальный НКД, независимый фильтр ликвидности по `trade_volume_rub`):

| наш DM против | KEYRATE | RUONIA |
|---|---|---|
| НРД `discount_margin` | med −14, m\|Δ\|62, std 96 | med −28, m\|Δ\|148, std 277 |
| НРД **`simple_margin`** | **med +2, m\|Δ\|36** | **med 0, m\|Δ\|12, std 20** |

RUONIA против simple_margin — **почти идеально (med 0, std 20bps)**. `discount_margin` НРД — их проприетарная fair-value метрика (NSS+Kalman кредит-слой, corr остатка −0.81 с уровнем dm, без закрытой формулы) И **само поле шумное/битое** на части бумаг: RU000A107456 dm=1175 но их же simple_margin=215, ytm=17.5% (наш DM 160); RU000A0JXK99 dm=1389 vs simple_margin=262. **Внедрено**: `simple_margin_bps` прокинут в юниверс (nrd.py CACHE_VERSION→4) + BondListItem + get_bonds; detail уже отдавал (BondNrd). UI-якорь сравнения = simple_margin, discount_margin оставить отдельной НРД-колонкой.

**Остаток после правильного таргета:** хвосты KEYRATE — неликвид (бо́льшая часть флоатеров <1млн₽/день оборота → wa_price ненадёжна, НРД метки модельные) + оферты. Ликвидное ядро сходится. НКД-баг стенда (RUONIA current-купон value=None → accrued=0) исправлен: реальный MOEX НКД → «RUONIA +39» исчез. **Прод НКД корректен** (accrued_override из MOEX snapshot во всех путях).

## 🔬 ГЛУБИННАЯ ПРИЧИНА РАСХОЖДЕНИЯ DM — ДОКОПАНО (2026-07-08, дополнено выше 2026-07-09)
Стенды: `scripts/hypothesis_matrix.py` (матрица гипотез, 37 бумаг, свежие сырые строки valuationnewadd), `scripts/sensitivity.py` (что двигает DM), `scripts/reconcile_after.py` (before/after фиксов). Итог — 4 слоя по величине:
1. **Базис данных (главный, излечим).** (а) Дневной юниверс-кэш держал вчерашние wa_price/dm — на свежих данных систематика KEYRATE −25 → **−8 mean / −14 median** → TTL кэша 4ч (внедрено). (б) У неликвида НРД dm посчитан от их fair value (`valuationnew`, у нас 403), не от wa_price — выбросы до ±1000bps несводимы без подписки.
2. **Оферты.** MOEX: `OFFERDATE` пуст, но **`BUYBACKDATE` заполнен** и **bondization отдаёт блок `offers`** (offerdate/offertype/price — бесплатно в том же запросе, внедрено в fetch_bond_schedule_full v2). ~17% юниверса с офертой до погашения. **Клэмп горизонта к оферте ПРОВЕРЕН И ОТВЕРГНУТ**: to-maturity ближе к НРД dm на ВСЕХ горизонтах (4дн…5.7лет) — НРД считает dm к погашению. Оферта = информационный флаг: `offer_date/offer_type` в detail reference + warning при оферте ≤180дн (цена может прайситься к оферте).
3. **Ядро остатка — CF-путь НРД ≠ наш (несводимо).** Разложение per-bond: наш flat-yield при ТОЙ ЖЕ цене на **−110bps mean** ниже их ytm → их купонный прогноз (NSS+Kalman имплайд-путь) выше наших IRS-форвардов на 1-2%. В dm почти сокращается (curve-relativity), остаток corr −0.81 с уровнем dm = кредитная кросс-секция их машины. **Закрытой формулы dm НЕТ** (b=ytm−dm не ложится ни на одну кривую ±110bps std — в отличие от z, где ytm−G(0.25) ±4-13bps).
4. **Конвенции — исключены измерением**: непрерывный/годовой дисконт ±6-10bps, simple-купоны KEYRATE +4bps, cd=val_date — шум, кривая parallel ±100bp → ΔDM≈0 (DM curve-relative).
- **Sensitivity (что реально двигает DM)**: цена — доминанта (ΔDM≈100/spread_dur bps на пункт; короткие/амортизируемые — до 230bps/пт), НКД сопоставим (короткая бумага: +5₽ → −379bps!), спред выпуска ~1:1, уровень кривой ~0. Для watchlist критичны свежие цена+НКД одного источника; гнаться за <10bps к НРД бессмысленно.
- **RUONIA median −70bps** — подозрение на конвенцию котировки базы (effective vs nominal daily-comp), потолок ~50-70bps, не дожато (низкий приоритет).

## Текущее состояние UI (готово)
- **Дизайн**: Realtime/swiss-минимализм (Juri Zaech vibe), монохром, Martian Mono шрифт. Темы light (бумага) / dark (LED-табло), persist localStorage. WCAG AA контраст пройден.
- **Список = весь рынок** (юниверс НРД ~453). Live-цена Alor + наш DM только для watchlist (WS-подписка на них). Остальным — НРД-аналитика (VWAP цена, discount_margin, z-spread, duration, спред, база, рейтинг, погашение).
- **Watchlist**: ★-колонка (sticky) toggle, persist localStorage `watch`, обогащается live+наш DM.
- **Фильтры**: 3 независимые группы флажков (watchlist ★ / база КС·RUONIA / рейтинг AAA·AA·A·BBB·BB↓·NR) — AND между группами, OR внутри. Рейтинг нормализован в бакет (`rating_bucket`).
- **Поиск**: клиентский по юниверсу, результаты строками в таблице.
- **KPI**: пересчитываются по отфильтрованному набору (AVG DM = наш ?? НРД discount_margin).
- **Drawer**: карточка любой бумаги (не только кэш) — референс (MOEX+НРД), cashflow-график (факт+прогноз, линия «сегодня»), НРД-блок, наш DM.
- **Колонка RATING**, footer с источниками-статусами (● ALOR/CBONDS/NRD).

## 💼 МОДУЛЬ «ФОНДЫ» (2026-07-02, Ф1 готов и закоммичен)
Продукт стал мульти-модульным: переключатель «Флоатеры | Фонды» в Topbar (persist `module`). 3 хедж-фонда: **R5** (RUB: корп флоатеры 0.9× + длинные ОФЗ 2.2× капитала), **D5** (USD: + замещающие), **Y5** (CNY: + юаневые). Плечо = явные РЕПО-сделки; NAV = MV − РЕПО; net carry = купоны − funding.

**Бэкенд** (SQLite `data/portfolio.db`, том в проде): `services/portfolio_db.py` (funds/positions/repo_deals/nav_daily), `services/instruments.py` (классификатор 7 классов; ISIN→SECID через q-поиск ISS — board-less endpoint ОФЗ не резолвит; борд-строка по CURRENCYID==FACEUNIT — иначе у замещающих рублёвый НКД с TQCB), `services/fixed_income.py` (YTM/dur/DV01/G-spread к КБД; yield-to-put), `services/fx.py` (**TOM с MOEX** LAST→WAPRICE→PREV TTL 60с + 15-мин stale-буфер + ЦБ-фолбэк), `services/portfolio.py` (агрегация; месячный кэшфлоу-календарь из bondization, флоатеры оценкой по current coupon; `snapshot_all_navs`). Фоновый `fund_nav_snapshotter` в main.py (раз в час, идемпотентно за день). API `api/routes/funds.py`: CRUD/snapshot CSV/repo/summary/cashflow/nav_history.

**Фронт** `frontend-react/src/components/funds/`: карточки фондов, деталка (KPI-строка, классы с фильтром, таблица позиций WGT/warn-бейджи, РЕПО-секция, инлайн-редактор капитала — ввод в ₽), **тумблер валюты ₽|$|¥** (пересчёт всех метрик по TOM, подпись источника), 12-мес кэшфлоу-чарт (купоны/прогноз/принципал), экспорт позиций CSV (Excel-RU: BOM+`;`+запятая), форма «В фонд» в Drawer скринера (заменяет qty, не суммирует).

**Валидировано**: ОФЗ 26238 G-spread −2бп; ГазКЗ-37Д YTM 8.0% USD, НКД в USD; РЖД 1Р-26R yield-to-put; микс-РЕПО WA по ₽-эквиваленту; cross-fund изоляция удаления.

**Ф2–Ф4 (2026-07-02, тот же день):**
- **Фикс-движок добит**: дюрация Маколея, выпуклость (numeric bump ±10бп). Yield-to-worst: ISS OFFERDATE пуст даже у бумаг с офертой → наш детект «первый купон без value = оферта» и есть YTW-консистентная оценка (пост-офертный купон не определён).
- **Risk-split** в summary: рублёвый DV01 фиксов vs MV флоатеров (rate dur≈0, риск в carry) vs валютный DV01; **fx_split**: MV/РЕПО/нетто по валютам. UI: RiskStrip-строка в деталке.
- **Сценарии** (`fund_scenarios`, endpoint `/{code}/scenarios`, UI-таблица): КС ±100/±200 (фиксы −DV01·Δ+½conv·Δy²·MV; флоатеры Δcarry=MV·Δ перефиксинг; РЕПО RUB ходит с КС), steepener/flattener (Δy от дюрации: −mag@≤1г…+mag@≥7л), FX ±10% (чистая валютная экспозиция; carry валютных ∝Δ). Проверено: R5 КС−200 → +1.44 млн MtM (convexity-асимметрия) + carry +0.33 млн (funding-выигрыш > потери купона флоатеров); D5 FX±10% = ±7.7 млн.
- **Календарь** `/_meta/calendar`: купоны/амортизации/погашения/оферты 90дн по всем фондам (поймал реальную оферту ГТЛК 30.07). **Алерты** `/{code}/alerts`: Δz/Δdm D/D из history.py ≥20бп. **Сравнение фондов** таблицей на главном экране. **Бенчмарки** `services/benchmarks.py` + `/_meta/benchmarks`: RGBITR/RUCBTRNS/RUCEU история с MOEX ISS (кэш день), perf 1д/30д/YTD.
- **Фундамент атрибуции**: nav_daily + fx_usd/fx_cny (ALTER-миграции в init_db), pos_daily (дневной позиционный срез с ценами) — полная P&L-атрибуция (carry/rolldown/spread/rate/FX) в Ф5 вместе со сделками.
- Ф5 план: сделки+P&L+атрибуция, паи (цены юзера), графики NAV vs бенчмарк (nav_daily копится), рейтинги НРД, двухвалютный NAV.

## 📊 ФЛОАТЕР-МЕТРИКИ + КРОСС-СЕКЦИЯ (2026-07-02)
Реализован полный пакет метрик, специфичных для флоатеров (адаптация bond-аналитики под плавающий купон: rate duration≈до рефиксинга мала, spread duration≈до погашения — весь кредитный риск).
- **Бэкенд**: `services/metrics.py` (чистые функции: `macaulay_years` spread duration, `days_to_refix`, `current_coupon_pct`, `carry_bps`, `breakeven_base_pct`, `base_level_pct` с короткого конца кривой, `rank_pct` перцентиль в бакете, `years_to`). `services/history.py` (ежедневный снапшот юниверса → `nrd_history.json`, `dod_map("z"/"dm"/"px")` день-к-дню, cap 40 дат — записывается на каждой загрузке юниверса; Δ появляется со 2-го дня).
- **Схемы**: `BondListItem` += spread_dur_yrs, z_pctile, delta_z_dod (по всему рынку) + carry_bps, days_to_refix, current_coupon_pct (watch). Новая модель `FloaterRisk` в detail. `nrd.py` universe items += current_yield_pct/nrd_calc_date, CACHE_VERSION→3 (regen).
- **Кросс-секция** (`_universe_bonds`): перцентиль z внутри рейтинг-бакета, spread duration = срок до погашения (по всем 456, без сети). Watch: carry/refix/current_coupon от MOEX-расписания + кривой.
- **Фронт**: 3 новые колонки (CARRY, z%ile с мини-баром, Δz D/D); Kpis += MEDIAN DM + REPRICED D/D; Drawer += секция «Флоатер-риск» (spread/rate duration, carry, breakeven, mod_dur/convexity/pvbp) + staleness-чипы per-source (fresh/stale по датам); `AnalyticsPanel.jsx` (тумблер 📊 АНАЛИТИКА в Toolbar) — 3 inline-SVG графика: scatter z vs spread duration (цвет=рейтинг), распределение z по бакетам (p25-медиана-p75), профиль рефиксинга watchlist. Валидировано превью: медиана z монотонна по бакетам AAA 325→B 1914 (sanity ✓), 456 строк, консоль чистая, light+dark.

## ✅ ИСПРАВЛЕНО (quick wins, 2026-07-01)
- Блокировка event loop: `get_curves` теперь async (`asyncio.to_thread`), `get_access_token` (Alor) уводится в поток из всех async-хендлеров (bonds/meta/curves/orderbook). Cbonds-fetch и bootstrap больше не вешают сервис.
- Гонки токен-кэша: `auth.py` — `threading.Lock` + double-check + атомарная запись (`os.replace`); `nrd.py` — `asyncio.Lock` (`_ensure_token`) + атомарный `_save_json`.
- НРД масштаб спредов — `_sane_bps` guard (±20000 bps): нетипичные единицы → None + warning.
- Семафор MOEX: `_MOEX_SEM = Semaphore(8)` + `_moex_get` (ретрай на 429/5xx) для snapshot/securities/schedules.
- TTL кривых: `_load_curves_sync` инвалидирует кэш при смене даты.
- Честный `is_stale` в detail: `rates_date < today`.
- Дедуп `fetch_nrd_metrics` в detail (был 2×, стал 1×).
- Фронт: WS reconnect-таймер чистится в `close()` — нет зомби-сокетов.

## 🎯 NRD-STYLE ПАЙПЛАЙН z-СПРЕДА (2026-07-01, стенд `scripts/nrd_pipeline_probe.py`)
Построен пайплайн под методику (met_rub п.4.9): прогноз купонов на кривой ОЖИДАНИЙ индекса (кубич.сплайн из свопов) → дисконт по **КБД ОФЗ непрерывно** `exp(−(r_g(τ)+z)τ)` → solve **z-спред**. Цель: наш z ≈ НРД `z_spread`.
- **КБД ОФЗ (G-curve)**: `iss/engines/stock/zcyc.json?iss.only=yearyields` — готовые OFZ zero-yields (0.25y→13.6%, 3y→14.6%, 10y→16.4%). Восходящая; short-end ≈ текущий КС ~14%.
- **Результат сверки**: RUONIA **±7–70bps (медиана ~40)** — методика воспроизведена ✅. KEYRATE **РЕШЁН 2026-07-02** (см. ниже), было систематически −150…−208.
- **KEYRATE-РАЗГАДКА (2026-07-02, реверс-инжиниринг)**: публикуемый НРД `z_spread` = **`ytm_eff − G(горизонт рефиксинга)`** — производная от их YTM, а НЕ независимый solve п.4.9 по всей кривой. Подтверждено на 68 бумагах nrd_cache: медиана |ytm−G(dur)−z| = 4–13bps. `duration` НРД у флоатера = ДЛИНА КУПОННОГО ПЕРИОДА (месячный → 0.08), G клэмпится к короткому концу КБД (~13.6%). Наш старый −150/−208 = разница между средней G по потокам до погашения (~15%+) и G на коротке. Их ytm — из проприетарной fair-value машины (NSS+Kalman, имплайд-купоны шумные), но наш y из projected-CF воспроизводит его достаточно.
- **Наш прод-аналог (V7)**: y = плоская непрерывная доходность наших projected CF (`solve_flat_y`), z = (e^y−1) − G(τ_reset), τ_reset = длина текущего купонного периода. **Сверка: watch-выборка (12 KEYRATE) mean|Δ|=43, медиана −17; live API mean|Δ|=27; юниверс (93) медиана −13; AAA..A mean|Δ|=86, остаток растёт к низким рейтингам (BB+138/647) — кросс-секция NSS+Kalman эмитента, из публичных данных несводимо. Устойчив во времени (12 дат июня: bias≈0, std 17–32bps = сглаживание Калмана + рыночный шум).** RUONIA-путь не тронут (старый solve по кривой, бит-в-бит).
- **История НРД доступна**: `valuationnewadd` с `filter={"isin": X}` отдаёт дневные строки с ~мая 2026 (фильтр `val_date $gte/$lte` НЕ работает — резать локально). Пригодно для day-over-day аналитики/бэктестов.
- **СПФИ КС-кривая подключена**: `rates.get_spfi_curve()` — 13 GET `cbonds.ru/indexes/{ID}` (98488 3M … 98510 10Y), regex `"actual_value.numeric"`, кэш `spfi_cache.json` на день, фолбэк hardcode 30.06 с warning. Для фикса оказалась НЕ нужна (кривая ниже IRS-effective → купоны ниже), оставлена как источник данных. Метод.СПФИ: `fs.moex.com/f/21666/20250324-ext-spfi-swaprates.pdf`.
- **Проверенные и ОТБРОШЕННЫЕ гипотезы** (матрица в `scripts/nrd_pipeline_probe.py`): spot-на-дату-фиксинга vs форвард (±2bps — кривая плоская), СПФИ-кривая (−322), сплайн с exp-экстраполяцией к ω=7.5% (эффект ~0 — короткие duration), лаг фиксинга 5дн (~0), купонное усреднение (~0), без effective-conv (−245, конверсия (1+r/4)^4−1 ПОДТВЕРЖДЕНА).
- **ПРОМОУТНУТО В ПРОД (2026-07-02)**: `services/zspread.py` — `compute_z_bps` ветвится по базе (KEYRATE → `solve_flat_y` + `current_period_len`; RUONIA → старый `solve_z_bps`). **Value-фикс**: `fetch_coupon_schedules` теперь отдаёт тройки (start, end, value) с TTL-кэшем на день (`schedule_cache.json` v2 `{"date","items"}`, старый формат отбрасывается) — зафиксированный купон больше не перепрогнозируется в проде. `bonds._universe_bonds` передаёт value в `compute_z_bps`; `calculate_valuation_metrics` нормализует тройки в пары. Стенд: `scripts/nrd_pipeline_probe.py` (матрица вариантов + `--universe` + паритет-проверка прод-модуля 17/17 ✓). UI-колонка **OUR Z** без изменений.

## ⚡ ПРОИЗВОДИТЕЛЬНОСТЬ (2026-07-01)
Диагноз: `iss.moex.com` медленный (~3.5с/запрос) и флаки под нагрузкой (ConnectTimeout при burst); каждый тогл watchlist/открытие карточки re-дёргали MOEX **последовательно**. Alor `get_last_prices_dict` ждал полные 10с (неликвид не шлёт котировку). Было: watch(8) 27–39с, карточка 10–17с. (NB: наивный `gather` без кэша сделал ХУЖЕ — 60–70с из-за шторма коннектов + ретрая таймаута.)
Фиксы: (1) `_universe_bonds`/`get_bond_details` — независимые вызовы через `asyncio.gather`; (2) `_moex_get` **fail-fast** на таймаут (ретрай только 429/5xx), семафор 8→5; (3) **кэш `fetch_moex_securities`** (память+диск `securities_cache.json`) + **TTL-кэш `fetch_moex_snapshot`** (120с) — повторные действия не бомбят ISS; (4) Alor WS-таймаут 10с→4с; (5) `api.js` — `extra` (watchlist) теперь идёт в universe-запрос (без него dirty/DM/coupon = None); (6) `next_coupon` из реального расписания MOEX + fallback цены (live→prev→НРД). **Итог: watch 4.3с cold / 1.3с warm; карточка 1.1с.** NB бэкенд `/api/ws/market` — пустой pass-through (нет фонового Alor-фида; цены только из блокирующего fetch при загрузке) — кандидат на постоянный фид.

## ⚠️ ИЗВЕСТНЫЕ ПРОБЛЕМЫ (аудит 2026-07-01, НЕ исправлены)
### CRITICAL — секреты (2026-07-01: РАЗРЕШЕНО)
- **Alor JWT + rates_cache в git-истории** — исправлено. Файлы были только в tip-коммите `4c7ee02`; переписан через `commit --amend` (added-then-removed → исчезли из дерева), force-push (`--force-with-lease`), `origin/main` теперь `1612955`, в удалённой истории секретов нет.
  - Факт-чек утечки: `.env` **никогда не коммитился** → НРД apikey и Alor **refresh** в git не попадали. В историю утёк только Alor **access-JWT** (уже **протух**, `exp` в прошлом) — торгового риска нет, но светил PII (client id / agreements / portfolios / scope OrdersCreate·Trades). `rates_cache.json` — не секрет.
  - **Остаточный риск**: старый коммит `4c7ee02` может ещё открываться на GitHub по прямому хешу (кэш GitHub GC) и остаётся в форках/старых клонах — данные считать засвеченными. Опц.: сделать репо private + попросить GitHub Support почистить кэш. Ротация Alor refresh/НРД apikey — перестраховка (не были в git), не блокер.

### HIGH
1. Блокировка event loop: `auth.py:39` (requests.post refresh, без timeout) + `rates.py` (Cbonds) — sync в async, вешают весь сервис. → httpx async / asyncio.to_thread.
2. Гонки токен-кэша (нет lock, не атомарная запись) → дубль-логины/rate-limit.
3. ~~Нет auth на API~~ — **исправлено** (см. «Авторизация»): гейт `require_user` на всех роутерах данных + WS, JWT-cookie, аккаунты через CLI. CORS всё ещё `*` (не критично — same-origin, credentials не отдаём).
4. ~~DM смещён: сетка начисления ≠ сетка дисконта~~ — **проверено 2026-07-08, НЕ баг** (купон начисляется за полный период — покупатель платит НКД за истёкший стаб; «фикс» ломал par-инвариант DM=spread). Реальные баги DM-cashflow (перепрогноз зафикс. купона, игнор амортизации) исправлены (см. «Расчёты»).
5. НРД масштаб спредов — эвристика ×10000/×100 без sanity-guard (nrd.py:116); нетипичные единицы → CF/DM ×100 врут.
6. Фронт: WS reconnect-таймер не чистится (api.js:56,70) — зомби-сокеты.

### MEDIUM
- TQCB борд захардкожен (market_data.py:220) — часть флоатеров без prev/accrued.
- N+1 MOEX без семафора; нет ретраев/429.
- Стейл: кривые кэшируются в процессе без TTL (после полуночи — вчерашние); `is_stale=False` захардкожен (bonds.py:360); WS-цены не сбрасываются по дате.
- `fetch_nrd_metrics` дважды в detail (bonds.py:301,411).
- ~~Амортизируемые: DM-модель от полного номинала~~ — **исправлено 2026-07-08**: обе модели (z и DM) учитывают амортизацию (принципал по графику bondization, купоны от остаточного номинала). NB: вторичные пути `get_bonds` (список по явным ISIN) и `/{isin}/valuation` amorts не получают (schedule_full там не грузится — N лишних ISS-запросов); фронт их не использует.
- `price_vs_nrd` от разных баз (список=wa_price, detail=fair||wa).
- Фронт: 453× useAnimationControls без React.memo + нет debounce поиска.

### LOW
- Молчаливые except без логов; print вместо logger; нет валидации ISIN; тесты только test_nrd; double-load на маунте (StrictMode); WS без unsubscribe; CWD-зависимые пути кэшей.

### Ложные срабатывания (проверены, НЕ баги)
- Фильтр рейтинга модификаторы — бэкенд нормализует в бакет, ок.
- Дубли key={isin} — в universe extra не добавляет строки отдельно, дублей нет (но watch-бумага вне юниверса не покажется).

## СЛЕДУЮЩИЕ ШАГИ (по приоритету)
1. **Security**: ротация Alor/НРД токенов + чистка git-истории (нужно решение по force-push).
2. **Quick wins** (безопасно): async-обёртка блокирующих вызовов; TTL кривых + честный is_stale; дедуп fetch_nrd_metrics в detail; семафор MOEX; WS-timer cleanup; guard масштаба НРД; guard base=UNKNOWN.
3. **Крупное**: переписать дисконтирование DM под реальные периоды (свести с НРД); auth на API; амортизация в DM-модели (в z-модели сделана 2026-07-02); кривая под методику НРД для <10bps совпадения.
4. Опции: watchlist на бэкенд (шаринг); внутридневной НРД (valuationnewaddintraday); графики кривых/стакан в drawer.

## Файлы кэшей (gitignored, локальные)
`nrd_cache.json`, `nrd_universe_cache.json`, `shortnames_cache.json`, `schedule_cache.json`, `*_token_cache.json`, `rates_cache.json`, `isins_cache.json`, `.env`, `frontend-react/dist/`. `isins.txt` (16 старых ISIN) + `bondsearch_20_11_2025.xlsx` — устаревший снапшот, юниверс теперь из НРД (не привилегированы).
