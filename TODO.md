# TODO — shabshab (desk)

Ветка: `floaters-desk-deploy`. Прод: assetallocator.ru/desk.

---

## 🔴 Стакан + алерты (новая крупная фича)

### Стакан выпуска (order book)
- [x] Кнопка открытия стакана **рядом с названием** вверху карточки облигации.
- [x] Панель стакана открывается **слева** от карточки (вторая выезжающая панель).
- [x] Данные: `GET /api/orderbook/{isin}`. Live-обновление — **поллинг 3с** (react-query refetchInterval). Alor WS — TODO (оптимизация нагрузки).
- [x] **Для КАЖДОГО уровня цены** (bid и ask) считать **SM и DM** — бэкенд готов: `services/bond_details.load_reprice_ctx` (тёплый ctx один раз) + `reprice_at_price(ctx, price)` (чистая, батчится по уровням). `/orderbook` считает SM/DM per-level с полными amorts/offers.
- [x] Колонки уровня: цена · объём (шт; ₽ в title) · SM · DM · YTM.
- [~] Работает для флоатеров (SM/DM/YTM). Для фиксов: кнопка есть, но per-level SM/DM = None (base не RUONIA/KEYRATE) → показывается только YTM. Проверить на живом фиксе; при желании g-спред per-level.

### Алерты по стакану — СДЕЛАНО (2026-07-27, задеплоено)
- [x] **Тип 1 — форма:** объём + цена/YTM/DM(g-спред) + сторона. `OrderbookAlerts.jsx` в панели стакана. op авто-выводится из стороны+метрики.
- [x] **Тип 2 — Ctrl/Cmd-клик** по уровню → префилл формы (сторона+цена уровня). Объём вводит юзер.
- [x] Мониторинг: `alerts_monitor` в main.py, каждые 12с в торговые часы, батч по (isin,kind), матч против Alor-стакана «на уровне/лучше» + накопленный объём шт/₽. Нотификация: `AlertsWatcher` (тост+бип WebAudio, поллинг 8с). WS-push/e-mail — позже.
- [x] Хранение: portfolio.db v3 таблица alerts, per-user. `services/alerts.py` CRUD + evaluate. `api/routes/alerts.py` GET/POST/DELETE.
- [x] Состояния: active/fired/cancelled + история (list_for_user).
- [x] Полировка: редактирование алерта (PATCH), подсветка сработавшего уровня в стакане (красный ⚠), плашка в StatusBar + попап + клик→карточка/стакан, красная строка бумаги. Задеплоено 2026-07-27.
- [x] **Alor WS реал-тайм стакан** — `services/alor_ws.py` персистентный WS, reprice+memo, broadcast через канал orderbook; фронт `connectOrderbookWs`, HTTP-поллинг фолбэком. Формат Alor подтверждён. Задеплоено 2026-07-27.
- [ ] Хвосты: WS-push алертов (мало смысла — монитор 12с); e-mail/Telegram-push (отложено).

**Заметки по реализации:**
- [x] SM/DM per-level: чистая `reprice_at_price(ctx, price)` вынесена из reprice (тёплый ctx → быстро для 10-50 уровней).
- Мониторинг: отдельный воркер в поллере, который для watch-алертов тянет стакан и матчит. Не долбить Alor на каждый алерт — батчить по isin.

---

## 🟡 Флоатеры — техдолг

### Расчёт (точность, низкий приоритет)
- [ ] RUONIA начавшийся период: арифм. среднее вместо daily-comp (~8 bps погрешность).
- [ ] `cut_at_offer` reset-детект мимо не-Cbonds бумаг → старый спред за reset.
- [ ] `yield_over_index` для амортизаторов apples-to-oranges (индекс роллится до дальнего погашения) — вторичка.
- [ ] Settle-модель: календарный T+1 vs бизнес — нужен бэктест (референс был НРД, off → отложено бессрочно).

### Чистка / рефактор
- [x] Мёртвый код — ПРОВЕРЕНО (список устарел): `get_cashflow_items`/`get_moex_coupon_fallback`/`searchBonds`/роут `/valuation` уже удалены; `calculate_floater_metrics` живой (`last_prices.py:301`); `solve_z_bps` живой (тест + `scripts/nrd_pipeline_probe.py`). Удалять нечего.
- [x] `/orderbook` не передавал amorts/offers → расходился с карточкой — ПОЧИНЕНО (reprice_at_price). Осталось `/valuation` роут (фронт не зовёт).
- [ ] Core↔services циклический импорт (держится на lazy-import) — landmine рефактора.
- [ ] 5 cashflow-builder'ов — консолидация (частично слито).

### Безопасность
- [x] login rate-limit по proxy-IP: был XFF, но брал ЛЕВЫЙ (спуфабельный) адрес → обходился ротацией. ПОЧИНЕНО: правый адрес (Caddy-added, edge). backoff 5→300с→…→1ч cap.
- [ ] Полный non-root контейнер (сейчас `cap_drop:ALL`+`no-new-privileges`; `data/` root bind-mount → нужен gosu-entrypoint).

### Данные / инфра
- [x] Рейтинг-драйн: negative-кэш 404 (miss-маркер, _NEG_TTL 1д) + драйн 24/7 + батч /api/fixed + дедуп по реестру — СДЕЛАНО (коммиты perf(ratings)/fix(ratings)).
- [ ] Свежесть КС/RUONIA/cbr_forecast в UI (сейчас частично в `/meta` warnings).
- [ ] Хардкоды руками: `_CB_MEETINGS`, MOEX-праздники, `cbr_forecast.json` (обновлять после заседаний ЦБ).
- [ ] Суборд-исключение из B-бакета — нет флага в MOEX (блокировано данными).

### Аналитика (если шире)
- [ ] Сейчас: DM vs spread-dur + распределения по рейтингу/эмитенту. Можно: динамика спредов, тепловая карта, скрин по carry/Y-IDX.

---

## 🟢 Фиксы — хвосты
- [x] Аналитика фиксов (scatter g-спред/дюрация, box-распределение по рейтингам/эмитентам, гистограмма срочности). Тоггл Список/Аналитика. `FixedAnalytics.jsx`. Задеплоено 2026-07-26.
- [x] Карточка фикса: reprice-калькулятор цены (как у флоатеров). `compute_fixed_row(price_override=)` + `GET /api/fixed/{isin}/reprice` + инпут в FixedCard. Задеплоено 2026-07-26.
- [x] Рейтинг: 2-й источник **smart-lab.ru** подключён (`enrich_smartlab.py`, фолбэк в `ratings.refresh` при промахе corpbonds). MOEX ISS рейтинг не отдаёт. Тест 8/8 мелких ВДО. Драйн перебирает ~784 непокрытых, покрытие растёт (смотреть СТАТУС).

---

## Выкинуто осознанно
Фонды (убрали), НРД-сравнения (удалили слой), z-перцентиль/Δz D-D (были на мёртвом НРД-z).
