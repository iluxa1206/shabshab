"""Аукционы ОФЗ Минфина: итоги, квартальные планы, план/факт и аналитика.

Зачем отдельно от истории размещений (services/primary_placements): биржа знает
про аукцион только строку борда PSAU — объём и цену. Цена ОТСЕЧЕНИЯ, спрос,
коэффициент удовлетворения, «несостоявшийся», ДРПА — всё это есть только у
самого Минфина, а план квартала — тем более. ISS здесь не нужен вовсе.

Источники (оба на minfin.gov.ru, обязателен БРАУЗЕРНЫЙ User-Agent — без него
сайт отдаёт 503):
  • итоги — годовые xlsx `INTERNET_Auction_Results_rus_<год>_<дата>.xlsx` со
    страницы-индекса аукционов. Файл текущего года ПЕРЕВЫПУСКАЕТСЯ после каждого
    аукциона (дата в имени = «по состоянию на»), прошлые годы — финальные;
  • планы — HTML-страницы «График аукционов … на N квартал» из индекса
    /ru/statistics/docs/auction: даты аукционов и таблица «индикативное
    распределение планового объёма привлечения по срокам до погашения».

ВАЖНО про смысл плана. План — это «объём привлечения средств» по ст. 113 БК:
деньги БЕЗ НКД и БЕЗ премии сверх номинала. Длинные ОФЗ-ПД идут по 58–92 %
номинала, поэтому «размещено по номиналу» и «привлечено по 113-й» расходятся
на треть. Факт для сравнения с планом:
    proceeds_113 = размещено_по_номиналу × min(ср.взвеш. цена, 100) / 100.
Витрина показывает ОБА числа, прогресс — по 113-й.

Сеть только в sync_*; парсеры и аналитика — чистые функции над строками.
"""
from __future__ import annotations

import html as html_lib
import io
import logging
import re
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Optional

import httpx

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

BASE_URL = "https://minfin.gov.ru"
RESULTS_INDEX_URL = BASE_URL + "/ru/perfomance/public_debt/internal/operations/ofz/auction/"
PLANS_INDEX_URL = BASE_URL + "/ru/statistics/docs/auction"
# Без браузерного UA Минфин отвечает 503 (проверено 2026-09-15 с ноута и прода).
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "Chrome/128 Safari/537.36")
TIMEOUT = 30.0

_RESULTS_RE = re.compile(r"(/common/upload/[^\"'\s]*?INTERNET_Auction_Results_rus_(\d{4})_(\d{8})\.xlsx)")
_PLAN_LINK_RE = re.compile(r"href=\"([^\"]*?id_65=(\d+)-grafik_auktsionov[^\"]*?na_(i|ii|iii|iv)_kvartal_(\d{4})_goda[^\"]*)\"",
                           re.I)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
           "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_DATE_RU_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})\s*г", re.I)
_QUARTER_TITLE_RE = re.compile(r"на\s+(I{1,3}|IV)\s+квартал\s+(\d{4})\s+года", re.I)

# Стандартные корзины срока для срезов статистики (когда плана нет или он
# другой): короткие/средние/длинные — как Минфин чаще всего и делит.
STD_BUCKETS = [("до 5 лет", None, 5.0), ("от 5 до 10 лет", 5.0, 10.0), ("от 10 лет", 10.0, None)]

_FMT = {"аукцион": "auction", "дрпа": "drpa"}


# ────────────────────────── нормализация ──────────────────────────

