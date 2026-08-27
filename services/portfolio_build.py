"""Конструктор портфеля флоатеров: по фильтру и сумме собирает набор из n бумаг.

Отбор бумаг НЕ дублируется — рейтинг/эмитент/ISIN/ОФЗ/срок/суборд считает
`screener_core.static_candidates` (тот же код, что у вкладки СИГНАЛЫ и бота).
Здесь живёт только то, чего в скринере нет: портфельные фильтры (база купона,
амортизация, колл, оборот), жадный набор с кэпами диверсификации, раздача денег
по стакану и агрегаты набора.

Ценообразование позиции (VWAP по лестнице → Y-IDX к этой цене) повторяет
`screener_core.evaluate_candidates` поверх его же публичных хелперов —
осознанный дубль: скринеру бумага с недостаточной глубиной не нужна вовсе
(`vwap_passes`), а портфелю годится, просто меньшей позицией. Расхождение цифр
ловит tests/test_portfolio_build.py::test_no_pricing_drift.

Модуль в сеть не ходит: всё, что нужно, подаёт `build_live`.
"""
import logging
import math
from datetime import date
from typing import Dict, List, Optional

from services import screener_core as sc
from services.screener_core import FilterError

logger = logging.getLogger(__name__)

BASES = ("KEYRATE", "RUONIA")
MODES = ("spread", "ladder")
RATING_SCALE = {"AAA": 1, "AA": 2, "A": 3, "BBB": 4, "BB": 5, "B": 6}
POOL_SIZE = 30              # кандидатов «на замену» отдаём наружу
CALENDAR_MONTHS = 24
MAX_N = 100

REASON_TXT = {
    "no_metrics": "нет метрик", "stale": "цена несвежая",
    "implausible": "цена неправдоподобна", "thin": "тонкая цена",
    "no_ask": "нет оффера", "no_depth": "пустой стакан",
    "spread_out": "вне диапазона спреда", "base": "другая база",
    "amort": "с амортизацией", "call": "с коллом/офертой",
    "adv": "мало оборота", "excluded": "убрана вручную",
    "zero_lot": "меньше одной бумаги", "not_in_universe": "нет в универсе флоатеров",
    "capped_out": "не прошла отбор набора",
}


def _rej(isin, name, reason: str) -> dict:
    return {"isin": isin, "name": name or isin, "reason": reason,
            "reason_txt": REASON_TXT.get(reason, reason)}

PARAM_DEFAULTS = {
    # отбор (общий со скринером)
    "ratings": [], "emitters": [], "isins": [],
    "issuer": "all", "years_min": None, "years_max": None, "hide_subord": True,
    "spread_min": None, "spread_max": None,
    # отбор (портфельный)
    "bases": [], "no_amort": False, "no_call": False, "min_adv_rub": None,
    # сборка
    "mode": "spread", "buckets": [1.0, 2.0, 3.0],
    "n": 15, "amount_rub": 100_000_000.0,
    "max_per_emitter": 1, "max_emitter_share": None, "max_rating_share": None,
    # ручная правка (состояние живёт в UI)
    "exclude": [], "pin": [], "manual": {},
}


# ───────────────────────────── валидация ─────────────────────────────

def _str_list(raw, field: str, upper: bool = False) -> list:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise FilterError(f"{field}: ожидался список")
    out = []
    for v in raw:
        v = str(v or "").strip()
        if v:
            out.append(v.upper() if upper else v)
    return out


def _num(raw, key: str, p: dict) -> None:
    v = raw.get(key)
    if v is None or v == "":
        return
    try:
        p[key] = float(v)
    except (TypeError, ValueError):
        raise FilterError(f"{key}: должно быть числом")


def _share(p: dict, key: str) -> None:
    v = p.get(key)
    if v is None:
        return
    if not (0 < v <= 1):
        raise FilterError(f"{key}: доля в пределах (0; 1]")


