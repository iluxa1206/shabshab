# ТЗ: API для фронтенда поверх текущего проекта

## 1. Цель

Нужно реализовать HTTP API, которое отдаёт фронтенду:

- список отслеживаемых облигаций;
- карточку облигации с референсными данными;
- рыночные данные;
- рассчитанные метрики;
- прогнозный cashflow;
- служебную информацию о дате расчёта, источниках и статусе данных.

API должно использовать уже существующую математику и текущие интеграции проекта, а не переписывать бизнес-логику с нуля.

## 2. Текущее состояние проекта

Проект сейчас представляет собой набор Python-скриптов без выделенного backend-слоя.

Основные модули:

- `rates.py`
  - загрузка рыночных ставок OIS RUONIA и IRS KEYRATE;
  - локальный кэш `rates_cache.json`.
- `forwards.py`
  - построение кривых;
  - расчёт implied rates и forwards;
  - логика согласована с Excel-листом `IRS`.
- `cashflow.py`
  - загрузка и кэширование референсных данных по облигациям;
  - получение параметров из MOEX, floaters.ru и локального Excel;
  - расчёт и печать cashflow.
- `valuation.py`
  - расчёт модельных купонов;
  - dirty price;
  - XIRR;
  - spread к базовой ставке.
- `last_prices.py`
  - получение last price через WebSocket Alor;
  - агрегация краткой рыночной информации.
- `parse_isins.py`
  - основной CLI-сценарий;
  - объединяет все источники данных и выводит агрегированный результат.
- `orderbook.py`
  - отдельный CLI для стакана.
- `auth.py`
  - получение access token Alor;
  - кэш токена.

Вывод по структуре:

- бизнес-логика уже есть;
- API-слоя нет;
- нет HTTP-контрактов и pydantic-схем;
- нет выделенного service/repository слоя;
- основная orchestration-логика сейчас находится в `parse_isins.py`;
- код рассчитан на CLI, а не на многократные API-вызовы.

## 3. Архитектурная цель

Нужно не "обернуть `parse_isins.py` как есть", а выделить API-слой поверх существующих модулей.

Нужна структура:

- `app.py` или `api/main.py`
  - создание `FastAPI`;
  - регистрация роутеров.
- `api/schemas.py`
  - pydantic-модели запросов и ответов.
- `api/routes/*.py`
  - роутеры.
- `services/market_data.py`
  - загрузка last price, кривых, референсных данных.
- `services/bonds.py`
  - сборка карточки бумаги.
- `services/cashflow.py`
  - подготовка cashflow в JSON.
- `services/valuation.py`
  - подготовка valuation-ответов.

Важно:

- расчётные формулы должны оставаться в `forwards.py`, `cashflow.py`, `valuation.py`;
- HTTP-слой не должен содержать бизнес-математику;
- роутеры должны работать через сервисы, а не напрямую через большие CLI-скрипты.

## 4. Что нельзя делать

- Нельзя вызывать `parse_isins.py` как subprocess из API.
- Нельзя парсить текстовый stdout CLI ради фронта.
- Нельзя возвращать "сырые" поля от разных источников без нормализации.
- Нельзя смешивать WebSocket-подключение Alor, бизнес-логику и сериализацию ответа в одном endpoint.

## 5. Основные сущности API

### 5.1 BondReference

Референсные данные по бумаге:

- `isin`
- `short_name`
- `face_value`
- `face_unit`
- `base_rate_type`
- `spread_bps`
- `formula`
- `start_date`
- `maturity_date`
- `coupon_period_days`
- `coupons_per_year`
- `next_coupon_date`
- `accrued_interest`

### 5.2 BondMarketData

Рыночные данные:

- `last_price_pct`
- `price_source`
- `calc_date`
- `rates_date`
- `market_timestamp` или `as_of`

### 5.3 BondValuation

Рассчитанные метрики:

