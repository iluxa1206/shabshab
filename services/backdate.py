"""Метрики флоатера НА ПРОШЛУЮ ДАТУ: (isin, дата D, цена) → SM/DM/y-idx/YTM.

Честность входов на D:
  • цена     — задаётся пользователем ИЛИ close/legalclose из MOEX history;
  • НКД      — факт ACCINT из MOEX history на D (фолбэк: НКД из графика купонов);
  • номинал  — факт FACEVALUE из MOEX history на D (амортизация учтена биржей);
  • кривая   — curve_asof(): архив своп-котировок на дату ≤ D (mode="market",
    копится curve_history) → честный bootstrap; иначе ГИБРИД (mode="realized"):
    сегмент D→сегодня из реализованного дневного факта индекса ЦБ (КС/RUONIA),
    дальше — сегодняшняя bootstrap-кривая, сшитая по DF. Для прошедшего отрезка
    реализованный факт точнее любой тогдашней рыночной кривой (эти купоны уже
    зафиксированы именно так); хвост — сегодняшние ожидания.
  • фиксинги — история индекса ЦБ покрывает D→сегодня фактом, начавшиеся на D
    периоды проецируются по факту (та же реализованная философия, что кривая).

Расчёт — тот же calculate_valuation_metrics, что прайсит live-таблицу:
результаты сопоставимы с текущими SM/DM/y-idx колонка-в-колонку.
"""
from __future__ import annotations

import asyncio
import logging
from bisect import bisect_right
from datetime import date, timedelta
from typing import Optional, Tuple

import httpx

from core.forwards import BootstrappedForwardCurve, CurveBootstrapper, DiscountCurve
from services.exceptions import NotFoundException, CalculationException

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- кривая as-of

def build_hybrid_curve(base: str, calc_date: date, hist_pairs: list,
                       today_curve: DiscountCurve) -> BootstrappedForwardCurve:
    """Гибридная кривая на прошлую дату D: DF от D+1 до старта сегодняшней кривой
    компаундится дневным ФАКТОМ индекса (ступень: последняя ставка ЦБ ≤ t), дальше
    подшиваются узлы сегодняшней кривой, отмасштабированные сплайс-DF.

    Узлы ставятся на каждой смене ставки — log-linear интерполяция DF между ними
    воспроизводит дневной компаундинг точно (exp линеен по дням при конст. ставке).
    Конвенция forward() — как у боевой кривой той же базы (BootstrappedForwardCurve).
    """
    eff = calc_date + timedelta(days=1)            # T+1, как effective start bootstrap
    splice = today_curve.calc_date                  # effective start сегодняшней кривой
    if eff >= splice:
        return today_curve                          # D сегодня/вчера — гибрид не нужен

    dates = [d for d, _ in hist_pairs]
    rates = [r for _, r in hist_pairs]
    if not dates or dates[0] > eff:
        raise CalculationException(
            f"история {base} не покрывает {eff.isoformat()} — гибридная кривая невозможна")

    def rate_at(d: date) -> float:
        i = bisect_right(dates, d) - 1
        return rates[i]

    nodes = []
    df = 1.0
    d = eff
    prev_rate = rate_at(d)
    while d < splice:
        r = rate_at(d)
        if r != prev_rate:
            nodes.append((d, df))                   # узел на смене ставки
            prev_rate = r
        df /= (1.0 + (r / 100.0) / 365.0)           # дневной факт, ACT/365
        d += timedelta(days=1)
    nodes.append((splice, df))

    scale = df
    for nd, ndf in today_curve.nodes:
        if nd > splice:
            nodes.append((nd, scale * ndf))

    return BootstrappedForwardCurve(eff, nodes, base)


