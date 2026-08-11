"""Вкладка СИГНАЛЫ: фильтры скринера веб-аккаунта + лента срабатываний.
Identity — cookie-сессия (require_user). Доставка срабатываний идёт не отсюда,
а WS-каналом 'signals' (см. services/signals.run_cycle)."""
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel

from api.routes.auth import require_user
from services import signals

router = APIRouter()


class SignalParams(BaseModel):
    # отбор бумаг: три селектора по ИЛИ (пусто = весь рынок)
    ratings: List[str] = []
    emitters: List[str] = []
    isins: List[str] = []
    # условия сделки (всегда И)
    side: str = "ask"                   # ask (оффер) | bid
    spread_min: Optional[float] = None  # Y-IDX, бп
    spread_max: Optional[float] = None
    min_money_rub: Optional[float] = None


class SignalCreate(BaseModel):
    name: str
    params: SignalParams
    cooldown_min: int = 60
    sound: bool = True
    desktop: bool = True


class SignalPatch(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    params: Optional[SignalParams] = None
    cooldown_min: Optional[int] = None
    sound: Optional[bool] = None
    desktop: Optional[bool] = None


@router.get("", tags=["Signals"])
async def list_filters(user: dict = Depends(require_user)):
    return {"filters": signals.list_for_user(user["email"]),
            "ratings": signals.RATINGS}


@router.post("", tags=["Signals"])
async def create_filter(body: SignalCreate, user: dict = Depends(require_user)):
    try:
        return signals.create(user["email"], body.name, body.params.model_dump(),
                              cooldown_min=body.cooldown_min, sound=body.sound,
                              desktop=body.desktop)
    except signals.FilterError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{fid}", tags=["Signals"])
async def patch_filter(body: SignalPatch, fid: int = Path(...),
                       user: dict = Depends(require_user)):
    try:
        f = signals.update(user["email"], fid, name=body.name, enabled=body.enabled,
                           params=body.params.model_dump() if body.params else None,
                           cooldown_min=body.cooldown_min, sound=body.sound,
                           desktop=body.desktop)
    except signals.FilterError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if f is None:
        raise HTTPException(status_code=404, detail="Фильтр не найден")
    return f


@router.delete("/{fid}", tags=["Signals"])
async def delete_filter(fid: int = Path(...), user: dict = Depends(require_user)):
    if not signals.delete(user["email"], fid):
        raise HTTPException(status_code=404, detail="Фильтр не найден")
    return {"ok": True}


@router.post("/preview", tags=["Signals"])
async def preview_filter(params: SignalParams, user: dict = Depends(require_user)):
    """Что попадёт под условия прямо сейчас — до сохранения фильтра."""
    try:
        return await signals.preview(user["email"], params.model_dump())
    except signals.FilterError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/hits", tags=["Signals"])
async def list_hits(limit: int = signals.HITS_LIMIT, user: dict = Depends(require_user)):
    return {"hits": signals.hits_for_user(user["email"], limit=min(int(limit), 500))}


@router.post("/hits/seen", tags=["Signals"])
async def mark_hits_seen(user: dict = Depends(require_user)):
    return {"updated": signals.mark_seen(user["email"])}


@router.delete("/hits", tags=["Signals"])
async def clear_hits(user: dict = Depends(require_user)):
    return {"deleted": signals.clear_hits(user["email"])}


@router.get("/search", tags=["Signals"])
async def search_bonds(q: str = "", user: dict = Depends(require_user)):
    """Поиск бумаги по подстроке ISIN/имени/эмитента — пикер «отдельные бумаги»."""
    from services import instruments_registry
    return {"results": instruments_registry.search(q, limit=10)}


@router.get("/emitters", tags=["Signals"])
async def list_emitters(q: str = "", user: dict = Depends(require_user)):
    """Эмитенты универса для пикера фильтра."""
    from services import instruments_registry
    rows = instruments_registry.universe_rows()
    q = (q or "").strip().lower()
    names = sorted({(r.get("emitter_name") or "").strip()
                    for r in rows if r.get("emitter_name")})
    if q:
        names = [n for n in names if q in n.lower()]
    return {"emitters": names[:30], "total": len(names)}
