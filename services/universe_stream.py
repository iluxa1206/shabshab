"""Живой поток котировок по ВСЕМУ юниверсу флоатеров + событийный пересчёт метрик.

Пул из нескольких персистентных WS-сокетов Alor: юниверс (~600 бумаг) шардируется
по сокетам, на каждый шард — QuotesSubscribe пачкой. В один сокет весь рынок не
лезет (лимиты подписок на соединение у Alor), отсюда пул. Alor ретранслирует
MOEX, так что это тот же источник, что board-снапшот, только push'ем.

Тик котировки:
  * цена → единый кэш цен (session_prices);
  * бумага → dirty-set событийного пересчёта;
  * есть WS-подписчик (избранное) → немедленный broadcast патча строки.

Событийный пересчёт (metrics_worker, такт 5с): полный enrich_bond только по
бумагам, у которых сменилась ЦЕНА СДЕЛКИ, и только по непосчитанным сегодня
уровням — кэш (isin, цена)→строка держит хит-рейт ~90% (облигация ходит по
5–30 уровням за день). Смена только bid/ask полного пересчёта не заказывает —
её разбирает отдельная дешёвая очередь (_sides_dirty): там пересчитываются
ТОЛЬКО цено-зависимые числа сторон, батчем по методике, без пересборки потока.
Версия кэша = (calc_date, поколение кривых): пересборка кривых или новый день
сбрасывают всё.

Почему не reprice каждые 5с по всем: 584 × ~60мс = ~35с CPU на окно — 7 ядер.
Событийно с кэшем уровней то же самое стоит ~1–2% ядра.
"""
import asyncio
import json
import logging
import os
import time
from datetime import date
from typing import Dict, Optional

import aiohttp

from auth import alor_token, REFRESH_TOKEN, BASE_API
from core.orderbooks import WS_DEPTH, WS_COMPRESS

logger = logging.getLogger(__name__)

_WS_URL = BASE_API.replace("https://", "wss://") + "/ws"
_SHARD_SIZE = int(os.getenv("ALOR_POOL_SHARD", "150"))   # ISIN на сокет
_QUOTE_FREQ_MS = 1000       # серверный троттл Alor на бумагу; для таблицы хватает
# Стрим стаканов всего юниверса (лестницы фильтра по объёму пушем, а не
# батч-снимком раз в 120с). Отдельные сокеты от котировок — вдвое больше
# подписок и на порядок больше трафика; выключается флагом.
_DEPTH_STREAM = os.getenv("DEPTH_STREAM", "1") not in ("0", "false", "no")
_DEPTH_FREQ_MS = 1700       # книга шевелится часто — прижимаем частоту пуша
# У ФИКСОВ книга живее (ОФЗ и ликвидные корпораты), а нужна она одному
# потребителю — фильтру по объёму тикета, где важна не миллисекундная свежесть
# лестницы, а её глубина. Отдельная, более редкая частота: замер 28.08.2026 —
# на общей 1700 мс пул фиксов давал ~3400 пушей/мин против 700 у флоатеров.
_FIXED_DEPTH_FREQ_MS = int(os.getenv("FIXED_DEPTH_FREQ_MS", "4000"))
_DEPTH_LEVELS = WS_DEPTH    # как у батч-снимка (services/depth._DEPTH_LEVELS)
# За этим сдвигом средневзвеса от цены сделки наклон перестаёт быть честным
# приближением, и спред по нему считается точно (см. yoi_wap ниже).
WAP_EXACT_PP = float(os.getenv("UNIVERSE_WAP_EXACT_PP", "0.5"))
# ФИКСЫ В ПУЛЕ: витрина фиксов — тот же монитор, ей нужны те же живые котировки,
# стаканы и пересчёт метрик по цене. Выключается флагом: пул фиксов это ещё
# ~740 подписок котировок и столько же стаканов, и откат на проде должен быть
# переменной окружения, а не релизом.
from services.feature_flags import fixed_stream_enabled as _FIXED_STREAM_ON
_FIXED_STREAM = _FIXED_STREAM_ON()
# СТАКАНЫ фиксам качаем: лестница на 20 уровней нужна фильтру по объёму тикета
# (цена набора и спред по ней). Это недёшево — замер 28.08.2026: с фиксами в
# depth-пуле пушей стаканов 3400/мин против 660/мин, впятеро трафика и парсинга,
# — поэтому вынесено в переменную: FIXED_DEPTH_STREAM=0 отключает стаканы фиксов
# целиком, и витрина остаётся с верхом стакана из котировочного сокета.
_FIXED_DEPTH = os.getenv("FIXED_DEPTH_STREAM", "1") not in ("0", "false", "no")
# Пока прогрев не положил универс фиксов, владелец пула заглядывает сюда чаще
# обычного такта: иначе витрина фиксов ждёт потока целых 5 минут после старта.
_FIXED_WAIT_SEC = float(os.getenv("FIXED_POOL_WAIT_SEC", "30"))
# ISIN фиксов, попавших в пул: по нему движок выбирает ветку расчёта (у фикса
# своя математика — YTM/g-спред, а не Y-IDX/DM).
_fixed_isins: set = set()
_RECONCILE_SEC = 300        # пересборка шардов под изменившийся юниверс
_BATCH_SEC = 5.0            # такт событийного пересчёта
# Потолок полных пересчётов за такт (хвост — следующим). 40×~60мс ≈ 2.4с
# потоковой работы на 5-секундное окно: стартовая волна юниверса длится ~1.5
# минуты, зато CPU не насыщается и loop не лагает (80 давало 4.8с/окно — на
# двухъядерном хосте это почти постоянная занятость ядра).
# С приходом ФИКСОВ пул вырос вдвое (609 → 1347 бумаг), и прежний потолок 40
# (480 строк/мин) стал упираться: замер 28.08.2026 — очередь держалась на 165
# бумагах при полностью выбранном потолке, то есть часть строк ждала такта без
# нужды. 60 берём потому, что кэш уровней снял цену пересчёта до 2–3 мс/шт
# (0.8–1.3 с работы на минуту): 60×5 мс ≈ 0.3 с на 5-секундное окно даже с
# запасом на промахи кэша. Вынесено в переменную окружения — крутить на проде
# по замеру «мс/шт» из минутной сводки движка.
_MAX_BATCH = int(os.getenv("UNIVERSE_FULL_BATCH", "60"))
# Потолок дешёвой очереди сторон за такт. 100×25мс ≈ 2.5с на 5-секундное окно —
# половина такта, вторая остаётся полному пересчёту (60×5мс ≈ 0.3с) и запасу.
# Прежние 60 брались из замера 13 мс на бумагу; после того как цены наборов по
# объёму стали реально собираться (глубина книги 50), сторона стоит 20–30 мс, и
# на 60 волна нового размера тикета растягивалась на минуту.
_MAX_SIDES_BATCH = int(os.getenv("UNIVERSE_SIDES_BATCH", "100"))
# ПОТОЛОК АДАПТИВНЫЙ, а не жёсткий: сколько бумаг влезет в отведённый кусок
# такта по ФАКТИЧЕСКОЙ цене стороны. Замер на проде 01.09.2026 — сторона стоит
# 13–35 мс, а очередь при жёстких 100 съедала 4–7 секунд ИЗ МИНУТЫ, то есть
# около 10% пула: тяжёлый поток простаивал, а прочерки в биде и оффере всё
# равно расходились по рынку минутами. Считаем N по бюджету и цене, с полом на
# случай пустого замера и потолком, чтобы один такт не ушёл в счёт целиком.
_SIDES_BUDGET_SEC = float(os.getenv("UNIVERSE_SIDES_BUDGET_SEC", "2.5"))
_SIDES_BATCH_MAX = int(os.getenv("UNIVERSE_SIDES_BATCH_MAX", "400"))
_sides_ms_avg = 0.0             # скользящая цена пересчёта одной стороны, мс


def _sides_batch() -> int:
    """Сколько сторон брать в этот такт: бюджет / фактическая цена бумаги."""
    if _sides_ms_avg <= 0:
        return _MAX_SIDES_BATCH
    n = int(_SIDES_BUDGET_SEC * 1000.0 / _sides_ms_avg)
    return max(_MAX_SIDES_BATCH, min(n, _SIDES_BATCH_MAX))
_PRICE_KEY_DIGITS = 3       # квантование цены в ключе кэша уровней
# Кэш Y-IDX по НАБОРУ цен бумаги: isin → (ключ набора, момент, {цена: бп}).
# Стоимость батча почти вся в сборке потока и кривой, а не в числе цен (66 мс
# за 3 цены против 69 мс за 11, замер на проде 28.08.2026), поэтому экономить
# надо ЦЕЛЫЙ вызов, когда набор цен не изменился.
_YOI_TTL_SEC = float(os.getenv("UNIVERSE_YOI_TTL_SEC", "60"))
_yoi_cache: Dict[str, tuple] = {}
_yoi_cache_epoch = 0        # правка параметров/смена дня — сдвигаем, кэш мимо

