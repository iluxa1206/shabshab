import os
import logging
import uvicorn
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

from api.routes import health, meta, bonds, curves, orderbook, ws, auth, funds, instruments, fixed, status, alerts, history
from api.routes.auth import require_user
from fastapi import Depends
from services.exceptions import APIException
from contextlib import asynccontextmanager
import asyncio
from datetime import date, datetime, timedelta, timezone
from services.market_data import MarketDataService
from services import instruments_registry

async def ws_market_data_broadcaster():
    """Background task to push updates to connected WS clients."""
    while True:
        try:
            # Only fetch for ISINs that actually have active market subscriptions
            active_isins = list(ws.manager.market_subscriptions.keys())
            if active_isins:
                prices = await MarketDataService.fetch_last_prices(active_isins)
                for isin in active_isins:
                    if isin in prices:
                        payload = {"last_price_pct": prices[isin]}
                        await ws.manager.broadcast_market_data(isin, payload)
        except Exception as e:
            logger.warning(f"WS Broadcaster error: {e}")

        await asyncio.sleep(5)  # Fetch and broadcast every 5 seconds


# Опрос Alor по всему юниверсу флоатеров (вне watchlist) — редко, чтобы держать
# колонку PRICE более-менее актуальной без нагрузки WS на 453 бумаги.
_ISINS_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "isins_cache.json")
UNIVERSE_POLL_INTERVAL = 600      # 10 минут
UNIVERSE_POLL_CHUNK = 150         # ISIN за один WS-заход (меньше Alor WS-сессий: ~3 вместо 9)
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
            if _in_moex_trading_hours():
                uni = await instruments_registry.fetch_floater_universe()
                isins = [u["isin"] for u in uni if u.get("isin")]
                for i in range(0, len(isins), UNIVERSE_POLL_CHUNK):
                    await MarketDataService.fetch_last_prices(isins[i:i + UNIVERSE_POLL_CHUNK])
                    await asyncio.sleep(1)  # мягкий rate-limit между чанками
                # полные метрики после наполнения цен
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

async def fund_nav_snapshotter():
    """Раз в час пишет дневной снапшот метрик фондов в nav_daily (перезапись за
    сегодня идемпотентна) — история NAV для графиков паёв/бенчмарков (Ф2).
    Стартует после прогрева universe poller'а, чтобы флоатерные метрики уже были."""
    from services.portfolio import snapshot_all_navs
    await asyncio.sleep(900)
    while True:
        try:
            await snapshot_all_navs()
        except Exception as e:
            logger.warning(f"NAV snapshotter error: {e}")
        await asyncio.sleep(3600)

async def warmup_caches():
    """Прогрев дорогих на ХОЛОДНУЮ кэшей сразу при старте, чтобы ПЕРВЫЙ запрос
    пользователя не платил их латентность (после каждого деплоя контейнер холодный).
    Тяжёлое: cbr._refresh (~1.2с — 2 сетевых запроса к cbr.ru за историей КС/RUONIA)
    и bootstrap кривых. Идёт конкурентно, старт сервера не блокирует; поллер
    отдельно (он спит 30с и греет ещё и цены Alor + метрики юниверса)."""
    try:
        from services import cbr
        from services.market_data import MarketDataService, market_cache
        from services.universe import compute_universe_metrics
        await asyncio.to_thread(cbr.ks_history)      # триггерит _refresh (сеть)
        await asyncio.to_thread(cbr.ruonia_history)
        await MarketDataService.get_curves()          # bootstrap RUONIA/KEYRATE
        await MarketDataService.get_zspread_ctx()      # ExpCurve + g-curve
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
        await _warm_fixed(market_cache)
    except Exception as e:
        logger.warning(f"warmup error: {e}")


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
                          g_spread_bps=m.get("g_spread_bps"))
            except Exception:
                pass
        out.append(lv)
    return out


async def alerts_monitor():
    """Фон: активные алерты против Alor-стакана. При выполнении условия (метрика
    op порог + накопленный объём «на уровне/лучше») переводит active→fired.
    Батчит по (isin, kind): один снапшот + один reprice-контекст на выпуск."""
    from services import alerts as alerts_svc
    from services.orderbook_svc import build_metrics_fn
    from api.routes.orderbook import fetch_alor_orderbook_snapshot
    await asyncio.sleep(45)
    while True:
        try:
            if _in_moex_trading_hours():
                active = alerts_svc.active_all()
                groups: dict = {}
                for a in active:
                    groups.setdefault((a["isin"], a.get("kind") or "floater"), []).append(a)
                for (isin, kind), grp in groups.items():
                    try:
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
                    except Exception as e:
                        logger.warning(f"alert monitor {isin} error: {e}")
            await asyncio.sleep(ALERT_POLL_INTERVAL)
        except Exception as e:
            logger.warning(f"alerts_monitor loop error: {e}")
            await asyncio.sleep(30)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    from services.portfolio_db import init_db
    init_db()  # схема + сид фондов R5/D5/Y5 (идемпотентно)
    warm = asyncio.create_task(warmup_caches())
    task = asyncio.create_task(ws_market_data_broadcaster())
    poller = asyncio.create_task(universe_price_poller())
    nav_snap = asyncio.create_task(fund_nav_snapshotter())
    prewarm = asyncio.create_task(daily_prewarm())
    alert_mon = asyncio.create_task(alerts_monitor())
    from services.alor_ws import alor_orderbook_ws
    alor_ws = asyncio.create_task(alor_orderbook_ws())
    spread_snap = asyncio.create_task(spread_snapshotter())
    yield
    warm.cancel()
    task.cancel()
    poller.cancel()
    nav_snap.cancel()
    prewarm.cancel()
    alert_mon.cancel()
    alor_ws.cancel()
    spread_snap.cancel()

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
app.include_router(funds.router, prefix="/api/funds", dependencies=_gate)
app.include_router(instruments.router, prefix="/api/instruments", dependencies=_gate)
app.include_router(fixed.router, prefix="/api/fixed", dependencies=_gate)
app.include_router(status.router, prefix="/api/status", dependencies=_gate)
app.include_router(alerts.router, prefix="/api/alerts", dependencies=_gate)
app.include_router(history.router, prefix="/api/history", dependencies=_gate)
app.include_router(ws.router, prefix="/api/ws")  # WS проверяет cookie внутри хендлера

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
