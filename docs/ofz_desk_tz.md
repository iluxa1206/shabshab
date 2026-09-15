# ТЗ: вкладка ОФЗ v2 (`/fixed/ofz`)

Дата: 2026-09-15. Страница `frontend-react/src/components/fixed/OfzDesk.jsx`,
бэк `api/routes/fixed.py`, `api/routes/curves.py`, `services/fixed_income.py`.
Правило страницы (см. память `ofz-desk`): **своих расчётов в браузере нет** —
YTM/дюрация/g-спред только из движка фиксов, КБД не интерполируется на фронте.

## Что меняем

1. График сворачивается (чип «ГРАФИК», состояние в localStorage `ofzChart`).
2. Под точками — столбики объёма торгов за день, тултип раскладывает по режимам
   (стакан / адресные (РПС) / прочее, по бордам).
3. Сдвиг кривой и точек: «вчера» (предыдущий торговый день) или произвольная
   дата. Рисуем вторую КБД и «тени» точек с прошлой доходностью, Δ в бп.
4. Таблица под графиком — **та же таблица, что в мониторе ФИКСОВ**
   (`FIXED_COLS`, меню колонок, ширины, сортировка), отфильтрованная `cls === "ofz"`,
   плюс колонка `ΔYTM` к дате сравнения. Своя таблица в OfzDesk удаляется.

## Контракты API (бэк и фронт делаются параллельно — по этим схемам)

### `GET /api/curves/gcurve?date=YYYY-MM-DD` (расширение существующей ручки)

Без `date` — как сейчас. С `date` — КБД на эту дату из ISS
`https://iss.moex.com/iss/engines/stock/zcyc.json?iss.meta=off&iss.only=yearyields&date=YYYY-MM-DD`
(поля `tradedate, tradetime, period, value`). Выходной/праздник → ISS отдаёт
пустой `data`: шагаем назад до 7 дней и отдаём ближайший торговый день.
Ответ: `{points: [[tau, yield_pct], ...], curve_date: "YYYY-MM-DD", requested: "YYYY-MM-DD", stale: false}`
— формат `points`/`curve_date` как у текущей ручки.
Кэш: новая таблица `gcurve_daily(date TEXT, tau REAL, value REAL, PRIMARY KEY(date,tau))`
в `services/portfolio_db.py`; сегодняшняя кривая из `get_gcurve` тоже
пишется туда (одна строка на тенор) — архив копится сам. Читаем из таблицы,
в ISS ходим только при промахе. Валидация даты: ISO, не в будущем, не старше
2014-01-01 (глубина zcyc на ISS).

### `GET /api/fixed/ofz/volumes?date=YYYY-MM-DD` (новая)

Объём торгов ОФЗ за день по бумагам и режимам. Без `date` или `date` = сегодня:
живые данные — `val_today` из fixed universe (TQOB, стакан) + адресные
сделки сегодняшнего дня из `block_trade` (market=`ndm`, суммируем `value` по
`board`) + тики `trade_tick` не нужны (TQOB уже в val_today). Прошлая дата:
`bond_day` по `board` (`services/block_trades.read_*`; если нужного читателя
нет — добавить `read_bond_days(date, isins)` рядом с `bond_days_present`).
Ответ:
```
{date: "YYYY-MM-DD", live: true|false,
 items: {ISIN: {total: руб, book: руб, rps: руб, other: руб,
                boards: {"TQOB": руб, "PSOB": руб, ...}}}}
```
Классификация борда: `TQOB`,`TQOY`,`TQOD`,`TQOE` → `book` (стакан);
борды режима переговорных/адресных сделок ISS market=ndm (`PSOB`,`PTOB`,`PSEU` и
любые из `block_trade.market='ndm'`) → `rps`; остальное → `other`.
Названия бордов для тултипа: словарь `BOARD_LABELS` в `services/block_trades.py`
(есть похожее для размещений — переиспользовать, если найдётся).

### `GET /api/fixed/ofz/asof?date=YYYY-MM-DD` (новая)

