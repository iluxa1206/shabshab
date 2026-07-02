import os
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import health, meta, bonds, curves, orderbook, ws, auth, funds
from api.routes.auth import require_user
from fastapi import Depends
from services.exceptions import APIException
from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, timedelta, timezone
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
            print(f"WS Broadcaster error: {e}")

        await asyncio.sleep(5)  # Fetch and broadcast every 5 seconds


# Опрос Alor по всему юниверсу флоатеров (вне watchlist) — редко, чтобы держать
# колонку PRICE более-менее актуальной без нагрузки WS на 453 бумаги.
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
    from api.routes.bonds import compute_universe_metrics
    from services.market_data import market_cache
    await asyncio.sleep(30)  # прогрев: не конкурировать со стартом
    while True:
        try:
            if _in_moex_trading_hours():
                uni = await nrd_service.fetch_floater_universe()  # кэш на день
                isins = [u["isin"] for u in uni if u.get("isin")]
                for i in range(0, len(isins), UNIVERSE_POLL_CHUNK):
                    await MarketDataService.fetch_last_prices(isins[i:i + UNIVERSE_POLL_CHUNK])
                    await asyncio.sleep(1)  # мягкий rate-limit между чанками
                # полные метрики после наполнения цен
                metrics = await compute_universe_metrics(uni, isins)
                if metrics:
                    market_cache["universe_metrics"] = metrics
        except Exception as e:
            print(f"Universe poller error: {e}")
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
            print(f"NAV snapshotter error: {e}")
        await asyncio.sleep(3600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    from services.portfolio_db import init_db
    init_db()  # схема + сид фондов R5/D5/Y5 (идемпотентно)
    task = asyncio.create_task(ws_market_data_broadcaster())
    poller = asyncio.create_task(universe_price_poller())
    nav_snap = asyncio.create_task(fund_nav_snapshotter())
    yield
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
app.include_router(ws.router, prefix="/api/ws")  # WS проверяет cookie внутри хендлера

# --- Frontend (static dashboard) ---
# Приоритет: React-билд (frontend-react/dist), фоллбэк — старый vanilla frontend/.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REACT_DIST = os.path.join(_ROOT, "frontend-react", "dist")
_FRONTEND_DIR = _REACT_DIST if os.path.isdir(_REACT_DIST) else os.path.join(_ROOT, "frontend")
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/app", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")

    @app.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(url="/app/")

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="127.0.0.1", port=8000, reload=True)
