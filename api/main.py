import os
import logging
import sys
import time
import uvicorn

# GIL-конвой: CPU-bound Python-поток (heavy-кранш: компаундинг RUONIA по дням,
# reprice) на дефолтном switch-interval 5мс тут же перезахватывает GIL, и на
# двухъядерном хосте event loop голодал СЕКУНДАМИ (стеки сторожа: coupon_calib.
# _index_grow держал loop 13–17с, хотя крутился «в отдельном потоке»). Интервал
# 1мс заставляет планировщик отдавать GIL loop'у на порядок чаще: лаги падают до
# миллисекунд ценой ~1-2% CPU на переключения.
sys.setswitchinterval(0.001)
from fastapi import FastAPI, Request

# Логи приложения (services/*, api/*) — в stdout с таймстампами; uvicorn настраивает
# только свои логгеры, наши без basicConfig терялись (раньше вся диагностика шла print'ом)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Второй приёмник логов — ФАЙЛ на томе data/. `docker logs` обнуляется при
# каждом редеплое (контейнер пересоздаётся), поэтому разбор жалобы «вчера сайт
# висел» упирался в отсутствие улик. Ротация: 5 файлов по 20 МБ.
try:
    from logging.handlers import RotatingFileHandler
    from services.paths import log_path
    _fh = RotatingFileHandler(log_path("app.log"), maxBytes=20 * 1024 * 1024,
                              backupCount=5, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
except Exception as e:                    # том не смонтирован / нет прав — не падаем
    logger.warning("file log disabled: %s", e)

# Шум сетевых клиентов — в WARNING: httpx пишет INFO-строку на КАЖДЫЙ запрос к
# MOEX (три доски раз в 5 секунд), и в файле ротация съедала бы диагностику
# ротацией того же httpx. Ошибки соединений при этом остаются видны.
for _noisy in ("httpx", "httpcore", "urllib3", "websockets", "aiosqlite"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import health, meta, bonds, curves, orderbook, ws, auth, instruments, fixed, status, history, trades, blocks, calc, tg, signals as signals_route
from api.routes.auth import require_user
from fastapi import Depends
from services.exceptions import APIException
from contextlib import asynccontextmanager
import asyncio
from datetime import date, datetime, timedelta, timezone
from services.market_data import MarketDataService
from services import instruments_registry
from services.pools import run_bg

WS_PUSH_INTERVAL = 5        # такт пуша цен в торговые часы, сек
WS_IDLE_INTERVAL = 60       # вне торговых часов — только перепроверка календаря
WS_PRICE_HEARTBEAT = 60     # пуш неизменной цены не чаще раза в столько секунд


async def ws_market_data_broadcaster():
    """Пуш last-price подписчикам WS.

    Только ISIN с ЖИВЫМИ подписчиками (active_market_isins) — раньше брались все
    ключи карты, а опустевшие не удалялись: за аптайм список рос монотонно и
    каждые 5с уходил в Alor запросом цен по бумагам, которые никто не смотрит.
    Вне торговых часов не опрашиваем вовсе — цена не меняется (тот же гейт, что
    у universe_price_poller / depth_poller).

    ПУШИМ ТОЛЬКО ИЗМЕНЕНИЯ. Раньше такт слал цену безусловно, а фронт на каждый
    пуш дёргал /reprice: 20 бумаг × 5с = 3.4 запроса/сек круглосуточно (6132 за
    полчаса в проде) на пересчёт того же самого числа. Heartbeat раз в минуту
    оставляем, чтобы клиент видел живой поток; новый подписчик получает
    последнюю цену снапшотом при subscribe.

    В Alor отсюда НЕ ходим. Раньше каждый такт открывал одноразовый WS-сокет
    (подписка на пачку, ждать до 4с, закрыть) — сокет каждые 5 секунд. Теперь
    цены уже лежат в кэше: quotes_poller сеет его board-снапшотом MOEX тем же
    тактом, а по избранному кэш обновляют live-пуши alor_ws (Alor и так
    транслирует данные MOEX — источник один, дублировать канал незачем)."""
    last_push: dict[str, tuple[float, float]] = {}   # isin → (цена, monotonic)
    while True:
        idle = True
        try:
            if _in_moex_trading_hours():
                # такт держим коротким и без подписчиков: свежий подписчик должен
                # получить цену через 5с, а не ждать длинный idle-такт (сети тут нет)
                idle = False
                # бумаги с живым стримом Alor (избранное) пропускаем: по ним
                # broadcast делает сам alor_ws на каждый пуш. Стрим упал — набор
                # пуст, и такт автоматически снова закрывает их собой
                from services.universe_stream import live_isins
                streamed = live_isins()
                active_isins = [i for i in ws.manager.active_market_isins()
                                if i not in streamed]
                if active_isins:
                    prices = MarketDataService.session_prices()
                    now = time.monotonic()
                    for isin in active_isins:
                        px = prices.get(isin)
                        if px is None:
                            continue
                        prev = last_push.get(isin)
                        if prev and prev[0] == px and now - prev[1] < WS_PRICE_HEARTBEAT:
                            continue
                        last_push[isin] = (px, now)
                        await ws.manager.broadcast_market_data(isin, {"last_price_pct": px})
                    # отписались — снимаем и знак, иначе карта растёт за аптайм
                    for gone in set(last_push) - set(active_isins):
                        last_push.pop(gone, None)
        except Exception as e:
            logger.warning(f"WS Broadcaster error: {e}")

        await asyncio.sleep(WS_IDLE_INTERVAL if idle else WS_PUSH_INTERVAL)


# Опрос Alor по всему юниверсу флоатеров (вне watchlist) — редко, чтобы держать
# колонку PRICE более-менее актуальной без нагрузки WS на 453 бумаги.
from services.paths import cache_path as _cache_path
_ISINS_CACHE = _cache_path("isins_cache.json")
UNIVERSE_POLL_INTERVAL = 600      # 10 минут
UNIVERSE_POLL_CHUNK = 150         # ISIN за один WS-заход батч-снимка стаканов (depth_poller)
_MSK = timezone(timedelta(hours=3))

def _in_moex_trading_hours() -> bool:
    """Пн–Пт, ~07:00–23:50 МСК (охватывает утреннюю+основную+вечернюю сессии)."""
    now = datetime.now(_MSK)
    if now.weekday() >= 5:  # сб/вс
        return False
    minutes = now.hour * 60 + now.minute
    return 7 * 60 <= minutes <= 23 * 60 + 50

async def _warm_fixed(market_cache):
    """Прогрев вкладки ФИКСЫ: универс (ОФЗ-ПД + ликвидные корпораты) + метрики
    к погашению (YTM/g-спред/z-спред/дюрация). Расписания MOEX day-кэшируются →
    первый прогон тяжёлый (bondization ~700 бумаг), дальше дёшево. Кладём в
    market_cache, эндпоинт /api/fixed отдаёт кэш."""
    try:
        from services import fixed_income as fi
        funi = await fi.fetch_fixed_universe()
        _r, _k, _cd, _rd = await MarketDataService.get_curves()
        _ek, _eu, g = await MarketDataService.get_zspread_ctx()
        fcd = _cd or _rd or date.today()
        fm = await fi.compute_fixed_metrics_all(funi, g, fcd)
        fi.apply_ytm_delta(fm, fcd.isoformat())   # Δ YTM день-к-дню
        market_cache["fixed_universe"] = funi
        market_cache["fixed_metrics"] = fm
        market_cache["fixed_calc_date"] = fcd.isoformat()
    except Exception as e:
        logger.warning(f"fixed warm error: {e}")


async def universe_price_poller():
    """Раз в UNIVERSE_POLL_INTERVAL: (1) тянет last-price Alor по всему юниверсу
    чанками → market_cache['last_prices']; (2) считает полные метрики (dirty/DM/
    z_model/carry/next_coupon) по всему юниверсу → market_cache['universe_metrics'].
    Юниверс-роут читает эти кэши — бумаги вне watchlist получают live-цену И расчёт.
    Данные MOEX кэшируются на день, поэтому тяжёлый прогрев (bondization) — раз/день."""
    from services.universe import compute_universe_metrics
    from services.market_data import market_cache
    from services.instruments_sync import sync_instruments
    await asyncio.sleep(30)  # прогрев: не конкурировать со стартом
    _last_reg_sync = None
    while True:
        try:
            # ежедневный синк реестра инструментов (обнаружение новых бумаг +
            # добор maturity из MOEX). Раз в день, независимо от торговых часов.
            _today = date.today().isoformat()
            if _last_reg_sync != _today:
                try:
                    await sync_instruments()
                    _last_reg_sync = _today
                except Exception as e:
                    logger.warning(f"instruments sync error: {e}")
            # бэкфилл эмитента (MOEX EMITTER_ID) — drain-loop: сливаем ВСЕ
            # resolvable за один цикл (эмитент статичен → кэш навсегда). Бумаги без
            # EMITTER_ID (len(emap)<len(miss)) не зацикливаем — выходим. Для
            # фильтра/агрегатов по эмитентам.
            try:
                from services import instruments_registry as _reg
                _filled = 0
                for _ in range(20):   # 20·40=800 > юниверса — хватает на первый проход
                    _miss = _reg.isins_missing_emitter(40)
                    if not _miss:
                        break
                    _emap = await MarketDataService.fetch_emitter_info(_miss)
                    # В _emap только те, по кому MOEX ОТВЕТИЛ: реальный id либо
                    # sentinel 0 («EMITTER_ID нет» — делистинг/ОФЗ), он же уводит
                    # бумагу из missing на неделю. Кого в ответе нет — сетевой сбой,
                    # не клеймим: вернёмся к ним следующим циклом.
                    for _i, (_eid, _enm) in _emap.items():
                        _reg.set_emitter(_i, _eid, _enm)
                    _filled += sum(1 for _v in _emap.values() if _v[0])
                    if not _emap:
                        break   # MOEX недоступен — крутить батч бессмысленно
                    await asyncio.sleep(0.5)      # мягкий rate-limit между батчами
                if _filled:
                    logger.info(f"emitter backfill: +{_filled}")
            except Exception as e:
                logger.warning(f"emitter backfill error: {e}")
            # дискавери новых флоатеров — драйн КАЖДЫЙ цикл (negative-кэш + cap/цикл),
            # 24/7: бэклог MOEX-листинга сходится за часы, а не «80/день». Без этого
            # раз/день + cap head перечекивался, хвост не достигался (голодание).
            try:
                from services.instruments_sync import discover_floaters
                _nd = await discover_floaters(cap=60)
                if _nd:
                    logger.info(f"discovery: +{_nd} new floaters")
            except Exception as e:
                logger.warning(f"discovery drain error: {e}")
            if _in_moex_trading_hours():
                uni = await instruments_registry.fetch_floater_universe()
                isins = [u["isin"] for u in uni if u.get("isin")]
                # Цены НЕ опрашиваем: раньше здесь шёл Alor-свип чанками по 150
                # (4 одноразовых WS-сессии, каждая ждала неликвид до 4с), но
                # market_cache['last_prices'] уже держит свежим quotes_poller
                # (board-снапшот MOEX тактом 5с) + live-пуши alor_ws — те же
                # котировки без единой лишней сессии.
                metrics = await compute_universe_metrics(uni, isins, _ISINS_CACHE)
                if metrics:
                    market_cache["universe_metrics"] = metrics
                await _warm_fixed(market_cache)
            # рейтинги с corpbonds — НЕ зависят от торговых часов (парсятся всегда),
            # драйн 24/7: cap/цикл, negative-кэш промахов → сходится за проход.
            # Универсы тянем независимо (кэшированы), чтобы драйн шёл и ночью.
            try:
                from services import ratings, fixed_income as fi
                fx_uni = market_cache.get("fixed_universe") or await fi.fetch_fixed_universe()
                fl_uni = await instruments_registry.fetch_floater_universe()
                # ОФЗ исключаем: суверен → AAA по правилу (bucket_of_fixed), на
                # corpbonds его нет → 32 гарантированных 404 в начале списка зря.
                fx = [u["isin"] for u in fx_uni if u.get("isin") and u.get("cls") != "ofz"]
                fl = [u["isin"] for u in fl_uni if u.get("isin")]
                allids = list(dict.fromkeys(fx + fl))
                await ratings.refresh(allids, cap=80)
            except Exception as e:
                logger.warning(f"ratings drain error: {e}")
        except Exception as e:
            logger.warning(f"Universe poller error: {e}")
        await asyncio.sleep(UNIVERSE_POLL_INTERVAL)

def _thread_stacks_brief() -> str:
    """Верхушки стеков всех потоков, кроме своего — атрибуция «кто держал CPU»
    в момент лага. Только наш код (пути с /app либо репо), по 3 кадра."""
    import sys
    import threading
    me = threading.get_ident()
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for tid, frame in sys._current_frames().items():
        if tid == me:
            continue
        frames, f = [], frame
        while f is not None and len(frames) < 3:
            fn = f.f_code.co_filename
            if "site-packages" not in fn and ("/app" in fn or "shabshab" in fn):
                frames.append(f"{fn.rsplit('/', 1)[-1]}:{f.f_lineno}:{f.f_code.co_name}")
            f = f.f_back
        if frames:
            out.append(f"[{names.get(tid, tid)}] " + " < ".join(frames))
    return " | ".join(out) or "(стеков нашего кода нет — C-код/сеть)"


STREAM_SILENCE_MIN = float(os.getenv("STREAM_SILENCE_MIN", "10"))


# Как часто ПОВТОРЯТЬ предупреждение об одной и той же беде. Сторожа ходят
# часто, но одна поломка не должна звонить каждый такт: чинить её начинают с
# первого сообщения, а дальше поток превращает тревогу в фон.
STREAM_ALERT_REPEAT_MIN = float(os.getenv("STREAM_ALERT_REPEAT_MIN", "60"))

# что сейчас сломано → когда об этом сообщили в последний раз, по сторожам
_stream_alerted: dict = {}
_disk_alerted: dict = {}


async def _watch_alert(state: dict, problems: dict, title: str, tail: str,
                       ok_text: str) -> None:
    """Доводит находки сторожа до людей и даёт отбой, когда починилось.

    Лога мало: отказы, которые тут ловятся, тем и коварны, что снаружи
    выглядят нормально — рынок «спокоен», обслуживание «прошло». Никто не идёт
    смотреть логи, потому что нет повода. Поэтому предупреждение уходит в
    телеграм админам.

    Новая беда сообщается сразу, известная — не чаще STREAM_ALERT_REPEAT_MIN.
    Восстановление сообщается один раз: «отбой» без предшествующей тревоги
    никому не нужен. Недоставленное предупреждение НЕ помечается сообщённым —
    иначе одна сетевая ошибка похоронила бы его до конца дня."""
    from services.tg_notify import notify_admins
    now = time.monotonic()

    fixed = [k for k in state if k not in problems]
    for k in fixed:
        state.pop(k, None)
    if fixed and not problems:
        await notify_admins(ok_text)

    due = [k for k in problems
           if now - state.get(k, float("-inf")) >= STREAM_ALERT_REPEAT_MIN * 60]
    if not due:
        return
    body = "\n".join(f"• {problems[k]}" for k in due)
    if await notify_admins(f"{title}\n{body}\n\n{tail}"):
        for k in due:
            state[k] = now


async def _stream_alert(problems: dict) -> None:
    """Тревога сторожа стримов: молчащий сокет — это слепой скринер."""
    await _watch_alert(
        _stream_alerted, problems, "🛑 <b>Стрим молчит</b>",
        "<i>Скринер слеп: сигналы не придут, пока это так.</i>",
        "✅ <b>Стримы ожили</b> — данные снова идут")


async def stream_watchdog(period_sec: int = 300):
    """Сторож молчания стримов Alor.

    Самый неприятный отказ — тихий: сокеты не поднялись или отвалились,
    стаканы и сделки не идут, а система выглядит как «рынок спокоен». Сигналы
    честно молчат (скринер не видит глубины), и понять это можно только зайдя
    в /api/status. Раз в пять минут в торговые часы сверяем, есть ли бумаги на
    сокетах и капали ли сделки за последние STREAM_SILENCE_MIN минут; находки
    идут в лог И в телеграм админам (см. _stream_alert) — лог тут не адресат,
    в него никто не смотрит именно тогда, когда надо."""
    from services import trades_stream, universe_stream
    from services.market_data import market_cache as _mc
    await asyncio.sleep(300)          # даём пулам подняться после старта
    while True:
        try:
            if _in_moex_trading_hours():
                problems: dict = {}
                us, ts = universe_stream.stats(), trades_stream.stats()
                if not us.get("streamed"):
                    problems["books"] = ("стаканы — 0 бумаг на сокетах")
                elif not (_mc.get("depth") or {}):
                    problems["depth"] = (f"сокеты стаканов живы "
                                         f"({us['streamed']} бумаг), но кэш глубины пуст")
                if not ts.get("streamed"):
                    problems["trades"] = "сделки — 0 бумаг на сокетах"
                else:
                    last = ts.get("last_ts")
                    quiet = _quiet_min(last)
                    if quiet is not None and quiet > STREAM_SILENCE_MIN:
                        problems["quiet"] = (f"сделок нет {quiet:.0f} мин "
                                             f"(последняя {last})")
                for msg in problems.values():
                    logger.warning("СТРИМ МОЛЧИТ: %s", msg)
                await _stream_alert(problems)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("stream watchdog error: %s", e)
        await asyncio.sleep(period_sec)


def _quiet_min(last_ts):
    """Сколько минут прошло с отметки стрима ('YYYY-MM-DD HH:MM:SS' МСК)."""
    if not last_ts:
        return None
    try:
        seen = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_MSK)
    except (ValueError, TypeError):
        return None
    return (datetime.now(_MSK) - seen).total_seconds() / 60


async def loop_lag_watchdog():
    """Сторож event loop: секундный такт, замер задержки пробуждения. Лаг
    больше полсекунды = что-то синхронное держит ядро — пишем в лог, чтобы
    «сайт подвисает» диагностировался строкой, а не гаданием. При большом лаге
    печатаем стеки потоков — прямая атрибуция виновника."""
    while True:
        t = time.monotonic()
        await asyncio.sleep(1.0)
        lag = time.monotonic() - t - 1.0
        if lag > 0.5:
            logger.warning("event loop лаг %.2fс — ядро блокировано синхронной работой", lag)
            if lag > 2.0:
                try:
                    logger.warning("потоки в момент лага: %s", _thread_stacks_brief())
                except Exception:
                    pass


QUOTES_POLL_INTERVAL = float(os.getenv("QUOTES_POLL_INTERVAL", "5"))


async def quotes_poller():
    """Котировки всего рынка тактом 5с (торговые часы).

    Board-снапшот MOEX — 3 запроса (TQCB/TQOB/TQRD) на ~540 бумаг разом, в
    ответе LAST/BID/OFFER/WAPRICE/VALTODAY. Дешевле любого per-isin опроса, за
    это и взят: избранное живёт push-стримом Alor, а рынок — вот этим тактом.

    Поллер зовёт снапшот с force=True, поэтому TTL-кэш всегда свежий, и
    остальные его потребители (метрики юниверса, карточки) ходят в готовое.

    Он же сеет market_cache['last_prices'] — единый кэш цен, из которого живут
    session_prices() (метрики юниверса, broadcaster). Раньше этот кэш наполнял
    отдельный Alor-опрос (одноразовые WS-сессии чанками по 150); Alor ретранслирует
    те же котировки MOEX, так что источник тут один и тот же, а сессий — ноль.
    Бумаги живого стрима не трогаем: их цены в кэш пишет пул universe_stream
    своим пушем, и он свежее снапшота."""
    from services.market_data import market_cache
    from services.universe_stream import live_isins
    await asyncio.sleep(20)      # даём стартовому прогреву занять сеть первым
    while True:
        try:
            if _in_moex_trading_hours():
                snap = await MarketDataService.fetch_board_snapshot(force=True)
                if snap:
                    now = time.time()
                    streamed = live_isins()
                    fresh = {i: v["last"] for i, v in snap.items()
                             if v.get("last") is not None and i not in streamed}
                    market_cache["last_prices"].update(fresh)
                    market_cache["last_prices_ts"].update({i: now for i in fresh})
                    market_cache["quotes_ts"] = now
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"quotes poller error: {e}")
        await asyncio.sleep(QUOTES_POLL_INTERVAL if _in_moex_trading_hours()
                            else WS_IDLE_INTERVAL)