def _num(v) -> Optional[float]:
    """Число из ячейки Минфина: '-', '-****', '' → None; '1 234,5' → 1234.5."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not t or t.startswith("-*") or t in ("-", "—", "–"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _iso(v) -> Optional[str]:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", v.strip())
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v.strip())
        if m:
            return m.group(0)
    return None


def quarter_of(d: str) -> str:
    """'2026-07-15' → '2026Q3'."""
    y, m = int(d[:4]), int(d[5:7])
    return f"{y}Q{(m - 1) // 3 + 1}"


def quarter_bounds(q: str) -> tuple[str, str]:
    """'2026Q3' → ('2026-07-01', '2026-09-30')."""
    y, n = int(q[:4]), int(q[-1])
    m0 = (n - 1) * 3 + 1
    start = date(y, m0, 1)
    end = date(y + (1 if n == 4 else 0), 1 if n == 4 else m0 + 3, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def next_quarter(q: str) -> str:
    y, n = int(q[:4]), int(q[-1])
    return f"{y + 1}Q1" if n == 4 else f"{y}Q{n + 1}"


def proceeds_113(placed_mln: Optional[float], wap_price: Optional[float]) -> Optional[float]:
    """Привлечение по ст. 113 БК: номинал × min(цена, 100)/100. Премия сверх
    номинала в программу заимствований не идёт, дисконт — уменьшает."""
    if placed_mln is None:
        return None
    if not placed_mln:
        return 0.0
    if wap_price is None:
        return None
    return placed_mln * min(wap_price, 100.0) / 100.0


# ────────────────────────── парсер итогов (xlsx) ──────────────────────────

def parse_results_xlsx(data: bytes) -> list[dict]:
    """Годовой xlsx Минфина → строки аукционов. Чистая функция.

    Лист 1; заголовок — строка, где A начинается с 'Дата', данные — строки, где
    A — дата, конец — 'Итого'. Все '-', '-****', '' → None. Статусы:
    ok / failed (аукцион без цены или с нулевым размещением) / drpa.

    Две раскладки: с 2024 года 15 колонок (вторая — «Формат»: Аукцион/ДРПА),
    файлы 2021–2023 — 14 колонок без формата (ДРПА тогда не было). Раскладку
    узнаём по заголовку и в старой подставляем «Аукцион»."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    ws = wb.worksheets[0]
    out: list[dict] = []
    started = False
    has_fmt = True
    for row in ws.iter_rows(values_only=True):
        a = row[0] if row else None
        if not started:
            if isinstance(a, str) and a.strip().lower().startswith("дата"):
                started = True
                has_fmt = any(isinstance(h, str) and "формат" in h.lower() for h in row)
            continue
        if isinstance(a, str) and a.strip().lower().startswith("итого"):
            break
        d = _iso(a)
        if not d:
            continue
        cells = list(row) if has_fmt else [row[0], "Аукцион", *row[1:]]
        cells = cells + [None] * (15 - len(cells))
        fmt_raw = str(cells[1] or "").strip()
        fmt = _FMT.get(fmt_raw.lower(), fmt_raw.lower() or "auction")
        code = str(cells[2] or "").strip().upper()
        if not code:
            continue
        sec_type = str(cells[3] or "").strip()
        placed = _num(cells[12])
        cut_price, wap_price = _num(cells[7]), _num(cells[8])
        # доходность 0 у ПК — «не рассчитывается», а не ноль процентов
        cut_yield = _num(cells[9]) or None
        wap_yield = _num(cells[10]) or None
        if fmt == "drpa":
            status = "drpa"
        elif cut_price is None or not placed:
            status = "failed"
        else:
            status = "ok"
        dm = _num(cells[5])
        out.append({
            "date": d, "code": code, "fmt": fmt, "sec_type": sec_type,
            "maturity": _iso(cells[4]),
            "days_to_mat": int(dm) if dm is not None else None,
            "offered_mln": _num(cells[6]),
            "cut_price": cut_price, "wap_price": wap_price,
            "cut_yield": cut_yield, "wap_yield": wap_yield,
            "demand_mln": _num(cells[11]),
            "placed_mln": placed if placed is not None else 0.0,
            "revenue_mln": _num(cells[13]),
            "fill_ratio": _num(cells[14]),
            "status": status,
        })
    wb.close()
    return out


# ────────────────────────── парсер плана (HTML) ──────────────────────────

def _strip_tags(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_lib.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def parse_bucket(label: str) -> Optional[tuple[Optional[float], Optional[float]]]:
    """Текст корзины → (lo, hi) лет; None, если это не корзина срока.

    «до 10 лет включительно» → (None, 10); «от 5 до 10 лет» → (5, 10);
    «от 10 лет» / «свыше 10 лет» → (10, None). Включительность — граница в hi:
    корзина берёт срок lo < t <= hi."""
    t = label.lower().replace(",", ".")
    if "лет" not in t and "год" not in t:
        return None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]
    if not nums:
        return None
    if re.search(r"\bот\b|свыше|более|больше", t) and re.search(r"\bдо\b", t) and len(nums) >= 2:
        return nums[0], nums[1]
    if re.search(r"\bот\b|свыше|более|больше", t):
        return nums[0], None
    if re.search(r"\bдо\b|не более|менее", t):
        return None, nums[0]
    return None


def parse_plan_html(html: str, url: str = "") -> dict:
    """Страница графика квартала → {quarter, dates[], buckets[], src_id, src_url}.

    Текст документа есть прямо в HTML (doc качать не нужно). Даты — «1 июля
    2026 г.» по русским месяцам, только внутри своего квартала (на странице
    есть и чужие даты — публикации, соседние документы). Корзины — строки
    таблицы «Индикативное распределение…»: `до 10 лет включительно | 900`."""
    m = re.search(r"id_65=(\d+)", url or "") or re.search(r"id_65=(\d+)", html)
    src_id = int(m.group(1)) if m else None

    title = ""
    tm = re.search(r"<title>(.*?)</title>", html, flags=re.S | re.I)
    if tm:
        title = _strip_tags(tm.group(1))
    qm = _QUARTER_TITLE_RE.search(title) or _QUARTER_TITLE_RE.search(_strip_tags(html))
    quarter = None
    if qm:
        quarter = f"{int(qm.group(2))}Q{_ROMAN[qm.group(1).lower()]}"

    text = _strip_tags(html)
    dates: list[str] = []
    for dd, mon, yy in _DATE_RU_RE.findall(text):
        d = f"{int(yy):04d}-{_MONTHS[mon.lower()]:02d}-{int(dd):02d}"
        if quarter and quarter_of(d) != quarter:
            continue
        if d not in dates:
            dates.append(d)
    dates.sort()

    buckets: list[dict] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        tds = [_strip_tags(x) for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)]
        tds = [x for x in tds if x]
        if len(tds) < 2:
            continue
        rng = parse_bucket(tds[0])
        if rng is None:
            continue
        amount = _num(tds[1].replace(" ", ""))
        if amount is None:
            continue
        label = re.sub(r"\s+", " ", tds[0]).strip()
        if any(b["bucket"] == label for b in buckets):
            continue
        buckets.append({"bucket": label, "lo_y": rng[0], "hi_y": rng[1], "amount_bln": amount})

    return {"quarter": quarter, "title": title, "dates": dates, "buckets": buckets,
            "src_id": src_id, "src_url": url}


