"""Ежедневное наполнение реестра инструментов (services.instruments_registry).

Собирает ISIN+параметры из доступных источников (замороженный NRD-кэш + Cbonds-
выгрузка + ручной слой), upsert в реестр, затем добирает недостающие maturity/
issue/face из MOEX (Cbonds-выгрузка их не содержит). Обнаружение новых бумаг =
появление ISIN, которого не было в реестре (флаг reviewed=0 → на admin-ревью).

Сеть — здесь (async); чистое ядро мёржа — instruments_registry.sync_from_sources.
"""
from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)


async def sync_instruments() -> dict:
    """Полный проход синка. Возвращает статистику (new/updated/enriched)."""
    from services import instruments_registry as reg, ref_data, nrd
    from services.market_data import MarketDataService

    # 1. источники без сети: замороженный NRD-кэш + Cbonds + ручной слой
    try:
        frozen = nrd._load_json(nrd.UNIVERSE_FILE).get("items", [])
    except Exception:
        frozen = []
    try:
        cbonds = ref_data.load_cbonds()
        manual = ref_data.load_manual()
    except Exception:
        cbonds, manual = {}, {}
    stats = reg.sync_from_sources(frozen, cbonds, manual)

    # 2. MOEX-дискавери (A1): авторитетный live-список торгуемых бумаг TQCB.
    #    - существующим — освежаем maturity/name/face (не-locked поля);
    #    - НОВЫЕ ISIN (нет в реестре) — проверяем на флоатер (bondization: есть
    #      будущий купон с value=None → ставка не зафиксирована = плавающий),
    #      подтверждённые кладём source='moex', reviewed=0 (в очередь ревью).
    #    Так список авто-актуален без ручной Cbonds-выгрузки.
    discovered = 0
    try:
        listing = await MarketDataService.fetch_bond_listing()
    except Exception as e:
        logger.warning("MOEX listing failed: %s", e)
        listing = {}
    # «только MOEX-торгуемые»: не-торгуемые (коммерческие/OTC — нет в листинге)
    # деактивируются, вернувшиеся — реактивируются (sanity-guard внутри)
    traded_stats = reg.sync_active_set(set(listing.keys()))

    known = {r["isin"] for r in reg.universe_rows(only_priceable=False, only_floaters=False)}
    # существующие: освежить maturity/name/face ПРЯМО из листинга (market-level даёт
    # MATDATE — закрывает главный пробел «нет maturity» без per-bond вызовов)
    for isin in known & set(listing):
        mo = listing[isin]
        upd = {"isin": isin, "maturity_date": mo.get("maturity"),
               "short_name": mo.get("short_name"), "face_value": mo.get("face")}
        if any(v is not None for k, v in upd.items() if k != "isin"):
            reg.upsert(upd, source="moex", mark_new=False)
    # новые кандидаты: сперва дешёвый пре-фильтр по листингу (coupon_percent
    # None/0 = ставка не зафиксирована → вероятный флоатер), лишь потом дорогой
    # bondization-чек. Отсекает ~фикс-купонные без лишних сетевых вызовов.
    new_isins = [i for i in listing if i not in known
                 and listing[i].get("coupon_percent") in (None, 0.0)]
    for isin in new_isins[:_MAX_DISCOVERY_PER_RUN]:
        try:
            if await _is_floater(isin):
                mo = listing[isin]
                reg.upsert({"isin": isin, "short_name": mo.get("short_name"),
                            "maturity_date": mo.get("maturity"), "face_value": mo.get("face")},
                           source="moex", mark_new=True)
                discovered += 1
        except Exception:
            continue
    if len(new_isins) > _MAX_DISCOVERY_PER_RUN:
        logger.info("discovery capped: %d new ISINs, checked %d",
                    len(new_isins), _MAX_DISCOVERY_PER_RUN)

    # 3. добор maturity/issue/face/частоты из БОРД-НЕЗАВИСИМОГО справочника MOEX
    #    (fetch_security_master ловит maturity даже вне TQCB — покрытие шире, чем
    #    board-методы; у Cbonds-бумаг maturity нет вовсе). Заодно инференс базы для
    #    ОФЗ-ПК (RUONIA-флоатеры Минфина) по имени.
    incomplete_rows = [r for r in reg.universe_rows(only_priceable=False, only_floaters=False)
                       if not r.get("maturity_date")]
    missing = [r["isin"] for r in incomplete_rows]
    enriched = 0
    if missing:
        try:
            secs = await MarketDataService.fetch_security_master(missing)
        except Exception as e:
            logger.warning("MOEX security master enrich failed: %s", e)
            secs = {}
        for isin, mo in (secs or {}).items():
            if not mo:
                continue
            freq = mo.get("coupon_freq")
            upd = {"isin": isin, "maturity_date": mo.get("maturity"),
                   "issue_date": mo.get("issue"), "face_value": _f(mo.get("face"))}
            if freq:
                upd["coupons_per_year"] = int(freq)
                upd["coupon_period_days"] = round(365 / freq)
            # база ОФЗ-ПК (Минфин, плавающий по RUONIA) по имени
            name = (mo.get("name") or "").upper()
            if _looks_ofz_pk(name, isin):
                upd["base"] = "RUONIA"
            if any(v is not None for k, v in upd.items() if k != "isin"):
                reg.upsert(upd, source="moex", mark_new=False)
                enriched += 1

    # 4. ретайр погашенных (A2): active=0 при maturity < сегодня
    retired = reg.retire_matured(date.today().isoformat())

    # 5. самопроверка данных: реклассификация фикс-бумаг + бэк-аут маржи vs факт
    #    КС/RUONIA (ловит неверную маржу/базу из Cbonds — инвариант «расчёт верен»)
    try:
        from services.instruments_validate import validate_priceable
        vstats = await validate_priceable()
    except Exception as e:
        logger.warning("registry validation failed: %s", e)
        vstats = {}

    stats.update({"discovered": discovered, "enriched": enriched, "retired": retired,
                  "reclassified_fixed": vstats.get("reclassified_fixed", 0),
                  "suspect": vstats.get("suspect", 0),
                  "deactivated": traded_stats.get("deactivated", 0),
                  "reactivated": traded_stats.get("reactivated", 0),
                  "synced_at": date.today().isoformat()})
    logger.info("instruments sync: %s | registry=%s", stats, reg.count())
    return stats


_MAX_DISCOVERY_PER_RUN = 80   # bondization-проверок новых ISIN за прогон (rate-limit)


def _looks_ofz_pk(name_upper: str, isin: str) -> bool:
    """ОФЗ-ПК (Минфин, купон плавает по RUONIA) — по имени/названию выпуска.
    SU29xxx — тикерный префикс серии ОФЗ-ПК на MOEX."""
    return ("ОФЗ-ПК" in name_upper or "ОФЗ ПК" in name_upper
            or "29" == (isin[8:10] if len(isin) > 10 else "")  # редко
            or "SU29" in name_upper)


async def _is_floater(isin: str) -> bool:
    """Флоатер ⇔ у бумаги есть БУДУЩИЙ купон с незафиксированной суммой (value=None):
    у фикс-купонных все value известны заранее. Сигнал из MOEX bondization."""
    from services.market_data import MarketDataService
    full = await MarketDataService.fetch_bond_schedule_full(isin)
    coupons = (full or {}).get("coupons") or []
    if not coupons:
        return False
    return any(c.get("value") is None for c in coupons)


def _f(x):
    try:
        return float(x) if x is not None else None
    except (ValueError, TypeError):
        return None
