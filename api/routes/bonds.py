import asyncio
import os
import re
import logging
from datetime import date
from typing import Optional, Literal, Dict
from fastapi import APIRouter, Query, Path, HTTPException

from api.schemas import (
    BondListItem, BondListResponse, BondFiltersResponse,
    BondDetailsResponse, CashflowResponse,
    RepriceResponse, BondAuditResponse, CouponDaysResponse,
    PaymentsCalendarResponse,
)
from services.market_data import MarketDataService
from services.bonds import (
    create_bond_ref_data, build_ref_external, external_formula, next_coupon_after,
    coupons_per_year as _coupons_per_year,
)
from services.valuation import calculate_valuation_metrics
from services.exceptions import NotFoundException
from services import instruments_registry
from services import live_quotes
from services.paths import cache_path as _cache_path
from core.cashflow import read_isins_from_file

logger = logging.getLogger(__name__)

router = APIRouter()

# ISO 6166; тот же паттерн, что в funds. Валидируем ВСЕ входные ISIN: они
# интерполируются в URL к MOEX/Alor f-строками — мусор/`..%2F` не должен уходить
# во внешние запросы.
_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_SECID_RE = re.compile(r"[A-Z0-9]{4,20}")


def _require_isin(isin: str) -> str:
    v = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(v):
        raise HTTPException(status_code=422, detail=f"Невалидный ISIN: {isin!r}")
    return v


def get_base_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE_LABEL = {"KEYRATE": "Ключевая ставка", "RUONIA": "RUONIA"}
# ОФЗ-ПК: суверен Минфина. Эмитент в реестре — авторитет, имя выпуска (ОФЗ 29xxx /
# SU29…) — фолбэк для строк без emitter_name. Субфеды («Минфин Амурской обл.»,
# «Амур 24001») сюда НЕ попадают: для витрины это корпоративный риск.
def _is_ofz(u: dict, name: str) -> bool:
    """Выпуск Минфина — ОДНО правило на проект (screener_core.is_ofz).

    Своя регулярка тут была уже, «ОФЗ или SU2…», и она не знала ни SECID-улики,
    ни серий SU3…/SU4…: чип ОФЗ/КОРП в мониторе и фильтр «только ОФЗ» в
    сигналах/портфеле/боте отбирали РАЗНЫЕ множества."""
    from services.screener_core import is_ofz
    return is_ofz(dict(u, name=name or u.get("name")))


async def compute_universe_metrics(uni: list, isins: list) -> dict:
    """Прокси в services.universe (конвейер вынесен из route-слоя)."""
    from services.universe import compute_universe_metrics as _cum
    return await _cum(uni, isins, _cache_path("isins_cache.json"))


def _uni_item(u, name, mx, adv=None, avg7=None,
              vol_bid=None, vol_ask=None):
    """BondListItem: строка универса реестра + наши метрики mx (universe.enrich_bond).

    Спред-дюрация приходит ТОЛЬКО из движка (mx["spread_dur"] — Macaulay потока
    выбранного горизонта, services.valuation._dur_block). Суррогат «нет дюрации →
    подставим срок до погашения» убран: на оси графика аналитики он смешивал две
    разные метрики, и точка бумаги с офертой стояла на сроке погашения рядом со
    своим спредом к оферте.""" 
    base = u.get("base_rate_type", "UNKNOWN")
    spread = u.get("spread_issue_bps") or 0
    label = _BASE_LABEL.get(base, base)
    if u.get("face_index"):
        # У линкера складывать базу со спредом нельзя: ставка купона
        # ФИКСИРОВАНА и равна этому «спреду», а по базе индексируется номинал.
        formula = f"номинал по {label}, купон {spread / 100:g}%"
    else:
        formula = f"{label} + {spread / 100:g}%" if spread else label
    last = mx.get("last")
    return BondListItem(
        isin=u["isin"], short_name=name, base_rate_type=base, formula=formula,
        face_index=u.get("face_index"),
        spread_issue_bps=int(spread),
        coupons_per_year=_coupons_per_year(u.get("coupon_period_days"),
                                           u.get("coupons_per_year")),
        maturity_date=u.get("maturity_date"),
        next_coupon_date=mx.get("next_coupon"), last_price_pct=last,
        bid_price_pct=mx.get("bid"), ask_price_pct=mx.get("ask"),
        y_idx_bid_bps=mx.get("yoi_bid"), y_idx_ask_bps=mx.get("yoi_ask"),
        face_value_rub=mx.get("face_px"), accrued_rub=mx.get("accrued_settle"),
        y_idx_slope_bps_per_pct=mx.get("yoi_slope"),
        dirty_price_rub=mx.get("dirty"), dm_bps=mx.get("dm"),
        wap_price_pct=mx.get("wap"), y_idx_wap_bps=mx.get("yoi_wap"),
        **_vol_fields(mx, vol_bid, vol_ask),
        val_today=mx.get("val_today"), adv_1m_rub=adv,
        delta_to_prev_close=mx.get("delta"), disc_margin_bps=mx.get("disc_dm"),
        yield_xirr_pct=mx.get("ytm"), index_yield_pct=mx.get("base_ytm"),
        yield_over_index_bps=mx.get("yoi"), y_idx_avg7_bps=avg7,
        price_implausible=mx.get("implausible") or False,
        price_thin=mx.get("price_thin") or False, price_stale=mx.get("price_stale") or False,
        emitter_id=u.get("emitter_id"), emitter_name=u.get("emitter_name"),
        rating=u.get("rating"), ratings_ea=u.get("ratings_ea"),
        z_model_bps=mx.get("z_model"), spread_dur_yrs=mx.get("spread_dur"),
        days_to_refix=mx.get("refix"), current_coupon_pct=mx.get("current_coupon"),
        preferred_horizon=mx.get("horizon") or "maturity", offer_date=mx.get("offer_date"),
        offer_kind=mx.get("offer_kind"), has_call=u.get("has_call"),
        is_ofz=_is_ofz(u, name), has_amort=bool(mx.get("has_amort")),
        sm_to_offer_bps=mx.get("sm_to_offer"), disc_margin_to_offer_bps=mx.get("dm_to_offer"),
    )


