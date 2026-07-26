"""Расчётный конвейер юниверса флоатеров (вынесен из api/routes/bonds.py).

Единый enrich_bond для обеих веток (фоновый расчёт всего юниверса и live-ветка
watchlist) — раньше это были две почти построчные копии в route-модуле, которые
уже начинали разъезжаться. Route теперь только транспорт/маппинг в схемы.
"""
import logging
from datetime import date
from typing import Dict, List, Optional

from services.market_data import MarketDataService
from services.bonds import (
    create_bond_ref_data, build_ref_external, next_coupon_after, reconcile_face,
)
from services.valuation import calculate_valuation_metrics
from services.zspread import compute_z_bps
from services import metrics

logger = logging.getLogger(__name__)


async def _aempty():
    return {}


def build_universe_ref(u: dict, isin: str, cache: dict, secs: dict):
    """BondRefData из локального кэша (isins_cache) или внешних источников
    (справочник MOEX + база/спред из строки универса реестра)."""
    data = cache.get(isin)
    if data:
        return create_bond_ref_data(data, isin)
    base = u.get("base_rate_type") or None
    return build_ref_external(isin, secs.get(isin, {}),
                              base=base if base != "UNKNOWN" else None,
                              spread_bps=u.get("spread_issue_bps") or 0)


def enrich_bond(u: dict, ref, full: dict, *, last: Optional[float],
                prev: Optional[float], accrued: Optional[float],
                ruonia_curve, keyrate_curve, exp_ks, exp_ru, g_curve,
                calc_date: date, prev_date: Optional[str] = None) -> dict:
    """Полный набор наших метрик по одной бумаге юниверса: dirty/SM/discDM/
    z_model/carry/refix/next_coupon/offer-метрики. Источники цен/НКД собирает
    вызывающий (фон — board snapshot + кэш поллера; watch — live-цена +
    per-isin snapshot); расчётная логика одна."""
    isin = u["isin"]
    base = u.get("base_rate_type", "UNKNOWN")
    coupons_full = full.get("coupons") or []
    amorts, offers = full.get("amorts"), full.get("offers")

    # Номинал: сверяем с фактом купона (value/valueprc). Ловит тихий фолбэк на
    # 1000, когда бумаги нет в securities-кэше.
    reconcile_face(ref, coupons_full, calc_date)

    periods = []
    for c in coupons_full:
        try:
            periods.append((date.fromisoformat(c["start"]), date.fromisoformat(c["end"]),
                            c.get("value")))
        except Exception:
            pass

    delta = round(last - float(prev), 4) if (last is not None and prev is not None) else None
    # цена для расчёта: live → prev-close (не зависим от момента WS-цены)
    price_calc = last if last is not None else prev

    curve = ruonia_curve if base == "RUONIA" else keyrate_curve
    dirty = dm = disc_dm = z_model = yoi = ytm = base_ytm = None
    implausible = False
    hz, off_d, sm_off, dm_off = "maturity", None, None, None
    if price_calc is not None and curve and base in ("RUONIA", "KEYRATE"):
        try:
            m = calculate_valuation_metrics(ref, price_calc, curve, calc_date,
                                            accrued_override=accrued,
                                            periods=periods or None,
                                            amorts=amorts, offers=offers)
            dirty, dm, disc_dm = m.get("dirty_price_rub"), m.get("dm_bps"), m.get("disc_margin_bps")
            yoi = m.get("yield_over_index_bps")
            ytm, base_ytm = m.get("yield_xirr_pct"), m.get("index_yield_pct")
            implausible = bool(m.get("price_implausible"))
            hz, off_d = m.get("preferred_horizon", "maturity"), m.get("offer_date")
            sm_off, dm_off = m.get("sm_to_offer_bps"), m.get("disc_margin_to_offer_bps")
        except Exception as e:
            logger.warning(f"valuation error {isin}: {e}")

    next_cpn = None
    if periods:
        future = [e for (_s, e, _v) in periods if e > calc_date]
        next_cpn = min(future) if future else None
    if next_cpn is None:
        try:
            next_cpn = next_coupon_after(ref, calc_date)
        except Exception:
            pass

    exp = exp_ru if base == "RUONIA" else exp_ks
    if exp and g_curve and price_calc is not None and base in ("RUONIA", "KEYRATE") and not implausible:
        try:
            coupons = ([{"start": c.get("start"), "end": c.get("end"), "value": c.get("value")}
                        for c in coupons_full]
                       if coupons_full else
                       [{"start": s.isoformat(), "end": e.isoformat(), "value": v}
                        for (s, e, v) in periods])
            z_model = compute_z_bps(ref, exp, g_curve, calc_date, price_calc,
                                    accrued if accrued is not None else ref.accrued_rub,
                                    coupons, amorts, offers)
        except Exception as e:
            logger.warning(f"z_model error {isin}: {e}")

    carry = refix = cur_cpn = None
    try:
        cpns = coupons_full or [{"start": s.isoformat(), "end": e.isoformat(), "value": v}
                                for (s, e, v) in periods]
        cb = metrics.carry_refix_block(cpns, amorts, ref.face_value, price_calc,
                                       exp, u.get("current_yield_pct"), calc_date)
        carry, refix, cur_cpn = cb["carry_bps"], cb["days_to_refix"], cb["current_coupon_pct"]
    except Exception as e:
        logger.warning(f"carry block error {isin}: {e}")

    # тонкая цена: PREVDATE (дата последней цены MOEX) старше 4 дней → бумага не
    # торговалась, цена несвежая, DM/z с ненадёжной цены. Возраст PREVDATE, а не
    # NUMTRADES: последний по выходным=0 у ВСЕХ (рынок закрыт) → ложный флаг.
    price_thin = False
    if prev_date:
        try:
            price_thin = (calc_date - date.fromisoformat(prev_date)).days > 4
        except (ValueError, TypeError):
            price_thin = False
    return {"last": last, "dirty": dirty, "dm": dm, "disc_dm": disc_dm, "yoi": yoi, "delta": delta,
            "ytm": ytm, "base_ytm": base_ytm,
            "next_coupon": next_cpn, "z_model": z_model, "carry": carry,
            "refix": refix, "current_coupon": cur_cpn, "implausible": implausible,
            "price_thin": price_thin,
            "horizon": hz, "offer_date": off_d, "sm_to_offer": sm_off, "dm_to_offer": dm_off}