- `clean_price_pct` — чистая цена в процентах (Alor, либо модельная)
- `dirty_price_rub` — грязная цена в рублях (чистая + НКД)
- `dm_bps` — дисконтный маржин (discount margin, в bps)
- `dm_label` — тип DM (например, `to_maturity`, `to_call`, и т.п.)
- `yield_xirr_pct` (опционально, для UI; в v1 может считаться или быть отложено) — доходность XIRR по cashflow
- `base_yield_pct` (опционально/вторично, если используется DM) — базовая ставка по cashflow base
- `spread_to_base_bps` (опционально/вторично, если используется DM) — спред к базе (разница XIRR)
- `pricing_status` — статус расчёта/валидности
- `warnings` — массив предупреждений

Пояснения:
- `spread_issue_bps` — спред, указанный в формуле выпуска (фиксированный к базе)
- `spread_to_base_bps` — разница между доходностью XIRR бумаги и базовой ставки (расчётная метрика)
- `dm_bps` — дисконтный маржин, сравнивает доходность бумаги с forward'ами по базе (основная метрика для floaters)
DM является основным сравнительным показателем для флоатеров; XIRR-метрики (yield_xirr_pct, spread_to_base_bps) вторичны и могут использоваться для UI.
#### Schedule (купонные даты)
Приоритет получения расписания купонов:
1. Таблица/график купонов MOEX ISS, если доступен
2. Fallback (v1): `FIRST_COUPON_DATE + step_months (12/4/2 → 1/3/6 месяцев)` с `CalendarMode=NONE`
3. Только если выше недоступно — из Excel

#### Formula (base + spread_issue_bps)
Приоритет получения формулы:
1. floaters.ru
2. Excel
3. эвристический парсинг

Примечание: поле `STARTDATE` с floaters.ru (Размещение) нельзя использовать как начало купонного периода.

### 5.4 CashflowItem

Одна строка cashflow:

- `number`
- `period_start`
- `period_end`
- `payment_date`
- `coupon_formula`
- `base_rate_pct`
- `spread_bps`
- `coupon_rate_pct`
- `amount_rub`
- `type`

`type`:

- `COUPON`
- `REDEMPTION`

### 5.5 BondDetailsResponse

Полная карточка облигации:

- `reference`
- `market`
- `valuation`
- `cashflow`
- `sources`
- `warnings`

## 6. Предлагаемые endpoint'ы

### 6.1 `GET /api/health`

Назначение:

- healthcheck для фронта и деплоя.

Ответ:

- `status`
- `version`
- `time`

### 6.2 `GET /api/bonds`

Назначение:

- вернуть список облигаций из `isins.txt` с краткой информацией.

Параметры:

- `with_market=true|false`
- `with_valuation=true|false`

Ответ:

- массив объектов:
  - `isin`
  - `short_name`
  - `base_rate_type`
  - `formula`
  - `maturity_date`
  - `next_coupon_date`
  - `last_price_pct`
  - `yield_xirr_pct`
  - `spread_to_base_bps`

Примечание:

- это основной endpoint для таблицы/списка на фронте.
- при `with_market=false` не нужно открывать WebSocket Alor.

### 6.3 `GET /api/bonds/{isin}`

Назначение:

- вернуть полную карточку одной бумаги.

Ответ:

- `reference`
- `market`
- `valuation`
- `cashflow`
- `sources`
- `warnings`

### 6.4 `GET /api/bonds/{isin}/cashflow`

Назначение:

- вернуть только прогнозный cashflow по бумаге.

Ответ:

- `isin`
- `calc_date`
- `currency`
- `items`
- `redemption_amount`

### 6.5 `GET /api/bonds/{isin}/valuation`

Назначение:

- вернуть только valuation.

Ответ:

- `isin`
- `calc_date`
- `clean_price_pct`
- `dirty_price_rub`
- `yield_xirr_pct`
- `base_yield_pct`
- `spread_to_base_bps`
- `warnings`

### 6.6 `GET /api/curves`

Назначение:

- вернуть текущие узлы и сегменты кривых для отладки и графиков.

Параметры:

- `type=ruonia|keyrate|all`

Ответ:

- `calc_date`
- `curve_type`
- `nodes`
- `segments`

Где:

- `nodes`: массив `{date, df}`
- `segments`: массив `{start_date, end_date, forward_rate_pct}`