# СЕТКА ЦЕН: спред на КАЖДОЙ цене книги, посчитанный заранее и разом.
#
# Цена набора на объём — это VWAP по лестнице, и она меняется от размера
# тикета, который задаёт пользователь. Считать её спред «по запросу» значит
# гнать через движок весь рынок на каждое новое число в фильтре: 26 мс на
# бумагу однопоточно — это полминуты ожидания на 1300 бумагах, сколько батч ни
# поднимай. Но батч почти не дорожает от числа цен (замер на проде 28.08.2026,
# самая тяжёлая бумага: 3 цены — 68 мс, 51 — 101 мс, 201 — 141 мс, то есть база
# ~65 мс и ~0,37 мс за цену), поэтому в тот же вызов кладётся СЕТКА по всему
# диапазону книги. Дальше спред на любой объём — это поиск по сетке с
# интерполяцией между соседями, ~микросекунда, и волна пересчёта исчезает.
#
# Интерполяция здесь — не тот наклон, что убрали 27.08.2026: там линия шла из
# ОДНОГО якоря на всю лестницу и уезжала вместе с ним, здесь она соединяет две
# точно посчитанные соседние точки на интервале в один шаг цены.
_GRID_STEP = float(os.getenv("UNIVERSE_GRID_STEP", "0.01"))   # п.п., шаг цены облигации
_GRID_MAX_NODES = int(os.getenv("UNIVERSE_GRID_NODES", "260"))
# Запас по краям отрезка, п.п. Книга дышит между построениями сетки, и без
# запаса цена набора вылезает за край на первом же движении — а это полная
# пересборка сетки вместо чтения готовой.
_GRID_PAD = float(os.getenv("UNIVERSE_GRID_PAD", "0.3"))
# Потолок денег, на который строится сетка: дальше по лестнице набор не уедет
# ни у кого (реальные тикеты на порядок меньше), а каждый лишний узел — цена
# в батче. Это ещё и потолок «до какого объёма честен спред из сетки».
_GRID_MONEY_CAP = float(os.getenv("UNIVERSE_GRID_MONEY_CAP", "300e6"))
# Жёсткий предел отрезка от лучшей цены, п.п. Одних денег мало: когда книга не
# добирает потолок (а на 300 млн не добирает почти никто), «последний взятый
# уровень» оказывается хвостом лестницы на 90 или 110 — замер на проде
# 28.08.2026 давал медиану 1111 узлов на бумагу. Набор, уехавший от лучшей цены
# дальше этого предела, — не тикет, а вынос книги: такие цены обслуживает
# движок обычным порядком (см. apply_vol_sizes).
_GRID_SPAN_PP = float(os.getenv("UNIVERSE_GRID_SPAN", "1.0"))
# Бумаг за такт в догреве. 25×150мс ≈ 3.8с — влезает в 5-секундный такт, а
# рынок прогревается за пару минут простоя.
_GRID_WARM_BATCH = int(os.getenv("UNIVERSE_GRID_WARM", "25"))
# ПОТОЛОК ПОСТРОЕНИЙ СЕТКИ ЗА ТАКТ. Волна нового размера тикета ставит в очередь
# сторон пол-рынка, и каждая такая бумага строила себе сетку прямо в дешёвой
# ветке: 13 мс на стороны превращались в ~165 мс, а такт со 100 бумагами — в 15
# секунд тяжёлой работы вместо полутора. Очередь копилась быстрее, чем
# разгребалась, и «спред на 1 млн» расползался по рынку минутами.
# Самой волне сетка НЕ НУЖНА: цены наборов активных размеров считаются тем же
# батчем альт-цен (см. _fill_side_metrics), сетка нужна СЛЕДУЮЩЕМУ размеру.
# Поэтому строим её с потолком на такт, а остальным достроит догрев.
_GRID_BUILD_PER_TICK = int(os.getenv("UNIVERSE_GRID_BUILD_TICK", "12"))
# Сколько прочерков в сторонах добираем за такт (см. _blank_side_targets).
# Дешёвая ветка стоит ~13–35 мс на бумагу, и очередь такта всё равно ограничена
# _MAX_SIDES_BATCH — добор кладём в неё только когда живой поток её не занял.
_BLANK_SIDES_BATCH = int(os.getenv("UNIVERSE_BLANK_SIDES", "150"))
# Пауза перед повтором у бумаг, которым сетку строить не из чего (нет пуша
# котировки, книга пустая, расчёт не сошёлся). Без неё они вечно занимали
# первые места в догреве: он берёт первые N из _eval_ctx, каждый такт
# натыкался на тот же безнадёжный хвост и не двигался вовсе.
_GRID_RETRY_SEC = float(os.getenv("UNIVERSE_GRID_RETRY_SEC", "180"))
# Контекстов за такт. Меньше сетевого догрева: каждой бумаге нужно расписание
# (промах day-кэша = ходка в MOEX), и торопиться тут некуда — это хвост рынка.
_CTX_WARM_BATCH = int(os.getenv("UNIVERSE_CTX_WARM", "10"))
# Доля такта под догрев. Живой пересчёт идёт первым и бюджет не делит: догрев
# берёт то, что осталось от этих секунд, и не начинается, если такт уже съеден.
_WARM_BUDGET_SEC = float(os.getenv("UNIVERSE_WARM_BUDGET_SEC", "3.0"))
# Сколько догрев считает ОДНИМ заходом в heavy. Бюджет такта тратится не
# целиком за раз, а слайсами: тяжёлый счёт держит GIL, и пока он идёт, event
# loop не просыпается вовсе. Пачка сеток (25 бумаг × ~150 мс) занимала ядро
# почти на четыре секунды — сторож писал «лаг 2.4с», а запросы витрины в этот
# момент ждали столько же. Тот же объём работы, нарезанный по 0,4 с, даёт петле
# продышаться между кусками: суммарная скорость прогрева та же, отзывчивость
# на порядок лучше.
_WARM_SLICE_SEC = float(os.getenv("UNIVERSE_WARM_SLICE_SEC", "0.4"))
# Тот же слайс для ЖИВОГО пересчёта. Пачка полного пересчёта (60 бумаг × ~140
# мс) занимала ядро на восемь секунд одним заходом — именно она, а не догрев,
# давала лаги в 3 с после рестарта, когда очередь набита всем рынком.
_HEAVY_SLICE_SEC = float(os.getenv("UNIVERSE_HEAVY_SLICE_SEC", "0.4"))
# Сколько такта отдаём живому пересчёту. Остаток идёт сторонам и догреву;
# недосчитанное возвращается в очередь и продолжится следующим тактом.
_FULL_BUDGET_SEC = float(os.getenv("UNIVERSE_FULL_BUDGET_SEC", "2.5"))
# С какого времени бумага считается ТЯЖЁЛОЙ и попадает в лог поимённо. Слайсы
# режут пачку, но одна бумага внутри неделима: пока она считается, ядро занято.
# Средняя стоит ~140 мс, а отдельные выпуски — секунды, и без имени в логе их
# не найти (сторож лага показывает функцию, а не ISIN).
_SLOW_BOND_MS = float(os.getenv("UNIVERSE_SLOW_BOND_MS", "700"))
# Потолок заходов за такт. Слайсы режут время, а этот счётчик — страховка от
# холостого кружения: бумага, которой сетка не строится (нет узлов, нет книги),
# остаётся в целях и после захода, и без потолка догрев перебирал бы один и тот
# же список весь бюджет такта.
_WARM_MAX_SLICES = int(os.getenv("UNIVERSE_WARM_SLICES", "8"))
_yoi_grid: Dict[str, tuple] = {}   # isin → (epoch, [узлы], {узел: бп})
_grid_builds = 0                   # построений сетки с прошлой сводки
_grid_budget = 0                   # остаток построений сетки в текущем такте
_grid_cold: Dict[str, float] = {}  # isin → monotonic неудачной попытки сетки

# ── состояние ────────────────────────────────────────────────────────────────
# Вход точного расчёта Y-IDX по ЛЮБОЙ цене (bid/ask/средневзвес): поток, кривая
# и база от цены не зависят, поэтому храним их на БУМАГУ и переиспользуем весь
# день. Собирается на промахе кэша уровней — из тех же данных, которыми считался
# сам уровень, без единого сетевого вызова.
_eval_ctx: Dict[str, dict] = {}
_last_quote: Dict[str, dict] = {}    # isin → последний пуш {last_price, bid, ask, ...}
# Бумаги, у которых сдвинулись ТОЛЬКО стороны стакана. Полный пересчёт им не
# нужен (уровень цены сделки тот же), но спред сторон обязан пересчитаться по
# методике: раньше его «правил наклон» уже в браузере, и точное число приходило
# только со следующей сделкой — у неликвида это часы.
#
# ОЧЕРЕДЬ С ПРИОРИТЕТОМ (isin → ключ сортировки, меньше = раньше), а не набор:
# у неё два потребителя с разной срочностью. Движение сторон (приоритет 0) —
# это свежие данные, их ждут прямо сейчас; волна «включили фильтр по объёму»
# ставит в очередь пол-рынка разом (см. register_vol_sizes) и не должна
# отодвигать живой поток на минуту вперёд. Порядок внутри волны — от бумаг с
# самым крупным собирающимся набором: их под фильтром объёма и смотрят.
_SIDES_PRIO_LIVE = 0.0        # движение сторон стакана
_SIDES_PRIO_WAVE = 1.0        # волна нового размера тикета (+ ранг ликвидности)
_sides_dirty: Dict[str, float] = {}


def _queue_sides(isin: str, prio: float = _SIDES_PRIO_LIVE) -> None:
    """В очередь сторон. Живое событие ПОВЫШАЕТ приоритет бумаги, уже стоящей в
    волне: она нужна раньше, а не вторым заходом."""
    cur = _sides_dirty.get(isin)
    if cur is None or prio < cur:
        _sides_dirty[isin] = prio

# ОБЪЁМ ТИКЕТА: размеры, которые сейчас смотрят в браузере. Y-IDX по VWAP-цене
# набора считается ЗДЕСЬ, по методике, а не линеаризацией в браузере (он
# репрайсить не умеет). Размеры регистрирует API-запрос таблицы; живут TTL, а
# их число ограничено — каждый размер это ещё одна цена в батче (~1,5 мс).
_VOL_TTL_SEC = float(os.getenv("UNIVERSE_VOL_TTL_SEC", "600"))
# Сколько РАЗНЫХ размеров тикета движок считает разом. Это не «сколько влезет
# в одну вкладку» (их две — бид и оффер), а сколько их сейчас смотрят все
# вместе: две открытые вкладки с разными тикетами — уже четыре. Каждый размер
# стоит ~0,4 мс в батче альт-цен, поэтому потолок держим выше числа сторон.
_VOL_MAX_SIZES = int(os.getenv("UNIVERSE_VOL_MAX", "6"))
# потолок размера тикета: 100 млрд ₽ — заведомо больше любого реального объёма
# рынка флоатеров, но конечен (вход приходит от клиента)
_VOL_SIZE_MAX_RUB = 1e11
_vol_sizes: Dict[float, float] = {}     # размер, ₽ → monotonic последнего спроса
_vol_wave_pending = False               # размер увидели впервые — раздать спреды


def register_vol_sizes(sizes) -> None:
    """Клиент смотрит таблицу с фильтром по объёму — запоминаем размеры тикета,
    чтобы движок считал по ним Y-IDX в своём такте.

    Приходит от клиента (WS-сообщение / параметр ручки), поэтому вход режем:
    столько же размеров, сколько движок готов считать, и в пределах разумной
    суммы. Иначе одна кривая вкладка растит словарь без края."""
    now = time.monotonic()
    ok = []
    for v in sizes or []:
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        # потолок считаем ДО среза по количеству: иначе горсть мусора в начале
        # списка съедала бы квоту, и живой размер до реестра не доезжал
        if 0 < v <= _VOL_SIZE_MAX_RUB:
            ok.append(round(v, 2))
    fresh = False
    for v in ok[:_VOL_MAX_SIZES]:
        fresh = fresh or v not in _vol_sizes
        _vol_sizes[v] = now
    for k in [k for k, t in _vol_sizes.items() if now - t > _VOL_TTL_SEC]:
        _vol_sizes.pop(k, None)
    if fresh:
        # РАЗМЕР ВИДЯТ ВПЕРВЫЕ — ставим бумаги в очередь сами, иначе цену набора
        # получили бы только те, кто о себе напомнит (сделкой или движением
        # сторон). У застывшего неликвида такого повода может не быть весь день,
        # и в таблице у него навсегда остался бы прочерк.
        # НЕ ВЕСЬ РЫНОК, а только те, у кого набор ДЕЙСТВИТЕЛЬНО собирается:
        # у остальных цена набора — None, ради которого незачем гонять
        # переоценку в 20–30 мс. Замер на проде 28.08.2026 по 120 бумагам:
        # тикет 1 млн набирают 68%, 5 млн — 47%, 20 млн — 24%, то есть очередь
        # короче вдвое-вчетверо, а проверка стоит ~0,05 мс на бумагу.
        # ФИКСЫ сюда не кладём: у них своей дешёвой ветки нет (recrunch_sides их
        # молча пропускает), а очередь с потолком на такт они бы просто заняли.
        global _vol_wave_pending
        _vol_wave_pending = True


def apply_vol_sizes(isins: Optional[list] = None) -> Dict[str, dict]:
    """Спреды на новый размер тикета — ИЗ СЕТКИ, без движка.

    Цена набора считается по книге (~0,05 мс на бумагу), спред снимается с
    заранее посчитанной сетки цен (см. _yoi_grid) — весь рынок обходится за
    десятки миллисекунд вместо получасовой очереди по 26 мс на бумагу.
    Бумаги, у которых сетки ещё нет (движок по ним сегодня не проходил),
    отправляются в обычную очередь сторон — там сетка и построится."""
    from services.market_data import market_cache
    um = market_cache.get("universe_metrics") or {}
    out: Dict[str, dict] = {}
    # ВЕСЬ УНИВЕРС ВИТРИНЫ, а не только те, кто пушил котировку: цена набора
    # считается по КНИГЕ, и у застывшей бумаги без единого пуша за день она есть
    # ровно так же. Проверка стоит ~0,05 мс на бумагу.
    for isin in (isins if isins is not None else list(um or _last_quote)):
        if isin in _fixed_isins:
            continue
        row = um.get(isin)
        if not row:
            continue
        vol_px = _vol_prices(isin)
        live = {k: p for k, p in vol_px.items() if p is not None}
        if live and isin not in _yoi_grid:
            # набор собирается, а спред взять неоткуда — это работа для движка
            _queue_sides(isin, _SIDES_PRIO_WAVE)
            continue
        yoi = {k: yoi_at(isin, p) for k, p in live.items()}
        if any(v is None for v in yoi.values()):
            # цена вне сетки (книга разъехалась с прошлого прохода) — туда же
            _queue_sides(isin, _SIDES_PRIO_WAVE)
            continue
        # СТРОКУ ОБНОВЛЯЕМ И ПРИ ПУСТОМ НАБОРЕ: иначе в таблице осталось бы
        # число от ПРОШЛОГО размера тикета — хуже прочерка, потому что выглядит
        # как ответ на текущий фильтр.
        r = dict(row)
        r["vol_px"] = vol_px or None
        r["yoi_vol"] = yoi or None
        out[isin] = r
    return out


