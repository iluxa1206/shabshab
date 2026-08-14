"""Живой поток котировок по ВСЕМУ юниверсу флоатеров + событийный пересчёт метрик.

Пул из нескольких персистентных WS-сокетов Alor: юниверс (~600 бумаг) шардируется
по сокетам, на каждый шард — QuotesSubscribe пачкой. В один сокет весь рынок не
лезет (лимиты подписок на соединение у Alor), отсюда пул. Alor ретранслирует
MOEX, так что это тот же источник, что board-снапшот, только push'ем.

Тик котировки:
  * цена → единый кэш цен (session_prices);
  * бумага → dirty-set событийного пересчёта;
  * есть WS-подписчик (избранное) → немедленный broadcast патча строки.

Событийный пересчёт (metrics_worker, такт 5с): полный enrich_bond только по
бумагам, у которых сменилась ЦЕНА СДЕЛКИ, и только по непосчитанным сегодня
уровням — кэш (isin, цена)→строка держит хит-рейт ~90% (облигация ходит по
5–30 уровням за день). Смена только bid/ask пересчёта не заказывает: Y-IDX по
верху стакана правится наклоном yoi_slope. Версия кэша = (calc_date, поколение
кривых): пересборка кривых или новый день сбрасывают всё.

Почему не reprice каждые 5с по всем: 584 × ~60мс = ~35с CPU на окно — 7 ядер.
Событийно с кэшем уровней то же самое стоит ~1–2% ядра.
"""
import asyncio
import json
import logging
import os
import time
from datetime import date
from typing import Dict, Optional

import aiohttp

from auth import get_access_token, REFRESH_TOKEN, BASE_API

logger = logging.getLogger(__name__)

_WS_URL = BASE_API.replace("https://", "wss://") + "/ws"
_SHARD_SIZE = int(os.getenv("ALOR_POOL_SHARD", "150"))   # ISIN на сокет
_QUOTE_FREQ_MS = 1000       # серверный троттл Alor на бумагу; для таблицы хватает
# Стрим стаканов всего юниверса (лестницы фильтра по объёму пушем, а не
# батч-снимком раз в 120с). Отдельные сокеты от котировок — вдвое больше
# подписок и на порядок больше трафика; выключается флагом.
_DEPTH_STREAM = os.getenv("DEPTH_STREAM", "1") not in ("0", "false", "no")
_DEPTH_FREQ_MS = 1700       # книга шевелится часто — прижимаем частоту пуша
_DEPTH_LEVELS = 20          # как у батч-снимка (services/depth._DEPTH_LEVELS)
_RECONCILE_SEC = 300        # пересборка шардов под изменившийся юниверс
_BATCH_SEC = 5.0            # такт событийного пересчёта
# Потолок полных пересчётов за такт (хвост — следующим). 40×~60мс ≈ 2.4с
# потоковой работы на 5-секундное окно: стартовая волна юниверса длится ~1.5
# минуты, зато CPU не насыщается и loop не лагает (80 давало 4.8с/окно — на
# двухъядерном хосте это почти постоянная занятость ядра)
_MAX_BATCH = 40
_PRICE_KEY_DIGITS = 3       # квантование цены в ключе кэша уровней

# ── состояние ────────────────────────────────────────────────────────────────
_last_quote: Dict[str, dict] = {}    # isin → последний пуш {last_price, bid, ask, ...}
_dirty: set = set()                  # бумаги со сменившейся ценой сделки
_streamed: set = set()               # ISIN на живых сокетах пула (для live_isins)
_depth_streamed: set = set()         # ISIN на живых depth-сокетах
_depth_msgs = 0                      # пуши стаканов с последней сводки (диагностика)
_level_memo: Dict[tuple, dict] = {}  # (isin, px_key) → строка метрик
_memo_version: Optional[tuple] = None
_memo_hits = 0
_memo_misses = 0


def live_isins() -> set:
    """ISIN, по которым котировка идёт push'ем. Их пропускают quotes_poller
    (сеяние кэша цен) и WS-broadcaster (пуш поллером): пул делает и то и другое
    сам. Пул упал — набор пуст, поллеры подхватывают."""
    return set(_streamed)


def depth_stream_covers(n_universe: int) -> bool:
    """Стрим стаканов покрывает юниверс — HTTP-батч depth_poller лишний.
    Порог 0.8: пара отвалившихся шардов не должна выключать фолбэк целиком."""
    return _DEPTH_STREAM and n_universe > 0 and len(_depth_streamed) >= 0.8 * n_universe


