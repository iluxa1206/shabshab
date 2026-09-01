"""Вкладка СИГНАЛЫ: фильтры скринера веб-аккаунта и доставка событий в браузер
(WS-пуш → всплывающее окно, звук, системное уведомление).

Мониторинг СОБЫТИЙНЫЙ, а не по расписанию: тик сравнивает текущий набор
бумаг с прошлым и шлёт только изменения — бумага попала в набор (`new`) либо
сдвинулись её спред (`spread`) или объём по условиям фильтра (`money`).
Молчащий рынок молчит; шевеление видно в тот же тик.

Тик частый (см. SIGNALS_INTERVAL), потому что данные уже в памяти: стаканы
текут push'ом от Alor в market_cache['depth'] (services/universe_stream), а
метрики считает движок universe_stream. Сеть на такте не трогаем.

Условия фильтра, VWAP на объём и прогон по рынку — общие с Telegram-ботом,
живут в services/screener_core.py."""
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock
from services.screener_core import (BLOCK_BASES, RATINGS, FilterError,  # noqa: F401
                                    block_matches, block_meta, evaluate,
                                    evaluate_candidates, market_snapshot,
                                    meta_years, money_floor,
                                    normalize_block_params, normalize_params,
                                    static_candidates, warm_exact_ctx)

logger = logging.getLogger(__name__)

EVENTS_LIMIT = 100          # длина ленты, отдаваемой вкладке
_MAX_FILTERS_PER_USER = 30
_EVENTS_KEEP = 500          # хвост истории на пользователя


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json"))
    d["kind"] = d.get("kind") or "book"
    for k in ("enabled", "sound", "desktop"):
        d[k] = bool(d[k])
    d["tg_target_id"] = d.get("tg_target_id")
    return d


def _normalize(kind: str, params: dict) -> dict:
    return normalize_block_params(params) if kind == "block" else normalize_params(params)


def _check_kind(kind: Optional[str]) -> str:
    kind = (kind or "book").strip()
    if kind not in ("book", "block"):
        raise FilterError("kind: book | block")
    return kind


# --- CRUD (per user_email) ---

def _tg_target(user_email: str, target_id: Optional[int]) -> Optional[int]:
    """Проверяет, что адресат существует и принадлежит этому аккаунту: иначе
    фильтр слал бы в чужой канал по подобранному id."""
    if target_id in (None, "", 0):
        return None
    from services import tg_targets
    t = tg_targets.get(int(target_id))
    if not t or t["user_email"] != (user_email or "").strip().lower():
        raise FilterError("Адресат Telegram не найден")
    return int(target_id)


def create(user_email: str, name: str, params: dict, *, change_pct: float = 10.0,
           sound: bool = True, desktop: bool = True, kind: str = "book",
           tg_target_id: Optional[int] = None) -> dict:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise FilterError("Название: 1–60 символов")
    kind = _check_kind(kind)
    change_pct = float(change_pct if change_pct is not None else 10.0)
    if not (0.1 <= change_pct <= 100):
        raise FilterError("Порог изменения: 0,1–100 %")
    p = _normalize(kind, params)
    with _lock, _connect() as c:
        n = c.execute("SELECT COUNT(*) FROM signal_filters WHERE user_email=?",
                      (user_email,)).fetchone()[0]
        if n >= _MAX_FILTERS_PER_USER:
            raise FilterError(f"Больше {_MAX_FILTERS_PER_USER} фильтров не поддерживается")
        cur = c.execute(
            "INSERT INTO signal_filters(user_email,name,params_json,change_pct,"
            "sound,desktop,kind,tg_target_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (user_email, name, json.dumps(p, ensure_ascii=False), change_pct,
             int(bool(sound)), int(bool(desktop)), kind,
             _tg_target(user_email, tg_target_id), _now()))
        fid = cur.lastrowid
    return get(fid)


def get(fid: int) -> Optional[dict]:
    with _connect() as c:
        r = c.execute("SELECT * FROM signal_filters WHERE id=?", (fid,)).fetchone()
    return _row(r) if r else None


def list_for_user(user_email: str) -> List[dict]:
    with _connect() as c:
        rows = c.execute("SELECT * FROM signal_filters WHERE user_email=? ORDER BY id",
                         (user_email,)).fetchall()
    return [_row(r) for r in rows]


