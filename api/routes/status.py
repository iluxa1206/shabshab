"""Страница СТАТУС: живость подключений (Alor/MOEX/CBR/corpbonds) + полнота
прогрева данных (расписания/цены/метрики/рейтинги X из Y) + таймстемпы кэшей."""
import asyncio
import logging
from datetime import date

import httpx
from fastapi import APIRouter

from services.market_data import MarketDataService as M, market_cache, _trading_day

logger = logging.getLogger(__name__)
router = APIRouter()


async def _ping(url: str, timeout: float = 4.0) -> dict:
    """Живость внешнего хоста: код ответа + латентность мс. 2xx/3xx/4xx = хост
    отвечает (up); только сетевой сбой/таймаут = down."""
    import time
    t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
        return {"up": True, "code": r.status_code, "ms": round((time.perf_counter() - t) * 1000)}
    except Exception as e:
        return {"up": False, "code": None, "ms": round((time.perf_counter() - t) * 1000),
                "error": type(e).__name__}


@router.get("", tags=["Status"])
async def get_status():
    from services import instruments_registry as reg, ratings, fixed_income as fi
    from auth import get_access_token, REFRESH_TOKEN

    # универсы
    fl_uni = await reg.fetch_floater_universe()
    fx_uni = market_cache.get("fixed_universe") or await fi.fetch_fixed_universe()
    fl_ids = [u["isin"] for u in fl_uni if u.get("isin")]
    fx_items = [(u["isin"], u.get("cls")) for u in fx_uni if u.get("isin")]
    fl_n, fx_n = len(fl_ids), len(fx_items)

    # кривые (in-memory)
    ru, ks, calc_date, rates_date = await M.get_curves()

    # подключения — пинги конкурентно + Alor-токен в потоке
    alor_tok, moex, cbonds, cbr_site = await asyncio.gather(
        asyncio.to_thread(get_access_token, REFRESH_TOKEN),
        _ping("https://iss.moex.com/iss/index.json"),
        _ping("https://corpbonds.ru/"),
        _ping("https://www.cbr.ru/"),
    )

    # полнота данных
    M._ensure_full_mem()
    sched_n = len(M._full_mem)
    prices = market_cache.get("last_prices", {})
    fresh = M.cached_prices()
    um = M.universe_metrics() or {}
    fxm = market_cache.get("fixed_metrics") or {}

    # рейтинги
    rat_fx = ratings.bucket_map_fixed(fx_items)
    fx_rated = sum(1 for v in rat_fx.values() if v)
    fl_rated = len(reg.ratings_map(fl_ids))
    rc = ratings._load()
    rat_json_ok = sum(1 for v in rc.values() if v.get("bucket"))
    rat_json_miss = sum(1 for v in rc.values() if v.get("miss"))

    total = fl_n + fx_n

    def frac(n, d):
        return {"n": n, "total": d, "pct": round(100 * n / d) if d else 0}

    return {
        "connections": [
            {"name": "Alor (котировки/стакан)", "up": bool(alor_tok),
             "detail": "токен активен" if alor_tok else "нет токена"},
            {"name": "MOEX ISS (расписания/цены)", "up": moex["up"],
             "detail": f"{moex['code']} · {moex['ms']} мс" if moex["up"] else moex.get("error", "нет связи")},
            {"name": "ЦБ РФ (КС/RUONIA/кривые)", "up": cbr_site["up"] and ru is not None,
             "detail": (f"кривые загружены · ставки {rates_date}" if ru is not None
                        else f"{cbr_site['code']} · нет кривых")},
            {"name": "corpbonds.ru (рейтинги)", "up": cbonds["up"],
             "detail": f"{cbonds['code']} · {cbonds['ms']} мс" if cbonds["up"] else cbonds.get("error", "нет связи")},
        ],
        "data": [
            {"key": "Расписания (bondization)", **frac(sched_n, total),
             "hint": "купоны/амортизации/оферты MOEX"},
            {"key": "Цены (last)", **frac(len(prices), total),
             "hint": f"свежих (не стейл): {len(fresh)}"},
            {"key": "Метрики флоатеров", **frac(len(um), fl_n),
             "hint": "DM/z/Y−IDX по юниверсу"},
            {"key": "Метрики фиксов", **frac(len(fxm), fx_n),
             "hint": "YTM/g-спред/дюрация"},
            {"key": "Рейтинги флоатеров", **frac(fl_rated, fl_n), "hint": "реестр (corpbonds)"},
            {"key": "Рейтинги фиксов", **frac(fx_rated, fx_n),
             "hint": "ОФЗ=AAA правилом; corpbonds не покрывает свежие 2025"},
        ],
        "ratings_drain": {
            "cached": len(rc), "rated": rat_json_ok, "miss": rat_json_miss,
            "hint": "json-кэш corpbonds (rated + negative-кэш промахов)",
        },
        "universe": {"floaters": fl_n, "fixed": fx_n},
        "timestamps": {
            "calc_date": str(calc_date) if calc_date else None,
            "rates_date": str(rates_date) if rates_date else None,
            "fixed_calc_date": market_cache.get("fixed_calc_date"),
            "schedules_trading_day": _trading_day(),
            "schedules_cache_day": M._full_mem_date,
        },
        "server_time_utc": date.today().isoformat(),
    }
