"""Аналитика первички: спред размещения, премия к вторичке, срезы рынка.

Три вещи, которых нет в сыром слое размещений (services/primary_placements):

1. **Спред размещения** — Y-IDX по цене книги на дату книги. Считается полным
   backdate-пересчётом (кривая as-of, НКД и номинал факт того дня), поэтому
   живёт в кэше `placement_metrics`, а не на чтении: 228 флоатеров реестра за
   год — это 228 прогонов солвера, на каждую отрисовку витрины их не запустить.
   Кэш заодно делает колонку сортируемой и годной для агрегатов.

2. **Премия размещения** — тот же спред через месяц на вторичке. Отвечает на
   вопрос, который иначе задать некому: занял эмитент дёшево или доплатил.
   Знак: `premium_bps > 0` — бумага уехала ШИРЕ (на вторичке требуют больше,
   книгу закрыли жадно), `< 0` — уже (разместились щедро, рынок разобрал).

3. **Срезы** — объём и медианная маржа/спред по месяцам, рейтингам и базам.

Отдельный модуль от primary_placements намеренно: там сбор и чтение сырых
дневных итогов биржи (ISS → база → витрина), тут — наша математика поверх, с
зависимостью на весь прайсинг (реестр, кривые, backdate). Смешивать значит
тащить прайсинг в слой сбора.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Optional

from services.portfolio_db import _connect, _lock
from services.pools import run_bg

logger = logging.getLogger(__name__)

# Версия расчёта: смена методики двигает её, и ночной такт пересчитывает всё.
ANALYTICS_VER = 1
_FLOAT_BASES = ("KEYRATE", "RUONIA")

# Точка «после книги». Окно, а не конкретный день: у неликвида торгов в нужную
# дату может не быть вовсе, а сравнивать надо один раз и честно.
AFTER_TARGET_DAYS = int(os.getenv("PLACEMENT_AFTER_DAYS", "30"))
AFTER_MIN_DAYS = int(os.getenv("PLACEMENT_AFTER_MIN", "20"))
AFTER_MAX_DAYS = int(os.getenv("PLACEMENT_AFTER_MAX", "45"))
# До этого возраста строка со ссылкой на будущее ещё перепроверяется: у
# размещения моложе месяца точки вторички просто не существует. Дальше её
# отсутствие — окончательный ответ (бумага не торгуется), и дёргать нечего.
AFTER_RETRY_DAYS = AFTER_MAX_DAYS + 15


# ────────────────────────── кэш метрик ──────────────────────────

def save_metrics(rows: list[dict]) -> int:
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = [(r["secid"], r.get("isin"), r.get("place_date"), r.get("price"),
            r.get("y_idx_bps"), r.get("dm_bps"), r.get("curve_mode"),
            r.get("after_date"), r.get("after_y_idx_bps"), r.get("premium_bps"),
            ANALYTICS_VER, now, r.get("err")) for r in rows]
    with _lock, _connect() as c:
        cur = c.executemany(
            "INSERT INTO placement_metrics(secid,isin,place_date,price,y_idx_bps,"
            "dm_bps,curve_mode,after_date,after_y_idx_bps,premium_bps,engine_ver,"
            "calc_at,err) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(secid) DO UPDATE SET isin=excluded.isin,"
            "place_date=excluded.place_date, price=excluded.price,"
            "y_idx_bps=excluded.y_idx_bps, dm_bps=excluded.dm_bps,"
            "curve_mode=excluded.curve_mode, after_date=excluded.after_date,"
            "after_y_idx_bps=excluded.after_y_idx_bps, premium_bps=excluded.premium_bps,"
            "engine_ver=excluded.engine_ver, calc_at=excluded.calc_at, err=excluded.err",
            out)
        return cur.rowcount or 0


def metrics_map(secids=None) -> dict[str, dict]:
    q = "SELECT * FROM placement_metrics"
    args: list = []
    ids = [s for s in (secids or []) if s]
    if secids is not None:
        if not ids:
            return {}
        if len(ids) <= 900:
            q += f" WHERE secid IN ({','.join('?' * len(ids))})"
            args = ids
    with _connect() as c:
        return {r["secid"]: dict(r) for r in c.execute(q, args)}


def _after_point(isin: str, place_date: str) -> tuple[Optional[str], Optional[float]]:
    """Спред вторички через ~месяц после книги: ближайший к цели день в окне.

    Источник — суточные снимки spread_daily (та же метрика Y-IDX, что в
    мониторе), поэтому премия сравнивает одно с одним."""
    with _connect() as c:
        rows = c.execute(
            "SELECT date, y_idx FROM spread_daily WHERE isin = ? AND y_idx IS NOT NULL "
            "AND date >= DATE(?, ?) AND date <= DATE(?, ?)",
            (isin, place_date, f"+{AFTER_MIN_DAYS} days",
             place_date, f"+{AFTER_MAX_DAYS} days")).fetchall()
    if not rows:
        return None, None
    target = date.fromisoformat(place_date) + timedelta(days=AFTER_TARGET_DAYS)
    best = min(rows, key=lambda r: abs((date.fromisoformat(r["date"]) - target).days))
    return best["date"], float(best["y_idx"])


def pending(limit: int) -> list[str]:
    """Что считать: новые выпуски и те, у кого точка вторички ещё не созрела.

    Пересчёт по версии движка — сюда же: сменилась методика, значит старые
    строки не сопоставимы с новыми, а именно сопоставимость и есть смысл этой
    таблицы."""
    with _connect() as c:
        rows = c.execute(
            "SELECT p.secid FROM (SELECT secid, MIN(date) d FROM placement_day "
            "  GROUP BY secid) p "
            "LEFT JOIN placement_metrics m ON m.secid = p.secid "
            "WHERE m.secid IS NULL "
            "   OR m.engine_ver IS NOT ? "
            "   OR (m.after_y_idx_bps IS NULL AND m.err IS NULL "
            "       AND p.d >= DATE('now', ?)) "
            "ORDER BY p.d DESC LIMIT ?",
            (ANALYTICS_VER, f"-{AFTER_RETRY_DAYS} days", limit)).fetchall()
    return [r["secid"] for r in rows]


async def compute(limit: int = 60) -> dict:
    """Считает спред размещения и премию пачкой. → статистика прогона.

    Не флоатеры реестра тоже получают строку — с err: иначе они попадали бы в
    очередь каждую ночь вечно. Спред у них не считается принципиально (движок
    backdate флоатерный), и «нет числа» — это ответ, а не пробел."""
    from services import instruments_registry as reg
    from services import primary_placements as pp
    from services.backdate import load_backdate_ctx, reprice_asof
    from services.valuation import pick_horizon

    secids = await run_bg(pending, limit)
    if not secids:
        return {"pending": 0, "done": 0}
    rows = {r["secid"]: r for r in await run_bg(pp.aggregates, None, None, None, 0.0,
                                                pp.MAX_ROWS)}
    labels = await run_bg(reg.labels_map)
    out, ok, skipped, failed = [], 0, 0, 0
    for sec in secids:
        r = rows.get(sec)
        if not r:
            continue
        isin = r.get("isin") or sec
        lab = labels.get(isin) or {}
        base = lab.get("base")
        rec = {"secid": sec, "isin": isin, "place_date": r["first_date"],
               "price": r.get("wa_price")}
        if base not in _FLOAT_BASES:
            rec["err"] = "не флоатер реестра"
            skipped += 1
            out.append(rec)
            continue
        if rec["price"] is None:
            rec["err"] = "нет цены размещения"
            skipped += 1
            out.append(rec)
            continue
        try:
            ctx = await load_backdate_ctx(isin, date.fromisoformat(r["first_date"]))
            m = reprice_asof(ctx, rec["price"])
            # горизонт — тот, что выбрало ПРАВИЛО ЦЕНЫ (та же функция, что у
            # витрин): спред к оферте и спред к погашению нельзя складывать в
            # одну колонку, см. services/valuation.pick_horizon
            hz = pick_horizon(m, "auto")
            rec["y_idx_bps"] = hz.get("yield_over_index_bps")
            rec["dm_bps"] = hz.get("disc_margin_bps")
            rec["curve_mode"] = ctx.get("curve_mode")
        except Exception as e:                                   # noqa: BLE001
            rec["err"] = f"{type(e).__name__}: {e}"[:200]
            failed += 1
            out.append(rec)
            continue
        after_date, after = await run_bg(_after_point, isin, r["first_date"])
        rec["after_date"], rec["after_y_idx_bps"] = after_date, after
        if after is not None and rec["y_idx_bps"] is not None:
            rec["premium_bps"] = after - rec["y_idx_bps"]
        ok += 1
        out.append(rec)
    saved = await run_bg(save_metrics, out)
    return {"pending": len(secids), "done": saved, "priced": ok,
            "skipped": skipped, "failed": failed}


# ─────────────────── архив анонсов и сверка «ориентир ↔ факт» ───────────────────

# Омоглифы: серию выпуска пишут смешанной раскладкой даже внутри одной строки
# («001P-21R» латиницей у Россетей, «001Р-01» кириллицей у ЛОЭСК). Без этой
# таблицы одинаковые на вид серии не совпадают ни одним сравнением.
_HOMO = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")


def _norm(s: Optional[str]) -> str:
    """Ключ сравнения: латиница, без разделителей и ведущих нулей в числах."""
    import re
    if not s:
        return ""
    t = str(s).upper().translate(_HOMO)
    t = re.sub(r"[^0-9A-Z]", "", t)
    return re.sub(r"0+(\d)", r"\1", t)


def _series_of(comment: Optional[str]) -> Optional[str]:
    """Серия из комментария анонса: первый токен до запятой («002Р-08, ESG»)."""
    if not comment:
        return None
    head = comment.split(",")[0].strip()
    return head or None


def archive_announces(rows: list[dict]) -> int:
    """Снимок выгрузки анонсов → durable-архив.

    Кэш bondresearch перезаписывается целиком и держит только ~20 будущих
    размещений: без архива вопрос «где закрылась книга относительно ориентира»
    не с чем сопоставлять — вчерашнего ориентира уже нет нигде."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = datetime.now(timezone.utc).date().isoformat()
    out = []
    for r in rows or []:
        issuer = (r.get("issuer") or "").strip()
        if not issuer:
            continue
        series = _series_of(r.get("comment"))
        key = f"{issuer}|{series or ''}"
        out.append((key, issuer, series, r.get("book_date"), r.get("issue_date"),
                    r.get("coupon_guide"), 1 if r.get("is_floater") else 0,
                    r.get("volume_mln"), "/".join(r.get("ratings") or []),
                    json.dumps(r, ensure_ascii=False),
                    r.get("first_seen") or today, now))
    if not out:
        return 0
    with _lock, _connect() as c:
        cur = c.executemany(
            "INSERT INTO primary_announce(key,issuer,series,book_date,issue_date,"
            "coupon_guide,is_floater,volume_mln,ratings,payload,first_seen,last_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET book_date=excluded.book_date,"
            "issue_date=excluded.issue_date, coupon_guide=excluded.coupon_guide,"
            "is_floater=excluded.is_floater, volume_mln=excluded.volume_mln,"
            "ratings=excluded.ratings, payload=excluded.payload,"
            # first_seen НЕ трогаем: он про то, когда анонс впервые увидели
            "last_seen=excluded.last_seen", out)
        return cur.rowcount or 0