def list_enabled() -> List[dict]:
    """Включённые фильтры СТАКАНА — их гоняет run_cycle. Блочные сюда не
    попадают: они срабатывают не на снимок рынка, а на приход сделки."""
    with _connect() as c:
        rows = c.execute("SELECT * FROM signal_filters WHERE enabled=1 "
                         "AND COALESCE(kind,'book')='book'").fetchall()
    return [_row(r) for r in rows]


def list_enabled_blocks() -> List[dict]:
    """Включённые фильтры крупных сделок — их применяет block_trades."""
    with _connect() as c:
        rows = c.execute("SELECT * FROM signal_filters WHERE enabled=1 "
                         "AND kind='block' ORDER BY id").fetchall()
    return [_row(r) for r in rows]


def block_filter_owners() -> set:
    """Кто вообще завёл блок-фильтр — включённый ИЛИ выключенный.

    Этим отделяется «настроил сам» от «ничего не трогал»: у первых умолчание из
    env не работает вовсе, иначе выключенный фильтр воскрешал бы дефолтный
    звонок и выключить уведомления было бы нечем."""
    with _connect() as c:
        return {r["user_email"] for r in c.execute(
            "SELECT DISTINCT user_email FROM signal_filters WHERE kind='block'").fetchall()}


def update(user_email: str, fid: int, *, name: Optional[str] = None,
           enabled: Optional[bool] = None, params: Optional[dict] = None,
           change_pct: Optional[float] = None, sound: Optional[bool] = None,
           desktop: Optional[bool] = None,
           tg_target_id: Optional[int] = -1) -> Optional[dict]:
    """tg_target_id: -1 — не трогать, None — вернуть доставку в личку,
    число — слать в этот канал (см. services/tg_targets)."""
    f = get(fid)
    if not f or f["user_email"] != user_email:
        return None
    sets, args = [], []
    if tg_target_id != -1:
        sets.append("tg_target_id=?")
        args.append(_tg_target(user_email, tg_target_id))
    if name is not None:
        name = name.strip()
        if not name or len(name) > 60:
            raise FilterError("Название: 1–60 символов")
        sets.append("name=?"); args.append(name)
    if enabled is not None:
        sets.append("enabled=?"); args.append(int(bool(enabled)))
    if sound is not None:
        sets.append("sound=?"); args.append(int(bool(sound)))
    if desktop is not None:
        sets.append("desktop=?"); args.append(int(bool(desktop)))
    if change_pct is not None:
        change_pct = float(change_pct)
        if not (0.1 <= change_pct <= 100):
            raise FilterError("Порог изменения: 0,1–100 %")
        sets.append("change_pct=?"); args.append(change_pct)
    if params is not None:
        sets.append("params_json=?")
        args.append(json.dumps(_normalize(f["kind"], params), ensure_ascii=False))
        # условия сменились — прошлое состояние набора недействительно
        _reset_state(fid)
    if sets:
        with _lock, _connect() as c:
            c.execute(f"UPDATE signal_filters SET {', '.join(sets)} WHERE id=?",
                      (*args, fid))
    return get(fid)


def delete(user_email: str, fid: int) -> bool:
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM signal_filters WHERE id=? AND user_email=?",
                        (fid, user_email))
        if cur.rowcount:
            c.execute("DELETE FROM signal_state WHERE filter_id=?", (fid,))
            c.execute("DELETE FROM signal_events WHERE filter_id=?", (fid,))
        return cur.rowcount > 0


def delete_all(user_email: str, kind: Optional[str] = None) -> int:
    """Сносит фильтры пользователя разом — вместе с их состоянием и их
    событиями в ленте, ровно как поштучный delete().

    kind ограничивает вид (book|block): колонки в UI сносятся раздельно,
    «удалить все» в одной не должно тронуть вторую."""
    kind = _check_kind(kind) if kind else None
    with _lock, _connect() as c:
        q = "SELECT id FROM signal_filters WHERE user_email=?"
        args = [user_email]
        if kind:
            q += " AND COALESCE(kind,'book')=?"
            args.append(kind)
        ids = [r["id"] for r in c.execute(q, args).fetchall()]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        c.execute(f"DELETE FROM signal_state WHERE filter_id IN ({marks})", ids)
        c.execute(f"DELETE FROM signal_events WHERE filter_id IN ({marks})", ids)
        c.execute(f"DELETE FROM signal_filters WHERE id IN ({marks})", ids)
    return len(ids)