def curve_asof(base: str, calc_date: date, today_curve: DiscountCurve,
               hist_pairs: list) -> Tuple[DiscountCurve, str]:
    """Кривая на прошлую дату + режим ('market' архив котировок / 'realized' гибрид)."""
    try:
        from services.curve_history import quotes_asof
        quotes = quotes_asof(base, calc_date)
    except Exception as e:
        logger.warning(f"curve_history quotes_asof failed: {e}")
        quotes = None
    if quotes:
        try:
            boot = (CurveBootstrapper.bootstrap_ruonia if base == "RUONIA"
                    else CurveBootstrapper.bootstrap_keyrate)
            return boot(quotes, calc_date), "market"
        except Exception as e:
            logger.warning(f"as-of bootstrap {base}@{calc_date} failed: {e} — фолбэк на гибрид")
    return build_hybrid_curve(base, calc_date, hist_pairs, today_curve), "realized"


# ------------------------------------------------------- дневная строка MOEX

async def fetch_history_row(secid: str, d: date, board: str = "TQCB") -> Optional[dict]:
    """Строка MOEX history на дату ≤ d (ближайший торговый день, до 10 дн назад):
    {date, close, legalclose, accint, facevalue}. None — торгов не было/бумаги нет."""
    url = (f"https://iss.moex.com/iss/history/engines/stock/markets/bonds/"
           f"boards/{board}/securities/{secid}.json")
    params = {
        "iss.meta": "off",
        "from": (d - timedelta(days=10)).isoformat(),
        "till": d.isoformat(),
        "history.columns": "TRADEDATE,CLOSE,LEGALCLOSEPRICE,ACCINT,FACEVALUE",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=8)
        if resp.status_code != 200:
            return None
        h = resp.json().get("history", {})
        cols, data = h.get("columns", []), h.get("data", [])
        if not data:
            return None
        idx = {c: i for i, c in enumerate(cols)}
        row = data[-1]                              # последняя строка ≤ d
        g = lambda c: row[idx[c]] if c in idx else None
        return {
            "date": g("TRADEDATE"),
            "close": g("CLOSE"),
            "legalclose": g("LEGALCLOSEPRICE"),
            "accint": g("ACCINT"),
            "facevalue": g("FACEVALUE"),
        }
    except Exception as e:
        logger.warning(f"MOEX history fetch {secid}@{d}: {e}")
        return None


def _accrued_from_periods(periods, d: date, face: float) -> Optional[float]:
    """Фолбэк-НКД из графика купонов: value·(d−start)/(end−start) периода, где start≤d<end."""
    for c in periods or []:
        try:
            s = c[0] if isinstance(c, (tuple, list)) else c.get("start")
            e = c[1] if isinstance(c, (tuple, list)) else c.get("end")
            v = c[2] if isinstance(c, (tuple, list)) else c.get("value")
            s = date.fromisoformat(s) if isinstance(s, str) else s
            e = date.fromisoformat(e) if isinstance(e, str) else e
        except Exception:
            continue
        if s and e and v is not None and s <= d < e and e > s:
            return float(v) * (d - s).days / (e - s).days
    return None


# ------------------------------------------------------------------ контекст

