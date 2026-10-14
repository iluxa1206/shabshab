"""MOEX EMITTER_ID для бумаг вне реестра (фиксы): {isin: [emitter_id, title]}.

Карта всего рынка облигаций собирается списком ISS /securities.json (≈32
страницы по 100, секунды) вместо description по бумаге (≈1500 запросов).
Эмитент выпуска не меняется — карта durable (data/cache/emitters.json), полный
пересбор раз в сутки, раньше — только если встретились ISIN без эмитента и с
прошлого пересбора прошло ≥ 6 ч (свежие выпуски)."""
import logging
import time
from typing import Dict, Iterable, Optional

from services import paths

logger = logging.getLogger(__name__)

CACHE_FILE = "emitters.json"
_FULL_SEC = 24 * 3600
_MISSING_SEC = 6 * 3600
_PAGE = 100
_MAX_PAGES = 200            # предохранитель от бесконечного листания
_URL = ("https://iss.moex.com/iss/securities.json?engine=stock&market=bonds"
        "&is_trading=1&iss.meta=off&limit={limit}&start={start}"
        "&securities.columns=isin,emitent_id,emitent_title")

_mem: Dict[str, object] = {"doc": None}


def _doc() -> dict:
    if _mem["doc"] is None:
        try:
            import json
            with open(paths.cache_path(CACHE_FILE), encoding="utf-8") as f:
                _mem["doc"] = json.load(f)
        except (OSError, ValueError):
            _mem["doc"] = {"ts": 0, "map": {}}
    return _mem["doc"]


def emitter_of(isin: str) -> Optional[list]:
    """[emitter_id, title] или None."""
    return _doc()["map"].get(isin)


async def _fetch(client) -> dict:
    out: dict = {}
    for page in range(_MAX_PAGES):
        r = await client.get(_URL.format(limit=_PAGE, start=page * _PAGE), timeout=30)
        r.raise_for_status()
        rows = r.json()["securities"]["data"]
        if not rows:
            break
        for isin, eid, title in rows:
            if isin and eid is not None:
                out[isin] = [int(eid), title]
    return out


async def ensure(isins: Iterable[str]) -> None:
    """Пересобрать карту, если она устарела или не знает кого-то из isins.
    Ошибка сети не роняет вызывающего: остаётся прежняя карта."""
    doc = _doc()
    age = time.time() - float(doc.get("ts") or 0)
    missing = any(i not in doc["map"] for i in isins)
    if age < _FULL_SEC and not (missing and age >= _MISSING_SEC):
        return
    from services.market_data import moex_client
    try:
        async with moex_client() as client:
            fresh = await _fetch(client)
    except Exception as e:
        logger.warning("emitters: ISS list failed: %s", e)
        return
    # куцый ответ (обрыв на середине листания) не затирает полную карту
    if len(fresh) < 0.5 * len(doc["map"]):
        logger.warning("emitters: ответ подозрительно мал (%d против %d) — оставляю прежнюю карту",
                       len(fresh), len(doc["map"]))
        return
    new_doc = {"ts": time.time(), "map": {**doc["map"], **fresh}}
    paths.atomic_write_json(paths.cache_path(CACHE_FILE), new_doc)
    _mem["doc"] = new_doc
    logger.info("emitters: карта эмитентов обновлена, %d бумаг", len(fresh))


# Синтетический эмитент Минфина — тот же, что у ОФЗ-флоатеров в реестре
# (services.instruments_registry._OFZ_EMITTER_ID): MOEX у ОФЗ EMITTER_ID не даёт.
OFZ_EMITTER = (-1, "Минфин России")


def registry_names() -> dict:
    """{emitter_id: имя} из реестра: у флоатеров оно короткое и каноничное, и
    эмитент фикса под тем же id должен называться так же — иначе фильтр «все
    бумаги эмитента» делит одного эмитента на два имени."""
    from services import instruments_registry as reg
    try:
        rows = reg.universe_rows(only_floaters=False, only_priceable=False)
    except Exception as e:
        logger.warning("registry emitter names failed: %s", e)
        return {}
    return {r["emitter_id"]: r["emitter_name"] for r in rows
            if r.get("emitter_id") and r.get("emitter_name")}


def fixed_emitter(u: dict, canon: Optional[dict] = None) -> tuple:
    """(emitter_id, имя) строки универса фиксов. canon — registry_names()
    (передают, когда зовут в цикле). Нет эмитента — id None, имя из имени выпуска."""
    if u.get("cls") == "ofz":
        return OFZ_EMITTER
    eid, _title = emitter_of(u.get("isin")) or (None, None)
    name = (canon if canon is not None else registry_names()).get(eid)
    return eid, name or u.get("issuer")