### 6.7 `GET /api/orderbook/{isin}`

Назначение:

- вернуть текущий стакан по бумаге.

Статус:

- необязательный endpoint первой очереди;
- можно вынести во вторую очередь.

## 7. Формат ответа для фронта

Все даты возвращать только в ISO формате:

- `YYYY-MM-DD`
- для timestamp: `YYYY-MM-DDTHH:MM:SSZ` или timezone-aware ISO.

Все проценты возвращать в процентах, а не в долях:

- `15.23` означает `15.23%`

Все spread в basis points:

- `125` означает `125 bps`

Все денежные значения возвращать в абсолютной сумме:

- `1015.6`
- `43.01`

Не использовать в API:

- форматированный текст;
- `%` в строках;
- `RUB 1,000.00` строкой;
- смешение русского и английского форматов чисел.

## 8. Источники данных и приоритеты

Для одной бумаги данные сейчас собираются из нескольких мест.

Приоритеты нужно зафиксировать явно.

### 8.1 Референсные параметры

Приоритет:

1. MOEX
2. floaters.ru
3. локальный Excel `bondsearch_*.xlsx`

Логика:

- `SHORTNAME`, `MATDATE`, `NEXTCOUPON`, `ACCRUEDINT`, `FACEVALUE`, `FACEUNIT`, `COUPONPERIOD` брать из MOEX;
- `FORMULA`, `BASE_RATE`, `MARGIN` сначала брать из floaters.ru;
- если нет, брать из локального Excel;
- `STARTDATE`, `ENDDATE`, `FREQUENCY` можно добивать из Excel, если их не дал основной источник.

### 8.2 Рыночная цена

Источник:

- Alor WebSocket.

Fallback:

- если цена недоступна, API должен вернуть `last_price_pct = null` и warning;
- не подставлять "100.0" как рыночную цену в API-ответ.

### 8.3 Кривые ставок

Источники:

- Cbonds OIS RUONIA;
- Cbonds IRS KEYRATE;
- локальный `rates_cache.json`.

## 9. Бизнес-логика, которую нужно сохранить

### 9.1 Форварды

Форварды должны считаться по логике Excel-листа `IRS`, уже перенесённой в `forwards.py`.

Форварды ОБЯЗАТЕЛЬНО должны вычисляться через текущую реализацию `DiscountCurve.forward(t1, t2)`, основанную на DF с log-linear интерполяцией и FLAT_FORWARD экстраполяцией. Компаунинг должен строго соответствовать conventions в `forwards.py`.

### 9.2 Доходность

`yield_xirr_pct` должна считаться как XIRR:

- отрицательный поток в `calc_date` = `dirty_price`;
- далее будущие модельные купоны и погашение.

Примечание: Для флоатеров XIRR-метрики (доходность и spread к базе) являются вторичными и могут считаться для UI, но основная метрика — DM.

### 9.3 Spread к базе

`spread_to_base_bps`:

- считается как разница между:
  - `yield_xirr_pct` по cashflow `base + spread`;
  - `base_yield_pct` по cashflow только `base`.

Примечание: Для флоатеров spread к базе вторичен, основной показатель — дисконтный маржин (DM).

### 9.4 Конвенции расчётов (фиксируем для v1)