def _reset_state(fid: int) -> None:
    with _lock, _connect() as c:
        c.execute("DELETE FROM signal_state WHERE filter_id=?", (fid,))


# --- лента событий ---

def _with_maturity(rows: List[dict]) -> List[dict]:
    """Дописывает погашение и срок до ГОРИЗОНТА ПРАЙСИНГА. Считаем НА ЧТЕНИИ, а
    не пишем в событие: срок тает каждый день, а лента живёт неделями —
    записанное число лет к моменту просмотра уже врёт. Реестр недоступен —
    просто без срока.

    Срок считает общий meta_years: своя арифметика тут мерила до ПОГАШЕНИЯ,
    хотя фильтр, породивший событие, отобрал бумагу по горизонту — в пуше
    стояло «8,2 г» у бумаги, попавшей под окно «до 2 лет»."""
    if not rows:
        return rows
    try:
        from services import instruments_registry as reg
        labels = reg.labels_map(sorted({r["isin"] for r in rows}))
    except Exception:
        return rows
    from services.market_data import MarketDataService
    mx = MarketDataService.universe_metrics() or {}
    today = date.today()
    for r in rows:
        meta = block_meta(labels, r["isin"], mx)
        r["maturity"] = meta.get("maturity")
        yrs = meta_years(meta, today)
        r["years"] = None if yrs is None else max(0.0, yrs)
    return rows


def events_for_user(user_email: str, limit: int = EVENTS_LIMIT) -> List[dict]:
    # Зовётся и мимо роута (бот, api/routes/tg.py), поэтому границы дублируем:
    # отрицательный limit в SQLite снимает лимит целиком.
    limit = max(1, min(int(limit or EVENTS_LIMIT), 500))
    with _connect() as c:
        rows = c.execute(
            "SELECT e.*, f.name AS filter_name, f.kind AS filter_kind, "
            "f.params_json AS filter_params FROM signal_events e "
            "LEFT JOIN signal_filters f ON f.id = e.filter_id "
            "WHERE e.user_email=? ORDER BY e.fired_at DESC, e.id DESC LIMIT ?",
            (user_email, int(limit) * 2)).fetchall()
    # Порядок правим уже на прочитанном: два формата времени в одной колонке
    # (см. event_moment) SQL сортирует как строки, а нам нужен хронологический.
    # Читаем с запасом (×2) — иначе строковый LIMIT мог бы отрезать событие,
    # которое после нормализации времени попадает на страницу.
    rows = sorted(rows, key=lambda r: (event_moment(r["fired_at"]), r["id"]),
                  reverse=True)[:int(limit)]
    out = []
    for r in rows:
        d = dict(r)
        # режим набора объёма показываем в ленте словами; фильтр могли уже
        # удалить — тогда single_px остаётся единственной уликой режима
        params = d.pop("filter_params", None)
        mode = None
        if params:
            try:
                mode = (json.loads(params) or {}).get("money_mode")
            except (TypeError, ValueError):
                mode = None
        d["money_mode"] = mode or ("single" if d.get("single_px") is not None
                                   else None)
        out.append(d)
    return _with_maturity(out)


_MSK = timezone(timedelta(hours=3))


def event_moment(iso: Optional[str]) -> datetime:
    """Время события к одному масштабу — для сортировки ленты.

    В таблице два формата: события стакана всегда писались UTC с зоной
    ('2026-08-20T11:07:09+00:00'), а крупные сделки до 2026-08-20 — строкой
    МСК без зоны ('2026-08-20 14:25:45'). Строковая сортировка мешала их в
    разнобой (пробел < 'T'), и лента прыгала между заявками и сделками.
    Наивную строку читаем как МСК — ровно так её и писали."""
    if not iso:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return (t if t.tzinfo else t.replace(tzinfo=_MSK)).astimezone(timezone.utc)


