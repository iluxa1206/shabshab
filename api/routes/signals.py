"""Вкладка СИГНАЛЫ: фильтры скринера веб-аккаунта + лента срабатываний.
Identity — cookie-сессия (require_user). Доставка срабатываний идёт не отсюда,
а WS-каналом 'signals' (см. services/signals.run_cycle)."""
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel

from api.routes.auth import require_user
from services import signals

router = APIRouter()


class SignalParams(BaseModel):
    # отбор бумаг: три селектора по ИЛИ (пусто = весь рынок)
    ratings: List[str] = []
    emitters: List[str] = []
    isins: List[str] = []
    # исключения — бьют включение (см. screener_core.selected). В превью они
    # обязаны быть: форма показывает, что попадёт под условия, и без них
    # обещала бы бумаги, по которым сигнала не будет
    emitters_ex: List[str] = []
    isins_ex: List[str] = []
    issuer: str = "all"                 # all | ofz | corp
    # условия сделки (всегда И)
    side: str = "ask"                   # ask (оффер) | bid
    spread_min: Optional[float] = None  # Y-IDX, бп
    spread_max: Optional[float] = None
    min_money_rub: Optional[float] = None
    money_mode: str = "book"           # book — набор по лестнице | single — одна заявка
    years_min: Optional[float] = None   # срок до погашения, лет
    years_max: Optional[float] = None
    hide_subord: bool = False           # прятать суборды
    repeat_on_money: bool = True        # повтор при изменении объёма
    improve_only: bool = True           # повтор только при улучшении (планка)
    book_min_qty: Optional[float] = None  # прятать мелочь в стакане сообщения


class BlockParams(BaseModel):
    """Фильтр крупной сделки (kind=block): не состояние стакана, а факт сделки
    в ленте — отсюда порог в рублях, режим торгов и база купона."""
    ratings: List[str] = []
    emitters: List[str] = []
    isins: List[str] = []
    emitters_ex: List[str] = []         # исключения — см. SignalParams
    isins_ex: List[str] = []
    issuer: str = "all"                 # all | ofz | corp
    bases: List[str] = []               # KEYRATE/RUONIA/FIXED, пусто = любая
    min_value_rub: Optional[float] = None
    markets: str = "all"                # all | main (безадресные) | ndm (РПС)
    side: str = "any"                   # any | buy | sell — агрессор
    spread_min: Optional[float] = None  # Y-IDX сделки, бп (только флоатеры)
    spread_max: Optional[float] = None
    years_min: Optional[float] = None   # срок до погашения, лет
    years_max: Optional[float] = None
    hide_subord: bool = False


class SignalCreate(BaseModel):
    name: str
    kind: str = "book"                  # book — стакан | block — крупная сделка
    # форма kind-зависимая, поэтому dict: разбор и валидация — в screener_core
    # (normalize_params / normalize_block_params), одни и те же для API и бота
    params: dict = {}
    change_pct: float = 10.0
    sound: bool = True
    desktop: bool = True
    # куда слать в телеграм: id канала из /api/signals/targets (null — в личку)
    tg_target_id: Optional[int] = None


class SignalPatch(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    params: Optional[dict] = None
    change_pct: Optional[float] = None
    sound: Optional[bool] = None
    desktop: Optional[bool] = None
    # -1 «не трогать» отличает «поле не прислали» от «убрать канал» (null):
    # у Pydantic обе ситуации иначе выглядят одинаково
    tg_target_id: Optional[int] = -1


@router.get("", tags=["Signals"])
async def list_filters(user: dict = Depends(require_user)):
    return {"filters": signals.list_for_user(user["email"]),
            "ratings": signals.RATINGS, "bases": signals.BLOCK_BASES}


@router.post("", tags=["Signals"])
async def create_filter(body: SignalCreate, user: dict = Depends(require_user)):
    try:
        return signals.create(user["email"], body.name, body.params,
                              change_pct=body.change_pct, sound=body.sound,
                              desktop=body.desktop, kind=body.kind,
                              tg_target_id=body.tg_target_id)
    except signals.FilterError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/all", tags=["Signals"])
async def delete_all_filters(kind: Optional[str] = None,
                             user: dict = Depends(require_user)):
    """Снести все фильтры пользователя разом (вместе с их событиями в ленте).
    kind=book|block — только свой вид; без него сносится всё."""
    try:
        return {"deleted": signals.delete_all(user["email"], kind=kind)}
    except signals.FilterError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/preview", tags=["Signals"])
async def preview_filter(params: SignalParams, user: dict = Depends(require_user)):
    """Что попадёт под условия прямо сейчас — до сохранения фильтра."""
    try:
        return await signals.preview(user["email"], params.model_dump())
    except signals.FilterError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/preview-block", tags=["Signals"])
async def preview_block_filter(params: BlockParams, user: dict = Depends(require_user)):
    """Сколько сделок СЕГОДНЯ прошло бы под условия — блок-фильтр событийный,
    «набора прямо сейчас» у него не бывает."""
    import asyncio
    try:
        return await asyncio.to_thread(signals.preview_block, params.model_dump())
    except signals.FilterError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/targets", tags=["Signals"])
async def list_targets(user: dict = Depends(require_user)):
    """Каналы доставки аккаунта — для селектора «куда слать» в форме фильтра.
    Регистрируются в боте: переслать пост из канала (см. api/routes/tg)."""
    from services import tg_targets, tg_users
    return {"targets": tg_targets.list_for_user(user["email"]),
            "has_private": tg_users.has_chats(user["email"])}


@router.get("/events", tags=["Signals"])
async def list_events(limit: int = Query(signals.EVENTS_LIMIT, ge=1, le=500),
                      user: dict = Depends(require_user)):
    """Лента срабатываний + счётчик непрочитанных (для колокольчика).

    Границы limit ЖЁСТКИЕ: отрицательное значение SQLite трактует как «без
    лимита» (LIMIT -1), и ?limit=-1 вытягивал всю ленту разом мимо min(...)."""
    return {"events": signals.events_for_user(user["email"], limit=limit),
            "unseen": signals.unseen_count(user["email"])}


@router.post("/events/seen", tags=["Signals"])
async def mark_events_seen(user: dict = Depends(require_user)):
    return {"updated": signals.mark_seen(user["email"])}


@router.delete("/events", tags=["Signals"])
async def clear_events(user: dict = Depends(require_user)):
    return {"deleted": signals.clear_events(user["email"])}


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


# Параметрические маршруты — В КОНЦЕ: FastAPI сопоставляет по порядку, и
# объявленный выше /{fid} перехватывал DELETE /events (422 вместо чистки ленты).
@router.patch("/{fid}", tags=["Signals"])
async def patch_filter(body: SignalPatch, fid: int = Path(...),
                       user: dict = Depends(require_user)):
    try:
        f = signals.update(user["email"], fid, name=body.name, enabled=body.enabled,
                           params=body.params, change_pct=body.change_pct,
                           sound=body.sound, desktop=body.desktop,
                           tg_target_id=body.tg_target_id)
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
