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
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import health, meta, bonds, curves, orderbook, ws, auth, instruments, fixed, status, alerts, history, trades, blocks, calc, tg, signals as signals_route
from api.routes.auth import require_user
from fastapi import Depends
from services.exceptions import APIException
from contextlib import asynccontextmanager
import asyncio
from datetime import date, datetime, timedelta, timezone
from services.market_data import MarketDataService
from services import instruments_registry

WS_PUSH_INTERVAL = 5        # такт пуша цен в торговые часы, сек
WS_IDLE_INTERVAL = 60       # вне торговых часов — только перепроверка календаря
WS_PRICE_HEARTBEAT = 60     # пуш неизменной цены не чаще раза в столько секунд


async def ws_market_data_broadcaster():
    """Пуш last-price подписчикам WS.

    Только ISIN с ЖИВЫМИ подписчиками (active_market_isins) — раньше брались все
    ключи карты, а опустевшие не удалялись: за аптайм список рос монотонно и
    каждые 5с уходил в Alor запросом цен по бумагам, которые никто не смотрит.
    Вне торговых часов не опрашиваем вовсе — цена не меняется (тот же гейт, что
    у universe_price_poller / depth_poller / alerts_monitor).

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
                    for _i in _miss:
                        if _i in _emap:
                            _eid, _enm = _emap[_i]
                            _reg.set_emitter(_i, _eid, _enm)
                        else:
                            # нерезолвимая (нет EMITTER_ID: делистинг/ОФЗ) — sentinel 0,
                            # чтобы ушла из missing и не крутила drain вечно
                            _reg.set_emitter(_i, 0, None)
                    _filled += len(_emap)
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
        await asyncio.to_thread(cbr.ks_history)      # триггерит _refresh (сеть)
        progress.advance("warmup", detail="история RUONIA", force=True)
        await asyncio.to_thread(cbr.ruonia_history)
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
            logger.info("daily 09:00 prewarm: готово (расписаний %d)",
                        len(MarketDataService._full_mem))
        except Exception as e:
            logger.warning(f"daily prewarm error: {e}")


ALERT_POLL_INTERVAL = 12   # проверка алертов против стакана, сек


def _ob_levels(raw, metrics_fn):
    """[{price, volume}] Alor → [{price, qty, yield_pct, dm_bps, g_spread_bps}]."""
    out = []
    for e in raw:
        p, q = e.get("price"), e.get("volume")
        if p is None:
            continue
        lv = {"price": p, "qty": q, "yield_pct": None, "dm_bps": None, "g_spread_bps": None}
        if metrics_fn:
            try:
                m = metrics_fn(p)
                lv.update(yield_pct=m.get("yield_pct"), dm_bps=m.get("dm_bps"),
                          y_idx_bps=m.get("y_idx_bps"), g_spread_bps=m.get("g_spread_bps"))
            except Exception:
                pass
        out.append(lv)
    return out


async def alerts_monitor():
    """Фон: активные алерты против Alor-стакана. При выполнении условия (метрика
    op порог + накопленный объём «на уровне/лучше») переводит active→fired.
    Батчит по (isin, kind): один снапшот + один reprice-контекст на выпуск."""
    from services import alerts as alerts_svc
    from services.market_data import market_cache
    from services.orderbook_svc import build_metrics_fn
    from api.routes.orderbook import fetch_alor_orderbook_snapshot
    OB_LIVE_FRESH = 15   # свежесть пуша пула, сек: старше — фолбэк на HTTP
    await asyncio.sleep(45)
    while True:
        try:
            if _in_moex_trading_hours():
                active = alerts_svc.active_all()
                groups: dict = {}
                for a in active:
                    groups.setdefault((a["isin"], a.get("kind") or "floater"), []).append(a)
                # алертные бумаги — в пул подписок alor_ws: стакан по ним течёт
                # push'ем, и HTTP-снапшот ниже нужен только пока подписка
                # раскачивается (или WS лежит)
                market_cache["alert_isins"] = {isin for isin, _k in groups.keys()}
                for (isin, kind), grp in groups.items():
                    try:
                        live = (market_cache.get("ob_live") or {}).get(isin)
                        if live and time.time() - live["ts"] < OB_LIVE_FRESH:
                            snap = live
                        else:
                            snap = await fetch_alor_orderbook_snapshot(isin, 30)
                        if not snap:
                            continue
                        metrics_fn, face = None, None
                        if any(a["metric"] != "price" for a in grp):
                            try:
                                metrics_fn, _cd, face = await build_metrics_fn(isin, kind)
                            except Exception:
                                metrics_fn = None
                        asks_raw = sorted((e for e in snap.get("asks", []) if e.get("price") is not None),
                                          key=lambda e: e["price"])
                        bids_raw = sorted((e for e in snap.get("bids", []) if e.get("price") is not None),
                                          key=lambda e: e["price"], reverse=True)
                        asks = _ob_levels(asks_raw, metrics_fn)
                        bids = _ob_levels(bids_raw, metrics_fn)
                        for a in grp:
                            levels = asks if a["side"] == "buy" else bids
                            hit = alerts_svc.evaluate(a, levels, face)
                            if hit:
                                alerts_svc.mark_fired(a["id"], hit["price"], hit["volume"])
                                logger.info("alert fired id=%s %s %s %s%s%s vol=%s",
                                            a["id"], isin, a["side"], a["metric"],
                                            a["op"], a["threshold"], hit["volume"])
                                # Telegram: стакан уже на руках — рендер и HTTP
                                # уходят в очередь, монитор не ждёт
                                from services import tg_notify
                                tg_notify.enqueue(a, bids, asks, face, hit)
                    except Exception as e:
                        logger.warning(f"alert monitor {isin} error: {e}")
            await asyncio.sleep(ALERT_POLL_INTERVAL)
        except Exception as e:
            logger.warning(f"alerts_monitor loop error: {e}")
            await asyncio.sleep(30)


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


TG_SCREENER_INTERVAL = int(os.getenv("TG_SCREENER_INTERVAL", "180"))


async def tg_screener_worker():
    """Фон: скринер-фильтры Telegram-бота против снапшота метрик универса.
    Вся логика в services.tg_screener.run_cycle; тут только такт и торговые часы."""
    from services import tg_screener
    await asyncio.sleep(90)     # ждём прогрева движка метрик
    while True:
        try:
            if _in_moex_trading_hours():
                sent = await tg_screener.run_cycle()
                if sent:
                    logger.info("tg screener: %d сообщений", sent)
        except Exception as e:
            logger.warning(f"tg_screener_worker error: {e}")
        await asyncio.sleep(TG_SCREENER_INTERVAL)


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
                await asyncio.to_thread(write_snapshot)
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
            await asyncio.to_thread(write_snapshot)
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
    seeded = False
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
                if not seeded:
                    # знак уведомлений ставим ПОСЛЕ первого прохода: иначе
                    # холодный старт вывалил бы в колокольчик всю сессию разом
                    seeded = True
                    await asyncio.to_thread(bt.seed_alert_mark)
                else:
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
                                await asyncio.to_thread(bt.prune))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"block trades worker error: {e}")
        # вне торгов такт редкий, но пока есть неоценённый хвост — держим
        # рабочий темп, иначе вечерний наплыв досчитывался бы часами
        idle = 60 if await asyncio.to_thread(bt.unpriced_count) else 600
        await asyncio.sleep(BLOCK_POLL_INTERVAL if _in_moex_trading_hours() else idle)


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
            res = await asyncio.to_thread(ta.prune)
            logger.info("tick archive prune: %s", res)
            if res.get("deleted", 0) >= ARCHIVE_VACUUM_MIN_ROWS:
                vac = await asyncio.to_thread(ta.vacuum)
                logger.info("tick archive vacuum: %s", vac)
            logger.info("tick archive: %s", await asyncio.to_thread(ta.db_stats))
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    from services.portfolio_db import init_db
    init_db()  # схема alerts/spread_daily/bar_hourly/trade_tick (идемпотентно)

    async def _seed_tick_watermarks():
        """Знак дрейна для архива, накопленного до инкрементального режима.
        В фоне: GROUP BY по миллионам тиков не должен держать старт сервера."""
        try:
            from services import trades_archive as ta
            n = await asyncio.to_thread(ta.seed_watermarks)
            if n:
                logger.info("tick drain watermarks seeded: %d", n)
        except Exception as e:
            logger.warning(f"watermark seed error: {e}")

    seed = asyncio.create_task(_seed_tick_watermarks())
    warm = asyncio.create_task(warmup_caches())
    task = asyncio.create_task(ws_market_data_broadcaster())
    poller = asyncio.create_task(universe_price_poller())
    prewarm = asyncio.create_task(daily_prewarm())
    alert_mon = asyncio.create_task(alerts_monitor())
    from services.alor_ws import alor_orderbook_ws
    alor_ws = asyncio.create_task(alor_orderbook_ws())
    spread_snap = asyncio.create_task(spread_snapshotter())
    bars_worker = asyncio.create_task(hourly_bars_worker())
    depth_task = asyncio.create_task(depth_poller())
    archive_task = asyncio.create_task(archive_maintenance())
    blocks_task = asyncio.create_task(block_trades_worker())
    quotes_task = asyncio.create_task(quotes_poller())
    from services.universe_stream import universe_stream_pool, metrics_worker
    pool_task = asyncio.create_task(universe_stream_pool())
    engine_task = asyncio.create_task(metrics_worker())
    lag_task = asyncio.create_task(loop_lag_watchdog())
    from services.tg_notify import tg_notify_worker
    tg_task = asyncio.create_task(tg_notify_worker())
    tg_scr_task = asyncio.create_task(tg_screener_worker())
    signals_task = asyncio.create_task(signals_worker())
    yield
    tg_task.cancel()
    tg_scr_task.cancel()
    signals_task.cancel()
    quotes_task.cancel()
    pool_task.cancel()
    engine_task.cancel()
    lag_task.cancel()
    seed.cancel()
    warm.cancel()
    task.cancel()
    poller.cancel()
    prewarm.cancel()
    alert_mon.cancel()
    alor_ws.cancel()
    spread_snap.cancel()
    bars_worker.cancel()
    depth_task.cancel()
    archive_task.cancel()
    blocks_task.cancel()

app = FastAPI(
    title="Shabshab Floaters API",
    version="1.1.2",
    description="API for fetching floater bond analytics, cashflows, and market data.",
    lifespan=lifespan
)

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
app.include_router(alerts.router, prefix="/api/alerts", dependencies=_gate)
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