async def warmup_caches():
    """Прогрев дорогих на ХОЛОДНУЮ кэшей сразу при старте, чтобы ПЕРВЫЙ запрос
    пользователя не платил их латентность (после каждого деплоя контейнер холодный).
    Тяжёлое: cbr._refresh (~1.2с — 2 сетевых запроса к cbr.ru за историей КС/RUONIA)
    и bootstrap кривых. Идёт конкурентно, старт сервера не блокирует; поллер
    отдельно (он спит 30с и греет ещё и цены Alor + метрики юниверса)."""
    from services import progress
    # шагов ровно столько, сколько await'ов ниже: страница СТАТУС показывает,
    # на чём именно стоит прогрев после рестарта
    progress.start("warmup", "Прогрев кэшей после старта", total=6,
                   detail="ставки ЦБ")
    try:
        from services import cbr
        from services.market_data import MarketDataService, market_cache
        from services.universe import compute_universe_metrics
        await run_bg(cbr.ks_history)                 # триггерит _refresh (сеть)
        progress.advance("warmup", detail="история RUONIA", force=True)
        await run_bg(cbr.ruonia_history)
        progress.advance("warmup", detail="кривые RUONIA/KEYRATE", force=True)
        await MarketDataService.get_curves()          # bootstrap RUONIA/KEYRATE
        progress.advance("warmup", detail="z-спред контекст (g-curve)", force=True)
        await MarketDataService.get_zspread_ctx()      # ExpCurve + g-curve
        progress.advance("warmup", detail="метрики флоатеров", force=True)
        # Метрики юниверса (dm/z/carry) — сразу на prev-close, НЕ дожидаясь медленного
        # прогрева live-цен Alor поллером (30с сон + чанки по 4с WS-таймаута = ~60с
        # пустых метрик после рестарта). Поллер потом уточнит их live-ценами.
        if not market_cache.get("universe_metrics"):
            uni = await instruments_registry.fetch_floater_universe()
            isins = [u["isin"] for u in uni if u.get("isin")]
            if uni:
                m = await compute_universe_metrics(uni, isins, _ISINS_CACHE)
                if m:
                    market_cache["universe_metrics"] = m
        progress.advance("warmup", detail="метрики фиксов", force=True)
        await _warm_fixed(market_cache)
        # календарь выплат — тем же прогревом (плашка выплат в нижней строке
        # иначе платит его полным пересчётом при первом открытии)
        progress.advance("warmup", detail="календарь выплат", force=True)
        try:
            from services.payments_calendar import build_payments_calendar
            await build_payments_calendar()
        except Exception as e:
            logger.warning("warmup календаря выплат: %s", e)
        progress.finish("warmup", detail="кэши готовы")
    except Exception as e:
        logger.warning(f"warmup error: {e}")
        progress.finish("warmup", error=f"{type(e).__name__}: {e}")


