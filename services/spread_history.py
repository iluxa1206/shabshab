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
        # ГОРИЗОНТ пишем вместе со спредом: он у бумаги меняется во времени
        # (подтянулась дата колла из corpbonds, цена перешла порог выкупа), и
        # число без него несопоставимо со вчерашним — линия истории обрывалась
        # на 200+ б.п. без движения цены (СибурХ1Р04/05/06, 12.08.2026).
        # Рядом кладём спред ко ВТОРОМУ горизонту: график сам выберет ту ветку,
        # что совпадает с сегодняшним горизонтом бумаги.
        rows.append((isin, d, "floater", m.get("last"), m.get("disc_dm"),
                     None, m.get("z_model"), m.get("ytm"), m.get("yoi"), "snap",
                     m.get("horizon"), m.get("y_idx_alt"), m.get("alt_horizon")))

    fxm = market_cache.get("fixed_metrics") or {}
    for isin, m in fxm.items():
        if not isin or not isinstance(m, dict):
            continue
        rows.append((isin, d, "fixed", m.get("last"), None,
                     m.get("g_spread_bps"), m.get("z_spread_bps"), m.get("ytm"), None, "snap",
                     m.get("horizon"), None, None))

    # пишем только строки с хоть каким-то спредом (иначе шум пустых)
    rows = [r for r in rows if r[4] is not None or r[5] is not None
            or r[6] is not None or r[8] is not None]
    if not rows:
        return 0
    with _lock, _connect() as c:
        c.executemany(
            "INSERT OR REPLACE INTO spread_daily(isin,date,kind,price_pct,dm_bps,"
            "g_spread_bps,z_bps,ytm,y_idx,src,horizon,y_idx_alt,alt_horizon) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    logger.info("spread snapshot %s: %d строк", d, len(rows))
    return len(rows)


def read_history(isin: str, days: int = 400) -> List[dict]:
    """Точная история по бумаге, по возрастанию даты."""
    with _connect() as c:
        r = c.execute(
            "SELECT date, kind, price_pct, dm_bps, g_spread_bps, z_bps, ytm, y_idx, "
            "y_idx_alt, horizon, alt_horizon, src, engine_ver "
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


def drop_honest(isin: str) -> int:
    """Сносит ВСЕ honest-строки бумаги, независимо от версии движка.

    Нужен, когда изменились ПАРАМЕТРЫ самой бумаги (спека фиксинга, маржа): версия
    движка та же, а числа уже другие, поэтому drop_stale_honest такие строки не
    заметит. Снимки 'snap' не трогаем — их писал живой движок в свой день.
    Вызывающий обязан пересчитать историю заново (ensure_honest_backfill)."""
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM spread_daily WHERE isin=? AND src='honest'", (isin,))
        return cur.rowcount or 0


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


def drop_untrusted(isin: str, dates: set) -> int:
    """Удаляет легаси-строки БЕЗ src за перечисленные даты. Нужно для дат, по
    которым честный движок посчитать не смог (выходные сессии: свеча есть, а
    строки MOEX history нет) — candle-est мусор там иначе жил бы вечно и
    рисовался как exact. src='snap'/'honest' не трогаются."""
    if not dates:
        return 0
    n = 0
    with _lock, _connect() as c:
        for d in dates:
            cur = c.execute(
                "DELETE FROM spread_daily WHERE isin=? AND date=? AND src IS NULL",
                (isin, d))
            n += cur.rowcount or 0
    return n


def rows_without_horizon(isin: str, days: int = 400) -> List[dict]:
    """Строки архива, у которых горизонт неизвестен (писались до того, как снимок
    начал его сохранять). Их спред несопоставим с сегодняшним, если горизонт
    бумаги с тех пор сменился — scripts/backfill_horizon.py пересчитывает их
    честным as-of движком на их же цене."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT date, price_pct, y_idx FROM spread_daily "
            "WHERE isin=? AND kind='floater' AND horizon IS NULL AND date >= ? "
            "ORDER BY date", (isin, cutoff)).fetchall()]


def mark_horizon(isin: str, horizon: str, days: int = 400) -> int:
    """Ставит горизонт строкам без него, НЕ трогая сам спред. Для бумаг, у
    которых горизонт был и остаётся единственным (нет ни оферт, ни коллов):
    пересчитывать нечего, а метка нужна — без неё агрегат считает строку
    несопоставимой с сегодняшней и выбрасывает её с графика."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _lock, _connect() as c:
        cur = c.execute(
            "UPDATE spread_daily SET horizon=? WHERE isin=? AND kind='floater' "
            "AND horizon IS NULL AND date >= ?", (horizon, isin, cutoff))
        return cur.rowcount or 0


def mark_horizon_dates(isin: str, by_date: dict) -> int:
    """Ставит ТОЛЬКО метку горизонта на конкретные даты, не трогая спред.

    Нужна, когда честный движок знает горизонт дня, но не может пересчитать сам
    спред (y_idx=None: не брекетируется DM у глубокого дисконта, дыра в индексе
    и т.п.). Без метки строка вылетает с графика целиком — правило «один горизонт
    на линию» считает её несопоставимой, — хотя её собственный y_idx посчитан
    живым движком в тот же день и к тому же горизонту."""
    upd = [(h, isin, d) for d, h in by_date.items() if h]
    if not upd:
        return 0
    with _lock, _connect() as c:
        cur = c.executemany(
            "UPDATE spread_daily SET horizon=? WHERE isin=? AND date=? "
            "AND kind='floater' AND horizon IS NULL", upd)
        return cur.rowcount or 0


def prev_horizon(isin: str, date: str) -> Optional[str]:
    """Горизонт ближайшей строки ЛЕВЕЕ даты (None — таких нет). Нужен для дней
    без сделок: честная серия их не строит, а снимок строку всё равно писал."""
    with _connect() as c:
        r = c.execute(
            "SELECT horizon FROM spread_daily WHERE isin=? AND kind='floater' "
            "AND date < ? AND horizon IS NOT NULL ORDER BY date DESC LIMIT 1",
            (isin, date)).fetchone()
    return r["horizon"] if r else None


def update_horizon(isin: str, points: list) -> int:
    """Проставляет горизонт (и спред к нему + ко второму) у строк, где его нет.
    Трогает только строки с horizon IS NULL: прошлое, уже посчитанное с горизонтом,
    не переписываем. Возвращает число обновлённых строк."""
    upd = [(p.get("y_idx_bps"), p.get("y_idx_alt_bps"), p.get("horizon"),
            p.get("alt_horizon"), isin, p["date"])
           for p in points if p.get("horizon") and p.get("y_idx_bps") is not None]
    if not upd:
        return 0
    with _lock, _connect() as c:
        cur = c.executemany(
            "UPDATE spread_daily SET y_idx=?, y_idx_alt=?, horizon=?, alt_horizon=? "
            "WHERE isin=? AND date=? AND kind='floater' AND horizon IS NULL", upd)
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
                        p.get("y_idx_alt_bps"), p.get("horizon"), p.get("alt_horizon"),
                        engine_ver, isin, p["date"]))
        elif p["date"] in existing_dates:
            upd.append((p.get("y_idx_bps"), p.get("y_idx_alt_bps"), p.get("horizon"),
                        p.get("alt_horizon"), isin, p["date"]))
        else:
            ins.append((isin, p["date"], "floater", p.get("price"), p.get("dm_bps"),
                        None, None, p.get("ytm"), p.get("y_idx_bps"), "honest", engine_ver,
                        p.get("horizon"), p.get("y_idx_alt_bps"), p.get("alt_horizon")))
    with _lock, _connect() as c:
        if ins:
            c.executemany(
                "INSERT OR IGNORE INTO spread_daily(isin,date,kind,price_pct,dm_bps,"
                "g_spread_bps,z_bps,ytm,y_idx,src,engine_ver,horizon,y_idx_alt,alt_horizon) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ins)
        if upd:
            c.executemany(
                "UPDATE spread_daily SET y_idx=?, y_idx_alt=?, horizon=?, alt_horizon=? "
                "WHERE isin=? AND date=? AND y_idx IS NULL", upd)
        if rew:
            c.executemany(
                "UPDATE spread_daily SET y_idx=?, dm_bps=?, ytm=?, y_idx_alt=?, "
                "horizon=?, alt_horizon=?, src='honest', engine_ver=? "
                "WHERE isin=? AND date=?", rew)
    return len(ins) + len(upd) + len(rew)
