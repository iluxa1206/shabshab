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
from datetime import timedelta, timezone
from typing import Optional

import aiohttp

from auth import alor_token, REFRESH_TOKEN, BASE_API

from services.pools import run_bg

logger = logging.getLogger(__name__)

_WS_URL = BASE_API.replace("https://", "wss://") + "/ws"
_MSK_TZ = timezone(timedelta(hours=3))
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
_faces: dict = {"at": 0.0, "map": {}, "unit": {}}
_fx: dict = {"at": 0.0, "rates": {}}
_FX_TTL = 600               # курс валюты номинала: порог в рублях, а не в юанях
_stats = {"ticks": 0, "saved": 0, "flushes": 0, "last_ts": None, "no_board": 0,
          "skipped_small": 0, "no_fx": 0}
# Состояние каждого сокета: тихо отвалившийся шард уносит с собой ~250 бумаг, а
# снаружи это неотличимо от «по ним просто не торгуют». Считаем ВХОДЯЩИЕ
# сообщения до порога — это признак жизни сокета, а не ликвидности бумаг.
_shards: dict[int, dict] = {}
# Переподписка добирает хвост: при обрыве сокета сделки идут мимо нас, и
# крупный принт приезжал потом ISS-дрейном с его 15 минутами (замер 2026-08-25:
# 57 крупных биржевых сделок за день, кучами вокруг обрывов и рестартов).
# Брокер отдаёт последние сделки прямо на подписку — depth>0 закрывает дыру
# без отдельного REST-запроса; дубли снимает INSERT OR IGNORE по TRADENO.
_RESUB_DEPTH = int(os.getenv("ALOR_TRADES_RESUB_DEPTH", "10"))
_REPORT_SEC = 300           # период сводки в лог
_last_report = 0.0


def stats() -> dict:
    """Состояние слоя — для /api/status."""
    now = time.time()
    shards = [{"id": sid, "isins": s["isins"], "up": s["up"], "ticks": s["ticks"],
               "resubs": s["resubs"], "errors": s["errors"],
               "quiet_min": round((now - s["last"]) / 60, 1) if s["last"] else None}
              for sid, s in sorted(_shards.items())]
    return {"streamed": len(_streamed), "core": len(_core), "scope": _SCOPE,
            "buffered": sum(len(v) for v in _buf.values()),
            "shards": {"total": len(shards), "up": sum(1 for s in shards if s["up"]),
                       "mute": [s["id"] for s in shards if s["up"] and not s["ticks"]],
                       "list": shards},
            **_stats}


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
        _faces["unit"] = {i: v["face_unit"] for i, v in (listing or {}).items()
                          if v.get("face_unit")}
    return _faces["map"]


async def _fx_map() -> dict:
    """{валюта: курс к рублю} — по нему рублёвый объём тика валютной бумаги.

    Курс живёт отдельно от номиналов: номинал меняется раз в день (амортизация),
    курс — постоянно, и шестичасовой TTL номиналов ему не годится."""
    now = time.time()
    if _fx["rates"] and now - _fx["at"] < _FX_TTL:
        return _fx["rates"]
    try:
        from services import fx
        rates = await fx.get_fx_rates()
    except Exception as e:
        logger.warning("trades stream fx: %s", e)
        return _fx["rates"]
    if rates:
        _fx["rates"], _fx["at"] = rates, now
    return _fx["rates"]


def _flush_sync(chunks: list[tuple[str, list]], faces: dict) -> int:
    """Синхронная запись пачки (в to_thread) — одной транзакцией на весь такт:
    при рыночном охвате поштучная запись по бумаге держала ядро сотнями коротких
    транзакций (см. upsert_ticks_bulk)."""
    from services.trades_archive import upsert_ticks_bulk
    return upsert_ticks_bulk(chunks, faces)


# Колокольчик по безадресным сделкам живёт на ЭТОМ потоке, а не на ISS-ленте
# (services/block_trades): у ISS 15 минут задержки, и уведомление о принте
# приезжало ровно тогда, когда оно уже никому не нужно. Адресные (РПС) остаются
# на ISS — подписки на них у брокера нет.
BLOCK_ALERTS_FROM_STREAM = os.getenv("TRADES_STREAM_ALERTS", "1") not in ("0", "false", "no")