Доходность ОФЗ на прошлую дату для «теней» точек и колонки ΔYTM.
Источник по приоритету: 1) `spread_daily` (kind фикс, `ytm`, `price_pct`,
`horizon`) на эту дату; 2) иначе `bond_day.waprice` (или `close`) на дату →
`services.fixed_income.compute_fixed_row(row, full, g_curve=None, calc_date=date, price_override=px)`
— YTM и дюрация без кривой; 3) нет цены — бумага в ответе отсутствует.
«Вчера» = предыдущий торговый день: если на дату нет ни одной строки, шагаем
назад до 7 дней и возвращаем фактическую дату.
Ответ: `{date: "YYYY-MM-DD", requested: "...", items: {ISIN: {ytm, tau, px, src: "snap"|"reprice"}}}`
где `tau` — дюрация Маколея на ту дату (та же метрика, что ось X сегодня).
Кэш в памяти по дате (LRU ~30 дат) — пересчёт 60 бумаг дорогой только первый раз.

## Фронт: график (`OfzDesk.jsx` → вынести в `OfzChart.jsx`)

- Панель объёма: отдельная полоса под scatter, тот же X (tau); столбик на бумагу,
  высота — `total`; цвет — `--mut` с прозрачностью, стакан/РПС стеком двух
  оттенков (book снизу, rps сверху, other третьим). Тултип при hover: имя,
  total, book, rps, other, и построчно борды из `boards` с подписью.
  Данные: `fetchOfzVolumes(date)`; для сегодня refetch 60 с.
- Сдвиг: чипы `Δ: ВЫКЛ | ВЧЕРА | ДАТА` + `<input type=date>` (появляется при
  ДАТА; хранить в localStorage `ofzCmp`). При включении: `fetchGCurve(date)` и
  `fetchOfzAsof(date)`. Вторая кривая — пунктир `--mut-2`; у каждой точки
  тень (полый кружок) в `(tau_prev, ytm_prev)` + тонкий коннектор к текущей;
  тултип точки дополняется «ΔYTM +12 бп с 11.09». Над графиком строка сдвига
  кривой по тенорам 1/2/3/5/7/10/15 лет: `Δ КБД: 1Y +8 · 3Y +5 · 10Y −2 бп`
  (разность двух наборов `points` по совпадающим тенорам — это не интерполяция).
  Фактическую дату сравнения показываем (`curve_date`/`date` из ответов), если
  она отличается от запрошенной — с пометкой «ближайший торговый».
- Сворачивание: чип «ГРАФИК» в шапке, `localStorage.ofzChart = "0"|"1"`.
- Всё через `MeasuredSvg`/хелперы `charts/index.js` — «размер по контейнеру»,
  без хардкода ширин (см. память `charts-overhaul-2026-08`).
- Подсказки: `api.js` → добавить `fetchGCurve(date?)`, `fetchOfzVolumes(date?)`,
  `fetchOfzAsof(date)`.

## Фронт: таблица (`FixedMonitor.jsx` → выделить `FixedTable`)

- Из `FixedMonitor.jsx` выделить компонент таблицы `FixedTable` (файл
  `fixed/FixedTable.jsx`): принимает `rows`, `cols`/меню колонок, ширины,
  сортировку, `onOpen`, `extraCols` (массив колонок в формате `FIXED_COLS`,
  дописываются в конец). Монитор ФИКСОВ использует его без изменения поведения
  (тесты `fixed/*.test.jsx` зелёные, снимки ширин/колонок не меняются).
- Ключи localStorage колонок ОФЗ — свои (`cols_ofz`, `cols_known_ofz`,
  `colw_ofz`), чтобы настройка ОФЗ не ломала монитор.
- Колонка `ΔYTM` (`key: "d_ytm_cmp"`, label «ΔYTM», sub «К ДАТЕ, БП», w 9):
  `(ytm_now − ytm_asof) × 100` бп, цвет по знаку (`pos`/`neg`), прочерк без
  сравнения. Значение приходит в строке как `b.d_ytm_cmp` — OfzDesk мёржит
  `asof.items` в строки перед передачей в таблицу.

## Границы

- Не трогать: движок метрик фиксов, `compute_fixed_row` (кроме вызова с
  `calc_date`, если он уже это умеет), ядро `services/universe_stream`.
- Не добавлять расчёты YTM/дюрации на фронте.
- Тесты: бэк — `tests/test_ofz_desk_api.py` (gcurve date-фолбэк на выходной,
  классификация бордов, asof приоритет snap > reprice); фронт — `OfzChart.test.jsx`
  (рендер теней и Δ-строки на фикстуре), `FixedTable.test.jsx` (extraCols).
- Коммиты не делать, деплой не делать — интеграцию и деплой делает главная сессия.
