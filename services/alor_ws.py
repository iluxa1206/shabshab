"""Alor WebSocket — реал-тайм по бумагам, которые смотрит фронт. Персистентный
WS к wss://api.alor.ru/ws, три вида подписок на одном сокете:

* OrderBookGetAndSubscribe по manager.orderbook_subscriptions — открытая
  карточка/стакан. На пуш reprice уровней (memo price→metrics, тик повторяет
  цены → считаем только новые) → broadcast_orderbook.
* AllTradesGetAndSubscribe по manager.market_subscriptions (избранное) — питает
  живой средневзвес дня (services/live_quotes): считаем свой VWAP по тикам, а не
  берём биржевой WAPRICE, чтобы цифра сходилась со слоем «Средневзвес» на графике.

КОТИРОВОК ЗДЕСЬ НЕТ: QuotesSubscribe по всему юниверсу (и избранному в том
числе) держит пул services/universe_stream — он же broadcast'ит патчи строк.

Дополняет HTTP-поллинг /api/orderbook (фронт держит его фолбэком): если WS ляжет,
стакан продолжит обновляться поллингом."""
import asyncio
import json
import logging
import os
import time

import aiohttp

from auth import alor_token, REFRESH_TOKEN, BASE_API

logger = logging.getLogger(__name__)

_WS_URL = BASE_API.replace("https://", "wss://") + "/ws"
# Глубина подписки. ПОТОЛОК БЕЗ СЖАТИЯ — 20: на 50 Alor отвечал «400 The
# orderbook subscription with the depth more than 20 is allowed only with
# enabled compression», подписка отклонялась КАЖДЫЙ раз, и стакан карточки молча
# жил на HTTP-поллинге 3 с. Ошибка терялась вместе с ответами на подписку (у них
# нет поля data), поэтому в логах не было ни строки.
_DEPTH = 20
_FREQ_MS = 800         # серверный троттл Alor: не чаще раза в 800мс на бумагу
# Потолок бумаг с живым VWAP (подписка AllTrades на бумагу), звёздочек можно
# наставить до 300 (_MAX_SUBS_PER_CLIENT). Сверх потолка средневзвес бумаги —
# биржевой WAPRICE из снапшота; котировки не капятся (их шардирует пул).
_LIVE_CAP = int(os.getenv("ALOR_LIVE_CAP", "60"))
_CTX_TTL = 300         # пересборка reprice-контекста per isin, сек
_RECONCILE_SEC = 2.0   # период сверки подписок с фронтом


class _Sub:
    __slots__ = ("guid", "kind", "metrics_fn", "face", "ctx_ts", "memo", "_logged")

    def __init__(self, guid):
        self.guid = guid
        self.kind = None
        self.metrics_fn = None
        self.face = None
        self.ctx_ts = 0.0
        self.memo = {}     # price -> {yield_pct, dm_bps, g_spread_bps}


async def _detect_kind(isin: str) -> str:
    from services.market_data import market_cache
    fx = market_cache.get("fixed_universe") or []
    return "fixed" if any(u.get("isin") == isin for u in fx) else "floater"


async def _ensure_ctx(sub: _Sub, isin: str) -> None:
    now = time.time()
    if sub.metrics_fn is not None and now - sub.ctx_ts < _CTX_TTL:
        return
    from services.orderbook_svc import build_metrics_fn
    if sub.kind is None:
        sub.kind = await _detect_kind(isin)
    try:
        sub.metrics_fn, _cd, sub.face = await build_metrics_fn(isin, sub.kind)
        sub.ctx_ts = now
        sub.memo = {}     # ctx пересобран → memo невалиден
    except Exception as e:
        logger.debug(f"alor_ws ctx {isin}: {e}")


