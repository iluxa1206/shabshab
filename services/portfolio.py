"""Агрегация фонда: позиции снапшота → рыночная стоимость, метрики, разрезы, РЕПО-нога.

Позиция оценивается в валюте номинала и конвертируется в рубли по курсу ЦБ:
  MV = qty × (face × price/100 + НКД) × fx

Метрики по классам:
  corp_float          — DM/Z/carry из фонового universe_metrics (кэш поллера)
  corp_fix / ofz_pd   — YTM/дюрация/DV01/G-spread из fixed_income (bondization)
  fx_*                — YTM/дюрация/DV01 в валюте номинала (без G-spread: КБД рублёвая)
  ofz_pk              — MV + текущий купон (DM-модель ОФЗ-ПК — фаза 2)

Фондирование: явные РЕПО-сделки. NAV = Σ MV − Σ РЕПО. Капитал = funds.capital
(если задан) иначе NAV. Экспозиция класса выражается в «× капитала».
Carry (годовой, приближение Ф1): купонный доход активов − стоимость РЕПО.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Dict, List, Optional

from services import portfolio_db as db
from services import instruments
from services.fx import get_fx
from services.fixed_income import fixed_metrics_from_schedule
from services.market_data import MarketDataService

# классы, для которых считаем фикс-метрики (известные кэшфлоу)
_FIXED_CLASSES = {"corp_fix", "ofz_pd", "fx_usd", "fx_eur", "fx_cny"}
_RUB_FIXED = {"corp_fix", "ofz_pd"}


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def _position_rows(positions: List[dict], info: Dict[str, dict],
                         fx: Dict[str, float], calc_date: date) -> List[dict]:
    """Оценка каждой позиции: цена, MV, метрики класса."""
    uni_m = MarketDataService.universe_metrics()
    live = MarketDataService.cached_prices()
    g_curve = await MarketDataService.get_gcurve()

    # bondization разом для фикс-классов (кэш на день внутри).
    # ОФЗ: endpoint резолвит только SECID (SU…), не ISIN — берём secid из справочника.
    fixed_isins = [p["isin"] for p in positions if info[p["isin"]]["cls"] in _FIXED_CLASSES]
    schedules = await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(
            info[i]["sec"].get("secid") or i) for i in fixed_isins))
    sched_by = dict(zip(fixed_isins, schedules))

    rows: List[dict] = []
    for p in positions:
        isin, qty = p["isin"], float(p["qty"])
        meta = info[isin]
        cls, ccy, sec = meta["cls"], meta["ccy"], meta["sec"]
        um = uni_m.get(isin) or {}
        fx_rate = fx.get(ccy, 1.0)

        face = _f(sec.get("face")) or 1000.0
        accrued = _f(sec.get("accrued")) or 0.0
        # цена: live Alor → метрики юниверса → prev MOEX
        price = _f(live.get(isin))
        if price is None:
            price = _f(um.get("last"))
        if price is None:
            price = _f(sec.get("prev"))

        row = {
            "isin": isin, "name": meta["name"], "cls": cls, "label": meta["label"],
            "ccy": ccy, "qty": qty, "price_pct": price, "face": face,
            "accrued": accrued, "fx": fx_rate, "maturity": sec.get("maturity"),
            "mv_rub": None, "ytm_pct": None, "dm_bps": None, "z_bps": None,
            "mod_dur": None, "dv01_rub": None, "g_spread_bps": None,
            "current_coupon_pct": None, "carry_bps": None, "warn": None,
        }

        if cls in _FIXED_CLASSES:
            fm = fixed_metrics_from_schedule(
                sched_by.get(isin) or {}, price if price is not None else 0.0,
                accrued, calc_date, g_curve=g_curve if cls in _RUB_FIXED else None)
            if fm.get("face_current"):
                face = fm["face_current"]  # остаточный номинал (амортизация)
                row["face"] = face
            row["ytm_pct"] = fm.get("ytm_pct")
            row["mod_dur"] = fm.get("mod_dur")
            row["g_spread_bps"] = fm.get("g_spread_bps")
            if fm.get("dv01") is not None:
                row["dv01_rub"] = round(fm["dv01"] * qty * fx_rate, 2)
            if fm.get("put_date"):
                row["warn"] = f"оценка к оферте {fm['put_date']}"
        elif cls == "corp_float":
            row["dm_bps"] = um.get("dm")
            row["z_bps"] = um.get("z_model")
            row["carry_bps"] = um.get("carry")
            row["current_coupon_pct"] = _f(um.get("current_coupon"))
        elif cls == "ofz_pk":
            row["current_coupon_pct"] = _f(um.get("current_coupon"))

        if price is not None:
            dirty_ccy = face * price / 100.0 + accrued
            row["mv_rub"] = round(qty * dirty_ccy * fx_rate, 2)
        else:
            row["warn"] = row["warn"] or "нет цены"
        rows.append(row)
    return rows


def _wavg(rows: List[dict], key: str) -> Optional[float]:
    """Взвешенное по MV среднее key среди строк, где есть и MV, и метрика."""
    pairs = [(r["mv_rub"], r[key]) for r in rows if r.get("mv_rub") and r.get(key) is not None]
    if not pairs:
        return None
    total = sum(mv for mv, _ in pairs)
    return round(sum(mv * v for mv, v in pairs) / total, 2) if total else None


def _class_breakdown(rows: List[dict], capital: Optional[float]) -> List[dict]:
    by_cls: Dict[str, List[dict]] = {}
    for r in rows:
        by_cls.setdefault(r["cls"], []).append(r)
    out = []
    for cls, rs in by_cls.items():
        mv = sum(r["mv_rub"] or 0.0 for r in rs)
        out.append({
            "cls": cls,
            "label": instruments.CLASS_LABELS.get(cls, cls),
            "mv_rub": round(mv, 2),
            "x_capital": round(mv / capital, 2) if capital else None,
            "n": len(rs),
            "ytm_pct": _wavg(rs, "ytm_pct"),
            "dm_bps": _wavg(rs, "dm_bps"),
            "z_bps": _wavg(rs, "z_bps"),
            "mod_dur": _wavg(rs, "mod_dur"),
            "dv01_rub": round(sum(r["dv01_rub"] or 0.0 for r in rs), 2),
        })
    out.sort(key=lambda x: -x["mv_rub"])
    return out


def _carry_annual_rub(rows: List[dict]) -> float:
    """Купонный доход активов, ₽/год (приближение Ф1: текущий купон, иначе YTM)."""
    total = 0.0
    for r in rows:
        mv = r.get("mv_rub")
        if not mv:
            continue
        rate = r.get("current_coupon_pct")
        if rate is None:
            rate = r.get("ytm_pct")
        if rate is not None:
            total += mv * rate / 100.0
    return total


async def fund_summary(code: str, include_positions: bool = True) -> Optional[dict]:
    fund = db.get_fund(code)
    if fund is None:
        return None
    positions = db.get_positions(code)
    repos = db.list_repos(code)
    calc_date = date.today()

    fx_info = await get_fx()
    fx = fx_info["rates"]
    info = await instruments.classify([p["isin"] for p in positions])
    rows = await _position_rows(positions, info, fx, calc_date) if positions else []

    mv_total = sum(r["mv_rub"] or 0.0 for r in rows)
    repo_rub_amounts = [r["amount"] * fx.get(r["ccy"], 1.0) for r in repos]
    repo_total = sum(repo_rub_amounts)
    funding_cost = sum(a * r["rate_pct"] / 100.0 for a, r in zip(repo_rub_amounts, repos))
    nav = mv_total - repo_total
    capital = _f(fund.get("capital")) or (nav if nav > 0 else None)

    carry_assets = _carry_annual_rub(rows)
    net_carry = carry_assets - funding_cost

    summary = {
        "code": fund["code"], "name": fund["name"], "base_ccy": fund["base_ccy"],
        "calc_date": calc_date.isoformat(),
        "snap_date": positions[0]["snap_date"] if positions else None,
        "n_positions": len(rows),
        "mv_rub": round(mv_total, 2),
        "repo_rub": round(repo_total, 2),
        "nav_rub": round(nav, 2),
        "capital_rub": round(capital, 2) if capital else None,
        "capital_is_derived": fund.get("capital") is None,
        "leverage_gross": round(mv_total / capital, 2) if capital else None,
        "dv01_rub": round(sum(r["dv01_rub"] or 0.0 for r in rows), 2),
        "funding_cost_rub": round(funding_cost, 2),
        # взвешиваем по ₽-эквиваленту — валютное РЕПО иначе искажает среднюю ставку
        "repo_rate_wa": round(funding_cost / repo_total * 100.0, 2) if repo_total else None,
        "carry_assets_rub": round(carry_assets, 2),
        "net_carry_rub": round(net_carry, 2),
        "net_carry_pct_capital": round(net_carry / capital * 100.0, 2) if capital else None,
        "fx": {k: v for k, v in fx.items() if k != "RUB"},
        "fx_label": fx_info.get("label"),
        "classes": _class_breakdown(rows, capital),
        "n_no_price": sum(1 for r in rows if r["mv_rub"] is None),
    }
    if include_positions:
        summary["positions"] = rows
    return summary


async def funds_overview() -> List[dict]:
    """Мини-сводка всех фондов для главного экрана (без позиций)."""
    return [s for s in await asyncio.gather(
        *(fund_summary(f["code"], include_positions=False) for f in db.list_funds())) if s]


def _add_months(d: date, months: int) -> date:
    y, m = d.year + (d.month - 1 + months) // 12, (d.month - 1 + months) % 12 + 1
    try:
        return d.replace(year=y, month=m)
    except ValueError:  # 31-е → конец короткого месяца
        return d.replace(year=y, month=m + 1, day=1)


async def fund_cashflow(code: str, months: int = 12) -> Optional[dict]:
    """Агрегированный денежный поток фонда по месяцам: купоны + принципал
    (амортизации/погашение из MOEX bondization; у него финальное погашение —
    последняя амортизация, отдельного «редемпшена» не добавляем).

    Будущие купоны флоатеров у MOEX без value — оцениваем по текущему купону
    из universe_metrics (иначе по последнему известному value); помечаем est."""
    fund = db.get_fund(code)
    if fund is None:
        return None
    positions = db.get_positions(code)
    calc_date = date.today()
    horizon = _add_months(calc_date, months)

    fx = (await get_fx())["rates"]
    info = await instruments.classify([p["isin"] for p in positions])
    uni_m = MarketDataService.universe_metrics()
    scheds = await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(
            info[p["isin"]]["sec"].get("secid") or p["isin"]) for p in positions))

    buckets: Dict[str, dict] = {}

    def _bucket(day: str) -> dict:
        ym = day[:7]
        return buckets.setdefault(ym, {
            "month": ym, "coupons_rub": 0.0, "coupons_est_rub": 0.0, "principal_rub": 0.0})

    for p, sched in zip(positions, scheds):
        isin, qty = p["isin"], float(p["qty"])
        meta = info[isin]
        fx_rate = fx.get(meta["ccy"], 1.0)
        coupons = (sched or {}).get("coupons") or []
        amorts = (sched or {}).get("amorts") or []

        # остаточный номинал = сумма будущих амортизаций (bondization надёжен
        # только так; поле face купонов не проецируется — см. STATUS.md)
        face_left = sum(_f(a.get("value")) or 0.0 for a in amorts
                        if a.get("date") and a["date"] > calc_date.isoformat()) \
            or (_f(meta["sec"].get("face")) or 1000.0)

        last_known = None
        for c in coupons:
            v = _f(c.get("value"))
            if v is not None:
                last_known = (c, v)
            end = c.get("end")
            if not end or end <= calc_date.isoformat() or end > horizon.isoformat():
                continue
            b = _bucket(end)
            if v is not None:
                b["coupons_rub"] += qty * v * fx_rate
            else:
                # прогноз: текущий купон (universe) → последний известный value
                rate = _f((uni_m.get(isin) or {}).get("current_coupon"))
                try:
                    days = (date.fromisoformat(end) - date.fromisoformat(c.get("start"))).days
                except (TypeError, ValueError):
                    days = 91
                if rate is not None:
                    est = face_left * rate / 100.0 * days / 365.0
                elif last_known:
                    est = last_known[1]
                else:
                    continue
                b["coupons_est_rub"] += qty * est * fx_rate

        for a in amorts:
            d, v = a.get("date"), _f(a.get("value"))
            if not d or v is None or d <= calc_date.isoformat() or d > horizon.isoformat():
                continue
            _bucket(d)["principal_rub"] += qty * v * fx_rate

    items = [
        {**b, "coupons_rub": round(b["coupons_rub"], 2),
         "coupons_est_rub": round(b["coupons_est_rub"], 2),
         "principal_rub": round(b["principal_rub"], 2)}
        for b in sorted(buckets.values(), key=lambda x: x["month"])]
    return {"code": code, "calc_date": calc_date.isoformat(), "months": months,
            "items": items,
            "fx": {k: v for k, v in fx.items() if k != "RUB"}}


async def snapshot_all_navs() -> int:
    """Дневной снапшот метрик всех фондов в nav_daily (идемпотентно за день)."""
    n = 0
    for s in await funds_overview():
        db.save_nav(s["code"], s["calc_date"], s)
        n += 1
    return n
