"""Общий слой стакана: построение per-level metrics_fn(price) под тип бумаги
(флоатер: Y-IDX+DM+YTM, Y-IDX — первичная метрика; фикс: YTM+g-спред).
Переиспользуют роут /api/orderbook и фоновый монитор алертов — один источник расчёта."""
import os
import logging
from datetime import date

from services.market_data import MarketDataService, market_cache
from services.exceptions import NotFoundException

logger = logging.getLogger(__name__)


async def build_metrics_fn(isin: str, kind: str = "floater"):
    """→ (metrics_fn, calc_date, face). metrics_fn(price) даёт
    {yield_pct, dm_bps?, g_spread_bps?} под тип бумаги. Тёплый контекст строится
    один раз на выпуск, далее reprice по уровням без I/O. Бросает NotFoundException."""
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

        def metrics_fn(price):
            m = fi.compute_fixed_row(row, full_sched, g, calc_date, price_override=price)
            return {"g_spread_bps": m.get("g_spread_bps"), "yield_pct": m.get("ytm")}
        return metrics_fn, calc_date, face

    # флоатер
    from services.paths import cache_path as _cache_path
    from services.bond_details import load_reprice_ctx, reprice_at_price
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    ctx = await load_reprice_ctx(isin, cache)
    face = getattr(ctx["ref_obj"], "face_value", None)

    def metrics_fn(price):
        # горизонт уровня — по правилу цены (как в /orderbook и в карточке),
        # рядом кладём второй горизонт: свитчер «погашение ↔ оферта» на графике
        # переключается по готовым числам, без пересчёта
        from services.valuation import horizon_pair
        m = reprice_at_price(ctx, price)
        h, alt, alt_key = horizon_pair(m)
        return {"dm_bps": h.get("disc_margin_bps", m.get("disc_margin_bps")),
                "yield_pct": h.get("yield_xirr_pct", m.get("yield_xirr_pct")),
                "y_idx_bps": h.get("yield_over_index_bps", m.get("yield_over_index_bps")),
                "horizon": h.get("horizon"),
                "y_idx_alt_bps": alt.get("yield_over_index_bps"),
                "alt_horizon": alt.get("horizon") if alt else None}
    return metrics_fn, ctx["calc_date"], face
