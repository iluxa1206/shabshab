# ТЗ: вкладка АУКЦИОН в разделе ФИКСЫ (`/fixed/auction`)

Дата: 2026-09-15. Ветка `floaters-desk-deploy`.
Новые файлы: `services/ofz_auctions.py`, `api/routes/auctions.py`,
`frontend-react/src/components/fixed/AuctionDesk.jsx` (+ `AuctionCharts.jsx`,
`AuctionDesk.test.jsx`), `scripts/backfill_ofz_auctions.py`,
`tests/test_ofz_auctions.py` (+ фикстуры). Правки: `api/main.py`
(include_router + ночной такт), `services/portfolio_db.py` (таблицы),
`frontend-react/src/components/Topbar.jsx` (SUBNAV), `App.jsx` (route),
`api.js`.

## Смысл

План/факт аукционов ОФЗ Минфина + история всех аукционов + аналитика:
сколько заняли против квартального плана, темп, спрос/покрытие, доходности
размещения, премия к вторичке, разрез по типам и срокам.

## Источники (проверены 2026-09-15, доступны и с ноута, и с прода
161.104.17.23; нужен браузерный User-Agent, без него 503)

### 1. Итоги аукционов — годовые xlsx Минфина

Страница-индекс: `https://minfin.gov.ru/ru/perfomance/public_debt/internal/operations/ofz/auction/`
В HTML ссылки вида
`/common/upload/library/2026/09/main/INTERNET_Auction_Results_rus_2026_20260910.xlsx`
(регэксп `INTERNET_Auction_Results_rus_(\d{4})_(\d{8})\.xlsx`). За текущий год
файл ПЕРЕВЫПУСКАЕТСЯ после каждого аукциона (дата в имени = «по состоянию
на»), за прошлые годы — финальные (2021…2025 есть в индексе). Берём для
каждого года ссылку с максимальной датой.

Формат (лист 1, 15 колонок; заголовок — строка, где `A == 'Дата'`; данные —
строки, где `A` — datetime; конец — строка `A == 'Итого'`):

| # | колонка | значение |
|---|---|---|
| 1 | Дата | datetime |
| 2 | Формат | `Аукцион` / `ДРПА` (доп. размещение после аукциона) |
| 3 | Код выпуска | `26253RMFS` — БЕЗ контрольной цифры; SECID = `SU`+код+цифра: искать в `sec_ref` по `secid LIKE 'SU26253RMFS%'` (там весь рынок с погашенными, ISIN тоже там) |
| 4 | Тип | `ОФЗ-ПД` / `ОФЗ-ПК` / `ОФЗ-ИН` / `ОФЗ-АД` / `ОФЗ-н` |
| 5 | Дата погашения | datetime |
| 6 | Дней до погашения | int |
| 7 | Объём предложения, млн руб | число; у части аукционов «в объёме остатков» — цифра сотни млрд (921 780) |
| 8 | Цена отсечения, % | число или `'-****'` (несостоявшийся) |
| 9 | Цена средневзвешенная, % | то же |
| 10 | Доходность по цене отсечения, % | число; у ПК — `'-****'` или 0 → NULL; у ИН — реальная |
| 11 | Доходность по средневзвешенной, % | то же |
| 12 | Спрос по номиналу, млн | число; у ДРПА `'-'` → NULL |
| 13 | Размещено по номиналу, млн | число (0 у несостоявшегося) |
| 14 | Выручка, млн | число |
| 15 | Коэффициент удовлетворения (13/12) | число или `'-'` |

Статусы: `ok`; `failed` — цены `'-****'`/размещено 0 и формат Аукцион;
`drpa` — формат ДРПА. Все `'-'`, `'-****'`, `''` → NULL, числа могут быть
строками с пробелами/запятой — нормализовать.

Локальные копии для фикстур/сверки лежат в scratchpad сессии, но проще
скачать заново: `res2026.xlsx` (79 строк), `res2025.xlsx` (155 строк).

### 2. Планы на квартал — HTML-страницы Минфина

Индекс: `https://minfin.gov.ru/ru/statistics/docs/auction` — ссылки
`auction?id_65=<id>-grafik_auktsionov_..._na_<римская>_kvartal_<год>_goda`
(есть с IV кв. 2023). На каждой странице текст документа ЕСТЬ в HTML (doc
качать не нужно):
- даты аукционов: `1 июля 2026 г.` … (регэксп по месяцам ru);
- таблица «Индикативное распределение планового объема привлечения средств
  … по срокам до погашения»: строки вида `до 10 лет включительно | 900`,
  `от 10 лет | 600` (III кв. 2026); в других кварталах корзины могут быть
  `до 5 лет`, `от 5 до 10 лет`, `от 10 лет` — парсить в (lo, hi) лет,
  `hi=NULL` для «от N лет», включительность — граница в hi.
