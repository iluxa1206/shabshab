"""Ежедневное наполнение реестра инструментов (services.instruments_registry).

Собирает ISIN+параметры из доступных источников (замороженный NRD-кэш + Cbonds-
выгрузка + ручной слой), upsert в реестр, затем добирает недостающие maturity/
issue/face из MOEX (Cbonds-выгрузка их не содержит). Обнаружение новых бумаг =
появление ISIN, которого не было в реестре (флаг reviewed=0 → на admin-ревью).

Сеть — здесь (async); чистое ядро мёржа — instruments_registry.sync_from_sources.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date

from services.paths import cache_path

logger = logging.getLogger(__name__)

# Замороженный seed-файл универса флоатеров (исторический дамп, читается без сети).
# Используется только для холодного bootstrap реестра инструментов.
_FROZEN_SEED = cache_path("nrd_universe_cache.json")


def load_frozen_seed() -> list[dict]:
    """Замороженный seed универса флоатеров с диска (bootstrap холодного реестра)."""
    try:
        with open(_FROZEN_SEED, "r", encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


async def sync_instruments() -> dict:
    """Полный проход синка. Возвращает статистику (new/updated/enriched)."""
    from services import instruments_registry as reg, ref_data
    from services.market_data import MarketDataService

    # 1. источники без сети: замороженный seed универса + Cbonds + ручной слой
    frozen = load_frozen_seed()
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
            # keep_source: дневной рефреш maturity/name НЕ провенанс параметров —
            # иначе все cbonds-строки за день «становились» moex
            reg.upsert(upd, source="moex", mark_new=False, keep_source=True)
    # новые флоатеры: bondization-дискавери с negative-кэшем (см. discover_floaters).
    discovered = await discover_floaters(listing, reg=reg)

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
                # 365/freq — только фолбэк: не затираем период, уже посчитанный из
                # фактического графика (discovery). upsert-COALESCE спасает от None,
                # но НЕ от перезаписи известного значения новым числом.
                if not (reg.get(isin) or {}).get("coupon_period_days"):
                    upd["coupon_period_days"] = round(365 / freq)
            # база ОФЗ-ПК (Минфин, плавающий по RUONIA) по имени
            name = (mo.get("name") or "").upper()
            if _looks_ofz_pk(name, isin):
                upd["base"] = "RUONIA"
            if any(v is not None for k, v in upd.items() if k != "isin"):
                reg.upsert(upd, source="moex", mark_new=False)
                enriched += 1

    # 3b. правило ОФЗ-ПК: margin=0/avg-RUONIA/Т-7 + Минфин/AAA для серии 29xxx
    #     (MOEX не отдаёт маржу ОФЗ-ПК → без правила бумаги непрайсуемы и невидимы)
    try:
        ofz_fixed = reg.normalize_ofz_pk()
    except Exception as e:
        logger.warning("ofz-pk normalize failed: %s", e)
        ofz_fixed = 0

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

    # 6. обогащение из corpbonds.ru (авторитетная формула купона) для проблемных:
    #    incomplete (нет base/margin) + suspect (маржа расходится). Формула даёт
    #    точную базу/маржу/режим + ловит ЭКЗОТИКУ (инверсные/CPI/G-Curve → EXOTIC,
    #    вне линейной модели). Внешний сайт → капим и rate-limit'им.
    cb_stats = {}
    try:
        from services.enrich_corpbonds import enrich_registry
        # incomplete (нет base/margin) + suspect (маржа расходится) + EXOTIC
        # (перепроверка: детект экзотики раньше ошибался, напр. Σ-приклеенная база).
        # Квоты на класс + negative-кэш (enrich_pending): без них общий срез [:cap]
        # голодал — incomplete (355) вытеснял suspect/exotic за край, а стабильный
        # порядок дёргал одни и те же первые 60 каждый день.
        from services.enrich_corpbonds import PARSER_VERSION
        targets = (reg.enrich_pending([r["isin"] for r in reg.list_incomplete()],
                                      _CORPBONDS_QUOTA_INCOMPLETE, parser_ver=PARSER_VERSION)
                   + reg.enrich_pending([s["isin"] for s in reg.list_suspect()],
                                        _CORPBONDS_QUOTA_SUSPECT, parser_ver=PARSER_VERSION)
                   + reg.enrich_pending([e["isin"] for e in reg.list_exotic()],
                                        _CORPBONDS_QUOTA_EXOTIC, parser_ver=PARSER_VERSION)
                   + reg.enrich_pending([n["isin"] for n in reg.list_no_spec()],
                                        _CORPBONDS_QUOTA_NO_SPEC, parser_ver=PARSER_VERSION)
                   + reg.enrich_pending(_call_unknown_with_offer(),
                                        _CORPBONDS_QUOTA_CALL, parser_ver=PARSER_VERSION))
        targets = list(dict.fromkeys(targets))[:_MAX_CORPBONDS_PER_RUN]
        if targets:
            cb = await enrich_registry(targets, apply=True, delay=0.6)
            cb_stats = cb.get("stats", {})
    except Exception as e:
        logger.warning("corpbonds enrich failed: %s", e)

    # 6.5. фолбэк для тех, кого corpbonds не знает: база и маржа из истории
    #      выплат. Иначе свежие выпуски навсегда остаются без параметров —
    #      непрайсуемыми и с прочерком спреда в ленте.
    inf_stats = {}
    try:
        inf_stats = await infer_missing_params(reg=reg)
    except Exception as e:
        logger.warning("infer base/margin failed: %s", e)

    # 7. слой bondresearch.ru: наблюдаемые рынком лаг/метод фиксинга (br_* колонки,
    #    приоритет спеки manual > bondresearch > парсер > калибратор). Сбой сайта
    #    не валит синк; куцый ответ не затирает слой (sanity внутри apply_specs).
    br_stats = {}
    try:
        from services import bondresearch
        br_stats = bondresearch.apply_specs(await bondresearch.fetch_specs())
    except Exception as e:
        logger.warning("bondresearch specs sync failed: %s", e)

    # 8. бэктест спеки фиксинга по факту выплат: порция бумаг за проход
    #    (самые давно не проверенные первыми) → spec_verdict в реестре,
    #    фильтр «спека расходится» в Справочнике читает его.
    bt_stats = {}
    try:
        from services import spec_backtest
        bt_stats = await spec_backtest.run()
    except Exception as e:
        logger.warning("spec backtest failed: %s", e)

    # 9. внешняя сверка типа купона (smart-lab): единственная проверка нашего
    #    вывода НЕ нашими данными — см. services/smartlab_audit
    sl_stats = {}
    try:
        from services import smartlab_audit
        sl_stats = await smartlab_audit.run()
    except Exception as e:
        logger.warning("smart-lab audit failed: %s", e)

    stats.update({"discovered": discovered, "enriched": enriched, "retired": retired,
                  "inferred": inf_stats.get("filled", 0),
                  "sl_checked": sl_stats.get("checked", 0),
                  "sl_mismatch": sl_stats.get("mismatch", 0),
                  "sl_reverted": sl_stats.get("reverted", 0),
                  "br_specs": br_stats.get("written", 0),
                  "spec_checked": bt_stats.get("checked", 0),
                  "spec_bad": bt_stats.get("bad", 0) + bt_stats.get("warn", 0),
                  "ofz_pk_normalized": ofz_fixed,
                  "reclassified_fixed": vstats.get("reclassified_fixed", 0),
                  "suspect": vstats.get("suspect", 0),
                  "cb_exotic": cb_stats.get("exotic", 0),
                  "cb_filled": cb_stats.get("filled", 0),
                  "deactivated": traded_stats.get("deactivated", 0),
                  "reactivated": traded_stats.get("reactivated", 0),
                  "synced_at": date.today().isoformat()})
    logger.info("instruments sync: %s | registry=%s", stats, reg.count())
    return stats


_MAX_INFER_PER_RUN = 40       # калибровок базы/маржи по истории купонов за прогон
_MAX_DISCOVERY_PER_RUN = 80   # bondization-проверок новых ISIN за прогон (rate-limit)
_MAX_CORPBONDS_PER_RUN = 70   # запросов к corpbonds.ru за прогон (внешний сайт)
# квоты corpbonds-обогащения по классам очереди (Σ = cap): раздельные, чтобы
# большой incomplete не вытеснял остальные за срез
_CORPBONDS_QUOTA_INCOMPLETE = 30
_CORPBONDS_QUOTA_SUSPECT = 10
_CORPBONDS_QUOTA_EXOTIC = 10
_CORPBONDS_QUOTA_NO_SPEC = 10   # прайсуемые без текста формулы (дефолт-спека)
# бумаги с будущей офертой и неизвестным has_call (маркер p/c). Класс идёт
# ПОСЛЕДНИМ в срезе targets[:cap] — cap поднят с 60 до 70 под него, иначе
# квота четырёх старших классов (ровно 60) съедала его целиком каждый прогон.
_CORPBONDS_QUOTA_CALL = 10


async def infer_missing_params(cap: int = _MAX_INFER_PER_RUN, reg=None) -> dict:
    """База и маржа по ИСТОРИИ КУПОНОВ для бумаг, которых нет на corpbonds.

    Зачем: discovery заводит выпуск в реестр по bondization (есть будущий купон
    без суммы ⇒ флоатер), но параметры приходят только из corpbonds, а свежие
    выпуски 2025-26 он не индексирует — на 12.08.2026 таких «флоатер без базы»
    было 500. Для ленты и витрины это глухой прочерк: без базы и маржи спред не
    считается ничем.

    Факт выплат — источник не хуже проспекта: ставка купона = индекс + маржа.
    Правило приёмки в coupon_calib.infer_base_margin намеренно строгое (см. там
    же валидацию), поэтому неоднозначные выпуски остаются без базы, а не
    получают выдуманную.

    Тем же проходом ловим обратную ошибку: фикс-купонную бумагу, которую
    discovery приняла за флоатер из-за неопубликованного хвоста графика
    (coupon_calib.looks_fixed_coupons) — такие уходят в base='FIXED'.

    Сеть — только MOEX bondization, тот же дневной кэш, что у остального синка.
    """
    from services import coupon_calib as cc
    from services.market_data import MarketDataService
    if reg is None:
        from services import instruments_registry as reg
    today = date.today()
    stats = {"checked": 0, "filled": 0, "skipped": 0, "fixed": 0}
    targets = [r["isin"] for r in reg.list_incomplete()
               if r["base"] is None and not r.get("manual_locked")]
    # ротация та же, что у corpbonds: сначала ни разу не пробованные
    targets = reg.enrich_pending(targets, cap)
    filled: list[str] = []
    for isin in targets:
        row = reg.get(isin) or {}
        try:
            full = await MarketDataService.fetch_bond_schedule_full(isin)
        except Exception:
            continue                  # сетевой сбой — не помечаем, повторим
        stats["checked"] += 1
        coupons = (full or {}).get("coupons") or []
        amorts = (full or {}).get("amortizations") or []
        face = row.get("face_value") or 1000.0
        fx = cc.looks_fixed_coupons(coupons, face, today, amorts)
        if fx:
            reg.reclassify_fixed(isin)      # reviewed=0 — на подтверждение админом
            reg.mark_enrich_attempt(isin, "filled")
            stats["fixed"] += 1
            logger.info("infer %s: ФИКС %.2f%% (КС ходила на %.1fпп, %d купонов)",
                        isin, fx["rate"], fx["ks_span_pp"], fx["n"])
            continue
        spec, why = cc.infer_base_margin(coupons, face, today, amorts)
        if not spec:
            stats["skipped"] += 1
            logger.debug("infer %s: %s", isin, why)
            reg.mark_enrich_attempt(isin, "nodata")
            continue
        # пишем ТОЛЬКО базу и маржу: режим/лаг фиксинга дальше определяет
        # обычная цепочка (парсер проспекта > калибратор), у неё правил больше
        reg.upsert({"isin": isin, "base": spec["base"], "margin_bps": spec["margin_bps"]},
                   source="coupon-calib")
        reg.mark_enrich_attempt(isin, "filled")
        filled.append(isin)
        stats["filled"] += 1
        logger.info("infer %s: %s +%dбп (err %.3fпп, %d купонов, разброс %.1fпп)",
                    isin, spec["base"], spec["margin_bps"], spec["err_pp"],
                    spec["n"], spec["span_pp"])
    if filled:
        # у сделок этих бумаг спред был закрыт прочерком как у «не-флоатера» —
        # возвращаем их в очередь расчёта, иначе прочерк остался бы навсегда
        try:
            from services import block_trades as bt
            stats["requeued"] = await asyncio.to_thread(bt.reset_metrics, filled)
        except Exception as e:
            logger.warning("infer: пересчёт спреда не запущен: %s", e)
    return stats


def _call_unknown_with_offer() -> list[str]:
    """Кандидаты на выяснение call-опциона: has_call IS NULL И есть будущая оферта
    в day-кэше bondization. Без сети (cached_schedule) — не прогретые расписания
    просто не попадают в срез, доберутся следующим прогоном."""
    from core.valuation import next_offer_info
    from services import instruments_registry as reg
    from services.market_data import MarketDataService
    today = date.today()
    out = []
    for r in reg.list_call_unknown():
        sched = MarketDataService.cached_schedule(r["isin"])
        if sched and next_offer_info(sched.get("offers"), today):
            out.append(r["isin"])
    return out


def _looks_ofz_pk(name_upper: str, isin: str) -> bool:
    """ОФЗ-ПК (Минфин, купон плавает по RUONIA) — по имени/названию выпуска.
    SU29xxx — тикерный префикс серии ОФЗ-ПК на MOEX. Только имя: клауза по
    isin[8:10]=='29' была совпадением двух символов случайного кода, ловила
    чужие бумаги (RU000A1029M4 «Автодор» → base=RUONIA при базе КС)."""
    return ("ОФЗ-ПК" in name_upper or "ОФЗ ПК" in name_upper
            or "SU29" in name_upper
            or bool(re.match(r"ОФЗ\s*29\d{3}\b", name_upper)))


async def discover_floaters(listing: dict | None = None,
                            cap: int = _MAX_DISCOVERY_PER_RUN,
                            delay: float = 0.25, reg=None) -> int:
    """Найти НОВЫЕ флоатеры среди торгуемых на MOEX и завести их в реестр.

    Флоатер ⇔ у бумаги есть БУДУЩИЙ купон с незафиксированной суммой (value=None,
    сигнал из MOEX bondization); у фикс-купонных все value известны заранее.

    Кандидат = ISIN из листинга, которого нет ни в реестре, ни в negative-кэше
    discovery_seen (уже проверен). coupon_percent — НЕ отсечка, а лишь ПРИОРИТЕТ
    порядка: у флоатера текущий период зафиксирован → cp задан, поэтому фильтр по
    cp None/0 терял ~28% флоатеров (ВЭБ/РЖД/ДОМ.РФ и т.п.). Проверяем ВСЕХ, но
    вероятных (cp None/0) — первыми. Negative-кэш → каждый ISIN чекается ровно раз,
    cap не голодает, бэклог листинга сходится за проходы. delay — мягкий rate-limit."""
    from services.market_data import MarketDataService
    if reg is None:
        from services import instruments_registry as reg
    if listing is None:
        try:
            listing = await MarketDataService.fetch_bond_listing()
        except Exception as e:
            logger.warning("discovery listing failed: %s", e)
            return 0
    if not listing:
        return 0
    # приоритет: вероятные флоатеры (cp None/0) вперёд, остальные следом — но в
    # обоих случаях проверяем bondization'ом (cp не решает исход)
    cand = sorted(listing.keys(),
                  key=lambda i: 0 if listing[i].get("coupon_percent") in (None, 0.0) else 1)
    pending = reg.discovery_pending(cand, cap)
    discovered = 0
    for isin in pending:
        try:
            full = await MarketDataService.fetch_bond_schedule_full(isin)
        except Exception:
            continue  # сетевой сбой — НЕ помечаем seen, повторим в следующий проход
        coupons = (full or {}).get("coupons") or []
        if not coupons:
            # нет графика (структурная нота без bondization / свежий выпуск) —
            # помечаем NULL: перечекнётся после TTL, но не забивает cap каждый цикл
            reg.mark_discovery_seen(isin, None)
            continue
        is_fl = any(c.get("value") is None for c in coupons)
        reg.mark_discovery_seen(isin, is_fl)
        if is_fl:
            mo = listing[isin]
            # купонный период — из ФАКТИЧЕСКОГО графика (два последних купона /
            # размещение+первый), а не round(365/freq). Точнее для генерации
            # форвард-графика без bondization и для display в Справочнике.
            from core.cashflow import coupon_period_from_coupons
            cpd = coupon_period_from_coupons(coupons, issue_date=mo.get("issue"),
                                             today=date.today())
            row = {"isin": isin, "short_name": mo.get("short_name"),
                   "maturity_date": mo.get("maturity"), "face_value": mo.get("face")}
            if cpd and cpd > 0:
                row["coupon_period_days"] = cpd
                row["coupons_per_year"] = max(1, round(365 / cpd))
            reg.upsert(row, source="moex", mark_new=True)
            discovered += 1
        if delay:
            await asyncio.sleep(delay)
    await asyncio.to_thread(MarketDataService.flush_schedule_cache)   # дозапись хвоста дебаунс-кэша
    return discovered


def _f(x):
    try:
        return float(x) if x is not None else None
    except (ValueError, TypeError):
        return None
