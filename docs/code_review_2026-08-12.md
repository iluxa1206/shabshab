# Ревью проекта shabshab — 2026-08-12

**Охват:** бэкенд FastAPI (~35k LOC Python) + фронт React (~11.5k LOC). Ветка `floaters-desk-deploy`.
**Метод:** блочный разбор по функционалу (11 блоков) + сквозной скан на уязвимости, ресурсы, зацикливания, мёртвый код.
**Общий вывод:** база зрелая и дисциплинированная. Единый паттерн: WAL+lock у SQLite, экспоненциальный backoff у WS, day-кэши MOEX, отдельный heavy-executor против фриза ядра, fail-closed авторизация. **Реальных дыр безопасности не найдено.** Под правку — одна корректностная находка (гонка кэша расчёта), остальное — эффективность и чистота.

---

## Приоритетный список

| # | Severity | Файл | Проблема | Рекомендация |
|---|----------|------|----------|--------------|
| 1 | 🔴 MEDIUM (корректность) | `services/coupon_calib.py:680` / `services/universe.py:306` | Гонка на глобальном кэше расчёта между двумя путями на разных потоках → тихо неверные DM/SM | Пустить `compute_watch_metrics` через `run_heavy` |
| 2 | 🟠 LOW (корректность) | `core/valuation.py:107` | Латентный бесконечный цикл при `coupons_per_year > 12` | `step_months = max(12 // coupons_per_year, 1)` + guard |
| 3 | 🟡 Эффективность | `services/universe.py:262` | N+1 обращений к SQLite в backfill | Bulk-read всех строк реестра раз перед циклом |
| 4 | 🟡 Наблюдаемость | весь код (229 мест) | Широкий `except Exception` глушит реальные баги наравне с сетью | В сетевых воркерах сузить типы исключений |
| 5 | 🟡 Память | `api/main.py` (`universe_price_poller`) | Полный пересчёт универса каждый такт → каветат OOM 768MiB | Инкрементальный пересчёт изменившихся ISIN |
| 6 | 🟢 Косметика | `core/forwards.py:346`, `api/routes/tg.py:128`, `api/main.py:840` | guard в while-True; `compare_digest` на webhook-secret; `reload=True` из репо | Мелкие правки |

---

## Детали находок

### 1. 🔴 MEDIUM — Гонка на глобальном кэше расчёта

**Файлы:** `services/coupon_calib.py:680` (`_GROW_PATH`, `_lvl_cache`), `services/universe.py:306`

Метрики флоатеров считаются двумя путями, **на разных потоках**:

- `compute_universe_metrics._crunch` → обёрнут в `run_heavy` (выделенный single-worker поток), `services/universe.py:281`.
- `compute_watch_metrics` → per-bond цикл выполняется **прямо в корутине на event loop**, `services/universe.py:306`; дёргается на каждый запрос `/api/bonds` (`api/routes/bonds.py:116`).

Оба вызывают `enrich_bond` → `coupon_calib._index_grow_cached`, который мутирует **глобальные переменные без локов**:

```python
_GROW_PATH: dict = {"key": None, "levels": None, "state": None}   # stateful дневная рекуррентность
_lvl_cache: dict = {"key": None, "map": None, "last": None}
```

Фоновый пересчёт универса (heavy-поток, замер ~33с CPU) и запрос watchlist (event loop) **пересекаются во времени**. Два потока одновременно перестраивают/итерируют `_GROW_PATH` с разными ключами → промежуточные уровни индекса портятся → **неверные DM/SM в этот такт**. Самовосстановление следующим циклом, но пользователю в момент пересечения отдаётся мусор без предупреждения.

**Рекомендация (одна правка, убирает и гонку, и блокировку event loop):** вынести per-bond цикл `compute_watch_metrics` в `_crunch` и пустить через тот же `run_heavy` — единственный worker сериализует оба пути:

```python
# services/universe.py, compute_watch_metrics
from services.heavy import run_heavy
def _crunch() -> dict:
    out: dict = {}
    for u in uni_rows:
        ...
    return out
return await run_heavy(_crunch)
```

Побочно решает и то, что сейчас бисекции солверов watchlist крутятся на event loop (блокировка, пусть N мал).

---

### 2. 🟠 LOW — Латентный бесконечный цикл `generate_coupon_dates`

**Файл:** `core/valuation.py:107`

