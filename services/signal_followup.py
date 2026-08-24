"""Судьба заявки: что стало с уровнем через FOLLOWUP_MIN после сигнала.

Сигнал отвечает «вот заявка», но не отвечает на главный вопрос — она настоящая
или это подстава, которую снимут через минуту. Через четверть часа смотрим
стакан и ленту сделок и отвечаем РЕПЛАЕМ на исходное сообщение:

    ↩︎ 15 мин: забрали — 18,4м по 99,75 · RS 173 → 165

Только для события «заявка» (первое попадание бумаги под условия): повторы по
спреду и объёму дали бы цепочку ответов на одну бумагу.

Классификация чистая (classify) и потому проверяемая тестами; сеть и база — на
краях модуля.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

FOLLOWUP_MIN = float(os.getenv("SIGNAL_FOLLOWUP_MIN", "15"))
# Заявка «стоит», если на уровне осталось не меньше этой доли: стакан дышит
# мелочью, и точное равенство ловило бы шум вместо изменения.
KEEP_RATIO = 0.8
# Ниже этого от уровня практически ничего не осталось.
GONE_RATIO = 0.2
# Исчезнувший объём считаем ЗАБРАННЫМ, если сделками закрыта хотя бы такая его
# часть: снятие заявки на 25 млн и случайный принт на 100к иначе выглядели бы
# одинаково.
TRADED_RATIO = 0.3
# Цена сделки «по этому уровню» — с допуском в одну сотую пункта: биржа
# исполняет по цене заявки, но соседний тик в пределах спреда тоже наш.
PX_EPS = 0.01
_MSK = timezone(timedelta(hours=3))


def classify(qty_then: Optional[float], qty_now: Optional[float],
             traded_qty: float) -> str:
    """Исход: kept | partial | taken | pulled. Чистая функция — вся арифметика
    порогов здесь, чтобы её можно было проверить без стакана и без сети."""
    if not qty_then or qty_then <= 0:
        return "kept" if qty_now else "pulled"
    left = (qty_now or 0.0) / qty_then
    if left >= KEEP_RATIO:
        return "kept"
    gone = qty_then - (qty_now or 0.0)
    if left > GONE_RATIO:
        return "partial"
    return "taken" if gone > 0 and traded_qty >= TRADED_RATIO * gone else "pulled"


def schedule(chat_id: int, message_id: int, m: dict, side: Optional[str]) -> None:
    """Ставит проверку на FOLLOWUP_MIN вперёд. Никогда не бросает: доставка
    сигнала уже состоялась, и её нельзя ронять из-за учёта."""
    try:
        px = m.get("single_px") if m.get("single_px") is not None else m.get("price")
        if px is None or not m.get("isin"):
            return
        now = datetime.now(timezone.utc)
        with _lock, _connect() as c:
            c.execute(
                "INSERT INTO signal_followup(chat_id,message_id,isin,name,side,"
                "price,qty,money,val_bps,fired_at,due_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (int(chat_id), int(message_id), m["isin"], m.get("name"),
                 side or "ask", float(px), _level_qty(m, px),
                 m.get("level_money_rub"), m.get("val_bps"), now.isoformat(),
                 (now + timedelta(minutes=FOLLOWUP_MIN)).isoformat()))
    except Exception as e:
        logger.warning("followup schedule: %s", e)


def _level_qty(m: dict, px: float) -> Optional[float]:
    """Сколько бумаг стояло на уровне сигнала — из снимка стакана события."""
    book = m.get("book") or {}
    for row in (book.get("asks") or []) + (book.get("bids") or []):
        if row.get("price") is not None and abs(row["price"] - px) < 0.005:
            return row.get("qty")
    return None


def due(limit: int = 20) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM signal_followup WHERE done=0 AND due_at<=? "
            "ORDER BY due_at LIMIT ?", (now, limit)).fetchall()
    return [dict(r) for r in rows]


def close(fid: int, outcome: str, done: int = 1) -> None:
    with _lock, _connect() as c:
        c.execute("UPDATE signal_followup SET done=?, outcome=? WHERE id=?",
                  (done, outcome, fid))


def level_now(isin: str, side: str, px: float) -> tuple:
    """(qty на уровне сейчас, лучшая цена стороны) из живого стакана."""
    from services.market_data import market_cache
    from services.screener_core import _px as _p, _qty as _q
    ladder = ((market_cache.get("depth") or {}).get(isin) or {}).get(
        "a" if side == "ask" else "b") or []
    qty, best = None, None
    for lvl in ladder:
        p, q = _p(lvl), _q(lvl)
        if p is None:
            continue
        if best is None:
            best = p
        if abs(p - px) < 0.005:
            qty = q
    return qty, best


def traded_since(isin: str, side: str, px: float, frm_iso: str) -> tuple:
    """(бумаг, рублей) сделками по цене не хуже уровня с момента сигнала."""
    from services.tape import read_isin_trades
    frm = _msk(frm_iso)
    rows = read_isin_trades(isin, frm=frm, limit=500) or []
    qty = val = 0.0
    for r in rows:
        p = r.get("price")
        if p is None:
            continue
        if side == "ask" and p > px + PX_EPS:
            continue
        if side == "bid" and p < px - PX_EPS:
            continue
        qty += float(r.get("qty") or 0)
        val += float(r.get("value") or 0)
    return qty, val


def _msk(iso: str) -> str:
    """UTC-отметка события → 'YYYY-MM-DD HH:MM:SS' МСК, как хранится архив."""
    try:
        return datetime.fromisoformat(iso).astimezone(_MSK).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S")


# ── текст ответа ──────────────────────────────────────────────────────────

_HEAD = {"taken": "забрали", "pulled": "сняли", "partial": "частично",
         "kept": "стоит"}


def render(row: dict, outcome: str, qty_now: Optional[float], best: Optional[float],
           traded_qty: float, traded_val: float, y_now: Optional[float]) -> str:
    """Одна строка ответа: что стало с уровнем и куда ушёл спред."""
    from services.tg_notify import _num, _short_money, _book_qty

    bits = [f"<b>{_HEAD.get(outcome, outcome)}</b>"]
    if outcome == "taken":
        bits.append(f"{_short_money(traded_val)} по {_num(row['price'])}")
    elif outcome == "pulled":
        bits.append("без сделок" if traded_qty <= 0
                    else f"сделками {_short_money(traded_val)}")
    elif outcome == "partial":
        left = _book_qty({"qty": qty_now}).strip()
        was = _book_qty({"qty": row.get("qty")}).strip()
        bits.append(f"осталось {left} из {was}"
                    + (f", прошло {_short_money(traded_val)}" if traded_val else ""))
    else:                                   # kept
        left = _book_qty({"qty": qty_now}).strip()
        if left:
            bits.append(f"{left} на {_num(row['price'])}")

    # спред «было → стало»: половина ценности ответа в том, куда уехала оценка
    was_y = row.get("val_bps")
    if was_y is not None and y_now is not None and abs(y_now - was_y) >= 1:
        bits.append(f"RS {was_y:.0f} → {y_now:.0f}")
    elif was_y is not None:
        bits.append(f"RS {was_y:.0f}")

    if outcome in ("taken", "pulled") and best is not None:
        side_txt = "оффер" if row.get("side") == "ask" else "бид"
        bits.append(f"{side_txt} теперь {_num(best)}")

    mins = int(FOLLOWUP_MIN)
    return f"↩︎ {mins} мин · " + " · ".join(bits)


# ── воркер ────────────────────────────────────────────────────────────────

async def run_due() -> int:
    """Проверяет созревшие follow-up и отвечает в чат. → сколько отправлено."""
    from services import telegram
    from services.screener_core import exact_y_idx, warm_exact_ctx

    rows = due()
    if not rows:
        return 0
    await warm_exact_ctx([r["isin"] for r in rows])
    sent = 0
    for r in rows:
        try:
            qty_now, best = level_now(r["isin"], r["side"], r["price"])
            if qty_now is None and best is None:
                # стакана по бумаге нет вовсе (стрим не поднялся, рынок закрыт) —
                # молчим: «уровня нет» тут означает отсутствие данных, а не событие
                close(r["id"], "no_book", done=2)
                continue
            traded_qty, traded_val = traded_since(
                r["isin"], r["side"], r["price"], r["fired_at"])
            outcome = classify(r.get("qty"), qty_now, traded_qty)
            y_now = exact_y_idx(r["isin"], r["price"])
            text = render(r, outcome, qty_now, best, traded_qty, traded_val, y_now)
            # «стоит» — без звука: ответ нужен для полноты картины, а телефон
            # дёргать нечем (решение юзера 24.08)
            ok = await telegram.send_message(
                r["chat_id"], text, reply_to=r["message_id"],
                disable_notification=(outcome == "kept"))
            close(r["id"], outcome, done=1 if ok else 2)
            sent += 1 if ok else 0
        except Exception as e:
            logger.warning("followup %s: %s", r.get("isin"), e)
            close(r["id"], "error", done=2)
    return sent
