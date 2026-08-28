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


def _bars_stat() -> dict:
    """Сводка накопленного тик/бар-архива (см. services/bars.py). Битая или ещё
    не созданная таблица не должна валить весь статус."""
    from services.portfolio_db import _connect
    try:
        with _connect() as c:
            b = c.execute("SELECT COUNT(*) n, COUNT(DISTINCT isin) p, MIN(ts) a "
                          "FROM bar_hourly").fetchone()
            t = c.execute("SELECT COUNT(*) n, COUNT(DISTINCT isin) p, MIN(ts) a "
                          "FROM trade_tick").fetchone()
        return {"bars": b["n"], "papers": b["p"], "bars_from": (b["a"] or "")[:10],
                "ticks": t["n"], "tick_papers": t["p"], "ticks_from": (t["a"] or "")[:10]}
    except Exception:
        return {"bars": 0, "papers": 0, "bars_from": None,
                "ticks": 0, "tick_papers": 0, "ticks_from": None}


def _blocks_stat() -> dict:
    """Слой крупных сделок (block_trade/block_day): сколько накоплено и с какой
    даты. Таблицы моложе прод-базы — их отсутствие статус не валит."""
    from services.portfolio_db import _connect
    try:
        with _connect() as c:
            b = c.execute("SELECT COUNT(*) n, COUNT(DISTINCT isin) p, MIN(ts) a, "
                          "MAX(ts) z FROM block_trade").fetchone()
            nd = c.execute("SELECT COUNT(*) n FROM block_trade WHERE market='ndm'").fetchone()
            d = c.execute("SELECT COUNT(*) n, MIN(date) a FROM block_day").fetchone()
            # список бумаг — чтобы пересечь с юниверсом в питоне: лента шире
            # реестра (весь рынок облигаций), и «покрытие» без пересечения
            # давало бы больше 100%
            isins = {r[0] for r in c.execute("SELECT DISTINCT isin FROM block_trade")}
        return {"blocks": b["n"], "papers": b["p"], "from": (b["a"] or "")[:10],
                "till": b["z"], "ndm": nd["n"], "days": d["n"],
                "days_from": (d["a"] or "")[:10], "isins": isins}
    except Exception:
        return {"blocks": 0, "papers": 0, "from": None, "till": None,
                "ndm": 0, "days": 0, "days_from": None, "isins": set()}