```python
step_months = 12 // coupons_per_year   # coupons_per_year=365 → 0
...
current = first_coupon_date
while current <= maturity_date:
    dates.append(current)
    current = add_months(current, step_months)   # add_months(x, 0) не двигает → вечный цикл
```

При `coupons_per_year > 12` (битый фид, дневной купон) `step_months` обнуляется, `add_months` не сдвигает дату → воркер виснет. Нужен только некорректный вход, но цена страховки — одна строка.

**Рекомендация:** `step_months = max(12 // coupons_per_year, 1)` либо ранний выход при `coupons_per_year > 12`. Аналогично добавить `guard`-счётчик в `while True` у `core/forwards.py:346` (`_fixed_leg_schedule`) по образцу `extend_periods_to_maturity` (там уже `guard < 600`).

---

### 3. 🟡 Эффективность — N+1 к SQLite в backfill купонного периода

**Файл:** `services/universe.py:262-276`

Цикл backfill `coupon_period_days` вызывает `instruments_registry.get(isin)` по каждой бумаге (~540) — каждый вызов открывает новое соединение SQLite. До 540 обращений за 10-минутный такт на heavy-потоке.

**Рекомендация:** прочитать все строки реестра одним `SELECT` в dict перед циклом (внутренний bulk-`SELECT` уже есть в модуле) и сравнивать по памяти; запись — только при расхождении, как сейчас.

---

### 4. 🟡 Наблюдаемость — широкий `except Exception`

**Где:** 229 мест по коду (сетевые воркеры, фоновые циклы).

Паттерн `except Exception: logger.warning(...)` корректен для устойчивости воркеров, но одинаково глушит сетевые сбои и реальные баги (`KeyError`, `AttributeError`, `TypeError`). Логика ошибок тонет в шуме сетевых предупреждений.

**Рекомендация:** в сетевых путях сузить до конкретики — `except (httpx.HTTPError, aiohttp.ClientError, asyncio.TimeoutError)`; программные исключения пусть всплывают или логируются отдельным уровнем.

---

### 5. 🟡 Память — полный пересчёт универса каждый такт

**Файл:** `api/main.py` (`universe_price_poller`, `compute_universe_metrics`)

Полный крунч ~540 бумаг каждые 10 минут. Смягчён day-кэшем MOEX, но известен каветат OOM 768MiB (см. память `legacy-est-spread-daily`).

**Рекомендация:** инкрементальный пересчёт только изменившихся ISIN (по свежести цены/расписания), а не всего универса каждый такт.

---

### 6. 🟢 Косметика (defense-in-depth, не эксплуатируется)

- `api/routes/tg.py:128` — сравнение webhook-secret через `!=`, не `hmac.compare_digest`. Тайминг-атака теоретическая, риск ≈ 0. Для единообразия с `tg_webapp` заменить на `compare_digest`.
- `api/main.py:840` — `uvicorn.run(..., reload=True)` в `__main__`. Только локальный dev (прод через docker/uvicorn), но убрать из репо-примера, чтобы не скопировали в прод.
- `services/signals.py:270` — `_candidates_cache` без эвикции. Ограничен числом фильтров × размер универса; при росте числа фильтров подрастёт память. Low.

---

## Визуал / UX

Фронт — грамотный дата-плотный десктоп-терминал. Серьёзных UX-дыр нет, замечания мелкие.

| # | Severity | Файл | Замечание | Рекомендация |
|---|----------|------|-----------|--------------|
| V1 | 🟡 A11y-контраст | `frontend-react/src/styles.css:16` | Зелёный `--up: #157a37` на белом ≈ **4.3:1** — чуть ниже AA 4.5 для мелкого текста (11–13px = normal). `--down: #c0271f` ≈ 4.9:1 — ок. | Затемнить зелёный до ~`#0f6b2f` (≈ 5:1) |
| V2 | 🟢 A11y-цвет | `frontend-react/src/components/BondTable.jsx:168` + цветные ячейки | Дельты несут знак «+/−» (`fmt.signed`, `format.js:11`) → дальтоникам ок. Но часть цветных ячеек (спред/DM/rich-cheap) окрашена без знака/иконки. ~8% мужчин red-green. | Где цвет — единственный носитель, добавить знак или стрелку ▲▼ |
| V3 | 🟢 Мобилка | `frontend-react/src/styles.css:95` | Базовый шрифт `13px`, метки `11px`. Плотный desktop-first терминал; на телефоне мелко. <16px на инпутах → авто-зум iOS. | Для деск-аудитории приемлемо; при желании поднять инпуты до 16px |
| V4 | 🟢 Вкус | `frontend-react/src/styles.css:35` | Тёмная тема `--bg: #000000` — чистый чёрный + яркий текст даёт halation/усталость части глаз. | Косметика; многие терминалы берут `#0a0a0a` |

