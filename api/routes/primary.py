"""Витрина анонсов первички (services/primary_calendar)."""
from fastapi import APIRouter, Depends, Path, Query

from api.routes.auth import require_admin


# Потолок «облигационного» разрыва цены первых торгов и цены книги, п.п.
DEBUT_MAX_PP = 10.0


def pp_max_rows() -> int:
    """Дефолт лимита витрины — потолок сервиса (годовой объём вдвое меньше)."""
    from services.primary_placements import MAX_ROWS
    return MAX_ROWS

router = APIRouter()


@router.get("", tags=["Primary"])
@router.get("/", tags=["Primary"], include_in_schema=False)
async def get_primary_calendar():
    """Планируемые размещения: даты книги/размещения, ориентир купона, объём +
    спред по нашей модели (services/primary_pricing).

    Кэш выгрузки обновляется по TTL и раз в сутки из дневного синка; источник
    внешний, его падение отдаёт последний известный снимок. Спред считается
    ПОВЕРХ снимка на актуальных кривых — замораживать его в суточном кэше
    нельзя: кривая двигается в течение дня, а сравнение с монитором имеет смысл
    только когда обе цифры с одной кривой.

    Сбой прайсинга не роняет витрину: строки уедут без model, таблица покажет
    ориентир организатора текстом — как до появления колонки."""
    from services.primary_calendar import get_calendar
    from services.primary_pricing import price_rows_cached
    data = await get_calendar()
    try:
        models = await price_rows_cached(data["rows"])
        data["rows"] = [{**r, "model": m} for r, m in zip(data["rows"], models)]
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("первичка: спреды не посчитаны: %s", e)
    return data


@router.get("/placements", tags=["Primary"])
async def get_placements(
    date_from: str = Query(None, alias="from", description="YYYY-MM-DD"),
    date_to: str = Query(None, alias="to", description="YYYY-MM-DD"),
    q: str = Query(None, description="Поиск по имени/SECID"),
    min_rub: float = Query(0, ge=0, description="Порог объёма размещения, ₽"),
    active: bool = Query(False, description="Только те, где книга ещё набирается"),
    limit: int = Query(pp_max_rows(), ge=1, le=5000),
):
    """ФАКТ размещений с биржи: строка = выпуск (первый день, объём, цена).

    Здесь, в отличие от анонсов, ничего не прогнозируется — это дневные итоги
    бордов «Размещение» из ISS (services/primary_placements). Подписи (эмитент,
    тип купона, маржа, рейтинг) подмешиваются из реестра, если бумага в нём
    есть: у выпуска-«однодневки» и у неторгуемого более выпуска их не будет.

    Окно дат фильтрует ПЕРВЫЙ ДЕНЬ выпуска, а объём и цена всегда считаются по
    всей его истории (см. primary_placements.aggregates). active=1 показывает
    выпуски, книга которых ещё набирается, и окно тогда не применяется.
    """
    from services import primary_placements as pp
    from services import instruments_registry as reg
    from services.pools import run_bg

    rows = await run_bg(pp.aggregates, date_from, date_to, q, min_rub, limit,
                        active)
    from services import placement_analytics as pa
    mets = await run_bg(pa.metrics_map, [r["secid"] for r in rows])
    debut = await run_bg(pp.debut_map)
    labels = await run_bg(reg.labels_map, [r["isin"] for r in rows if r.get("isin")])
    for r in rows:
        lab = labels.get(r.get("isin") or "") or {}
        # эмитент: наше каноническое имя, иначе полное юрлицо из справочника
        # MOEX — у 4/5 размещений года бумаги в реестре нет вовсе
        r["emitter"] = lab.get("emitter") or r.pop("emitent_moex", None)
        r["base"] = lab.get("base")                 # KEYRATE/RUONIA/FIXED/None
        r["rating"] = lab.get("rating")
        r["margin_bps"] = lab.get("margin_bps")
        r["maturity"] = lab.get("maturity")
        r["coupon_text"] = lab.get("coupon_text")
        r["coupons_per_year"] = lab.get("coupons_per_year")
        r["in_registry"] = bool(lab)
        r.pop("emitent_moex", None)
        # ОФЗ-аукцион Минфина — это не «ещё одно размещение»: другой механизм
        # (цена отсечения, а не номинал), другой масштаб, свой фильтр в витрине
        r["is_ofz"] = r.get("sec_type") == "ofz_bond"
        # ДОЛЯ РАЗМЕЩЁННОГО. Приоритет у цифры биржи (ISSUESIZEPLACED): наша
        # сумма по дням знает только собранное окно истории, а книга могла
        # начаться раньше. Фолбэк — своя сумма, тогда помечаем источник.
        size = r.get("issue_size") or 0
        placed = r.get("issue_size_placed")
        src = "moex" if placed else ("own" if r.get("volume") else None)
        if not placed:
            placed = r.get("volume")
        r["placed_pct"] = round(100.0 * placed / size, 1) if (size and placed) else None
        r["placed_src"] = src
        m = mets.get(r["secid"]) or {}
        r["spread_bps"] = m.get("y_idx_bps")
        for key in ("curve_mode", "horizon", "horizon_date", "after_price",
                    "after_curve_mode", "after_err"):
            r[key] = m.get(key)
        r["spread_price"] = m.get("price")
        r["premium_bps"] = m.get("premium_bps")
        r["after_date"] = m.get("after_date")
        r["spread_err"] = m.get("err")
        # ДЕБЮТ: цена первых торгов минус цена книги, в пунктах цены. Есть у
        # втрое большего числа выпусков, чем спред (тот только у флоатеров
        # реестра), и одинаково читается у фикса и у флоатера
        d = debut.get(r["secid"]) or {}
        r["debut_date"] = d.get("date")
        r["debut_price"] = d.get("close")
        gap = (round(d["close"] - r["first_price"], 2)
               if d.get("close") is not None and r.get("first_price") else None)
        # Разрыв больше DEBUT_MAX_PP — это не дебют облигации, а другая шкала
        # цены: структурные ноты ВТБ/ГПБ размещаются по 100, а торгуются от
        # стоимости корзины (54 или 140 на первых торгах). Для настоящего
        # выпуска десять пунктов за десять дней — дефолтная динамика, а не
        # приём книги. Цену показываем, число — нет: пусть смотрит человек.
        r["debut_odd"] = gap is not None and abs(gap) > DEBUT_MAX_PP
        r["debut_pct"] = None if r["debut_odd"] else gap
    # усечение выдачи должно быть ВИДНО: молча обрезанный список читается как
    # полный рынок первички за период
    return {"rows": rows, "stats": await run_bg(pp.stats),
            "truncated": len(rows) >= limit}