def normalize(raw: dict) -> dict:
    raw = raw or {}
    p = dict(PARAM_DEFAULTS)
    p["ratings"] = _str_list(raw.get("ratings"), "ratings", upper=True)
    p["emitters"] = _str_list(raw.get("emitters"), "emitters")
    p["isins"] = _str_list(raw.get("isins"), "isins", upper=True)
    p["exclude"] = _str_list(raw.get("exclude"), "exclude", upper=True)
    p["pin"] = _str_list(raw.get("pin"), "pin", upper=True)
    p["bases"] = _str_list(raw.get("bases"), "bases", upper=True)
    for r in p["ratings"]:
        if r not in sc.RATINGS:
            raise FilterError(f"rating: {' '.join(sc.RATINGS)}")
    for b in p["bases"]:
        if b not in BASES:
            raise FilterError(f"base: {' '.join(BASES)}")
    iss = (raw.get("issuer") or "all").strip().lower()
    if iss not in sc.ISSUERS:
        raise FilterError("issuer: " + " | ".join(sc.ISSUERS))
    p["issuer"] = iss
    p["hide_subord"] = (True if raw.get("hide_subord") is None
                        else bool(raw.get("hide_subord")))
    p["no_amort"] = bool(raw.get("no_amort"))
    p["no_call"] = bool(raw.get("no_call"))
    for k in ("years_min", "years_max", "spread_min", "spread_max",
              "min_adv_rub", "amount_rub", "max_emitter_share",
              "max_rating_share"):
        _num(raw, k, p)
    for k in ("years_min", "years_max"):
        if p[k] is not None and p[k] < 0:
            raise FilterError("Срок до погашения: неотрицательное число")
    if (p["years_min"] is not None and p["years_max"] is not None
            and p["years_min"] > p["years_max"]):
        raise FilterError("Срок до погашения: «от» больше «до»")
    if (p["spread_min"] is not None and p["spread_max"] is not None
            and p["spread_min"] > p["spread_max"]):
        raise FilterError("Диапазон спреда: «от» больше «до»")
    _share(p, "max_emitter_share")
    _share(p, "max_rating_share")

    mode = (raw.get("mode") or "spread").strip().lower()
    if mode not in MODES:
        raise FilterError("mode: " + " | ".join(MODES))
    p["mode"] = mode
    if raw.get("buckets") is not None:
        edges = []
        for v in raw["buckets"]:
            try:
                edges.append(float(v))
            except (TypeError, ValueError):
                raise FilterError("buckets: числа (границы корзин, лет)")
        if any(b <= 0 for b in edges) or edges != sorted(edges):
            raise FilterError("buckets: положительные границы по возрастанию")
        p["buckets"] = edges

    n_raw = raw.get("n")
    try:
        p["n"] = PARAM_DEFAULTS["n"] if n_raw in (None, "") else int(n_raw)
    except (TypeError, ValueError):
        raise FilterError("n: целое число бумаг")
    if not (1 <= p["n"] <= MAX_N):
        raise FilterError(f"n: от 1 до {MAX_N} бумаг")
    if p["amount_rub"] <= 0:
        raise FilterError("amount_rub: положительное число")
    try:
        p["max_per_emitter"] = int(raw.get("max_per_emitter")
                                   if raw.get("max_per_emitter") is not None
                                   else PARAM_DEFAULTS["max_per_emitter"])
    except (TypeError, ValueError):
        raise FilterError("max_per_emitter: целое число")
    if p["max_per_emitter"] < 1:
        raise FilterError("max_per_emitter: не меньше 1")

    manual = raw.get("manual") or {}
    if not isinstance(manual, dict):
        raise FilterError("manual: словарь ISIN → рубли")
    out_manual = {}
    for k, v in manual.items():
        isin = str(k or "").strip().upper()
        if not isin:
            continue
        try:
            rub = float(v)
        except (TypeError, ValueError):
            raise FilterError(f"manual[{isin}]: должно быть числом")
        if rub <= 0:
            raise FilterError(f"manual[{isin}]: положительная сумма")
        out_manual[isin] = rub
    if sum(out_manual.values()) > p["amount_rub"]:
        raise FilterError("Ручные суммы больше суммы портфеля")
    if len(out_manual) > p["n"]:
        raise FilterError("Ручных позиций больше, чем бумаг в портфеле")
    p["manual"] = out_manual
    return p


# ─────────────────────────── отбор и цена ────────────────────────────