async def daily_prewarm():
    """Каждый день в 09:00 МСК — контролируемый тяжёлый прогрев дня: расписания
    bondization (кэш протухает в 09:00 по _trading_day) + фикс-метрики + метрики
    юниверса. К 10:00 (основная сессия) всё готово; тяжёлый ре-warm не бьёт по
    карточкам/стакану среди дня (раньше протухал в полночь → лениво утром)."""
    from services.market_data import MarketDataService, market_cache
    from services.universe import compute_universe_metrics
    while True:
        now = datetime.now(_MSK)
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep(max(1.0, (target - now).total_seconds()))
        try:
            logger.info("daily 09:00 prewarm: старт")
            uni = await instruments_registry.fetch_floater_universe()
            isins = [u["isin"] for u in uni if u.get("isin")]
            if uni:
                m = await compute_universe_metrics(uni, isins, _ISINS_CACHE)
                if m:
                    market_cache["universe_metrics"] = m
            await _warm_fixed(market_cache)
            # Календарь выплат: полный поток по универсу считается раз в день и
            # держится в памяти. Без прогрева его первым платил случайный
            # пользователь — 5 секунд ожидания на открытии плашки выплат.
            try:
                from services.payments_calendar import build_payments_calendar
                cal = await build_payments_calendar()
                logger.info("prewarm календаря выплат: событий %d", len(cal["events"]))
            except Exception as e:
                logger.warning("prewarm календаря выплат: %s", e)
            logger.info("daily 09:00 prewarm: готово (расписаний %d)",
                        len(MarketDataService._full_mem))
        except Exception as e:
            logger.warning(f"daily prewarm error: {e}")


