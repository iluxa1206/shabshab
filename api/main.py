import os
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import health, meta, bonds, curves, orderbook, ws
from services.exceptions import APIException
from contextlib import asynccontextmanager
import asyncio
from services.market_data import MarketDataService

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(ws_market_data_broadcaster())
    yield
    task.cancel()

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

app.include_router(health.router, prefix="/api")
app.include_router(meta.router, prefix="/api")
app.include_router(bonds.router, prefix="/api/bonds")
app.include_router(curves.router, prefix="/api/curves")
app.include_router(orderbook.router, prefix="/api/orderbook")
app.include_router(ws.router, prefix="/api/ws")

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