def _extra_reason(u: dict, row: dict, p: dict, adv: dict) -> Optional[str]:
    """Портфельные фильтры — то, чего нет в скринерном static_candidates."""
    if p["bases"] and (u.get("base_rate_type") or "").upper() not in p["bases"]:
        return "base"
    if p["no_amort"] and row.get("has_amort"):
        return "amort"
    if p["no_call"] and (u.get("has_call") or row.get("offer_date")):
        return "call"
    if p["min_adv_rub"] is not None:
        # бумаги без записей в архиве баров считаем неликвидом: обещать оборот,
        # которого мы не видели, хуже, чем не показать бумагу
        if (adv.get(u.get("isin")) or 0.0) < p["min_adv_rub"]:
            return "adv"
    return None


def _bad_price(row: dict) -> Optional[str]:
    if row.get("implausible"):
        return "implausible"
    if row.get("price_stale"):
        return "stale"
    if row.get("price_thin"):
        return "thin"
    if not (row.get("ask") or 0) > 0:
        return "no_ask"
    return None


def price_position(isin: str, row: dict, ladder, want: float) -> Optional[dict]:
    """Цена набора want рублей по лестнице оффера и Y-IDX к ней.
    None — глубины нет вовсе. partial=True — книги не хватило (в отличие от
    скринера, бумагу не выбрасываем: ограничим позицию доступным).

    Y-IDX — только по методике: наклон от якоря убран 27.08.2026, он уводил
    число вслед за уехавшим якорем. Не посчиталось — позиция идёт без спреда,
    а не с выдуманным."""
    face = row.get("face_px") or 1000.0
    accrued = row.get("accrued_settle") or 0.0
    v = sc.vwap_for(ladder, want, face, accrued)
    if not v:
        return None
    y = sc.exact_y_idx(isin, v["px"])
    if y is None and v["levels"] == 1:
        # набор уложился в один уровень — Y-IDX верха стакана точен для него
        # (он сам посчитан по методике движком метрик)
        y = row.get("yoi_ask")
    return {"price": round(v["px"], 4), "y_idx_bps": round(float(y), 1) if y is not None else None,
            "money_avail": v["money"], "levels": v["levels"], "capped": bool(v["partial"]),
            "face_current": face, "accrued": accrued}


def _priced(cands: List[dict], metrics: dict, depth_map: dict, adv: dict,
            p: dict, ticket: float) -> tuple:
    """(строки-кандидаты, отсев). Цена считается на РАЗМЕР ТИКЕТА, а не по верху
    стакана: покупаться будет вся лестница."""
    lo, hi = p["spread_min"], p["spread_max"]
    manual, pin = p["manual"], set(p["pin"])
    rows, rejected = [], []

    def drop(u, reason):
        rejected.append(_rej(u.get("isin"), u.get("name"), reason))

    for u in cands:
        isin = u.get("isin")
        forced = isin in pin or isin in manual
        row = metrics.get(isin)
        if not row:
            drop(u, "no_metrics")
            continue
        reason = _extra_reason(u, row, p, adv) if not forced else None
        if reason:
            drop(u, reason)
            continue
        bad = _bad_price(row)
        if bad:
            drop(u, bad)
            continue
        want = max(ticket, manual.get(isin) or 0.0)
        ladder = (depth_map.get(isin) or {}).get("a")
        pr = price_position(isin, row, ladder, want)
        if pr is None:
            drop(u, "no_depth")
            continue
        y = pr["y_idx_bps"]
        if (lo is not None or hi is not None) and not forced:
            if y is None or (lo is not None and y < lo) or (hi is not None and y > hi):
                drop(u, "spread_out")
                continue
        rows.append({
            "isin": isin, "name": u.get("name") or isin,
            "emitter": u.get("emitter_name"), "rating": u.get("rating"),
            "base": u.get("base_rate_type"), "margin_bps": u.get("spread_issue_bps"),
            "cpy": u.get("coupons_per_year"), "years": u.get("_years"),
            "maturity_date": u.get("maturity_date"),
            "spread_dur": row.get("spread_dur"),
            "current_coupon_pct": row.get("current_coupon"),
            "adv_rub": adv.get(isin),
            "pinned": isin in pin, "manual": isin in manual,
            # ёмкость ВСЕЙ стороны: money_avail из price_position равен
            # запрошенному тикету, по нему перераздача остатка невозможна
            "money_side": sc.side_money_rub(depth_map.get(isin), "ask",
                                            pr["face_current"], pr["accrued"]) or 0.0,
            "_ladder": ladder, "_row": row,
            **pr,
        })
    rows.sort(key=lambda r: (r["y_idx_bps"] is not None, r["y_idx_bps"] or 0), reverse=True)
    return rows, rejected


