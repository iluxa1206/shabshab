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

# последний ХОРОШИЙ хвост рынка вне юниверса: сбой ISS не должен схлопывать пул
_last_rest: list = []

_buf: dict[str, list] = {}          # isin → сырые тики до ближайшего flush
_streamed: set = set()              # ISIN на живых сокетах
_core: set = set()                  # из них — флоатер-юниверс (пишем целиком)
# Универс ФИКСОВ — такой же «свой» набор: витрина фиксов живёт на своём VWAP и
# обороте дня по тикам, а не на биржевых WAPRICE/VALTODAY (те отстают). Без
# этого набора фикс попадал под общий порог для бумаг вне юниверса и его
# дневной счёт был неполным.
_fixed: set = set()
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
# Сеанс дольше этого считаем состоявшимся — только он сбрасывает бэкофф сокета.
_UP_OK_SEC = float(os.getenv("ALOR_WS_UP_OK_SEC", "60"))
# Потолок буфера на возврат не записанной пачки (см. _requeue). 100 тыс. тиков —
# это минуты потока всего рынка на пике, а по памяти ~30 МБ: буфер переживает
# короткий отказ записи, но не растёт до OOM, если запись легла совсем.
_REQUEUE_MAX = int(os.getenv("TRADES_STREAM_REQUEUE_MAX", "100000"))
_last_report = 0.0


# Состояние ВЛАДЕЛЬЦА пула — отдельно от сокетов: «0 бумаг на сокетах» одинаково
# выглядит и когда пул ещё не строил шарды (холодный старт / упавший на листинге
# ISS reconcile), и когда все сокеты отвалились. Причину носим с собой.
_pool = {"started": 0.0, "built": 0.0, "shards": 0, "err": None, "err_at": 0.0}


def pool_state() -> dict:
    """Почему на сокетах пусто: старт пула, последняя сборка шардов, ошибка."""
    return dict(_pool)


