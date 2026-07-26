"""Вкладка ФИКСЫ — список облигаций с фиксированным купоном (ОФЗ-ПД + ликвидные
корпораты) с метриками к погашению. Универс и метрики прогреваются фоновым
поллером (market_cache), эндпоинт отдаёт кэш; при холодном кэше — быстрый фетч
универса (2 запроса), метрики появляются по мере прогрева."""
import re
import logging
from datetime import date
from fastapi import APIRouter, Path, HTTPException

from services.market_data import market_cache, MarketDataService
from services.exceptions import NotFoundException

logger = logging.getLogger(__name__)
router = APIRouter()

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

# поля метрик, доливаемые в строку из market_cache['fixed_metrics']
_METRIC_KEYS = ("last", "prev", "price_stale", "dirty", "ytm", "delta_ytm", "cur_yield",
                "g_spread_bps", "z_spread_bps", "mod_dur", "mac_dur", "convexity",
                "dv01", "put_date")


@router.get("", tags=["Fixed"])
async def get_fixed():
    """{items, total, calc_date} — фиксы с YTM/g-спред/z-спред/дюрацией."""
    from services import fixed_income as fi
    uni = market_cache.get("fixed_universe")
    if not uni:
        uni = await fi.fetch_fixed_universe()
    metrics = market_cache.get("fixed_metrics") or {}

    items = []
    for u in uni:
        m = metrics.get(u["isin"], {})
        item = {
            "isin": u["isin"], "secid": u.get("secid"), "name": u.get("name"),
            "issuer": u.get("issuer"),
            "cls": u.get("cls"), "maturity_date": u.get("maturity_date"),
            "coupon_pct": u.get("coupon_pct"), "val_today": u.get("val_today"),
            # цена: из метрик (last→prev с флагом) иначе сырой board
            "last_price_pct": m.get("last", u.get("last") if u.get("last") is not None else u.get("prev")),
        }
        for k in _METRIC_KEYS:
            if k in m:
                item[k] = m[k]
        items.append(item)

    return {"items": items, "total": len(items),
            "calc_date": market_cache.get("fixed_calc_date") or date.today().isoformat()}


def _display_cashflow(full: dict, calc_date: date) -> list:
    """Будущие потоки для карточки: купоны (ставка+₽) + амортизации/погашение."""
    from services.zspread import _d
    out = []
    for c in full.get("coupons") or []:
        e, v = _d(c.get("end")), c.get("value")
        if e and e > calc_date and v is not None:
            out.append({"date": e.isoformat(), "type": "COUPON",
                        "amount": round(float(v), 2), "rate_pct": c.get("valueprc")})
    amorts = [(_d(a.get("date")), a.get("value")) for a in (full.get("amorts") or [])]
    amorts = sorted((d, v) for d, v in amorts if d and v is not None and d > calc_date)
    for i, (d, v) in enumerate(amorts):
        out.append({"date": d.isoformat(),
                    "type": "MATURITY" if i == len(amorts) - 1 else "AMORT",
                    "amount": round(float(v), 2), "rate_pct": None})
    out.sort(key=lambda x: (x["date"], x["type"] == "COUPON"))
    return out


@router.get("/{isin}", tags=["Fixed"])
async def get_fixed_details(isin: str = Path(...)):
    """Карточка фикс-бумаги: справка + метрики к погашению + поток платежей."""
    isin = isin.strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="bad isin")
    from services import fixed_income as fi
    uni = market_cache.get("fixed_universe") or await fi.fetch_fixed_universe()
    row = next((u for u in uni if u.get("isin") == isin), None)
    if row is None:
        raise NotFoundException(f"{isin} не найден в универсе фиксов", {"isin": isin})

    secid = row.get("secid") or isin
    board = "TQOB" if row.get("cls") == "ofz" else "TQCB"
    full = await MarketDataService.fetch_bond_schedule_full(secid)
    _r, _k, cd, rd = await MarketDataService.get_curves()
    _ek, _eu, g = await MarketDataService.get_zspread_ctx()
    calc_date = cd or rd or date.today()
    m = fi.compute_fixed_row(row, full, g, calc_date)

    return {
        "reference": {
            "isin": isin, "secid": secid, "name": row.get("name"), "cls": row.get("cls"),
            "board": board, "maturity_date": row.get("maturity_date"),
            "coupon_pct": row.get("coupon_pct"), "face": row.get("face"),
        },
        "market": {
            "last_price_pct": m.get("last"), "prev_close_pct": row.get("prev"),
            "price_stale": m.get("price_stale", False), "dirty_rub": m.get("dirty"),
            "accrued_rub": row.get("accrued"), "val_today": row.get("val_today"),
        },
        "metrics": {
            "ytm_pct": m.get("ytm"), "cur_yield_pct": m.get("cur_yield"),
            "g_spread_bps": m.get("g_spread_bps"), "z_spread_bps": m.get("z_spread_bps"),
            "mod_dur": m.get("mod_dur"), "mac_dur": m.get("mac_dur"),
            "convexity": m.get("convexity"), "dv01": m.get("dv01"),
            "put_date": m.get("put_date"),
        },
        "cashflow": _display_cashflow(full, calc_date),
        "calc_date": calc_date.isoformat(),
    }
