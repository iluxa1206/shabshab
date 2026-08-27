"""Сборка карточки бумаги (вынесена из api/routes/bonds.py — оркестрация
9 источников + расчёты не место в route-хендлере). Возвращает plain dicts,
Pydantic-модели (BondDetailsResponse и вложенные) коэрсятся на route-слое.
"""
import asyncio
import logging
from datetime import datetime, date, timezone
from typing import Optional

from services.market_data import MarketDataService
from services import metrics
from services.bonds import (
    create_bond_ref_data, extract_bond_reference_dict,
    build_ref_external, external_formula, reconcile_face, amort_remaining_face,
)
from services.valuation import calculate_valuation_metrics, pick_horizon
from services.cashflow import build_cashflow_from_moex
from services.zspread import project_cfs
from services.exceptions import NotFoundException, CalculationException

logger = logging.getLogger(__name__)


async def _aempty():
    return {}


async def build_bond_details(isin: str, cache: dict) -> dict:
    """Полная карточка: reference/market/valuation/cashflow/floater/warnings."""
    data = cache.get(isin)
    external = data is None

    # все независимые сетевые вызовы — одним gather (MOEX ISS ~3.5с/запрос,
    # последовательно карточка грузилась 10-17с)
    res = await asyncio.gather(
        MarketDataService.fetch_last_prices([isin]),                                  # 0
        MarketDataService.fetch_moex_snapshot([isin]),                                # 1
        MarketDataService.fetch_coupon_schedules([isin]),                             # 2
        MarketDataService.get_curves(),                                               # 3
        MarketDataService.fetch_bond_schedule_full(isin),                             # 4
        MarketDataService.fetch_moex_securities([isin]),                              # 5
        MarketDataService.fetch_moex_shortnames() if external else _aempty(),         # 6
        MarketDataService.get_zspread_ctx(),                                          # 7
        return_exceptions=True,
    )
    _ok = lambda x, d: d if isinstance(x, Exception) else x
    market_prices = _ok(res[0], {})
    snapshot = _ok(res[1], {})
    schedules = _ok(res[2], {})
    ruonia_curve, keyrate_curve, calc_date, rates_date = _ok(res[3], (None, None, None, None))
    sched_full = _ok(res[4], {"coupons": [], "amorts": []})
    mo_map = _ok(res[5], {})
    shortnames = _ok(res[6], {})
    exp_ks, exp_ru, g_curve = _ok(res[7], (None, None, None))

    if data:
        ref_obj = create_bond_ref_data(data, isin)
        ref_dict = extract_bond_reference_dict(isin, data, ref_obj)
    else:
        # любая бумага вне кэша — справочник MOEX + база/спред из Cbonds-справки
        mo = mo_map.get(isin, {})
        if not mo:
            raise NotFoundException(f"Bond {isin} not found on MOEX", {"isin": isin})
        ref_obj = build_ref_external(isin, mo)
        ref_dict = {
            "isin": isin,
            "short_name": shortnames.get(isin) or mo.get("name") or isin,
            "face_value": ref_obj.face_value,
            "face_unit": mo.get("face_unit") or "RUB",
            "base_rate_type": ref_obj.base,
            "spread_bps": ref_obj.spread_issue_bps,
            "formula": external_formula(ref_obj),
            "start_date": ref_obj.issue_date,
            "maturity_date": ref_obj.maturity_date,
            "coupon_period_days": ref_obj.coupon_period_days,
            "coupons_per_year": ref_obj.coupons_per_year,
            "next_coupon_date": None,
            "accrued_interest": ref_obj.accrued_rub,
        }

    # Cbonds ID для прямой ссылки на страницу выпуска (cbonds.ru/bonds/{id}/).
    # Поиска по ISIN у cbonds нет — без id ссылку строить не из чего, поэтому
    # None здесь означает «кнопку не показывать», а не «дать общий список».
    try:
        from services.ref_data import load_cbonds
        ref_dict["cbonds_id"] = (load_cbonds().get(isin) or {}).get("cbonds_id")
    except Exception as e:
        logger.warning(f"cbonds_id lookup failed for {isin}: {e}")
    # SECID — для запасной ссылки на MOEX там, где cbonds_id нет (ОФЗ и свежие
    # выпуски вне bondsearch-выгрузки). У ОФЗ issue.aspx понимает только SECID
    # (SU29025RMFS2), по ISIN отдаёт редирект; у корпоратов SECID == ISIN.
    # Справочник MOEX кэшируется на день, поэтому запрос почти всегда локальный.
    ref_dict["moex_secid"] = (mo_map.get(isin) or {}).get("secid") or None

    last_price = market_prices.get(isin)
    prev_close_pct = snapshot.get(isin, {}).get("prev")
    accrued_live = snapshot.get(isin, {}).get("accrued")
    periods = schedules.get(isin) or _periods_from_coupons(sched_full.get("coupons"))

    # Номинал: сверяем с фактом купона (value/valueprc); правит тихий фолбэк на 1000
    _cd_face = calc_date or date.today()
    if reconcile_face(ref_obj, (sched_full or {}).get("coupons"), _cd_face):
        ref_dict["face_value"] = ref_obj.face_value
    # остаток из графика амортизаций авторитетнее кэша: стейл-кэш у
    # амортизируемых бумаг завышал dirty/SM/DM (БалтЛизП10: 1000 vs 900)
    _rem = amort_remaining_face((sched_full or {}).get("amorts"), _cd_face,
                                ref_obj.face_value)
    if _rem is not None and abs(_rem - ref_obj.face_value) > 0.5:
        ref_obj.face_value = _rem
        ref_dict["face_value"] = _rem

    # НКД на calc_date из MOEX (приоритет над стейл-кэшем) — для dirty и карточки
    if accrued_live is not None:
        ref_obj.accrued_rub = accrued_live
        ref_dict["accrued_interest"] = accrued_live

    # ближайшая будущая оферта (bondization offers) — информационный флаг.
    # Оценку НЕ клэмпим: НРД dm тоже к погашению (сверка 2026-07-08 — клэмп
    # к оферте ухудшает совпадение на всех горизонтах), но цена бумаги
    # с близкой офертой может прайситься к ней → DM/z несопоставимы.
    # горизонт оферты — от settle (как pricing), не от today; состоявшиеся оферты
    # отфильтрованы (не будущее событие). Показываем и call, и put, но вид (kind)
    # различаем: только put — гарантированный горизонт держателя (см. offer_kind).
    from core.valuation import settle_date as _settle, next_offer_info
    _off_ref = _settle(calc_date) if calc_date else date.today()
    # maturity отсекает техническую запись «Оферта/Погашение» на дату погашения:
    # опциона нет, и рисовать в референсе «Оферта (пут)» без свитчера горизонта
    # (его там нечему переключать) — прямое противоречие в карточке
    next_offer = next_offer_info(sched_full.get("offers"), _off_ref,
                                 ref_obj.maturity_date)
    if next_offer:
        ref_dict["offer_date"] = next_offer[0]
        ref_dict["offer_type"] = next_offer[1]
        ref_dict["offer_kind"] = next_offer[2]

    if not calc_date:
        calc_date = rates_date or date.today()
    if not rates_date:
        rates_date = date.today()

    # честный is_stale: ставки не сегодняшние (выходные/до обновления Cbonds)
    market_data = {
        "last_price_pct": last_price,
        "price_source": "Alor WebSocket",
        "calc_date": calc_date,
        "rates_date": rates_date,
        "market_timestamp": datetime.now(timezone.utc),
        "is_stale": rates_date < date.today(),
        "prev_close_clean_pct": prev_close_pct,
        "prev_close_dm_bps": None,
    }

    curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    cfs = []

    # Cashflow по реальному расписанию MOEX: прошлые купоны = факт, будущие = прогноз
    formula = (data.get("FORMULA", "") if data else "") or external_formula(ref_obj)
    cf_warnings: list = []
    try:
        cfs, _ = build_cashflow_from_moex(
            ref_obj, curve, calc_date,
            sched_full.get("coupons", []), sched_full.get("amorts", []), formula,
            offers=sched_full.get("offers"), warnings_out=cf_warnings,
        )
    except Exception as e:
        logger.warning(f"Cashflow error for {isin}: {e}")

    val_dict = {
        "clean_price_pct": last_price or 100.0,
        "dirty_price_rub": ref_obj.face_value + ref_obj.accrued_rub,  # fallback
        "dm_bps": None, "dm_label": None, "yield_xirr_pct": None,
        "index_yield_pct": None, "yield_over_index_bps": None,
        "pricing_status": "NO_MARKET_DATA",
        "warnings": ["No market price available, using Par (100.00) for dirty calc where needed"]
    }

    _acc_dt = _acc_date(snapshot.get(isin, {}).get("accrued_date"))
    if last_price is not None and curve:
        try:
            val_dict = calculate_valuation_metrics(
                ref_obj, last_price, curve, calc_date,
                accrued_override=accrued_live, periods=periods,
                amorts=sched_full.get("amorts"), offers=sched_full.get("offers"),
                ruonia_curve=ruonia_curve, accrued_date=_acc_dt,
            )
        except Exception as e:
            val_dict["pricing_status"] = "CALCULATION_ERROR"
            val_dict["warnings"] = [str(e)]

    if prev_close_pct is not None and curve:
        try:
            prev_metrics = calculate_valuation_metrics(
                ref_obj, prev_close_pct, curve, calc_date,
                accrued_override=accrued_live, periods=periods,
                amorts=sched_full.get("amorts"), offers=sched_full.get("offers"),
                ruonia_curve=ruonia_curve, accrued_date=_acc_dt,
            )
            market_data["prev_close_dm_bps"] = prev_metrics.get("dm_bps")
        except Exception:
            pass

    # блок флоатер-риска: spread duration (Macaulay проектных потоков), rate duration
    # (≈ до рефиксинга), текущий купон/база. Считаем на нашей кривой ожиданий.
    floater_block = None
    if ref_obj.base in ("RUONIA", "KEYRATE"):
        try:
            exp = exp_ru if ref_obj.base == "RUONIA" else exp_ks
            coupons = sched_full.get("coupons", [])
            px = last_price or prev_close_pct
            # Дюрации — ИЗ ВЫБРАННОГО ГОРИЗОНТА val_dict (единственный расчёт,
            # services.valuation._dur_block). Своей копии тут больше нет: она
            # считалась всегда к погашению, и карточка расходилась с витриной
            # у бумаги, чьи спреды посчитаны к оферте.
            _hz_sel = pick_horizon(val_dict)
            spread_dur = _hz_sel.get("dur_yrs")
            mod_dur = _hz_sel.get("mod_duration")
            convexity = _hz_sel.get("convexity")
            pvbp = _hz_sel.get("pvbp")
            # ставка начавшегося периода из модельного cashflow — фолбэк текущего
            # купона для RUONIA-average (MOEX не даёт valueprc/value до конца периода)
            cur_cpn_model = None
            for it in cfs:
                if (it.get("type") == "COUPON" and it.get("period_start")
                        and it["period_start"] <= calc_date < it["period_end"]):
                    cur_cpn_model = it.get("coupon_rate_pct")
                    break
            cb = metrics.carry_refix_block(coupons, sched_full.get("amorts"),
                                           ref_obj.face_value, px, exp,
                                           None, calc_date,
                                           current_coupon_override=cur_cpn_model)
            refix = cb["days_to_refix"]
            floater_block = {
                "spread_duration_yrs": spread_dur,
                "rate_duration_yrs": round(refix / 365.0, 3) if refix is not None else None,
                "days_to_refix": refix, "current_coupon_pct": cb["current_coupon_pct"],
                "base_rate_pct": cb["base_rate_pct"],
                "mod_duration": mod_dur, "convexity": convexity, "pvbp": pvbp,
            }
        except Exception as e:
            logger.warning(f"Floater risk error for {isin}: {e}")

    warnings = list(cf_warnings)   # деградация cashflow-таблицы (фолбэк на факты)
    # ПРАВИЛО ЦЕНЫ (services.valuation._preferred_horizon) — объясняем в карточке,
    # почему метрики показаны к тому горизонту, к которому показаны. Свитчер в
    # карточке позволяет посмотреть любой горизонт вручную.
    _hz = val_dict.get("preferred_horizon", "maturity")
    _to = {"put": "ОФЕРТЕ", "call": "CALL"}.get(_hz, "ПОГАШЕНИЮ")
    if next_offer:
        _kind = "Call-оферта" if next_offer[2] == "call" else "Пут-оферта"
        warnings.append(f"{_kind} {next_offer[0].isoformat()}, расчёт к {_to}")
    # ТАБЛИЦУ ПОТОКОВ НЕ РЕЖЕМ, а размечаем. Карточка пишет «расчёт к ОФЕРТЕ», а
    # поток шёл до погашения — у 25 бумаг из 111 с офертой это сотня лишних
    # строк (RU000A10DK98: горизонт 25.11.2026, таблица до 2035). Но резать
    # нельзя: горизонт — это ПРЕДПОЛОЖЕНИЕ правила цены с порогом 10 bps, а
    # график — ФАКТ эмитента; при резке сотня строк мигала бы от одного тика
    # цены, и пут — это право держателя, а не расписание. Помечаем хвост, фронт
    # рисует разделитель и приглушает.
    _hz_date = (val_dict.get("horizons", {}).get(_hz) or {}).get("date")
    if _hz_date and cfs:
        for _it in cfs:
            _pd = _it.get("payment_date") if isinstance(_it, dict) else None
            if _pd:
                _it["beyond_horizon"] = str(_pd) > str(_hz_date)

    return {
        "reference": ref_dict,
        "market": market_data,
        "valuation": val_dict,
        "cashflow_horizon": {"date": _hz_date, "kind": _hz},
        "cashflow": cfs,
        "floater": floater_block,
        "sources": {"details": "MOEX", "market": "Alor"},
        "warnings": warnings,
    }