# ──────────────────────────── набор бумаг ────────────────────────────

def _bucket(years: Optional[float], edges: List[float]) -> Optional[int]:
    if years is None:
        return None
    for i, e in enumerate(edges):
        if years < e:
            return i
    return len(edges)


def bucket_label(i: int, edges: List[float]) -> str:
    fmt = lambda v: (f"{v:g}")
    if i == 0:
        return f"до {fmt(edges[0])} л"
    if i == len(edges):
        return f"от {fmt(edges[-1])} л"
    return f"{fmt(edges[i - 1])}–{fmt(edges[i])} л"


class _Caps:
    """Счётчики диверсификации. Деньги считаются по ПЛАНОВОМУ тикету: реальный
    размер позиции узнаётся только после раздачи по стакану, а решение «брать
    или нет» принимается здесь."""

    def __init__(self, p: dict, ticket: float):
        self.per_emitter = p["max_per_emitter"]
        self.em_share, self.rt_share = p["max_emitter_share"], p["max_rating_share"]
        self.amount, self.ticket = p["amount_rub"], ticket
        self.manual = p["manual"]
        self.cnt: Dict[str, int] = {}
        self.em_money: Dict[str, float] = {}
        self.rt_money: Dict[str, float] = {}

    def _plan(self, r: dict) -> float:
        return self.manual.get(r["isin"]) or self.ticket

    def ok(self, r: dict) -> bool:
        em = r.get("emitter") or r["isin"]
        if self.cnt.get(em, 0) >= self.per_emitter:
            return False
        plan = self._plan(r)
        if self.em_share is not None:
            if (self.em_money.get(em, 0.0) + plan) / self.amount > self.em_share + 1e-9:
                return False
        if self.rt_share is not None:
            rt = (r.get("rating") or "—").upper()
            if (self.rt_money.get(rt, 0.0) + plan) / self.amount > self.rt_share + 1e-9:
                return False
        return True

    def take(self, r: dict) -> None:
        em = r.get("emitter") or r["isin"]
        rt = (r.get("rating") or "—").upper()
        plan = self._plan(r)
        self.cnt[em] = self.cnt.get(em, 0) + 1
        self.em_money[em] = self.em_money.get(em, 0.0) + plan
        self.rt_money[rt] = self.rt_money.get(rt, 0.0) + plan


def _pick(rows: List[dict], p: dict, ticket: float) -> tuple:
    """(взятые, предупреждения). Кэпы диверсификации не ослабляются молча:
    не набралось n — так и сообщаем."""
    caps, warns = _Caps(p, ticket), []
    n, taken, seen = p["n"], [], set()

    def grab(r):
        taken.append(r)
        seen.add(r["isin"])
        caps.take(r)

    # ручные и прикнопленные — вне очереди и вне кэпов: человек уже решил
    for r in rows:
        if len(taken) >= n:
            break
        if r["manual"] or r["pinned"]:
            grab(r)
    rest = [r for r in rows if r["isin"] not in seen]

    if p["mode"] == "ladder":
        edges = p["buckets"]
        nb = len(edges) + 1
        by_bucket: Dict[int, List[dict]] = {i: [] for i in range(nb)}
        for r in rest:
            b = _bucket(r.get("years"), edges)
            if b is not None:
                by_bucket[b].append(r)
        left = max(0, n - len(taken))
        base_q, extra = divmod(left, nb)
        # остаток квоты — корзинам с бо́льшим числом кандидатов
        order = sorted(range(nb), key=lambda i: -len(by_bucket[i]))
        quota = {i: base_q + (1 if k < extra else 0) for k, i in enumerate(order)}
        short = []
        for i in range(nb):
            got = 0
            for r in by_bucket[i]:
                if got >= quota[i]:
                    break
                if caps.ok(r):
                    grab(r)
                    got += 1
            if got < quota[i]:
                short.append(f"{bucket_label(i, edges)} ({got} из {quota[i]})")
        if short:
            warns.append("Корзины недобраны, квота ушла соседям: " + ", ".join(short))
        rest = [r for r in rest if r["isin"] not in seen]

    for r in rest:
        if len(taken) >= n:
            break
        if caps.ok(r):
            grab(r)

    if len(taken) < n:
        warns.append(f"Набралось {len(taken)} из {n}: кандидатов под фильтр и "
                     f"ограничения диверсификации не хватило")
    return taken, warns


