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
    # возраст-алерт: rates_date — дата котировок СПФИ; > 1 торгового дня назад = stale
    # (T-1 норма: клиринг вчерашний; допускаем до 4 дней на выходные+праздник)
    elif (date.today() - rates_date).days > 4:
        warnings.append(f"Rates quotes are {(date.today() - rates_date).days} days old (Cbonds stale fallback?).")

    # дрейф наших метрик vs НРД (наполняет universe_price_poller)
    from services.market_data import market_cache
    drift = market_cache.get("nrd_drift")
    if drift:
        warnings.extend(drift.get("alerts", []))

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
        warnings=warnings,
        nrd_drift=drift,
    )