# Ночной прогрев спреда: топ по обороту, окно, которое реально смотрят.
NIGHTLY_WARM = os.getenv("NIGHTLY_WARM", "1") not in ("0", "false", "no")
NIGHTLY_WARM_TOP = int(os.getenv("NIGHTLY_WARM_TOP", "200"))
NIGHTLY_WARM_DAYS = int(os.getenv("NIGHTLY_WARM_DAYS", "150"))


async def nightly_spread_warm():
    """Каждую ночь в 03:00 МСК — досчёт спреда по самым торгуемым бумагам.

    Честный as-of дорог (своя кривая/НКД/номинал на каждый день, порядка минут
    на бумагу), поэтому греем топ по обороту на окно 150 дней, а не весь универс
    на всю глубину. Считается один раз: результат штампуется metrics_ver и при
    следующем открытии графика не пересчитывается."""
    if not NIGHTLY_WARM:
        logger.info("nightly warm выключен (NIGHTLY_WARM=0)")
        return
    from services import bars as bars_svc
    while True:
        now = datetime.now(_MSK)
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep(max(1.0, (target - now).total_seconds()))
        try:
            logger.info("nightly warm 03:00: старт (топ %d, окно %d дн)",
                        NIGHTLY_WARM_TOP, NIGHTLY_WARM_DAYS)
            await bars_svc.warm_hot(days=NIGHTLY_WARM_DAYS, top=NIGHTLY_WARM_TOP)
        except Exception as e:
            logger.warning("nightly warm error: %s", e)


