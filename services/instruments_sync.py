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

    # 2. добор maturity/issue/face из MOEX для бумаг без даты погашения
    #    (Cbonds не даёт maturity; без неё флоатер не прайсится — perp guard)
    missing = [r["isin"] for r in reg.universe_rows() if not r.get("maturity_date")]
    enriched = 0
    if missing:
        try:
            secs = await MarketDataService.fetch_moex_securities(missing)
        except Exception as e:
            logger.warning("MOEX securities enrich failed: %s", e)
            secs = {}
        for isin, mo in (secs or {}).items():
            if not mo:
                continue
            upd = {"isin": isin,
                   "maturity_date": mo.get("maturity"),
                   "issue_date": mo.get("issue"),
                   "face_value": _f(mo.get("face"))}
            if any(v is not None for k, v in upd.items() if k != "isin"):
                reg.upsert(upd, source="moex", mark_new=False)
                enriched += 1
    stats["enriched"] = enriched
    stats["synced_at"] = date.today().isoformat()
    logger.info("instruments sync: %s", stats)
    return stats


def _f(x):
    try:
        return float(x) if x is not None else None
    except (ValueError, TypeError):
        return None
