"""Единая лента сделок: тиковый архив Alor + крупные сделки всего рынка (ISS).

Раньше это были две вкладки, потому что источники разные. Пользователю разница
не нужна, а склеить их можно ЧЕСТНО: `trade_tick.trade_id` — это TRADENO MOEX,
тот же ключ, что и `block_trade.trade_id`, поэтому одна сделка в двух архивах
опознаётся как одна строка, а не задваивается.

Что даёт каждый источник (см. [[hourly-bars-archive]] и services/block_trades):
  • trade_tick — ВСЕ безадресные сделки любого размера, но только по юниверсу
    реестра; глубина своя, копится с начала архива;
  • block_trade — сделки от порога записи (1 млн ₽) по ВСЕМУ рынку облигаций,
    включая адресные режимы (РПС, размещения, выкупы), которых в стакане нет.

Отсюда правило склейки: block_trade богаче полями (режим, доходность, валюта)
и шире по рынку, поэтому при совпадении trade_id он побеждает; тик добавляет
мелочь и глубину назад.

Агрегаты считаются по ОБЪЕДИНЕНИЮ (тик исключается, если такая сделка уже есть
в block_trade), а не суммированием двух источников — иначе пересечение
посчиталось бы дважды.
"""
from __future__ import annotations

import logging
from typing import Optional

from services.block_trades import _bind_isins, _TMP
from services.portfolio_db import _connect

logger = logging.getLogger(__name__)

MARKETS = ("bonds", "ndm")


def _cond(frm: Optional[str], till: Optional[str], min_value: float,
          boards: Optional[list[str]], isins: Optional[list[str]],
          side: Optional[str], tmp: bool, alias: str = "",
          max_value: Optional[float] = None) -> tuple[str, list]:
    """Общая часть WHERE — одинаковая для обеих таблиц (колонки совпадают)."""
    p = f"{alias}." if alias else ""
    q, args = "", []
    if frm:
        q += f" AND {p}ts >= ?"
        args.append(frm)
    if till:
        q += f" AND {p}ts <= ?"
        args.append(till + " 23:59:59" if len(till) == 10 else till)
    if min_value:
        q += f" AND {p}value >= ?"
        args.append(min_value)
    if max_value:
        q += f" AND {p}value <= ?"
        args.append(max_value)
    if boards:
        q += f" AND {p}board IN ({','.join('?' * len(boards))})"
        args.extend(boards)
    if side in ("buy", "sell"):
        q += f" AND {p}side = ?"
        args.append(side)
    if tmp:
        q += f" AND {p}isin IN (SELECT isin FROM {_TMP})"
    elif isins:
        q += f" AND {p}isin IN ({','.join('?' * len(isins))})"
        args.extend(isins)
    return q, args


def _union(frm, till, min_value, market, boards, isins, side, tmp,
           max_value=None) -> tuple[str, list]:
    """Подзапрос «объединённая лента»: block_trade + тики, которых там нет.

    Порядок веток важен только для читаемости — дедуп делает NOT EXISTS по
    первичному ключу block_trade, то есть индексным поиском на каждую строку
    тика, а не вложенным сканом.
    """
    b_cond, b_args = _cond(frm, till, min_value, boards, isins, side, tmp,
                           max_value=max_value)
    t_cond, t_args = _cond(frm, till, min_value, boards, isins, side, tmp, alias="t",
                           max_value=max_value)

    blocks = ("SELECT trade_id, isin, ts, price, qty, value, side, board, market, "
              "yld, cur, secid, y_idx_bps, dm_bps FROM block_trade WHERE 1=1" + b_cond)
    # тики знают только безадресные борды: при выборе адресного режима их вклад
    # заведомо пуст, и лишний скан 8+ млн строк не нужен
    if market == "ndm":
        return "(" + blocks + " AND market='ndm')", b_args
    if market == "bonds":
        blocks += " AND market='bonds'"

    # спред у тика есть только от порога записи (BLOCK_YIDX_MIN_RUB): гонять
    # солвер по миллионам мелких принтов смысла нет, у них колонка пустая.
    # Раньше тут стоял безусловный NULL, и свежая крупная сделка ждала спреда
    # ~15 минут — до приезда той же строки из ISS.
    ticks = ("SELECT t.trade_id, t.isin, t.ts, t.price, t.qty, t.value, t.side, "
             "t.board, 'bonds' AS market, NULL AS yld, 'SUR' AS cur, NULL AS secid, "
             "t.y_idx_bps, t.dm_bps "
             "FROM trade_tick t WHERE 1=1" + t_cond +
             " AND NOT EXISTS (SELECT 1 FROM block_trade b WHERE b.trade_id = t.trade_id)")
    return "(" + blocks + " UNION ALL " + ticks + ")", [*b_args, *t_args]


