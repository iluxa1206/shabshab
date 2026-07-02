"""
Адаптер НРД Ценового центра (NSDDATA API v2, OpenAPI 3.0).

Контракт подтверждён по официальной спеке https://nsddata.ru/ru/products/api/scheme :
  Авторизация:
    POST /api/auth/login    {login, password}        -> {access_token, refresh_token}
    POST /api/auth/refresh   {refresh_token}          -> {access_token, refresh_token}
    GET  /api/auth/check     (Bearer)                 -> 200 | 401
  Данные (Bearer в заголовке Authorization):
    GET /api/get/valuationnew?filter=<json>&limit=&skip=     -> [ValuationNew]
    GET /api/get/valuationnewadd?filter=<json>&limit=&skip=  -> [ValuationNewAdd]

  filter — JSON-объект (Mongo-стиль) в query:
    {"isin": "RU000A10BF48"}
    {"isin": {"$in": ["...","..."]}}
    {"val_date": {"$gte": "2025-10-01", "$lte": "2025-10-02"}}

Конфиг в .env: NRD_LOGIN, NRD_APIKEY (пароль = apikey подписки), NRD_API_BASE (по умолчанию
https://nsddata.ru), NRD_SPREAD_UNIT (bps|percent — единицы спредов в ответе НРД).

Грейсфул: без NRD_LOGIN/NRD_APIKEY -> пустой результат, вызывающий код отдаёт nrd=None.
"""
from __future__ import annotations

import os
import json
import time
import asyncio
import logging
from pathlib import Path
from datetime import date
from typing import Dict, List, Optional, Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Сериализуем login/refresh: без лока параллельные корутины плодят
# дубль-логины (rate-limit) и гоняют невалидированную запись кэша токена.
_auth_lock = asyncio.Lock()

# Правдоподобный диапазон спреда/маржи флоатера в bps. Масштаб полей НРД —
# эвристика (×10000 vs ×100); если результат вне диапазона — вероятна смена
# единиц на стороне НРД: логируем и обнуляем, чтобы кривой спред не утёк в купон.
_BPS_SANE_MAX = 20000  # 200%

NRD_API_BASE = os.getenv("NRD_API_BASE", "https://nsddata.ru").rstrip("/")
NRD_LOGIN = os.getenv("NRD_LOGIN", "")
NRD_APIKEY = os.getenv("NRD_APIKEY", "")

# Подтверждено на живых данных: НРД отдаёт доходности/спреды ДОЛЯМИ (0.017 = 1.7%).
# yields: доля -> % (×100). spreads: доля -> bps (×10000).
SCALE_PCT = 100.0
SCALE_BPS = 10000.0

PATH_LOGIN = "/api/auth/login"
PATH_REFRESH = "/api/auth/refresh"
PATH_VALUATION = "/api/get/valuationnew"
PATH_VALUATION_ADD = "/api/get/valuationnewadd"

CACHE_FILE = Path("nrd_cache.json")
UNIVERSE_FILE = Path("nrd_universe_cache.json")
TOKEN_CACHE_FILE = Path("nrd_token_cache.json")
HTTP_TIMEOUT = 30.0
# Бамп при изменении маппинга/масштабов — старый кэш с другим scale инвалидируется
CACHE_VERSION = 3  # v3: юниверс + current_yield_pct/nrd_calc_date (флоатер-метрики)

# База купона НРД -> наш тип (для юниверса)
_UNI_BASE = {"CBRATED": "KEYRATE", "KEYRATE": "KEYRATE", "RUONIARATED": "RUONIA", "RUONIA": "RUONIA"}

import re as _re


def _rating_grade(s):
    """Из строки рейтинга ('AAA(RU)', 'ruAAA', 'AA+.ru') достаёт грейд ('AAA','AA+')."""
    if not s:
        return None
    t = _re.sub(r"\(RU\)|\.RU|\|RU\||RU", "", str(s).upper())
    m = _re.search(r"([ABCD]{1,3})([+-]?)", t)
    return (m.group(1) + m.group(2)) if m else None


def rating_bucket(ratings: dict):
    """Бакет для фильтра: AAA/AA/A/BBB/BB/... по лучшему из агентств (АКРА→Эксперт→НКР→НРА)."""
    if not ratings:
        return None
    for k in ("akra", "expert_ra", "nkr", "nra"):
        g = _rating_grade(ratings.get(k))
        if g:
            return g.rstrip("+-")  # AAA / AA / A / BBB / BB / B / CCC
    return None

