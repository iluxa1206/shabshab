import os
from fastapi import APIRouter
from datetime import date
from api.schemas import MetaResponse
from services.market_data import MarketDataService
from services import nrd as nrd_service
from auth import REFRESH_TOKEN

router = APIRouter()

@router.get("/meta", response_model=MetaResponse, tags=["System"])
async def get_meta():
    ruonia, keyrate, calc_date, rates_date = await MarketDataService.get_curves()

    warnings = []
    if calc_date is None or rates_date is None:
        warnings.append("Market curves data is unavailable or stale.")

    # статус источников: настроен ли доступ / загружены ли данные
    source_status = {
        "alor": bool(REFRESH_TOKEN),                 # ценами живёт WS; здесь — есть ли креды
        "cbonds": bool(ruonia and keyrate),          # кривые ставок построены
        "nrd": nrd_service.is_configured(),          # НРД ценовой центр подключён
    }

    return MetaResponse(
        calc_date=calc_date or date.today(),
        rates_date=rates_date or date.today(),
        sources={
            "prices": "Alor WebSocket",
            "rates": "Cbonds (OIS RUONIA, IRS KEYRATE)",
            "reference": "MOEX, floaters.ru, Excel Cache"
        },
        source_status=source_status,
        warnings=warnings
    )
