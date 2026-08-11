"""Y-IDX сделки: во что встала цена принта в терминах спреда к индексу.

Зачем: цена сделки в процентах номинала сама по себе ничего не говорит —
100,04 у одного флоатера дорого, у другого дёшево. Сравнимая величина — Y-IDX
(доходность над индексом, bps), та же первичная метрика, что в таблице, стакане
и истории (см. [[yidx-primary]]).

Механика ровно та же, что у стакана: один тёплый контекст на выпуск
(build_metrics_fn) и дальше reprice по ценам БЕЗ I/O.

КОГДА считаем: в момент прихода сделки в архив (демон, block_trades.
price_new_trades), а не при чтении ленты. Замер на проде: 103 выпуска с
холодными контекстами — 65 с, с тёплыми — 2.3 с; на открытии вкладки это
неприемлемо, а демону за такт достаётся десяток бумаг. Побочно так и честнее:
модель берётся на момент сделки, а не на момент, когда кто-то открыл ленту.

ВАЖНО про прошлые дни: считаем ТЕКУЩЕЙ моделью (сегодняшняя кривая, сегодняшний
НКД), поэтому у сделок за прошлые сессии уровень оценочный — тот же компромисс,
что у спреда часовых баров (см. [[hourly-bars-archive]]). Честный as-of движок
дневной и слишком дорогой для ленты. Для сделок текущей сессии расхождения нет.

Считаем только по флоатерам: у фикса аналог — g-спред, другая шкала, в одной
колонке их мешать нельзя.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Потолок выпусков за один вызов: лента отдаёт до 5000 строк, но контекст
# строится на ВЫПУСК, и это единственная дорогая часть. Больше сотни выпусков
# в одном экране всё равно не читают, а хвост списка получит y_idx=None.
MAX_ISINS = int(os.getenv("TAPE_YIDX_MAX_ISINS", "120"))
# Контекст живёт минутами: кривая и НКД внутри дня меняются медленно, а
# пересборка на каждый запрос ленты — самая дорогая часть обогащения.
_CTX_TTL = float(os.getenv("TAPE_YIDX_CTX_TTL", "300"))
_ctx_cache: dict[str, tuple[float, object]] = {}
_FLOAT_BASES = ("KEYRATE", "RUONIA")


async def _metrics_fn(isin: str):
    """metrics_fn(price) для выпуска или None, если посчитать нечем."""
    hit = _ctx_cache.get(isin)
    now = time.monotonic()
    if hit and now - hit[0] < _CTX_TTL:
        return hit[1]
    from services.orderbook_svc import build_metrics_fn
    try:
        fn, _cd, _face = await build_metrics_fn(isin, "floater")
    except Exception as e:                     # нет в реестре, нет кривой, экзотика
        logger.debug("y-idx ленты: %s пропущен (%s)", isin, e)
        fn = None
    _ctx_cache[isin] = (now, fn)
    return fn


def _prune_cache() -> None:
    now = time.monotonic()
    for k, (at, _) in list(_ctx_cache.items()):
        if now - at > _CTX_TTL:
            _ctx_cache.pop(k, None)


async def enrich(rows: list[dict], labels: Optional[dict] = None,
                 max_isins: int = MAX_ISINS) -> int:
    """Проставляет строкам y_idx_bps (+ dm_bps) по цене сделки. → сколько посчитано.

    rows правятся на месте. Бумаги не-флоатеры и всё, что не удалось посчитать,
    остаются с y_idx_bps=None — колонка в UI покажет прочерк, а не ноль."""
    if not rows:
        return 0
    if labels is None:
        from services import instruments_registry as reg
        labels = await asyncio.to_thread(reg.labels_map)

    # порядок выпусков — по первому появлению в ленте: обрезание потолком
    # оставляет верх экрана (самое свежее), а не случайную часть
    order: list[str] = []
    for r in rows:
        isin = r.get("isin")
        if not isin or isin in order:
            continue
        if (labels.get(isin) or {}).get("base") not in _FLOAT_BASES:
            continue
        order.append(isin)
    if not order:
        return 0
    if len(order) > max_isins:
        logger.info("y-idx ленты: считаю %d выпусков из %d", max_isins, len(order))
        order = order[:max_isins]

    fns = {}
    for isin in order:                       # последовательно: контексты делят
        fns[isin] = await _metrics_fn(isin)  # общие тёплые кэши, гонка их не ускорит

    def _calc() -> int:
        n = 0
        # цена → метрики кэшируются на выпуск: у ликвидной бумаги сотни сделок
        # идут по десятку уникальных цен
        memo: dict[tuple[str, float], dict] = {}
        for r in rows:
            fn = fns.get(r.get("isin"))
            px = r.get("price")
            if fn is None or px is None:
                continue
            key = (r["isin"], float(px))
            m = memo.get(key)
            if m is None:
                try:
                    m = fn(float(px)) or {}
                except Exception as e:
                    logger.debug("y-idx %s @%s: %s", r["isin"], px, e)
                    m = {}
                memo[key] = m
            y = m.get("y_idx_bps")
            if y is not None:
                r["y_idx_bps"] = round(float(y), 1)
                n += 1
            if m.get("dm_bps") is not None:
                r["dm_bps"] = round(float(m["dm_bps"]), 1)
        return n

    # солвер спреда — чистый CPU, в event loop он держал бы WS-пуши
    done = await asyncio.to_thread(_calc)
    _prune_cache()
    return done


async def for_price(isin: str, price: float) -> Optional[float]:
    """Y-IDX одной цены (алерты о крупных сделках). None — если считать нечем."""
    fn = await _metrics_fn(isin)
    if fn is None or price is None:
        return None
    try:
        m = await asyncio.to_thread(fn, float(price))
    except Exception as e:
        logger.debug("y-idx %s @%s: %s", isin, price, e)
        return None
    y = (m or {}).get("y_idx_bps")
    return round(float(y), 1) if y is not None else None


async def for_rows(rows: Iterable[dict]) -> int:
    """Обогащение произвольного списка сделок (алерты): считает по одной, зато
    без потолка выпусков — сделок в очереди звонка единицы."""
    from services import instruments_registry as reg
    labels = await asyncio.to_thread(reg.labels_map)
    n = 0
    for r in rows:
        if (labels.get(r.get("isin")) or {}).get("base") not in _FLOAT_BASES:
            continue
        y = await for_price(r["isin"], r.get("price"))
        if y is not None:
            r["y_idx_bps"] = y
            n += 1
    return n
