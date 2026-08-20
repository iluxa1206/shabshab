"""Календарь выплат по всему юниверсу флоатеров: будущие купоны и погашения
(амортизации + финал) в деньгах на одну бумагу.

Будущий поток — из КАНОНИЧЕСКОГО build_cashflows_with_spread (тот же, что
карточка/SM/DM): проекция незафиксированных купонов форвардом, кэп/флор,
амортизация, residual. Зафиксированные купоны MOEX (value не null) помечаются
projected=False, спроецированные — True.

Полный пересчёт по 500+ бумагам — чистая математика на day-кэше расписаний,
но не бесплатная → результат кэшируется в памяти на (calc_date). Окно from/to
режется уже по кэшу на route-слое.
"""
import asyncio
import logging
import os
from datetime import date, timedelta
from typing import List, Optional

from services.market_data import MarketDataService
from services.universe import build_universe_ref, _aempty
from services import instruments_registry

logger = logging.getLogger(__name__)

_cache = {"key": None, "events": [], "calc_date": None}
_lock = asyncio.Lock()

# Сколько дней НАЗАД календарь помнит уже выплаченное. Канонический билдер
# строит поток только вперёд от даты поставки, а «что платили вчера» — такой же
# рабочий вопрос, как «что заплатят завтра» (плашка выплат в нижней строке).
# Прошлое берётся ФАКТОМ MOEX (value не null), без проекции.
PAST_WINDOW_DAYS = int(os.getenv("CALENDAR_PAST_DAYS", "7"))


def _d(s) -> Optional[date]:
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def _bond_events(ref, u: dict, name: str, curve, calc_date: date,
                 full: dict, index_pct_fn) -> List[dict]:
    """Будущие платежи одной бумаги. Канонический builder; на экзотике/ошибке —
    фолбэк на сырой график MOEX (зафиксированные купоны + амортизации + финал)."""
    from core.valuation import build_cashflows_with_spread, settle_date
    settle = settle_date(calc_date)
    isin = ref.isin
    coupons = full.get("coupons") or []
    amorts = full.get("amorts") or []
    offers = full.get("offers") or []
    # факт MOEX: end → value (не null = купон зафиксирован)
    known = {c.get("end"): c.get("value") for c in coupons}

    base = {"isin": isin, "name": name, "emitter": u.get("emitter_name") or "—",
            "base": ref.base}
    out: List[dict] = []

    # Ближний хвост потока ФАКТОМ MOEX — то, чего канонический билдер не даёт:
    # он строит поток строго ПОСЛЕ даты поставки (купон, платящийся в settle,
    # для оценки принадлежит продавцу через НКД). Для календаря такие выплаты
    # нужны, как и уже прошедшие за последние дни.
    # paid ставится по СЕГОДНЯ, а не по settle: завтрашний купон — ещё не выплата.
    past_lo = calc_date - timedelta(days=PAST_WINDOW_DAYS)
    for c in coupons:
        end = _d(c.get("end"))
        if not end or not (past_lo <= end <= settle) or c.get("value") is None:
            continue
        vp = c.get("valueprc")
        out.append({
            **base, "date": end, "type": "COUPON",
            "amount_rub": round(float(c["value"]), 2),
            "rate_pct": round(float(vp), 4) if vp is not None else None,
            "projected": False, "paid": end <= calc_date,
        })
    for a in amorts:
        d = _d(a.get("date"))
        if not d or a.get("value") is None or not (past_lo <= d <= settle):
            continue
        out.append({**base, "date": d, "type": "REDEMPTION",
                    "amount_rub": round(float(a["value"]), 2),
                    "rate_pct": None, "projected": False, "paid": d <= calc_date})

    cfs = None
    if curve is not None and ref.base in ("KEYRATE", "RUONIA"):
        try:
            periods = [(c.get("start"), c.get("end"), c.get("value")) for c in coupons]
            cfs = build_cashflows_with_spread(
                ref, curve, calc_date, ref.spread_issue_bps or 0,
                explicit_periods=periods or None, amorts=amorts, offers=offers,
                index_pct_fn=index_pct_fn,
            )
        except Exception as e:
            logger.warning(f"calendar builder failed {isin}: {e}")
            cfs = None

    if cfs is not None:
        for cf in cfs:
            is_cpn = cf.type == "COUPON"
            end_iso = (cf.period_end or cf.pay_date).isoformat() if is_cpn else None
            out.append({
                **base, "date": cf.pay_date, "type": cf.type,
                "amount_rub": round(cf.amount_rub, 2),
                "rate_pct": cf.coupon_rate_pct if is_cpn else None,
                "projected": bool(is_cpn and known.get(end_iso) is None),
            })
        return out

    # фолбэк (экзотика ИПЦ/GCurve и пр.): только факт MOEX, без проекции.
    # degraded=True — событие построено МИМО канонического билдера (без лесенки
    # маржи/кэпа/прогноза) и может расходиться с карточкой; фронт может пометить.
    for c in coupons:
        end = _d(c.get("end"))
        if not end or end <= settle or c.get("value") is None:
            continue
        vp = c.get("valueprc")
        face = ref.face_value or 0
        days = None
        s = _d(c.get("start"))
        if s and (end - s).days:
            days = (end - s).days
        # ставка: valueprc, иначе восстановление из суммы (как в карточке)
        rate = (float(vp) if vp is not None
                else (float(c["value"]) / face * 365.0 / days * 100.0
                      if face and days else None))
        out.append({
            **base, "date": end, "type": "COUPON",
            "amount_rub": round(float(c["value"]), 2),
            "rate_pct": round(rate, 4) if rate is not None else None,
            "projected": False, "degraded": True,
        })
    rem = ref.face_value
    for a in amorts:
        d = _d(a.get("date"))
        if not d or a.get("value") is None:
            continue
        if d > settle:
            out.append({**base, "date": d, "type": "REDEMPTION",
                        "amount_rub": round(float(a["value"]), 2),
                        "rate_pct": None, "projected": False})
    # финальное погашение, если его нет в amorts
    mat = ref.maturity_date
    if mat and mat > settle and not any(_d(a.get("date")) == mat for a in amorts):
        out.append({**base, "date": mat, "type": "REDEMPTION",
                    "amount_rub": round(rem, 2), "rate_pct": None, "projected": False})
    return out