# ────────────────────────── деньги и лоты ────────────────────────────

def _unit_rub(r: dict) -> float:
    """Стоимость ОДНОЙ бумаги: остаточный номинал по цене набора + НКД."""
    return r["face_current"] * r["price"] / 100.0 + (r["accrued"] or 0.0)


def _size(taken: List[dict], p: dict) -> tuple:
    """Раздача денег: ручные суммы фиксируются, остальное поровну с урезкой по
    стакану и ОДНИМ проходом перераздачи. Итераций до сходимости нет намеренно —
    недобор честнее показать, чем размазать."""
    manual, warns, rejected = p["manual"], [], []
    auto = [r for r in taken if r["isin"] not in manual]
    fixed = [r for r in taken if r["isin"] in manual]
    give: Dict[str, float] = {}

    for r in fixed:
        want = manual[r["isin"]]
        give[r["isin"]] = want
        r["capped"] = False          # ручную сумму не режем, режим другой
        if want > (r["money_avail"] or 0) + 1e-6:
            r["price_estimated"] = True
            warns.append(f"{r['name']}: ручная сумма больше стакана — цена оценочная "
                         f"(книга целиком {(r['money_avail'] or 0) / 1e6:.1f} млн)")

    rest_amount = p["amount_rub"] - sum(manual.values())
    if auto and rest_amount > 0:
        target = rest_amount / len(auto)
        for r in auto:
            give[r["isin"]] = min(target, r["money_avail"] or 0.0)
        leftover = rest_amount - sum(give[r["isin"]] for r in auto)
        if leftover > 1e-6:
            # запас считается по ВСЕЙ стороне книги, а не по набору на тикет:
            # money_avail равен запрошенному, и по нему запаса не видно вовсе
            room = [(r, (r["money_side"] or 0.0) - give[r["isin"]]) for r in auto]
            room = [(r, x) for r, x in room if x > 1e-6]
            total_room = sum(x for _, x in room)
            if total_room > 0:
                for r, x in room:
                    give[r["isin"]] += min(x, leftover * x / total_room)
        # добавка съедает уровни глубже набранных — цена и Y-IDX обязаны
        # пересчитаться на фактический размер, иначе в таблице стоял бы спред
        # верха книги при покупке половины стакана
        for r in auto:
            g = give[r["isin"]]
            if g > (r["money_avail"] or 0.0) + 1.0:
                pr = price_position(r["isin"], r["_row"], r["_ladder"], g)
                if pr:
                    r.update(pr)
                    give[r["isin"]] = min(g, pr["money_avail"])
            r["capped"] = give[r["isin"]] < target - 1e-6

    positions = []
    for r in taken:
        money = give.get(r["isin"], 0.0)
        unit = _unit_rub(r)
        qty = int(math.floor(money / unit)) if unit > 0 else 0
        if qty <= 0:
            rejected.append(_rej(r["isin"], r["name"], "zero_lot"))
            continue
        r = {k: v for k, v in r.items() if not k.startswith("_")}
        r["qty"] = qty
        r["money_rub"] = round(qty * unit, 2)
        r.setdefault("price_estimated", False)
        r["adv_days"] = (round(r["money_rub"] / r["adv_rub"], 2)
                         if r.get("adv_rub") else None)
        positions.append(r)

    capped = sum(1 for r in positions if r.get("capped"))
    if capped:
        warns.append(f"Позиций урезано стаканом: {capped}")
    return positions, warns, rejected


# ──────────────────────────── агрегаты ───────────────────────────────

def _r(v: Optional[float], d: int) -> Optional[float]:
    """round, переживающий None: метрика может быть не посчитана ни у одной
    бумаги набора (пустой spread_dur, неизвестный Y-IDX) — тогда наружу None,
    а не падение расчёта всего портфеля."""
    return None if v is None else round(v, d)


