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

# Автоподбор спеки за проход синка: считает по прошлым купонам каждой бумаги и
# ходит в MOEX за расписанием, поэтому берём порцию — очередь бэктеста (шаг 8)
# и так подаёт кандидатов постепенно.
_AUTOFIT_LIMIT = 12
_AUTOFIT_HISTORY_DAYS = 400

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
        # FACEVALUE замещающих/юаневых бумаг — В ВАЛЮТЕ (FACEUNIT), а расчёты у
        # нас рублёвые. Такой номинал в реестре победил бы isins_cache в
        # BondRefData (services/bonds.py:46) и испортил PV/НКД в 12-83 раза.
        _unit = (mo.get("face_unit") or "").upper()
        _face = mo.get("face") if _unit in ("", "SUR", "RUB", "RUR") else None
        upd = {"isin": isin, "maturity_date": mo.get("maturity"),
               "short_name": mo.get("short_name"), "face_value": _face}
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
    #    Добираем и ДАТУ РАЗМЕЩЕНИЯ: она нужна не для расчёта, а чтобы отличать
    #    свежий выпуск от старого при перепопытках обогащения. Порядок — сначала
    #    бумаги без maturity (без неё бумага не считается вовсе), затем без
    #    issue_date; срез на прогон, потому что запрос идёт ПОШТУЧНО.
    no_mat = [r["isin"] for r in reg.universe_rows(only_priceable=False, only_floaters=False)
              if not r.get("maturity_date")]
    # непрайсуемые — ради формулы купона из карточки биржи (COUPON_BENCHMARK);
    # квота отдельная, иначе бэклог 400+ вытеснит добор issue_date насовсем
    incompl = reg.isins_incomplete_newest_first()[:_MAX_SECMASTER_INCOMPLETE]
    picked = set(no_mat) | set(incompl)
    no_issue = [i for i in reg.isins_missing_issue_date() if i not in picked]
    missing = (no_mat + incompl + no_issue)[:_MAX_SECMASTER_PER_RUN]
    enriched = 0
    exotic: list[tuple[str, str]] = []
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
            # формула купона из карточки биржи — единственный источник, который
            # знает СВЕЖИЙ выпуск в день размещения (corpbonds доливает неделями)
            b_base, b_bps, b_exotic = _benchmark_params(mo)
            if b_exotic:
                exotic.append((isin, b_exotic))
            elif b_base:
                cur = reg.get(isin) or {}
                if not cur.get("base"):
                    upd["base"] = b_base
                if cur.get("margin_bps") is None and b_bps is not None:
                    upd["margin_bps"] = b_bps
            if any(v is not None for k, v in upd.items() if k != "isin"):
                reg.upsert(upd, source="moex", mark_new=False)
                enriched += 1
        # G-кривая как бенчмарк — вне линейной модели «индекс + маржа»: помечаем
        # экзотикой, а не заводим с ложной базой (ошибка была бы тихой: бумага
        # попала бы в универс и считалась бы как КС-флоатер)
        for isin, note in exotic:
            reg.set_exotic(isin, note)

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
                                        _CORPBONDS_QUOTA_CALL, parser_ver=PARSER_VERSION)
                   + reg.enrich_pending([c["isin"] for c in reg.list_call_dates_missing()],
                                        _CORPBONDS_QUOTA_CALL_DATES, parser_ver=PARSER_VERSION))
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

    # 8b. АВТОПОДБОР спеки для расходящихся + пересчёт их истории.
    #     Шаг 8 только ПОМЕЧАЕТ вердиктом, и пометка может пролежать месяцами:
    #     ОФЗ 29008/29009/29010 висели с BAD и ошибкой 2.4–2.9 пп в ставке
    #     купона, пока их не нашли отдельным прогоном 21.08.2026. Замыкаем петлю:
    #     что чинится лагом с подтверждением на отложенных купонах — чиним, что
    #     не чинится — оставляем помеченным (там другая причина, обычно маржа).
    fit_stats = {}
    try:
        from services import spec_autofit
        fit_stats = await spec_autofit.autofit(apply=True, limit=_AUTOFIT_LIMIT)
        if fit_stats.get("isins"):
            # правка спеки меняет ПРОШЛЫЕ купоны в проекции, а история спреда
            # живёт отдельно от витрины: без пересчёта график остаётся на старых
            # числах до бампа версии движка
            from services.backdate import ensure_honest_backfill
            from services.spread_history import drop_honest
            for isin in fit_stats["isins"]:
                try:
                    drop_honest(isin)
                    await ensure_honest_backfill(isin, _AUTOFIT_HISTORY_DAYS)
                except Exception as e:
                    logger.warning("autofit history %s: %s", isin, e)
        for isin, name, cur, sug in (fit_stats.get("suspect_margin") or []):
            logger.warning("СПЕКА: %s %s — лагом не лечится, похоже смещена маржа "
                           "%s → ~%s bps (нужен разбор руками)", isin, name, cur, sug)
    except Exception as e:
        logger.warning("spec autofit failed: %s", e)

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
                  "spec_autofixed": fit_stats.get("applied", 0),
                  "spec_margin_suspect": len(fit_stats.get("suspect_margin") or []),
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
_MAX_CORPBONDS_PER_RUN = 80   # запросов к corpbonds.ru за прогон (внешний сайт)
_MAX_SECMASTER_PER_RUN = 150  # запросов в справочник MOEX за прогон (ПОШТУЧНО:
                              # /iss/securities/{isin}.json, батча у ISS нет)