def day_stats(user_email: str, days: int = 1) -> dict:
    """Сколько раз какая бумага звонила за последние `days` торговых суток МСК.

    Для настройки порогов на первых порах: «звонит ли фильтр по делу или одна
    бумага даёт половину ленты». Причины разложены, потому что 14 «заявок» и
    14 «RS» — разные болезни: первое лечится grace-окном, второе порогом.

    Окно и границу дня считаем по МСК на ПРОЧИТАННОМ (event_moment), а не в
    SQL: в колонке fired_at два формата времени (см. event_moment), и
    строковое сравнение резало бы день не там.

    Лента и так обрезана до _EVENTS_KEEP строк на пользователя, поэтому
    читаем её целиком — отдельного индекса под статистику не заводим."""
    days = max(1, min(int(days or 1), 30))
    now_msk = datetime.now(_MSK)
    since = (now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
             - timedelta(days=days - 1))
    with _connect() as c:
        rows = c.execute(
            "SELECT isin, name, reason, fired_at, filter_id FROM signal_events "
            "WHERE user_email=? ORDER BY id DESC LIMIT ?",
            (user_email, _EVENTS_KEEP)).fetchall()
    names = {f["id"]: f["name"] for f in list_for_user(user_email)}
    by_isin: dict = {}
    by_filter: dict = {}
    total = 0
    for r in rows:
        if event_moment(r["fired_at"]) < since.astimezone(timezone.utc):
            continue
        total += 1
        d = by_isin.setdefault(r["isin"], {"isin": r["isin"], "name": r["name"],
                                           "total": 0, "reasons": {}})
        d["name"] = d["name"] or r["name"]
        d["total"] += 1
        why = r["reason"] or "?"
        d["reasons"][why] = d["reasons"].get(why, 0) + 1
        fname = names.get(r["filter_id"]) or f"#{r['filter_id']}"
        by_filter[fname] = by_filter.get(fname, 0) + 1
    return {"since": since.date().isoformat(), "days": days, "total": total,
            "rows": sorted(by_isin.values(), key=lambda d: -d["total"]),
            "filters": sorted(by_filter.items(), key=lambda kv: -kv[1]),
            # вся ли история попала в окно: лента обрезается по _EVENTS_KEEP, и
            # на шумном дне «за 7 дней» покажет только то, что уцелело
            "capped": len(rows) >= _EVENTS_KEEP}


def unseen_count(user_email: str) -> int:
    with _connect() as c:
        return c.execute("SELECT COUNT(*) FROM signal_events "
                         "WHERE user_email=? AND seen=0", (user_email,)).fetchone()[0]


def mark_seen(user_email: str) -> int:
    with _lock, _connect() as c:
        cur = c.execute("UPDATE signal_events SET seen=1 WHERE user_email=? AND seen=0",
                        (user_email,))
        return cur.rowcount


def clear_events(user_email: str) -> int:
    """Чистит только ленту. Состояние набора не трогаем — иначе все бумаги
    тут же переоткрылись бы как «новые» и лента наполнилась заново."""
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM signal_events WHERE user_email=?", (user_email,))
        return cur.rowcount


def _trim_events(user_email: str) -> None:
    with _lock, _connect() as c:
        c.execute(
            "DELETE FROM signal_events WHERE user_email=? AND id NOT IN "
            "(SELECT id FROM signal_events WHERE user_email=? "
            " ORDER BY fired_at DESC, id DESC LIMIT ?)",
            (user_email, user_email, _EVENTS_KEEP))


# --- детект событий ---

# Пороги повторного сигнала. Смысл каждого — своя единица, а не общий процент:
#   спред   — АБСОЛЮТНЫЕ базисные пункты: 5 бп при уровне 160–380 значимы,
#             а 5% от уровня это 8–19 бп, то есть порог плавал бы вместе с бумагой;
#   объём   — процент (change_pct фильтра) от денег В ГРАНИЦАХ СПРЕДА фильтра
#             (screener_core.money_in_spread), а не от набора VWAP: набор всегда
#             равен запрошенному лимиту и не менялся никогда.
# ЦЕНЫ среди причин НЕТ намеренно: спред уже содержит движение цены, приведённое
# к сроку бумаги, поэтому «цена ушла на полфигуры» дублировала бы спред-событие
# и звонила бы там, где спред не изменился (сдвиг базы, а не оценки выпуска).
SPREAD_REPEAT_BPS = float(os.getenv("SIGNAL_REPEAT_SPREAD_BPS", "5"))
# Бумага, вышедшая из набора и вернувшаяся раньше этого срока, — не «заявка»:
# стакан дрожит вокруг границы фильтра, и каждое возвращение звонило заново.
RETURN_GRACE_MIN = float(os.getenv("SIGNAL_RETURN_GRACE_MIN", "30"))
# Чаще этого по одной бумаге в рамках фильтра не звоним вообще.
COOLDOWN_MIN = float(os.getenv("SIGNAL_COOLDOWN_MIN", "5"))