**Здорово (проверено):**

- 3 темы (light / dark / grey) с корректным `color-scheme` + CSS-переменными.
- Sticky заголовки таблиц + липкие колонки (звезда / имя) — навигация по плотным данным.
- 70 обработчиков пустого состояния; loading-UI в 10+ тяжёлых панелях.
- `prefers-reduced-motion` (3 медиа-блока); Esc-close меню/дровера (7 мест).
- 93 `aria-*`, 46 `role=`, только 3 `onClick` на `div/span` (остальное — семантические `<button>`, 153 шт).
- Нет `<img>` (иконки — inline SVG, `alt` не требуется), нет `dangerouslySetInnerHTML`, нет data-driven редиректов.
- `viewport` meta на месте; локаль-формат (десятичная запятая, «м/г/д»); адаптивные таблицы (`overflow-x: auto` + `body overflow-x: hidden`).

---

## Что проверено и признано здоровым

- **Авторизация:** все data-роутеры гейтятся `_gate=[Depends(require_user)]` на include-уровне (`api/main.py:789-802`). Публичны только `health`, `auth/login`, `ws` (cookie внутри хендлера), `tg/webhook` (secret-заголовок). Дырявых эндпоинтов нет.
- **JWT:** fail-closed без `AUTH_SECRET`, версия пароля `pv` инвалидирует старые токены, сверка с хранилищем при каждом декоде.
- **Пароли:** bcrypt + dummy-hash против user-enum, `verify` в threadpool, rate-limit логина с корректным разбором `X-Forwarded-For` (правый IP от Caddy).
- **SQL:** все динамические f-string в запросах — литералы колонок и плейсхолдеры `?`, значения через bound-args. Инъекций нет (`tape`, `signals`, `instruments_registry`, `block_trades`, `trades_archive`, `tg_screener`).
- **Инъекции кода:** ноль `eval/exec/pickle/os.system/shell=True/yaml.load`.
- **TG Mini App:** `initData` валидируется HMAC-SHA256 + `hmac.compare_digest` + проверка свежести `auth_date`.
- **Фронт:** cookie httpOnly (токен не в JS), `credentials: same-origin`, нет `dangerouslySetInnerHTML`, нет data-driven редиректов.
- **WS-циклы:** экспоненциальный backoff `min(backoff*2, 30)`, уважают `stop.is_set()`, корректно ловят `CancelledError`. Tight-reconnect нет.
- **Конкурентность:** SQLite в WAL с `_lock` на записи; тяжёлый CPU изолирован в single-worker `run_heavy` (второе ядро всегда у event loop).
- **Мёртвый код:** корневые дубли (`cashflow.py`, `valuation.py`, `forwards.py`, `orderbook.py`, `evaluate_math.py`, `parse_isins.py`, `readisins.py`) уже удалены в ветке; логика съехала в `core/` + `services/`.

---

## Блочная карта (для навигации)

| Блок | Модули |
|------|--------|
| 1. Ядро расчёта | `core/valuation, forwards, cashflow, rates, last_prices, orderbooks` |
| 2. Оценка флоатеров | `services/valuation, metrics, coupon_calib, zspread, implied_curve, ks_path, backdate` |
| 3. Рыночные данные/стримы | `services/market_data, alor_ws, universe_stream, live_quotes, bars` |
| 4. Универс/реестр | `services/instruments_registry, instruments_sync, universe, ref_data, ratings, enrich_*` |
| 5. Фиксы/кривые | `services/fixed_income, cbr, cbr_forecast, benchmarks, curve_history` |
| 6. Сделки/архивы | `services/tape, trades_archive, block_trades, bars, spread_history` |
| 7. Стакан | `services/orderbook_svc, depth, core/orderbooks` |
| 8. Сигналы/алерты/скринер/TG | `services/signals, alerts, screener_core, tg_*` |
| 9. Аудит/справочник | `services/bond_audit, bond_details, spec_backtest, instruments_validate` |
| 10. Инфра/auth | `services/auth_users, portfolio_db, progress, heavy`, `api/main.py` |
| 11. Фронт | `frontend-react/src` |