def announces(limit: int = 200, matched: Optional[bool] = None) -> list[dict]:
    """Архив анонсов, свежие сверху. matched=True — только сведённые с фактом."""
    q = "SELECT * FROM primary_announce"
    if matched is True:
        q += " WHERE matched_secid IS NOT NULL"
    elif matched is False:
        q += " WHERE matched_secid IS NULL"
    q += " ORDER BY COALESCE(issue_date, book_date) DESC LIMIT ?"
    with _connect() as c:
        rows = [dict(r) for r in c.execute(q, (limit,))]
    for r in rows:
        try:
            r["payload"] = json.loads(r["payload"]) if r.get("payload") else None
        except ValueError:
            r["payload"] = None
    return rows


def match_announces(window_before: int = 45, window_after: int = 60) -> dict:
    """Сверка анонса с фактом размещения. → статистика прогона.

    Матч по СЕРИИ внутри имени выпуска («Т Плюс 002Р-03» ↔ анонс «002Р-03»)
    плюс проверка эмитента; окно дат вокруг анонсированной даты размещения,
    потому что она регулярно едет. Назад окно шире, чем вперёд: анонс
    ДОРАЗМЕЩЕНИЯ («РЖД 001Р-54R, доразмещение») указывает на бумагу, книга
    которой открылась месяцем раньше.

    ЭМИТЕНТ ОБЯЗАТЕЛЕН. Серия сама по себе не идентификатор: «002Р-03» есть у
    десятков эмитентов, и первый же прогон на голой серии привязал анонс АЛРОСЫ
    к «Т Плюс 002Р-03», а Полипласт — к Ростовской области. Отсутствие привязки
    честнее: пустую ячейку человек перепроверит, ложную — нет.

    score: 1.0 — бренд анонса нашёлся в имени выпуска или в юрлице эмитента;
    0.9 — сошлось только начало бренда (в выгрузке пишут «ПКО ПКБ», в
    справочнике MOEX — «Первое клиентское бюро»), такая строка помечена в
    витрине как требующая взгляда."""
    with _connect() as c:
        anns = [dict(r) for r in c.execute(
            "SELECT key, issuer, series, issue_date, book_date FROM primary_announce "
            "WHERE matched_secid IS NULL")]
        facts = [dict(r) for r in c.execute(
            "SELECT p.secid, MIN(p.date) first_date, MAX(s.name) name, "
            "MAX(s.emitent_title) emitent, MAX(p.shortname) shortname "
            "FROM placement_day p LEFT JOIN sec_ref s ON s.secid = p.secid "
            "GROUP BY p.secid")]
    for f in facts:
        f["n_name"] = _norm(f.get("name") or f.get("shortname"))
        f["n_emit"] = _norm(f.get("emitent"))

    hits = []
    for a in anns:
        anchor = a.get("issue_date") or a.get("book_date")
        if not anchor:
            continue
        try:
            d0 = date.fromisoformat(anchor)
        except ValueError:
            continue
        lo = (d0 - timedelta(days=window_before)).isoformat()
        hi = (d0 + timedelta(days=window_after)).isoformat()
        cand = [f for f in facts if lo <= f["first_date"] <= hi]
        if not cand:
            continue
        n_ser, n_iss = _norm(a.get("series")), _norm(a.get("issuer"))
        if not n_ser or not n_iss:
            continue
        # Нормализация оставляет от кириллического бренда только омоглифы, и
        # «ВЭБ.РФ» ужимается до двух букв. Короткий ключ подстрокой совпадёт с
        # чем угодно, поэтому для него требуем ТОЧНОГО равенства имени.
        strict = len(n_iss) < 4
        best, score = None, 0.0
        for f in cand:
            # СУФФИКС, а не вхождение: серия стоит в конце имени выпуска, а
            # «БО-01» подстрокой сидит внутри «БО-012» — такой матч привязал бы
            # анонс к соседнему выпуску того же эмитента
            if not f["n_name"].endswith(n_ser):
                continue
            head = f["n_name"][:-len(n_ser)] or f["n_name"]   # имя без серии
            if head == n_iss:
                s = 1.0
            elif strict:
                continue
            elif n_iss in head or n_iss in f["n_emit"]:
                s = 1.0
            elif head[:4] and head[:4] == n_iss[:4]:
                # бренд выгрузки против юрлица справочника: «ПКО ПКБ» и «Первое
                # клиентское бюро» не пересекаются ни одной подстрокой, а начало
                # имени выпуска совпадает — но такую привязку помечаем
                s = 0.9
            else:
                continue
            if s > score:
                best, score = f, s
        if best:
            hits.append((best["secid"], score, a["key"]))

    if hits:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _lock, _connect() as c:
            c.executemany("UPDATE primary_announce SET matched_secid=?, match_score=?, "
                          "matched_at=? WHERE key=?",
                          [(h[0], h[1], now, h[2]) for h in hits])
    return {"announces": len(anns), "matched": len(hits)}