async def nightly_daily_rollup():
    """Каждую ночь в 03:30 МСК — свёртка часов в дни по ВСЕМУ юниверсу.

    Дешёвая (агрегация уже посчитанных часов: ни сети, ни солвера) и трогает
    только новые дни, поэтому идёт по всем бумагам, а не по топу.

    Своим тактом, а не хвостом ночного прогрева: и прогрев, и часовой демон
    (:07 каждого часа) пишут в ту же базу на 2 ГБ, и три писателя в одну минуту
    дают взаимные блокировки. 03:30 — свободная от обоих точка."""
    from services import bars as bars_svc
    while True:
        now = datetime.now(_MSK)
        target = now.replace(hour=3, minute=30, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep(max(1.0, (target - now).total_seconds()))
        try:
            stat = await bars_svc.build_daily_universe()
            logger.info("nightly daily rollup 03:30: %s", stat)
        except Exception as e:
            logger.warning("nightly daily rollup error: %s", e)


async def memory_watch(period_sec: int = 1800):
    """Раз в полчаса пишет в лог RSS и длины долгоживущих кэшей.

    Тот же снимок, что отдаёт /api/status/memory, но в лог: эндпоинт закрыт
    авторизацией, а утечку надо ловить по ТРЕНДУ за часы. 13.08 процесс вырос
    599 → 1004 МБ за ночь и почти упёрся в лимит контейнера — без истории по
    кэшам виновника не найти."""
    from api.routes.status import _rss_mb
    while True:
        try:
            from services import backdate as bd
            from services.market_data import MarketDataService as MD
            from services import universe_stream as us
            parts = [f"honest={len(bd._honest_memo)}", f"anchor={len(bd._anchor_memo)}",
                     f"full={len(MD._full_mem)}", f"snap={len(MD._snap_cache)}",
                     f"secid={len(MD._secid_cache)}", f"sec={len(MD._sec_cache)}",
                     f"levels={len(us._level_memo)}"]
            try:
                from services import trade_yidx as ty
                parts.append(f"tradectx={len(ty._ctx_cache)}")
            except Exception:
                pass
            logger.info("memory: RSS %.0f МБ · %s", _rss_mb(), " ".join(parts))
        except Exception as e:
            logger.warning("memory watch error: %s", e)
        await asyncio.sleep(period_sec)


SIGNALS_INTERVAL = float(os.getenv("SIGNALS_INTERVAL", "3"))


async def signals_worker():
    """Фон: фильтры вкладки СИГНАЛЫ против снапшота рынка.

    Такт секундный, и это дёшево: стаканы уже лежат в market_cache['depth']
    push'ем от Alor (universe_stream), метрики — в universe_metrics. Тик читает
    ПАМЯТЬ, статический отбор бумаг закеширован на фильтр, событий без движения
    рынка не возникает. Так задержка сигнала равна задержке стакана, а не
    периоду опроса."""
    from services import signals as signals_svc
    await asyncio.sleep(75)     # ждём прогрева движка метрик
    while True:
        try:
            if _in_moex_trading_hours():
                fired = await signals_svc.run_cycle()
                if fired:
                    logger.info("signals: события по %d фильтрам", fired)
        except Exception as e:
            logger.warning(f"signals_worker error: {e}")
        await asyncio.sleep(SIGNALS_INTERVAL)


async def spread_snapshotter():
    """Дневной снапшот спред-метрик (точная история). Разово при старте (если
    метрики прогреты) + каждый день ~19:00 МСК (после основной сессии, метрики
    свежие). Идемпотентно per (isin,date)."""
    from services.spread_history import write_snapshot
    from services.market_data import market_cache
    # стартовый снапшот: ждём прогрева метрик (ретрай до ~5мин), затем пишем
    for _ in range(10):
        await asyncio.sleep(30)
        if market_cache.get("universe_metrics") or market_cache.get("fixed_metrics"):
            try:
                await run_bg(write_snapshot)
            except Exception as e:
                logger.warning(f"spread snapshot (startup) error: {e}")
            break
    while True:
        now = datetime.now(_MSK)
        target = now.replace(hour=19, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep(max(1.0, (target - now).total_seconds()))
        try:
            await run_bg(write_snapshot)
        except Exception as e:
            logger.warning(f"spread snapshot error: {e}")


BARS_WORKER = os.getenv("BARS_WORKER", "1") not in ("0", "false", "no")
BARS_WORKER_DAYS = int(os.getenv("BARS_WORKER_DAYS", "3"))


async def hourly_bars_worker():
    """Часовой налив bar_hourly (средневзвес + спред) и тикового архива по всему
    юниверсу. Окно 3 дня с перехлёстом: закрывает дыры после рестарта и
    доливает свежий час. Тики Alor живут ~30 дней — то, что не слито вовремя,
    теряется навсегда, поэтому демон крутится всегда, а не по требованию UI."""
    if not BARS_WORKER:
        return
    from services import bars as bars_svc
    await asyncio.sleep(120)   # не конкурировать со стартовым прогревом
    while True:
        try:
            # полный обход юниверса (≈30 мин) — раз в 6 часов: подхватывает новые
            # выпуски и вернувшуюся ликвидность. Остальные часы — только бумаги,
            # по которым сделки реально идут (единицы минут).
            full = datetime.now(_MSK).hour % 6 == 0
            # concurrency 2 (не 4): хост двухъядерный, и обход на полной
            # параллельности насыщал CPU целиком — event loop просыпался с
            # лагом до 2с, сайт «подвисал» на время наверстки. Обход станет
            # дольше, но сервер остаётся отзывчивым — демону спешить некуда
            stat = await bars_svc.refresh_universe(days=BARS_WORKER_DAYS, full=full,
                                                   concurrency=2)
            logger.info("hourly bars (full=%s): %s", full, stat)
            # хвост дневной свёртки тем же тактом: окно то же, что у налива
            # часов, и трогаются только дни, где оборот изменился
            daily = await bars_svc.build_daily_universe(days=BARS_WORKER_DAYS)
            logger.info("daily rollup (хвост): %s", daily)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"hourly bars worker error: {e}")
        now = datetime.now(_MSK)
        nxt = (now + timedelta(hours=1)).replace(minute=7, second=0, microsecond=0)
        await asyncio.sleep(max(60.0, (nxt - now).total_seconds()))


BLOCK_POLL_INTERVAL = int(os.getenv("BLOCK_POLL_INTERVAL", "60"))     # опрос ленты, сек
BLOCK_WORKER = os.getenv("BLOCK_WORKER", "1") not in ("0", "false", "False")


async def block_trades_worker():
    """Крупные сделки по всему рынку облигаций: безадресные + РПС/адресные.

    Сквозная лента ISS читается курсором по TRADENO, поэтому такт дешёвый
    (единицы страниц на минуту торгов) — в отличие от Alor, куда за тем же
    пришлось бы ходить по каждой бумаге отдельно.

    Вне торговых часов не крутим; раз в сутки после закрытия догружаем дневные
    РПС-агрегаты — поштучных адресных сделок за прошлые дни ISS не отдаёт, и
    это единственный способ увидеть блоки за дни до запуска сбора."""
    if not BLOCK_WORKER:
        return
    from services import block_trades as bt
    await asyncio.sleep(45)              # пропускаем стартовый прогрев
    backfilled_on = None
    try:
        logger.info("block trades backfill: %s", await bt.backfill())
        backfilled_on = datetime.now(_MSK).date()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"block trades backfill error: {e}")
    while True:
        try:
            now = datetime.now(_MSK)
            if _in_moex_trading_hours():
                res = await bt.sweep()
                if res["saved"]:
                    logger.info("block trades: +%d (просмотрено %d, спред %s)",
                                res["saved"], res["seen"], res.get("priced", 0))
                else:
                    # тихий такт — время добить хвост без спреда (первый запуск
                    # после миграции, бумаги с холодным контекстом)
                    left = await bt.price_new_trades()
                    if left:
                        logger.info("block trades: спред досчитан по %d сделкам", left)
                # Звоним по тому, что этот такт реально принёс: безадресные
                # обычно уже отзвонил живой поток Alor (services/trades_stream),
                # здесь остаются адресные (РПС) и то, чего в потоке не было.
                # Холодный старт архив не вываливает: очередь ограничена окном
                # ins_at (см. block_trades.ALERT_MAX_AGE_MIN).
                sent = await bt.notify_blocks()
                if sent:
                    logger.info("block trades: %d уведомлений", sent)
            else:
                # Вне торгов новых сделок нет, но хвост без спреда добиваем:
                # за такт считается лишь потолок флоатеров, и вечерний наплыв
                # иначе ждал бы утра. Своих сделок расчёт не создаёт — просто
                # доходит до конца очереди и дальше возвращает 0.
                left = await bt.price_new_trades()
                if left:
                    logger.info("block trades: спред досчитан по %d сделкам", left)
                if now.hour == 1 and backfilled_on != now.date():
                    # ночью, когда дневная история ISS уже опубликована
                    backfilled_on = now.date()
                    logger.info("block trades backfill: %s", await bt.backfill())
                    # и тем же заходом ужимаем прошедшие дни до архивного порога:
                    # полный поток рынка нужен только внутри дня
                    logger.info("block trades prune: %s",
                                await run_bg(bt.prune))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"block trades worker error: {e}")
        # вне торгов такт редкий, но пока есть неоценённый хвост — держим
        # рабочий темп, иначе вечерний наплыв досчитывался бы часами
        idle = 60 if await run_bg(bt.unpriced_count) else 600
        await asyncio.sleep(BLOCK_POLL_INTERVAL if _in_moex_trading_hours() else idle)


