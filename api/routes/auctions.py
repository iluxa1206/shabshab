"""Аукционы ОФЗ Минфина: план/факт квартала, история, ряды, статистика,
сводка по выпускам (services/ofz_auctions). Данных с биржи здесь нет — всё из
xlsx и HTML самого Минфина; сеть только в POST /sync и ночном такте."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.routes.auth import require_admin

router = APIRouter()


def _default_from() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=365)).isoformat()


def _q(quarter: Optional[str]) -> Optional[str]:
    if quarter is None:
        return None
    q = quarter.strip().upper()
    if len(q) != 6 or q[4] != "Q" or not q[:4].isdigit() or q[5] not in "1234":
        raise HTTPException(status_code=422, detail="quarter: ожидается вид 2026Q3")
    return q


@router.get("/quarters", tags=["Auctions"])
async def get_quarters():
    """Кварталы, по которым есть план и/или факт (селектор витрины)."""
    from services import ofz_auctions as oa
    from services.pools import run_bg
    return {"rows": await run_bg(oa.quarters)}


@router.get("/plan", tags=["Auctions"])
async def get_plan(quarter: Optional[str] = Query(None, description="2026Q3; пусто — текущий")):
    """План/факт квартала: план по корзинам срока (ст. 113 БК), факт ПО НОМИНАЛУ
    и ПО 113-й, темп, «надо на аукцион», раскладка по датам графика.

    Прогресс считается по 113-й: план Минфина — деньги без НКД и без премии
    сверх номинала, и у дисконтных длинных ОФЗ он на треть меньше номинала."""
    from services import ofz_auctions as oa
    from services.pools import run_bg
    return await run_bg(oa.plan_fact, _q(quarter))


@router.get("/results", tags=["Auctions"])
async def get_results(
    date_from: Optional[str] = Query(None, alias="from", description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, alias="to", description="YYYY-MM-DD"),
    sec_type: Optional[str] = Query(None, alias="type", description="ОФЗ-ПД | ОФЗ-ПК | ОФЗ-ИН"),
    fmt: Optional[str] = Query(None, description="auction | drpa"),
    secid: Optional[str] = Query(None, description="SECID или код выпуска без контрольной цифры"),
    status: Optional[str] = Query(None, description="ok | failed | drpa"),
):
    """История аукционов: строки Минфина + срок, bid/cover, 113-я, премия к
    вторичке (wap-доходность минус вечерний YTM накануне из spread_daily; нет
    точки — премии нет, а не ноль). Дефолт периода — год назад."""
    from services import ofz_auctions as oa
    from services.pools import run_bg
    if not date_from and not secid:
        date_from = _default_from()
    rows = await run_bg(oa.results, date_from, date_to, sec_type, fmt, secid, status)
    return {"rows": rows, "from": date_from, "to": date_to,
            "sync": await run_bg(oa.sync_state)}


@router.get("/series", tags=["Auctions"])
async def get_series(
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
):
    """По датам аукционов: размещено, спрос, 113-я, число/несостоявшиеся,
    взвешенная доходность размещения (ПД) и кумулятив 113-й внутри квартала."""
    from services import ofz_auctions as oa
    from services.pools import run_bg
    return {"rows": await run_bg(oa.series, date_from or _default_from(), date_to)}


@router.get("/stats", tags=["Auctions"])
async def get_stats(
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
):
    """Итоги периода: объёмы, медиана bid/cover, доли несостоявшихся и ДРПА,
    разрез по типам и корзинам срока, топ-5, средняя премия к вторичке."""
    from services import ofz_auctions as oa
    from services.pools import run_bg
    return await run_bg(oa.stats, date_from or _default_from(), date_to)


@router.get("/issues", tags=["Auctions"])
async def get_issues():
    """Сводка по выпускам за всю историю: аукционов, размещено, последний
    выход, средняя доходность, диапазон цен."""
    from services import ofz_auctions as oa
    from services.pools import run_bg
    return {"rows": await run_bg(oa.by_issue)}


class SyncBody(BaseModel):
    results: bool = True
    plans: bool = True
    years: list[int] = []
    force: bool = False


@router.post("/sync", tags=["Auctions"])
async def sync_auctions(body: SyncBody = SyncBody(), _admin: dict = Depends(require_admin)):
    """Ручной синк с Минфина (админ): итоги за годы (пусто — текущий и прошлый)
    и/или планы кварталов. Идемпотентно: файл, который уже скачан под этим
    именем, повторно не качается (force — перечитать)."""
    from services import ofz_auctions as oa
    out: dict = {}
    if body.results:
        out["results"] = await oa.sync_results(years=body.years or None, force=body.force)
    if body.plans:
        out["plans"] = await oa.sync_plans(force=body.force)
    return out