# ────────────────────────── запись ──────────────────────────

_FIELDS = ("date", "code", "fmt", "secid", "isin", "sec_type", "maturity", "days_to_mat",
           "offered_mln", "cut_price", "wap_price", "cut_yield", "wap_yield", "demand_mln",
           "placed_mln", "revenue_mln", "fill_ratio", "status", "src_file", "at")


def upsert_results(rows: list[dict], src_file: str) -> int:
    """Строки парсера → ofz_auction. secid/isin здесь НЕ трогаем: их ставит
    resolve_secids, а перезапись файла не должна их стирать."""
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = [tuple(({**r, "src_file": src_file, "at": now}).get(k) for k in _FIELDS) for r in rows]
    upd = ",".join(f"{k}=excluded.{k}" for k in _FIELDS if k not in ("date", "code", "fmt", "secid", "isin"))
    with _lock, _connect() as c:
        cur = c.executemany(
            f"INSERT INTO ofz_auction({','.join(_FIELDS)}) VALUES({','.join('?' * len(_FIELDS))}) "
            f"ON CONFLICT(date, code, fmt) DO UPDATE SET {upd}", out)
        return cur.rowcount or 0


def resolve_secids() -> dict:
    """Код выпуска (без контрольной цифры) → SECID/ISIN через sec_ref по LIKE.

    Справочник всего рынка (sec_ref) знает и погашенные выпуски. При 2+
    совпадениях (теоретически: SU26230RMFS1 и что-то с тем же префиксом) берём
    торгуемую или ту, чья дата погашения совпадает с датой из файла Минфина;
    остальное — в лог."""
    with _connect() as c:
        pending = [dict(r) for r in c.execute(
            "SELECT DISTINCT code, maturity FROM ofz_auction WHERE secid IS NULL OR isin IS NULL")]
        found, ambiguous, missing = 0, 0, 0
        updates = []
        for p in pending:
            cands = [dict(r) for r in c.execute(
                "SELECT secid, isin, is_traded, mat_date FROM sec_ref WHERE secid LIKE ?",
                (f"SU{p['code']}%",))]
            if not cands:
                missing += 1
                continue
            if len(cands) > 1:
                ambiguous += 1
                by_mat = [x for x in cands if x.get("mat_date") == p.get("maturity")]
                traded = [x for x in cands if x.get("is_traded")]
                pick = (by_mat or traded or cands)[0]
                logger.info("аукционы ОФЗ: код %s → %d кандидатов (%s), взят %s",
                            p["code"], len(cands), ", ".join(x["secid"] for x in cands), pick["secid"])
            else:
                pick = cands[0]
            updates.append((pick["secid"], pick.get("isin") or pick["secid"], p["code"]))
            found += 1
    if updates:
        with _lock, _connect() as c:
            c.executemany("UPDATE ofz_auction SET secid=?, isin=? WHERE code=? AND secid IS NULL",
                          updates)
    return {"pending": len(pending), "resolved": found, "ambiguous": ambiguous, "missing": missing}


def save_plan(doc: dict) -> int:
    """План квартала → ofz_auction_plan + ofz_auction_dates (замена целиком:
    «уточнённый» график меняет и корзины, и даты)."""
    q = doc.get("quarter")
    if not q:
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock, _connect() as c:
        c.execute("DELETE FROM ofz_auction_plan WHERE quarter=?", (q,))
        c.execute("DELETE FROM ofz_auction_dates WHERE quarter=?", (q,))
        c.executemany(
            "INSERT INTO ofz_auction_plan(quarter,bucket,lo_y,hi_y,amount_bln,src_url,src_id,at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(q, b["bucket"], b.get("lo_y"), b.get("hi_y"), b.get("amount_bln"),
              doc.get("src_url"), doc.get("src_id"), now) for b in doc.get("buckets") or []])
        c.executemany("INSERT OR IGNORE INTO ofz_auction_dates(quarter,date) VALUES(?,?)",
                      [(q, d) for d in doc.get("dates") or []])
        # квартал без корзин (страница без таблицы) всё равно помечаем — иначе
        # каждый синк будет качать его заново
        if not doc.get("buckets"):
            c.execute("INSERT OR REPLACE INTO ofz_auction_plan(quarter,bucket,lo_y,hi_y,amount_bln,"
                      "src_url,src_id,at) VALUES(?,?,?,?,?,?,?,?)",
                      (q, "", None, None, None, doc.get("src_url"), doc.get("src_id"), now))
    return len(doc.get("buckets") or [])


def _sync_get(key: str) -> Optional[str]:
    with _connect() as c:
        r = c.execute("SELECT note FROM ofz_auction_sync WHERE key=?", (key,)).fetchone()
        return r["note"] if r else None


def _sync_set(key: str, note: str) -> None:
    with _lock, _connect() as c:
        c.execute("INSERT INTO ofz_auction_sync(key,at,note) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET at=excluded.at, note=excluded.note",
                  (key, datetime.now(timezone.utc).isoformat(timespec="seconds"), note))


def is_empty() -> bool:
    with _connect() as c:
        return not c.execute("SELECT 1 FROM ofz_auction LIMIT 1").fetchone()


# ────────────────────────── синк (сеть) ──────────────────────────

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
                             timeout=TIMEOUT, follow_redirects=True)