def _levels(sub: _Sub, raw) -> list:
    out = []
    for e in raw or []:
        p, q = e.get("price"), e.get("volume")
        if p is None:
            continue
        m = sub.memo.get(p)
        if m is None:
            if sub.metrics_fn:
                try:
                    m = sub.metrics_fn(p)
                except Exception:
                    m = {}
            else:
                m = {}
            sub.memo[p] = m or {}
            m = sub.memo[p]
        out.append({"price_pct": p, "quantity": q, "yield_pct": m.get("yield_pct"),
                    "dm_bps": m.get("dm_bps"), "y_idx_bps": m.get("y_idx_bps"),
                    "g_spread_bps": m.get("g_spread_bps"),
                    # спред ко ВТОРОМУ горизонту едет рядом: свитчер карточки
                    # («погашение ↔ оферта») переключает готовое число, и ручной
                    # выбор больше не гасит подписку в пользу поллинга
                    "y_idx_alt_bps": m.get("y_idx_alt_bps"),
                    "alt_horizon": m.get("alt_horizon"),
                    "horizon": m.get("horizon")})
    return out


def _ladder(sub: _Sub, raw_bids, raw_asks):
    """Полная лестница цен с метриками на пустых уровнях — то же, что HTTP-режим
    «все уровни». Считается общей функцией; цены кэшируются в sub.memo, поэтому
    после прогрева стоит почти ноль."""
    from services.orderbook_svc import build_ladder
    pairs = lambda rows: [(e["price"], e.get("volume")) for e in (rows or [])
                          if e.get("price") is not None]

    def _one(price, qty):
        m = sub.memo.get(price)
        if m is None:
            try:
                m = sub.metrics_fn(price) if sub.metrics_fn else {}
            except Exception:
                m = {}
            sub.memo[price] = m or {}
            m = sub.memo[price]
        return {"price_pct": price, "quantity": qty, "yield_pct": m.get("yield_pct"),
                "dm_bps": m.get("dm_bps"), "y_idx_bps": m.get("y_idx_bps"),
                "g_spread_bps": m.get("g_spread_bps"),
                "y_idx_alt_bps": m.get("y_idx_alt_bps"),
                "alt_horizon": m.get("alt_horizon"), "horizon": m.get("horizon")}

    try:
        res = build_ladder(pairs(raw_bids), pairs(raw_asks), _one)
    except Exception as e:
        logger.debug("alor_ws ladder: %s", e)
        return None
    if not res:
        return None
    bids, asks = res
    return {"bids": sorted(bids, key=lambda x: x["price_pct"], reverse=True),
            "asks": sorted(asks, key=lambda x: x["price_pct"])}


def _seed_price(isin: str, px) -> None:
    """Live-цена стрима → единый кэш цен (session_prices). Расчёты бэка
    (метрики юниверса, broadcaster, карточки) видят цену избранного с задержкой
    пуша, а не такта поллера."""
    if px is None:
        return
    from services.market_data import market_cache
    market_cache["last_prices"][isin] = px
    market_cache["last_prices_ts"][isin] = time.time()


