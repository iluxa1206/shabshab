"""Подписи бумаг для ленты сделок, фильтров сигналов и алертов — реестр + фиксы.

Реестр инструментов знает флоатеры (и часть фиксов), кэш универса фиксов —
остальное. Раньше сигналы и алерты брали только `labels_map()` реестра: у фикса
вне реестра не было ни имени, ни рейтинга, ни срока, и фильтры по рейтингу,
эмитенту и окну срока такую сделку молча отбрасывали. Лента сделок уже
достраивала подписи из универса фиксов (api/routes/blocks._labels) — теперь это
один код на всех.
"""
from __future__ import annotations

import time
from typing import Dict, Optional

_TTL = 300.0
_fx_cache: dict = {"at": 0.0, "fp": None, "map": {}}


def _fixed_labels() -> Dict[str, dict]:
    """{isin: подпись} по универсу фиксов; кэш на 5 минут (рейтинги — SQL-батч)."""
    from services.market_data import market_cache
    uni = market_cache.get("fixed_universe") or []
    fp = len(uni)
    now = time.monotonic()
    if _fx_cache["map"] and _fx_cache["fp"] == fp and now - _fx_cache["at"] < _TTL:
        return _fx_cache["map"]
    out: Dict[str, dict] = {}
    if uni:
        from services import emitters, ratings, ratings_br
        ids = [u["isin"] for u in uni if u.get("isin")]
        try:
            rmap = ratings.bucket_map_fixed([(u["isin"], u.get("cls")) for u in uni if u.get("isin")])
            steps = {i: r["rating"] for i, r in ratings_br.ratings_map(ids).items() if r.get("rating")}
        except Exception:
            rmap, steps = {}, {}
        try:
            canon = emitters.registry_names()
        except Exception:
            canon = {}
        for u in uni:
            isin = u.get("isin")
            if not isin:
                continue
            _eid, ename = emitters.fixed_emitter(u, canon)
            out[isin] = {"name": u.get("name") or isin, "emitter": ename or u.get("issuer"),
                         "base": "FIXED",
                         # ступень худшего рейтинга, иначе крупная шкала; ОФЗ — AAA
                         "rating": (None if u.get("cls") == "ofz" else steps.get(isin)) or rmap.get(isin),
                         "maturity": u.get("maturity_date"),
                         "perpetual": bool(u.get("perpetual")),
                         "faceunit": u.get("faceunit")}
    _fx_cache.update({"at": now, "fp": fp, "map": out})
    return out


def labels_all(isins: Optional[list] = None) -> Dict[str, dict]:
    """Реестр + фиксы. Приоритет у реестра: у флоатера подпись короче и каноничнее;
    пустые поля реестра добираются из универса фиксов."""
    from services import instruments_registry as reg
    out = dict(reg.labels_map() if isins is None else reg.labels_map(isins))
    for isin, fx in _fixed_labels().items():
        if isins is not None and isin not in isins:
            continue
        cur = out.get(isin)
        if cur is None:
            out[isin] = dict(fx)
            continue
        for k in ("emitter", "rating", "maturity"):
            if not cur.get(k) and fx.get(k):
                cur[k] = fx[k]
        cur["base"] = cur.get("base") or "FIXED"
    return out


def pricing_kind(isin: str, labels: dict) -> Optional[str]:
    """Каким движком считать спред сделки: "floater" | "fixed" | None (нечем).

    Фикс считаем только там, где Y-IDX определён: рублёвый номинал, не
    бессрочная (у валютных база RUONIA не применима — тот же guard, что в
    таблице, и тогда спред сделки «н/о», а не прочерк-поломка)."""
    lb = labels.get(isin) or {}
    base = lb.get("base")
    if base in ("KEYRATE", "RUONIA"):
        return "floater"
    if lb.get("perpetual"):
        return None
    if lb.get("faceunit") is not None:
        from services.currency import is_ruble
        if not is_ruble(lb.get("faceunit")):
            return None
        return "fixed"
    return None