# ────────────────────────── срезы рынка первички ──────────────────────────

# Грейд рейтинга: сравнивать «AA-» с «AA+» построчно бессмысленно — бакетов
# должно быть столько, сколько реально различают на рынке.
def _grade(rating: Optional[str]) -> str:
    r = (rating or "").upper().replace("RU", "").strip()
    if not r:
        return "нет"
    for g in ("AAA", "AA", "A", "BBB", "BB", "B"):
        if r.startswith(g):
            return g
    return "ниже"


def market_slices(months: int = 12, only_floaters: bool = True) -> dict:
    """Карта первички: объём и медианные маржа/спред по месяцам и по рейтингам.

    Медиана, а не среднее: один десятимиллиардный выпуск ВЭБа с нулевой маржой
    утаскивает среднее месяца туда, где не размещался никто.

    Маржа берётся из реестра (спека купона), спред — из кэша расчёта книги
    (placement_metrics), поэтому срез умеет молчать: месяц, где ни у одной
    бумаги нет посчитанного спреда, отдаёт по нему None, а не ноль."""
    from services import instruments_registry as reg
    from services import primary_placements as pp

    since = (datetime.now(timezone.utc).date()
             - timedelta(days=31 * max(1, months))).isoformat()
    rows = pp.aggregates(d_from=since, limit=pp.MAX_ROWS)
    labels = reg.labels_map([r["isin"] for r in rows if r.get("isin")])
    metrics = metrics_map([r["secid"] for r in rows])

    items = []
    for r in rows:
        lab = labels.get(r.get("isin") or "") or {}
        base = lab.get("base")
        if only_floaters and base not in _FLOAT_BASES:
            continue
        m = metrics.get(r["secid"]) or {}
        items.append({
            "month": (r["first_date"] or "")[:7],
            "grade": _grade(lab.get("rating")),
            "base": base or "—",
            "value_rub": r.get("value_rub") or 0.0,
            "margin_bps": lab.get("margin_bps"),
            "spread_bps": m.get("y_idx_bps"),
            "premium_bps": m.get("premium_bps"),
        })

    def agg(items_: list[dict], key: str) -> list[dict]:
        buckets: dict[str, list[dict]] = {}
        for it in items_:
            buckets.setdefault(it[key], []).append(it)
        out = []
        for k, group in buckets.items():
            mrg = [g["margin_bps"] for g in group if g["margin_bps"] is not None]
            spr = [g["spread_bps"] for g in group if g["spread_bps"] is not None]
            prm = [g["premium_bps"] for g in group if g["premium_bps"] is not None]
            out.append({
                key: k, "issues": len(group),
                "value_rub": sum(g["value_rub"] for g in group),
                "margin_med_bps": median(mrg) if mrg else None,
                "spread_med_bps": median(spr) if spr else None,
                "premium_med_bps": median(prm) if prm else None,
                "priced": len(spr),
            })
        return sorted(out, key=lambda x: x[key])

    return {"months": agg(items, "month"), "grades": agg(items, "grade"),
            "bases": agg(items, "base"), "issues": len(items),
            "only_floaters": only_floaters}