def _age_min(iso: Optional[str], now: datetime) -> Optional[float]:
    """Сколько минут прошло с отметки. Нет отметки/мусор → None (= «давно»)."""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 60.0


def _moved_abs(prev: Optional[float], cur: Optional[float], delta: float) -> bool:
    """Метрика ушла на delta в СВОИХ единицах (бп для спреда, п.п. для цены)."""
    if cur is None or prev is None:
        return False
    return abs(cur - prev) >= delta


def _moved_pct(prev: Optional[float], cur: Optional[float], pct: float) -> bool:
    """Метрика изменилась на pct% относительно прошлого значения. Появление
    значения там, где его не было (и наоборот) — тоже изменение."""
    if cur is None and prev is None:
        return False
    if cur is None or prev is None:
        return True
    base = abs(prev)
    if base < 1e-9:
        return abs(cur) > 1e-9
    return abs(cur - prev) / base * 100.0 >= pct


def _improved(side: str, best: Optional[float], cur: Optional[float]) -> bool:
    """Побил ли спред ПЛАНКУ — в пользу той стороны, за которой следит фильтр.

    Оффер (мы покупаем): улучшение — спред ВЫШЕ рекорда, то есть за ту же
    бумагу дают больше. Бид (мы продаём) — зеркально, чем ниже спред, тем
    дороже у нас берут. Планка двигается только вместе с отправленным
    событием, поэтому дребезг заявки туда-сюда молчит: пока цену возвращают на
    прежний уровень, рекорд не побит.

    Порог тот же SPREAD_REPEAT_BPS: побить рекорд на полбазисного пункта —
    не новость, а шум округления."""
    if cur is None:
        return False
    if best is None:
        return True
    return (cur - best >= SPREAD_REPEAT_BPS if side == "ask"
            else best - cur >= SPREAD_REPEAT_BPS)


def _new_best(side: str, best: Optional[float], cur: Optional[float]) -> Optional[float]:
    """Планка после события: экстремум в пользу стороны фильтра."""
    if cur is None:
        return best
    if best is None:
        return cur
    return max(best, cur) if side == "ask" else min(best, cur)