def _periods_from_coupons(coupons) -> list:
    """[(start, end, value)] из расписания bondization.

    Нужен, когда fetch_coupon_schedules промахнулся, а полное расписание есть:
    два независимых источника одного и того же, и терять оба сразу незачем.
    Без периодов расчёт слепнет — не видит фактических купонов и не может
    проверить НКД (РостелP21R 25.08: periods пусто + НКД 0 от ISS → сигнал
    250 bps против 121 верного)."""
    out = []
    for c in coupons or []:
        s, e = _acc_date(c.get("start")), _acc_date(c.get("end"))
        if s and e:
            out.append((s, e, c.get("value")))
    return sorted(out, key=lambda x: x[0])


def _acc_date(v):
    """'YYYY-MM-DD' из ISS → date. Мусор/пусто → None (тогда работает прежняя
    эвристика accrued_basis)."""
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


async def load_reprice_ctx(isin: str, cache: dict) -> dict:
    """Тёплый контекст для пересчёта под произвольную цену: ref_obj, кривая,
    calc_date, НКД, amorts/offers/periods/coupons и z-spread ctx. Один сетевой
    gather на isin — далее reprice_at_price(ctx, price) работает БЕЗ I/O, что
    позволяет батчить десятки уровней стакана по одной бумаге.

    Тот же богатый путь, что build_bond_details (periods/amorts/offers/НКД), но
    без cashflow-сборки. Переиспользует тёплые кэши (кривые в памяти, расписание
    на диске с day-TTL, снапшот)."""
    data = cache.get(isin)
    external = data is None
    res = await asyncio.gather(
        MarketDataService.fetch_coupon_schedules([isin]),                             # 0
        MarketDataService.get_curves(),                                               # 1
        MarketDataService.fetch_bond_schedule_full(isin),                             # 2
        MarketDataService.fetch_moex_snapshot([isin]),                                # 3
        MarketDataService.fetch_moex_securities([isin]) if external else _aempty(),   # 4
        MarketDataService.get_zspread_ctx(),                                          # 5
        return_exceptions=True,
    )
    _ok = lambda x, d: d if isinstance(x, Exception) else x
    schedules = _ok(res[0], {})
    ruonia_curve, keyrate_curve, calc_date, rates_date = _ok(res[1], (None, None, None, None))
    sched_full = _ok(res[2], {"coupons": [], "amorts": []})
    snapshot = _ok(res[3], {})
    mo_map = _ok(res[4], {})
    exp_ks, exp_ru, g_curve = _ok(res[5], (None, None, None))

    if data:
        ref_obj = create_bond_ref_data(data, isin)
    else:
        mo = mo_map.get(isin, {})
        if not mo:
            raise NotFoundException(f"Bond {isin} not found on MOEX", {"isin": isin})
        ref_obj = build_ref_external(isin, mo)

    if not calc_date:
        calc_date = rates_date or date.today()

    # номинал и НКД — как в карточке (иначе dirty/ставка расходятся)
    reconcile_face(ref_obj, (sched_full or {}).get("coupons"), calc_date)
    _rem = amort_remaining_face((sched_full or {}).get("amorts"), calc_date,
                                ref_obj.face_value)
    if _rem is not None and abs(_rem - ref_obj.face_value) > 0.5:
        ref_obj.face_value = _rem
    accrued_live = snapshot.get(isin, {}).get("accrued")
    accrued_src = snapshot.get(isin, {})
    if accrued_live is None:
        # ПРОМАХ ПЕРСОНАЛЬНОГО СНИМКА — добираем НКД из общего борд-снимка: он
        # день кэшируется в памяти (сети не будет) и это ТОТ ЖЕ источник, по
        # которому считает витрина.
        #
        # Без этого добора контекст молча считал начисление сам, и точный путь
        # расходился с таблицей: прод 27.08.2026, РЕСОЛизБО5 — 368 бп против
        # 382 в таблице при ctx_accrued=None; у ВЭБ2Р-53 та же причина дала
        # 166 против 188 и увела за собой всю лестницу стакана в телеграме,
        # потому что уровни считаются этим же путём.
        try:
            board = await MarketDataService.fetch_board_snapshot()
            accrued_src = board.get(isin) or {}
            accrued_live = accrued_src.get("accrued")
        except Exception as e:
            logger.debug("board accrued fallback %s: %s", isin, e)
    if accrued_live is not None:
        ref_obj.accrued_rub = accrued_live
    # дата, на которую биржа посчитала этот НКД: с нашей поставкой она
    # расходится (пятница/праздники), и без неё срок и НКД считаются на разные
    # дни — см. services/valuation.calculate_valuation_metrics
    accrued_date = _acc_date(accrued_src.get("accrued_date"))

    curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    if curve is None:
        raise CalculationException("Curve unavailable to reprice", {"isin": isin})

    return {
        "isin": isin,
        "ref_obj": ref_obj,
        "curve": curve,
        "ruonia_curve": ruonia_curve,   # база Y-IDX и для КС-бумаг
        "calc_date": calc_date,
        "accrued_live": accrued_live,
        "accrued_date": accrued_date,
        # НКД не дали ни персональный снимок, ни борд: считать спред «точно» в
        # этом состоянии нельзя — начисление придётся выдумывать, а ошибка в
        # десятые доли рубля превращается в десятки б.п. спреда (см. выше).
        # Потребители точного пути обязаны молчать, а не показывать сдвинутое
        # число (screener_core.exact_y_idx).
        "accrued_missing": accrued_live is None,
        # расписание купонов: свой источник, а при его промахе — из полного
        # bondization (см. _periods_from_coupons)
        "periods": schedules.get(isin) or _periods_from_coupons(
            sched_full.get("coupons")),
        "coupons": sched_full.get("coupons", []),
        "amorts": sched_full.get("amorts"),
        "offers": sched_full.get("offers"),
        "exp": exp_ru if ref_obj.base == "RUONIA" else exp_ks,
        "g_curve": g_curve,
    }