def _w(positions: List[dict], key: str) -> Optional[float]:
    tot = sum(r["money_rub"] for r in positions)
    vals = [(r["money_rub"], r.get(key)) for r in positions if r.get(key) is not None]
    if not tot or not vals:
        return None
    base = sum(m for m, _ in vals)
    return sum(m * v for m, v in vals) / base if base else None


def _shares(positions: List[dict], key: str) -> Dict[str, float]:
    tot = sum(r["money_rub"] for r in positions)
    out: Dict[str, float] = {}
    for r in positions:
        k = str(r.get(key) or "—")
        out[k] = out.get(k, 0.0) + r["money_rub"]
    return {k: round(v / tot, 4) for k, v in sorted(out.items(), key=lambda x: -x[1])} if tot else {}


def _rating_avg(positions: List[dict]) -> Optional[str]:
    """Среднее по денежным весам на буквенной шкале. Это индикатор СОСТАВА, а не
    кредитная оценка: шкала линейная, вероятности дефолта — нет."""
    pts = [(r["money_rub"], RATING_SCALE.get((r.get("rating") or "").upper()))
           for r in positions]
    pts = [(m, v) for m, v in pts if v]
    if not pts:
        return None
    avg = sum(m * v for m, v in pts) / sum(m for m, _ in pts)
    near = min(RATING_SCALE.items(), key=lambda kv: abs(kv[1] - avg))
    return near[0]


def _horizon_month(today: date, months: int = 12) -> str:
    m = today.month - 1 + months
    return f"{today.year + m // 12:04d}-{m % 12 + 1:02d}"


def _totals(positions: List[dict], p: dict, calendar_rows: List[dict],
            today: date) -> dict:
    money = round(sum(r["money_rub"] for r in positions), 2)
    dur_pnl = sum(r["money_rub"] * (r.get("spread_dur") or 0.0) for r in positions)
    em_share = _shares(positions, "emitter")
    horizon = _horizon_month(today)
    coupon_12m = round(sum(x["coupon_rub"] for x in calendar_rows
                           if x["month"] < horizon), 2)
    return {
        "money_rub": money,
        "shortfall_rub": round(p["amount_rub"] - money, 2),
        "count": len(positions),
        "emitters": len({r.get("emitter") or r["isin"] for r in positions}),
        "y_idx_w": _r(_w(positions, "y_idx_bps"), 1),
        "dur_w": _r(_w(positions, "spread_dur"), 2),
        "current_coupon_w": _r(_w(positions, "current_coupon_pct"), 3),
        "rating_avg": _rating_avg(positions),
        "pnl_100bp_rub": round(dur_pnl / 100.0, 2),
        "coupon_12m_rub": coupon_12m,
        "by_base": _shares(positions, "base"),
        "by_rating": _shares(positions, "rating"),
        "by_bucket": _shares(positions, "bucket"),
        "top_emitters": [{"emitter": k, "share": v} for k, v in list(em_share.items())[:5]],
        "hhi_emitter": round(sum(v * v for v in em_share.values()), 4),
    }


def _ev_date(v) -> Optional[date]:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _calendar(positions: List[dict], events: List[dict], today: date) -> List[dict]:
    """Выплаты набора по месяцам. Суммы событий — на ОДНУ бумагу (канонический
    билдер payments_calendar), умножаем на количество. Своей математики потоков
    здесь нет намеренно."""
    qty = {r["isin"]: r["qty"] for r in positions}
    by_month: Dict[str, dict] = {}
    for e in events or []:
        q = qty.get(e.get("isin"))
        if not q:
            continue
        d = _ev_date(e.get("date"))
        if not d or d <= today:
            continue
        amount = (e.get("amount_rub") or 0.0) * q
        m = d.strftime("%Y-%m")
        row = by_month.setdefault(m, {"month": m, "coupon_rub": 0.0,
                                      "redemption_rub": 0.0, "total_rub": 0.0})
        key = "coupon_rub" if e.get("type") == "COUPON" else "redemption_rub"
        row[key] += amount
        row["total_rub"] += amount
    out = [ {k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()}
            for row in sorted(by_month.values(), key=lambda r: r["month"]) ]
    return out[:CALENDAR_MONTHS]