def detect_events(fid: int, user_email: str, side: str, change_pct: float,
                  matches: List[dict], want_money: Optional[float],
                  repeat_on_money: bool = True,
                  improve_only: bool = False) -> List[dict]:
    """Сравнивает набор с прошлым состоянием → только события.

    Две причины повтора, у каждой своя единица (см. пороги выше): спред ушёл на
    SPREAD_REPEAT_BPS бп, объём по нашим условиям — на change_pct %.
    repeat_on_money=False выключает вторую: остаётся только спред (и первое
    попадание бумаги в набор — оно не повтор).

    improve_only меняет базу сравнения для спреда: не «ушёл от прошлого
    значения», а «побил ПЛАНКУ» (best_val_bps — лучший спред за жизнь строки
    состояния, см. _improved). Заявку двигают туда-обратно, и каждый шаг
    звонил заново; с планкой возврат на прежнюю цену молчит, а сообщение
    уходит только когда рынок дал больше, чем давал. Повтор по объёму в этом
    режиме выключен: долив в стакан не улучшает цену.
    «Заявка» (бумага пришла в набор) — только если
    её не видели дольше RETURN_GRACE_MIN; иначе это то же самое, что уже
    звонило, и проверяются обычные пороги. Поверх всего кулдаун COOLDOWN_MIN на
    бумагу: событие копится, но звонок не чаще.

    Состояние-базис двигается ТОЛЬКО вместе с отправленным событием — иначе
    медленный дрейф по чуть-чуть никогда не набрал бы порог. Отметка
    last_seen_at, наоборот, обновляется каждый тик присутствия."""
    now_iso = _now()
    now_dt = datetime.now(timezone.utc)
    events = []
    with _lock, _connect() as c:
        known = {r["isin"]: r for r in c.execute(
            "SELECT isin, val_bps, price, money_rub, money_ok_rub, last_seen_at, "
            "last_event_at, last_reason, best_val_bps FROM signal_state "
            "WHERE filter_id=?", (fid,)).fetchall()}

        for m in matches:
            prev = known.get(m["isin"])
            gone_min = _age_min(prev["last_seen_at"], now_dt) if prev else None
            # None в last_seen_at — строка от старой версии схемы: считаем, что
            # бумага в наборе была, и «заявкой» её заново не объявляем
            returned = prev is not None and gone_min is not None and gone_min > RETURN_GRACE_MIN

            if prev is None or returned:
                reason = "new"
            elif improve_only:
                # планка вместо прошлого значения: молчим, пока рекорд цел
                reason = ("spread"
                          if _improved(side, prev["best_val_bps"], m.get("val_bps"))
                          else None)
                since = _age_min(prev["last_event_at"], now_dt)
                if reason and since is not None and since < COOLDOWN_MIN:
                    reason = None
            else:
                # обе сработавшие причины в порядке важности; кулдаун гасит
                # ПОВТОР ТОЙ ЖЕ причины (спред, дрожащий у порога, звонит не
                # чаще раза в COOLDOWN_MIN), но не заслоняет другую — «спред
                # ушёл, а следом сняли весь объём» это две разные новости
                why = []
                if _moved_abs(prev["val_bps"], m.get("val_bps"), SPREAD_REPEAT_BPS):
                    why.append("spread")
                if repeat_on_money and _moved_pct(
                        prev["money_ok_rub"], m.get("money_ok_rub"), change_pct):
                    why.append("money")
                since = _age_min(prev["last_event_at"], now_dt)
                if since is not None and since < COOLDOWN_MIN and prev["last_reason"] in why:
                    why.remove(prev["last_reason"])
                reason = why[0] if why else None

            if reason:
                c.execute(
                    "INSERT INTO signal_events(filter_id,user_email,isin,name,side,"
                    "val_bps,price,money_rub,money_ok_rub,level_money_rub,"
                    "want_money_rub,levels,best,"
                    "single_px,reason,prev_val_bps,prev_price,prev_money_rub,"
                    "prev_money_ok_rub,fired_at,seen) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (fid, user_email, m["isin"], m.get("name"), side,
                     m.get("val_bps"), m.get("price"), m.get("money_rub"),
                     m.get("money_ok_rub"), m.get("level_money_rub"),
                     want_money, m.get("levels"), 1 if m.get("best") else 0,
                     m.get("single_px"), reason,
                     prev["val_bps"] if prev else None,
                     prev["price"] if prev else None,
                     prev["money_rub"] if prev else None,
                     prev["money_ok_rub"] if prev else None, now_iso))
                events.append(dict(
                    m, reason=reason, fired_at=now_iso, want_money_rub=want_money,
                    prev_price=prev["price"] if prev else None,
                    prev_val_bps=prev["val_bps"] if prev else None,
                    prev_money_rub=prev["money_rub"] if prev else None,
                    prev_money_ok_rub=prev["money_ok_rub"] if prev else None))

            if reason or prev is None:
                # ПЛАНКА двигается только с отправленным событием — как и
                # базис сравнения: иначе улучшение, погашенное кулдауном,
                # засчиталось бы рекордом и не позвонило бы никогда.
                # «Заявка» (первое попадание или возврат после долгого
                # отсутствия) планку ОБНУЛЯЕТ: история началась заново.
                best_prev = prev["best_val_bps"] if prev else None
                if reason == "new":
                    best = m.get("val_bps")
                elif reason:
                    best = _new_best(side, best_prev, m.get("val_bps"))
                else:
                    best = best_prev
                c.execute(
                    "INSERT INTO signal_state(filter_id,isin,val_bps,price,money_rub,"
                    "money_ok_rub,last_seen_at,last_event_at,last_reason,best_val_bps,"
                    "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(filter_id,isin) DO UPDATE SET val_bps=excluded.val_bps, "
                    "price=excluded.price, money_rub=excluded.money_rub, "
                    "money_ok_rub=excluded.money_ok_rub, last_seen_at=excluded.last_seen_at, "
                    "last_event_at=excluded.last_event_at, last_reason=excluded.last_reason, "
                    "best_val_bps=excluded.best_val_bps, "
                    "updated_at=excluded.updated_at",
                    (fid, m["isin"], m.get("val_bps"), m.get("price"),
                     m.get("money_rub"), m.get("money_ok_rub"), now_iso,
                     now_iso if reason else None, reason, best, now_iso))
            else:
                # присутствие отмечаем всегда: иначе бумага, спокойно стоящая в
                # наборе, через полчаса «протухла» бы и позвонила как новая
                c.execute("UPDATE signal_state SET last_seen_at=?, updated_at=? "
                          "WHERE filter_id=? AND isin=?", (now_iso, now_iso, fid, m["isin"]))

        # Забываем только то, что давно вне набора: короткий выход — дребезг
        # границы фильтра, а не уход бумаги.
        present = {m["isin"] for m in matches}
        for isin, r in known.items():
            if isin in present:      # эту строку только что обновили этим же тиком
                continue
            gone = _age_min(r["last_seen_at"], now_dt)
            if gone is not None and gone > RETURN_GRACE_MIN:
                c.execute("DELETE FROM signal_state WHERE filter_id=? AND isin=?",
                          (fid, isin))
    return events