async def _universe_bonds(extra_list, cache, limit, offset,
                          vol_bid=None, vol_ask=None):
    """Весь рынок флоатеров из реестра инструментов. Аналитика по всем;
    live-метрики — только для watchlist (extra). Расчёты в services.universe."""
    from services import universe as universe_svc
    uni = await instruments_registry.fetch_floater_universe()
    if not uni:
        return BondListResponse(items=[], total=0, limit=limit, offset=offset)

    cached_prices = MarketDataService.session_prices()   # цены текущего торгового дня
    uni_metrics = MarketDataService.universe_metrics()  # фоновый поллер
    shortnames = await MarketDataService.fetch_moex_shortnames()
    watch = set(extra_list)

    watch_rows = [u for u in uni if u.get("isin") in watch]
    watch_metrics = await universe_svc.compute_watch_metrics(watch_rows, cache) if watch_rows else {}
    # средний дневной оборот за месяц: один запрос по всему рынку, в памяти на
    # 15 минут (SQLite синхронный — читаем в потоке, не в event loop)
    from services import bars as bars_svc
    try:
        adv = await asyncio.to_thread(bars_svc.adv_map, 30)
    except Exception as e:
        logger.warning("adv_map failed: %s", e)
        adv = {}
    # база спреда за прошлую неделю: колонка «отклонение» сравнивает с ней
    # текущий Y-IDX. Тот же профиль — один запрос, кэш в памяти на 15 минут.
    try:
        avg7 = await asyncio.to_thread(bars_svc.spread_avg_map, 7)
    except Exception as e:
        logger.warning("spread_avg_map failed: %s", e)
        avg7 = {}

    # рейтинги по агентствам (Эксперт/АКРА) — из durable-кэша слоя, без сети
    try:
        from services import ratings_br
        ea = ratings_br.ea_map([u["isin"] for u in uni])
    except Exception as e:
        logger.warning("ratings_br ea_map failed: %s", e)
        ea = {}

    items = []
    for u in uni:
        isin = u["isin"]
        u["ratings_ea"] = ea.get(isin)
        name = shortnames.get(isin) or u.get("name") or isin
        mx = watch_metrics.get(isin) or uni_metrics.get(isin)
        if mx is None:
            mx = {"last": cached_prices.get(isin)}
        items.append(_uni_item(u, name, mx, adv.get(isin),
                               avg7.get(isin), vol_bid, vol_ask))
    return BondListResponse(items=items[offset:offset + limit], total=len(items), limit=limit, offset=offset)


def _vol_fields(mx: dict, vol_bid: Optional[float], vol_ask: Optional[float]) -> dict:
    """Цена набора тикета и её Y-IDX — из чисел, посчитанных движком в его такте.
    Здесь ничего не считается: своя арифметика в ручке разъехалась бы с движком."""
    out: dict = {}
    px_map, y_map = mx.get("vol_px") or {}, mx.get("yoi_vol") or {}
    for side, size in (("bid", vol_bid), ("ask", vol_ask)):
        if not size:
            continue
        key = f"{side}:{float(size):.0f}"
        out[f"vol_{side}_price_pct"] = px_map.get(key)
        out[f"y_idx_vol_{side}_bps"] = y_map.get(key)
    return out


