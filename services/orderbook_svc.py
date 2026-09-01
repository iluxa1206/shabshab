"""Общий слой стакана: построение per-level метрик под тип бумаги
(флоатер: Y-IDX+DM+YTM, Y-IDX — первичная метрика; фикс: YTM+g-спред).
Переиспользуют роут /api/orderbook и фоновый монитор алертов — один источник расчёта.

ДВА ИНТЕРФЕЙСА НА ОДНОМ КОНТЕКСТЕ:
  • build_levels_fn → levels_fn(СПИСОК цен) — основной. Поток, кривая и base leg
    от цены не зависят, поэтому вся лестница считается ОДНИМ проходом: замер
    28.08.2026 — 0,5 мс на цену против 33 мс за отдельный reprice_at_price
    (×65). Лестница на 60 уровней стоила 2 с, причём в event loop.
  • build_metrics_fn → metrics_fn(ОДНА цена) — совместимость для потребителей,
    которые считают цены поштучно с собственным мемо (лента сделок).
"""
import os
import logging
from datetime import date

from services.market_data import MarketDataService, market_cache
from services.exceptions import NotFoundException

logger = logging.getLogger(__name__)


def _px(p) -> float:
    """Ключ цены уровня. 4 знака — как у y_idx_many и fixed_side_metrics: цены
    приходят из стакана Alor и из шага лестницы, ключи обязаны совпадать."""
    return round(float(p), 4)


async def build_levels_fn(isin: str, kind: str = "floater", horizon: str = "auto"):
    """→ (levels_fn, calc_date, face). levels_fn(prices) даёт {цена: метрики}
    ОДНИМ проходом по бумаге. Тёплый контекст строится один раз на выпуск, далее
    счёт без I/O. Бросает NotFoundException.

    horizon — «auto» (правило цены НА КАЖДОМ уровне: цена уровня своя, значит и
    решение «сдам на оферту / держу до погашения» своё) либо явный ключ свитчера
    карточки."""
    if kind == "fixed":
        from services import fixed_income as fi
        uni = market_cache.get("fixed_universe") or await fi.fetch_fixed_universe()
        row = next((u for u in uni if u.get("isin") == isin), None)
        if row is None:
            raise NotFoundException(f"{isin} не найден в универсе фиксов", {"isin": isin})
        secid = row.get("secid") or isin
        full_sched = await MarketDataService.fetch_bond_schedule_full(secid)
        _r, _k, cd, rd = await MarketDataService.get_curves()
        _ek, _eu, g = await MarketDataService.get_zspread_ctx()
        calc_date = cd or rd or date.today()
        face = row.get("face")

        def levels_fn(prices):
            # у фикса батч уже есть — тот же, которым витрина считает стороны
            got = fi.fixed_side_metrics(row, full_sched, g, calc_date,
                                        [p for p in prices if p is not None])
            return {p: {"g_spread_bps": (got.get(_px(p)) or {}).get("g_spread_bps"),
                        "yield_pct": (got.get(_px(p)) or {}).get("ytm")}
                    for p in prices if p is not None}
        return levels_fn, calc_date, face

    # флоатер
    from services.paths import cache_path as _cache_path
    from services.bond_details import load_reprice_ctx
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    ctx = await load_reprice_ctx(isin, cache)
    face = getattr(ctx["ref_obj"], "face_value", None)

    def levels_fn(prices):
        """Все уровни ОДНИМ calculate_valuation_metrics: поток, кривая и base leg
        от цены не зависят и строятся один раз, на цену остаётся XIRR и солвер DM.
        Поштучный reprice на уровень стоил ×65 (замер 28.08.2026)."""
        from services.valuation import (calculate_valuation_metrics, horizon_at_price,
                                        alt_horizon)
        want = []
        for p in prices or []:
            if p is None:
                continue
            k = _px(p)
            if k > 0 and k not in want:
                want.append(k)
        if not want:
            return {}
        try:
            m = calculate_valuation_metrics(
                ctx["ref_obj"], want[0], ctx["curve"], ctx["calc_date"],
                accrued_override=ctx["accrued_live"], periods=ctx["periods"],
                amorts=ctx["amorts"], offers=ctx["offers"],
                ruonia_curve=ctx.get("ruonia_curve"),
                accrued_date=ctx.get("accrued_date"),
                alt_prices=want, alt_dm=True)
        except Exception as e:
            logger.debug("levels %s: %s", isin, e)
            return {}
        hzs = m.get("horizons") or {}
        out = {}
        for k in want:
            hz = horizon_at_price(k, m) if horizon in (None, "", "auto") else horizon
            if hz not in hzs:
                hz = "maturity"
            h = hzs.get(hz) or {}
            alt_key = alt_horizon(hz, hzs)
            alt = hzs.get(alt_key) or {}
            out[k] = {
                "y_idx_bps": (h.get("y_idx_by_price") or {}).get(k),
                "yield_pct": (h.get("ytm_by_price") or {}).get(k),
                "dm_bps": (h.get("dm_by_price") or {}).get(k),
                "horizon": hz,
                # спред ко ВТОРОМУ горизонту едет рядом: свитчер «погашение ↔
                # оферта» переключает готовое число, без пересчёта
                "y_idx_alt_bps": (alt.get("y_idx_by_price") or {}).get(k),
                "alt_horizon": alt_key,
            }
        return out
    return levels_fn, ctx["calc_date"], face