# ---------------------------------------------------------------------------
# Карты полей (ключи подтверждены на живых данных valuationnewadd).
#   MAP_DIRECT — без масштабирования (цены в %, числа как есть)
#   MAP_PCT    — доля -> процент (x100): доходности
#   MAP_BPS    — доля -> bps (x10000): спреды/маржи
# fair_value (rate) — из valuationnew; у текущей подписки доступа нет (403) -> None.
# ---------------------------------------------------------------------------
MAP_DIRECT: Dict[str, str] = {
    "fair_value_pct": "rate",              # valuationnew (может отсутствовать)
    "valuation_method": "method_number",   # valuationnew
    "duration": "duration",
    "mod_duration": "mod_duration",
    "convexity": "convexity",
    "pvbp": "pvbp",
    "nrd_price_pct": "wa_price",           # средневзвешенная цена осн. сессии, %
    "nrd_close_pct": "legal_close_price",  # цена аукциона закрытия, %
    "base_coupon_index": "base_coupon_index",  # база флоатера (напр. CBRATED)
    "coupon_type": "coupon_type",          # fix / float
    "nrd_calc_date": "val_date",
}
MAP_PCT: Dict[str, str] = {
    "ytm_pct": "yield_maturity",
    "current_yield_pct": "current_yield",
    "yield_maturity_pct": "yield_maturity",
    "yield_call_pct": "yield_call",
    "yield_put_pct": "yield_put",
    "ytm_close_pct": "ytm_close",
}
# Подтверждено на живых данных: масштаб у спредов РАЗНЫЙ.
#   FRAC (доля -> bps, ×10000): z_spread/g_spread/nominal_margin
#     (nominal_margin 0.017 -> 170 bps, совпало со спредом выпуска)
#   PCT  (процент -> bps, ×100): simple_margin/discount_margin
#     (discount_margin 1.93 -> 193 bps)
MAP_BPS_FRAC: Dict[str, str] = {
    "z_spread_bps": "z_spread",
    "g_spread_bps": "g_spread",
    "nominal_margin_bps": "nominal_margin",
}
MAP_BPS_PCT: Dict[str, str] = {
    "discount_margin_bps": "discount_margin",
    "simple_margin_bps": "simple_margin",
}

NRD_RATING_MAP: Dict[str, str] = {
    "akra": "akra_rating",
    "expert_ra": "expert_rating",
    "nkr": "ncr_rating",
    "nra": "nra_rating",
}

NRD_LIQUIDITY_MAP: Dict[str, str] = {
    "volume_rub": "trade_volume_rub",
    "volume_qty": "trade_volume_qty",
    "mode_volume": "mode_trade_volume",
    "q50_volume": "quantile_050_trade_volume",
}

_VAL_DATE_KEYS = ("val_date", "est_date")
_STR_FIELDS = ("valuation_method", "base_coupon_index", "coupon_type", "nrd_calc_date")


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, str):
            v = v.replace(" ", "").replace(" ", "").replace(",", ".")
        return float(v)
    except (ValueError, TypeError):
        return None


def _map_response(raw_new: dict, raw_add: dict) -> dict:
    """Сырые ответы методов -> нормализованный блок BondNrd."""
    merged = {**(raw_new or {}), **(raw_add or {})}
    out: Dict[str, Any] = {}

    for f, key in MAP_DIRECT.items():
        raw = merged.get(key)
        out[f] = (raw if raw not in ("", None) else None) if f in _STR_FIELDS else _num(raw)
    for f, key in MAP_PCT.items():
        v = _num(merged.get(key))
        out[f] = round(v * SCALE_PCT, 4) if v is not None else None
    for f, key in MAP_BPS_FRAC.items():
        v = _num(merged.get(key))
        out[f] = _sane_bps(int(round(v * SCALE_BPS)), f) if v is not None else None
    for f, key in MAP_BPS_PCT.items():
        v = _num(merged.get(key))
        out[f] = _sane_bps(int(round(v * SCALE_PCT)), f) if v is not None else None

    ratings = {k: merged.get(key) for k, key in NRD_RATING_MAP.items()
               if merged.get(key) not in ("", None)}
    out["ratings"] = ratings or None

    liquidity = {k: _num(merged.get(key)) for k, key in NRD_LIQUIDITY_MAP.items()
                 if merged.get(key) not in ("", None)}
    out["liquidity"] = liquidity or None

    out["source"] = "NRD Price Center"
    return out