def parse_results_index(html: str) -> dict[int, tuple[str, str]]:
    """Индекс аукционов → {год: (url, имя файла)} — на год ссылка с максимальной
    датой «по состоянию на»."""
    best: dict[int, tuple[str, str, str]] = {}
    for path, year, stamp in _RESULTS_RE.findall(html):
        y = int(year)
        if y not in best or stamp > best[y][0]:
            best[y] = (stamp, path, path.rsplit("/", 1)[-1])
    return {y: (BASE_URL + v[1] if v[1].startswith("/") else v[1], v[2]) for y, v in best.items()}


def parse_plans_index(html: str) -> dict[str, tuple[int, str]]:
    """Индекс графиков → {квартал: (id_65, url)}; при дублях («уточнённый»)
    побеждает больший id."""
    out: dict[str, tuple[int, str]] = {}
    for href, sid, roman, year in _PLAN_LINK_RE.findall(html):
        q = f"{int(year)}Q{_ROMAN[roman.lower()]}"
        sid = int(sid)
        url = html_lib.unescape(href)
        if url.startswith("/"):
            url = BASE_URL + url
        if q not in out or sid > out[q][0]:
            out[q] = (sid, url)
    return out


async def sync_results(years: Optional[list[int]] = None, all_years: bool = False,
                       force: bool = False) -> dict:
    """Индекс → xlsx по годам → парсинг → upsert → резолв SECID.

    Качаем только если имя файла в индексе отличается от записанного в
    ofz_auction_sync (за прошлые годы файл финальный — один раз и навсегда).
    По умолчанию — текущий и прошлый год; all_years — всё из индекса."""
    from services.pools import run_bg
    today = datetime.now(timezone.utc).date()
    want = None if all_years else (years or [today.year, today.year - 1])
    out: dict = {"years": {}, "downloaded": 0}
    async with _client() as client:
        r = await client.get(RESULTS_INDEX_URL)
        r.raise_for_status()
        index = parse_results_index(r.text)
        out["index"] = sorted(index)
        for y in sorted(index):
            if want is not None and y not in want:
                continue
            url, fname = index[y]
            key = f"results:{y}"
            if not force and await run_bg(_sync_get, key) == fname:
                out["years"][y] = {"status": "fresh", "file": fname}
                continue
            rr = await client.get(url)
            if rr.status_code != 200:
                logger.warning("аукционы ОФЗ %s: HTTP %s (%s)", y, rr.status_code, url)
                out["years"][y] = {"status": f"http {rr.status_code}", "file": fname}
                continue
            rows = await run_bg(parse_results_xlsx, rr.content)
            if not rows:
                logger.warning("аукционы ОФЗ %s: файл %s пуст — не пишем", y, fname)
                out["years"][y] = {"status": "empty", "file": fname}
                continue
            n = await run_bg(upsert_results, rows, fname)
            await run_bg(_sync_set, key, fname)
            out["downloaded"] += 1
            out["years"][y] = {"status": "updated", "file": fname, "rows": n}
    out["resolve"] = await run_bg(resolve_secids)
    return out


def _known_plan_ids() -> dict[str, Optional[int]]:
    with _connect() as c:
        return {r["quarter"]: r["src_id"] for r in c.execute(
            "SELECT quarter, MAX(src_id) src_id FROM ofz_auction_plan GROUP BY quarter")}


async def sync_plans(force: bool = False) -> dict:
    """Индекс графиков → страницы, которых нет в базе (по src_id) + всегда
    текущий и следующий квартал (их страница могла обновиться под тем же id)."""
    from services.pools import run_bg
    today = datetime.now(timezone.utc).date()
    cur_q = quarter_of(today.isoformat())
    always = {cur_q, next_quarter(cur_q)}
    out: dict = {"quarters": {}, "fetched": 0}
    async with _client() as client:
        r = await client.get(PLANS_INDEX_URL)
        r.raise_for_status()
        index = parse_plans_index(r.text)
        out["index"] = sorted(index)
        known = await run_bg(_known_plan_ids)
        for q, (sid, url) in sorted(index.items()):
            have = known.get(q)
            if not force and q not in always and have is not None and have >= sid:
                continue
            rr = await client.get(url)
            if rr.status_code != 200:
                logger.warning("план аукционов %s: HTTP %s (%s)", q, rr.status_code, url)
                out["quarters"][q] = {"status": f"http {rr.status_code}"}
                continue
            doc = await run_bg(parse_plan_html, rr.text, url)
            if doc.get("quarter") and doc["quarter"] != q:
                # заголовок страницы важнее слага ссылки
                logger.info("план аукционов: слаг %s, заголовок %s", q, doc["quarter"])
            doc["quarter"] = doc.get("quarter") or q
            n = await run_bg(save_plan, doc)
            out["fetched"] += 1
            out["quarters"][doc["quarter"]] = {
                "status": "updated", "buckets": n, "dates": len(doc.get("dates") or []),
                "plan_bln": sum(b["amount_bln"] or 0 for b in doc.get("buckets") or []), "src_id": sid}
    return out


# ────────────────────────── чтение ──────────────────────────