# Порог тревоги по свободному месту. Считается не «сколько осталось вообще», а
# «хватит ли на обслуживание»: VACUUM переписывает базу целиком и требует места
# в её размер, штатный бэкап снимает несжатую копию — тоже в размер базы.
# Кончится место — база перестанет ужиматься и перестанет копироваться, причём
# ТИХО: оба шага честно отказываются и пишут skip в лог.
DISK_NEED_RATIO = float(os.getenv("DISK_NEED_RATIO", "1.5"))
# Свежесть последней копии. Проверяем результат, а не механику: так ловится и
# упавший крон, и отказ по месту, и битый файл, не доживший до ротации.
BACKUP_STALE_HOURS = float(os.getenv("BACKUP_STALE_HOURS", "36"))
DISK_WATCH_PERIOD_SEC = int(os.getenv("DISK_WATCH_PERIOD_SEC", "3600"))


def _disk_problems() -> dict:
    """Что не так с местом и копиями. Пусто — всё в порядке."""
    import shutil

    from services.portfolio_db import DB_PATH
    out: dict = {}
    db = DB_PATH                       # тот же путь, что мерит сам VACUUM
    data = db.parent
    free = shutil.disk_usage(data).free
    size = db.stat().st_size if db.exists() else 0
    need = size * DISK_NEED_RATIO
    if size and free < need:
        out["space"] = (f"свободно {free / 1e9:.1f} ГБ при базе {size / 1e9:.1f} ГБ — "
                        f"на VACUUM и бэкап нужно ~{need / 1e9:.1f} ГБ; "
                        f"база перестанет ужиматься и копироваться")

    backups = sorted((data / "backups").glob("portfolio-*.db.gz"),
                     key=lambda f: f.stat().st_mtime, reverse=True)
    if not backups:
        out["backup"] = "резервных копий базы нет вовсе"
    else:
        age_h = (time.time() - backups[0].stat().st_mtime) / 3600
        if age_h > BACKUP_STALE_HOURS:
            out["backup"] = (f"последняя копия {age_h / 24:.1f} сут назад "
                             f"({backups[0].name}) — крон бэкапа не отработал")
    return out


