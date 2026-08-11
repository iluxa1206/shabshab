"""Точная история спредов: дневные снапшоты spread-метрик из тёплых кэшей
поллера (universe_metrics флоатеры + fixed_metrics фиксы). В отличие от
candle-оценки (историч. цена × текущая модель) — точные значения на дату, с
реальной кривой/НКД/сроком того дня. Копится вперёд, идемпотентно per (isin,date)."""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

_MSK_OFFSET = 3  # часы


def _msk_date() -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=_MSK_OFFSET)).date().isoformat()


def write_snapshot() -> int:
    """Снимок спред-метрик всего юниверса на сегодня (МСК) из market_cache.
    Возвращает число записанных строк. Идемпотентно (INSERT OR REPLACE)."""
    from services.market_data import market_cache, MarketDataService
    d = _msk_date()
    rows = []

    um = MarketDataService.universe_metrics() or {}
    for isin, m in um.items():
        if not isin or not isinstance(m, dict):
            continue
        rows.append((isin, d, "floater", m.get("last"), m.get("disc_dm"),
                     None, m.get("z_model"), m.get("ytm"), m.get("yoi"), "snap"))

    fxm = market_cache.get("fixed_metrics") or {}
    for isin, m in fxm.items():
        if not isin or not isinstance(m, dict):
            continue
        rows.append((isin, d, "fixed", m.get("last"), None,
                     m.get("g_spread_bps"), m.get("z_spread_bps"), m.get("ytm"), None, "snap"))

    # пишем только строки с хоть каким-то спредом (иначе шум пустых)
    rows = [r for r in rows if r[4] is not None or r[5] is not None
            or r[6] is not None or r[8] is not None]
    if not rows:
        return 0
    with _lock, _connect() as c:
        c.executemany(
            "INSERT OR REPLACE INTO spread_daily(isin,date,kind,price_pct,dm_bps,"
            "g_spread_bps,z_bps,ytm,y_idx,src) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    logger.info("spread snapshot %s: %d строк", d, len(rows))
    return len(rows)


def read_history(isin: str, days: int = 400) -> List[dict]:
    """Точная история по бумаге, по возрастанию даты."""
    with _connect() as c:
        r = c.execute(
            "SELECT date, kind, price_pct, dm_bps, g_spread_bps, z_bps, ytm, y_idx, src, engine_ver "
            "FROM spread_daily WHERE isin=? ORDER BY date DESC LIMIT ?",
            (isin, days)).fetchall()
    return [dict(x) for x in reversed(r)]


def keep_trade_days(rows: List[dict], trade_days) -> tuple:
    """Оставить только строки истории за дни, когда бумага РЕАЛЬНО торговалась.

    spread_daily наполняется вечерним снапшотом и honest-бэкфиллом по календарю,
    поэтому там заводятся даты, которых нет в свечах MOEX: сделок не было, цена
    берётся стейл prev-close, а точка спреда на графике всё равно рисуется. На
    графике цены такого дня нет — линии разъезжаются по датам, и пользователь
    видит спред, посчитанный от цены, которой в этот день никто не показывал.

    trade_days — множество ISO-дат со свечами. Возвращает (оставшиеся, сколько
    отброшено)."""
    kept = [r for r in rows if str(r.get("date", "")) in trade_days]
    return kept, len(rows) - len(kept)


def drop_stale_honest(isin: str, engine_ver: int) -> int:
    """Сносит honest-строки, посчитанные СТАРЫМ as-of движком (engine_ver < текущей
    либо NULL). Точки, персистённые до фикса НКД/SECID, иначе живут вечно: бэкфилл
    их не трогает (он дописывает только отсутствующие даты), и график показывал бы
    старые цифры без единого признака. Вечерние снапшоты (src='snap') не трогаем —
    это независимый источник, считанный в свой день на живых данных."""
    with _lock, _connect() as c:
        cur = c.execute(
            "DELETE FROM spread_daily WHERE isin=? AND src='honest' "
            "AND (engine_ver IS NULL OR engine_ver < ?)", (isin, engine_ver))
        return cur.rowcount or 0


def upsert_honest(isin: str, points: list, existing_dates: set,
                  engine_ver: int = 0, retrust_dates: Optional[set] = None) -> int:
    """Персистит честный бэкфилл: INSERT точек для дат без строки (src='honest');
    для существующих строк (вечерние снапшоты) НИЧЕГО не перезаписывает, кроме
    y_idx, если он NULL (легаси-строки до появления колонки — пересчитаны на их
    же цене, см. backdate.ensure_honest_backfill). Прошлое не меняется —
    идемпотентно. engine_ver штампуется на вставляемые строки, чтобы следующий
    фикс движка мог их инвалидировать (drop_stale_honest).

    retrust_dates — даты НЕДОВЕРЕННЫХ строк (легаси candle-est бэкфилл без src:
    y_idx/dm там — историческая цена × модель НА ДЕНЬ ПРОГОНА скрипта, у бумаг
    близко к погашению это сотни bps мусора). Такие строки перезаписываются
    честным расчётом на их же цене целиком (y_idx/dm/ytm) и перештамповываются
    src='honest' + engine_ver, чтобы дальше жить по правилам honest-строк."""
    retrust = retrust_dates or set()
    ins, upd, rew = [], [], []
    for p in points:
        if p.get("y_idx_bps") is None and p.get("dm_bps") is None:
            continue
        if p["date"] in retrust:
            rew.append((p.get("y_idx_bps"), p.get("dm_bps"), p.get("ytm"),
                        engine_ver, isin, p["date"]))
        elif p["date"] in existing_dates:
            upd.append((p.get("y_idx_bps"), isin, p["date"]))
        else:
            ins.append((isin, p["date"], "floater", p.get("price"), p.get("dm_bps"),
                        None, None, p.get("ytm"), p.get("y_idx_bps"), "honest", engine_ver))
    with _lock, _connect() as c:
        if ins:
            c.executemany(
                "INSERT OR IGNORE INTO spread_daily(isin,date,kind,price_pct,dm_bps,"
                "g_spread_bps,z_bps,ytm,y_idx,src,engine_ver) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ins)
        if upd:
            c.executemany(
                "UPDATE spread_daily SET y_idx=? WHERE isin=? AND date=? AND y_idx IS NULL",
                upd)
        if rew:
            c.executemany(
                "UPDATE spread_daily SET y_idx=?, dm_bps=?, ytm=?, src='honest', "
                "engine_ver=? WHERE isin=? AND date=?", rew)
    return len(ins) + len(upd) + len(rew)