_MAX_SECMASTER_INCOMPLETE = 60   # из них — на непрайсуемые (свежие выпуски вперёд)
# квоты corpbonds-обогащения по классам очереди (Σ = cap): раздельные, чтобы
# большой incomplete не вытеснял остальные за срез
_CORPBONDS_QUOTA_INCOMPLETE = 30
_CORPBONDS_QUOTA_SUSPECT = 10
_CORPBONDS_QUOTA_EXOTIC = 10
_CORPBONDS_QUOTA_NO_SPEC = 10   # прайсуемые без текста формулы (дефолт-спека)
# бумаги с будущей офертой и неизвестным has_call (маркер p/c). Класс идёт
# ПРЕДПОСЛЕДНИМ в срезе targets[:cap] — cap поднят с 60 до 80 под него и класс
# дат колла, иначе квота старших классов съедала бы их целиком каждый прогон.
_CORPBONDS_QUOTA_CALL = 10
# бумаги, у которых колл возможен (has_call=1/NULL), но ДАТ его нет: без даты
# колл не может стать горизонтом прайсинга. Разово их закрывает
# scripts/backfill_call_dates.py, эта квота лишь поддерживает — класс иссякает.
_CORPBONDS_QUOTA_CALL_DATES = 10


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


def _benchmark_params(mo: dict) -> tuple[str | None, int | None, str | None]:
    """Разбор COUPON_BENCHMARK/COUPON_BENCHMARK_SPREAD из карточки MOEX →
    (base, margin_bps, exotic_note).

    Коды биржи: RREFKEYR — ключевая ставка ЦБ, RUONIA — RUONIA, ZR_YLD_CRV —
    G-кривая ОФЗ. Последняя линейной моделью «индекс + маржа» не считается, и
    завести её как КС-флоатер хуже, чем не заводить вовсе: бумага попала бы в
    универс с молча неверным спредом. Неизвестный код тоже не трактуем.
    """
    b = (mo.get("benchmark") or "").strip().upper()
    if not b:
        return None, None, None
    spread = mo.get("benchmark_spread")
    bps = None if spread is None else int(round(float(spread) * 100))
    if b in ("RREFKEYR", "KEYRATE", "KEY_RATE"):
        return "KEYRATE", bps, None
    if b == "RUONIA":
        return "RUONIA", bps, None
    if b == "ZR_YLD_CRV":
        return None, None, f"MOEX benchmark {b} + {spread}"
    logger.info("неизвестный COUPON_BENCHMARK %r — пропускаем", b)
    return None, None, None


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
        mo = listing[isin]
        # ЛИНКЕР RUONIA — второй вид флоатера, которого правило «есть купон с
        # value=None» не ловит: ставка у него ФИКСИРОВАНА, плавает номинал, и
        # MOEX проставляет сумму во все купоны сразу. Без этой ветки ВЭБ2Р-58 и
        # его будущие собратья уходили в negative-кэш как фиксы и оседали во
        # вкладке ФИКСЫ с YTM, посчитанным на застывшем номинале.
        from services import linker as _lnk
        linked = False
        if not is_fl:
            try:
                linked = _lnk.is_ruonia_linked(coupons, mo.get("face"))
            except Exception as e:
                logger.warning("детект линкера %s: %s", isin, e)
            is_fl = linked
        reg.mark_discovery_seen(isin, is_fl)
        if is_fl:
            # купонный период — из ФАКТИЧЕСКОГО графика (два последних купона /
            # размещение+первый), а не round(365/freq). Точнее для генерации
            # форвард-графика без bondization и для display в Справочнике.
            from core.cashflow import coupon_period_from_coupons
            cpd = coupon_period_from_coupons(coupons, issue_date=mo.get("issue"),
                                             today=date.today())
            row = {"isin": isin, "short_name": mo.get("short_name"),
                   "maturity_date": mo.get("maturity"), "face_value": mo.get("face")}
            if linked:
                # Параметры линкера известны сразу и целиком: база сравнения —
                # RUONIA, «маржа выпуска» — та самая фиксированная ставка купона
                # (у ВЭБ2Р-58 1.85% ⇒ 185 bps), поэтому бумага заводится
                # прайсуемой, а не висит в очереди ревью без базы.
                rate = _lnk._fixed_rate_pct(coupons)
                row["base"] = "RUONIA"
                row["face_index"] = _lnk.RUONIA
                if rate is not None:
                    row["margin_bps"] = int(round(rate * 100))
                # номинал у линкера биржевой и растёт ежедневно — снимок дня
                # заведения в реестре только мешал бы (см. bonds.apply_registry_params)
                row.pop("face_value", None)
            # дата размещения — из САМОГО раннего купона графика. Листинг MOEX её
            # не отдаёт (колонки ISIN/SHORTNAME/MATDATE/COUPONPERCENT/FACEVALUE),
            # поэтому mo.get("issue") здесь всегда None, и у заведённых дискавери
            # бумаг issue_date оставался пустым. Без него нечем отличить свежий
            # выпуск от старого — а на этом стоит ежедневная перепопытка
            # обогащения (registry._FRESH_ISSUE_DAYS).
            starts = sorted(c.get("start") for c in coupons if c.get("start"))
            if starts:
                row["issue_date"] = starts[0]
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