async def build_payments_calendar() -> dict:
    """Все будущие платежи юниверса. {calc_date, events:[...]} — кэш на день."""
    from services.paths import cache_path as _cache_path

    ruonia_curve, keyrate_curve, cd, rd = await MarketDataService.get_curves()
    calc_date = cd or rd or date.today()
    # data_version в ключе: правка Справочника (маржа/спека/даты) пересобирает
    # календарь сразу, а не после смены торговой даты
    key = f"{calc_date.isoformat()}:{instruments_registry.data_version()}"
    if _cache["key"] == key:
        return {"calc_date": _cache["calc_date"], "events": _cache["events"]}

    async with _lock:
        if _cache["key"] == key:   # догнал параллельный билд
            return {"calc_date": _cache["calc_date"], "events": _cache["events"]}

        uni = await instruments_registry.fetch_floater_universe()
        cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
        ids = [u["isin"] for u in uni if u.get("isin")]
        external = [i for i in ids if i not in cache]
        shortnames, secs, sizes, fulls = await asyncio.gather(
            MarketDataService.fetch_moex_shortnames(),
            MarketDataService.fetch_moex_securities(external) if external else _aempty(),
            MarketDataService.fetch_issue_sizes(),
            asyncio.gather(*(MarketDataService.fetch_bond_schedule_full(i) for i in ids)),
        )
        await asyncio.to_thread(MarketDataService.flush_schedule_cache)
        full_by = dict(zip(ids, fulls))

        # индекс-провайдеры фиксинга начавшегося периода — один на базу
        index_fns = {}
        for b in ("KEYRATE", "RUONIA"):
            try:
                from functools import partial
                from services.coupon_calib import period_index_pct as _pip, index_history
                index_fns[b] = partial(_pip, idx=index_history(b))
            except Exception:
                index_fns[b] = None

        events: List[dict] = []
        for u in uni:
            isin = u["isin"]
            try:
                ref = build_universe_ref(u, isin, cache, secs)
                name = shortnames.get(isin) or u.get("name") or isin
                curve = ruonia_curve if ref.base == "RUONIA" else keyrate_curve
                evs = _bond_events(
                    ref, u, name, curve, calc_date,
                    full_by.get(isin) or {}, index_fns.get(ref.base))
                # объём выплаты держателям всего по выпуску: ₽/бумага × штук
                size = sizes.get(isin)
                for e in evs:
                    e["total_rub"] = round(e["amount_rub"] * size, 2) if size else None
                events.extend(evs)
            except Exception as e:
                logger.warning(f"calendar skip {isin}: {e}")
        events.sort(key=lambda e: (e["date"], e["emitter"], e["isin"]))

        _cache.update(key=key, events=events, calc_date=calc_date)
        return {"calc_date": calc_date, "events": events}
