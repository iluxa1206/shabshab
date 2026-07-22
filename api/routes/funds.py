"""Модуль «Фонды»: CRUD фондов, загрузка снапшота позиций (CSV), РЕПО-сделки, сводка.

Подключается в api/main.py под require_user (как остальные данные).
CSV снапшота: строки `ISIN;кол-во` — парсинг толерантный (как portfolio.js),
делаем на бэке, чтобы форматы совпадали между фронтом и curl-загрузкой.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Path
from pydantic import BaseModel, Field

from services import portfolio, portfolio_db as db

router = APIRouter()

_ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b")


def parse_snapshot_csv(text: str) -> tuple[list[tuple[str, float]], list[str]]:
    """[(isin, qty)], errors. Разделители ,;таб, десятичная запятая, пробелы-тысячи."""
    rows: dict[str, float] = {}
    order: list[str] = []
    errors: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _ISIN_RE.search(line.upper())
        if not m:
            continue  # заголовок/мусор
        isin = m.group(1)
        rest = line[:m.start()] + " " + line[m.end():]
        nm = re.search(r"[\d][\d\s.,]*", rest)
        qty = None
        if nm:
            cleaned = re.sub(r"[^\d.]", "", nm.group(0).replace(" ", "").replace(",", "."))
            try:
                qty = float(cleaned)
            except ValueError:
                qty = None
        if not qty or qty <= 0:
            errors.append(f"{isin}: не найдено количество")
            continue
        if isin in rows:
            rows[isin] += qty
        else:
            rows[isin] = qty
            order.append(isin)
    return [(i, rows[i]) for i in order], errors


class FundIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)
    name: str
    base_ccy: str = "RUB"


class FundPatch(BaseModel):
    name: Optional[str] = None
    base_ccy: Optional[str] = None
    capital: Optional[float] = None
    funding_spread_bps: Optional[float] = None


class SnapshotIn(BaseModel):
    # лимит тела (SEC #5): парсер грузит весь текст в память и гоняет regex
    # построчно — без потолка авторизованный юзер может послать гигантский body
    # (память/CPU DoS). 1 МБ с запасом покрывает любой реальный портфель.
    csv: str = Field(..., max_length=1_048_576)
    snap_date: Optional[str] = None  # ISO; по умолчанию сегодня


class PositionIn(BaseModel):
    isin: str
    qty: float


class RepoIn(BaseModel):
    amount: float = Field(..., gt=0)
    rate_pct: float
    ccy: str = "RUB"
    open_date: Optional[str] = None
    term_days: Optional[int] = None
    note: Optional[str] = None


def _fund_or_404(code: str) -> dict:
    f = db.get_fund(code.upper())
    if f is None:
        raise HTTPException(status_code=404, detail=f"Фонд {code} не найден")
    return f


@router.get("", tags=["Funds"])
async def get_funds():
    """Сводка всех фондов (без позиций) для главного экрана."""
    return {"funds": await portfolio.funds_overview()}


@router.post("", tags=["Funds"], status_code=201)
async def create_fund(body: FundIn):
    if db.get_fund(body.code.upper()):
        raise HTTPException(status_code=409, detail=f"Фонд {body.code} уже существует")
    return db.create_fund(body.code, body.name, body.base_ccy)


@router.patch("/{code}", tags=["Funds"])
async def patch_fund(code: str, body: FundPatch):
    _fund_or_404(code)
    return db.update_fund(code.upper(), **body.model_dump(exclude_none=True))


@router.delete("/{code}", tags=["Funds"])
async def remove_fund(code: str):
    _fund_or_404(code)
    db.delete_fund(code.upper())
    return {"ok": True}


@router.get("/{code}/summary", tags=["Funds"])
async def get_summary(code: str = Path(...)):
    _fund_or_404(code)
    return await portfolio.fund_summary(code.upper())


@router.get("/{code}/positions", tags=["Funds"])
async def get_positions(code: str):
    _fund_or_404(code)
    return {"positions": db.get_positions(code.upper())}


@router.get("/{code}/cashflow", tags=["Funds"])
async def get_cashflow(code: str, months: int = 12):
    """Агрегированный денежный поток фонда по месяцам (купоны + принципал)."""
    _fund_or_404(code)
    return await portfolio.fund_cashflow(code.upper(), months=max(1, min(months, 36)))


@router.get("/{code}/nav_history", tags=["Funds"])
async def get_nav_history(code: str, days: int = 120):
    _fund_or_404(code)
    return {"items": db.get_nav_history(code.upper(), days=max(1, min(days, 730)))}


@router.get("/{code}/scenarios", tags=["Funds"])
async def get_scenarios(code: str):
    """Сценарный анализ: КС ±100/±200, наклон кривой, FX ±10% → ΔNAV и Δcarry."""
    _fund_or_404(code)
    return await portfolio.fund_scenarios(code.upper())


@router.get("/{code}/alerts", tags=["Funds"])
async def get_alerts(code: str, threshold: float = 20.0):
    """Repricing-алерты D/D (Δz/Δdm из истории НРД) по бумагам фонда."""
    _fund_or_404(code)
    return await portfolio.fund_alerts(code.upper(), threshold_bps=threshold)


# префикс _meta, чтобы не пересекаться с /{code}-путями
@router.get("/_meta/calendar", tags=["Funds"])
async def get_calendar(days: int = 90):
    """События по всем фондам: купоны, амортизации, погашения, оферты."""
    return await portfolio.funds_calendar(days=max(1, min(days, 365)))


@router.get("/_meta/benchmarks", tags=["Funds"])
async def get_benchmarks_route(days: int = 180):
    from services.benchmarks import get_benchmarks, perf_pct
    data = await get_benchmarks(days=max(30, min(days, 730)))
    out = {}
    for code, v in data.items():
        items = v["items"]
        out[code] = {
            "label": v["label"],
            "last": items[-1] if items else None,
            "perf_1d": perf_pct(items, 1), "perf_30d": perf_pct(items, 30),
            "perf_ytd": perf_pct(items, (date.today() - date(date.today().year, 1, 1)).days),
            "items": items,
        }
    return out


@router.put("/{code}/snapshot", tags=["Funds"])
async def put_snapshot(code: str, body: SnapshotIn):
    """Заменяет снапшот позиций целиком из CSV `ISIN;кол-во`."""
    _fund_or_404(code)
    rows, errors = parse_snapshot_csv(body.csv)
    if not rows:
        raise HTTPException(status_code=422, detail="В CSV не найдено ни одной позиции")
    snap = body.snap_date or date.today().isoformat()
    n = db.replace_snapshot(code.upper(), rows, snap)
    return {"ok": True, "positions": n, "errors": errors, "snap_date": snap}


@router.put("/{code}/position", tags=["Funds"])
async def put_position(code: str, body: PositionIn):
    """Точечное редактирование: qty<=0 удаляет позицию."""
    _fund_or_404(code)
    isin = body.isin.strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=422, detail=f"Невалидный ISIN: {body.isin}")
    db.upsert_position(code.upper(), isin, body.qty, date.today().isoformat())
    return {"ok": True}


@router.get("/{code}/repo", tags=["Funds"])
async def get_repos(code: str):
    _fund_or_404(code)
    return {"repos": db.list_repos(code.upper())}


@router.post("/{code}/repo", tags=["Funds"], status_code=201)
async def post_repo(code: str, body: RepoIn):
    _fund_or_404(code)
    return db.add_repo(code.upper(), body.amount, body.rate_pct, body.ccy,
                       body.open_date, body.term_days, body.note)


@router.delete("/{code}/repo/{repo_id}", tags=["Funds"])
async def remove_repo(code: str, repo_id: int):
    _fund_or_404(code)
    if not db.delete_repo(code.upper(), repo_id):
        raise HTTPException(status_code=404, detail="РЕПО-сделка не найдена")
    return {"ok": True}