def stats() -> dict:
    return {"streamed": len(_streamed), "dirty": len(_dirty),
            "memo": len(_level_memo), "hits": _memo_hits, "misses": _memo_misses}


def _seed_price(isin: str, px) -> None:
    if px is None:
        return
    from services.market_data import market_cache
    market_cache["last_prices"][isin] = px
    market_cache["last_prices_ts"][isin] = time.time()


# ── доставка патча строки на фронт ───────────────────────────────────────────

# ключи enrich_bond → имена полей строки таблицы (те же, что в /api/bonds)
_METRIC_FIELDS = {
    "yoi": "yield_over_index_bps", "dm": "dm_bps", "disc_dm": "disc_margin_bps",
    "z_model": "z_model_bps", "ytm": "yield_xirr_pct", "base_ytm": "index_yield_pct",
    "dirty": "dirty_price_rub", "delta": "delta_to_prev_close",
    # Y-IDX верха стакана — те же числа, что в /api/bonds. Уходят в патч ВМЕСТЕ
    # с ценами сторон (ниже): фронт двигает спред стороны наклоном на каждом
    # тике, а здесь приходит точное значение и перекрывает линеаризацию.
    "yoi_bid": "y_idx_bid_bps", "yoi_ask": "y_idx_ask_bps",
    # горизонт прайсинга: он цено-зависим (правило цены vs цена выкупа), поэтому
    # едет в патче вместе с метриками — иначе маркер оферты в таблице остался бы
    # от прошлой цены и врал, к чему посчитан спред строки
    "horizon": "preferred_horizon",
}


def _metrics_patch(row: dict) -> dict:
    """Патч производных метрик для WS-пуша: фронт мерджит и НЕ зовёт /reprice —
    пересчёт уже сделан здесь, вторая ходка за тем же числом не нужна."""
    out = {_METRIC_FIELDS[k]: row[k] for k in _METRIC_FIELDS if row.get(k) is not None}
    if out:
        out["metrics"] = True     # маркер «производные посчитаны» для фронта
        # цены сторон — из ЭТОГО же расчёта, иначе спред стороны лёг бы на цену
        # из другого тика (рассинхрон «цена 99,00 / Y-IDX от 99,28»)
        for k in ("bid", "ask"):
            if row.get(k) is not None:
                out[k] = row[k]
    return out


async def _broadcast_quote(isin: str, data: dict) -> None:
    """Патч строки из пуша котировки — точечным подписчикам и wildcard-вкладкам
    (режим «вся таблица живая»)."""
    from api.routes import ws as wsmod
    from services import live_quotes
    if not wsmod.manager.has_market_audience(isin):
        return
    out = {
        "last_price_pct": data.get("last_price"),
        "bid": data.get("bid"), "ask": data.get("ask"),
        "bid_qty": data.get("bid_vol"), "ask_qty": data.get("ask_vol"),
        "src": "ws",
    }
    v = live_quotes.get(isin)
    if v:
        out["vwap_pct"] = v["vwap_pct"]
        out["vwap_volume"] = v["volume"]
        out["val_today"] = v["val_today"]
    await wsmod.manager.broadcast_market_data(
        isin, {k: v for k, v in out.items() if v is not None})


async def _on_quote(isin: str, data: dict) -> None:
    # Нет стороны стакана — брокер отдаёт 0, а не null. Нормализуем НА ВХОДЕ,
    # чтобы ноль не разошёлся по кэшу котировок, патчам WS и спреду по цене
    # (см. market_data._px_or_none).
    from services.market_data import _px_or_none
    for k in ("bid", "ask"):
        if k in data:
            data[k] = _px_or_none(data[k])
    px = data.get("last_price")
    _seed_price(isin, px)
    prev = _last_quote.get(isin)
    _last_quote[isin] = data
    # dirty только по смене цены СДЕЛКИ: bid/ask двигаются на порядок чаще и
    # полного пересчёта не стоят (их правит наклон в батче)
    if px is not None and (prev is None or prev.get("last_price") != px):
        _dirty.add(isin)
    await _broadcast_quote(isin, data)


# ── пул сокетов ──────────────────────────────────────────────────────────────