async def flush_vol_sizes() -> int:
    """Разбор волны нового размера тикета: считаем в тяжёлом пуле, раскладываем
    по строкам и пушем подписчикам. Возвращает число обновлённых бумаг."""
    global _vol_wave_pending
    if not _vol_wave_pending:
        return 0
    _vol_wave_pending = False
    from services.heavy import run_heavy
    from services.market_data import market_cache
    rows = await run_heavy(apply_vol_sizes)
    if not rows:
        return 0
    _store_rows(market_cache, rows)
    try:
        from api.routes import ws as wsmod
        await _push_metrics(wsmod, rows)
    except Exception as e:      # пуш не должен ронять раздачу чисел в таблицу
        logger.debug("flush_vol_sizes push: %s", e)
    return len(rows)


def active_vol_sizes() -> list:
    """Размеры тикета, которые смотрят ПРЯМО СЕЙЧАС: свежий спрос вперёд, его и
    режет потолок.

    Раньше потолок резал по ВЕЛИЧИНЕ (самые крупные вперёд), и размер, который
    пользователь смотрит сейчас, выпадал из расчёта, если в TTL (10 мин) висели
    четыре более крупных — свои же прошлые пробы в поле или чужие вкладки.
    Движок такой размер не считал вовсе, в колонках бида/оффера стоял прочерк, и
    ждать приходилось истечения TTL чужой регистрации, а не расчёта. Спрос —
    единственный правильный ключ: цену набора ждёт тот, кто её только что
    заказал."""
    now = time.monotonic()
    live = [(t, k) for k, t in _vol_sizes.items() if now - t <= _VOL_TTL_SEC]
    live.sort(key=lambda x: (-x[0], -x[1]))     # свежие вперёд, при равенстве — крупные
    return [k for _, k in live[:_VOL_MAX_SIZES]]
_dirty: set = set()                  # бумаги со сменившейся ценой сделки
_streamed: set = set()               # ISIN на живых сокетах пула (для live_isins)
_depth_streamed: set = set()         # ISIN на живых depth-сокетах
_depth_msgs = 0                      # пуши стаканов с последней сводки (диагностика)
_level_memo: Dict[tuple, dict] = {}  # (isin, px_key) → строка метрик
_memo_version: Optional[tuple] = None
_memo_hits = 0
_memo_misses = 0


def live_isins() -> set:
    """ISIN, по которым котировка идёт push'ем. Их пропускают quotes_poller
    (сеяние кэша цен) и WS-broadcaster (пуш поллером): пул делает и то и другое
    сам. Пул упал — набор пуст, поллеры подхватывают."""
    return set(_streamed)


def depth_stream_covers(isins) -> bool:
    """Стрим стаканов покрывает ЭТОТ набор бумаг — HTTP-батч depth_poller лишний.
    Порог 0.8: пара отвалившихся шардов не должна выключать фолбэк целиком.

    Считаем пересечение с набором, а не общий счётчик: в пуле теперь и фиксы, и
    их сокеты закрывали бы дыру в покрытии флоатеров одним лишь размером."""
    want = set(isins)
    if not _DEPTH_STREAM or not want:
        return False
    return len(want & _depth_streamed) >= 0.8 * len(want)


# Состояние ВЛАДЕЛЬЦА пула — отдельно от состояния сокетов. «0 бумаг на
# сокетах» снаружи одинаково выглядит и когда пул ещё не строил шарды (холодный
# старт, упавший на ISS reconcile), и когда все сокеты отвалились; лечится это
# по-разному, поэтому причину носим с собой, а не ищем в логах постфактум.
_pool = {"started": 0.0, "built": 0.0, "shards": 0, "err": None, "err_at": 0.0}


def pool_state() -> dict:
    """Почему на сокетах пусто: когда пул стартовал, когда в последний раз
    собрал шарды и на чём падал. Читает сторож стримов (api/main)."""
    return dict(_pool)


# Состояние КАЖДОГО сокета пула. Общий счётчик streamed падение одного шарда не
# показывает: 150 бумаг из 600 просто перестают шевелиться, сторож видит живых
# соседей и молчит, а поллер-фолбэк тихо тянет их снапшотом раз в 5 секунд.
_SHARD0 = {"isins": 0, "up": False, "msgs": 0, "conns": 0, "errors": 0, "last": 0.0}
_shards: Dict[int, dict] = {}         # котировки
_depth_shards: Dict[int, dict] = {}   # стаканы
# Сеанс дольше этого считаем состоявшимся — только он сбрасывает бэкофф сокета.
_UP_OK_SEC = float(os.getenv("ALOR_WS_UP_OK_SEC", "60"))


def _shard_view(src: Dict[int, dict]) -> dict:
    now = time.time()
    rows = [{"id": sid, **s,
             "quiet_min": round((now - s["last"]) / 60, 1) if s["last"] else None}
            for sid, s in sorted(src.items())]
    return {"total": len(rows), "up": sum(1 for s in rows if s["up"]),
            "mute": [s["id"] for s in rows if s["up"] and not s["msgs"]],
            "list": rows}


def stats() -> dict:
    return {"streamed": len(_streamed), "dirty": len(_dirty), "pool": pool_state(),
            "shards": _shard_view(_shards), "depth_shards": _shard_view(_depth_shards),
            "memo": len(_level_memo), "hits": _memo_hits, "misses": _memo_misses,
            # ПОЧЕМУ ЖДЁМ СПРЕД НА ОБЪЁМ: покрытие контекстами и сетками, длина
            # дешёвой очереди и размеры тикета, которые движок сейчас считает.
            # Без этих чисел «иногда долго грузится» не отличить от «размер не
            # попал в активные» и от «сетки снесла пересборка кривых».
            "ctx": len(_eval_ctx), "grids": len(_yoi_grid),
            "grids_cold": len(_grid_cold), "sides_queue": len(_sides_dirty),
            "vol_sizes": active_vol_sizes()}


def _seed_price(isin: str, px) -> None:
    if px is None:
        return
    from services.market_data import market_cache
    market_cache["last_prices"][isin] = px
    market_cache["last_prices_ts"][isin] = time.time()


# ── доставка патча строки на фронт ───────────────────────────────────────────

# ключи enrich_bond → имена полей строки таблицы (те же, что в /api/bonds)
_METRIC_FIELDS = {
    "yoi": "yield_over_index_bps", "dm": "dm_bps", "disc_dm": "disc_margin_bps",
    "z_model": "z_model_bps", "ytm": "yield_xirr_pct", "base_ytm": "index_yield_pct",
    "dirty": "dirty_price_rub", "delta": "delta_to_prev_close",
    # Y-IDX верха стакана — те же числа, что в /api/bonds. Уходят в патч ВМЕСТЕ
    # с ценами сторон (ниже): пара «цена → спред» обязана приезжать из одного
    # расчёта, иначе в строке окажется свежая цена со спредом от прошлой.
    "yoi_bid": "y_idx_bid_bps", "yoi_ask": "y_idx_ask_bps",
    # спред по средневзвесу дня (аналитика считает по нему, не по last price)
    "yoi_wap": "y_idx_wap_bps",
    # фильтр по объёму: цены VWAP-наборов активных размеров тикета и их Y-IDX.
    # Едут СЛОВАРЯМИ {"ask:5000000": …}, потому что размеры выбирает клиент, а
    # знать их в схеме строки незачем — фронт берёт свой ключ.
    "vol_px": "vol_px", "yoi_vol": "y_idx_vol",
    # горизонт прайсинга: он цено-зависим (правило цены vs цена выкупа), поэтому
    # едет в патче вместе с метриками — иначе маркер оферты в таблице остался бы
    # от прошлой цены и врал, к чему посчитан спред строки
    "horizon": "preferred_horizon",
    # спред-дюрация едет ВМЕСТЕ с горизонтом: она считается по потоку ИМЕННО
    # этого горизонта, и без неё смена горизонта в патче оставила бы в строке
    # дюрацию от прошлого расчёта (точка на графике аналитики против своего
    # спреда — тот же рассинхрон, что у пары «цена → спред»)
    "spread_dur": "spread_dur_yrs",
}


# Поля строки ФИКСА, зависящие от цены: имена уже совпадают с полями витрины
# (/api/fixed), переименовывать нечего — в отличие от флоатеров, где ключи
# enrich_bond и строки таблицы исторически разные.
_FIXED_PATCH_FIELDS = (
    "ytm", "ytm_bid", "ytm_ask", "ytm_wap", "cur_yield",
    "g_spread_bps", "g_spread_bid_bps", "g_spread_ask_bps", "g_spread_wap_bps",
    "z_spread_bps", "dirty", "mod_dur", "mac_dur", "convexity", "dv01",
    "delta_to_prev_close", "val_today", "wap_pct", "price_stale",
    # цены наборов тикета и метрики по ним — словарями по размеру («ask:5000000»),
    # потому что размеры выбирает клиент (см. register_vol_sizes)
    "vol_px", "g_spread_vol", "ytm_vol",
    # номинал и НКД: по ним фронт считает деньги уровня стакана
    "face_value_rub", "accrued_rub",
)


def _fixed_patch(row: dict) -> dict:
    """Патч строки фикса для WS-пуша. Число, которого больше нет, уезжает явным
    null — по той же причине, что и у флоатеров (см. _metrics_patch)."""
    out = {k: row[k] for k in _FIXED_PATCH_FIELDS if k in row}
    if out:
        out["metrics"] = True
        for k in ("bid", "ask"):
            if k in row:
                out[k] = row[k]
    return out


def _metrics_patch(row: dict) -> dict:
    """Патч производных метрик для WS-пуша: фронт мерджит и НЕ зовёт /reprice —
    пересчёт уже сделан здесь, вторая ходка за тем же числом не нужна.

    ЧИСЛО, КОТОРОГО БОЛЬШЕ НЕТ, УЕЗЖАЕТ ЯВНЫМ null. Раньше None-поля из патча
    выбрасывались, и фронт держал прежнее значение: у бумаги, чей оффер ушёл из
    книги или чей контекст расчёта остыл, в строке оставался спред от прошлой
    цены — ровно тот рассинхрон «свежая цена, старое число», от которого
    уходили 27.08.2026. Ключи, которых в строке НЕТ вовсе (пересчёт их не
    касался), в патч не попадают и на фронте не трогаются."""
    out = {_METRIC_FIELDS[k]: row[k] for k in _METRIC_FIELDS if k in row}
    if out:
        out["metrics"] = True     # маркер «производные посчитаны» для фронта
        # цены сторон — из ЭТОГО же расчёта, иначе спред стороны лёг бы на цену
        # из другого тика (рассинхрон «цена 99,00 / Y-IDX от 99,28»)
        for k in ("bid", "ask"):
            if k in row:
                out[k] = row[k]
    return out


async def _broadcast_quote(isin: str, data: dict) -> None:
    """Патч строки из пуша котировки — точечным подписчикам и wildcard-вкладкам
    (режим «вся таблица живая»)."""
    from api.routes import ws as wsmod
    from services import live_quotes
    if not wsmod.manager.has_market_audience(isin):
        return
    # Котировка — ПОЛНЫЙ снимок верха стакана, поэтому None здесь значит
    # «стороны в книге нет», и это надо показать, а не умолчать: раньше такие
    # поля вырезались, и в строке оставалась цена ушедшей заявки.
    out = {
        "last_price_pct": data.get("last_price"),
        "bid": data.get("bid"), "ask": data.get("ask"),
        "bid_qty": data.get("bid_vol"), "ask_qty": data.get("ask_vol"),
        "src": "ws",
    }
    # оборот и средневзвес — наоборот, добавка: их нет, пока по бумаге не
    # прошло сделок, и слать по ним null значило бы стирать живое число
    v = live_quotes.get(isin)
    if v:
        for k, val in (("vwap_pct", v["vwap_pct"]), ("vwap_volume", v["volume"]),
                       ("val_today", v["val_today"])):
            if val is not None:
                out[k] = val
    await wsmod.manager.broadcast_market_data(isin, out)