def _rss_mb() -> float:
    """RSS процесса, МБ — без psutil (его в образе нет)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    return 0.0


@router.get("/memory", tags=["Status"])
async def memory():
    """Сколько памяти держит процесс и НА ЧЁМ. Кэши в этом приложении живут
    внутри процесса, снаружи их не видно — а без размеров искать утечку можно
    только гаданием (13.08 RSS вырос 599 → 1004 МБ за ночь и подошёл к лимиту
    контейнера 1.2 ГБ). Здесь — длины всех долгоживущих словарей: у кого длина
    растёт со временем, тот и течёт."""
    caches: dict = {}

    def _len(name, obj):
        try:
            caches[name] = len(obj)
        except Exception:
            caches[name] = None

    try:
        from services import backdate as bd
        _len("backdate.asof_memo", bd._asof_memo)      # самый тяжёлый, см. memory_watch
        _len("backdate.honest_memo", bd._honest_memo)
        _len("backdate.anchor_memo", bd._anchor_memo)
    except Exception:
        pass
    try:
        from services.market_data import MarketDataService as MD
        _len("market_data.full_mem", MD._full_mem)
        _len("market_data.secid_cache", MD._secid_cache)
        _len("market_data.sec_cache", MD._sec_cache)
        _len("market_data.snap_cache", MD._snap_cache)
    except Exception:
        pass
    try:
        from services import universe_stream as us
        _len("universe_stream.level_memo", us._level_memo)
    except Exception:
        pass
    try:
        from services import trade_yidx as ty
        _len("trade_yidx.ctx_cache", ty._ctx_cache)
    except Exception:
        pass
    try:
        from services import coupon_calib as cc
        _len("coupon_calib.parse_cache", cc._parse_cache)
        _len("coupon_calib.idx_cache", cc._idx_cache)
        _len("coupon_calib.cache", cc._cache)
    except Exception:
        pass
    try:
        from services import instruments as ins
        _len("instruments.desc_cache", ins._desc_cache)
    except Exception:
        pass
    # market_cache — общий мешок горячих данных: интересны размеры его веток
    mc = {}
    for k, v in list(market_cache.items()):
        try:
            mc[k] = len(v) if hasattr(v, "__len__") else 1
        except Exception:
            mc[k] = None
    import gc
    return {"rss_mb": _rss_mb(), "gc_objects": len(gc.get_objects()),
            "caches": caches, "market_cache": mc}


@router.get("", tags=["Status"])
async def get_status():
    from services import instruments_registry as reg, ratings, fixed_income as fi, progress
    from services import trades_stream as tstream
    from services.universe_stream import stats as _us_stats
    from api.main import daemons_state as _daemons_state
    from auth import alor_token

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
        alor_token(),
        _ping("https://iss.moex.com/iss/index.json"),
        _ping("https://corpbonds.ru/"),
        _ping("https://www.cbr.ru/"),
    )

    # полнота данных
    M._ensure_full_mem()
    sched_n = len(M._full_mem)
    prices = market_cache.get("last_prices", {})
    fresh = M.session_prices()   # «свежие» = цены текущего торгового дня
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
    bars_stat = _bars_stat()
    blk = _blocks_stat()

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
             "hint": "DM/z/R-spread по юниверсу"},
            {"key": "Метрики фиксов", **frac(len(fxm), fx_n),
             "hint": "YTM/g-спред/дюрация"},
            {"key": "Рейтинги флоатеров", **frac(fl_rated, fl_n), "hint": "реестр (corpbonds)"},
            {"key": "Рейтинги фиксов", **frac(fx_rated, fx_n),
             "hint": "ОФЗ=AAA правилом; corpbonds не покрывает свежие 2025"},
            {"key": "Часовые бары (средневзвес)", **frac(bars_stat["papers"], total),
             "hint": f"строк {bars_stat['bars']} · глубина {bars_stat['bars_from'] or '—'}"},
            {"key": "Архив сделок (тики)", **frac(bars_stat["tick_papers"], total),
             "hint": f"сделок {bars_stat['ticks']} · с {bars_stat['ticks_from'] or '—'} "
                     f"(у брокера глубина ~30 дней, дальше только наш архив)"},
            {"key": "Крупные сделки (весь рынок)",
             **frac(len(blk["isins"] & ({*fl_ids} | {i for i, _ in fx_items})), total),
             "hint": f"сделок {blk['blocks']} (из них адресных/РПС {blk['ndm']}) по "
                     f"{blk['papers']} бумагам рынка · "
                     f"с {blk['from'] or '—'} · последняя {blk['till'] or '—'}; "
                     f"дневных РПС-агрегатов {blk['days']} с {blk['days_from'] or '—'}"},
        ],
        # живая лента: сколько бумаг идёт пушем Alor (у остальных сделки видны
        # только через ISS, а он публично отдаёт с задержкой 15 минут)
        "trades_stream": tstream.stats(),
        # пул котировок/стаканов по сокетам: мёртвый шард уносит 150 бумаг, и
        # общий счётчик streamed этого не показывает
        "universe_stream": _us_stats(),
        # фоновые воркеры: упавший и перезапущенный виден по restarts
        "daemons": _daemons_state(),
        # что грузится ПРЯМО СЕЙЧАС: обход баров, прогрев после рестарта, дрейн
        # рейтингов, разовые бэкфилл-скрипты (см. services/progress.py)
        "jobs": progress.snapshot(),
        # очереди реестра: несходящаяся очередь копится и стареет — здесь это видно
        # числом (n растёт, oldest_days растёт = голодание corpbonds-обогащения)
        "registry_queues": reg.queue_stats(),
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