@router.get("/placements/{secid}/days", tags=["Primary"])
async def get_placement_days(secid: str = Path(..., min_length=4, max_length=24)):
    """Разворот строки: по каким дням и почём набирался объём (доразмещения)."""
    from services import primary_placements as pp
    from services.pools import run_bg
    return {"secid": secid, "rows": await run_bg(pp.days_of, secid)}


@router.post("/placements/sync", tags=["Primary"])
async def sync_placements(
    days: int = Query(30, ge=1, le=2000, description="Окно бэкфилла, кал. дней"),
    force: bool = Query(False, description="Перечитать уже собранные даты"),
    _admin: dict = Depends(require_admin),
):
    """Ручной бэкфилл истории размещений. Идемпотентен: сходившие даты
    пропускаются (одна дата = один запрос ISS на каждый борд размещения)."""
    from services import primary_placements as pp
    return await pp.backfill(days=days, force=force)


@router.get("/placements/{secid}/aftermarket", tags=["Primary"])
async def get_aftermarket(secid: str = Path(..., min_length=4, max_length=24)):
    """Первые дни жизни выпуска: вторичка, РПС, выкуп.

    Размер книги не говорит, кому бумага досталась: крупный РПС на второй день —
    переупаковка между своими, а не рыночный спрос."""
    from services import placement_analytics as pa
    from services.pools import run_bg
    return await run_bg(pa.aftermarket, secid)


@router.get("/slices", tags=["Primary"])
async def get_slices(months: int = Query(12, ge=1, le=36),
                     floaters: bool = Query(True, description="Только флоатеры")):
    """Карта первички: объём и МЕДИАННЫЕ маржа/спред/премия по месяцам,
    рейтингам и базам купона. Медиана, а не среднее — один десятимиллиардный
    выпуск с нулевой маржой утаскивает среднее месяца туда, где не размещался
    никто."""
    from services import placement_analytics as pa
    from services.pools import run_bg
    return await run_bg(pa.market_slices, months, floaters)


@router.get("/announces", tags=["Primary"])
async def get_announces(limit: int = Query(200, ge=1, le=2000)):
    """Архив анонсов со сверкой «ориентир организатора ↔ факт размещения».

    У сведённой строки видно, где закрылась книга относительно потолка: ориентир
    почти всегда верхняя граница («КС + не выше 300 бп»), и разница с фактическим
    спредом — это и есть цена вопроса."""
    from services import placement_analytics as pa
    from services import primary_placements as pp
    from services.pools import run_bg
    rows = await run_bg(pa.announces, limit)
    secids = [r["matched_secid"] for r in rows if r.get("matched_secid")]
    if secids:
        mets = await run_bg(pa.metrics_map, secids)
        facts = {r["secid"]: r for r in await run_bg(
            pp.aggregates, None, None, None, 0.0, pp.MAX_ROWS)}
        for r in rows:
            sec = r.get("matched_secid")
            f, m = facts.get(sec) or {}, mets.get(sec) or {}
            r["fact_date"] = f.get("first_date")
            r["fact_name"] = f.get("shortname")
            r["fact_price"] = f.get("first_price")
            r["fact_curve_mode"] = m.get("curve_mode")
            r["fact_value_rub"] = f.get("value_rub")
            r["fact_spread_bps"] = m.get("y_idx_bps")
    return {"rows": rows}


@router.post("/analytics/run", tags=["Primary"])
async def run_analytics(limit: int = Query(60, ge=1, le=2000),
                        details: bool = Query(True, description="Обновить паспорта выпусков"),
                        _admin: dict = Depends(require_admin)):
    """Ручной прогон аналитики первички: спред книги, премия, сверка анонсов.
    Тот же расчёт, что ночью, — нужен после наливки истории и смены методики."""
    from services import placement_analytics as pa
    from services import primary_placements as pp
    out = {}
    if details:
        out["details"] = await pp.sync_sec_details()
    out["metrics"] = await pa.compute(limit=limit)
    out["match"] = await pa.match_announces()
    return out