# ───────────────────────────── сборка ────────────────────────────────

def build(params: dict, uni: List[dict], metrics: dict, depth_map: dict,
          adv: Optional[dict] = None, events: Optional[List[dict]] = None,
          today: Optional[date] = None) -> dict:
    """Чистая сборка без сети: рынок и справочники подаёт вызывающий."""
    today = today or date.today()
    adv = adv or {}
    p = params
    ticket = p["amount_rub"] / p["n"]

    cands = sc.static_candidates(p, uni, today, metrics)
    rejected_pre: List[dict] = []
    forced = set(p["pin"]) | set(p["manual"])
    have = {u.get("isin") for u in cands}
    if forced - have:
        # прикнопленная бумага могла не пройти фильтр — берём её из универса
        by_isin = {u.get("isin"): u for u in uni}
        for isin in forced - have:
            u = by_isin.get(isin)
            if not u:
                rejected_pre.append(_rej(isin, isin, "not_in_universe"))
                continue
            yrs = sc.horizon_years(u, metrics.get(isin), today)   # та же методика срока, что в отборе
            cands.append(dict(u, _years=round(yrs, 2) if yrs is not None else None))

    excluded = set(p["exclude"])
    rejected = [_rej(u.get("isin"), u.get("name"), "excluded")
                for u in cands if u.get("isin") in excluded]
    cands = [u for u in cands if u.get("isin") not in excluded]

    rows, rej_price = _priced(cands, metrics, depth_map, adv, p, ticket)
    rejected += rejected_pre + rej_price

    taken, warns = _pick(rows, p, ticket)
    positions, warns_size, rej_lot = _size(taken, p)
    rejected += rej_lot
    warns += warns_size

    edges = p["buckets"]
    for r in positions:
        b = _bucket(r.get("years"), edges)
        r["bucket"] = bucket_label(b, edges) if b is not None else None
    tot_money = sum(r["money_rub"] for r in positions) or 1.0
    for r in positions:
        r["weight_pct"] = round(100.0 * r["money_rub"] / tot_money, 2)

    lost = forced - {r["isin"] for r in positions}
    if lost:
        why = {x["isin"]: x["reason_txt"] for x in rejected}
        warns.append("Заданы вручную, но в набор не попали: " + ", ".join(
            sorted(f"{i} ({why.get(i, REASON_TXT['capped_out'])})" for i in lost)))

    cal = _calendar(positions, events, today)
    totals = _totals(positions, p, cal, today)
    if totals["shortfall_rub"] > 0.01:
        warns.append(f"Недобор суммы: {totals['shortfall_rub'] / 1e6:.2f} млн")

    picked = {r["isin"] for r in taken}
    pool = [{k: v for k, v in r.items() if not k.startswith("_")}
            for r in rows if r["isin"] not in picked][:POOL_SIZE]
    return {"positions": positions, "pool": pool, "totals": totals,
            "calendar": cal, "rejected": rejected, "warnings": warns,
            "calc_date": today.isoformat()}


async def build_live(raw: dict) -> dict:
    """Всё сетевое и кэшируемое — здесь; счёт — в build (его же зовут тесты)."""
    import asyncio
    from services import bars, payments_calendar

    p = normalize(raw)
    uni, metrics, depth_map = await sc.market_snapshot()
    if not metrics:
        raise FilterError("Движок метрик ещё не прогрелся — попробуй через минуту")
    adv, cal = await asyncio.gather(
        asyncio.to_thread(bars.adv_map, 30),
        payments_calendar.build_payments_calendar(),
        return_exceptions=True)
    if isinstance(adv, BaseException):
        logger.warning("portfolio: adv_map недоступен: %s", adv)
        adv = {}
    events, calc_date = [], None
    if isinstance(cal, BaseException):
        logger.warning("portfolio: календарь недоступен: %s", cal)
    else:
        events = cal.get("events") or []
        calc_date = cal.get("calc_date")
    out = build(p, uni, metrics, depth_map, adv, events)
    if not adv and p["min_adv_rub"] is not None:
        out["warnings"].append("Оборот недоступен — фильтр по обороту не применён")
    if not events:
        out["warnings"].append("Календарь выплат недоступен — купонный доход не посчитан")
    out["calendar_date"] = calc_date
    return out