async def compute_universe_metrics(uni: list, isins: list, cache_path: str) -> dict:
    """Фоновый расчёт полных метрик по всему юниверсу (вне watchlist). Данные MOEX
    батчатся (board snapshot одним запросом) и кэшируются на день. {isin: метрики}."""
    import asyncio
    want = {i for i in isins if i}
    uni_by = {u["isin"]: u for u in uni if u.get("isin") in want}
    ids = list(uni_by.keys())
    if not ids:
        return {}

    cache = MarketDataService.get_local_bond_cache(cache_path)
    external = [i for i in ids if i not in cache]

    prices = MarketDataService.cached_prices()
    board, curves, zctx, secs = await asyncio.gather(
        MarketDataService.fetch_board_snapshot(),
        MarketDataService.get_curves(),
        MarketDataService.get_zspread_ctx(),
        MarketDataService.fetch_moex_securities(external) if external else _aempty(),
    )
    ruonia_curve, keyrate_curve, cd, rd = curves
    exp_ks, exp_ru, g_curve = zctx
    calc_date = cd or rd or date.today()
    # bondization (купоны+амортизации) — из day-кэша, MOEX только на прогреве
    fulls = await asyncio.gather(*(MarketDataService.fetch_bond_schedule_full(i) for i in ids))
    full_by = dict(zip(ids, fulls))

    out: dict = {}
    for isin in ids:
        u = uni_by[isin]
        snap = board.get(isin, {})
        ref = build_universe_ref(u, isin, cache, secs)
        out[isin] = enrich_bond(
            u, ref, full_by.get(isin) or {},
            last=prices.get(isin), prev=snap.get("prev"), accrued=snap.get("accrued"),
            prev_date=snap.get("prev_date"),
            ruonia_curve=ruonia_curve, keyrate_curve=keyrate_curve,
            exp_ks=exp_ks, exp_ru=exp_ru, g_curve=g_curve, calc_date=calc_date)
    return out


async def compute_watch_metrics(uni_rows: List[dict], cache: dict) -> dict:
    """Live-метрики для watchlist-бумаг: цена из кэша поллера/WS, per-isin
    snapshot MOEX (точный prev/НКД, работает и для бумаг вне TQCB). {isin: метрики}."""
    import asyncio
    ids = [u["isin"] for u in uni_rows if u.get("isin")]
    if not ids:
        return {}
    external = [i for i in ids if i not in cache]
    prices = MarketDataService.cached_prices()
    snapshot, moex_ref, curves, zctx, fulls = await asyncio.gather(
        MarketDataService.fetch_moex_snapshot(ids),
        MarketDataService.fetch_moex_securities(external) if external else _aempty(),
        MarketDataService.get_curves(),
        MarketDataService.get_zspread_ctx(),
        asyncio.gather(*(MarketDataService.fetch_bond_schedule_full(i) for i in ids)),
    )
    full_by = dict(zip(ids, fulls))
    ruonia_curve, keyrate_curve, cd, rd = curves
    exp_ks, exp_ru, g_curve = zctx
    calc_date = cd or rd or date.today()

    out: dict = {}
    for u in uni_rows:
        isin = u["isin"]
        snap = snapshot.get(isin, {})
        prev = snap.get("prev")
        if prev is None:
            prev = (moex_ref.get(isin) or {}).get("prev")
        ref = build_universe_ref(u, isin, cache, moex_ref)
        out[isin] = enrich_bond(
            u, ref, full_by.get(isin) or {},
            last=prices.get(isin), prev=prev, accrued=snap.get("accrued"),
            prev_date=snap.get("prev_date"),
            ruonia_curve=ruonia_curve, keyrate_curve=keyrate_curve,
            exp_ks=exp_ks, exp_ru=exp_ru, g_curve=g_curve, calc_date=calc_date)
    return out


_cross_cache = {"date": None, "map": {}}


def cross_section_map(uni: list) -> dict:
    """{isin: spread_dur_yrs} по всему рынку, раз в день (кэш на дату)."""
    today = date.today().isoformat()
    if _cross_cache["date"] == today and _cross_cache["map"]:
        return _cross_cache["map"]
    today0 = date.today()
    out: dict = {}
    for u in uni:
        mat = u.get("maturity_date")
        out[u.get("isin")] = metrics.years_to(date.fromisoformat(mat), today0) if mat else None
    _cross_cache["date"] = today
    _cross_cache["map"] = out
    return out