async def _on_quote(isin: str, data: dict) -> None:
    # Нет стороны стакана — брокер отдаёт 0, а не null. Нормализуем НА ВХОДЕ,
    # чтобы ноль не разошёлся по кэшу котировок, патчам WS и спреду по цене
    # (см. market_data._px_or_none).
    from services.market_data import _px_or_none
    for k in ("bid", "ask"):
        if k in data:
            data[k] = _px_or_none(data[k])
    px = data.get("last_price")
    _seed_price(isin, px)
    prev = _last_quote.get(isin)
    _last_quote[isin] = data
    # Полный пересчёт заказывает смена цены СДЕЛКИ: она меняет уровень, а с ним
    # весь набор метрик строки.
    if px is not None and (prev is None or prev.get("last_price") != px):
        _dirty.add(isin)
    # Смена сторон — отдельная, ДЕШЁВАЯ очередь: пересчитывается только Y-IDX
    # сторон (поток и база не пересобираются, ~13 мс на бумагу). Наклон отсюда
    # убран 27.08.2026 — линия через якорь уводила число вслед за якорем.
    elif prev is not None and any(prev.get(k) != data.get(k) for k in ("bid", "ask")):
        # У ФИКСА отдельной дешёвой ветки нет: compute_fixed_row считает цену
        # сделки и обе стороны одним проходом по расписанию, дробить нечего.
        if isin in _fixed_isins:
            _dirty.add(isin)
        else:
            _queue_sides(isin)
    await _broadcast_quote(isin, data)


# ── пул сокетов ──────────────────────────────────────────────────────────────