- «уточненный» вариант квартала в индексе тоже бывает (IV кв. 2025) —
  побеждает страница с большим `id_65`.
- Квартал из заголовка: римская цифра I–IV + год → `2026Q3`.

ВАЖНО про смысл плана: это «объём привлечения средств» по ст. 113 БК —
деньги без НКД и без премии сверх номинала. Факт для сравнения с планом:
`proceeds_113 = размещено_по_номиналу × min(ср.взвеш.цена, 100)/100`
(дисконтные длинные ОФЗ по 58–92 % дают выручки заметно меньше номинала —
поэтому «по номиналу» и «по 113-й» расходятся на треть). В UI показываем
ОБА факта, план/факт-прогресс — по 113-й, подпись объясняет.

### 3. Вторичка для премии (уже в базе)

`spread_daily` (kind=fixed, есть с ~2026-07) — вечерний YTM по ISIN на дату.
Премия аукциона = `wap_yield − ytm_вторички(T−1 торговый)` в бп; нет данных —
NULL, не считать «0». ISIN — из `sec_ref`. Не реприсить движком (дорого).

## Хранилище (`services/portfolio_db.py`, CREATE TABLE IF NOT EXISTS)

```
ofz_auction(date TEXT, code TEXT, fmt TEXT,           -- PK(date, code, fmt)
  secid TEXT, isin TEXT, sec_type TEXT, maturity TEXT, days_to_mat INTEGER,
  offered_mln REAL, cut_price REAL, wap_price REAL, cut_yield REAL, wap_yield REAL,
  demand_mln REAL, placed_mln REAL, revenue_mln REAL, fill_ratio REAL,
  status TEXT, src_file TEXT, at TEXT)
ofz_auction_plan(quarter TEXT, bucket TEXT,            -- PK(quarter, bucket)
  lo_y REAL, hi_y REAL, amount_bln REAL, src_url TEXT, src_id INTEGER, at TEXT)
ofz_auction_dates(quarter TEXT, date TEXT, PRIMARY KEY(quarter, date))
ofz_auction_sync(key TEXT PRIMARY KEY, at TEXT, note TEXT)   -- 'results:2026' → имя файла
```

## Сервис `services/ofz_auctions.py`

- `parse_results_xlsx(bytes) -> list[dict]` — чистая функция.
- `parse_plan_html(html, url) -> {quarter, dates[], buckets[]}` — чистая.
- `async sync_results(years=None)`: индекс → ссылки по годам → качать, если
  имя файла отличается от `ofz_auction_sync['results:<year>']` → парсить →
  upsert → резолв secid/isin через `sec_ref` (LIKE; при 2+ совпадениях —
  логировать и взять с `is_traded` или `mat_date` = дате погашения).
  По умолчанию years = [текущий, прошлый]; бэкфилл — все из индекса.
- `async sync_plans()`: индекс кварталов → страницы, которых нет в
  `ofz_auction_plan` (по src_id) + всегда текущий и следующий квартал.
- httpx, UA `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36`,
  таймаут 30, follow_redirects; сеть только в sync-функциях.
- Аналитика (чистые функции над строками из базы):
  - `plan_fact(quarter)`: по корзинам и итого: `plan_bln`, `fact_nominal_bln`,
    `fact_113_bln`, `pct_113`, `auctions_held/auctions_planned`, `next_date`,
    `remaining`, `need_per_auction_bln` ((план−факт113)/оставшиеся),
    `avg_per_auction_bln`, `pace` (факт113 / (план × доля прошедших дат)).
    Корзина по `days_to_mat/365.25` на дату аукциона; ПК/ИН включаются в
    факт по сроку. ДРПА суммируется в факт (это тоже привлечение).
    Квартал без плана (старые) — только факт.
  - `results(from, to, sec_type, fmt, secid, status)`: строки + производные
    `term_y`, `bid_cover = demand/placed`, `proceeds_113_mln`, `premium_bps`.
  - `series(from, to)`: по датам аукционов: `placed_mln`, `demand_mln`,
    `proceeds_113_mln`, `n`, `n_failed`, `wap_yield_w` (взвешенная
    размещением, только ПД), плюс кумулятив внутри квартала.
  - `stats(from, to)`: итого/средние: спрос, размещение, bid-to-cover медиана,
    доля несостоявшихся, доля ДРПА, доля по типам и корзинам, топ-5 аукционов
    по размещению, средняя премия к вторичке.
  - `by_issue()`: по secid: n аукционов, размещено всего, последний аукцион,
    средняя wap-доходность, диапазон цен.
  - `quarters()`: список кварталов с планом и/или фактом.

## API `api/routes/auctions.py`, префикс `/api/auctions`, `dependencies=_gate`