async def build_metrics_fn(isin: str, kind: str = "floater", horizon: str = "auto"):
    """→ (metrics_fn, calc_date, face) — ОДНА цена за вызов, поверх levels_fn.

    Для потребителей с собственным мемо по ценам (лента сделок). Считающим
    лестницу целиком нужен levels_fn: там батч дешевле в десятки раз."""
    levels_fn, calc_date, face = await build_levels_fn(isin, kind, horizon)

    def metrics_fn(price):
        return levels_fn([price]).get(_px(price), {})
    return metrics_fn, calc_date, face


# Потолок синтетических уровней лестницы: цены считаются батчем (levels_fn), но
# сам поток на уровень всё же XIRR + солвер DM — держим лестницу в границах.
MAX_LADDER = 60


def build_ladder(raw_bids, raw_asks, level_fn, max_levels: int = MAX_LADDER):
    """Непрерывная лестница цен с рыночным шагом между минимальным bid и
    максимальным ask — с метриками на КАЖДОМ уровне, даже без заявки.

    Отвечает на вопрос «при какой цене спред станет X»: пустые уровни считаются
    так же, как уровни с заявкой. level_fn(price, qty) строит элемент выдачи —
    HTTP-роут заворачивает в pydantic-модель, WS отдаёт словарь.

    Общая для обоих потребителей: раньше лестницу строил только HTTP-роут, и
    режим «только заявки» в карточке гасил WS-подписку, роняя обновление стакана
    с 800 мс до 3 с поллинга.
    """
    plan = ladder_plan(raw_bids, raw_asks, max_levels)
    if plan is None:
        return None
    bids, asks = [], []
    for price, qty in plan["levels"]:
        lvl = level_fn(price, qty)
        (asks if price > plan["mid"] else bids).append(lvl)
    return bids, asks


def ladder_plan(raw_bids, raw_asks, max_levels: int = MAX_LADDER):
    """Сетка уровней лестницы БЕЗ метрик: {"levels": [(цена, объём|None), ...],
    "mid": цена деления сторон}.

    Отдельно от build_ladder, потому что цены надо знать ДО расчёта: метрики
    считаются на весь список одним проходом (levels_fn), а не по уровню за раз."""
    if not (raw_bids or raw_asks):
        return None
    prices = sorted({p for p, _ in list(raw_bids) + list(raw_asks)})
    qty_at = {round(p, 4): q for p, q in list(raw_bids) + list(raw_asks)}
    lo, hi = prices[0], prices[-1]
    diffs = [round(b - a, 6) for a, b in zip(prices, prices[1:]) if b - a > 1e-9]
    step = min(diffs) if diffs else 0.01
    nsteps = int(round((hi - lo) / step)) if step > 0 else 0
    if nsteps > max_levels:
        step = (hi - lo) / max_levels
        nsteps = max_levels
    best_bid = max((p for p, _ in raw_bids), default=None)
    best_ask = min((p for p, _ in raw_asks), default=None)
    mid = ((best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None
           else (best_bid if best_bid is not None else best_ask))
    levels = []
    for i in range(nsteps + 1):
        price = round(lo + i * step, 4)
        levels.append((price, qty_at.get(price)))
    return {"levels": levels, "mid": mid}
