"""Живая лента безадресных сделок юниверса: Alor WS → trade_tick, без задержки.

Зачем: общерыночную ленту (вкладка СДЕЛКИ) наливает services/block_trades из
сквозной ленты ISS, а публичный ISS отдаёт сделки С ЗАДЕРЖКОЙ 15 МИНУТ (замер
2026-08-12: последняя сделка в базе 11:52 при 12:07 на часах). Alor
ретранслирует MOEX в реальном времени, но только по подписке НА БУМАГУ — отсюда
пул сокетов с шардированием юниверса, как у котировок и стаканов
(services/universe_stream).

Что этот слой закрывает и чего НЕ закрывает:
  • безадресные сделки ВСЕГО рынка облигаций MOEX — realtime, пушем (этот
    модуль). Флоатер-юниверс пишем целиком, остальной рынок — от порога
    TRADES_STREAM_MIN_RUB: в ленте у него всё равно нижняя планка 1 млн ₽,
    а полный тиковый ряд для его баров доливает часовой REST-дрейн;
  • адресные (РПС, размещения, выкупы) — по-прежнему ISS с его 15 минутами:
    подписки на них у брокера нет в принципе.
Обе ленты склеиваются по TRADENO (services/tape), так что доехавшая позже
ISS-копия той же сделки дублем не станет — INSERT OR IGNORE по (isin, trade_id).

Водяной знак дрейна (tick_drain) стрим НЕ двигает: знак означает «история
вычитана целиком», а пуш даёт только то, что случилось при живом сокете. Дыру
на старте/обрыве закрывает обычный drain из services/trades_archive.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiohttp

from auth import get_access_token, REFRESH_TOKEN, BASE_API

logger = logging.getLogger(__name__)

_WS_URL = BASE_API.replace("https://", "wss://") + "/ws"
TRADES_STREAM = os.getenv("TRADES_STREAM", "1") not in ("0", "false", "no")
_SHARD_SIZE = int(os.getenv("ALOR_TRADES_SHARD", "250"))   # ISIN на сокет
# Пишем пачками: SQLite синхронный, а на открытии рынка тики идут очередью по
# всему юниверсу — executemany раз в пару секунд дешевле сотни одиночных вставок.
_FLUSH_SEC = float(os.getenv("TRADES_STREAM_FLUSH", "2"))
_RECONCILE_SEC = 300        # пересборка шардов под изменившийся юниверс
_FACES_TTL = 6 * 3600       # номиналы (амортизация) меняются не чаще раза в день

# ОХВАТ. market — весь торгуемый рынок облигаций MOEX (≈3100 бумаг): без него
# лента по всему, чего нет во флоатер-юниверсе (ОФЗ-ПД, корп-фиксы, бумаги вне
# реестра), жила на ISS с его 15-минутной задержкой. universe — прежнее
# поведение, аварийный откат одной переменной окружения.
_SCOPE = os.getenv("TRADES_STREAM_SCOPE", "market").strip().lower()
_MAX_ISINS = int(os.getenv("TRADES_STREAM_MAX", "3300"))
# Бумаги ВНЕ юниверса пишем от порога: мелкий принт по ОФЗ-ПД в ленте не нужен
# (её нижняя планка и так 1 млн ₽), а поток «всё подряд по всему рынку» — это
# кратный рост архива на тесном диске VPS. Часовые бары по фиксам от этого не
# страдают: их тиковый ряд доливает REST-дрейн hourly_bars_worker (kinds
# включает fixed), а бар и так закрывается постфактум.
# Флоатеров юниверса порог НЕ касается: там тик нужен живым — на нём считается
# сегодняшняя цена бара и VWAP до прихода дрейна.
_OTHER_MIN_RUB = float(os.getenv("TRADES_STREAM_MIN_RUB", "1000000"))

_buf: dict[str, list] = {}          # isin → сырые тики до ближайшего flush
_streamed: set = set()              # ISIN на живых сокетах
_core: set = set()                  # из них — флоатер-юниверс (пишем целиком)
_faces: dict = {"at": 0.0, "map": {}}
_stats = {"ticks": 0, "saved": 0, "flushes": 0, "last_ts": None, "no_board": 0,
          "skipped_small": 0}
_REPORT_SEC = 300           # период сводки в лог
_last_report = 0.0


def stats() -> dict:
    """Состояние слоя — для /api/status."""
    return {"streamed": len(_streamed), "core": len(_core), "scope": _SCOPE,
            "buffered": sum(len(v) for v in _buf.values()), **_stats}


def live_isins() -> set:
    """Бумаги, чьи сделки идут пушем. Их лента свежая; остальным — ISS с лагом."""
    return set(_streamed)


async def _faces_map() -> dict:
    """{isin: номинал} по всему рынку — одним запросом к ISS.

    Номинал нужен для рублёвого объёма тика (цена в % от номинала), и у
    амортизируемых бумаг он не совпадает с первичным. Листинг MOEX отдаёт
    ТЕКУЩИЙ FACEVALUE, поэтому ежедневного обхода по бумагам не нужно."""
    now = time.time()
    if _faces["map"] and now - _faces["at"] < _FACES_TTL:
        return _faces["map"]
    from services.market_data import MarketDataService
    listing = await MarketDataService.fetch_bond_listing()
    m = {i: v["face"] for i, v in (listing or {}).items() if v.get("face")}
    if m:                       # пустой ответ ISS не должен обнулять карту
        _faces["map"], _faces["at"] = m, now
    return _faces["map"]


def _flush_sync(chunks: list[tuple[str, list]], faces: dict) -> int:
    """Синхронная запись пачки (в to_thread) — одной транзакцией на весь такт:
    при рыночном охвате поштучная запись по бумаге держала ядро сотнями коротких
    транзакций (см. upsert_ticks_bulk)."""
    from services.trades_archive import upsert_ticks_bulk
    return upsert_ticks_bulk(chunks, faces)


async def _flusher(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.sleep(_FLUSH_SEC)
        if not _buf:
            continue
        chunks = [(isin, raw) for isin, raw in _buf.items() if raw]
        _buf.clear()
        if not chunks:
            continue
        try:
            faces = await _faces_map()
            saved = await asyncio.to_thread(_flush_sync, chunks, faces)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("trades stream flush: %s", e)
            continue
        _stats["flushes"] += 1
        _stats["saved"] += saved
        # сводка редкая: на такте раз в 2с построчный лог сам стал бы потоком
        global _last_report
        if time.time() - _last_report >= _REPORT_SEC:
            _last_report = time.time()
            logger.info("trades stream: %d сделок записано, %d бумаг на сокетах",
                        _stats["saved"], len(_streamed))


async def _shard_socket(shard_id: int, isins: list, stop: asyncio.Event) -> None:
    """Сокет шарда: AllTradesGetAndSubscribe по своим бумагам → буфер.

    depth=0 — только новые сделки: историю сессии при подписке брокер отдал бы
    пачкой на каждую бумагу, а её и так закрывает drain/ISS."""
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
                        guid = f"ut{shard_id}-{isin}-{n}"
                        guid_isin[guid] = isin
                        await ws.send_json({
                            "opcode": "AllTradesGetAndSubscribe", "code": isin,
                            "exchange": "MOEX", "format": "Simple", "depth": 0,
                            "guid": guid, "token": token})
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
                        if not data or not isin:
                            continue
                        _on_trade(isin, data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("trades stream shard %d: %s", shard_id, e)
        finally:
            _streamed.difference_update(isins)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


def _tick_value(isin: str, price, qty) -> float:
    """Рублёвый объём тика по кэшу номиналов (цена — % от номинала).

    Кэш пустой на старте (первый flush его и наливает) — тогда считаем по 1000 ₽:
    для порога этого достаточно, а промах в номинале даёт лишнюю запись, а не
    потерянную сделку."""
    face = _faces["map"].get(isin) or 1000.0
    try:
        return float(qty) * face * float(price) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _on_trade(isin: str, data: dict) -> None:
    """Пуш Alor → буфер в формате, который понимает trades_archive.upsert_ticks
    (тот же, что у REST alltrades: id/price/qty/time/side/board)."""
    if data.get("id") is None or data.get("price") is None or not data.get("qty"):
        return
    if isin not in _core and _OTHER_MIN_RUB > 0 \
            and _tick_value(isin, data.get("price"), data.get("qty")) < _OTHER_MIN_RUB:
        _stats["skipped_small"] += 1
        return
    if not data.get("board"):
        _stats["no_board"] += 1
    _buf.setdefault(isin, []).append({
        "id": data.get("id"), "price": data.get("price"), "qty": data.get("qty"),
        "time": str(data.get("time") or ""), "side": data.get("side"),
        "board": data.get("board"),
    })
    _stats["ticks"] += 1
    _stats["last_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # живая цена и средневзвес избранного считаются в services/live_quotes —
    # трогаем их только для бумаг, за которыми уже следит alor_ws (иначе
    # дневной агрегат пришлось бы держать по всему юниверсу)
    from services import live_quotes
    if live_quotes.get(isin) is not None:
        live_quotes.add_trade(isin, data.get("price"), data.get("qty"),
                              tid=data.get("id"), ts=str(data.get("time") or "") or None)


async def subscription_isins() -> list[str]:
    """Кого слушаем. Флоатер-юниверс идёт ПЕРВЫМ и режется на шарды раньше всех:
    при отказе брокера на хвосте подписок (лимит, лишние бумаги) первым делом
    остаётся живой именно он — по нему считаются бары, VWAP и спред.

    scope=market добавляет весь остальной торгуемый рынок облигаций MOEX. Список
    берём из того же листинга, что даёт номиналы, — второго запроса не нужно."""
    from services import instruments_registry
    uni = await instruments_registry.fetch_floater_universe()
    core = sorted({u["isin"] for u in uni if u.get("isin")})
    _core.clear()
    _core.update(core)
    if _SCOPE != "market":
        return core[:_MAX_ISINS]
    from services.market_data import MarketDataService
    all_bonds = await MarketDataService.fetch_bond_listing()
    rest = sorted(set(all_bonds or {}) - set(core))
    return (core + rest)[:_MAX_ISINS]


async def trades_stream_pool() -> None:
    """Владелец пула: режет рынок на шарды, держит по сокету на шард и один
    сливной таск, пересобирает пул при изменении списка бумаг."""
    if not TRADES_STREAM:
        return
    await asyncio.sleep(30)     # старт после прогрева, следом за пулом котировок
    tasks: list = []
    stops: list = []
    current: Optional[tuple] = None
    flush_stop = asyncio.Event()
    flush_task = asyncio.create_task(_flusher(flush_stop))
    try:
        while True:
            try:
                isins = await subscription_isins()
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
                    current = key
                    logger.info("trades stream: %d бумаг (%d юниверс) / %d сокетов "
                                "сделок, охват %s", len(isins), len(_core),
                                len(shards), _SCOPE)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("trades stream reconcile: %s", e)
            await asyncio.sleep(_RECONCILE_SEC)
    except asyncio.CancelledError:
        for s in stops:
            s.set()
        for t in tasks:
            t.cancel()
        flush_stop.set()
        flush_task.cancel()
        raise