async def _disk_alert(problems: dict) -> None:
    await _watch_alert(
        _disk_alerted, problems, "💽 <b>Диск и копии</b>",
        "<i>Тиковый архив и история спредов есть только здесь — "
        "восстановить их неоткуда.</i>",
        "✅ <b>С местом и копиями снова порядок</b>")


async def disk_watchdog(period_sec: int = DISK_WATCH_PERIOD_SEC):
    """Сторож места на диске и свежести резервных копий.

    Отказ тут не громкий, а тихий: и VACUUM (services/trades_archive.vacuum), и
    штатный бэкап заранее проверяют место и при нехватке ОТКАЗЫВАЮТСЯ — это
    правильно, но заметить отказ можно только в логе, куда никто не смотрит.
    Тем временем база растёт, место не возвращается, копии не снимаются."""
    await asyncio.sleep(120)
    while True:
        try:
            problems = await run_bg(_disk_problems)
            for msg in problems.values():
                logger.warning("ДИСК: %s", msg)
            await _disk_alert(problems)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("disk watchdog error: %s", e)
        await asyncio.sleep(period_sec)


ARCHIVE_VACUUM_MIN_ROWS = int(os.getenv("ARCHIVE_VACUUM_MIN_ROWS", "200000"))


async def archive_maintenance():
    """Ночное обслуживание тикового архива: ретеншен + возврат места ОС.

    Каждый день в 03:30 МСК (биржа закрыта, демон баров спит между часами).
    Прун оставляет за сырым окном только крупные принты — см. ретеншен в
    services/trades_archive. VACUUM зовём лишь после заметного удаления: он
    переписывает файл целиком и требует свободного места в размер БД."""
    from services import trades_archive as ta
    while True:
        now = datetime.now(_MSK)
        target = now.replace(hour=3, minute=30, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep(max(1.0, (target - now).total_seconds()))
        try:
            res = await run_bg(ta.prune)
            logger.info("tick archive prune: %s", res)
            if res.get("deleted", 0) >= ARCHIVE_VACUUM_MIN_ROWS:
                vac = await run_bg(ta.vacuum)
                logger.info("tick archive vacuum: %s", vac)
                if vac.get("skipped"):
                    # место кончилось ровно тогда, когда его надо возвращать —
                    # молча пропустить значит дать базе расти дальше
                    await _disk_alert({"vacuum": f"VACUUM пропущен: {vac['skipped']} "
                                                 f"(свободно {vac.get('free_mb')} МБ "
                                                 f"при базе {vac.get('before_mb')} МБ)"})
            # статистика планировщика: таблицы растут каждый день, и по стухшей
            # он выбирает индекс, из-за которого лента читает лишние сотни тысяч
            # строк (см. services/tape)
            logger.info("archive analyze: %s", await run_bg(ta.analyze))
            logger.info("tick archive: %s", await run_bg(ta.db_stats))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"archive maintenance error: {e}")


DEPTH_POLL_INTERVAL = int(os.getenv("DEPTH_POLL_INTERVAL", "120"))   # снимок стаканов, сек


async def depth_poller():
    """Батч-снимок стаканов по всему юниверсу раз в DEPTH_POLL_INTERVAL (торговые
    часы) → market_cache['depth']. Питает фильтр по объёму в таблице: VWAP на
    тикет считается по лестнице, а не по верху стакана. Вне торговых часов не
    крутим — книга не меняется, а снимок и так помечен ts."""
    from services import depth as depth_svc
    await asyncio.sleep(60)   # пропускаем стартовый прогрев (цены/метрики важнее)
    while True:
        try:
            if _in_moex_trading_hours():
                uni = await instruments_registry.fetch_floater_universe()
                isins = [u["isin"] for u in uni if u.get("isin")]
                # стаканы льются push'ем (depth-пул universe_stream) — HTTP-батч
                # не нужен; поллер остаётся фолбэком на случай падения стрима
                from services.universe_stream import depth_stream_covers
                if depth_stream_covers(len(isins)):
                    pass
                else:
                    n = await depth_svc.refresh_depth(isins, chunk=UNIVERSE_POLL_CHUNK)
                    logger.info("depth snapshot (фолбэк): %d/%d стаканов", n, len(isins))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"depth poller error: {e}")
        await asyncio.sleep(DEPTH_POLL_INTERVAL)