# ─────────────── что было с бумагой сразу после книги (зеркальный слой) ───────────────

def aftermarket(secid: str, days: int = 30) -> dict:
    """Оборот выпуска в первые дни ЖИЗНИ: вторичка, РПС, выкуп.

    Зачем: размещение показывает, сколько бумаги отдали, но не КОМУ и не
    надолго ли. Крупный РПС на второй день — это переупаковка книги между
    своими, а не рыночный спрос; выкуп (PSBB) сразу после размещения —
    техническая операция эмитента. По самой ленте размещений этого не видно.

    Источники уже собраны другими слоями: block_day — адресные режимы (РПС,
    выкуп, само размещение), bond_day — безадресные торги всего рынка. Глубина
    у обоих с 2024-01, то есть на всю историю размещений.
    """
    with _connect() as c:
        row = c.execute("SELECT MIN(date) d, MAX(isin) isin FROM placement_day "
                        "WHERE secid = ?", (secid,)).fetchone()
        if not row or not row["d"]:
            return {"secid": secid, "found": False}
        isin = row["isin"] or secid
        d0, d1 = row["d"], (date.fromisoformat(row["d"])
                            + timedelta(days=days)).isoformat()
        ndm = [dict(r) for r in c.execute(
            "SELECT board, SUM(value) value_rub, SUM(numtrades) trades, "
            "SUM(volume) volume, COUNT(DISTINCT date) days "
            "FROM block_day WHERE isin = ? AND date >= ? AND date <= ? "
            "GROUP BY board ORDER BY 2 DESC", (isin, d0, d1))]
        main = [dict(r) for r in c.execute(
            "SELECT board, SUM(value) value_rub, SUM(numtrades) trades, "
            "SUM(volume) volume, COUNT(DISTINCT date) days, "
            "MIN(date) first_date, AVG(waprice) wa_price "
            "FROM bond_day WHERE isin = ? AND date >= ? AND date <= ? "
            "GROUP BY board ORDER BY 2 DESC", (isin, d0, d1))]
    # PSAU в block_day — это само размещение, второй раз его показывать не надо:
    # витрина уже стоит на нём
    ndm = [r for r in ndm if r["board"] not in ("PSAU", "PAUS", "PACY")]
    return {
        "secid": secid, "isin": isin, "found": True,
        "from": d0, "to": d1, "days": days,
        "negotiated": ndm,                    # РПС и выкуп
        "market": main,                       # безадресные торги
        "negotiated_rub": sum(r["value_rub"] or 0 for r in ndm),
        "market_rub": sum(r["value_rub"] or 0 for r in main),
    }
