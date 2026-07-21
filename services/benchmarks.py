"""Бенчмарки фондов: история индексов MOEX (RGBITR, RUCBTRNS, индекс замещающих).

ISS: /iss/history/engines/stock/markets/index/securities/{SECID}.json
(CLOSE по датам, пагинация start=). Кэш память+диск на день — история
меняется раз в день после клиринга.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Dict, List, Optional

import httpx
import logging

logger = logging.getLogger(__name__)

# code -> (SECID MOEX, подпись)
BENCHMARKS = {
    "RGBITR": ("RGBITR", "ОФЗ (RGBITR, total return)"),
    "RUCBTRNS": ("RUCBTRNS", "Корп IG (RUCBTRNS, total return)"),
    "RUCEU": ("RUCEU", "Замещающие (RUCEU)"),
}
_HIST_URL = "https://iss.moex.com/iss/history/engines/stock/markets/index/securities/{secid}.json"
from services.paths import cache_path as _cache_path
_CACHE_FILE = _cache_path("benchmarks_cache.json")

_mem: dict = {"date": None, "data": None}


async def _fetch_one(client: httpx.AsyncClient, secid: str, frm: str) -> List[dict]:
    out: List[dict] = []
    start = 0
    while True:
        resp = await client.get(_HIST_URL.format(secid=secid), params={
            "iss.meta": "off", "iss.only": "history",
            "from": frm, "start": start, "history.columns": "TRADEDATE,CLOSE"}, timeout=10)
        resp.raise_for_status()
        h = resp.json().get("history", {})
        cols, rows = h.get("columns", []), h.get("data", [])
        if not rows:
            break
        di, ci = cols.index("TRADEDATE"), cols.index("CLOSE")
        out.extend({"date": r[di], "close": r[ci]} for r in rows if r[ci] is not None)
        if len(rows) < 100:  # ISS отдаёт страницами по 100
            break
        start += len(rows)
    return out


async def get_benchmarks(days: int = 180) -> dict:
    """{code: {label, items: [{date, close}]}} — кэш на день."""
    today = date.today().isoformat()
    if _mem["data"] is not None and _mem["date"] == today:
        return _mem["data"]

    frm = (date.today() - timedelta(days=days)).isoformat()
    data: Dict[str, dict] = {}
    try:
        async with httpx.AsyncClient() as client:
            for code, (secid, label) in BENCHMARKS.items():
                try:
                    items = await _fetch_one(client, secid, frm)
                except Exception as e:
                    logger.warning(f"Benchmark {secid} error: {e!r}")
                    items = []
                data[code] = {"label": label, "items": items}
    except Exception as e:
        logger.warning(f"Benchmarks fetch error: {e!r}")

    if any(v["items"] for v in data.values()):
        _mem.update({"date": today, "data": data})
        try:
            from services.paths import atomic_write_json as _awj
            _awj(_CACHE_FILE, {"date": today, "data": data})
        except OSError:
            pass
        return data

    # stale с диска
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            disk = json.load(f)
            return disk.get("data") or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return data


def perf_pct(items: List[dict], lookback_days: int) -> Optional[float]:
    """Изменение close за ~lookback_days, %."""
    if len(items) < 2:
        return None
    last = items[-1]
    target = date.fromisoformat(last["date"]).toordinal() - lookback_days
    best = min(items[:-1], key=lambda x: abs(date.fromisoformat(x["date"]).toordinal() - target))
    if best["close"]:
        return round((last["close"] / best["close"] - 1.0) * 100.0, 2)
    return None
