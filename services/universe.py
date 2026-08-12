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
from core.valuation import next_offer_info
from services.zspread import compute_z_bps
from services import metrics
from services import instruments_registry

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
                calc_date: date, prev_date: Optional[str] = None,
                bid: Optional[float] = None, ask: Optional[float] = None) -> dict:
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
    # остаток из графика амортизаций авторитетнее кэша (стейл-кэш завышал
    # dirty/SM/DM амортизируемых бумаг — БалтЛизП10 1000 vs 900)
    from services.bonds import amort_remaining_face
    _rem = amort_remaining_face(amorts, calc_date)
    if _rem is not None and abs(_rem - ref.face_value) > 0.5:
        ref.face_value = _rem

    periods = []
    for c in coupons_full:
        try:
            periods.append((date.fromisoformat(c["start"]), date.fromisoformat(c["end"]),
                            c.get("value")))
        except Exception:
            pass

    delta = round(last - float(prev), 4) if (last is not None and prev is not None) else None
    # цена для расчёта: live/сделка сегодня → prev-close (не зависим от момента WS-цены)
    price_calc = last if last is not None else prev
    # отображаемая цена: реальная (live/сделка) или prev-close как fallback (нет
    # сделок сегодня / бумага не в Alor-потоке) → строка не пустует, но помечена
    px_display = last if last is not None else prev
    price_stale = last is None and prev is not None

    curve = ruonia_curve if base == "RUONIA" else keyrate_curve
    dirty = dm = disc_dm = z_model = yoi = ytm = base_ytm = None
    yoi_bid = yoi_ask = None
    face_px = accrued_settle = yoi_slope = None
    implausible = False
    hz, off_d, sm_off, dm_off = "maturity", None, None, None
    # Маркер оферты для таблицы (даты из MOEX bondization). offertype у MOEX
    # колл не различает — на всём универсе только 'Оферта'/'Оферта (состоялось)'/
    # 'Оферта/Погашение', поэтому kind тут практически всегда 'put'. Call приходит
    # отдельным фактом из реестра (has_call, источник corpbonds) и БЕЗ даты, так
    # что это независимый флаг, а не переклассификация этой даты: у бумаги могут
    # быть и пут-оферта, и колл-опцион одновременно.
    # put-горизонт из valuation приоритетен — он согласован с sm_to_offer.
    # Считается и без цены/кривых — флаг справочный.
    off_kind = None
    # maturity — чтобы техническая запись «Оферта/Погашение» на дату погашения
    # не ставила ложный маркер p (у бумаги нет опциона, см. next_offer_info)
    _next_off = next_offer_info(offers, calc_date, ref.maturity_date)
    # ±0.5пп вокруг расчётной цены — численная производная Y-IDX по цене (bps на
    # 1пп). Ею фронт переводит VWAP-цену тикета в Y-IDX, не гоняя reprice на 540
    # бумаг: Y-IDX(цена) на масштабе стакана (доли пп) практически линеен.
    _dp = 0.5
    _probe = ([round(price_calc - _dp, 4), round(price_calc + _dp, 4)]
              if price_calc is not None else [])
    if price_calc is not None and curve and base in ("RUONIA", "KEYRATE"):
        try:
            m = calculate_valuation_metrics(ref, price_calc, curve, calc_date,
                                            accrued_override=accrued,
                                            periods=periods or None,
                                            amorts=amorts, offers=offers,
                                            ruonia_curve=ruonia_curve,
                                            alt_prices=[p for p in (bid, ask) if p] + _probe)
            # ГОРИЗОНТ ПРАЙСИНГА по правилу цены (services.valuation._preferred_horizon):
            # цена ниже цены пут-выкупа → бумага торгуется к оферте, выше цены
            # call-выкупа → к коллу, иначе к погашению. Колонки таблицы (Y-IDX/YTM/
            # SM/DM и Y-IDX стакана) берутся из ВЫБРАННОГО горизонта — иначе скринер
            # сравнивал бы бумагу с офертой через год по потоку на десять лет.
            hz = m.get("preferred_horizon", "maturity")
            _hzm = (m.get("horizons") or {}).get(hz) or {}
            dirty = m.get("dirty_price_rub")
            dm = _hzm.get("sm_bps", m.get("dm_bps"))
            disc_dm = _hzm.get("disc_margin_bps", m.get("disc_margin_bps"))
            yoi = _hzm.get("yield_over_index_bps", m.get("yield_over_index_bps"))
            # Y-IDX по верху стакана: покупка по ask, продажа по bid (тот же поток)
            _alt = _hzm.get("y_idx_by_price") or m.get("y_idx_by_price") or {}
            yoi_bid, yoi_ask = _alt.get(bid), _alt.get(ask)
            # номинал и НКД РОВНО те, из которых собран dirty (амортизация учтена,
            # НКД на дату поставки) — фронт считает ими деньги уровня стакана
            face_px, accrued_settle = m.get("pricing_face_rub"), m.get("accrued_settle_rub")
            _lo, _hi = _alt.get(_probe[0]), _alt.get(_probe[1])
            if _lo is not None and _hi is not None:
                yoi_slope = round((_hi - _lo) / (2 * _dp), 2)
            ytm = _hzm.get("yield_xirr_pct", m.get("yield_xirr_pct"))
            base_ytm = _hzm.get("index_yield_pct", m.get("index_yield_pct"))
            implausible = bool(m.get("price_implausible"))
            off_d = m.get("offer_date")
            off_kind = "call" if hz == "call" else ("put" if off_d is not None else None)
            sm_off, dm_off = m.get("sm_to_offer_bps"), m.get("disc_margin_to_offer_bps")
        except Exception as e:
            logger.warning(f"valuation error {isin}: {e}")
    if off_d is not None and off_kind is None:
        off_kind = "put"
    elif off_d is None and _next_off is not None:
        off_d, off_kind = _next_off[0], _next_off[2]

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

    refix = cur_cpn = None
    try:
        cpns = coupons_full or [{"start": s.isoformat(), "end": e.isoformat(), "value": v}
                                for (s, e, v) in periods]
        cb = metrics.carry_refix_block(cpns, amorts, ref.face_value, price_calc,
                                       exp, u.get("current_yield_pct"), calc_date)
        refix, cur_cpn = cb["days_to_refix"], cb["current_coupon_pct"]
    except Exception as e:
        logger.warning(f"refix block error {isin}: {e}")

    # тонкая цена: PREVDATE (дата последней цены MOEX) старше 4 дней → бумага не
    # торговалась, цена несвежая, DM/z с ненадёжной цены. Возраст PREVDATE, а не
    # NUMTRADES: последний по выходным=0 у ВСЕХ (рынок закрыт) → ложный флаг.
    price_thin = False
    if prev_date:
        try:
            price_thin = (calc_date - date.fromisoformat(prev_date)).days > 4
        except (ValueError, TypeError):
            price_thin = False
    # Амортизация: в графике MOEX больше одного транша погашения номинала
    # (единственная запись — это обычное погашение в конце). Признак статичный,
    # в WS-патч не уходит; нужен фильтру «без аморт» на фронте.
    has_amort = sum(1 for a in (amorts or []) if a.get("value") is not None) > 1
    return {"last": px_display, "dirty": dirty, "dm": dm, "disc_dm": disc_dm, "yoi": yoi, "delta": delta,
            "has_amort": has_amort,
            "bid": bid, "ask": ask, "yoi_bid": yoi_bid, "yoi_ask": yoi_ask,
            "face_px": face_px, "accrued_settle": accrued_settle, "yoi_slope": yoi_slope,
            "ytm": ytm, "base_ytm": base_ytm, "price_stale": price_stale,
            "next_coupon": next_cpn, "z_model": z_model,
            "refix": refix, "current_coupon": cur_cpn, "implausible": implausible,
            "price_thin": price_thin,
            # has_call сюда НЕ кладём: он статичный факт строки реестра, его
            # читает _uni_item прямо из u — не плодим второй источник той же правды
            # (эти метрики ещё и уходят в WS-патч, где место только цено-зависимым).
            "horizon": hz, "offer_date": off_d, "offer_kind": off_kind,
            "sm_to_offer": sm_off, "dm_to_offer": dm_off}


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

    # только цены текущего торгового дня: вчерашняя WS-цена не должна выигрывать
    # у сегодняшнего board-last (приоритет по свежести, не по источнику)
    prices = MarketDataService.session_prices()
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
    await asyncio.to_thread(MarketDataService.flush_schedule_cache)   # дозапись хвоста дебаунс-кэша
    full_by = dict(zip(ids, fulls))

    # Дальше сети нет — только счёт по ~540 бумагам и запись в реестр. В event
    # loop этот блок держал ядро десятками секунд каждые 10 минут (замер на
    # проде: 33с непрерывного CPU), и всё это время сервер не отвечал никому.
    # Уносим в поток: соединения SQLite открываются внутри вызовов, шаринга
    # между потоками нет, запись прикрыта своим threading.Lock.
    def _crunch() -> dict:
        out: dict = {}
        for isin in ids:
            u = uni_by[isin]
            snap = board.get(isin, {})
            ref = build_universe_ref(u, isin, cache, secs)
            out[isin] = enrich_bond(
                u, ref, full_by.get(isin) or {},
                last=prices.get(isin) or snap.get("last"), prev=snap.get("prev"),
                accrued=snap.get("accrued"), prev_date=snap.get("prev_date"),
                bid=snap.get("bid"), ask=snap.get("ask"),
                ruonia_curve=ruonia_curve, keyrate_curve=keyrate_curve,
                exp_ks=exp_ks, exp_ru=exp_ru, g_curve=g_curve, calc_date=calc_date)
            out[isin]["val_today"] = snap.get("vol")   # оборот сегодня, ₽ (board snapshot)
            out[isin]["wap"] = snap.get("waprice")     # средневзвес дня, % (WAPRICE)

        # backfill coupon_period_days из ФАКТИЧЕСКОГО графика (два последних купона /
        # размещение+первый) — точнее номинального round(365/freq). Схемы уже в руках
        # (fulls, day-кэш), без доп. сети. Пишем только при расхождении; manual-locked
        # строки upsert не трогает (coupon_period_days ∈ _MANUAL_FIELDS).
        try:
            from core.cashflow import coupon_period_from_coupons
            for isin in ids:
                cps = (full_by.get(isin) or {}).get("coupons") or []
                cpd = coupon_period_from_coupons(
                    cps, issue_date=uni_by[isin].get("issue_date"), today=calc_date)
                if not cpd or cpd <= 0:
                    continue
                cur = instruments_registry.get(isin) or {}
                # locked-строку upsert всё равно не тронет (coupon_period_days ∈
                # _MANUAL_FIELDS) — не дёргаем БД вхолостую каждый цикл
                if cur.get("manual_locked"):
                    continue
                if cpd != cur.get("coupon_period_days"):
                    instruments_registry.upsert(
                        {"isin": isin, "coupon_period_days": cpd},
                        source="moex", mark_new=False, keep_source=True)
        except Exception as e:
            logger.warning(f"coupon_period backfill error: {e}")
        return out

    from services.heavy import run_heavy
    return await run_heavy(_crunch)


async def compute_watch_metrics(uni_rows: List[dict], cache: dict) -> dict:
    """Live-метрики для watchlist-бумаг: цена из кэша поллера/WS, per-isin
    snapshot MOEX (точный prev/НКД, работает и для бумаг вне TQCB). {isin: метрики}."""
    import asyncio
    ids = [u["isin"] for u in uni_rows if u.get("isin")]
    if not ids:
        return {}
    external = [i for i in ids if i not in cache]
    prices = MarketDataService.session_prices()   # см. compute_universe_metrics
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
            bid=snap.get("bid"), ask=snap.get("ask"),
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