# ---------------------------------------------------------------------------
# Дисковый кэш данных (НРД обновляется раз в день с 08:00 МСК)
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # атомарная замена — нет полу-записанного кэша
    except OSError:
        pass


def _sane_bps(v: Optional[int], field: str) -> Optional[int]:
    """Guard масштаба: bps вне правдоподобного диапазона → None + лог."""
    if v is None:
        return None
    if abs(v) > _BPS_SANE_MAX:
        logger.warning("NRD %s = %d bps вне диапазона (±%d) — вероятно сменились "
                       "единицы, обнуляю", field, v, _BPS_SANE_MAX)
        return None
    return v


def is_configured() -> bool:
    return bool(NRD_LOGIN and NRD_APIKEY)


# ---------------------------------------------------------------------------
# Авторизация: login -> access/refresh, refresh при 401, повторный login при провале
# ---------------------------------------------------------------------------
async def _login(client: httpx.AsyncClient) -> Optional[str]:
    resp = await client.post(f"{NRD_API_BASE}{PATH_LOGIN}",
                             json={"login": NRD_LOGIN, "password": NRD_APIKEY},
                             timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    _save_json(TOKEN_CACHE_FILE, {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "ts": time.time(),
    })
    return data.get("access_token")


async def _refresh(client: httpx.AsyncClient) -> Optional[str]:
    cache = _load_json(TOKEN_CACHE_FILE)
    rt = cache.get("refresh_token")
    if not rt:
        return await _login(client)
    try:
        resp = await client.post(f"{NRD_API_BASE}{PATH_REFRESH}",
                                 json={"refresh_token": rt}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        _save_json(TOKEN_CACHE_FILE, {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token", rt),
            "ts": time.time(),
        })
        return data.get("access_token")
    except Exception:
        return await _login(client)


async def _ensure_token(client: httpx.AsyncClient, current: Optional[str]) -> Optional[str]:
    """Достаёт валидный токен под локом. Если другая корутина уже обновила
    (в кэше токен != current) — возвращаем свежий без нового логина."""
    async with _auth_lock:
        cache = _load_json(TOKEN_CACHE_FILE)
        tok = cache.get("access_token")
        if tok and tok != current:
            return tok
        if cache.get("refresh_token"):
            return await _refresh(client)
        return await _login(client)


async def _get_json(client: httpx.AsyncClient, path: str, params: dict) -> Any:
    """GET с Bearer; при 401 — refresh/login и один повтор."""
    cache = _load_json(TOKEN_CACHE_FILE)
    token = cache.get("access_token") or await _ensure_token(client, None)
    for attempt in range(2):
        resp = await client.get(f"{NRD_API_BASE}{path}", params=params,
                                headers={"Authorization": f"Bearer {token}"},
                                timeout=HTTP_TIMEOUT)
        if resp.status_code == 401 and attempt == 0:
            token = await _ensure_token(client, token)
            continue
        resp.raise_for_status()
        return resp.json()
    return None


def _rows_by_isin(payload: Any) -> Dict[str, dict]:
    """Ответ -> {isin: самая свежая строка по val_date}."""
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("items") or []
    result: Dict[str, dict] = {}
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        isin = row.get("isin") or row.get("ISIN")
        if not isin:
            continue
        prev = result.get(isin)
        if prev is None:
            result[isin] = row
        else:
            d_new = next((row.get(k) for k in _VAL_DATE_KEYS if row.get(k)), "")
            d_old = next((prev.get(k) for k in _VAL_DATE_KEYS if prev.get(k)), "")
            if str(d_new) >= str(d_old):
                result[isin] = row
    return result


async def _fetch_method(client: httpx.AsyncClient, path: str, isins: List[str]) -> Dict[str, dict]:
    """Один метод -> {isin: строка}. 403 (нет доступа к продукту) и прочие ошибки
    метода не валят весь блок — возвращаем то, что собрали."""
    by_isin: Dict[str, dict] = {}
    try:
        flt = json.dumps({"isin": {"$in": isins}}, ensure_ascii=False)
        payload = await _get_json(client, path, {"filter": flt, "limit": max(len(isins) * 4, 100)})
        by_isin = _rows_by_isin(payload)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return {}  # подписка не покрывает метод
        raise
    # fallback: API не поддержал $in -> по одному ISIN
    if not by_isin and isins:
        for isin in isins:
            try:
                p = await _get_json(client, path, {"filter": json.dumps({"isin": isin}), "limit": 5})
                by_isin.update(_rows_by_isin(p))
            except httpx.HTTPStatusError:
                pass
    return by_isin


async def fetch_floater_universe() -> List[dict]:
    """Весь юниверс рублёвых флоатеров (KEYRATE/RUONIA) из НРД valuationnewadd.
    Пагинация по 1000, фильтр coupon_type=float + база CBRATED/RUONIARATED. Кэш на день."""
    if not is_configured():
        return []
    today = date.today().isoformat()
    cache = _load_json(UNIVERSE_FILE)
    if cache.get("calc_date") == today and cache.get("version") == CACHE_VERSION:
        return cache.get("items", [])

    items: List[dict] = []
    try:
        async with httpx.AsyncClient() as client:
            skip = 0
            for _page in range(8):  # до 8000 инструментов
                payload = await _get_json(client, PATH_VALUATION_ADD, {"limit": 1000, "skip": skip})
                rows = payload if isinstance(payload, list) else (payload.get("data") if isinstance(payload, dict) else [])
                if not rows:
                    break
                for row in rows:
                    if row.get("coupon_type") != "float":
                        continue
                    base = _UNI_BASE.get((row.get("base_coupon_index") or "").upper())
                    if not base:
                        continue
                    m = _map_response({}, row)
                    items.append({
                        "isin": row.get("isin"),
                        "name": row.get("issuer") or row.get("name") or row.get("isin"),
                        "base_rate_type": base,
                        "spread_issue_bps": m.get("nominal_margin_bps"),
                        "maturity_date": row.get("maturity"),
                        "nrd_price_pct": m.get("nrd_price_pct"),
                        "discount_margin_bps": m.get("discount_margin_bps"),
                        "z_spread_bps": m.get("z_spread_bps"),
                        "nrd_duration": m.get("duration"),
                        "current_yield_pct": m.get("current_yield_pct"),
                        "rating": rating_bucket(m.get("ratings")),
                        "nrd_calc_date": m.get("nrd_calc_date") or row.get("val_date"),
                        "_val_date": row.get("val_date") or "",
                    })
                if len(rows) < 1000:
                    break
                skip += 1000
    except Exception as e:
        print(f"NRD universe error: {e}")
        return cache.get("items", [])

    # дедуп по ISIN — оставляем строку с максимальной датой оценки
    by_isin: Dict[str, dict] = {}
    for x in items:
        isin = x.get("isin")
        if not isin:
            continue
        prev = by_isin.get(isin)
        if prev is None or str(x.get("_val_date", "")) >= str(prev.get("_val_date", "")):
            by_isin[isin] = x
    items = list(by_isin.values())
    for x in items:
        x.pop("_val_date", None)
    _save_json(UNIVERSE_FILE, {"version": CACHE_VERSION, "calc_date": today, "items": items})
    return items


async def fetch_nrd_metrics(isins: List[str]) -> Dict[str, dict]:
    """{isin: нормализованный блок НРД}. Кэш на диск по дате расчёта.
    Без конфига/при ошибке — пустой dict (грейсфул)."""
    if not isins or not is_configured():
        return {}

    today = date.today().isoformat()
    cache = _load_json(CACHE_FILE)
    fresh = cache.get("calc_date") == today and cache.get("version") == CACHE_VERSION
    cached = cache.get("isins", {}) if fresh else {}

    missing = [i for i in isins if i not in cached]
    result = {i: cached[i] for i in isins if i in cached}

    if missing:
        try:
            async with httpx.AsyncClient() as client:
                raw_new = await _fetch_method(client, PATH_VALUATION, missing)
                raw_add = await _fetch_method(client, PATH_VALUATION_ADD, missing)
            for isin in missing:
                if isin not in raw_new and isin not in raw_add:
                    continue
                mapped = _map_response(raw_new.get(isin, {}), raw_add.get(isin, {}))
                result[isin] = mapped
                cached[isin] = mapped
            cache["version"] = CACHE_VERSION
            cache["calc_date"] = today
            cache["isins"] = cached
            cache["fetched_at"] = time.time()
            _save_json(CACHE_FILE, cache)
        except Exception as e:
            print(f"NRD fetch error: {e}")

    return result