def reprice_at_price(ctx: dict, price: float) -> dict:
    """Чистая цена → цена-зависимые метрики оценки (SM/DM/YTM/dirty/Y-IDX) на
    тёплом ctx (load_reprice_ctx) — БЕЗ сетевых вызовов и БЕЗ z/carry. Батчится
    по уровням стакана (тот же расчёт, что калькулятор карточки, с полными
    amorts/offers/periods/НКД → результаты совпадают с карточкой поштучно)."""
    return calculate_valuation_metrics(
        ctx["ref_obj"], price, ctx["curve"], ctx["calc_date"],
        accrued_override=ctx["accrued_live"], periods=ctx["periods"],
        amorts=ctx["amorts"], offers=ctx["offers"],
        ruonia_curve=ctx.get("ruonia_curve"),
        accrued_date=ctx.get("accrued_date"),
    )


def _reprice_z(ctx: dict, price: float) -> dict:
    """z_model (тоже цена-зависим) поверх reprice_at_price. Отдельно от core:
    нужен live-рефрешу строки таблицы по WS-тику, но НЕ уровням стакана."""
    ref_obj = ctx["ref_obj"]
    calc_date = ctx["calc_date"]
    amorts, offers = ctx["amorts"], ctx["offers"]
    accrued_live, exp, g_curve = ctx["accrued_live"], ctx["exp"], ctx["g_curve"]
    coupons = ctx["coupons"]
    isin = ctx["isin"]

    z_model = None
    if exp and g_curve and ref_obj.base in ("RUONIA", "KEYRATE"):
        try:
            from services.zspread import compute_z_bps
            cpn_dicts = [{"start": c.get("start"), "end": c.get("end"), "value": c.get("value")}
                         for c in coupons]
            z_model, _dur = compute_z_bps(
                ref_obj, exp, g_curve, calc_date, price,
                accrued_live if accrued_live is not None else ref_obj.accrued_rub,
                cpn_dicts, amorts, offers)
        except Exception as e:
            logger.warning(f"reprice z_model error {isin}: {e}")
    return {"z_model_bps": z_model}