def preview_block(params: dict, limit: int = 20) -> dict:
    """Сколько сделок СЕГОДНЯ попало бы под условия блок-фильтра.

    Живого набора у такого фильтра нет — событие мгновенное, поэтому вместо
    «что в наборе сейчас» показываем уже случившееся за день: иначе порог
    ставится вслепую и либо молчит неделю, либо звонит каждые пять минут."""
    from datetime import date

    from services import instruments_registry as reg

    p = normalize_block_params(params)
    day = date.today().isoformat()
    with _connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT trade_id,isin,secid,ts,market,side,value,y_idx_bps FROM block_trade "
            "WHERE ts >= ? AND value >= ? AND (cur IS NULL OR cur='SUR') "
            "ORDER BY value DESC LIMIT 500",
            # выборка по порогу С ЛЮФТОМ (money_floor): сделка на 48 млн под
            # «от 50» проходит block_matches, и предвыборка не должна отрезать
            # её раньше
            (day, money_floor(p["min_value_rub"]))).fetchall()]
    labels = reg.labels_map()
    today = date.today()
    # горизонт прайсинга — из живых метрик: срок ленты считается по той же
    # методике, что окно срока в мониторе (screener_core.block_meta)
    from services.market_data import MarketDataService
    _mx = MarketDataService.universe_metrics() or {}
    metas = {r["isin"]: block_meta(labels, r["isin"], _mx) for r in rows}
    hits = [r for r in rows if block_matches(r, metas[r["isin"]], p, today)]
    return {"ready": True, "total": len(hits), "capped": len(rows) >= 500,
            # срок до погашения — как в превью book-фильтра: «блок на 600 млн»
            # читается по-разному для годовой бумаги и для десятилетней
            "matches": [{"isin": h["isin"],
                         "name": (labels.get(h["isin"]) or {}).get("name") or h["isin"],
                         "maturity": metas[h["isin"]].get("maturity"),
                         "years": meta_years(metas[h["isin"]], today),
                         "money_rub": h["value"], "ts": h["ts"]}
                        for h in hits[:limit]]}


async def preview(user_email: str, params: dict, limit: int = 20) -> dict:
    """Прогон фильтра «прямо сейчас», без записи — форма показывает, что
    попадёт под условия, до сохранения."""
    p = normalize_params(params)
    uni, metrics, depth_map = await market_snapshot()
    if not metrics:
        return {"ready": False, "total": 0, "matches": []}
    # превью считает тем же верифицированным путём, что лента: иначе форма
    # обещала бы один спред, а событие приносило другой
    cands = static_candidates(p, uni, metrics=metrics)
    await warm_exact_ctx([c.get("isin") for c in cands])
    ms = evaluate_candidates(p, cands, metrics, depth_map, exact=True)
    return {"ready": True, "total": len(ms), "matches": ms[:limit]}


# --- цикл мониторинга ---

_candidates_cache: dict = {}      # fid → (params_json, [строки универса])