async def load_backdate_ctx(isin: str, d: date, board: str = "TQCB") -> dict:
    """Тёплый as-of контекст: статика бумаги + факт-строка MOEX history на D +
    кривая as-of. Далее reprice_asof(ctx, price) — без I/O (батчится по ценам)."""
    from datetime import date as _date
    from services.market_data import MarketDataService
    from services.bonds import create_bond_ref_data, build_ref_external
    from services.paths import cache_path as _cache_path
    from services.valuation import _index_provider

    if d >= _date.today():
        raise CalculationException("Дата должна быть в прошлом (для сегодня — обычный калькулятор)")

    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    data = cache.get(isin)
    external = data is None

    async def _aempty():
        return {}

    res = await asyncio.gather(
        MarketDataService.fetch_coupon_schedules([isin]),                             # 0
        MarketDataService.get_curves(),                                               # 1
        MarketDataService.fetch_bond_schedule_full(isin),                             # 2
        fetch_history_row(isin, d, board),                                            # 3
        MarketDataService.fetch_moex_securities([isin]) if external else _aempty(),   # 4
        return_exceptions=True,
    )
    _ok = lambda x, dd: dd if isinstance(x, Exception) else x
    schedules = _ok(res[0], {})
    ruonia_curve, keyrate_curve, _cd, _rd = _ok(res[1], (None, None, None, None))
    sched_full = _ok(res[2], {"coupons": [], "amorts": []})
    hist_row = _ok(res[3], None)
    mo_map = _ok(res[4], {})

    if data:
        ref_obj = create_bond_ref_data(data, isin)
    else:
        mo = mo_map.get(isin, {})
        if not mo:
            raise NotFoundException(f"Bond {isin} not found on MOEX", {"isin": isin})
        ref_obj = build_ref_external(isin, mo)

    if ref_obj.maturity_date and ref_obj.maturity_date <= d:
        raise CalculationException(f"{isin} погашена до {d.isoformat()}")
    if ref_obj.issue_date and ref_obj.issue_date > d:
        raise CalculationException(f"{isin} размещена после {d.isoformat()}")

    today_curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    if today_curve is None:
        raise CalculationException("Текущая кривая недоступна — as-of сшить не из чего")

    warnings: list = []
    _index_pct_fn, hist_pairs = _index_provider(ref_obj.base, warnings, None)
    if not hist_pairs:
        raise CalculationException(f"история {ref_obj.base} недоступна")

    curve, curve_mode = curve_asof(ref_obj.base, d, today_curve, hist_pairs)

    periods = schedules.get(isin)
    amorts = sched_full.get("amorts")
    offers = sched_full.get("offers")

    # номинал на D: факт биржи; фолбэк — остаток по графику амортизаций на D
    face_asof = hist_row.get("facevalue") if hist_row else None
    if face_asof is None:
        from services.bonds import amort_remaining_face
        face_asof = amort_remaining_face(amorts, d)
    if face_asof is not None and abs(face_asof - ref_obj.face_value) > 0.5:
        ref_obj.face_value = float(face_asof)

    # НКД на D: факт биржи; фолбэк — линейное начисление из графика купонов
    accrued_asof = hist_row.get("accint") if hist_row else None
    if accrued_asof is None:
        accrued_asof = _accrued_from_periods(periods, d, ref_obj.face_value)
        if accrued_asof is not None:
            warnings.append("НКД на дату оценён из графика купонов (MOEX history не отдал ACCINT)")
    if accrued_asof is None:
        raise CalculationException(f"НКД на {d.isoformat()} не восстановился")

    if curve_mode == "realized":
        warnings.append("кривая as-of: реализованный факт индекса до сегодня + текущая "
                        "своп-кривая дальше (архив котировок дату не покрывает)")

    return {
        "isin": isin,
        "date": d,
        "ref_obj": ref_obj,
        "curve": curve,
        "curve_mode": curve_mode,
        "accrued": float(accrued_asof),
        "close": hist_row.get("close") if hist_row else None,
        "legalclose": hist_row.get("legalclose") if hist_row else None,
        "trade_date": hist_row.get("date") if hist_row else None,
        "periods": periods,
        "amorts": amorts,
        "offers": offers,
        "ctx_warnings": warnings,
    }


async def fetch_history_range(secid: str, d_from: date, d_till: date,
                              board: str = "TQCB") -> list:
    """Все дневные строки MOEX history за диапазон (пагинация start=): по датам
    возрастания, [{date, close, legalclose, accint, facevalue}, ...]."""
    url = (f"https://iss.moex.com/iss/history/engines/stock/markets/bonds/"
           f"boards/{board}/securities/{secid}.json")
    out, start = [], 0
    try:
        async with httpx.AsyncClient() as client:
            while True:
                params = {
                    "iss.meta": "off", "start": start,
                    "from": d_from.isoformat(), "till": d_till.isoformat(),
                    "history.columns": "TRADEDATE,CLOSE,LEGALCLOSEPRICE,ACCINT,FACEVALUE",
                }
                resp = await client.get(url, params=params, timeout=10)
                if resp.status_code != 200:
                    break
                h = resp.json().get("history", {})
                cols, data = h.get("columns", []), h.get("data", [])
                if not data:
                    break
                idx = {c: i for i, c in enumerate(cols)}
                for row in data:
                    g = lambda c: row[idx[c]] if c in idx else None
                    out.append({"date": g("TRADEDATE"), "close": g("CLOSE"),
                                "legalclose": g("LEGALCLOSEPRICE"),
                                "accint": g("ACCINT"), "facevalue": g("FACEVALUE")})
                if len(data) < 100:
                    break
                start += len(data)
    except Exception as e:
        logger.warning(f"MOEX history range fetch {secid}: {e}")
    return out