def stats() -> dict:
    """Состояние слоя — для /api/status."""
    now = time.time()
    shards = [{"id": sid, "isins": s["isins"], "up": s["up"], "ticks": s["ticks"],
               "resubs": s["resubs"], "errors": s["errors"],
               "quiet_min": round((now - s["last"]) / 60, 1) if s["last"] else None}
              for sid, s in sorted(_shards.items())]
    return {"streamed": len(_streamed), "core": len(_core), "scope": _SCOPE,
            "pool": pool_state(),
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


def _requeue(chunks: list[tuple[str, list]]) -> None:
    """Не записанную пачку — обратно в буфер, старым тикам вперёд.

    Пачка уходит из _buf ДО записи, поэтому любая ошибка записи (диск, замок
    SQLite, упавший поток) уносила сделки навсегда: водяной знак дрейна стрим
    не двигает, но по бумагам ВНЕ юниверса дрейна и нет — их вернул бы только
    ISS со своей планкой 1 млн и 15 минутами.

    Потолок обязателен: если запись не встаёт вовсе (кончился диск), буфер без
    него растёт до OOM. При переполнении бумага теряет самые СТАРЫЕ свои тики,
    а хвост списка бумаг — все; о потере говорим ошибкой в лог, тихо терять
    сделки нельзя."""
    n = sum(len(v) for v in _buf.values())
    dropped = 0
    for isin, raw in chunks:
        free = _REQUEUE_MAX - n
        if free <= 0:
            dropped += len(raw)
            continue
        take = raw[-free:] if len(raw) > free else raw
        dropped += len(raw) - len(take)
        _buf.setdefault(isin, [])[:0] = take
        n += len(take)
    if dropped:
        logger.error("trades stream: буфер переполнен (%d тиков), ПОТЕРЯНО %d "
                     "сделок — запись в архив не встаёт", n, dropped)


async def _flush_once() -> int:
    """Один слив буфера в архив. Ошибка возвращает тики в буфер (см. _requeue)."""
    chunks = [(isin, raw) for isin, raw in _buf.items() if raw]
    _buf.clear()
    if not chunks:
        return 0
    try:
        faces = await _faces_map()
        await _fx_map()     # курс валюты номинала — тем же тактом: без него
                            # порог режет замещайки как «мелочь» (_tick_value)
        saved = await run_bg(_flush_sync, chunks, faces)
    except asyncio.CancelledError:
        # Отмена могла прийти УЖЕ ПОСЛЕ записи в потоке — тогда возврат в буфер
        # даст повтор, а не дубль: вставка идёт INSERT OR IGNORE по TRADENO.
        _requeue(chunks)
        raise
    except Exception as e:
        _requeue(chunks)
        logger.warning("trades stream flush: %s", e)
        return 0
    try:
        await _alert_on_ticks(chunks)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("trades stream alerts: %s", e)
    _stats["flushes"] += 1
    _stats["saved"] += saved
    return saved


async def _flusher(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.sleep(_FLUSH_SEC)
        if not _buf:
            continue
        await _flush_once()
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
    up_at = 0.0
    while not stop.is_set():
        try:
            # ТОКЕН ВНУТРИ try: alor_token() кидает, когда oauth не ответил за
            # свои 5 секунд. Снаружи это уносило ВЕСЬ таск шарда, а владелец
            # пула пересоздаёт таски только при смене состава бумаг — 250
            # бумаг оставались без потока до следующего reconcile с новым
            # юниверсом, то есть часами, и молча: ноля на сокетах нет, сторож
            # видит живых соседей.
            token = await alor_token()
            if not token:
                await asyncio.sleep(10)
                continue
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(_WS_URL, heartbeat=20, timeout=15) as ws:
                    up_at = time.monotonic()
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
        # БЭКОФФ СБРАСЫВАЕТ ТОЛЬКО ДОЛГИЙ СЕАНС, а не сам факт коннекта: если
        # брокер принимает соединение и тут же рвёт его (лимит подписок, чужой
        # токен), сброс на входе давал реконнект раз в секунду с каждого из 12+
        # сокетов — шторм по себе и по чужому rate-limit'у.
        if up_at and time.monotonic() - up_at >= _UP_OK_SEC:
            backoff = 1
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
    # ЖИВОЙ СЧЁТ ДНЯ — до порога и по всем «своим» бумагам: средневзвес и оборот
    # витрины должны считать каждую сделку, иначе VWAP смещён, а оборот занижен.
    # Вне своих наборов — только то, за чем уже следит alor_ws (там поток обрезан
    # порогом, и дневной счёт по нему всё равно был бы неполным).
    from services import live_quotes
    if isin in _core or isin in _fixed or live_quotes.get(isin) is not None:
        live_quotes.add_trade(isin, data.get("price"), data.get("qty"),
                              tid=data.get("id"), ts=str(data.get("time") or "") or None,
                              value=val)
    # Порогом режем только то, чей рублёвый объём знаем достоверно: у валютной
    # бумаги без курса «мелкий» объём — артефакт пересчёта, а не размер сделки.
    #
    # ФИКСЫ порогу ПОДЧИНЯЮТСЯ, хотя живой счёт получают целиком: их поток —
    # это ~400 тыс. тиков в день против 26 тыс. по остальному рынку (замер по
    # архиву 13–14.08.2026 против 25–27.08.2026), и складывать его в архив
    # значило бы растить базу на пару гигабайт в месяц ради ленты сделок,
    # которой хватает крупняка.
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
    # ФИКСЫ — вторым приоритетом, сразу за флоатерами: витрина фиксов такая же
    # живая. Пустой ответ источника не стирает набор (тот же guard, что у пула
    # котировок): куцый листинг не должен схлопывать подписки.
    from services.market_data import market_cache
    fx = {u["isin"] for u in (market_cache.get("fixed_universe") or []) if u.get("isin")}
    if fx:
        _fixed.clear()
        _fixed.update(fx)
    fixed = sorted(_fixed - set(core))
    if _SCOPE != "market":
        return (core + fixed)[:_MAX_ISINS]
    from services.market_data import MarketDataService
    all_bonds = await MarketDataService.fetch_bond_listing()
    rest = sorted(set(all_bonds or {}) - set(core) - set(fixed))
    # ISS отдаёт пустой (или куцый) листинг на любом своём сбое — молча, пустым
    # словарём. Раньше это схлопывало ПУЛ: список бумаг менялся 3166 → 608,
    # владелец убивал все 13 сокетов и поднимал 3, а весь рынок вне юниверса
    # слеп до следующего такта (замер 2026-08-28: так схлопывалось по разу в
    # час, каждый раз на 5 минут). Битый ответ не должен переподписывать пул:
    # держим последний хороший хвост, ждём следующего такта.
    if _last_rest and len(rest) < 0.5 * len(_last_rest):
        logger.warning("trades stream: листинг ISS куцый (%d бумаг вне юниверса "
                       "против %d) — пул не пересобираем", len(rest), len(_last_rest))
        rest = _last_rest
    elif rest:
        _last_rest[:] = rest
    return (core + fixed + rest)[:_MAX_ISINS]


_sock_tasks: list = []       # см. trades_stream_pool
_sock_stops: list = []
_flush_task = None           # сливной таск буфера, тоже на модуле


def _stop_sockets(tasks: list, stops: list) -> None:
    """Гасит текущий комплект сокетов пула: сигнал stop (сокет сам выходит из
    чтения), затем отмена таска."""
    for s in stops:
        s.set()
    for t in tasks:
        t.cancel()
    tasks.clear()
    stops.clear()


async def trades_stream_pool() -> None:
    """Владелец пула: режет рынок на шарды, держит по сокету на шард и один
    сливной таск, пересобирает пул при изменении списка бумаг."""
    if not TRADES_STREAM:
        return
    _pool["started"] = time.time()
    await asyncio.sleep(30)     # старт после прогрева, следом за пулом котировок
    # Ручки сокетов держим НА МОДУЛЕ: владельца пула перезапускает супервизор
    # (api.main._supervise), и локальный список унёс бы с собой ручки ЖИВЫХ
    # сокетов — старые подписки висели бы дальше, новые легли бы сверху, и
    # каждая сделка приезжала бы дважды с двойным комплектом подписок.
    tasks, stops = _sock_tasks, _sock_stops
    _stop_sockets(tasks, stops)
    current: Optional[frozenset] = None   # СОСТАВ последнего пула, не порядок
    retry = 0                   # пауза после ОШИБКИ: см. хвост цикла
    global _flush_task
    if _flush_task and not _flush_task.done():
        _flush_task.cancel()    # перезапуск пула не должен оставить ВТОРОЙ
                                # сливной таск на том же буфере
    flush_stop = asyncio.Event()
    flush_task = _flush_task = asyncio.create_task(_flusher(flush_stop))
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
                await live_quotes.seed_universe(_core | _fixed)
                # Сравниваем СОСТАВ, а не порядок: приоритетные группы (юниверс,
                # фиксы) переставляют бумаги внутри одного и того же рыночного
                # набора, и по кортежу пул пересобирался бы на каждой такой
                # перестановке — 13 сокетов заново из-за смены состава фиксов.
                key = frozenset(isins)
                if key != current:
                    _stop_sockets(tasks, stops)
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
                    _pool.update(built=time.time(), shards=len(shards))
                    logger.info("trades stream: %d бумаг (%d юниверс) / %d сокетов "
                                "сделок, охват %s", len(isins), len(_core),
                                len(shards), _SCOPE)
                _pool["err"] = None
                retry = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Пока reconcile падает (листинг ISS, реестр), сокетов НЕТ ни
                # одного: полный такт ожидания — это 5 минут слепой ленты.
                # Быстрый повтор с бэкоффом до обычного такта.
                _pool.update(err=str(e), err_at=time.time())
                logger.warning("trades stream reconcile: %s", e)
                retry = min(retry * 2 or 20, _RECONCILE_SEC)
            await asyncio.sleep(retry or _RECONCILE_SEC)
    except asyncio.CancelledError:
        _stop_sockets(tasks, stops)
        flush_stop.set()
        flush_task.cancel()
        # ПОСЛЕДНИЙ СЛИВ: в буфере лежит до _FLUSH_SEC секунд рыночного потока,
        # и на редеплое он просто исчезал. Флоатерам это позже закрыл бы REST-
        # дрейн (знак дрейна стрим не двигает), остальному рынку — никто.
        # Отмена уже пришла, поэтому щит: без него await умрёт на первом же
        # переключении, и слив не состоится.
        try:
            n = await asyncio.wait_for(asyncio.shield(_flush_once()), timeout=10)
            if n:
                logger.info("trades stream: финальный слив, %d сделок записано", n)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            logger.warning("trades stream: финальный слив не успел, %d тиков "
                           "в буфере", sum(len(v) for v in _buf.values()))
        except Exception as e:
            logger.warning("trades stream: финальный слив: %s", e)
        raise