def _alert_rows(chunks: list[tuple[str, list]], floor: float) -> list[dict]:
    """Тики такта крупнее порога → строки для block_trades.ingest_ticks.

    Возраст СДЕЛКИ проверяется отдельно от возраста записи: переподписка
    добирает у брокера хвост (depth>0), и после долгого обрыва в буфер приедет
    то, что случилось давно. В архив это нужно, звонить об этом — нет."""
    from services.trades_archive import _msk_ts
    from services.block_trades import ALERT_MAX_AGE_MIN
    from datetime import datetime, timedelta
    old = (datetime.now(_MSK_TZ) - timedelta(minutes=ALERT_MAX_AGE_MIN)
           ).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for isin, raw in chunks:
        for t in raw:
            val = t.get("val") or 0.0
            if val < floor or t.get("id") is None:
                continue
            if not t.get("fx_ok", True):
                continue        # рублёвый объём недостоверен — звонить нечем
            ts = _msk_ts(str(t.get("time") or ""))
            if ts < old:
                continue
            out.append({"isin": isin, "trade_id": t["id"], "ts": ts,
                        "price": t.get("price"), "qty": t.get("qty"),
                        "value": val, "side": t.get("side"),
                        "board": t.get("board")})
    return out


async def _alert_on_ticks(chunks: list[tuple[str, list]]) -> None:
    """Крупные сделки такта — в ленту блоков и сразу в рассылку.

    Дублем ISS-копия не станет: TRADENO у Alor тот же, вставка идёт
    INSERT OR IGNORE, а очередь звонка помечена флагом на строке."""
    if not BLOCK_ALERTS_FROM_STREAM:
        return
    from services import block_trades as bt
    if not bt.BLOCK_ALERTS:
        return
    floor = await bt.alert_floor()
    rows = _alert_rows(chunks, floor)
    if not rows:
        return
    saved = await run_bg(bt.ingest_ticks, rows)
    if not saved:
        return                  # всё это ISS уже принёс — звонить не о чем
    sent = await bt.notify_blocks()
    if sent:
        logger.info("trades stream: %d уведомлений о сделках (без ISS-лага)", sent)


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
            await _fx_map()     # курс валюты номинала — тем же тактом: без него
                                # порог режет замещайки как «мелочь» (_tick_value)
            saved = await run_bg(_flush_sync, chunks, faces)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("trades stream flush: %s", e)
            continue
        try:
            await _alert_on_ticks(chunks)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("trades stream alerts: %s", e)
        _stats["flushes"] += 1
        _stats["saved"] += saved
        # сводка редкая: на такте раз в 2с построчный лог сам стал бы потоком
        global _last_report
        if time.time() - _last_report >= _REPORT_SEC:
            _last_report = time.time()
            sh = stats()["shards"]
            logger.info("trades stream: %d сделок записано, %d бумаг на сокетах, "
                        "шарды %d/%d живы%s", _stats["saved"], len(_streamed),
                        sh["up"], sh["total"],
                        f", БЕЗ ЕДИНОГО ТИКА: {sh['mute']}" if sh["mute"] else "")