async def _shard_socket(shard_id: int, isins: list, stop: asyncio.Event) -> None:
    """Один сокет пула: подписка на свой шард, чтение до сигнала stop."""
    st = _shards.setdefault(shard_id, dict(_SHARD0, isins=len(isins)))
    st["isins"] = len(isins)
    backoff = 1
    up_at = 0.0
    while not stop.is_set():
        try:
            # ТОКЕН ВНУТРИ try: alor_token() кидает, когда oauth не ответил за
            # свои 5 секунд, и снаружи это уносило ВЕСЬ таск шарда. Владелец
            # пула пересоздаёт таски только при смене юниверса, так что 150
            # бумаг оставались без котировок до нового выпуска или погашения.
            token = await alor_token()
            if not token:
                await asyncio.sleep(10)
                continue
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(_WS_URL, heartbeat=20, timeout=15) as ws:
                    up_at = time.monotonic()
                    st["up"] = True
                    st["conns"] += 1
                    guid_isin = {}
                    for n, isin in enumerate(isins):
                        guid = f"up{shard_id}-{isin}-{n}"
                        guid_isin[guid] = isin
                        await ws.send_json({
                            "opcode": "QuotesSubscribe", "code": isin,
                            "exchange": "MOEX", "format": "Simple",
                            "frequency": _QUOTE_FREQ_MS, "guid": guid, "token": token})
                        if n % 50 == 49:
                            await asyncio.sleep(0.2)   # не бить пачкой подписок
                    _streamed.update(isins)
                    while not stop.is_set():
                        try:
                            msg = await ws.receive(timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                        aiohttp.WSMsgType.CLOSING):
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except Exception:
                            continue
                        data, guid = payload.get("data"), payload.get("guid")
                        isin = guid_isin.get(guid)
                        if data and isin:
                            st["msgs"] += 1
                            st["last"] = time.time()
                            await _on_quote(isin, data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            st["errors"] += 1
            logger.warning("universe pool shard %d: %s", shard_id, e)
        finally:
            st["up"] = False
            _streamed.difference_update(isins)
        # Бэкофф сбрасывает только СОСТОЯВШИЙСЯ сеанс: коннект, который брокер
        # рвёт сразу (лимит подписок, чужой токен), иначе давал реконнект раз в
        # секунду с каждого сокета пула.
        if up_at and time.monotonic() - up_at >= _UP_OK_SEC:
            backoff = 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def _depth_socket(shard_id: int, isins: list, stop: asyncio.Event,
                        freq_ms: int = _DEPTH_FREQ_MS) -> None:
    """Сокет стрима стаканов: OrderBookGetAndSubscribe на шард, пуш → кэш
    глубины (market_cache['depth']) — тот же формат, что batch-снимок, лестницы
    фильтра по объёму просто становятся push-свежими."""
    global _depth_msgs
    from services.market_data import market_cache
    st = _depth_shards.setdefault(shard_id, dict(_SHARD0, isins=len(isins)))
    st["isins"] = len(isins)
    backoff = 1
    up_at = 0.0
    while not stop.is_set():
        try:
            token = await alor_token()     # внутри try: см. _shard_socket
            if not token:
                await asyncio.sleep(10)
                continue
            async with aiohttp.ClientSession() as sess:
                # СЖАТИЕ ОБЯЗАТЕЛЬНО: без permessage-deflate Alor отбивает
                # подписку с depth>20 (см. core.orderbooks.WS_DEPTH).
                async with sess.ws_connect(_WS_URL, heartbeat=20, timeout=15,
                                           compress=WS_COMPRESS) as ws:
                    up_at = time.monotonic()
                    st["up"] = True
                    st["conns"] += 1
                    guid_isin = {}
                    for n, isin in enumerate(isins):
                        guid = f"ud{shard_id}-{isin}-{n}"
                        guid_isin[guid] = isin
                        await ws.send_json({
                            "opcode": "OrderBookGetAndSubscribe", "code": isin,
                            "exchange": "MOEX", "depth": _DEPTH_LEVELS, "format": "Simple",
                            "frequency": freq_ms, "guid": guid, "token": token})
                        if n % 50 == 49:
                            await asyncio.sleep(0.2)
                    _depth_streamed.update(isins)
                    # СОСТАВ ШАРДА И ЕГО МЕТКА СВЕЖЕСТИ: глобальный depth_ts
                    # обновляет ЛЮБОЙ живой шард, поэтому смерть одного сокета
                    # (150 бумаг) пряталась за соседями — get_depth() отдавал
                    # получасовые лестницы как свежие. См. services/depth.
                    market_cache.setdefault("depth_shard_isins", {})[shard_id] = list(isins)
                    market_cache.setdefault("depth_shard_ts", {})[shard_id] = time.time()
                    while not stop.is_set():
                        try:
                            msg = await ws.receive(timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                        aiohttp.WSMsgType.CLOSING):
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except Exception:
                            continue
                        data, guid = payload.get("data"), payload.get("guid")
                        isin = guid_isin.get(guid)
                        if not data or not isin:
                            continue
                        depth = market_cache.get("depth")
                        if depth is None:
                            depth = market_cache["depth"] = {}
                        depth[isin] = {
                            "b": [[float(e["price"]), float(e["volume"])]
                                  for e in (data.get("bids") or []) if e.get("price") is not None],
                            "a": [[float(e["price"]), float(e["volume"])]
                                  for e in (data.get("asks") or []) if e.get("price") is not None]}
                        _now_ts = time.time()
                        market_cache["depth_ts"] = _now_ts
                        market_cache.setdefault("depth_shard_ts", {})[shard_id] = _now_ts
                        _depth_msgs += 1
                        st["msgs"] += 1
                        st["last"] = _now_ts
        except asyncio.CancelledError:
            raise
        except Exception as e:
            st["errors"] += 1
            logger.warning("depth stream shard %d: %s", shard_id, e)
        finally:
            st["up"] = False
            _depth_streamed.difference_update(isins)
            # метку снимаем: пока сокет мёртв, его бумаги обязаны считаться
            # протухшими, а не жить на метке соседних шардов
            market_cache.get("depth_shard_ts", {}).pop(shard_id, None)
        if up_at and time.monotonic() - up_at >= _UP_OK_SEC:
            backoff = 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


# Ручки сокетов ПО ГРУППАМ (см. universe_stream_pool). Номера шардов группы
# лежат в своём диапазоне: floaters 0…, fixed 100… — сторож и статистика
# различают их по номеру.
_GROUP_STRIDE = 100
_groups: Dict[str, dict] = {
    "floaters": {"key": None, "shards": 0, "tasks": [], "stops": []},
    "fixed": {"key": None, "shards": 0, "tasks": [], "stops": []},
}


def _stop_sockets(tasks: list, stops: list) -> None:
    """Гасит текущий комплект сокетов пула: сначала сигнал stop (сокет выходит
    из чтения сам), потом отмена таска."""
    for s in stops:
        s.set()
    for t in tasks:
        t.cancel()
    tasks.clear()
    stops.clear()


def _rebuild_group(name: str, isins: list, base: int, group: dict,
                   depth: bool = True, depth_freq_ms: int = _DEPTH_FREQ_MS) -> int:
    """Пересобирает сокеты ОДНОЙ группы пула (флоатеры / фиксы) и возвращает
    число её шардов.

    Группы независимы СПЕЦИАЛЬНО: состав фиксов пересобирается по ликвидности
    каждый час, и общий пул на всех гасил бы вместе с ними живые подписки
    флоатеров каждый раз. Номера шардов у групп не пересекаются (base) — по ним
    сторож различает мёртвые сокеты в статистике.
    """
    _stop_sockets(group["tasks"], group["stops"])
    shards = [isins[i:i + _SHARD_SIZE] for i in range(0, len(isins), _SHARD_SIZE)]
    for n, shard in enumerate(shards):
        sid = base + n
        stop = asyncio.Event()
        group["stops"].append(stop)
        group["tasks"].append(asyncio.create_task(_shard_socket(sid, shard, stop)))
        if _DEPTH_STREAM and depth:
            dstop = asyncio.Event()
            group["stops"].append(dstop)
            group["tasks"].append(asyncio.create_task(
                _depth_socket(sid, shard, dstop, depth_freq_ms)))
    # группа ужалась — лишние номера уходят из статистики, иначе в ней вечно
    # висел бы «мёртвый» сокет без сообщений
    for sid in [s for s in _shards if base <= s < base + _GROUP_STRIDE
                and s >= base + len(shards)]:
        _shards.pop(sid, None)
        _depth_shards.pop(sid, None)
    logger.info("universe pool [%s]: %d бумаг / %d сокетов котировок%s",
                name, len(isins), len(shards),
                f" + {len(shards)} стаканов" if (_DEPTH_STREAM and depth) else "")
    return len(shards)


async def universe_stream_pool() -> None:
    """Владелец пула: режет юниверс на шарды, держит по сокету на шард,
    пересобирает пул при изменении юниверса (новые выпуски/погашения).

    Групп две — флоатеры и фиксы: у них разные источники состава и разный темп
    его смены, и пересборка одной не трогает сокеты другой."""
    from services import instruments_registry
    _pool["started"] = time.time()
    await asyncio.sleep(25)     # старт после прогрева и первого снапшота
    # Ручки сокетов держим НА МОДУЛЕ: владельца пула перезапускает супервизор
    # (api.main._supervise), и локальный список унёс бы с собой ручки ЖИВЫХ
    # сокетов — старые подписки остались бы висеть, новые легли бы сверху, и
    # брокер получил бы двойной комплект подписок на те же бумаги.
    for g in _groups.values():
        _stop_sockets(g["tasks"], g["stops"])
        g["key"] = None
        g["shards"] = 0
    retry = 0                   # пауза до следующего такта: см. хвост цикла
    fast_fixed = False          # прошлый такт ждал прогрева фиксов
    isins: list = []            # последний известный состав флоатеров
    while True:
        waiting_fixed = False   # универс фиксов ещё греется — заглянем раньше
        try:
            # БЫСТРЫЙ ПРОХОД (ждём прогрева фиксов) идёт ТОЛЬКО за фиксами.
            # Состав флоатер-юниверса в первые минуты после старта ещё дрожит
            # (реестр догружается: 609 → 615 → 609), и на учащённом такте это
            # пересобирало бы их сокеты по три раза за две минуты.
            if not fast_fixed:
                uni = await instruments_registry.fetch_floater_universe()
                isins = sorted({u["isin"] for u in uni if u.get("isin")})
                if not isins:
                    # Пустой юниверс — это сбой источника, а не «бумаг не
                    # осталось». Пересборка по нему убила бы все живые сокеты
                    # (см. схлопывание пула сделок, trades_stream).
                    raise RuntimeError("пустой юниверс — пул не пересобираем")
                group = _groups["floaters"]
                key = tuple(isins)
                if key != group["key"]:
                    group["shards"] = _rebuild_group("флоатеры", isins, 0, group)
                    group["key"] = key
            # ФИКСЫ — своя группа и СВОЙ guard: битый листинг фиксов не имеет
            # права уронить подписки флоатеров (ISS отдаёт пустой борд чаще, чем
            # кажется — см. сторож стримов 28.08.2026). Не дали фиксов — держим
            # прежний набор, а не выкидываем их из пула.
            if _FIXED_STREAM:
                from services.market_data import market_cache
                fx = sorted({u["isin"] for u in (market_cache.get("fixed_universe") or [])
                             if u.get("isin")} - set(isins))
                if fx:
                    _fixed_isins.clear()
                    _fixed_isins.update(fx)
                elif _fixed_isins:
                    logger.warning("universe pool: универс фиксов пуст — держим прежние")
                fgroup = _groups["fixed"]
                fkey = tuple(sorted(_fixed_isins))
                if fkey and fkey != fgroup["key"]:
                    fgroup["shards"] = _rebuild_group("фиксы", list(fkey),
                                                      _GROUP_STRIDE, fgroup,
                                                      depth=_FIXED_DEPTH,
                                                      depth_freq_ms=_FIXED_DEPTH_FREQ_MS)
                    fgroup["key"] = fkey
                # ХОЛОДНЫЙ СТАРТ: прогрев фиксов (~700 bondization) обычно не
                # успевает к первой сборке пула, и по обычному такту витрина
                # фиксов оставалась без потока пять минут (прод 28.08: старт
                # 12:52, фиксы 12:57). Ждать целый такт незачем — заглядываем
                # снова через полминуты, пока прогрев не положил универс.
                waiting_fixed = not fkey
            _pool.update(built=time.time(),
                         shards=sum(g["shards"] for g in _groups.values()))
            _pool["err"] = None
            retry = _FIXED_WAIT_SEC if waiting_fixed else 0
            fast_fixed = waiting_fixed
        except asyncio.CancelledError:
            for g in _groups.values():
                _stop_sockets(g["tasks"], g["stops"])
            raise
        except Exception as e:
            # Ошибка reconcile (ISS не отдал юниверс) на холодном старте
            # означает НОЛЬ сокетов: ждать полный такт — это 5 минут слепоты.
            # Возвращаемся быстро, с бэкоффом до обычного такта.
            _pool.update(err=str(e), err_at=time.time())
            logger.warning("universe pool reconcile: %s", e)
            retry = min(retry * 2 or 20, _RECONCILE_SEC)
            fast_fixed = False      # после ошибки идём полным проходом
        await asyncio.sleep(retry or _RECONCILE_SEC)


# ── событийный пересчёт с кэшем уровней ──────────────────────────────────────

def invalidate_params(isin: Optional[str] = None) -> None:
    """Правка Справочника (спека фиксинга, маржа, даты) → строка пересчитывается
    на ближайшем такте. Кэш уровней (isin, цена)→строка держит СТАРЫЕ параметры,
    а сам пересчёт заказывается только сменой цены: без этого пинка правка
    доезжала до таблицы со следующей сделкой, а в неликвиде не доезжала вовсе.
    isin=None — массовая правка (импорт xlsx): чистим весь кэш."""
    global _yoi_cache_epoch
    if isin:
        for k in [k for k in _level_memo if k[0] == isin]:
            _level_memo.pop(k, None)
        _eval_ctx.pop(isin, None)
        _yoi_cache.pop(isin, None)
        _yoi_grid.pop(isin, None)
        _grid_cold.pop(isin, None)
        if isin in _last_quote:
            _dirty.add(isin)
    else:
        _level_memo.clear()
        _eval_ctx.clear()
        _grid_cold.clear()
        _yoi_cache.clear()
        _yoi_grid.clear()
        _yoi_cache_epoch += 1
        _dirty.update(_last_quote.keys())


def _px_key(px: float) -> float:
    return round(float(px), _PRICE_KEY_DIGITS)


async def _push_metrics(wsmod, rows: Dict[str, dict]) -> None:
    """Подписчикам — производные пушем: /reprice с фронта не нужен."""
    for isin, row in rows.items():
        if wsmod.manager.has_market_audience(isin):
            patch = _fixed_patch(row) if row.get("_kind") == "fixed" else _metrics_patch(row)
            if patch:
                await wsmod.manager.broadcast_market_data(isin, patch)


def _grid_reach(ladder, face: float, accrued: float) -> Optional[float]:
    """Цена уровня, на котором сторона набирает потолок денег (_GRID_MONEY_CAP).
    Дальше этой цены VWAP никакого разумного тикета не уедет."""
    if not ladder:
        return None
    left, last = _GRID_MONEY_CAP, None
    for px, qty in ladder:
        if px is None or qty is None:
            continue
        last = float(px)
        left -= float(qty) * (face * float(px) / 100.0 + accrued)
        if left <= 0:
            break
    return last


def _grid_nodes(isin: str, sides: dict, wap) -> list:
    """Узлы сетки — по диапазону, ДОСТИЖИМОМУ НАБОРОМ, а не по всей книге.

    VWAP тикета лежит между лучшей ценой стороны и ценой последнего взятого
    уровня, поэтому сетка накрывает ровно этот отрезок. По всей лестнице её
    строить нельзя: на 50 уровнях книга тянется на 12 п.п. медианой (замер на
    проде 28.08.2026) — это хвосты заявок на 90 и 110, куда набор не доходит
    никогда, а сетка на них либо разрежается до бесполезного шага, либо стоит
    тысячи узлов.

    Шаг — шаг цены облигации (0,01 п.п.). Если отрезок шире потолка узлов, шаг
    растягивается: интерполяция между соседями честна и на растянутом шаге, а
    дыра в покрытии стоит целого пересчёта на движке."""
    from services import depth as depth_svc
    from services.market_data import market_cache
    lad = depth_svc.get_depth().get(isin) or {}
    row = (market_cache.get("universe_metrics") or {}).get(isin) or {}
    face = row.get("face_px") or 1000.0
    accrued = row.get("accrued_settle") or 0.0
    pxs = [v for v in list(sides.values()) + [wap] if v]
    for key in ("b", "a"):
        best = (lad.get(key) or [[None]])[0][0]
        if not best:
            continue
        best = float(best)
        pxs.append(best)
        reach = _grid_reach(lad.get(key), face, accrued)
        if reach:
            # деньги ИЛИ предел по цене — что ближе
            pxs.append(min(reach, best + _GRID_SPAN_PP) if key == "a"
                       else max(reach, best - _GRID_SPAN_PP))
    pxs = [float(p) for p in pxs if p and p > 0]
    if len(pxs) < 2:
        return []
    lo, hi = min(pxs) - _GRID_PAD, max(pxs) + _GRID_PAD
    step = max(_GRID_STEP, (hi - lo) / _GRID_MAX_NODES)
    n = int((hi - lo) / step) + 1
    return [round(lo + i * step, 4) for i in range(n)]


def _grid_nodes_if_needed(isin: str, sides: dict, wap, vol_px: dict) -> list:
    """Узлы сетки — ТОЛЬКО когда её надо строить заново.

    Сетка дорога (≈150 мс против 26 мс за три живые цены) и живёт до пересборки
    кривых, а стороны у ликвидной бумаги дёргаются десятки раз за это время.
    Строить её на каждом движении — платить полную цену за уже готовый ответ:
    в проде это подняло пересчёт стороны с 26 до 180 мс/шт. Поэтому строим,
    когда фильтр по объёму кто-то смотрит И сетки нет, она протухла или цена
    набора вышла за её края."""
    if not active_vol_sizes():
        return []
    g = _yoi_grid.get(isin)
    fresh = bool(g) and g[0] == _yoi_cache_epoch
    if fresh:
        live = [p for p in vol_px.values() if p is not None]
        if not live or all(yoi_at(isin, p) is not None for p in live):
            return []
        # набор уехал за края сетки — строим заново (в пределах бюджета такта)
    if _grid_budget <= 0:
        # ПОТОЛОК ТАКТА ВЫБРАН: числа текущих размеров тикета бумага всё равно
        # получит (их цены идут альт-ценами того же батча), а сетку ей построит
        # догрев. Иначе волна размера превращала дешёвую ветку в дорогую на
        # всю очередь разом — см. _GRID_BUILD_PER_TICK.
        return []
    nodes = _grid_nodes(isin, sides, wap)
    if nodes:
        globals()["_grid_budget"] = _grid_budget - 1
    return nodes


def _has_book(isin: str, book: Optional[dict] = None) -> bool:
    """В слое глубины есть хоть одна сторона книги — этого хватает и сетке, и
    цене набора (котировочный пуш им не нужен).

    book передаётся СНАРУЖИ, когда проверок много подряд: get_depth() отсеивает
    протухшие шарды и на этом пересобирает словарь всего рынка — звать его в
    цикле по универсу значит платить за эту пересборку сотни раз за такт."""
    lad = (book if book is not None else _depth_snapshot()).get(isin) or {}
    return bool(lad.get("a") or lad.get("b"))


def _depth_snapshot() -> dict:
    from services import depth as depth_svc
    return depth_svc.get_depth()


def _grid_warm_targets(limit: int) -> list:
    """Бумаги, которым сетки не хватает: контекст расчёта уже собран, а сетки
    нет или она от прошлой кривой. ФИКСЫ мимо — у них своя ветка расчёта."""
    out = []
    now = time.monotonic()
    book = _depth_snapshot()      # один снимок на весь обход (см. _has_book)
    for isin in _eval_ctx:
        if isin in _fixed_isins:
            continue
        g = _yoi_grid.get(isin)
        if g and g[0] == _yoi_cache_epoch:
            continue
        if not _last_quote.get(isin) and not _has_book(isin, book):
            continue          # ни котировки, ни книги — сетку строить не из чего
        if now - _grid_cold.get(isin, 0.0) < _GRID_RETRY_SEC:
            continue          # недавно не вышло — не занимать квоту такта
        out.append(isin)
        if len(out) >= limit:
            break
    return out


def warm_grids(isins: list, board: Optional[dict] = None,
               deadline: Optional[float] = None) -> int:
    """ДОГРЕВ СЕТОК заранее, в простое движка.

    Кривые пинятся на день (services/market_data), поэтому сетка, построенная
    один раз, живёт до вечера — и включение фильтра по объёму перестаёт быть
    событием для движка вообще. Без догрева первый фильтр за день (и первый
    после каждого рестарта) снова платит полную цену: ~150 мс на бумагу,
    полминуты ожидания на весь рынок.

    Греем в такты, когда обе очереди пусты: живой пересчёт важнее — он про то,
    что происходит на рынке ПРЯМО СЕЙЧАС, а сетка нужна к моменту, когда на
    таблицу посмотрят.

    deadline (монотонное время) — граница ОДНОГО захода: на ней цикл выходит,
    недоделанные бумаги вернутся следующим слайсом (их снова выберет
    _grid_warm_targets). Без границы пачка держала ядро столько, сколько
    занимал весь список, и витрина ждала вместе с ней."""
    global _grid_builds
    from services.yidx_exact import y_idx_many
    from services import live_quotes as _lq
    board = board or {}
    book = _depth_snapshot()      # один снимок на пачку (см. _has_book)
    n = 0
    for isin in isins:
        # проверка ДО работы, а не после: прервать надо перед следующей
        # бумагой, иначе граница ничего не ограничивает
        if deadline is not None and n and time.monotonic() >= deadline:
            break
        ev = _eval_ctx.get(isin)
        if ev is None:
            continue
        q = _last_quote.get(isin)
        # КОТИРОВКА НЕ ОБЯЗАТЕЛЬНА: сетка строится по КНИГЕ (слой глубины), а
        # пуш котировки у застывшей бумаги может не прийти за день ни разу —
        # раньше такая бумага не получала сетку вовсе и на каждый размер тикета
        # уезжала в очередь движка.
        if not q and not _has_book(isin, book):
            continue
        sides = _sides_of(q or {})
        wap = (_lq.get(isin) or {}).get("vwap_pct") or (board.get(isin) or {}).get("waprice")
        nodes = _grid_nodes(isin, sides, wap if (wap or 0) > 0 else None)
        if not nodes:
            _grid_cold[isin] = time.monotonic()
            continue
        got = y_idx_many(ev, nodes)
        if not got:
            _grid_cold[isin] = time.monotonic()
            continue
        _grid_cold.pop(isin, None)
        _yoi_grid[isin] = (_yoi_cache_epoch, nodes,
                           {x: got.get(round(x, 4)) for x in nodes})
        _grid_builds += 1
        n += 1
    return n


def yoi_at(isin: str, price) -> Optional[int]:
    """Спред на ЛЮБОЙ цене из сетки бумаги: соседние узлы + линейная вставка.
    None — сетки нет, цена вне её диапазона или соседи без числа."""
    if price is None:
        return None
    g = _yoi_grid.get(isin)
    if not g or g[0] != _yoi_cache_epoch:
        return None
    nodes, vals = g[1], g[2]
    px = float(price)
    if not nodes or px < nodes[0] or px > nodes[-1]:
        return None
    import bisect
    i = bisect.bisect_left(nodes, px)
    if i < len(nodes) and nodes[i] == px:
        v = vals.get(nodes[i])
        return None if v is None else int(round(v))
    if i == 0 or i >= len(nodes):
        return None
    lo, hi = nodes[i - 1], nodes[i]
    a, b = vals.get(lo), vals.get(hi)
    if a is None or b is None or hi <= lo:
        return None
    return int(round(a + (b - a) * (px - lo) / (hi - lo)))


def _fill_side_metrics(row: dict, isin: str, sides: dict, snap: dict) -> None:
    """Y-IDX сторон стакана и средневзвеса дня — ПО МЕТОДИКЕ, одним батчем.

    Поток, кривая и база от цены не зависят и уже лежат в _eval_ctx, поэтому
    три цены стоят ~13 мс на бумагу (замер на проде 27.08.2026) против 85 мс за
    цену при поштучном reprice. Наклон отсюда убран: он честен только рядом с
    якорем, а уехавший якорь уводил за собой все производные числа разом."""
    from services import live_quotes as _lq
    lvq = _lq.get(isin) or {}
    wap = lvq.get("vwap_pct") or snap.get("waprice")
    wap = wap if (wap or 0) > 0 else None
    row["yoi_wap"] = None
    for side in sides:
        row[f"yoi_{side}"] = None
    # цены VWAP-наборов по активным размерам тикета — такие же альт-цены
    vol_px = _vol_prices(isin)
    row["vol_px"] = vol_px or None
    row["yoi_vol"] = None

    ev = _eval_ctx.get(isin)
    if ev is None:
        return
    from services.yidx_exact import y_idx_many
    want = [v for v in list(sides.values()) + [wap] if v is not None]
    want += [p for p in vol_px.values() if p is not None]
    if not want:
        return
    # СЕТКА едет тем же батчем: доплата ~0,4 мс за цену против 65 мс за сам
    # вызов, зато спред на любой другой объём потом берётся из неё без движка
    # вообще (см. _yoi_grid). Но ТОЛЬКО когда фильтр по объёму кто-то смотрит:
    # без активных размеров тикета сетку некому спрашивать, а платить за неё
    # пришлось бы на каждом движении сторон по всему рынку.
    nodes = _grid_nodes_if_needed(isin, sides, wap, vol_px)
    # ТОТ ЖЕ НАБОР ЦЕН — ответ уже посчитан. Пересчёт заказывает движение
    # сторон, но в очередь бумага попадает и по другим поводам (волна нового
    # размера тикета, вернувшаяся на прежний уровень заявка), а цена набора по
    # объёму от дрожания верха книги обычно не меняется вовсе. TTL держит
    # число живым по кривой: она обновляется внутри дня, а версия дня при этом
    # та же, и вечный кэш залипал бы на утренней кривой.
    key = (tuple(sorted(want)), tuple(nodes[:1] + nodes[-1:]), len(nodes),
           _yoi_cache_epoch)
    hit = _yoi_cache.get(isin)
    if hit and hit[0] == key and time.time() - hit[1] <= _YOI_TTL_SEC:
        got = hit[2]
    else:
        got = y_idx_many(ev, want + [n for n in nodes if n not in want])
        _yoi_cache[isin] = (key, time.time(), got)
        if nodes:
            global _grid_builds
            _grid_builds += 1
            _yoi_grid[isin] = (_yoi_cache_epoch, nodes,
                               {n: got.get(round(n, 4)) for n in nodes})
    for side, v in sides.items():
        if v is not None:
            row[f"yoi_{side}"] = got.get(round(float(v), 4))
    if wap is not None:
        row["yoi_wap"] = got.get(round(float(wap), 4))
    if vol_px:
        # из батча, если цена в нём была; иначе — из готовой сетки (её в этом
        # проходе не пересчитывали, и это норма: она живёт до смены кривой)
        row["yoi_vol"] = {k: (got.get(round(float(p), 4))
                              if round(float(p), 4) in got else yoi_at(isin, p))
                          for k, p in vol_px.items() if p is not None}


def _vol_prices(isin: str, face: float = None, accrued: float = None) -> dict:
    """{"bid:5000000": цена, "ask:5000000": цена} — VWAP-цены наборов активных
    размеров по обеим сторонам. Набор считает тот же vwap_for, что скринер и
    портфель: одна арифметика книги на всё приложение.

    face/accrued — номинал и НКД бумаги (деньги уровня = qty × (номинал × цена%
    + НКД)). Не переданы — берём из строки метрик флоатера; у фикса своя схема
    строки, поэтому он передаёт их явно."""
    sizes = active_vol_sizes()
    if not sizes:
        return {}
    from services import depth as depth_svc
    from services.screener_core import vwap_for, vwap_passes
    from services.market_data import market_cache
    if face is None or accrued is None:
        row = (market_cache.get("universe_metrics") or {}).get(isin) or {}
        face = face if face is not None else row.get("face_px")
        accrued = accrued if accrued is not None else row.get("accrued_settle")
    face = face or 1000.0
    accrued = accrued or 0.0
    ladders = depth_svc.get_depth().get(isin) or {}
    out = {}
    for size in sizes:
        for side, key in (("bid", "b"), ("ask", "a")):
            v = vwap_for(ladders.get(key), size, face, accrued)
            # набор не собрался (книги не хватило даже с допуском) — числа нет
            out[f"{side}:{size:.0f}"] = round(v["px"], 4) if vwap_passes(v, size) else None
    return out


def _sides_of(q: dict) -> dict:
    """Цены сторон из котировки. 0 = стороны в стакане нет: ни цены, ни спреда
    по ней (МТС 2Р-03 — 8960 б.п. при отсутствующем оффере)."""
    out = {}
    for side in ("bid", "ask"):
        v = q.get(side)
        out[side] = v if (v or 0) > 0 else None
    return out


def recrunch_sides(isins: list, board: dict) -> Dict[str, dict]:
    """Дешёвый пересчёт ТОЛЬКО сторон стакана для бумаг из очереди _sides_dirty.

    Уровень цены сделки не менялся — строка метрик остаётся прежней, меняются
    её цено-зависимые числа по bid/ask. Без этого точный спред стороны приезжал
    бы только со следующей СДЕЛКОЙ (у неликвида — часы), а между сделками жила
    линеаризация в браузере."""
    from services.market_data import market_cache
    um = market_cache.get("universe_metrics") or {}
    out: Dict[str, dict] = {}
    for isin in isins:
        row = um.get(isin)
        if not row or isin not in _eval_ctx:
            continue
        snap = board.get(isin, {}) or {}
        q = _last_quote.get(isin)
        # КОТИРОВОЧНЫЙ ПУШ НЕ ОБЯЗАТЕЛЕН: стороны берём из биржевого снапшота,
        # когда пуша по бумаге сегодня не было. Раньше такая бумага молча
        # пропускалась — цена стороны в строке стояла (снапшот её даёт), а спред
        # к ней оставался прочерком до конца дня.
        if q is None:
            q = {"bid": snap.get("bid"), "ask": snap.get("ask")}
        row = dict(row)
        sides = _sides_of(q)
        for side, v in sides.items():
            row[side] = v
        _fill_side_metrics(row, isin, sides, snap)
        out[isin] = row
    return out


def _blank_side_targets(board: dict, limit: int) -> list:
    """Бумаги, у которых ЦЕНА стороны есть, а СПРЕДА к ней нет.

    Спред стороны считает движок, и заказывает этот расчёт только событие:
    сделка (полный пересчёт) или движение верха стакана (дешёвая очередь). По
    бумаге, с которой за сессию не случилось ни того ни другого, строка так и
    остаётся с прочерком — а цена стороны в ней есть, потому что приходит
    биржевым снапшотом. До 27.08.2026 дыру закрывал браузер: он дорисовывал
    спред наклоном от якоря. Наклон убран (число было неверным), и прочерк стал
    виден — сюда и приходили жалобы «спреды в биде и оффере не грузятся».

    Контекст расчёта обязателен: без него считать нечем, такие бумаги ждут
    своей очереди в догреве контекстов."""
    out = []
    um = _market_cache_um()
    for isin in _eval_ctx:
        if isin in _fixed_isins or isin in _sides_dirty:
            continue
        row = um.get(isin)
        if not row:
            continue
        snap = board.get(isin, {}) or {}
        q = _last_quote.get(isin) or {}
        for side in ("bid", "ask"):
            px = q.get(side) if q.get(side) is not None else snap.get(side)
            if (px or 0) > 0 and row.get("yoi_" + side) is None:
                out.append(isin)
                break
        if len(out) >= limit:
            break
    return out


def _blank_sides_count() -> int:
    """Сколько сторон ЖДЁТ движок: цена есть, спреда нет. Число для минутной
    сводки — по нему видно, расходится очередь или стоит на месте."""
    um = _market_cache_um()
    n = 0
    for isin, row in um.items():
        if isin in _fixed_isins:
            continue
        for side in ("bid", "ask"):
            if (row.get(side) or 0) > 0 and row.get("yoi_" + side) is None:
                n += 1
    return n


def _market_cache_um() -> dict:
    from services.market_data import market_cache
    return market_cache.get("universe_metrics") or {}


def _crunch_fixed(u: dict, ctx: dict, q: dict) -> Optional[dict]:
    """Строка ФИКСА по живой цене: YTM/g-спред/z-спред и те же числа по сторонам
    стакана и средневзвесу.

    Кэш уровней здесь не нужен: у фикса нет дешёвой ветки сторон — один вызов
    compute_fixed_row считает цену сделки, bid, ask и средневзвес по уже
    собранному потоку (несколько прогонов солвера, единицы мс)."""
    from services.fixed_income import compute_fixed_row
    from services import live_quotes
    isin = u["isin"]
    full = ctx["full_by"].get(isin) or {}
    if not full.get("coupons"):
        return None
    snap = ctx["board"].get(isin, {}) or {}
    row = dict(u)
    px = q.get("last_price")
    if px is not None:
        row["last"] = px
    sides = _sides_of(q)
    row["bid"], row["ask"] = sides["bid"], sides["ask"]
    # НКД и вчерашнее закрытие — из борд-снапшота: в справке универса они от
    # часового кэша, а НКД капает каждый день
    # оборот дня кладём В СТРОКУ до расчёта: по нему решается, доверять ли
    # своему тиковому средневзвесу (fixed_income._wap_of)
    for src, dst in (("prev", "prev"), ("accrued", "accrued"),
                     ("prev_date", "prev_date"), ("waprice", "wap"),
                     ("vol", "val_today")):
        if snap.get(src) is not None:
            row[dst] = snap[src]
    out = compute_fixed_row(row, full, ctx["g_curve"], ctx["calc_date"])
    # ФИЛЬТР ПО ОБЪЁМУ: цена набора тикета по лестнице стакана и метрики ПО НЕЙ.
    # Считаем здесь, а не в браузере: там нет ни потока, ни кривой, а спред по
    # цене набора обязан считаться той же методикой, что и по цене сделки.
    vol_px = _vol_prices(isin, face=out.get("face_value_rub"),
                         accrued=out.get("accrued_rub"))
    out["vol_px"] = vol_px or None
    out["g_spread_vol"] = out["ytm_vol"] = None
    if vol_px:
        from services.fixed_income import fixed_side_metrics
        prices = [p for p in vol_px.values() if p is not None]
        got = fixed_side_metrics(row, full, ctx["g_curve"], ctx["calc_date"], prices)
        out["g_spread_vol"] = {k: (got.get(round(float(p), 4)) or {}).get("g_spread_bps")
                               for k, p in vol_px.items() if p is not None}
        out["ytm_vol"] = {k: (got.get(round(float(p), 4)) or {}).get("ytm")
                          for k, p in vol_px.items() if p is not None}
    lv = live_quotes.get(isin) or {}
    vol = max(snap.get("vol") or 0, lv.get("val_today") or 0) or None
    if vol is not None:
        out["val_today"] = vol
    out["_kind"] = "fixed"
    return out


def _store_eval_ctx(isin: str, u: dict, ref, ctx: dict, snap: dict) -> None:
    """Вход точного расчёта Y-IDX по любой цене — на бумагу, на весь день.

    Цена сюда не входит вовсе (поток, кривая и база от неё не зависят), поэтому
    контекст собирается и бумаге, которая сегодня не торговалась: см. warm_ctx."""
    try:
        from services.bond_details import _acc_date, _periods_from_coupons
        full = (ctx.get("full_by") or {}).get(isin) or {}
        _eval_ctx[isin] = {
            "isin": isin, "ref_obj": ref,
            # база ставки нужна не расчёту, а ПЕРЕПРИВЯЗКЕ кривой (см.
            # _rebind_curves): по ней видно, какую из двух кривых класть заново
            "base": u.get("base_rate_type"),
            "curve": (ctx["ruonia_curve"] if u.get("base_rate_type") == "RUONIA"
                      else ctx["keyrate_curve"]),
            "ruonia_curve": ctx["ruonia_curve"],
            "calc_date": ctx["calc_date"],
            "accrued_live": snap.get("accrued"),
            "accrued_date": _acc_date(snap.get("accrued_date")),
            "periods": _periods_from_coupons(full.get("coupons")),
            "amorts": full.get("amorts"),
            "offers": full.get("offers"),
            # без биржевого НКД точного числа не бывает (27.08.2026)
            "accrued_missing": snap.get("accrued") is None,
        }
    except Exception as e:
        logger.debug("eval ctx %s: %s", isin, e)
        _eval_ctx.pop(isin, None)


def _ctx_warm_targets(uni_by: dict, limit: int) -> list:
    """Бумаги юниверса, у которых контекста расчёта нет вовсе."""
    out = []
    for isin in uni_by:
        if isin in _eval_ctx or isin in _fixed_isins:
            continue
        out.append(isin)
        if len(out) >= limit:
            break
    return out


def warm_ctx(isins: list, ctx: dict, deadline: Optional[float] = None) -> int:
    """Контекст расчёта бумагам, которые СЕГОДНЯ НЕ ТОРГОВАЛИСЬ.

    Полный пересчёт заказывает цена сделки, и без неё бумага не попадала в
    движок ни разу: ни контекста, ни сетки, ни спреда на объём — прочерк на
    весь день. Между тем спред набора считается по ЦЕНЕ ИЗ КНИГИ, а не по цене
    сделки: заявки в стакане стоят и у бумаги, по которой сегодня не прошло ни
    одного принта. Контекст цены не требует, поэтому строим его и им — сетку
    следом достроит догрев по лестнице.

    deadline — граница одного захода (см. warm_grids): сборка контекста это
    расписание купонов и калибровка фиксинга, десятки миллисекунд на бумагу, и
    пачка целиком держала ядро."""
    n = 0
    for isin in isins:
        if deadline is not None and n and time.monotonic() >= deadline:
            break
        u = (ctx.get("uni_by") or {}).get(isin)
        if u is None or isin in _eval_ctx:
            continue
        try:
            from services.universe import build_universe_ref
            ref = build_universe_ref(u, isin, ctx["cache"], ctx["secs"])
        except Exception as e:
            logger.debug("warm ctx %s: %s", isin, e)
            continue
        _store_eval_ctx(isin, u, ref, ctx, (ctx["board"].get(isin, {}) or {}))
        if isin in _eval_ctx:
            n += 1
    return n


def _crunch(batch: list, ctx: dict, enrich=None, deadline: Optional[float] = None,
            pending: Optional[list] = None) -> Dict[str, dict]:
    """Синхронный счёт батча (в to_thread). batch = [(isin, quote)].

    Кэш уровней: цена уже считалась сегодня на этой версии кривых → строка из
    памяти, enrich не зовём. Патчим только то, что живёт вне уровня: bid/ask и
    их Y-IDX (батчем по методике, см. _fill_side_metrics), оборот.

    deadline — граница ОДНОГО захода в heavy: счёт держит GIL, и пока идёт
    пачка, event loop не просыпается вовсе (60 бумаг × 140 мс = восемь секунд
    молчания сайта). Недосчитанные ISIN складываются в pending — вызывающий
    вернёт их в очередь, а не потеряет. Первая бумага считается всегда, иначе
    очередь не двигалась бы вовсе."""
    global _memo_hits, _memo_misses
    if enrich is None:
        from services.universe import enrich_bond, build_universe_ref
        enrich = enrich_bond
        build_ref = build_universe_ref
    else:                       # тестовая инъекция
        build_ref = lambda u, isin, cache, secs: None
    out: Dict[str, dict] = {}
    for idx, (isin, q) in enumerate(batch):
        if deadline is not None and out and time.monotonic() >= deadline:
            if pending is not None:
                pending.extend(i for i, _ in batch[idx:])
            break
        px = q.get("last_price")
        u = ctx["uni_by"].get(isin)
        if u is None:
            # ФИКС: своя математика (YTM/g-спред), своя витрина, свой кэш метрик
            fx = (ctx.get("fixed_by") or {}).get(isin)
            if fx is None:
                continue
            try:
                row = _crunch_fixed(fx, ctx, q)
            except Exception as e:
                logger.debug("fixed crunch %s: %s", isin, e)
                continue
            if row:
                out[isin] = row
            continue
        if px is None:
            continue
        key = (isin, _px_key(px))
        row = _level_memo.get(key)
        if row is None:
            _memo_misses += 1
            snap = ctx["board"].get(isin, {})
            _bond_t0 = time.perf_counter()
            try:
                ref = build_ref(u, isin, ctx["cache"], ctx["secs"])
                row = enrich(
                    u, ref, ctx["full_by"].get(isin) or {},
                    last=px, prev=snap.get("prev"), accrued=snap.get("accrued"),
                    prev_date=snap.get("prev_date"),
                    accrued_date=snap.get("accrued_date"),
                    bid=q.get("bid"), ask=q.get("ask"),
                    ruonia_curve=ctx["ruonia_curve"], keyrate_curve=ctx["keyrate_curve"],
                    exp_ks=ctx["exp_ks"], exp_ru=ctx["exp_ru"], g_curve=ctx["g_curve"],
                    calc_date=ctx["calc_date"])
            except Exception as e:
                logger.debug("universe crunch %s: %s", isin, e)
                continue
            _bond_ms = (time.perf_counter() - _bond_t0) * 1000.0
            if _bond_ms >= _SLOW_BOND_MS:
                # поимённо: слайс не режет бумагу пополам, поэтому такая строка
                # это ровно тот кусок, на который ядро замирает
                logger.warning("тяжёлая бумага %s: %.0f мс на полный пересчёт "
                               "(%s, купонов %d)", isin, _bond_ms,
                               u.get("base") or "?",
                               len((ctx["full_by"].get(isin) or {}).get("coupons") or []))
            _level_memo[key] = row
            _store_eval_ctx(isin, u, ref, ctx, snap)
        else:
            _memo_hits += 1
        row = dict(row)          # кэш неизменяем — наружу копия
        # ВНЕ УРОВНЯ: цены сторон стакана. Считаем их ПО МЕТОДИКЕ, а не наклоном
        # от цены сделки. Наклон — линия через якорь: пока якорь верен, ошибка
        # мала, но уехавший якорь уводит за собой все производные числа разом
        # (прод 27.08.2026 — вся лестница стакана в телеграме). Батч из двух-трёх
        # цен стоит ~13 мс на бумагу (замер там же), поток и база не пересобираются.
        sides = _sides_of(q)
        for side, v in sides.items():
            row[side] = v
        _fill_side_metrics(row, isin, sides, (ctx["board"].get(isin, {}) or {}))
        snap = ctx["board"].get(isin, {})
        # свой тиковый счёт впереди биржевого (см. services/universe): VALTODAY и
        # WAPRICE из ISS-снапшота отстают, тик уже здесь
        from services import live_quotes
        lv = live_quotes.get(isin) or {}
        vol = max(snap.get("vol") or 0, lv.get("val_today") or 0) or None
        if vol is not None:
            row["val_today"] = vol
        row["wap"] = lv.get("vwap_pct") or snap.get("waprice") or row.get("wap")
        out[isin] = row
    return out


# Отпечаток кривых живёт в market_data (им пользуются и стрим, и витрина
# анонсов первички): одна копия на проект, см. curves_fingerprint.
from services.market_data import curves_fingerprint as _curves_fp


async def _day_ctx() -> Optional[dict]:
    """Общий контекст батча: кривые, борд-снапшот, справочники, расписания —
    всё из day-кэшей, сеть только на промахах."""
    from services.market_data import MarketDataService, market_cache
    from services.paths import cache_path
    curves, zctx, board = await asyncio.gather(
        MarketDataService.get_curves(),
        MarketDataService.get_zspread_ctx(),
        MarketDataService.fetch_board_snapshot())
    ruonia_curve, keyrate_curve, cd, rd = curves
    if ruonia_curve is None or keyrate_curve is None:
        return None
    exp_ks, exp_ru, g_curve = zctx
    from services import instruments_registry
    uni = await instruments_registry.fetch_floater_universe()
    fixed_by: Dict[str, dict] = {}
    if _FIXED_STREAM:
        # справка фиксов (номинал/НКД/купон/прошлое закрытие) — из того же
        # часового кэша, что кормит витрину; сети здесь нет
        fixed_by = {u["isin"]: u for u in (market_cache.get("fixed_universe") or [])
                    if u.get("isin")}
    return {
        "uni_by": {u["isin"]: u for u in uni if u.get("isin")},
        "fixed_by": fixed_by,
        "cache": MarketDataService.get_local_bond_cache(cache_path("isins_cache.json")),
        "secs": {},              # промахи локального кэша обходятся без MOEX-добора
        "board": board,
        "ruonia_curve": ruonia_curve, "keyrate_curve": keyrate_curve,
        "exp_ks": exp_ks, "exp_ru": exp_ru, "g_curve": g_curve,
        "calc_date": cd or rd or date.today(),
        "version": (str(cd or date.today()), _curves_fp(market_cache)),
        "full_by": {},
    }


def _rebind_curves(ctx: Optional[dict] = None) -> int:
    """Новые кривые — в готовые контексты расчёта, на месте.

    ctx нет (движок ещё не собрал день) — контексты сносим: считать по ссылке
    на кривую, которой уже нет в кэше, хуже, чем построить контекст заново."""
    if not ctx:
        n = len(_eval_ctx)
        _eval_ctx.clear()
        return n
    ru, kr = ctx.get("ruonia_curve"), ctx.get("keyrate_curve")
    if ru is None or kr is None:
        n = len(_eval_ctx)
        _eval_ctx.clear()
        return n
    for ev in _eval_ctx.values():
        base = ev.get("base")
        if base is None:        # контекст от прошлой версии кода
            base = "RUONIA" if ev.get("curve") is ev.get("ruonia_curve") else "KEYRATE"
            ev["base"] = base
        ev["curve"] = ru if base == "RUONIA" else kr
        ev["ruonia_curve"] = ru
        ev["calc_date"] = ctx.get("calc_date", ev.get("calc_date"))
    return len(_eval_ctx)


def _check_version(version: tuple, ctx: Optional[dict] = None) -> None:
    """Новый день или пересобранные кривые → кэш уровней недействителен весь:
    та же цена даёт другой спред на другой кривой."""
    global _memo_version, _yoi_cache_epoch
    if version != _memo_version:
        same_day = bool(_memo_version) and _memo_version[0] == version[0]
        _level_memo.clear()
        # КОНТЕКСТ РАСЧЁТА КРИВОЙ НЕ ПРИНАДЛЕЖИТ. От неё в нём зависит одна
        # ССЫЛКА, всё остальное (ref_obj, график купонов, амортизации, оферты,
        # НКД) собрано из реестра и MOEX и на новой кривой ровно то же. Раньше
        # его сносили целиком, и каждая пересборка кривых внутри дня отправляла
        # весь рынок в холодный догрев по 10 бумаг за такт: 1300 бумаг — больше
        # десяти минут, всё это время цена набора по объёму стояла прочерком.
        # Внутри дня — ПЕРЕПРИВЯЗЫВАЕМ кривую на месте (микросекунды, без сети),
        # сносим только на смене дня: там меняются и потоки, и НКД, и графики.
        if same_day and _eval_ctx and ctx:
            _rebind_curves(ctx)
        else:
            _eval_ctx.clear()
        # спред по цене считан на ТОЙ кривой — набор цен прежний, число другое
        _yoi_cache.clear()
        _yoi_grid.clear()
        # безнадёжность была свойством ПРОШЛОЙ кривой и прошлой книги — на новой
        # версии бумага заслуживает попытки, а не паузы до истечения таймера
        _grid_cold.clear()
        _yoi_cache_epoch += 1
        _memo_version = version


def _store_rows(market_cache: dict, rows: Dict[str, dict]) -> None:
    """Строки такта — по своим витринам: флоатеры в universe_metrics (его читает
    /api/bonds), фиксы в fixed_metrics (его читает /api/fixed). Один кэш на всех
    завёл бы две разные схемы строки в одном словаре."""
    fx = {i: r for i, r in rows.items() if r.get("_kind") == "fixed"}
    fl = {i: r for i, r in rows.items() if i not in fx}
    if fl:
        um = market_cache.get("universe_metrics") or {}
        um.update(fl)
        market_cache["universe_metrics"] = um
    if fx:
        fm = market_cache.get("fixed_metrics") or {}
        # delta_ytm живёт дневным срезом (apply_ytm_delta) и от цены не зависит —
        # сохраняем прежнее значение, иначе тик сделки стирал бы колонку Δ YTM
        for isin, row in fx.items():
            prev = fm.get(isin) or {}
            if row.get("delta_ytm") is None and prev.get("delta_ytm") is not None:
                row["delta_ytm"] = prev["delta_ytm"]
        fm.update(fx)
        market_cache["fixed_metrics"] = fm


async def _warm_pass(ctx: dict, wsmod, market_cache: dict, tick0: float) -> None:
    """ДОГРЕВ ДОЛЕЙ ТАКТА, а не «в простое».

    Первая версия ждала пустых очередей — и не выполнялась НИ РАЗУ: на живом
    рынке стаканы пушат 600–900 сообщений в минуту, очередь сторон пополняется
    быстрее, чем разгребается, и условие «обе очереди пусты» ложно всегда
    (прод 28.08.2026: ctx застыл на 530 из 609, МТС 3Р-02 держала прочерк в
    спреде по средневзвесу при полностью исправном расчёте — 194 б.п. по первому
    же вызову вручную). Поэтому догрев берёт остаток бюджета такта: живой
    пересчёт по-прежнему первым, но хвост рынка перестаёт ждать тишины,
    которой не бывает.

    Порядок: сначала контексты (без них сетку не из чего строить), потом сетки."""
    from services.heavy import run_heavy
    from services.market_data import MarketDataService

    def left() -> float:
        return _WARM_BUDGET_SEC - (time.monotonic() - tick0)

    # контексты — только по остатку такта: за ними ходят в сеть, и это хвост
    # рынка (бумаги без сделок сегодня). Сетки ниже греются в любом случае.
    cold = _ctx_warm_targets(ctx["uni_by"], _CTX_WARM_BATCH) if left() > 0 else []
    if cold:
        keys = {i: ((ctx["fixed_by"].get(i) or {}).get("secid") or i) for i in cold}
        fulls = await asyncio.gather(
            *(MarketDataService.fetch_bond_schedule_full(keys[i]) for i in cold),
            return_exceptions=True)
        # расписания догрева — поверх батчевых: ctx переиспользуется тактом
        ctx["full_by"] = dict(ctx.get("full_by") or {})
        ctx["full_by"].update({i: ({} if isinstance(f, Exception) else f or {})
                               for i, f in zip(cold, fulls)})
        await run_heavy(warm_ctx, cold, ctx, time.monotonic() + _WARM_SLICE_SEC)
    # ГАРАНТИРОВАННЫЙ МИНИМУМ, а не «что осталось». На живом рынке остатка не
    # бывает: полный пересчёт и очередь сторон съедают такт целиком, и догрев
    # сеток не выполнялся НИ РАЗУ — рынок так и жил без сеток, а каждая волна
    # размера тикета оплачивала их заново по дорогой ветке. Поэтому при съеденном
    # бюджете греем урезанной пачкой, а не выходим.
    # ДОБОР ПРОЧЕРКОВ: бумаги, у которых цена стороны есть, а спреда нет (см.
    # _blank_side_targets). Ставим их в очередь сторон низким приоритетом —
    # живой поток остаётся первым, а хвост рынка перестаёт ждать события,
    # которого может не случиться за весь день.
    if len(_sides_dirty) < _sides_batch():
        for isin in _blank_side_targets(ctx["board"], _BLANK_SIDES_BATCH):
            _queue_sides(isin, _SIDES_PRIO_WAVE)

    # СЛАЙСАМИ, а не одной пачкой: бюджет такта тот же, но между заходами
    # управление возвращается петле (см. _WARM_SLICE_SEC). Первый заход
    # выполняется всегда — это тот самый гарантированный минимум.
    first, slices = True, 0
    while first or (left() > 0 and slices < _WARM_MAX_SLICES):
        slices += 1
        batch = _GRID_WARM_BATCH if left() > 0 else max(1, _GRID_WARM_BATCH // 5)
        targets = _grid_warm_targets(batch)
        if not targets:
            return
        warmed = await run_heavy(warm_grids, targets, ctx["board"],
                                 time.monotonic() + _WARM_SLICE_SEC)
        first = False
        # СЕТКА БЕЗ ПУБЛИКАЦИИ — это спред, которого никто не увидит: строку
        # обновляет либо волна размера (она уже прошла), либо движение сторон (у
        # неликвида его можно ждать часами). Поэтому прогретые бумаги сразу же
        # получают числа в строку и уходят подписчикам.
        if warmed and active_vol_sizes():
            # публикуем ТЕ, У КОГО СЕТКА ПОЯВИЛАСЬ, а не первые warmed из
            # списка: заход пропускает бумаги без книги и без узлов, и срез по
            # счётчику попал бы не в те строки
            done = [i for i in targets if i in _yoi_grid]
            rows = await run_heavy(apply_vol_sizes, done)
            if rows:
                _store_rows(market_cache, rows)
                await _push_metrics(wsmod, rows)
        if not warmed:          # заход не дал ничего — упёрлись не во время
            return


async def metrics_worker() -> None:
    """Такт 5с: полный пересчёт только изменившихся цен и только новых уровней.
    Результат — в market_cache['universe_metrics'] (его читает /api/bonds и
    /api/bonds/quotes) и push-патчем подписчикам избранного."""
    from api.routes import ws as wsmod
    from services.market_data import MarketDataService, market_cache
    await asyncio.sleep(40)
    done_since_log = 0
    sides_since_log = 0
    # ЦЕНА ТАКТА в миллисекундах: без неё «стало тяжелее» — спор, а не факт.
    # Полный пересчёт и пересчёт сторон меряются отдельно: у них разная природа
    # (сборка потока против переоценки готового) и разные рычаги настройки.
    full_ms = sides_ms = 0.0
    last_log = time.time()
    global _grid_budget
    while True:
        await asyncio.sleep(_BATCH_SEC)
        try:
            # минутная сводка — живой ли конвейер и каков хит-рейт кэша уровней
            # печатаем, если была ЛЮБАЯ работа: в тихом рынке полных пересчётов
            # нет вовсе, и по прежнему условию сводка молчала — как раз в том
            # режиме, который и надо мерить
            if (done_since_log or sides_since_log) and time.time() - last_log >= 60:
                global _depth_msgs
                logger.info("metrics engine: %d строк/мин (%.1fс, %.0fмс/шт) · "
                            "сторон %d/мин (%.1fс, %.0fмс/шт, пачка %d) · "
                            "memo %d (hit %d / miss %d) · ctx %d · сетки %d (+%d/мин, холодных %d) · "
                            "dirty %d (+%d сторон) · прочерков %d · "
                            "depth-пушей %d/мин (%d бумаг)",
                            done_since_log, full_ms / 1000.0,
                            full_ms / max(1, done_since_log),
                            sides_since_log, sides_ms / 1000.0,
                            sides_ms / max(1, sides_since_log), _sides_batch(),
                            len(_level_memo), _memo_hits, _memo_misses,
                            len(_eval_ctx), len(_yoi_grid), _grid_builds, len(_grid_cold),
                            len(_dirty), len(_sides_dirty), _blank_sides_count(),
                            _depth_msgs, len(_depth_streamed))
                done_since_log = 0
                sides_since_log = 0
                globals()["_grid_builds"] = 0
                full_ms = sides_ms = 0.0
                _depth_msgs = 0
                last_log = time.time()
            # СТРАХОВКА ВОЛНЫ: размер тикета мог прийти не по сокету (ручка
            # /api/bonds) — там задачу никто не заводит.
            if _vol_wave_pending:
                await flush_vol_sizes()
            ctx = await _day_ctx()
            if ctx is None:
                continue
            _check_version(ctx["version"], ctx)
            from services.heavy import run_heavy
            _tick0 = time.monotonic()
            _grid_budget = _GRID_BUILD_PER_TICK   # потолок построений на такт

            # ПОЛНЫЙ пересчёт — сменившим цену сделки (новый уровень цены).
            take = list(_dirty)[:_MAX_BATCH]
            _dirty.difference_update(take)
            if take:
                # расписания батча — из day-кэша (промах = одна ходка на бумагу в день).
                # У ОФЗ bondization по ISIN (RU000…) НЕ резолвится — только по
                # SECID (SU26…), поэтому фиксы спрашиваем их идентификатором.
                keys = {i: ((ctx["fixed_by"].get(i) or {}).get("secid") or i) for i in take}
                fulls = await asyncio.gather(
                    *(MarketDataService.fetch_bond_schedule_full(keys[i]) for i in take),
                    return_exceptions=True)
                ctx["full_by"] = {i: ({} if isinstance(f, Exception) else f or {})
                                  for i, f in zip(take, fulls)}
                # СЛАЙСАМИ: бюджет тот же, но между заходами управление
                # возвращается петле (см. _HEAVY_SLICE_SEC). Иначе пачка держит
                # GIL целиком, и всё это время сайт не отвечает.
                rest = [(i, _last_quote.get(i) or {}) for i in take]
                while rest:
                    pend: list = []
                    _t0 = time.perf_counter()
                    rows = await run_heavy(
                        _crunch, rest, ctx,
                        deadline=time.monotonic() + _HEAVY_SLICE_SEC, pending=pend)
                    full_ms += (time.perf_counter() - _t0) * 1000.0
                    if rows:
                        _store_rows(market_cache, rows)
                        done_since_log += len(rows)
                        await _push_metrics(wsmod, rows)
                    if not pend:
                        break
                    # такт не резиновый: остаток очереди досчитаем следующим,
                    # иначе стороны и догрев не получат времени вовсе
                    if time.monotonic() - _tick0 >= _FULL_BUDGET_SEC:
                        _dirty.update(pend)      # вернули в очередь, не потеряли
                        break
                    quotes = dict(rest)
                    rest = [(i, quotes.get(i) or {}) for i in pend]
            # ДЕШЁВАЯ ОЧЕРЕДЬ: у этих бумаг сдвинулись только стороны стакана —
            # уровень цены сделки прежний, пересчитываем ТОЛЬКО Y-IDX сторон и
            # средневзвеса (~13 мс на бумагу). Без этого точное число стороны
            # ждало бы следующей сделки, а до неё жила линеаризация в браузере.
            if _sides_dirty:
                # порядок — по приоритету: живые движения сторон впереди волны
                take_s = sorted(_sides_dirty, key=_sides_dirty.get)[:_sides_batch()]
                for _i in take_s:
                    _sides_dirty.pop(_i, None)
                _t0 = time.perf_counter()
                srows = await run_heavy(recrunch_sides, take_s, ctx["board"])
                _took = (time.perf_counter() - _t0) * 1000.0
                sides_ms += _took
                # цена стороны плавает от книги и глубины — держим скользящее
                # среднее, по нему следующий такт и выбирает размер пачки
                globals()["_sides_ms_avg"] = (
                    _took / len(take_s) if _sides_ms_avg <= 0
                    else 0.7 * _sides_ms_avg + 0.3 * (_took / len(take_s)))
                if srows:
                    _store_rows(market_cache, srows)
                    sides_since_log += len(srows)
                    await _push_metrics(wsmod, srows)
            # хвост такта — догрев контекстов и сеток (см. _warm_pass)
            await _warm_pass(ctx, wsmod, market_cache, _tick0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("metrics worker: %s", e)