`GET /quarters`, `GET /plan?quarter=2026Q3` (без quarter — текущий),
`GET /results?from&to&type&fmt&secid&status`, `GET /series?from&to`,
`GET /stats?from&to`, `GET /issues`, `POST /sync` (админ, как у
`/api/primary/placements/sync`; тело `{results: true, plans: true, years: []}`).
Даты — ISO; дефолт периода — год назад.

## Планировщик (`api/main.py`)

Рядом с ночным блоком `placements` (строка ~1173): `sync_results()` +
`sync_plans()` ночью; отдельно — в среду с 15:00 до 20:00 МСК раз в 30 мин
`sync_results()` (Минфин выкладывает итоги в день аукциона). На старте, если
`ofz_auction` пуста, — фоновый `sync_results()` за текущий и прошлый год
(не блокировать старт). `scripts/backfill_ofz_auctions.py --all` — все
годы из индекса + все планы.

## Фронт

- `Topbar.jsx` SUBNAV fixed: после `["/fixed/ofz", "ОФЗ"]` —
  `["/fixed/auction", "Аукцион"]`. `App.jsx`: route `/fixed/auction` под
  флагом `fixedOn`, как `/fixed/ofz`. Типизация пути `/fixed*` → fixed уже
  работает.
- `AuctionDesk.jsx` — три вида, переключатель чипами в шапке (состояние в
  localStorage `auctionView`): **ПЛАН/ФАКТ**, **ИСТОРИЯ**, **ВЫПУСКИ**.
  - ПЛАН/ФАКТ: селектор квартала; KPI-плашки (план, факт 113 / по номиналу,
    % выполнения, аукционов прошло/план, следующая дата, «надо на аукцион»
    против «в среднем размещали»); прогресс-бары по корзинам; график:
    кумулятив факт-113 по датам аукционов квартала против прямой равномерного
    плана; под ним столбики размещения по датам (стек по типу ПД/ПК/ИН) +
    линия спроса.
  - ИСТОРИЯ: фильтры периода (чипы КВ/ГОД/ВСЁ + даты), тип, формат, статус,
    поиск по выпуску; таблица (дата, выпуск, тип, формат, срок лет,
    предложение, спрос, размещено, выручка, 113-я, удовл. %, bid/cover, цена
    отс., цена ср.взв., YTM отс., YTM ср.взв., премия бп, статус). Строка
    несостоявшегося — приглушённая с меткой; ДРПА — тонкой строкой под своим
    аукционом. Клик по выпуску → `onOpen(isin, "fixed")` (карточка фикса),
    если isin известен. Сортировка по любой колонке, суммы в футере.
    График над таблицей: wap-доходность аукционов по датам (точки, цвет по
    корзине срока) — «где Минфин занимал».
  - ВЫПУСКИ: таблица по secid (n, всего размещено, последний аукцион, средняя
    доходность, диапазон цен), клик → фильтр ИСТОРИИ по выпуску.
- Графики — свой SVG (`charts/`: `MeasuredSvg`, `stackedBars`, `linePath`,
  `linearScale`, ось/сетка), размер по контейнеру, тема-ориентированные
  CSS-переменные как в `OfzChart`. lightweight-charts не брать.
- Стиль страницы — как у `OfzDesk`/`PrimaryCalendar` (классы `ia-*`,
  `fmt` из `format.js`, миллиарды — `fmt`-хелпер с 1 знаком).
- Пустое состояние: «Итоги аукционов ещё не загружены» + кнопка «обновить»
  для админа (`POST /sync`).
- `api.js`: `fetchAuctionPlan`, `fetchAuctionResults`, `fetchAuctionSeries`,
  `fetchAuctionStats`, `fetchAuctionIssues`, `fetchAuctionQuarters`,
  `syncAuctions`.
- Тест `AuctionDesk.test.jsx` — смоук как `PrimarySmoke.test.jsx` (мок
  `getBoundingClientRect`, иначе графики не рисуются в jsdom).

## Тесты бэка

`tests/test_ofz_auctions.py`: парсер xlsx на фикстуре (положить настоящий
`res2026.xlsx` в `tests/fixtures/`, ~21 КБ) — 66 строк, ДРПА/несостоявшийся
распознаны, `'-****'` → NULL; парсер плана на сохранённом фрагменте HTML III кв.
2026 — 14 дат, две корзины (≤10 лет 900, >10 лет 600); `plan_fact` на
синтетике — корзины, 113-я, need_per_auction; резолв кода в secid по LIKE.

## Проверка

`pytest tests/test_ofz_auctions.py`; `python3 scripts/backfill_ofz_auctions.py --all`
локально (займёт ~10 запросов); `curl localhost:8020/api/auctions/plan` →
III кв. 2026: план 1500 млрд, факт по 113-й меньше номинала; страница
`/app/fixed/auction` на 5175 — все три вида, нет ошибок в консоли; vitest зелёный.