# Дефолтный пул (asyncio.to_thread) обслуживает ТОЛЬКО запросы API — чтения
# SQLite под ручки. Питоновский дефолт — min(32, cpu+4), на двухъядерном VPS это
# 6 воркеров на всё приложение; фоновый I/O демонов уведён в свой пул
# (services/pools.run_bg), а этот подняли: работа тут ждёт диск, а не считает,
# и GIL SQLite отпускает.
API_POOL_WORKERS = int(os.getenv("API_POOL_WORKERS", "12"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=API_POOL_WORKERS, thread_name_prefix="api"))
    from services.pools import BG_WORKERS
    logger.info("пулы потоков: api=%d, bg=%d, heavy=1", API_POOL_WORKERS, BG_WORKERS)

    from services.portfolio_db import init_db
    init_db()  # схема spread_daily/bar_hourly/trade_tick (идемпотентно)

    async def _seed_tick_watermarks():
        """Знак дрейна для архива, накопленного до инкрементального режима.
        В фоне: GROUP BY по миллионам тиков не должен держать старт сервера."""
        try:
            from services import trades_archive as ta
            n = await run_bg(ta.seed_watermarks)
            if n:
                logger.info("tick drain watermarks seeded: %d", n)
        except Exception as e:
            logger.warning(f"watermark seed error: {e}")

    seed = asyncio.create_task(_seed_tick_watermarks())
    warm = asyncio.create_task(warmup_caches())
    task = asyncio.create_task(ws_market_data_broadcaster())
    poller = asyncio.create_task(universe_price_poller())
    prewarm = asyncio.create_task(daily_prewarm())
    from services.alor_ws import alor_orderbook_ws
    alor_ws = asyncio.create_task(alor_orderbook_ws())
    spread_snap = asyncio.create_task(spread_snapshotter())
    bars_worker = asyncio.create_task(hourly_bars_worker())
    night_warm = asyncio.create_task(nightly_spread_warm())
    night_roll = asyncio.create_task(nightly_daily_rollup())
    mem_watch = asyncio.create_task(memory_watch())
    depth_task = asyncio.create_task(depth_poller())
    archive_task = asyncio.create_task(archive_maintenance())
    blocks_task = asyncio.create_task(block_trades_worker())
    quotes_task = asyncio.create_task(quotes_poller())
    from services.universe_stream import universe_stream_pool, metrics_worker
    from services.trades_stream import trades_stream_pool
    pool_task = asyncio.create_task(universe_stream_pool())
    # безадресные сделки юниверса пушем: ISS-лента (block_trades) отстаёт на 15
    # минут, у Alor задержки нет — см. services/trades_stream
    tape_task = asyncio.create_task(trades_stream_pool())
    engine_task = asyncio.create_task(metrics_worker())
    lag_task = asyncio.create_task(loop_lag_watchdog())
    # тихий отказ стримов выглядит как «рынок спокоен» — сторож делает его громким
    stream_wd_task = asyncio.create_task(stream_watchdog())
    # место на диске и свежесть копий: оба отказа тихие, см. disk_watchdog
    disk_wd_task = asyncio.create_task(disk_watchdog())
    from services.tg_notify import tg_signal_worker
    from services.tg_poll import tg_poll_worker
    tg_sig_task = asyncio.create_task(tg_signal_worker())
    # команды бота: на этом VPS Telegram до нас не достучится (вебхук молчит),
    # поэтому апдейты забираем сами — см. services/tg_poll.py
    tg_poll_task = asyncio.create_task(tg_poll_worker())
    signals_task = asyncio.create_task(signals_worker())
    yield
    tg_sig_task.cancel()
    stream_wd_task.cancel()
    disk_wd_task.cancel()
    tg_poll_task.cancel()
    signals_task.cancel()
    quotes_task.cancel()
    pool_task.cancel()
    tape_task.cancel()
    engine_task.cancel()
    lag_task.cancel()
    seed.cancel()
    warm.cancel()
    task.cancel()
    poller.cancel()
    prewarm.cancel()
    alor_ws.cancel()
    spread_snap.cancel()
    bars_worker.cancel()
    night_warm.cancel()
    night_roll.cancel()
    mem_watch.cancel()
    depth_task.cancel()
    archive_task.cancel()
    blocks_task.cancel()
    from services import telegram as _tg
    await _tg.aclose()          # keepalive-пул Bot API живёт между вызовами

app = FastAPI(
    title="Shabshab Floaters API",
    version="1.1.2",
    description="API for fetching floater bond analytics, cashflows, and market data.",
    lifespan=lifespan
)

# Медленные запросы — в лог. Порог низкий намеренно: интерактивная ручка,
# отвечающая секунду, уже «подвисает» на глаз, а разбирать эпизод постфактум
# можно только по следу. Рядом в том же файле лежат записи сторожа лага
# (loop_lag_watchdog) — вместе они отделяют «ручка сама медленная» от
# «ядро/пул были заняты чем-то другим».
SLOW_REQUEST_SEC = float(os.getenv("SLOW_REQUEST_SEC", "1.0"))


@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    t0 = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        logger.warning("запрос %s %s упал за %.2fс", request.method,
                       request.url.path, time.monotonic() - t0)
        raise
    dt = time.monotonic() - t0
    if dt >= SLOW_REQUEST_SEC:
        logger.warning("медленный запрос %.2fс: %s %s%s → %s", dt, request.method,
                       request.url.path,
                       f"?{request.url.query}" if request.url.query else "",
                       response.status_code)
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # cookies/credentials не используем; "*" + credentials невалидно по спеке CORS
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(APIException)
async def api_exception_handler(request: Request, exc: APIException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details
            }
        },
    )

# health и auth открыты; всё остальное закрыто зависимостью require_user (401 без сессии).
app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api/auth")
_gate = [Depends(require_user)]
app.include_router(meta.router, prefix="/api", dependencies=_gate)
app.include_router(bonds.router, prefix="/api/bonds", dependencies=_gate)
app.include_router(curves.router, prefix="/api/curves", dependencies=_gate)
app.include_router(orderbook.router, prefix="/api/orderbook", dependencies=_gate)
app.include_router(instruments.router, prefix="/api/instruments", dependencies=_gate)
app.include_router(fixed.router, prefix="/api/fixed", dependencies=_gate)
app.include_router(status.router, prefix="/api/status", dependencies=_gate)
app.include_router(signals_route.router, prefix="/api/signals", dependencies=_gate)
app.include_router(history.router, prefix="/api/history", dependencies=_gate)
app.include_router(trades.router, prefix="/api/trades", dependencies=_gate)
app.include_router(blocks.router, prefix="/api/blocks", dependencies=_gate)
app.include_router(calc.router, prefix="/api/calc", dependencies=_gate)
app.include_router(ws.router, prefix="/api/ws")  # WS проверяет cookie внутри хендлера
app.include_router(tg.router, prefix="/api/tg")  # webhook защищён secret-заголовком

# --- Frontend (static dashboard) ---
# Приоритет: React-билд (frontend-react/dist), фоллбэк — старый vanilla frontend/.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REACT_DIST = os.path.join(_ROOT, "frontend-react", "dist")
_FRONTEND_DIR = _REACT_DIST if os.path.isdir(_REACT_DIST) else os.path.join(_ROOT, "frontend")


class _SPAStaticFiles(StaticFiles):
    """SPA-fallback: неизвестные пути под /app (react-router: /app/funds/X5,
    /app/curves/kspath) отдают index.html — hard reload/deep-link больше не 404.
    Пути с расширением (ассеты) честно 404-ятся (протухший хеш бандла не должен
    маскироваться под HTML)."""
    async def get_response(self, path: str, scope):
        from starlette.exceptions import HTTPException as _StarletteHTTPException
        try:
            resp = await super().get_response(path, scope)
        except _StarletteHTTPException as e:
            # StaticFiles на отсутствующий файл КИДАЕТ 404, а не возвращает response
            if e.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
                return await super().get_response("index.html", scope)
            raise
        if resp.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
            return await super().get_response("index.html", scope)
        return resp


if os.path.isdir(_FRONTEND_DIR):
    app.mount("/app", _SPAStaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")

    @app.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(url="/app/")

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="127.0.0.1", port=8000, reload=True)