def _candidates(f: dict, uni: List[dict], metrics: dict) -> List[dict]:
    """Статический отбор кешируется на фильтр: рейтинг/эмитент/срок/суборд не
    меняются от тика к тику, а перебор 577 бумаг каждые пару секунд — пустая
    работа. Кэш сбрасывается сменой условий или обновлением универса."""
    key = (json.dumps(f["params"], sort_keys=True), len(uni))
    hit = _candidates_cache.get(f["id"])
    if hit and hit[0] == key:
        return hit[1]
    cands = static_candidates(f["params"], uni, metrics=metrics)
    _candidates_cache[f["id"]] = (key, cands)
    return cands


async def run_cycle() -> int:
    """Один тик: enabled-фильтры против снапшота рынка. События пишутся в ленту
    и пушатся в браузер. → число фильтров, давших события."""
    from api.routes import ws as wsmod

    filters = list_enabled()
    if not filters:
        return 0
    uni, metrics, depth_map = await market_snapshot()
    if not metrics:
        return 0

    fired = 0
    for f in filters:
        try:
            cands = _candidates(f, uni, metrics)
            # контексты пересчёта для кандидатов фильтра: Y-IDX события считается
            # верифицированным путём (как уровень стакана), а не наклоном от
            # якоря — наклон врал сотнями bps, когда котировка и снимок глубины
            # расходились во времени (см. screener_core.exact_y_idx)
            await warm_exact_ctx([c.get("isin") for c in cands])
            matches = evaluate_candidates(f["params"], cands, metrics, depth_map,
                                          exact=True)
            events = detect_events(f["id"], f["user_email"], f["params"]["side"],
                                   f.get("change_pct") or 10.0, matches,
                                   f["params"].get("min_money_rub"),
                                   repeat_on_money=f["params"].get(
                                       "repeat_on_money", True),
                                   # ключа нет — фильтр заведён до режима
                                   # планки: включаем, как в PARAM_DEFAULTS,
                                   # иначе старые фильтры остались бы шумными
                                   # до первой правки формы
                                   improve_only=f["params"].get(
                                       "improve_only", True) is not False)
            if not events:
                continue
            # дата погашения и срок — во всплывающее окно и в телеграм: «спред
            # 380 бп» читается по-разному для годовой бумаги и для пятилетней
            _with_maturity(events)
            _trim_events(f["user_email"])
            await wsmod.manager.broadcast_signal(f["user_email"], {
                "type": "signal",
                "filter_id": f["id"], "filter_name": f["name"],
                "side": f["params"]["side"],
                "sound": f["sound"], "desktop": f["desktop"],
                "matches": events,
            })
            # Копия в привязанный телеграм-чат. Событиям добавляем СНИМОК
            # СТАКАНА того же такта: пока уведомление доедет до телефона, книга
            # поменяется, а «что там стояло» — первый вопрос после сигнала.
            from services import tg_notify
            from services.screener_core import book_snapshot
            tg_events = []
            for e in events:
                row = metrics.get(e["isin"]) or {}
                # ДИАГНОСТИКА расхождения спреда (2026-08-27): пишем ингредиенты
                # числа рядом с самим числом — иначе разбор задним числом
                # упирается в то, что состояние живого воркера невоспроизводимо.
                try:
                    from services.screener_core import y_idx_diag
                    logger.info("signal diag %s px=%s val=%s ур=%s | %s | ask-лестница %s",
                                e["isin"], e.get("price"), e.get("val_bps"), e.get("levels"),
                                y_idx_diag(e["isin"], e.get("price"), row,
                                           f["params"].get("side") or "ask"),
                                ((depth_map.get(e["isin"]) or {}).get("a") or [])[:3])
                except Exception as _de:
                    logger.warning("signal diag %s: %s", e.get("isin"), _de)
                tg_events.append(dict(e, book=book_snapshot(
                    depth_map.get(e["isin"]), row,
                    row.get("face_px") or 1000.0, row.get("accrued_settle") or 0.0,
                    isin=e["isin"], min_qty=f["params"].get("book_min_qty"))))
            tg_notify.enqueue_signal(f["user_email"], f["id"], f["name"],
                                     f["params"]["side"], tg_events,
                                     target_id=f.get("tg_target_id"))
            fired += 1
        except Exception as e:
            logger.warning("signal filter %s error: %s", f.get("id"), e)
    return fired