async def reprice_bond(isin: str, price: float, cache: dict) -> dict:
    """Пересчёт всех метрик оценки под ПРОИЗВОЛЬНУЮ чистую цену (калькулятор в
    карточке И live-рефреш строки таблицы по WS-тику). Тёплые кэши → мгновенно."""
    ctx = await load_reprice_ctx(isin, cache)
    out = dict(reprice_at_price(ctx, price))
    out.update(_reprice_z(ctx, price))
    return out


# Разбег поиска цены при инверсии спреда: от 20% (глубокий дефолтный уровень) до
# 200% номинала. Шире смысла нет — и по краям расчёт всё равно отдаёт None:
# санити-фильтр valuation гасит спред вне [_SANE_BPS], а на слишком высокой цене
# срабатывает guard «номинальный убыток».
_SOLVE_LO, _SOLVE_HI = 20.0, 200.0
_SOLVE_STEP = 0.9   # шаг расширения скобки от старта (×/÷ по цене)


async def solve_price_for_yidx(isin: str, y_idx_bps: float, cache: dict,
                               horizon: str = "auto") -> dict:
    """Обратная задача калькулятора: спред Y-IDX → чистая цена + все метрики под
    ней. Y-IDX монотонно убывает по цене (дороже бумага — ниже доходность, значит
    и спред к базе), поэтому берётся бисекция по цене на ТЁПЛОМ ctx: каждая
    итерация — reprice_at_price без единого сетевого вызова.

    Скобка ищется расширением от 100% номинала, а не сразу по краям диапазона:
    на экстремальных ценах valuation отдаёт спред None (санити-фильтр / guard
    номинального убытка), и «пустой» край не годится в границу бисекции.

    Возвращает метрики reprice_bond плюс clean_price_pct найденной цены."""
    ctx = await load_reprice_ctx(isin, cache)

    # ГОРИЗОНТ ФИКСИРУЕМ на всю бисекцию. Правило цены цено-зависимо: при
    # "auto" функция Y-IDX(цена) рвалась бы на переходе через цену выкупа
    # (левее — к оферте, правее — к погашению), а бисекция требует монотонной
    # непрерывной функции. Карточка присылает уже разрешённый ключ горизонта.
    def yidx(p: float):
        m = reprice_at_price(ctx, p)
        return pick_horizon(m, horizon).get("yield_over_index_bps")

    lo = hi = 100.0
    y_lo = y_hi = yidx(lo)
    if y_lo is None:
        raise CalculationException("Spread is not computable for this bond", {"isin": isin})

    # Расширение скобки. Наткнулись на None (цена вышла за область, где спред
    # осмыслен) — не бросаем расширение, а уполовиниваем шаг и подходим к границе
    # ближе: иначе грубый шаг в 10% объявлял бы недостижимым спред, который живёт
    # в паре процентов от текущей цены.
    def expand(p0, y0, down: bool):
        p, y = p0, y0
        step = _SOLVE_STEP if down else 1.0 / _SOLVE_STEP
        limit = _SOLVE_LO if down else _SOLVE_HI
        while (y < y_idx_bps if down else y > y_idx_bps):
            if (p <= limit if down else p >= limit) or abs(step - 1.0) < 1e-3:
                break
            nxt = max(p * step, limit) if down else min(p * step, limit)
            v = yidx(nxt)
            if v is None:
                step = 1.0 + (step - 1.0) / 2   # к границе вычислимости мельче
                continue
            p, y = nxt, v
        return p, y

    # вниз по цене — вверх по спреду (нужно, если целевой спред больше текущего)
    lo, y_lo = expand(lo, y_lo, down=True)
    # вверх по цене — вниз по спреду
    hi, y_hi = expand(hi, y_hi, down=False)

    if not (y_hi <= y_idx_bps <= y_lo):
        raise CalculationException(
            f"Spread {y_idx_bps:.0f} bps unreachable: range {y_hi:.0f}..{y_lo:.0f} bps",
            {"isin": isin, "y_idx_min_bps": y_hi, "y_idx_max_bps": y_lo})

    # бисекция внутри скобки; выходим, как только цена стабилизировалась на
    # 1e-6 % (глубже котировок всё равно нет)
    for _ in range(60):
        if hi - lo < 1e-6:
            break
        mid = (lo + hi) / 2
        y = yidx(mid)
        if y is None:
            raise CalculationException("Spread is not computable at probe price", {"isin": isin})
        if y > y_idx_bps:
            lo = mid        # спред слишком велик → цена должна быть выше
        else:
            hi = mid

    # финальный прогон на ОКРУГЛЁННОЙ цене: в карточку уходит ровно та цена,
    # которая встанет в поле ввода, и метрики под ней сходятся один в один.
    # Два знака — шаг котировки на рынке; спред ответа из-за округления отходит
    # от заказанного на единицы bps, но цифры карточки честны для этой цены
    price = round((lo + hi) / 2, 2)
    out = dict(reprice_at_price(ctx, price))
    out.update(_reprice_z(ctx, price))
    out["clean_price_pct"] = price
    return out
