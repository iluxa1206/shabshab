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

from api.routes import health, meta, bonds, curves, orderbook, ws, auth, funds, instruments
from api.routes.auth import require_user
from fastapi import Depends
from services.exceptions import APIException
from contextlib import asynccontextmanager
import asyncio
from datetime import date, datetime, timedelta, timezone
from services.market_data import MarketDataService
from services import nrd as nrd_service

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

async def universe_price_poller():
    """Раз в UNIVERSE_POLL_INTERVAL: (1) тянет last-price Alor по всему юниверсу
    чанками → market_cache['last_prices']; (2) считает полные метрики (dirty/DM/
    z_model/carry/next_coupon) по всему юниверсу → market_cache['universe_metrics'].
    Юниверс-роут читает эти кэши — бумаги вне watchlist получают live-цену И расчёт.
    Данные MOEX кэшируются на день, поэтому тяжёлый прогрев (bondization) — раз/день."""
    from services.universe import compute_universe_metrics
    from services.market_data import market_cache
    from services import history
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
                    for _i, (_eid, _enm) in _emap.items():
                        _reg.set_emitter(_i, _eid, _enm)
                    _filled += len(_emap)
                    if len(_emap) < len(_miss):   # часть не резолвится — не крутимся
                        break
                    await asyncio.sleep(0.5)      # мягкий rate-limit между батчами
                if _filled:
                    logger.info(f"emitter backfill: +{_filled}")
            except Exception as e:
                logger.warning(f"emitter backfill error: {e}")
            if _in_moex_trading_hours():
                uni = await nrd_service.fetch_floater_universe()  # реестр / НРД
                isins = [u["isin"] for u in uni if u.get("isin")]
                # дневной срез истории НРД-метрик — здесь, а не по первому запросу
                # дашборда (раньше история писалась, только если кто-то зашёл)
                try:
                    history.record_snapshot(uni)
                except Exception as e:
                    logger.warning(f"history snapshot error: {e}")
                for i in range(0, len(isins), UNIVERSE_POLL_CHUNK):
                    await MarketDataService.fetch_last_prices(isins[i:i + UNIVERSE_POLL_CHUNK])
                    await asyncio.sleep(1)  # мягкий rate-limit между чанками
                # полные метрики после наполнения цен
                metrics = await compute_universe_metrics(uni, isins, _ISINS_CACHE)
                if metrics:
                    market_cache["universe_metrics"] = metrics
                    # дрейф наших SM/DM/z против публикаций НРД (алерт в лог,
                    # срез в /meta) — ловим молчаливую смену методики НРД
                    from services.drift import compute_nrd_drift
                    market_cache["nrd_drift"] = compute_nrd_drift(uni, metrics)
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
        from services import cbr, history
        from services.market_data import MarketDataService, market_cache
        import services.nrd as nrd_service
        from services.universe import compute_universe_metrics
        await asyncio.to_thread(cbr.ks_history)      # триггерит _refresh (сеть)
        await asyncio.to_thread(cbr.ruonia_history)
        await MarketDataService.get_curves()          # bootstrap RUONIA/KEYRATE
        await MarketDataService.get_zspread_ctx()      # ExpCurve + g-curve
        # Метрики юниверса (dm/z/carry) — сразу из НРД-цен, НЕ дожидаясь медленного
        # прогрева live-цен Alor поллером (30с сон + чанки по 4с WS-таймаута = ~60с
        # пустых метрик после рестарта). Поллер потом уточнит их live-ценами.
        if not market_cache.get("universe_metrics"):
            uni = await nrd_service.fetch_floater_universe()
            isins = [u["isin"] for u in uni if u.get("isin")]
            if uni:
                try:
                    history.record_snapshot(uni)
                except Exception as e:
                    logger.warning(f"history snapshot error: {e}")
                m = await compute_universe_metrics(uni, isins, _ISINS_CACHE)
                if m:
                    market_cache["universe_metrics"] = m
    except Exception as e:
        logger.warning(f"warmup error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from services.portfolio_db import init_db
    init_db()  # схема + сид фондов R5/D5/Y5 (идемпотентно)
    warm = asyncio.create_task(warmup_caches())
    task = asyncio.create_task(ws_market_data_broadcaster())
    poller = asyncio.create_task(universe_price_poller())
    nav_snap = asyncio.create_task(fund_nav_snapshotter())
    yield
    warm.cancel()
    task.cancel()
    poller.cancel()
    nav_snap.cancel()

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