def _load_rows(d_from: Optional[str] = None, d_to: Optional[str] = None,
               sec_type: Optional[str] = None, fmt: Optional[str] = None,
               secid: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    sql = ("SELECT a.*, s.shortname FROM ofz_auction a LEFT JOIN sec_ref s ON s.secid = a.secid "
           "WHERE 1=1")
    args: list = []
    if d_from:
        sql += " AND a.date >= ?"
        args.append(d_from)
    if d_to:
        sql += " AND a.date <= ?"
        args.append(d_to)
    if sec_type:
        sql += " AND a.sec_type = ?"
        args.append(sec_type)
    if fmt:
        sql += " AND a.fmt = ?"
        args.append(fmt)
    if secid:
        # витрина может прислать и SECID, и голый код выпуска
        sql += " AND (a.secid = ? OR a.code = ?)"
        args += [secid, secid.upper()]
    if status:
        sql += " AND a.status = ?"
        args.append(status)
    sql += " ORDER BY a.date, a.code, a.fmt"
    with _connect() as c:
        return [dict(r) for r in c.execute(sql, args)]


def _secondary_ytm(rows: list[dict], lookback_days: int = 7) -> dict[tuple[str, str], float]:
    """{(isin, дата аукциона): YTM вторички на последний торговый день ДО аукциона}
    из spread_daily kind=fixed (вечерний снимок, есть с ~2026-07). Нет точки в
    окне — премии нет (None), а не «0»."""
    isins = sorted({r["isin"] for r in rows if r.get("isin")})
    if not isins or not rows:
        return {}
    d_min = min(r["date"] for r in rows)
    d_max = max(r["date"] for r in rows)
    lo = (date.fromisoformat(d_min) - timedelta(days=lookback_days)).isoformat()
    hist: dict[str, list[tuple[str, float]]] = {}
    with _connect() as c:
        for i in range(0, len(isins), 800):
            chunk = isins[i:i + 800]
            for r in c.execute(
                    f"SELECT isin, date, ytm FROM spread_daily WHERE kind='fixed' AND ytm IS NOT NULL "
                    f"AND date >= ? AND date < ? AND isin IN ({','.join('?' * len(chunk))}) "
                    f"ORDER BY isin, date", [lo, d_max, *chunk]):
                hist.setdefault(r["isin"], []).append((r["date"], float(r["ytm"])))
    out: dict[tuple[str, str], float] = {}
    for r in rows:
        isin = r.get("isin")
        if not isin or isin not in hist:
            continue
        floor = (date.fromisoformat(r["date"]) - timedelta(days=lookback_days)).isoformat()
        best = None
        for d, y in hist[isin]:
            if floor <= d < r["date"]:
                best = y
            elif d >= r["date"]:
                break
        if best is not None:
            out[(isin, r["date"])] = best
    return out


def enrich(rows: list[dict], sec_ytm: Optional[dict] = None) -> list[dict]:
    """Производные поля: срок в годах, bid/cover, 113-я, премия к вторичке (бп).
    Премия только у ПД: у ИН доходность реальная, у ПК её нет вовсе."""
    for r in rows:
        dm = r.get("days_to_mat")
        r["term_y"] = round(dm / 365.25, 2) if dm is not None else None
        dem, pl = r.get("demand_mln"), r.get("placed_mln")
        r["bid_cover"] = round(dem / pl, 2) if dem and pl else None
        r["proceeds_113_mln"] = proceeds_113(pl, r.get("wap_price"))
        prem = None
        if sec_ytm and r.get("sec_type") == "ОФЗ-ПД" and r.get("wap_yield") is not None:
            y = sec_ytm.get((r.get("isin"), r["date"]))
            if y is not None:
                prem = round((r["wap_yield"] - y) * 100.0, 1)
                r["secondary_ytm"] = y
        r["premium_bps"] = prem
    return rows


def results(d_from: Optional[str] = None, d_to: Optional[str] = None,
            sec_type: Optional[str] = None, fmt: Optional[str] = None,
            secid: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    rows = _load_rows(d_from, d_to, sec_type, fmt, secid, status)
    return enrich(rows, _secondary_ytm(rows))


# ────────────────────────── план/факт ──────────────────────────

def bucket_for(term_y: Optional[float], buckets: list[dict]) -> Optional[str]:
    """Корзина плана по сроку: lo < t <= hi (включительность в hi)."""
    if term_y is None:
        return None
    for b in buckets:
        lo, hi = b.get("lo_y"), b.get("hi_y")
        if (lo is None or term_y > lo) and (hi is None or term_y <= hi):
            return b["bucket"]
    return None


def plan_fact_calc(quarter: str, plan_rows: list[dict], dates: list[str],
                   rows: list[dict], today: Optional[str] = None) -> dict:
    """Чистый расчёт план/факт квартала. rows — строки аукционов квартала
    (ДРПА тоже: это такое же привлечение), plan_rows — корзины, dates — даты
    графика. today — для «сколько прошло / осталось» (тесты подставляют)."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    buckets = [b for b in plan_rows if b.get("bucket")]
    plan_total = sum(b.get("amount_bln") or 0 for b in buckets) if buckets else None
    rows = enrich([dict(r) for r in rows])

    fact_nom = sum((r.get("placed_mln") or 0) for r in rows) / 1000.0
    fact_113 = sum((r.get("proceeds_113_mln") or 0) for r in rows) / 1000.0

    held_dates = sorted({r["date"] for r in rows})
    planned = sorted(dates)
    passed = [d for d in planned if d < today or (d == today and d in held_dates)]
    remaining_dates = [d for d in planned if d not in passed]
    next_date = remaining_dates[0] if remaining_dates else None
    remaining = len(remaining_dates)
    held = len(held_dates)
    gap = (plan_total - fact_113) if plan_total is not None else None
    need = (max(gap, 0.0) / remaining) if (gap is not None and remaining) else None
    avg = fact_113 / held if held else None
    pace = None
    if plan_total and planned and passed:
        expected = plan_total * len(passed) / len(planned)
        pace = fact_113 / expected if expected else None

    by_bucket = []
    for b in buckets:
        sel = [r for r in rows if bucket_for(r.get("term_y"), buckets) == b["bucket"]]
        nom = sum((r.get("placed_mln") or 0) for r in sel) / 1000.0
        p113 = sum((r.get("proceeds_113_mln") or 0) for r in sel) / 1000.0
        amt = b.get("amount_bln") or 0
        by_bucket.append({
            "bucket": b["bucket"], "lo_y": b.get("lo_y"), "hi_y": b.get("hi_y"),
            "plan_bln": amt, "fact_nominal_bln": round(nom, 3), "fact_113_bln": round(p113, 3),
            "pct_113": round(100.0 * p113 / amt, 1) if amt else None,
            "n": len([r for r in sel if r["fmt"] == "auction"]),
        })
    # строки вне корзин (план без покрытия срока) — видно отдельной строкой
    if buckets:
        other = [r for r in rows if bucket_for(r.get("term_y"), buckets) is None]
        if other:
            by_bucket.append({
                "bucket": "вне корзин", "lo_y": None, "hi_y": None, "plan_bln": None,
                "fact_nominal_bln": round(sum((r.get("placed_mln") or 0) for r in other) / 1000.0, 3),
                "fact_113_bln": round(sum((r.get("proceeds_113_mln") or 0) for r in other) / 1000.0, 3),
                "pct_113": None, "n": len(other)})

    # по датам: и прошедшие с фактом, и будущие из графика — для графика
    # кумулятива против равномерной прямой плана
    all_dates = sorted(set(planned) | set(held_dates))
    by_date, cum = [], 0.0
    n_plan = len(planned) or len(all_dates)
    for i, d in enumerate(all_dates):
        sel = [r for r in rows if r["date"] == d]
        p113 = sum((r.get("proceeds_113_mln") or 0) for r in sel) / 1000.0
        cum += p113
        by_type: dict[str, float] = {}
        for r in sel:
            by_type[r.get("sec_type") or "?"] = by_type.get(r.get("sec_type") or "?", 0.0) \
                + (r.get("placed_mln") or 0) / 1000.0
        idx = planned.index(d) + 1 if d in planned else None
        by_date.append({
            "date": d, "planned": d in planned, "held": d in held_dates,
            "n": len([r for r in sel if r["fmt"] == "auction"]),
            "n_failed": len([r for r in sel if r["status"] == "failed"]),
            "placed_bln": round(sum((r.get("placed_mln") or 0) for r in sel) / 1000.0, 3),
            "demand_bln": round(sum((r.get("demand_mln") or 0) for r in sel) / 1000.0, 3),
            "proceeds_113_bln": round(p113, 3),
            "cum_113_bln": round(cum, 3) if d in held_dates else None,
            "plan_line_bln": round(plan_total * idx / n_plan, 3) if (plan_total is not None and idx) else None,
            "by_type": {k: round(v, 3) for k, v in by_type.items()},
            "issues": [{"code": r["code"], "secid": r.get("secid"), "fmt": r["fmt"],
                        "status": r["status"], "placed_mln": r.get("placed_mln"),
                        "wap_yield": r.get("wap_yield"), "sec_type": r.get("sec_type")} for r in sel],
        })

    return {
        "quarter": quarter, "from": quarter_bounds(quarter)[0], "to": quarter_bounds(quarter)[1],
        "has_plan": plan_total is not None,
        "plan_bln": plan_total,
        "fact_nominal_bln": round(fact_nom, 3), "fact_113_bln": round(fact_113, 3),
        "pct_113": round(100.0 * fact_113 / plan_total, 1) if plan_total else None,
        "pct_nominal": round(100.0 * fact_nom / plan_total, 1) if plan_total else None,
        "auctions_held": held, "auctions_planned": len(planned),
        "dates_passed": len(passed), "remaining": remaining, "next_date": next_date,
        "need_per_auction_bln": round(need, 3) if need is not None else None,
        "avg_per_auction_bln": round(avg, 3) if avg is not None else None,
        "pace": round(pace, 3) if pace is not None else None,
        "n_rows": len(rows), "n_failed": len([r for r in rows if r["status"] == "failed"]),
        "n_drpa": len([r for r in rows if r["fmt"] == "drpa"]),
        "drpa_113_bln": round(sum((r.get("proceeds_113_mln") or 0) for r in rows if r["fmt"] == "drpa") / 1000.0, 3),
        "buckets": by_bucket, "by_date": by_date, "dates": planned,
        "src_url": next((b.get("src_url") for b in plan_rows if b.get("src_url")), None),
        "today": today,
    }


def _plan_rows(quarter: str) -> list[dict]:
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM ofz_auction_plan WHERE quarter=? ORDER BY COALESCE(lo_y, -1), COALESCE(hi_y, 1e9)",
            (quarter,))]


def _plan_dates(quarter: str) -> list[str]:
    with _connect() as c:
        return [r["date"] for r in c.execute(
            "SELECT date FROM ofz_auction_dates WHERE quarter=? ORDER BY date", (quarter,))]


def plan_fact(quarter: Optional[str] = None, today: Optional[str] = None) -> dict:
    """План/факт квартала из базы. Квартал без плана (старые) — только факт."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    quarter = quarter or quarter_of(today)
    lo, hi = quarter_bounds(quarter)
    return plan_fact_calc(quarter, _plan_rows(quarter), _plan_dates(quarter),
                          _load_rows(lo, hi), today)


def quarters() -> list[dict]:
    """Кварталы с планом и/или фактом, свежие сверху."""
    with _connect() as c:
        plans = {r["quarter"]: r["plan"] for r in c.execute(
            "SELECT quarter, SUM(amount_bln) plan FROM ofz_auction_plan GROUP BY quarter")}
        facts: dict[str, dict] = {}
        for r in c.execute("SELECT date, placed_mln, wap_price FROM ofz_auction"):
            q = quarter_of(r["date"])
            f = facts.setdefault(q, {"nom": 0.0, "p113": 0.0, "n": 0})
            f["nom"] += (r["placed_mln"] or 0) / 1000.0
            f["p113"] += (proceeds_113(r["placed_mln"], r["wap_price"]) or 0) / 1000.0
            f["n"] += 1
    out = []
    for q in sorted(set(plans) | set(facts), reverse=True):
        f = facts.get(q) or {}
        out.append({"quarter": q, "has_plan": q in plans and plans[q] is not None,
                    "plan_bln": plans.get(q), "fact_nominal_bln": round(f.get("nom", 0.0), 3),
                    "fact_113_bln": round(f.get("p113", 0.0), 3), "n": f.get("n", 0)})
    return out


# ────────────────────────── ряды и статистика ──────────────────────────

def series_calc(rows: list[dict]) -> list[dict]:
    """По датам аукционов: размещение, спрос, 113-я, число/несостоявшиеся,
    взвешенная размещением wap-доходность (только ПД) + кумулятив 113-й
    внутри квартала."""
    rows = enrich([dict(r) for r in rows])
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["date"], []).append(r)
    out, cum, cur_q = [], 0.0, None
    for d in sorted(by):
        sel = by[d]
        q = quarter_of(d)
        if q != cur_q:
            cur_q, cum = q, 0.0
        p113 = sum((r.get("proceeds_113_mln") or 0) for r in sel)
        cum += p113
        pd_rows = [r for r in sel if r.get("sec_type") == "ОФЗ-ПД" and r.get("wap_yield") is not None
                   and (r.get("placed_mln") or 0) > 0]
        w = sum(r["placed_mln"] for r in pd_rows)
        wy = sum(r["wap_yield"] * r["placed_mln"] for r in pd_rows) / w if w else None
        by_type: dict[str, float] = {}
        for r in sel:
            t = r.get("sec_type") or "?"
            by_type[t] = by_type.get(t, 0.0) + (r.get("placed_mln") or 0)
        out.append({
            "date": d, "quarter": q,
            "placed_mln": round(sum((r.get("placed_mln") or 0) for r in sel), 3),
            "demand_mln": round(sum((r.get("demand_mln") or 0) for r in sel), 3),
            "proceeds_113_mln": round(p113, 3),
            "cum_113_mln": round(cum, 3),
            "n": len([r for r in sel if r["fmt"] == "auction"]),
            "n_failed": len([r for r in sel if r["status"] == "failed"]),
            "n_drpa": len([r for r in sel if r["fmt"] == "drpa"]),
            "wap_yield_w": round(wy, 3) if wy is not None else None,
            "by_type": {k: round(v, 3) for k, v in by_type.items()},
        })
    return out


def series(d_from: Optional[str] = None, d_to: Optional[str] = None) -> list[dict]:
    return series_calc(_load_rows(d_from, d_to))


def stats_calc(rows: list[dict]) -> dict:
    """Итоги периода: объёмы, покрытие, доли несостоявшихся/ДРПА, разрез по
    типам и стандартным корзинам, топ-5 по размещению, средняя премия."""
    rows = [dict(r) for r in rows]      # уже обогащённые (results) или сырые
    if rows and "proceeds_113_mln" not in rows[0]:
        rows = enrich(rows)
    auctions = [r for r in rows if r["fmt"] == "auction"]
    placed = sum((r.get("placed_mln") or 0) for r in rows)
    p113 = sum((r.get("proceeds_113_mln") or 0) for r in rows)
    demand = sum((r.get("demand_mln") or 0) for r in auctions)
    drpa = sum((r.get("placed_mln") or 0) for r in rows if r["fmt"] == "drpa")
    covers = [r["bid_cover"] for r in auctions if r.get("bid_cover")]
    prem = [r["premium_bps"] for r in rows if r.get("premium_bps") is not None]

    def share(sel: list[dict]) -> dict:
        v = sum((r.get("placed_mln") or 0) for r in sel)
        return {"placed_mln": round(v, 3), "share": round(v / placed, 4) if placed else None,
                "n": len([r for r in sel if r["fmt"] == "auction"])}

    by_type = {t: share([r for r in rows if r.get("sec_type") == t])
               for t in sorted({r.get("sec_type") or "?" for r in rows})}
    by_bucket = {}
    for label, lo, hi in STD_BUCKETS:
        sel = [r for r in rows if r.get("term_y") is not None
               and (lo is None or r["term_y"] > lo) and (hi is None or r["term_y"] <= hi)]
        by_bucket[label] = share(sel)
    pd_rows = [r for r in rows if r.get("sec_type") == "ОФЗ-ПД" and r.get("wap_yield") is not None
               and (r.get("placed_mln") or 0) > 0]
    w = sum(r["placed_mln"] for r in pd_rows)
    top = sorted(auctions, key=lambda r: r.get("placed_mln") or 0, reverse=True)[:5]
    return {
        "n_rows": len(rows), "n_auctions": len(auctions),
        "n_dates": len({r["date"] for r in rows}),
        "n_failed": len([r for r in auctions if r["status"] == "failed"]),
        "failed_share": round(len([r for r in auctions if r["status"] == "failed"]) / len(auctions), 4) if auctions else None,
        "n_drpa": len([r for r in rows if r["fmt"] == "drpa"]),
        "drpa_share": round(drpa / placed, 4) if placed else None,
        "placed_mln": round(placed, 3), "proceeds_113_mln": round(p113, 3),
        "revenue_mln": round(sum((r.get("revenue_mln") or 0) for r in rows), 3),
        "demand_mln": round(demand, 3),
        "offered_mln": round(sum((r.get("offered_mln") or 0) for r in auctions), 3),
        "bid_cover_median": round(median(covers), 2) if covers else None,
        "bid_cover_total": round(demand / sum((r.get("placed_mln") or 0) for r in auctions), 2)
        if auctions and sum((r.get("placed_mln") or 0) for r in auctions) else None,
        "wap_yield_w": round(sum(r["wap_yield"] * r["placed_mln"] for r in pd_rows) / w, 3) if w else None,
        "premium_avg_bps": round(sum(prem) / len(prem), 1) if prem else None,
        "premium_n": len(prem),
        "by_type": by_type, "by_bucket": by_bucket,
        "top": [{k: r.get(k) for k in ("date", "code", "secid", "isin", "shortname", "sec_type",
                                       "placed_mln", "wap_yield", "wap_price", "bid_cover",
                                       "proceeds_113_mln")} for r in top],
    }


def stats(d_from: Optional[str] = None, d_to: Optional[str] = None) -> dict:
    return stats_calc(results(d_from, d_to))


def by_issue() -> list[dict]:
    """Сводка по выпуску: сколько раз выходил, размещено всего, последний
    аукцион, средняя (взвешенная) wap-доходность, диапазон цен."""
    with _connect() as c:
        rows = [dict(r) for r in c.execute("""
            SELECT a.code, MAX(a.secid) secid, MAX(a.isin) isin, MAX(s.shortname) shortname,
                   MAX(a.sec_type) sec_type, MAX(a.maturity) maturity,
                   COUNT(*) n_rows,
                   SUM(CASE WHEN a.fmt='auction' THEN 1 ELSE 0 END) n,
                   SUM(CASE WHEN a.status='failed' THEN 1 ELSE 0 END) n_failed,
                   SUM(CASE WHEN a.fmt='drpa' THEN 1 ELSE 0 END) n_drpa,
                   MIN(a.date) first_date, MAX(a.date) last_date,
                   SUM(COALESCE(a.placed_mln,0)) placed_mln,
                   SUM(COALESCE(a.demand_mln,0)) demand_mln,
                   SUM(COALESCE(a.placed_mln,0) * MIN(COALESCE(a.wap_price,0),100)/100.0) proceeds_113_mln,
                   SUM(CASE WHEN a.sec_type='ОФЗ-ПД' AND a.wap_yield IS NOT NULL
                            THEN a.wap_yield*COALESCE(a.placed_mln,0) END) /
                   NULLIF(SUM(CASE WHEN a.sec_type='ОФЗ-ПД' AND a.wap_yield IS NOT NULL
                                   THEN COALESCE(a.placed_mln,0) END),0) wap_yield_w,
                   MIN(a.wap_price) price_min, MAX(a.wap_price) price_max,
                   MIN(a.wap_yield) yield_min, MAX(a.wap_yield) yield_max
            FROM ofz_auction a LEFT JOIN sec_ref s ON s.secid = a.secid
            GROUP BY a.code ORDER BY last_date DESC, placed_mln DESC""")]
        last = {(r["code"], r["date"]): dict(r) for r in c.execute(
            "SELECT code, date, wap_yield, wap_price, placed_mln FROM ofz_auction WHERE fmt='auction'")}
    for r in rows:
        l = last.get((r["code"], r["last_date"])) or {}
        r["last_wap_yield"] = l.get("wap_yield")
        r["last_wap_price"] = l.get("wap_price")
        r["last_placed_mln"] = l.get("placed_mln")
        for k in ("placed_mln", "demand_mln", "proceeds_113_mln"):
            r[k] = round(r[k] or 0, 3)
        r["wap_yield_w"] = round(r["wap_yield_w"], 3) if r["wap_yield_w"] is not None else None
    return rows


def sync_state() -> dict:
    with _connect() as c:
        rows = {r["key"]: {"at": r["at"], "note": r["note"]} for r in c.execute(
            "SELECT key, at, note FROM ofz_auction_sync")}
        n = c.execute("SELECT COUNT(*) n, MIN(date) d0, MAX(date) d1 FROM ofz_auction").fetchone()
    return {"files": rows, "rows": n["n"], "from": n["d0"], "to": n["d1"]}