async def _shard_socket(shard_id: int, isins: list, stop: asyncio.Event) -> None:
    """Один сокет пула: подписка на свой шард, чтение до сигнала stop."""
    backoff = 1
    while not stop.is_set():
        token = await asyncio.to_thread(get_access_token, REFRESH_TOKEN)
        if not token:
            await asyncio.sleep(10)
            continue
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(_WS_URL, heartbeat=20, timeout=15) as ws:
                    backoff = 1
                    guid_isin = {}
                    for n, isin in enumerate(isins):
                        guid = f"up{shard_id}-{isin}-{n}"
                        guid_isin[guid] = isin
                        await ws.send_json({
                            "opcode": "QuotesSubscribe", "code": isin,
                            "exchange": "MOEX", "format": "Simple",
                            "frequency": _QUOTE_FREQ_MS, "guid": guid, "token": token})
                        if n % 50 == 49:
                            await asyncio.sleep(0.2)   # не бить пачкой подписок
                    _streamed.update(isins)
                    while not stop.is_set():
                        try:
                            msg = await ws.receive(timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                        aiohttp.WSMsgType.CLOSING):
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except Exception:
                            continue
                        data, guid = payload.get("data"), payload.get("guid")
                        isin = guid_isin.get(guid)
                        if data and isin:
                            await _on_quote(isin, data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("universe pool shard %d: %s", shard_id, e)
        finally:
            _streamed.difference_update(isins)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def _depth_socket(shard_id: int, isins: list, stop: asyncio.Event) -> None:
    """Сокет стрима стаканов: OrderBookGetAndSubscribe на шард, пуш → кэш
    глубины (market_cache['depth']) — тот же формат, что batch-снимок, лестницы
    фильтра по объёму просто становятся push-свежими."""
    global _depth_msgs
    from services.market_data import market_cache
    backoff = 1
    while not stop.is_set():
        token = await asyncio.to_thread(get_access_token, REFRESH_TOKEN)
        if not token:
            await asyncio.sleep(10)
            continue
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(_WS_URL, heartbeat=20, timeout=15) as ws:
                    backoff = 1
                    guid_isin = {}
                    for n, isin in enumerate(isins):
                        guid = f"ud{shard_id}-{isin}-{n}"
                        guid_isin[guid] = isin
                        await ws.send_json({
                            "opcode": "OrderBookGetAndSubscribe", "code": isin,
                            "exchange": "MOEX", "depth": _DEPTH_LEVELS, "format": "Simple",
                            "frequency": _DEPTH_FREQ_MS, "guid": guid, "token": token})
                        if n % 50 == 49:
                            await asyncio.sleep(0.2)
                    _depth_streamed.update(isins)
                    while not stop.is_set():
                        try:
                            msg = await ws.receive(timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                        aiohttp.WSMsgType.CLOSING):
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except Exception:
                            continue
                        data, guid = payload.get("data"), payload.get("guid")
                        isin = guid_isin.get(guid)
                        if not data or not isin:
                            continue
                        depth = market_cache.get("depth")
                        if depth is None:
                            depth = market_cache["depth"] = {}
                        depth[isin] = {
                            "b": [[float(e["price"]), float(e["volume"])]
                                  for e in (data.get("bids") or []) if e.get("price") is not None],
                            "a": [[float(e["price"]), float(e["volume"])]
                                  for e in (data.get("asks") or []) if e.get("price") is not None]}
                        market_cache["depth_ts"] = time.time()
                        _depth_msgs += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("depth stream shard %d: %s", shard_id, e)
        finally:
            _depth_streamed.difference_update(isins)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def universe_stream_pool() -> None:
    """Владелец пула: режет юниверс на шарды, держит по сокету на шард,
    пересобирает пул при изменении юниверса (новые выпуски/погашения)."""
    from services import instruments_registry
    await asyncio.sleep(25)     # старт после прогрева и первого снапшота
    tasks: list = []
    stops: list = []
    current: Optional[tuple] = None
    while True:
        try:
            uni = await instruments_registry.fetch_floater_universe()
            isins = sorted({u["isin"] for u in uni if u.get("isin")})
            key = tuple(isins)
            if key != current:
                for s in stops:
                    s.set()
                for t in tasks:
                    t.cancel()
                tasks, stops = [], []
                shards = [isins[i:i + _SHARD_SIZE]
                          for i in range(0, len(isins), _SHARD_SIZE)]
                for n, shard in enumerate(shards):
                    stop = asyncio.Event()
                    stops.append(stop)
                    tasks.append(asyncio.create_task(_shard_socket(n, shard, stop)))
                    if _DEPTH_STREAM:
                        dstop = asyncio.Event()
                        stops.append(dstop)
                        tasks.append(asyncio.create_task(_depth_socket(n, shard, dstop)))
                current = key
                logger.info("universe pool: %d бумаг / %d сокетов котировок%s",
                            len(isins), len(shards),
                            f" + {len(shards)} стаканов" if _DEPTH_STREAM else "")
        except asyncio.CancelledError:
            for s in stops:
                s.set()
            for t in tasks:
                t.cancel()
            raise
        except Exception as e:
            logger.warning("universe pool reconcile: %s", e)
        await asyncio.sleep(_RECONCILE_SEC)


# ── событийный пересчёт с кэшем уровней ──────────────────────────────────────

def invalidate_params(isin: Optional[str] = None) -> None:
    """Правка Справочника (спека фиксинга, маржа, даты) → строка пересчитывается
    на ближайшем такте. Кэш уровней (isin, цена)→строка держит СТАРЫЕ параметры,
    а сам пересчёт заказывается только сменой цены: без этого пинка правка
    доезжала до таблицы со следующей сделкой, а в неликвиде не доезжала вовсе.
    isin=None — массовая правка (импорт xlsx): чистим весь кэш."""
    if isin:
        for k in [k for k in _level_memo if k[0] == isin]:
            _level_memo.pop(k, None)
        if isin in _last_quote:
            _dirty.add(isin)
    else:
        _level_memo.clear()
        _dirty.update(_last_quote.keys())


def _px_key(px: float) -> float:
    return round(float(px), _PRICE_KEY_DIGITS)


def _crunch(batch: list, ctx: dict, enrich=None) -> Dict[str, dict]:
    """Синхронный счёт батча (в to_thread). batch = [(isin, quote)].

    Кэш уровней: цена уже считалась сегодня на этой версии кривых → строка из
    памяти, enrich не зовём. Патчим только то, что живёт вне уровня: bid/ask
    (Y-IDX по ним — наклоном), оборот."""
    global _memo_hits, _memo_misses
    if enrich is None:
        from services.universe import enrich_bond, build_universe_ref
        enrich = enrich_bond
        build_ref = build_universe_ref
    else:                       # тестовая инъекция
        build_ref = lambda u, isin, cache, secs: None
    out: Dict[str, dict] = {}
    for isin, q in batch:
        px = q.get("last_price")
        u = ctx["uni_by"].get(isin)
        if px is None or u is None:
            continue
        key = (isin, _px_key(px))
        row = _level_memo.get(key)
        if row is None:
            _memo_misses += 1
            snap = ctx["board"].get(isin, {})
            try:
                ref = build_ref(u, isin, ctx["cache"], ctx["secs"])
                row = enrich(
                    u, ref, ctx["full_by"].get(isin) or {},
                    last=px, prev=snap.get("prev"), accrued=snap.get("accrued"),
                    prev_date=snap.get("prev_date"),
                    bid=q.get("bid"), ask=q.get("ask"),
                    ruonia_curve=ctx["ruonia_curve"], keyrate_curve=ctx["keyrate_curve"],
                    exp_ks=ctx["exp_ks"], exp_ru=ctx["exp_ru"], g_curve=ctx["g_curve"],
                    calc_date=ctx["calc_date"])
            except Exception as e:
                logger.debug("universe crunch %s: %s", isin, e)
                continue
            _level_memo[key] = row
        else:
            _memo_hits += 1
        row = dict(row)          # кэш неизменяем — наружу копия
        # вне уровня: верх стакана (Y-IDX наклоном от цены сделки) и оборот
        slope = row.get("yoi_slope")
        yoi = row.get("yoi")
        for side, fld in (("bid", "yoi_bid"), ("ask", "yoi_ask")):
            v = q.get(side)
            # 0 = стороны в стакане нет: ни цены, ни спреда по ней. Раньше ноль
            # ехал в колонку как «0,00», а наклон от него давал спред в тысячи
            # б.п. (МТС 2Р-03 — 8960 при отсутствующем оффере).
            v = v if (v or 0) > 0 else None
            row[side] = v
            row[fld] = None
            if v is not None and yoi is not None and slope is not None:
                row[fld] = int(round(yoi + (v - px) * slope))
        snap = ctx["board"].get(isin, {})
        # свой тиковый счёт впереди биржевого (см. services/universe): VALTODAY и
        # WAPRICE из ISS-снапшота отстают, тик уже здесь
        from services import live_quotes
        lv = live_quotes.get(isin) or {}
        vol = max(snap.get("vol") or 0, lv.get("val_today") or 0) or None
        if vol is not None:
            row["val_today"] = vol
        row["wap"] = lv.get("vwap_pct") or snap.get("waprice") or row.get("wap")
        out[isin] = row
    return out


async def _day_ctx() -> Optional[dict]:
    """Общий контекст батча: кривые, борд-снапшот, справочники, расписания —
    всё из day-кэшей, сеть только на промахах."""
    from services.market_data import MarketDataService, market_cache
    from services.paths import cache_path
    curves, zctx, board = await asyncio.gather(
        MarketDataService.get_curves(),
        MarketDataService.get_zspread_ctx(),
        MarketDataService.fetch_board_snapshot())
    ruonia_curve, keyrate_curve, cd, rd = curves
    if ruonia_curve is None or keyrate_curve is None:
        return None
    exp_ks, exp_ru, g_curve = zctx
    from services import instruments_registry
    uni = await instruments_registry.fetch_floater_universe()
    return {
        "uni_by": {u["isin"]: u for u in uni if u.get("isin")},
        "cache": MarketDataService.get_local_bond_cache(cache_path("isins_cache.json")),
        "secs": {},              # промахи локального кэша обходятся без MOEX-добора
        "board": board,
        "ruonia_curve": ruonia_curve, "keyrate_curve": keyrate_curve,
        "exp_ks": exp_ks, "exp_ru": exp_ru, "g_curve": g_curve,
        "calc_date": cd or rd or date.today(),
        "version": (str(cd or date.today()), market_cache.get("curves_ts") or 0),
        "full_by": {},
    }


def _check_version(version: tuple) -> None:
    """Новый день или пересобранные кривые → кэш уровней недействителен весь:
    та же цена даёт другой спред на другой кривой."""
    global _memo_version
    if version != _memo_version:
        _level_memo.clear()
        _memo_version = version


async def metrics_worker() -> None:
    """Такт 5с: полный пересчёт только изменившихся цен и только новых уровней.
    Результат — в market_cache['universe_metrics'] (его читает /api/bonds и
    /api/bonds/quotes) и push-патчем подписчикам избранного."""
    from api.routes import ws as wsmod
    from services.market_data import MarketDataService, market_cache
    await asyncio.sleep(40)
    done_since_log = 0
    last_log = time.time()
    while True:
        await asyncio.sleep(_BATCH_SEC)
        try:
            # минутная сводка — живой ли конвейер и каков хит-рейт кэша уровней
            if done_since_log and time.time() - last_log >= 60:
                global _depth_msgs
                logger.info("metrics engine: %d строк/мин · memo %d (hit %d / miss %d) · "
                            "dirty %d · depth-пушей %d/мин (%d бумаг)",
                            done_since_log, len(_level_memo), _memo_hits, _memo_misses,
                            len(_dirty), _depth_msgs, len(_depth_streamed))
                done_since_log = 0
                _depth_msgs = 0
                last_log = time.time()
            if not _dirty:
                continue
            take = list(_dirty)[:_MAX_BATCH]
            _dirty.difference_update(take)
            ctx = await _day_ctx()
            if ctx is None:
                continue
            _check_version(ctx["version"])
            # расписания бумаг батча — из day-кэша (промах = одна ходка на бумагу в день)
            fulls = await asyncio.gather(
                *(MarketDataService.fetch_bond_schedule_full(i) for i in take),
                return_exceptions=True)
            ctx["full_by"] = {i: ({} if isinstance(f, Exception) else f or {})
                              for i, f in zip(take, fulls)}
            batch = [(i, _last_quote.get(i) or {}) for i in take]
            from services.heavy import run_heavy
            rows = await run_heavy(_crunch, batch, ctx)
            if not rows:
                continue
            um = market_cache.get("universe_metrics") or {}
            um.update(rows)
            market_cache["universe_metrics"] = um
            done_since_log += len(rows)
            # подписчикам — производные пушем: /reprice с фронта не нужен
            for isin, row in rows.items():
                if wsmod.manager.has_market_audience(isin):
                    patch = _metrics_patch(row)
                    if patch:
                        await wsmod.manager.broadcast_market_data(isin, patch)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("metrics worker: %s", e)