_honest_memo: dict = {}     # (isin, days, board) → (msk_day, result); прошлое не меняется,
                            # хвост realized-кривой обновляется раз в день — TTL сутки


async def honest_spread_series(isin: str, days: int = 180, board: str = "TQCB") -> dict:
    """Честная динамика спредов: для КАЖДОГО торгового дня — свой calc_date,
    своя as-of кривая, фактические НКД/номинал/close того дня → SM/DM/y-idx.
    В отличие от candle-оценки (историч. цена × сегодняшняя модель) серия не
    зависит от сегодняшних НКД/срока; хвост кривой за «сегодня» — текущий рынок.
    Расчёт ~15с на 120 дней → мемо на день."""
    from datetime import date as _date, timedelta as _td
    key = (isin, days, board)
    hit = _honest_memo.get(key)
    if hit and hit[0] == _date.today():
        return hit[1]
    d_till = _date.today() - _td(days=1)
    d_from = d_till - _td(days=int(days * 1.55) + 7)   # запас на выходные

    ctx = await load_backdate_ctx(isin, d_till, board)  # статика + кривая на d_till
    rows = await fetch_history_range(isin, d_from, d_till, board)
    rows = [r for r in rows if r.get("close") is not None][-days:]

    from services.valuation import calculate_valuation_metrics
    ref = ctx["ref_obj"]
    from services.valuation import _index_provider
    warnings: list = []
    _fn, hist_pairs = _index_provider(ref.base, warnings, None)
    from services.market_data import MarketDataService
    _r, _k, _cd, _rd = await MarketDataService.get_curves()
    today_curve = _r if ref.base == "RUONIA" else _k

    points = []
    for r in rows:
        try:
            d = _date.fromisoformat(r["date"])
        except (TypeError, ValueError):
            continue
        try:
            curve, mode = curve_asof(ref.base, d, today_curve, hist_pairs)
            if r.get("facevalue"):
                ref.face_value = float(r["facevalue"])
            accint = r.get("accint")
            if accint is None:
                accint = _accrued_from_periods(ctx["periods"], d, ref.face_value)
            m = calculate_valuation_metrics(
                ref, r["close"], curve, d, accrued_override=accint,
                periods=ctx["periods"], amorts=ctx["amorts"], offers=ctx["offers"])
            points.append({
                "date": r["date"], "price": r["close"],
                "sm_bps": m.get("sm_bps"), "dm_bps": m.get("disc_margin_bps"),
                "ytm": m.get("yield_xirr_pct"),
                "y_idx_bps": m.get("yield_over_index_bps"),
                "curve_mode": mode, "src": "honest",
            })
        except Exception as e:
            logger.debug(f"honest point {isin}@{r['date']}: {e}")
    result = {"isin": isin, "points": points, "warnings": ctx["ctx_warnings"]}
    _honest_memo[key] = (_date.today(), result)
    return result


def reprice_asof(ctx: dict, price: float) -> dict:
    """Чистая цена на прошлую дату → полный набор метрик (SM/DM/y-idx/YTM/dirty).
    Тот же движок, что live-таблица; calc_date = прошлая дата, входы as-of."""
    from services.valuation import calculate_valuation_metrics
    m = calculate_valuation_metrics(
        ctx["ref_obj"], price, ctx["curve"], ctx["date"],
        accrued_override=ctx["accrued"], periods=ctx["periods"],
        amorts=ctx["amorts"], offers=ctx["offers"],
    )
    m["warnings"] = sorted(set((m.get("warnings") or []) + ctx["ctx_warnings"]))
    m["curve_mode"] = ctx["curve_mode"]
    return m