async def _shard_socket(shard_id: int, isins: list, stop: asyncio.Event) -> None:
    """Сокет шарда: AllTradesGetAndSubscribe по своим бумагам → буфер.

    Первая подписка идёт с depth=0 — только новые сделки: историю сессии брокер
    отдал бы пачкой на каждую из 250 бумаг, а её и так закрывает drain/ISS.
    ПЕРЕподписка (обрыв, реконнект) берёт depth=_RESUB_DEPTH: за время обрыва
    сделки шли мимо нас, и крупный принт приезжал потом ISS-дрейном с его 15
    минутами. Хвост от брокера закрывает эту дыру сразу; дубли снимает
    INSERT OR IGNORE по TRADENO, а звонок по несвежей сделке — отсечка возраста
    в _alert_rows."""
    st = _shards.setdefault(shard_id, {"isins": len(isins), "up": False, "ticks": 0,
                                       "resubs": 0, "errors": 0, "last": 0.0})
    st["isins"] = len(isins)
    backoff = 1
    while not stop.is_set():
        token = await alor_token()
        if not token:
            await asyncio.sleep(10)
            continue
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(_WS_URL, heartbeat=20, timeout=15) as ws:
                    backoff = 1
                    depth = _RESUB_DEPTH if st["resubs"] else 0
                    st["resubs"] += 1
                    st["up"] = True
                    guid_isin = {}
                    for n, isin in enumerate(isins):
                        guid = f"ut{shard_id}-{isin}-{n}"
                        guid_isin[guid] = isin
                        await ws.send_json({
                            "opcode": "AllTradesGetAndSubscribe", "code": isin,
                            "exchange": "MOEX", "format": "Simple", "depth": depth,
                            "guid": guid, "token": token})
                        if n % 50 == 49:
                            await asyncio.sleep(0.2)   # не бить пачкой подписок
                    _streamed.update(isins)
                    if depth:
                        logger.info("trades stream: шард %d переподписан (%d бумаг, "
                                    "добор хвоста depth=%d)", shard_id, len(isins), depth)
                    while not stop.is_set():
                        try:
                            msg = await ws.receive(timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                        aiohttp.WSMsgType.CLOSING):
                            logger.warning("trades stream: шард %d — сокет закрылся (%s), "
                                           "%d бумаг без потока до переподписки",
                                           shard_id, msg.type.name, len(isins))
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
                        # счётчик ДО порога: это признак жизни сокета, а не
                        # ликвидности его бумаг
                        st["ticks"] += 1
                        st["last"] = time.time()
                        _on_trade(isin, data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            st["errors"] += 1
            logger.warning("trades stream shard %d: %s", shard_id, e)
        finally:
            st["up"] = False
            _streamed.difference_update(isins)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


# Валюта номинала MOEX → ключ курса в services/fx. Рубль пишут и SUR, и RUB.
_RUB_UNITS = ("", "SUR", "RUB", "RUR")
_FX_ALIAS = {"CNH": "CNY"}


def _tick_value(isin: str, price, qty) -> tuple[float, bool]:
    """(рублёвый объём тика, курс известен) — цена идёт в % от номинала.

    Кэш номиналов пустой на старте (первый flush его и наливает) — тогда считаем
    по 1000 ₽: для порога этого достаточно, а промах в номинале даёт лишнюю
    запись, а не потерянную сделку.

    У замещающих и юаневых бумаг номинал — В ВАЛЮТЕ (FACEUNIT), хотя расчёты
    рублёвые; без курса объём занижался в 12–83 раза, и порог TRADES_STREAM_MIN_RUB
    выбрасывал такую сделку как мелочь — в ленту она попадала только ISS-дрейном
    с его 15 минутами. Курса нет — объём отдаём как есть со ФЛАГОМ False: тик
    важен для архива и баров, а вот звонить по недостоверному рублёвому объёму
    нельзя (см. _alert_rows)."""
    face = _faces["map"].get(isin) or 1000.0
    try:
        base = float(qty) * face * float(price) / 100.0
    except (TypeError, ValueError):
        return 0.0, True
    unit = (_faces["unit"].get(isin) or "").upper()
    if unit in _RUB_UNITS:
        return base, True
    rate = _fx["rates"].get(_FX_ALIAS.get(unit, unit))
    if not rate:
        _stats["no_fx"] += 1
        return base, False
    return base * rate, True


def _on_trade(isin: str, data: dict) -> None:
    """Пуш Alor → буфер в формате, который понимает trades_archive.upsert_ticks
    (тот же, что у REST alltrades: id/price/qty/time/side/board)."""
    if data.get("id") is None or data.get("price") is None or not data.get("qty"):
        return
    val, fx_ok = _tick_value(isin, data.get("price"), data.get("qty"))
    # Порогом режем только то, чей рублёвый объём знаем достоверно: у валютной
    # бумаги без курса «мелкий» объём — артефакт пересчёта, а не размер сделки.
    if (isin not in _core and _OTHER_MIN_RUB > 0 and fx_ok
            and val < _OTHER_MIN_RUB):
        _stats["skipped_small"] += 1
        return
    if not data.get("board"):
        _stats["no_board"] += 1
    _buf.setdefault(isin, []).append({
        "id": data.get("id"), "price": data.get("price"), "qty": data.get("qty"),
        "time": str(data.get("time") or ""), "side": data.get("side"),
        "board": data.get("board"),
        # рублёвый объём уже посчитан — кладём рядом, чтобы очередь алертов
        # (см. _alert_rows) не считала его второй раз; trade_tick лишний ключ
        # игнорирует, у него объём пересчитывается по номиналу дня
        "val": val, "fx_ok": fx_ok,
    })
    _stats["ticks"] += 1
    _stats["last_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # живая цена, средневзвес и ОБОРОТ дня — services/live_quotes. Держим их по
    # всему юниверсу: тик сюда уже пришёл, дневной счёт стоит двух сложений, а
    # биржевые WAPRICE/VALTODAY из ISS-снапшота приезжают с задержкой. Вне
    # юниверса — только то, за чем следит alor_ws (там поток обрезан порогом,
    # оборот по нему был бы неполным).
    from services import live_quotes
    if isin in _core or live_quotes.get(isin) is not None:
        live_quotes.add_trade(isin, data.get("price"), data.get("qty"),
                              tid=data.get("id"), ts=str(data.get("time") or "") or None,
                              value=val)


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
                await _faces_map()      # номиналы нужны ДО первого тика: по ним
                                        # считается рублёвый объём (_tick_value)
                await _fx_map()         # и курс — у валютных номиналов тоже
                # дневные агрегаты юниверса из архива — счёт с открытия сессии, а
                # не с момента старта процесса; сверка идемпотентна
                from services import live_quotes
                await live_quotes.seed_universe(_core)
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
                    # рынок ужался — лишние номера шардов уходят из статистики,
                    # иначе в сводке вечно висел бы «мёртвый» сокет без тиков
                    for sid in [s for s in _shards if s >= len(shards)]:
                        _shards.pop(sid, None)
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
