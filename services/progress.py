"""Реестр фоновых задач: что грузится прямо сейчас и сколько уже сделано.

Страница СТАТУС показывает полноту кэшей (X из Y бумаг), но по ней не видно,
идёт ли прямо сейчас налив и когда он кончится. Здесь — живой прогресс: обход
юниверса баров, прогрев кэшей, дрейн рейтингов, разовые бэкфилл-скрипты.

Состояние лежит в SQLite, а не в памяти процесса, по двум причинам: бэкфиллы
запускаются отдельным процессом (`docker compose exec`) и иначе были бы не
видны, и прогресс переживает рестарт контейнера — видно, что задача оборвалась,
а не молча исчезла.

Запись throttled: шаги идут по бумаге (тысячи за проход), а в базу уходит не
чаще раза в секунду — кроме первого и последнего шага, они пишутся всегда.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))
_WRITE_EVERY_SEC = 1.0
# сколько держать завершённые задачи в выдаче — чтобы «только что закончилось»
# было видно, но список не превращался в журнал
KEEP_FINISHED_MIN = 30

_last_write: dict[str, float] = {}


def _now() -> str:
    return datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S")


def _exec(sql: str, args: tuple = ()) -> bool:
    """Прогресс — вспомогательная телеметрия: его сбой не должен ронять задачу.
    Возвращает, удалась ли запись: при 'database is locked' счётчик нельзя
    считать записанным, иначе шаги теряются насовсем (обход баров показывал
    567 из 1186 при том, что прошёл весь список)."""
    try:
        with _lock, _connect() as c:
            c.execute(sql, args)
        return True
    except Exception as e:
        logger.debug("progress %s: %s", type(e).__name__, e)
        return False


def start(key: str, label: str, total: Optional[int] = None,
          detail: Optional[str] = None) -> None:
    """Начать (или перезапустить) задачу. Повторный старт обнуляет счётчик."""
    _last_write[key] = 0.0
    _exec(
        "INSERT INTO job_progress(key,label,done,total,state,detail,started_at,updated_at,"
        "finished_at,pid) VALUES(?,?,0,?,'running',?,?,?,NULL,?) "
        "ON CONFLICT(key) DO UPDATE SET label=excluded.label, done=0, total=excluded.total, "
        "state='running', detail=excluded.detail, started_at=excluded.started_at, "
        "updated_at=excluded.updated_at, finished_at=NULL, pid=excluded.pid",
        (key, label, total, detail, _now(), _now(), os.getpid()))


def advance(key: str, n: int = 1, detail: Optional[str] = None,
            force: bool = False) -> None:
    """Сдвинуть счётчик на n. Пишет не чаще раза в секунду (force — всегда)."""
    now = time.monotonic()
    if not force and now - _last_write.get(key, 0.0) < _WRITE_EVERY_SEC:
        # шаг всё равно должен попасть в базу, иначе счётчик отстанет навсегда:
        # копим его прямо в SQL-выражении при следующей записи
        _pending[key] = _pending.get(key, 0) + n
        return
    _last_write[key] = now
    inc = _pending.pop(key, 0) + n
    ok = (_exec("UPDATE job_progress SET done=done+?, updated_at=? WHERE key=?",
                (inc, _now(), key)) if detail is None else
          _exec("UPDATE job_progress SET done=done+?, detail=?, updated_at=? WHERE key=?",
                (inc, detail, _now(), key)))
    if not ok:
        _pending[key] = _pending.get(key, 0) + inc   # запишем со следующим шагом


_pending: dict[str, int] = {}


def set_done(key: str, done: int, detail: Optional[str] = None,
             force: bool = False) -> None:
    """Выставить счётчик абсолютным значением — когда шаг цикла может закончиться
    по-разному (continue/исключение) и инкремент в одном месте не поставить."""
    now = time.monotonic()
    if not force and now - _last_write.get(key, 0.0) < _WRITE_EVERY_SEC:
        return
    _last_write[key] = now
    _exec("UPDATE job_progress SET done=?, detail=COALESCE(?,detail), updated_at=? WHERE key=?",
          (done, detail, _now(), key))


def set_total(key: str, total: int) -> None:
    _exec("UPDATE job_progress SET total=?, updated_at=? WHERE key=?", (total, _now(), key))


def finish(key: str, detail: Optional[str] = None, error: Optional[str] = None) -> None:
    """Закрыть задачу: state=done либо failed (при error).

    Успешно завершённая задача доводится до total: несколько шагов могло
    потеряться на блокировках базы, а «done» с недобором читается как «оборвалось
    на середине».
    """
    inc = _pending.pop(key, 0)
    _last_write.pop(key, None)
    state = "failed" if error else "done"
    for attempt in range(3):
        ok = _exec(
            "UPDATE job_progress SET done=CASE WHEN ?='done' AND total IS NOT NULL "
            "THEN total ELSE done+? END, state=?, detail=COALESCE(?,detail), "
            "updated_at=?, finished_at=? WHERE key=?",
            (state, inc, state, error or detail, _now(), _now(), key))
        if ok:
            return
        time.sleep(0.2 * (attempt + 1))   # финал важнее шага: не теряем состояние


def snapshot() -> list[dict]:
    """Идущие задачи + недавно завершённые. Считает pct, скорость и остаток.

    Задача, которую никто не двигал больше 10 минут, помечается stale: процесс
    умер (рестарт контейнера, убитый скрипт), и висящий «running» врал бы.
    """
    cutoff = (datetime.now(_MSK) - timedelta(minutes=KEEP_FINISHED_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _connect() as c:
            rows = c.execute(
                "SELECT * FROM job_progress WHERE state='running' OR finished_at >= ? "
                "ORDER BY state='running' DESC, started_at DESC", (cutoff,)).fetchall()
    except Exception:
        return []

    out = []
    now = datetime.now(_MSK)
    for r in rows:
        d = dict(r)
        done, total = d.get("done") or 0, d.get("total") or 0
        state = d.get("state")
        started = _parse(d.get("started_at"))
        updated = _parse(d.get("updated_at"))
        if state == "running" and updated and (now - updated).total_seconds() > 600:
            state = "stale"
        elapsed = (now - started).total_seconds() if started else 0
        rate = done / elapsed if elapsed > 0 and done else 0     # шагов в секунду
        out.append({
            "key": d["key"], "label": d["label"], "done": done, "total": total or None,
            "pct": round(100 * done / total) if total else None,
            "state": state, "detail": d.get("detail"),
            "started_at": d.get("started_at"), "updated_at": d.get("updated_at"),
            "elapsed_sec": round(elapsed),
            "eta_sec": round((total - done) / rate) if state == "running" and rate and total and total > done else None,
        })
    return out


def _parse(s: Optional[str]):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_MSK) if s else None
    except (ValueError, TypeError):
        return None