- Day count: ACT/365
- Settlement: T0
- Price: Alor clean %, Dirty = clean*face/100 + НКД
- RUONIA model: daily comp (соответствует bootstrap'у RUONIA OIS)
- KEYRATE model: quarterly comp через `((1+R/4)^(4α)-1)` (соответствует IRS bootstrap)
- Rounding: внутренние вычисления в decimal, в API-ответах bps округлять до int
- Купонные даты: business day adjust NONE
#### Рекомендации по пагинации и производительности

Рекомендуется поддерживать опциональные query-параметры:
- `limit`, `offset` — для постраничной выборки;
- `fields` — список полей через запятую (например, для облегчения ответа).

По умолчанию endpoint списка должен возвращать только "лёгкие" метрики (например, `dm_bps` для последней даты) и избегать тяжёлых расчётов, если не указан `with_valuation=true`.

## 10. Что нужно доработать перед/вместе с API

Это не блокеры для каркаса API, но важные технические долги.

### 10.1 Выделить orchestration из `parse_isins.py`

Нужна функция уровня сервиса, например:

- `get_bond_snapshot(isin: str) -> BondDetailsResponse`

Чтобы API и CLI использовали один и тот же код.

### 10.2 Убрать `print`-ориентированную логику

Сейчас `cashflow.py` и `parse_isins.py` сильно ориентированы на stdout.

Нужно:

- расчёт отдельно;
- сериализация отдельно;
- печать отдельно.

### 10.3 Нормализовать ошибки

API не должен возвращать traceback или молча съедать ошибку.

Нужно завести нормализованный формат ошибок:

```json
{
  "error": {
    "code": "MARKET_DATA_UNAVAILABLE",
    "message": "Last price is unavailable",
    "details": {
      "isin": "RU000A107456"
    }
  }
}
```

### 10.4 Развести cache/data/service слои

Сейчас кэширование размазано по файлам.

Нужно выделить:

- cache layer;
- source adapters;
- aggregation service.

## 11. Предлагаемая реализация первой очереди

### Этап 1

Сделать минимально рабочий backend:

- `GET /api/health`
- `GET /api/bonds`
- `GET /api/bonds/{isin}`
- `GET /api/bonds/{isin}/cashflow`
- `GET /api/bonds/{isin}/valuation`

Без:

- orderbook endpoint;
- server push;
- streaming updates.

### Этап 2

Добавить:

- `GET /api/curves`
- `GET /api/orderbook/{isin}`
- websocket endpoint для live-обновлений цены/стакана

## 12. Требования к реализации

- Использовать `FastAPI`, так как зависимость уже есть в проекте.
- Использовать `Pydantic`-схемы для всех ответов.
- Не читать Excel и не строить кривые на каждый endpoint повторно без кэширования.
- Предусмотреть memory cache хотя бы на время жизни процесса.
- Вынести конфигурацию в `.env`.
- Не хранить секреты в API-ответах.

## 13. Требования к производительности

- `GET /api/bonds` без рынка: целевое время ответа до `1s` при использовании кэша.
- `GET /api/bonds/{isin}`: целевое время ответа до `1s` при использовании кэша.
- Если нужен live price, допустим деградирующий сценарий:
  - либо background refresh;
  - либо ответ с последним закэшированным значением и флагом stale.

## 14. Требования к фронту

Фронту должно быть достаточно следующих данных для карточки:

- название;
- ISIN;
- формула купона;
- базовая ставка;
- спред;
- dirty price;
- yield xirr;
- base yield;
- spread to base;
- cashflow list;
- calc date;
- source warnings.

Фронт не должен:

- пересчитывать купоны;
- пересчитывать XIRR;
- восстанавливать формулу из кусков сырых данных.

## 15. Критерий готовности

Backend считается готовым, если:

- есть рабочий FastAPI-приложение;
- есть описанные выше endpoint'ы первой очереди;
- ответы типизированы через Pydantic;
- `GET /api/bonds/{isin}` возвращает полную карточку бумаги в JSON;
- фронт может построить:
  - список бумаг;
  - страницу одной бумаги;
  - таблицу cashflow;
  - блок valuation;
- расчёты API используют те же формулы, что и текущий код проекта.

## 16. Что должен сделать разработчик

1. Выделить service-функции из `parse_isins.py`.
2. Собрать FastAPI-приложение.
3. Описать Pydantic-модели ответа.
4. Реализовать endpoint'ы первой очереди.
5. Нормализовать ошибки и warnings.
6. Добавить минимальные тесты на сериализацию и один smoke-test на endpoint.


### 10.5 Проверки и smoke-tests (обязательные)

- Проверка FLAT_FORWARD кривой: `forward(5Y,5Y+1M) ≈ forward(4Y,5Y)`
- Валидация cashflow: нет купонных дат после даты погашения
- Dirty price использует НКД; cashflow не содержит НКД
- Если использован fallback (schedule/formula), добавить warning code