def _spread_clause(y_min: Optional[float], y_max: Optional[float]) -> tuple[str, list]:
    """Фильтр по R-spread — только внешним слоем над объединением: у тиков
    колонки нет (там литерал NULL), и строки без спреда фильтр отсекает
    осознанно — «спред от X» про сделки, у которых спред посчитан."""
    q, args = "", []
    if y_min is not None:
        q += " AND y_idx_bps >= ?"
        args.append(y_min)
    if y_max is not None:
        q += " AND y_idx_bps <= ?"
        args.append(y_max)
    return q, args


def read_tape(frm: Optional[str] = None, till: Optional[str] = None,
              min_value: float = 0, market: Optional[str] = None,
              boards: Optional[list[str]] = None, isins: Optional[list[str]] = None,
              side: Optional[str] = None, limit: int = 500,
              y_min: Optional[float] = None, y_max: Optional[float] = None,
              before_ts: Optional[str] = None,
              before_id: Optional[int] = None,
              max_value: Optional[float] = None) -> list[dict]:
    """Лента сделок, новые сверху.

    before_ts/before_id — КУРСОР пагинации: «строго раньше вот этой сделки».
    Пара, а не одно время: в одну секунду проходят десятки сделок, и по
    `ts <` страница теряла бы хвост секунды, а по `ts <=` зациклилась бы на
    ней. Курсор совпадает с порядком сортировки (ts DESC, trade_id DESC),
    поэтому страницы стыкуются без дублей и дыр даже на живой ленте."""
    cur_q, cur_args = "", []
    if before_ts:
        if before_id is not None:
            cur_q = " AND (ts < ? OR (ts = ? AND trade_id < ?))"
            cur_args = [before_ts, before_ts, before_id]
        else:
            cur_q = " AND ts < ?"
            cur_args = [before_ts]
    with _connect() as c:
        tmp = _bind_isins(c, isins)
        sub, args = _union(frm, till, min_value, market, boards, isins, side, tmp,
                           max_value=max_value)
        yq, yargs = _spread_clause(y_min, y_max)
        rows = c.execute(f"SELECT * FROM {sub} WHERE 1=1{yq}{cur_q} "
                         f"ORDER BY ts DESC, trade_id DESC LIMIT ?",
                         [*args, *yargs, *cur_args, limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["negotiated"] = d.get("market") == "ndm"
        out.append(d)
    return out


def read_isin_trades(isin: str, frm: Optional[str] = None, till: Optional[str] = None,
                     min_value: float = 0, side: Optional[str] = None,
                     limit: int = 500, order: str = "ts",
                     market: Optional[str] = "bonds") -> list[dict]:
    """Сделки ОДНОЙ бумаги из ОБЪЕДИНЁННОГО архива (маркеры крупных принтов на
    графике). Раньше слой читал только trade_tick и не видел сделок, которых там
    нет: тиковый архив по бумаге начинается с первого дрейна, а ISS-лента ловит
    весь рынок (замер 2026-08-14: 15 из 516 крупных сделок у флоатеров юниверса
    и 796 из 3240 по рынку — включая ОФЗ 29010 на 37 и 49 млн ₽).

    market='bonds' по умолчанию: адресные сделки рисует ОТДЕЛЬНЫЙ слой РПС
    (/api/blocks/{isin}), и без этого фильтра одна сделка получила бы два
    маркера. order='value' — лимит режет мелочь, а не дальнюю половину окна.
    Дедуп по TRADENO делает _union, поэтому сделка из обоих архивов — одна
    строка."""
    with _connect() as c:
        tmp = _bind_isins(c, [isin])
        sub, args = _union(frm, till, min_value, market, None, [isin], side, tmp)
        tail = ("ORDER BY value DESC LIMIT ?" if order == "value"
                else "ORDER BY ts DESC, trade_id DESC LIMIT ?")
        rows = c.execute(f"SELECT * FROM {sub} WHERE 1=1 {tail}",
                         [*args, limit]).fetchall()
    out = [dict(r) for r in rows]
    for d in out:
        d["negotiated"] = d.get("market") == "ndm"
    out.sort(key=lambda r: (r.get("ts") or "", r.get("trade_id") or 0))
    return out


def count_isin_trades(isin: str, frm: Optional[str] = None, till: Optional[str] = None,
                      min_value: float = 0, side: Optional[str] = None,
                      market: Optional[str] = "bonds") -> int:
    """Сколько сделок бумаги подходит под фильтр во всём объединении."""
    with _connect() as c:
        tmp = _bind_isins(c, [isin])
        sub, args = _union(frm, till, min_value, market, None, [isin], side, tmp)
        return c.execute(f"SELECT COUNT(*) FROM {sub}", args).fetchone()[0]


def tape_stats(frm: Optional[str] = None, till: Optional[str] = None,
               min_value: float = 0, market: Optional[str] = None,
               boards: Optional[list[str]] = None, isins: Optional[list[str]] = None,
               side: Optional[str] = None, top: int = 10,
               y_min: Optional[float] = None, y_max: Optional[float] = None,
               max_value: Optional[float] = None) -> dict:
    """Итоги окна по ВСЕМ подходящим сделкам, а не по срезанным лимитом.

    Обороты — только по рублёвым выпускам: у валютных VALUE приходит в валюте
    расчётов, и сложение дало бы бессмысленное число."""
    _V = "SUM(CASE WHEN cur IS NULL OR cur='SUR' THEN value ELSE 0 END)"
    with _connect() as c:
        tmp = _bind_isins(c, isins)
        sub, args = _union(frm, till, min_value, market, boards, isins, side, tmp,
                           max_value=max_value)
        yq, yargs = _spread_clause(y_min, y_max)
        if yq:
            sub, args = f"(SELECT * FROM {sub} WHERE 1=1{yq})", [*args, *yargs]
        tot = c.execute(
            f"SELECT COUNT(*) n, {_V} v, "
            f"SUM(CASE WHEN side='buy' THEN value ELSE 0 END) bv, "
            f"SUM(CASE WHEN side='sell' THEN value ELSE 0 END) sv, "
            f"SUM(CASE WHEN market='ndm' THEN 1 ELSE 0 END) nn, "
            f"SUM(CASE WHEN market='ndm' THEN value ELSE 0 END) nv "
            f"FROM {sub}", args).fetchone()
        tops = c.execute(f"SELECT isin, COUNT(*) n, {_V} v FROM {sub} "
                         f"GROUP BY isin ORDER BY v DESC LIMIT ?",
                         [*args, top]).fetchall()
        last = c.execute("SELECT MAX(ts) t FROM ("
                         "SELECT MAX(ts) ts FROM block_trade "
                         "UNION ALL SELECT MAX(ts) FROM trade_tick)").fetchone()
    n = tot["n"] or 0
    ndm_n = tot["nn"] or 0
    return {"n": n, "value": tot["v"] or 0,
            "buy_value": tot["bv"] or 0, "sell_value": tot["sv"] or 0,
            "by_market": {"bonds": {"n": n - ndm_n,
                                    "value": (tot["v"] or 0) - (tot["nv"] or 0)},
                          "ndm": {"n": ndm_n, "value": tot["nv"] or 0}},
            "top": [{"isin": r["isin"], "n": r["n"], "value": r["v"] or 0} for r in tops],
            "archive_till": last["t"] if last else None}