async def alor_orderbook_ws():
    from api.routes import ws as wsmod
    from services import live_quotes
    manager = wsmod.manager
    backoff = 1
    while True:
        token = await alor_token()
        if not token:
            await asyncio.sleep(10)
            continue
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(_WS_URL, heartbeat=20, timeout=15) as ws:
                    backoff = 1
                    subs = {}        # isin -> _Sub (стакан с reprice уровней)
                    trades = {}      # isin -> guid (поток сделок → живой VWAP)
                    guid_map = {}    # guid -> (канал, isin)
                    seq = 0
                    last_reconcile = 0.0
                    last_trade_push: dict[str, float] = {}
                    _unknown = [0]      # счётчик данных с чужим guid (см. ниже)
                    _bad = [0]          # счётчик битых сообщений (троттл лога)

                    async def _sub(op: str, isin: str, prefix: str, extra: dict) -> str:
                        nonlocal seq
                        seq += 1
                        guid = f"{prefix}-{isin}-{seq}"
                        guid_map[guid] = (prefix, isin)
                        await ws.send_json({"opcode": op, "code": isin, "exchange": "MOEX",
                                            "format": "Simple", "guid": guid,
                                            "token": token, **extra})
                        return guid

                    async def _unsub(guid: str) -> None:
                        guid_map.pop(guid, None)
                        try:
                            await ws.send_json({"opcode": "unsubscribe",
                                                "token": token, "guid": guid})
                        except Exception:
                            pass

                    while True:
                        now = time.time()
                        # сверка подписок с фронтом
                        if now - last_reconcile >= _RECONCILE_SEC:
                            last_reconcile = now
                            # стакан — только фронт-подписчики (открытые карточки)
                            want_ob = {i for i, socks in manager.orderbook_subscriptions.items() if socks}
                            # ДИАГНОСТИКА ПОДПИСОК. Воркер работал молча, и когда
                            # стакан карточки оставался на HTTP-поллинге, понять
                            # где обрыв — фронт не подписался, Alor не шлёт, пуш не
                            # доходит — было нечем.
                            for isin in want_ob - set(subs):
                                guid = await _sub("OrderBookGetAndSubscribe", isin, "ob",
                                                  {"depth": _DEPTH, "frequency": _FREQ_MS})
                                subs[isin] = _Sub(guid)
                                logger.info("alor_ws: подписка на стакан %s (глубина %d, "
                                            "троттл %d мс)", isin, _DEPTH, _FREQ_MS)
                            for isin in set(subs) - want_ob:
                                await _unsub(subs.pop(isin).guid)
                                logger.info("alor_ws: отписка от стакана %s", isin)

                            # избранное: поток сделок → живой VWAP. Уже подписанные
                            # держим, добор — до потолка (котировки шардирует
                            # universe_stream, здесь только AllTrades).
                            all_mk = sorted(i for i, socks in manager.market_subscriptions.items() if socks)
                            want_mk = set(all_mk) & set(trades)
                            for isin in all_mk:
                                if len(want_mk) >= _LIVE_CAP:
                                    break
                                want_mk.add(isin)
                            if len(all_mk) > _LIVE_CAP:
                                logger.info("alor_ws: живой VWAP на %d из %d бумаг (потолок)",
                                            len(want_mk), len(all_mk))
                            for isin in want_mk - set(trades):
                                trades[isin] = await _sub("AllTradesGetAndSubscribe", isin, "t",
                                                          {"depth": 0})
                                # дневной агрегат из архива — в фоне, цикл не ждёт сети
                                asyncio.create_task(live_quotes.ensure_day(isin))
                            for isin in set(trades) - want_mk:
                                await _unsub(trades.pop(isin))
                                live_quotes.drop(isin)
                                last_trade_push.pop(isin, None)
                        # читаем сообщение (короткий таймаут → сверка отзывчива)
                        try:
                            msg = await ws.receive(timeout=_RECONCILE_SEC)
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
                        # ОТВЕТ НА ПОДПИСКУ приходит без "data" — только
                        # requestGuid + httpCode. Раньше он молча уходил в
                        # continue вместе с ОШИБКАМИ подписки: стакан оставался
                        # на HTTP-поллинге, а в логах не было ни строки.
                        code = payload.get("httpCode")
                        if code is not None and int(code) != 200:
                            rg = payload.get("requestGuid")
                            logger.warning("alor_ws: подписка отклонена (%s) %s: %s",
                                           code, guid_map.get(rg, ("?", "?"))[1],
                                           payload.get("message") or payload)
                            continue
                        data, guid = payload.get("data"), payload.get("guid")
                        if not data or guid not in guid_map:
                            if data and guid:
                                _unknown[0] += 1
                                if _unknown[0] in (1, 50):
                                    logger.warning("alor_ws: данные с неизвестным guid %s "
                                                   "(известно %d) — подписка разъехалась",
                                                   guid, len(guid_map))
                            continue
                        chan, isin = guid_map[guid]

                        try:
                            if chan == "ob":
                                sub = subs.get(isin)
                                if not sub:
                                    continue
                                if not manager.orderbook_subscriptions.get(isin):
                                    continue
                                await _ensure_ctx(sub, isin)
                                out = {
                                    "orderbook": {"bids": _levels(sub, data.get("bids")),
                                                  "asks": _levels(sub, data.get("asks"))},
                                    # лестница едет рядом: режим «все уровни» в карточке
                                    # раньше гасил подписку и падал на поллинг 3 с
                                    "ladder": _ladder(sub, data.get("bids"), data.get("asks")),
                                    "pricing_status": "SUCCESS", "warnings": [], "src": "ws",
                                }
                                await manager.broadcast_orderbook(isin, out)
                                # первый пуш по бумаге — в лог: дальше молчим, иначе
                                # при троттле 800 мс лог зальёт всё
                                if not getattr(sub, "_logged", False):
                                    sub._logged = True
                                    logger.info("alor_ws: стакан %s пошёл в эфир "
                                                "(уровней %d, лестница %s)", isin,
                                                len(out["orderbook"]["asks"] or []),
                                                "есть" if out.get("ladder") else "нет")
                            elif chan == "t":
                                _seed_price(isin, data.get("price"))
                                # рублёвый объём считаем и здесь: тик избранной бумаги
                                # приходит и на этот сокет, и на сокет trades_stream —
                                # кто первый, того и агрегат (второй уйдёт как дубль по
                                # trade_id). Без value оборот бы терялся на этой ветке.
                                from services.trades_stream import _tick_value
                                # _tick_value отдаёт КОРТЕЖ (объём ₽, курс известен).
                                # Флаг нужен только очереди звонков (_alert_rows):
                                # в дневной агрегат объём кладём безусловно — ровно
                                # так же делает trades_stream._on_trade.
                                val, _fx_ok = _tick_value(isin, data.get("price"),
                                                          data.get("qty"))
                                live_quotes.add_trade(isin, data.get("price"), data.get("qty"),
                                                      tid=data.get("id"),
                                                      ts=str(data.get("time") or "") or None,
                                                      value=val)
                                # сделка двигает и цену, и средневзвес; при подписке Alor
                                # отдаёт пачку исторических сделок — троттл не даёт ей
                                # превратиться в череду пушей
                                if now - last_trade_push.get(isin, 0.0) >= 1.0:
                                    last_trade_push[isin] = now
                                    out = {"src": "ws"}
                                    if data.get("price") is not None:
                                        out["last_price_pct"] = data["price"]
                                    v = live_quotes.get(isin)
                                    if v:
                                        out["vwap_pct"] = v["vwap_pct"]
                                        out["vwap_volume"] = v["volume"]
                                        out["val_today"] = v["val_today"]
                                    await manager.broadcast_market_data(isin, out)
                        except Exception as e:
                            # ОДНО БИТОЕ СООБЩЕНИЕ НЕ РВЁТ СЕАНС. Внешний except
                            # накрывает весь while, поэтому ошибка в ветке сделок
                            # уносила и подписки на СТАКАНЫ, а backoff при входе
                            # сбрасывается в 1 — выходил шторм реконнектов на
                            # КАЖДОМ тике (так жил баг с кортежем _tick_value).
                            # Логируем ПО СЧЁТЧИКУ (как _unknown): системная
                            # ошибка бьёт на каждом тике, и шторм реконнектов
                            # сменился бы штормом в логе.
                            _bad[0] += 1
                            if _bad[0] in (1, 10, 100) or _bad[0] % 1000 == 0:
                                logger.warning("alor_ws: сообщение %s %s: %s "
                                               "(всего сбоев %d)", chan, isin, e, _bad[0])
        except Exception as e:
            logger.warning(f"alor_ws error: {e}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)