@router.get("", response_model=BondListResponse, tags=["Bonds"])
async def get_bonds(
    limit: int = Query(50, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    with_market: bool = Query(True),
    with_valuation: bool = Query(False),
    universe: bool = Query(False, description="Весь юниверс флоатеров из реестра"),
    extra: Optional[str] = Query(None, description="Доп. ISIN'ы (через запятую) — любые бумаги вне списка"),
    fields: Optional[str] = Query(None),
    vol_bid: Optional[float] = Query(None, description="Тикет на биде, ₽ — вернуть цену набора и её Y-IDX"),
    vol_ask: Optional[float] = Query(None, description="Тикет на оффере, ₽"),
    cols: Optional[str] = Query(None, description="Видимые колонки через запятую — "
                                                  "движок не считает то, чего никто не видит")
):
    base_dir = get_base_dir()
    isins_path = os.path.join(base_dir, "isins.txt")
    cache_path = _cache_path("isins_cache.json")

    try:
        isins = read_isins_from_file(isins_path)
    except Exception:
        isins = []

    extra_list = [x.strip().upper() for x in (extra.split(",") if extra else []) if x.strip()]
    extra_list = [x for x in extra_list if _ISIN_RE.fullmatch(x)]  # мусор молча отбрасываем
    cache = MarketDataService.get_local_bond_cache(cache_path)

    if universe:
        # размеры тикета регистрируем В ДВИЖКЕ: он посчитает Y-IDX цены набора в
        # своём такте по методике, а ручка только выберет нужный размер. Считать
        # здесь нельзя — это 13 мс на бумагу, то есть секунды на весь универс.
        if vol_bid or vol_ask:
            from services.universe_stream import register_vol_sizes
            register_vol_sizes([v for v in (vol_bid, vol_ask) if v])
        # ВИДИМЫЕ КОЛОНКИ — туда же и по той же причине: спреды сторон считает
        # отдельная очередь, и при выключенных колонках она забирала такт у
        # того, что на экране действительно есть.
        if cols:
            from services.universe_stream import register_metric_scope
            register_metric_scope([c.strip() for c in cols.split(",") if c.strip()])
        return await _universe_bonds(extra_list, cache, limit, offset, vol_bid, vol_ask)

    # добавленные пользователем бумаги (watchlist) — в начало, чтобы были видны
    base_set = set(isins)
    all_isins = [e for e in extra_list if e not in base_set] + isins

    total = len(all_isins)
    paginated_isins = all_isins[offset:offset + limit]

    external = [i for i in paginated_isins if i not in cache]

    market_prices = {}
    prev_close_prices = {}
    ruonia_curve = keyrate_curve = calc_date = rates_date = None
    moex_snapshot = {}
    moex_ref = {}
    schedules = {}

    if with_market or with_valuation:
        market_prices = await MarketDataService.fetch_last_prices(paginated_isins)
        moex_snapshot = await MarketDataService.fetch_moex_snapshot(paginated_isins)
        prev_close_prices = {i: v["prev"] for i, v in moex_snapshot.items() if v.get("prev") is not None}
        ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()

    if with_valuation:
        schedules = await MarketDataService.fetch_coupon_schedules(paginated_isins)

    if external:
        moex_ref = await MarketDataService.fetch_moex_securities(external)

    if not calc_date:
        calc_date = rates_date or date.today()

    items = []

    for isin in paginated_isins:
        data = cache.get(isin)
        if data:
            ref_obj = create_bond_ref_data(data, isin)
            short_name = data.get("SHORTNAME", "")
            formula = data.get("FORMULA", "")
        else:
            # внешняя бумага: справочник MOEX + база/спред из Cbonds-справки
            ref_obj = build_ref_external(isin, moex_ref.get(isin, {}))
            short_name = (moex_ref.get(isin) or {}).get("name") or isin
            formula = external_formula(ref_obj)

        last_price_pct = prev_close_pct = dirty_price_rub = dm_bps = delta_to_prev_close = None
        yield_xirr_pct = index_yield_pct = None

        if with_market:
            last_price_pct = market_prices.get(isin)
            prev_close_pct = prev_close_prices.get(isin) or (moex_ref.get(isin) or {}).get("prev")
            if last_price_pct is not None and prev_close_pct is not None:
                delta_to_prev_close = round(last_price_pct - float(prev_close_pct), 4)

        if with_valuation and last_price_pct is not None and (ruonia_curve or keyrate_curve) and ref_obj.base in ("RUONIA", "KEYRATE"):
            curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
            try:
                metrics = calculate_valuation_metrics(
                    ref_obj, last_price_pct, curve, calc_date,
                    accrued_override=moex_snapshot.get(isin, {}).get("accrued"),
                    periods=schedules.get(isin),
                    ruonia_curve=ruonia_curve,
                )
                dirty_price_rub = metrics.get("dirty_price_rub")
                dm_bps = metrics.get("dm_bps")
                yield_xirr_pct = metrics.get("yield_xirr_pct")
                index_yield_pct = metrics.get("index_yield_pct")
            except Exception:
                pass

        items.append(
            BondListItem(
                isin=isin,
                short_name=short_name,
                base_rate_type=ref_obj.base,
                face_index=ref_obj.face_index,
                formula=formula,
                spread_issue_bps=ref_obj.spread_issue_bps,
                coupons_per_year=_coupons_per_year(ref_obj.coupon_period_days,
                                                   ref_obj.coupons_per_year),
                maturity_date=ref_obj.maturity_date,
                next_coupon_date=next_coupon_after(ref_obj, calc_date),
                last_price_pct=last_price_pct,
                dirty_price_rub=dirty_price_rub,
                dm_bps=dm_bps,
                delta_to_prev_close=delta_to_prev_close,
                yield_xirr_pct=yield_xirr_pct,
                index_yield_pct=index_yield_pct,
            )
        )

    return BondListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/search", tags=["Bonds"])
async def search_bonds(q: str = Query(..., min_length=2)):
    """Поиск облигаций на MOEX по названию/ISIN для добавления в список."""
    return {"items": await MarketDataService.search_bonds(q)}


@router.get("/filters", response_model=BondFiltersResponse, tags=["Bonds"])
async def get_bond_filters():
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    
    bases = set()
    for isin, data in cache.items():
        ref = create_bond_ref_data(data, isin)
        bases.add(ref.base)
        
    return BondFiltersResponse(
        issuers=["MOEX Issuers"], # Placeholder, would extract from actual data if available
        classes=["Floater"],
        base_rates=sorted(list(bases - {"UNKNOWN"})),
        maturities=["1Y", "3Y", "5Y", "10Y"] # Placeholder, can be generated dynamically
    )


# ВАЖНО: до /{isin}, иначе "calendar" матчится как ISIN-путь
@router.get("/calendar", response_model=PaymentsCalendarResponse, tags=["Bonds"])
async def get_payments_calendar(
    date_from: Optional[date] = Query(None, alias="from"),
    date_to: Optional[date] = Query(None, alias="to"),
):
    """Календарь выплат юниверса: будущие купоны/погашения в ₽ на бумагу.
    Полный расчёт кэшируется на день; from/to режут окно (дефолт — год вперёд)."""
    from services.payments_calendar import build_payments_calendar
    data = await build_payments_calendar()
    cd = data["calc_date"]
    lo = date_from or cd
    hi = date_to or date(lo.year + 1, lo.month, min(lo.day, 28))
    events = [e for e in data["events"] if lo <= e["date"] <= hi]
    return PaymentsCalendarResponse(calc_date=cd, date_from=lo, date_to=hi, events=events)


# ЖУРНАЛ ИЗМЕНЕНИЙ СТРОК КОТИРОВОК: {ключ размеров: {isin: (когда, содержимое)}}.
# Держит ответ маленьким при частом такте — см. параметр since в get_quotes.
# Ключ верхнего уровня — размеры тикета: с ними состав строки другой, и делить
# журнал между запросами с разными размерами нельзя.
_QUOTE_LOG: Dict[str, Dict[str, tuple]] = {}
_QUOTE_LOG_TS: Dict[str, float] = {}
_QUOTE_LOG_TTL_SEC = 900.0
# Отметка процесса, которому принадлежит журнал. Клиент возвращает её вместе с
# since, и с чужой отметкой дельта не выдаётся — приходит полный ответ.
# Ловит рестарт бэка (журнал пуст, а since у клиента из прошлой жизни) и второй
# воркер, если он когда-нибудь появится: у него свой журнал, и since от соседа
# заставил бы его молча проглатывать строки, которых клиент не видел.
_QUOTE_EPOCH = f"{os.getpid()}-{id(_QUOTE_LOG):x}"


def _quote_delta(items: list, key: str, since: Optional[float],
                 epoch: Optional[str] = None) -> tuple:
    """(строки к отдаче, отметка времени ответа). since=None → отдаём всё.

    Содержимое строки сравнивается целиком: изменилось хоть одно поле — строка
    получает новую отметку и уедет всем, кто спрашивал раньше. Не изменилась —
    лежит в журнале со старой отметкой и в дельту не попадает."""
    import time
    now = time.time()
    if epoch is not None and epoch != _QUOTE_EPOCH:
        since = None                     # журнал не тот — отдаём всё
    seen = _QUOTE_LOG.setdefault(key, {})
    _QUOTE_LOG_TS[key] = now
    # журналы под размеры тикета, которые давно никто не спрашивает, копят
    # память по всему рынку — выкидываем
    for k, ts in list(_QUOTE_LOG_TS.items()):
        if now - ts > _QUOTE_LOG_TTL_SEC:
            _QUOTE_LOG.pop(k, None)
            _QUOTE_LOG_TS.pop(k, None)
    out = []
    for it in items:
        body = tuple(sorted((k, v) for k, v in it.items() if k != "isin"))
        prev = seen.get(it["isin"])
        mt = prev[0] if (prev is not None and prev[1] == body) else now
        seen[it["isin"]] = (mt, body)
        if since is None or mt > since:
            out.append(it)
    return out, now


@router.get("/quotes", tags=["Bonds"])
async def get_quotes(
    vol_bid: Optional[float] = Query(None, description="Тикет на биде, ₽ — вернуть цену набора и её Y-IDX"),
    vol_ask: Optional[float] = Query(None, description="Тикет на оффере, ₽"),
    since: Optional[float] = Query(None, description="Отметка прошлого ответа — вернуть только изменившиеся строки"),
    epoch: Optional[str] = Query(None, description="Метка процесса из прошлого ответа: не совпала — придёт всё")
):
    """Котировки всего рынка одним компактным ответом — фронт тянет их тактом 5с.

    Отдаёт то, что двигается внутри дня: цену последней сделки, верх стакана,
    средневзвес дня (WAPRICE биржи) и оборот. Всё остальное в строке таблицы
    (расчётные метрики, справочник) живёт своим циклом и здесь не дублируется.

    Источник — board-снапшот MOEX, который держит свежим quotes_poller; сюда
    ходит только чтение кэша, сети на запрос нет. По избранному фронт получает
    те же поля push'ем через WS, и они авторитетнее: приходят от Alor без
    задержки биржевого снапшота.

    vol_bid/vol_ask — размеры тикета, которые сейчас смотрят в таблице. Цена
    набора и её Y-IDX едут ТЕМ ЖЕ ТАКТОМ 5 с, а не только при перезагрузке
    таблицы: движок считает бумаги пачками (сетка, очередь сторон, догрев), и
    число, появившееся через такт после включения фильтра, раньше доезжало до
    строки лишь со следующим полным запросом /api/bonds. Заодно продлевает
    регистрацию размера в движке — пока вкладка опрашивает котировки, размер
    заведомо активен.

    ОБЪЯВЛЕН ДО /{isin}: иначе путь съест роут карточки как ISIN.
    """
    from services.market_data import market_cache
    from services.universe_stream import (_vol_prices as _us_vol_prices,
                                          yoi_at as _us_yoi_at,
                                          live_sides as _us_live_sides)
    if vol_bid or vol_ask:
        from services.universe_stream import register_vol_sizes
        register_vol_sizes([v for v in (vol_bid, vol_ask) if v])
    snap = await MarketDataService.fetch_board_snapshot()
    # ОДИН СНИМОК ГЛУБИНЫ НА ЗАПРОС. get_depth() проходит по всем ISIN всех
    # шардов, а при протухшем — пересобирает словарь целиком; в цикле по трём
    # тысячам бумаг это десятки миллисекунд блокировки петли на каждом такте.
    book = None
    if vol_bid or vol_ask:
        from services import depth as _depth_svc
        book = _depth_svc.get_depth()
    # Y-IDX — из событийного движка (universe_stream): он пересчитывает метрики
    # по факту сделки, поэтому спред у торгуемых бумаг здесь живой, а не
    # 10-минутной давности поллера
    um = market_cache.get("universe_metrics") or {}
    items = []
    for isin, v in snap.items():
        if v.get("last") is None and v.get("bid") is None and v.get("ask") is None:
            continue
        # средневзвес и оборот — свой счёт по тикам Alor, когда он есть: биржевые
        # WAPRICE/VALTODAY в снапшоте отстают. Оборот берём большим из двух (свой
        # полон только при живом стриме) — см. services/universe.
        lv = live_quotes.get(isin) or {}
        it = {"isin": isin, "last": v.get("last"), "bid": v.get("bid"),
              "ask": v.get("ask"),
              "wap": lv.get("vwap_pct") or v.get("waprice"),
              "vol": max(v.get("vol") or 0, lv.get("val_today") or 0) or None}
        m = um.get(isin)
        # ПО НАЛИЧИЮ КЛЮЧА, А НЕ ПО ЗНАЧЕНИЮ (тот же приём, что у bid/ask ниже).
        # Движок кладёт в строку явный None, когда считать стало нечем (контекст
        # остыл, цена ушла из модели, взвёлся sanity), и это НОВОСТЬ. Отдавая
        # только не-None, ручка не умела сказать «числа больше нет»: фронт с
        # переходом на сверку по наличию ключа читал отсутствие как «не
        # изменилось» и держал в ячейке спред от прошлой цены.
        if m and "yoi" in m:
            it["yoi"] = m["yoi"]
            # ЦЕНА РАСЧЁТА У ПЕРВИЧНОЙ МЕТРИКИ — как у сторон и средневзвеса.
            # Без неё клиент ставил yoi вообще без сверки и снимал приглушение,
            # перетирая результат более строгой WS-сверки: поллер ходит раз в
            # секунду и всегда оказывается последним.
            if m.get("last") is not None:
                it["yoi_px"] = m["last"]
        # спред по средневзвесу берём ИЗ ДВИЖКА: он посчитан по методике в его
        # такте (≤5 с назад). Раньше здесь стоял пересчёт наклоном от цены
        # сделки ради свежести wap — но приближение, обновлённое мгновенно,
        # хуже точного числа пятисекундной давности (27.08.2026).
        if m and "yoi_wap" in m:
            it["yoi_wap"] = m["yoi_wap"]
            # цена расчёта средневзвеса — рядом с числом, как у сторон: клиент
            # ставит спред, только если она совпала с той, что окажется в строке
            if m.get("wap") is not None:
                it["yoi_wap_px"] = m["wap"]
        # СПРЕДЫ СТОРОН — ВТОРЫМ ПУТЁМ. Считает их движок, а до строки монитора
        # они доезжали ТОЛЬКО его WS-патчем: в QUOTE_METRIC_FIELDS сторон не
        # было. Пока патч и снимок согласны, разницы нет; разошлись (прод
        # 01.09.2026: сторона бралась из пуша, а пуш пришёл без неё) — на
        # сервере число правильное, в браузере прочерк, и лечило его только
        # следующее движение книги. У неликвида его может не быть часами.
        # Теперь котировки везут стороны своим тактом: рассинхрон живёт секунду.
        # ВЕРХ СТАКАНА — ИЗ ДВИЖКА, СНАПШОТ ФОЛБЭКОМ. Два источника цен с разной
        # задержкой: движок читает книгу Alor в реальном времени, борд-снапшот
        # ISS отстаёт. Пока бумага «живая», фронт держит цену Alor и спред к ней
        # подходит; замолчала — приезжал снапшот, ОТКАТЫВАЛ цену назад и гасил
        # спред под неё, а число движка к откаченной цене уже не подходило. Так
        # и жили прочерки после тика (02.09.2026: у 21 стороны из 24 проверенных
        # цена снапшота расходилась с ценой движка).
        #
        # Цена и спред обязаны быть ОДНОЙ парой, поэтому берём обе оттуда, где
        # их посчитали вместе. Снапшот остаётся для бумаг вне универса движка
        # (в ответе их вчетверо больше, чем он считает).
        # СВЕЖАЯ ПАРА ИЗ СЕТКИ ПОБЕЖДАЕТ ЧИСЛА ДВИЖКА. Очередь сторон обходит
        # универс за две-три минуты, и всё это время строка жила спредом к
        # прежней цене — при том что ответ лежал в памяти: сетка даёт спред на
        # любой цене интерполяцией, без солвера. Берём последний верх книги и
        # спрашиваем сетку (см. live_sides); сетки нет — остаёмся на числах
        # движка, как раньше.
        fresh = _us_live_sides(isin, m) if m else {}
        for k, px in (("yoi_bid", "bid"), ("yoi_ask", "ask")):
            if px in fresh:
                it[px], it[k] = fresh[px][0], fresh[px][1]
                it[f"{k}_px"] = fresh[px][0]
                continue
            # `px in m`, а не `m.get(px) is not None`: пустая сторона у движка
            # значит «заявку сняли», и это тоже новость — откатываться на
            # снапшот в такой момент означало бы вернуть цену, которой на рынке
            # уже нет. Ключа нет вовсе (чужая структура строки) — берём снапшот.
            if m and px in m:
                it[px] = m[px]
                if m.get(k) is not None and m.get(px) is not None:
                    # цена расчёта едет рядом: клиент ставит спред, только если
                    # она совпала с той, что окажется в строке (у бумаги на
                    # стриме там цена из push'а, и сверка не даёт числу от
                    # прошлой цены сесть на новую — 27.08.2026)
                    it[k], it[f"{k}_px"] = m[k], m[px]
        if m and (vol_bid or vol_ask):
            # ключ размера строится ЗДЕСЬ, а наружу поля едут плоскими
            # (vol_bid_px/vol_bid_y): размер известен из самого запроса, и
            # тащить его в каждую строку ответа незачем
            px_map, y_map = m.get("vol_px") or {}, m.get("yoi_vol") or {}
            # ЧИСЛО, КОТОРОЕ УЖЕ ЕСТЬ В ПАМЯТИ, НЕ ЖДЁТ ОЧЕРЕДИ. Цена набора и
            # спред по ней попадали в строку только когда движок доберётся до
            # бумаги в очереди сторон — круг по универсу это две-три минуты, и
            # всё это время пользователь, включивший фильтр по объёму, смотрел
            # на прочерк. Между тем цена набора считается по кэшу глубины
            # арифметикой (_vol_prices), а спред на любой цене берётся из
            # готовой сетки (yoi_at) — обе операции без сети и без солвера.
            # Считаем лениво: только для бумаг, где чего-то не хватает.
            live_px = None
            for side, size in (("bid", vol_bid), ("ask", vol_ask)):
                if not size:
                    continue
                key = f"{side}:{float(size):.0f}"
                px, y = px_map.get(key), y_map.get(key)
                # `key not in px_map`, а НЕ `px is None`: движок кладёт ключ на
                # каждый размер и пишет None, когда книги на тикет не хватает —
                # это ОТВЕТ, а не отсутствие ответа. Пока разницы не было,
                # половина универса (тикет 5 млн собирают 47 % бумаг)
                # пересчитывалась на каждом запросе вечно и всегда давала тот
                # же None.
                if key not in px_map:
                    if live_px is None:
                        # снимок глубины и нужные размеры — снаружи (см.
                        # _vol_prices): иначе get_depth() пересобирал бы книгу
                        # всего рынка на каждой бумаге цикла
                        live_px = _us_vol_prices(
                            isin, face=m.get("face_px"),
                            accrued=m.get("accrued_settle"),
                            sizes=[v for v in (vol_bid, vol_ask) if v],
                            ladders=(book or {}).get(isin) or {})
                    px = live_px.get(key)
                if px is not None and y is None:
                    y = _us_yoi_at(isin, px)   # спред к ЭТОЙ цене, из сетки
                if px is None:
                    continue
                if key in px_map:
                    # ЦЕНА И СПРЕД — ОДНОЙ ПАРОЙ, спред явным null. Раньше цена
                    # набора уезжала всегда, а спред — только когда он есть, и
                    # отсутствующее поле на клиенте прежнее число не стирает
                    # (присвоение идёт по наличию ключа): в ячейке вставала новая
                    # цена набора со спредом от ПРЕДЫДУЩЕЙ. Соседняя ветка это
                    # правило соблюдает с 02.09, на числа движка его не
                    # распространили. Цена честна и без спреда — это арифметика
                    # книги, — поэтому гасим спред, а не цену.
                    it[f"vol_{side}_px"] = px
                    it[f"vol_{side}_y"] = y
                elif y is not None:
                    # ЖИВУЮ ЦЕНУ — ТОЛЬКО В ПАРЕ СО СПРЕДОМ. Она новее всего,
                    # что есть в строке, и рядом со спредом от прошлого прохода
                    # даёт рассинхрон 27.08.2026: пара выглядит согласованной и
                    # врёт. Нет спреда к ней — молчим, остаёмся на числах движка.
                    it[f"vol_{side}_px"], it[f"vol_{side}_y"] = px, y
        items.append(it)
    # ДЕЛЬТА. Такт опроса — секунда, а за секунду меняется десяток строк из трёх
    # тысяч: полный ответ (294 КБ сырых, 54 КБ gzip) был бы на 4/5 повтором
    # того, что у клиента уже лежит. Клиент возвращает since из прошлого ответа
    # и получает только изменившееся; периодический запрос без since сверяет
    # состояние целиком.
    key = f"{float(vol_bid or 0):.0f}:{float(vol_ask or 0):.0f}"
    out, stamp = _quote_delta(items, key, since, epoch)
    return {"ts": market_cache.get("quotes_ts"), "n": len(out), "items": out,
            "since": stamp, "epoch": _QUOTE_EPOCH,
            "full": len(out) == len(items), "total": len(items)}


@router.get("/{isin}", response_model=BondDetailsResponse, tags=["Bonds"])
async def get_bond_details(isin: str = Path(...)):
    isin = _require_isin(isin)
    cache = MarketDataService.get_local_bond_cache(
        _cache_path("isins_cache.json"))
    from services.bond_details import build_bond_details
    return BondDetailsResponse(**await build_bond_details(isin, cache))


@router.get("/{isin}/audit", response_model=BondAuditResponse, tags=["Bonds"])
async def get_bond_audit(isin: str = Path(...)):
    """Паспорт бумаги: все спарсенные/рассчитанные данные с провенансом,
    по-купонный бэктест спеки фиксинга, waterfall PV, санити-чеки."""
    isin = _require_isin(isin)
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    from services.bond_audit import build_bond_audit
    return BondAuditResponse(**await build_bond_audit(isin, cache))


@router.get("/{isin}/coupon-days", response_model=CouponDaysResponse, tags=["Bonds"])
async def get_coupon_day_rates(isin: str = Path(...)):
    """Полная дневная раскладка фиксинга по всем неистёкшим купонам: по каждому
    дню — дата наблюдения, значение индекса, факт ЦБ / форвард-ступень кривой."""
    isin = _require_isin(isin)
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    from services.bond_audit import coupon_day_rates
    return CouponDaysResponse(**await coupon_day_rates(isin, cache))


@router.get("/{isin}/candles", tags=["Bonds"])
async def get_bond_candles(
    isin: str = Path(...),
    tf: Literal["5m", "1h", "1d", "1w"] = Query("1d", description="Таймфрейм свечи"),
    board: Optional[str] = Query(None, description="Борд MOEX (TQCB/TQOB/TQRD…); пусто — резолв по ISIN"),
    secid: Optional[str] = Query(None, description="SECID (для ОФЗ ≠ ISIN); пусто — резолв по ISIN"),
):
    """OHLCV-свечи MOEX для графика. 5m — агрегация 1-мин.

    Без secid/board тикер и борд резолвятся по ISIN (как в history/bars):
    прибитый TQCB отдавал по ОФЗ (SU26…@TQOB) и риск-сектору (TQRD) ПУСТУЮ
    серию — полноэкранный график этих бумаг стоял без свечей."""
    isin = _require_isin(isin)
    if not secid or not board:
        from services.backdate import resolve_market
        rsec, rboard = await resolve_market(isin, board)
        secid, board = secid or rsec, board or rboard
    if not _SECID_RE.fullmatch(secid) or not re.fullmatch(r"[A-Z0-9]{4}", board):
        raise HTTPException(status_code=400, detail="bad secid/board")
    return {"isin": isin, "tf": tf, "candles": await MarketDataService.fetch_candles(secid, tf, board)}



@router.get("/{isin}/cashflow", response_model=CashflowResponse, tags=["Bonds"])
async def get_bond_cashflow(isin: str = Path(...)):
    # Re-use logic from get_bond_details internally to stay DRY in a real app
    # Here extending it directly for clarity
    isin = _require_isin(isin)
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    data = cache.get(isin)
    
    if not data:
        raise NotFoundException(f"Bond {isin} not found in cache", {"isin": isin})
        
    ref_obj = create_bond_ref_data(data, isin)

    ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()
    if not calc_date:
        calc_date = rates_date or date.today()

    # Amort/offer-aware builder (тот же, что карточка) — прежний get_cashflow_items
    # игнорировал амортизацию: купоны на полный номинал + принципал одним бул-платежом.
    sched_full = await MarketDataService.fetch_bond_schedule_full(isin)
    curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    from services.cashflow import build_cashflow_from_moex
    from services.bonds import external_formula
    formula = data.get("FORMULA", "") or external_formula(ref_obj)
    cfs, fv = build_cashflow_from_moex(
        ref_obj, curve, calc_date,
        sched_full.get("coupons", []), sched_full.get("amorts", []), formula,
        offers=sched_full.get("offers"),
    )

    return CashflowResponse(
        isin=isin,
        calc_date=calc_date,
        items=cfs,
        redemption_amount=fv
    )

@router.get("/{isin}/reprice", response_model=RepriceResponse, tags=["Bonds"])
async def reprice_bond_valuation(
    isin: str = Path(...),
    price: float = Query(..., gt=0, le=1000, description="Чистая цена, % от номинала"),
):
    """Пересчёт цена-зависимых метрик (SM/DM/YTM/dirty/Y-IDX/z_model) под
    произвольную чистую цену. Использует калькулятор карточки И live-рефреш строки
    таблицы по WS-тику. Тёплые кэши → мгновенно."""
    isin = _require_isin(isin)
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    from services.bond_details import reprice_bond
    metrics = await reprice_bond(isin, price, cache)
    return RepriceResponse(**metrics)


@router.get("/{isin}/price_from_spread", response_model=RepriceResponse, tags=["Bonds"])
async def price_from_spread(
    isin: str = Path(...),
    y_idx: float = Query(..., ge=-5000, le=20000, description="Целевой spread, bps"),
    horizon: str = Query("auto", description="maturity | put | call — горизонт, в котором "
                                             "задан целевой спред (auto = правило цены)"),
):
    """Обратная задача калькулятора: спред Y-IDX → чистая цена и все метрики под
    ней. Бисекция по цене на тёплом контексте (без сетевых вызовов внутри цикла).
    Цена возвращается в clean_price_pct.

    horizon фиксирует, В КАКОЙ метрике задан целевой спред: карточка со свитчером
    «к оферте» просит цену под спред к оферте, иначе цифра ответа не совпала бы
    с плиткой, из которой пользователь её взял."""
    isin = _require_isin(isin)
    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    from services.bond_details import solve_price_for_yidx
    metrics = await solve_price_for_yidx(isin, y_idx, cache, horizon=horizon)
    return RepriceResponse(**metrics)
