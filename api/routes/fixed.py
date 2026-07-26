"""Вкладка ФИКСЫ — список облигаций с фиксированным купоном (ОФЗ-ПД + ликвидные
корпораты) с метриками к погашению. Универс и метрики прогреваются фоновым
поллером (market_cache), эндпоинт отдаёт кэш; при холодном кэше — быстрый фетч
универса (2 запроса), метрики появляются по мере прогрева."""
import logging
from datetime import date
from fastapi import APIRouter

from services.market_data import market_cache

logger = logging.getLogger(__name__)
router = APIRouter()

# поля метрик, доливаемые в строку из market_cache['fixed_metrics']
_METRIC_KEYS = ("last", "prev", "price_stale", "dirty", "ytm", "cur_yield",
                "g_spread_bps", "z_spread_bps", "mod_dur", "mac_dur", "convexity",
                "dv01", "put_date")


@router.get("", tags=["Fixed"])
async def get_fixed():
    """{items, total, calc_date} — фиксы с YTM/g-спред/z-спред/дюрацией."""
    from services import fixed_income as fi
    uni = market_cache.get("fixed_universe")
    if not uni:
        uni = await fi.fetch_fixed_universe()
    metrics = market_cache.get("fixed_metrics") or {}

    items = []
    for u in uni:
        m = metrics.get(u["isin"], {})
        item = {
            "isin": u["isin"], "secid": u.get("secid"), "name": u.get("name"),
            "cls": u.get("cls"), "maturity_date": u.get("maturity_date"),
            "coupon_pct": u.get("coupon_pct"), "val_today": u.get("val_today"),
            # цена: из метрик (last→prev с флагом) иначе сырой board
            "last_price_pct": m.get("last", u.get("last") if u.get("last") is not None else u.get("prev")),
        }
        for k in _METRIC_KEYS:
            if k in m:
                item[k] = m[k]
        items.append(item)

    return {"items": items, "total": len(items),
            "calc_date": market_cache.get("fixed_calc_date") or date.today().isoformat()}
