import os
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import date
from api.schemas import MetaResponse
from services.market_data import MarketDataService
from services import nrd as nrd_service
from services import nrd_config, instruments_registry
from api.routes.auth import require_admin
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

    # свежесть истории ЦБ (КС/RUONIA) — раньше выводилась только в логи, не в UI
    from services import cbr
    def _last(hist):
        return hist[-1][0] if hist else None
    try:
        ks_last, ruo_last = await asyncio.gather(
            asyncio.to_thread(lambda: _last(cbr.ks_history())),
            asyncio.to_thread(lambda: _last(cbr.ruonia_history())),
        )
        for lbl, d in (("КС", ks_last), ("RUONIA", ruo_last)):
            if d and (date.today() - d).days > 4:
                warnings.append(f"История {lbl} ЦБ отстаёт на {(date.today() - d).days} дн "
                                f"(последняя {d.isoformat()}).")
    except Exception:
        pass

    # дрейф наших метрик vs НРД (наполняет universe_price_poller)
    from services.market_data import market_cache
    drift = market_cache.get("nrd_drift")
    if drift:
        warnings.extend(drift.get("alerts", []))

    # статус источников: настроен ли доступ / загружены ли данные
    source_status = {
        "alor": bool(REFRESH_TOKEN),                 # ценами живёт WS; здесь — есть ли креды
        "cbonds": bool(ruonia and keyrate),          # кривые ставок построены
        "nrd": nrd_service.is_active(),              # НРД-слой включён И подключён
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


# --- НРД-слой: статус + переключение (админ) ----------------------------------
class NrdToggle(BaseModel):
    enabled: bool


@router.get("/nrd", tags=["System"])
async def nrd_status():
    """Состояние НРД-слоя: включён (runtime-флаг), есть ли креды, реально ли активен,
    + счётчик реестра инструментов (источник универса без НРД)."""
    return {
        "enabled": nrd_config.is_enabled(),
        "configured": nrd_service.is_configured(),
        "active": nrd_service.is_active(),
        "registry": instruments_registry.count(),
    }


@router.post("/nrd", tags=["System"])
async def nrd_toggle(body: NrdToggle, _admin: dict = Depends(require_admin)):
    """Включить/выключить НРД-слой (только админ). Сбрасывает in-memory кэши НРД,
    смена подхватывается сразу. Включение без кред — флаг встанет, но active=False."""
    state = nrd_config.set_enabled(body.enabled)
    return {
        "enabled": state["enabled"],
        "configured": nrd_service.is_configured(),
        "active": nrd_service.is_active(),
    }
