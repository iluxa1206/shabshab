"""Паспорт бумаги — аудит-развёртка для верификации каждой цифры карточки.

Собирает по одной бумаге:
  • реестр (все поля + провенанс: source/updated_at/manual_locked + enrich_seen);
  • спеку фиксинга по слоям: manual/db → парсер проспекта → калибратор,
    с указанием, какой слой дал mode/lag (та же логика, что стенд
    scripts/verify_fixing_specs, но по одной бумаге и с по-купонной детализацией);
  • бэктест: по каждому прошлому зафиксированному купону — предсказанная спекой
    ставка vs наблюдённая (value·365/(days·face) − маржа), ошибка в пп;
  • рынок: цена/источник/свежесть, НКД наш vs MOEX, свежесть истории индекса;
  • waterfall PV: по-платёжная развёртка дисконтирования на решённом XIRR —
    Σ PV будущих потоков обязана сходиться с dirty (иначе display-cashflow
    разошёлся с pricing-потоками — это находка, а не косметика);
  • светофор санити-чеков поверх всего.

Только чтение: сетевые вызовы — те же кэшируемые, что у карточки.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from core.valuation import _is_settlement_day_off

from services.market_data import MarketDataService
from services.bonds import create_bond_ref_data, build_ref_external, external_formula
from services.valuation import calculate_valuation_metrics
from services.cashflow import build_cashflow_from_moex
from services.exceptions import NotFoundException
from services import instruments_registry as reg

logger = logging.getLogger(__name__)

# Пороги бэктеста спеки — те же, что в стенде verify_fixing_specs
TOL_OK_PP = 0.15
TOL_BAD_PP = 0.50
MAX_PAST = 8          # прошлых купонов в детализации

# Допуск сходимости Σ PV с dirty: XIRR решается до 1e-10 по ставке, но потоки
# display-cashflow могут отличаться от pricing (окно оферты, амортизация) —
# расхождение больше 0.1% номинала = реальный рассинхрон пайплайнов.
_PV_GAP_TOL_FRAC = 0.001


async def _aempty():
    return {}


def _iso(d):
    if isinstance(d, date):
        return d.isoformat()
    return d


def _parse_d(s):
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


# ── Спека фиксинга: слои + провенанс ────────────────────────────────────────

def _resolve_spec_layers(isin: str, coupons, margin_pct, face, today, amorts):
    """Эффективная спека + все слои (manual/db, парсер, калибратор) + провенанс
    mode/lag. Зеркалит resolve_spec_with_source из scripts/verify_fixing_specs."""
    from services.ref_data import params, coupon_formula
    from services.coupon_calib import parse_prospectus_formula, calibrate

    p = params(isin)
    effective = coupon_formula(isin, coupons=coupons, margin_pct=margin_pct,
                               face=face, calc_date=today, amorts=amorts)

    manual_layer = {k: p.get(k) for k in
                    ("coupon_mode", "fixing_lag", "fixing_lag_unit", "cap_pct", "floor_pct")
                    if p.get(k) is not None} or None

    coupon_text = p.get("coupon_text")
    parser_layer = None
    if coupon_text:
        try:
            parser_layer = parse_prospectus_formula(coupon_text)
        except Exception as e:
            logger.warning(f"audit parse formula {isin}: {e}")

    calib_layer = None
    try:
        fixed_mode = (manual_layer or {}).get("coupon_mode") or \
                     (parser_layer or {}).get("mode")
        calib_layer = calibrate(isin, coupons, margin_pct, face or 1000.0, today,
                                base=p.get("base") or "KEYRATE", amorts=amorts,
                                fixed_mode=fixed_mode)
    except Exception as e:
        logger.warning(f"audit calibrate {isin}: {e}")

    src_mode = src_lag = None
    if p.get("coupon_mode") is not None:
        src_mode = "manual/db"
    if p.get("fixing_lag") is not None:
        src_lag = "manual/db"
    if parser_layer:
        if src_mode is None and parser_layer.get("mode") is not None:
            src_mode = "parser"
        if src_lag is None and parser_layer.get("lag") is not None:
            src_lag = "parser"
    if src_mode is None and effective.get("coupon_mode") is not None:
        src_mode = "calibrator"
    if src_lag is None and effective.get("fixing_lag") is not None:
        src_lag = "calibrator"
    if effective.get("coupon_mode") is None:
        src_mode = "default"

    return {
        "effective": effective,
        "sources": {"mode": src_mode or "none", "lag": src_lag or "none"},
        "layers": {"manual": manual_layer, "parser": parser_layer,
                   "calibrator": calib_layer},
        "coupon_text": coupon_text,
    }


# ── Бэктест спеки по прошлым купонам ────────────────────────────────────────

def _backtest(isin: str, base: str, spec: dict, coupons, margin_pct, face,
              today, amorts):
    """По каждому прошлому зафиксированному купону: предсказанная спекой полная
    ставка vs наблюдённая. Возвращает {rows, n, mean_err_pp, max_err_pp, verdict,
    fix_prelude}. Логика 1:1 со стендом verify_fixing_specs.backtest_bond."""
    from services.coupon_calib import (_past_rows, _index, _realized,
                                       projected_ks_pct, fixing_probe_date)

    out = {"rows": [], "n": 0, "mean_err_pp": None, "max_err_pp": None,
           "verdict": "NO_DATA", "fix_prelude": 0}

    mode = spec.get("coupon_mode")
    lag = spec.get("fixing_lag") if spec.get("fixing_lag") is not None else 0
    unit = spec.get("fixing_lag_unit") or "cal"
    avg_w = spec.get("avg_window_days")
    if mode is None:
        if base == "RUONIA":
            mode, lag, unit = "average", 0, "cal"   # прод: форвард-проекция ≈ avg lag0
        else:
            mode, avg_w = "average", 1              # дефолт: точечный фиксинг (окно 1)

    # fix-to-float прелюдия: ведущие блоки одинаковых зафиксированных ставок —
    # не флоатер-режим, в бэктест не входят
    prc = [c.get("valueprc") for c in coupons or []]
    lead = 0
    while lead + 1 < len(prc) and prc[lead] is not None and prc[lead] == prc[lead + 1]:
        v = prc[lead]
        while lead < len(prc) and prc[lead] == v:
            lead += 1
    cps_bt = coupons[lead:] if lead >= 1 else coupons
    if lead >= 1:
        out["fix_prelude"] = lead

    rows_past = _past_rows(cps_bt, margin_pct, face, today, amorts)[-MAX_PAST:]
    if not rows_past:
        return out
    idx = _index(base)
    if not idx or not idx[0]:
        return out

    # Маржа-лесенка: надбавка периода зависит от НОМЕРА купона («S 1-7 = 2.5%,
    # S8-21 = 4.6%»). Без неё бэктест сравнивал факт со скаляром реестра и давал
    # ложные BAD до 3пп (ТрансФин-М, БинФарм, Мегафон). Ординалы — по полному
    # графику MOEX (1-based), как в прайсинге.
    ms = spec.get("margin_schedule")
    ord_by_start = {}
    if ms:
        def _ck(c):
            v = c.get("start") or ""
            return v if isinstance(v, str) else v.isoformat()
        for i, c in enumerate(sorted(coupons or [], key=_ck)):
            s0 = c.get("start")
            if isinstance(s0, str):
                try:
                    s0 = date.fromisoformat(s0)
                except ValueError:
                    continue
            ord_by_start[s0] = i + 1
        # лесенка задаёт, КАКИЕ купоны плавающие: фикс-ступени вне диапазонов
        # («1-16 купоны — X% годовых») не флоатер — из бэктеста вон
        flo = {o for st in ms for o in range(st["from"], st["to"] + 1)}
        rows_past = [r for r in rows_past if ord_by_start.get(r[0]) in flo]
        if not rows_past:
            return out

    def _margin_for(s):
        o = ord_by_start.get(s)
        if o is not None:
            for st in ms or []:
                if st["from"] <= o <= st["to"]:
                    return st["bps"] / 100.0
        return margin_pct

    pspec = {"mode": mode, "lag": lag, "lag_unit": unit, "base": base,
             "avg_window_days": avg_w, "compounded": spec.get("compounded")}
    cap, floor = spec.get("cap_pct"), spec.get("floor_pct")
    errs = []
    for s, e, obs in rows_past:
        probe = fixing_probe_date(pspec, s)
        covered = _realized(idx, probe, today)
        row = {"start": _iso(s), "end": _iso(e), "days": (e - s).days,
               "observed_pct": round(obs + margin_pct, 4),
               "predicted_pct": None, "err_pp": None, "skipped": None}
        if not covered:
            row["skipped"] = "окно фиксинга не покрыто историей индекса"
            out["rows"].append(row)
            continue
        pred = projected_ks_pct(pspec, s, e, today, fwd_pct=lambda d: None, idx=idx)
        if pred is None or (pred == 0.0 and mode == "point"):
            row["skipped"] = "нет значения индекса на дату фиксинга"
            out["rows"].append(row)
            continue
        # битая строка MOEX: value ≈ 0 при живой базе (наблюдалось у Мегафон2P4
        # — купон 0.02% при КС 21%). Это дефект данных, а не ошибка спеки:
        # без отсечки один такой купон уводил вердикт в BAD (2.6пп на 8 куп.)
        if obs + margin_pct < 1.0 and pred > 2.0:
            row["skipped"] = "битые данные MOEX (ставка купона ≈ 0)"
            out["rows"].append(row)
            continue
        # предсказание — с маржой ЛЕСЕНКИ своего периода; факт (obs) снят на
        # скаляре реестра, поэтому к нему прибавляем скаляр
        pred_full, obs_full = pred + _margin_for(s), obs + margin_pct
        if cap is not None:
            pred_full = min(pred_full, float(cap))
        if floor is not None:
            pred_full = max(pred_full, float(floor))
        err = pred_full - obs_full
        errs.append(err)
        row.update({"predicted_pct": round(pred_full, 4),
                    "err_pp": round(err, 4)})
        out["rows"].append(row)

    if errs:
        a = sorted(abs(x) for x in errs)
        mean_abs = sum(a) / len(a)
        # вердикт по МЕДИАНЕ: один аномальный купон (правка эмитента, дефект
        # выгрузки) не должен объявлять спеку неверной — систематику ловит
        # медиана, а разовый выброс виден в max_err_pp
        med = a[len(a) // 2] if len(a) % 2 else (a[len(a) // 2 - 1] + a[len(a) // 2]) / 2
        out["n"] = len(errs)
        out["mean_err_pp"] = round(mean_abs, 3)
        out["med_err_pp"] = round(med, 3)
        out["max_err_pp"] = round(max(a), 3)
        out["verdict"] = ("OK" if med < TOL_OK_PP
                          else "WARN" if med < TOL_BAD_PP else "BAD")
    return out


# ── Основная сборка ─────────────────────────────────────────────────────────

async def build_bond_audit(isin: str, cache: dict) -> dict:
    data = cache.get(isin)
    external = data is None

    res = await asyncio.gather(
        MarketDataService.fetch_last_prices([isin]),                                # 0
        MarketDataService.fetch_moex_snapshot([isin]),                              # 1
        MarketDataService.get_curves(),                                             # 2
        MarketDataService.fetch_bond_schedule_full(isin),                           # 3
        MarketDataService.fetch_moex_securities([isin]) if external else _aempty(), # 4
        MarketDataService.fetch_coupon_schedules([isin]),                           # 5
        return_exceptions=True,
    )
    _ok = lambda x, d: d if isinstance(x, Exception) else x
    market_prices = _ok(res[0], {})
    snapshot = _ok(res[1], {})
    ruonia_curve, keyrate_curve, calc_date, rates_date = _ok(res[2], (None, None, None, None))
    sched_full = _ok(res[3], {"coupons": [], "amorts": [], "offers": []})
    mo_map = _ok(res[4], {})
    schedules = _ok(res[5], {})

    if data:
        ref_obj = create_bond_ref_data(data, isin)
        formula = data.get("FORMULA", "") or external_formula(ref_obj)
    else:
        mo = mo_map.get(isin, {})
        if not mo:
            raise NotFoundException(f"Bond {isin} not found on MOEX", {"isin": isin})
        ref_obj = build_ref_external(isin, mo)
        formula = external_formula(ref_obj)

    if not calc_date:
        calc_date = rates_date or date.today()
    today = date.today()
    warnings = []
    checks = []

    def check(cid, label, status, detail=None):
        checks.append({"id": cid, "label": label, "status": status,
                       "detail": detail})

    # ── реестр + провенанс ──────────────────────────────────────────────────
    reg_row = None
    try:
        reg_row = reg.get(isin)
    except Exception as e:
        warnings.append(f"реестр недоступен: {e}")
    enrich = None
    try:
        enrich = reg.enrich_info(isin)
    except Exception:
        pass
    registry_block = None
    if reg_row:
        registry_block = dict(reg_row)
        registry_block["enrich"] = enrich
        if reg_row.get("manual_locked"):
            check("manual_locked", "Ручная заморозка параметров", "info",
                  "manual_locked=1 — sync и парсер НЕ обновляют расчётные поля "
                  "(источник: ручная правка или импорт xlsx)")
        mc = reg_row.get("margin_check_pp")
        if mc is not None:
            st = "bad" if abs(mc) > 1.5 else "ok"
            check("margin_check", "Бэк-аут маржи vs факт индекса", st,
                  f"расхождение {mc:+.2f} пп (порог подозрения ±1.5)")
    else:
        check("registry", "Бумага в реестре", "warn",
              "нет строки реестра — параметры только из Cbonds/MOEX-кэша")

    # ── спека фиксинга + бэктест ────────────────────────────────────────────
    base = ref_obj.base
    margin_pct = (ref_obj.spread_issue_bps or 0) / 100.0
    coupons = sched_full.get("coupons") or []
    amorts = sched_full.get("amorts") or []
    offers = sched_full.get("offers") or []

    # НОМИНАЛ: график амортизаций (Σ будущих траншей, вкл. финальное погашение)
    # > реестр > кэш. isins_cache у амортизируемых бумаг стейлится (БалтЛизП10:
    # кэш 1000 при остатке 900) → _face_on откатывал прошлые купоны к 1100 и
    # бэктест/оценка получали ложную систематику −1.5пп (факт выплат при этом
    # сходился с проспектом до копейки). reconcile_face не спасает — он ловит
    # только кратные (×3+) промахи деноминации, не амортизацию.
    from services.bonds import amort_remaining_face
    face_cache = ref_obj.face_value
    face_reg = (reg_row or {}).get("face_value")
    face_amort = amort_remaining_face(amorts, calc_date)
    face = face_amort or face_reg or face_cache or 1000.0
    ref_obj.face_value = face          # оценка/waterfall тоже на честном остатке
    srcs = {"график MOEX": face_amort, "реестр": face_reg, "кэш": face_cache}
    used_src = next(k for k, v in srcs.items() if v == face)
    vals = {v for v in srcs.values() if v}
    if len(vals) > 1:
        check("face", "Номинал: график MOEX vs реестр vs кэш", "warn",
              " · ".join(f"{k} {v:.0f}₽" for k, v in srcs.items() if v is not None)
              + f"; применён {used_src} — стейл-источники травят карточку/конвейер")
    else:
        check("face", "Номинал (остаток)", "ok", f"{face:.0f}₽ · {used_src}")

    spec_block = {"effective": {}, "sources": {}, "layers": {}, "coupon_text": None}
    backtest = {"rows": [], "n": 0, "verdict": "NO_DATA"}
    if base in ("KEYRATE", "RUONIA"):
        try:
            spec_block = _resolve_spec_layers(isin, coupons, margin_pct, face,
                                              today, amorts)
        except Exception as e:
            warnings.append(f"спека: {e}")
        try:
            backtest = _backtest(isin, base, spec_block.get("effective") or {},
                                 coupons, margin_pct, face, today, amorts)
        except Exception as e:
            warnings.append(f"бэктест: {e}")

        v = backtest.get("verdict")
        if v == "OK":
            check("spec_backtest", "Обратный пересчёт купонов по спеке", "ok",
                  f"{backtest['n']} куп., средн. |ошибка| {backtest['mean_err_pp']} пп")
        elif v == "WARN":
            check("spec_backtest", "Обратный пересчёт купонов по спеке", "warn",
                  f"средн. {backtest['mean_err_pp']} пп, макс {backtest['max_err_pp']} пп "
                  "— возможен неверный лаг/режим")
        elif v == "BAD":
            check("spec_backtest", "Обратный пересчёт купонов по спеке", "bad",
                  f"средн. {backtest['mean_err_pp']} пп — спека почти наверняка неверна")
        else:
            check("spec_backtest", "Обратный пересчёт купонов по спеке", "na",
                  "нет прошлых зафиксированных купонов или истории индекса")
    else:
        check("spec_backtest", "Обратный пересчёт купонов по спеке", "na",
              f"база {base} — спека фиксинга неприменима")

    # ── период: реестр vs факт графика ──────────────────────────────────────
    period_fact = None
    try:
        from core.cashflow import coupon_period_from_coupons
        period_fact = coupon_period_from_coupons(
            coupons, issue_date=(reg_row or {}).get("issue_date"), today=today)
    except Exception:
        pass
    period_db = (reg_row or {}).get("coupon_period_days") or ref_obj.coupon_period_days
    if period_fact and period_db:
        if abs(period_fact - period_db) > 3:
            check("period", "Купонный период: реестр vs факт", "warn",
                  f"реестр {period_db} дн, факт графика {period_fact} дн")
        else:
            check("period", "Купонный период: реестр vs факт", "ok",
                  f"{period_db} дн (факт {period_fact})")
    else:
        check("period", "Купонный период: реестр vs факт", "na",
              "недостаточно данных графика")

    # ── свежесть истории индекса ────────────────────────────────────────────
    index_block = None
    if base in ("KEYRATE", "RUONIA"):
        try:
            from services.coupon_calib import index_history
            dts, rates = index_history(base)
            if dts:
                last_d, last_r = dts[-1], rates[-1]
                age = (today - last_d).days
                index_block = {"base": base, "last_date": _iso(last_d),
                               "last_value_pct": last_r, "age_days": age,
                               "n_points": len(dts), "first_date": _iso(dts[0])}
                st = "ok" if age <= 4 else "warn" if age <= 10 else "bad"
                check("index_fresh", f"История {base}", st,
                      f"последняя точка {last_d.isoformat()} ({last_r}%), {age} дн назад")
            else:
                check("index_fresh", f"История {base}", "bad", "история пуста")
        except Exception as e:
            check("index_fresh", f"История {base}", "bad", f"ошибка: {e}")

    # ── рынок / цена / НКД ──────────────────────────────────────────────────
    last_price = market_prices.get(isin)
    snap = snapshot.get(isin, {})
    prev_close = snap.get("prev")
    accrued_moex = snap.get("accrued")
    accrued_cache = ref_obj.accrued_rub
    session = None
    try:
        session = MarketDataService.session_prices().get(isin)
    except Exception:
        pass

    market_block = {
        "last_price_pct": last_price,
        "price_source": "Alor WebSocket" if last_price is not None else None,
        "session_price_pct": session,
        "prev_close_pct": prev_close,
        "accrued_moex_rub": accrued_moex,
        "accrued_cache_rub": accrued_cache,
        "calc_date": _iso(calc_date),
        "rates_date": _iso(rates_date),
        "index": index_block,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if last_price is not None:
        check("price", "Цена", "ok", f"{last_price}% (Alor live/сделка)")
    elif prev_close is not None:
        check("price", "Цена", "warn",
              f"live нет — prev close {prev_close}% (метрики от несвежей цены)")
    else:
        check("price", "Цена", "bad", "цены нет — оценка невозможна")

    if rates_date:
        st = "ok" if rates_date >= today else "warn"
        check("rates_fresh", "Кривая ставок", st,
              f"rates_date {rates_date.isoformat()}" +
              ("" if st == "ok" else " — не сегодняшняя (выходной/до обновления)"))

    if accrued_moex is not None and accrued_cache is not None:
        d = abs(accrued_moex - accrued_cache)
        st = "ok" if d < 0.51 else "warn"
        check("accrued", "НКД: MOEX vs кэш", st,
              f"MOEX {accrued_moex}₽, кэш {accrued_cache}₽ (Δ {d:.2f}₽); "
              "в расчёт идёт MOEX")

    # Прежний чек «Σ амортизаций vs max(face) купонов» удалён: MOEX перезаписывает
    # facevalue ВСЕГО графика текущим остатком (у амортизированных все строки = 900),
    # сравнение с Σ траншей (= исходный номинал) давало ложный WARN. Его роль
    # выполняет чек "face" выше (сверка трёх источников номинала).

    # ── горизонт: последний купон vs погашение ──────────────────────────────
    if ref_obj.maturity_date and coupons:
        last_end = max((_parse_d(c.get("end")) for c in coupons
                        if _parse_d(c.get("end"))), default=None)
        if last_end:
            gap = abs((ref_obj.maturity_date - last_end).days)
            st = "ok" if gap <= 3 else "warn"
            check("maturity", "Погашение vs конец графика купонов", st,
                  f"maturity {ref_obj.maturity_date.isoformat()}, "
                  f"последний купон до {last_end.isoformat()}")

    # ── оценка + waterfall PV ───────────────────────────────────────────────
    curve = ruonia_curve if base == "RUONIA" else keyrate_curve
    px = last_price if last_price is not None else prev_close
    val_dict = {}
    waterfall = {"rows": [], "pv_sum_rub": None, "pv_gap_rub": None}
    cfs = []
    try:
        cfs, _fv = build_cashflow_from_moex(
            ref_obj, curve, calc_date, coupons, amorts, formula, offers=offers)
    except Exception as e:
        warnings.append(f"cashflow: {e}")

    if px is not None and curve and base in ("RUONIA", "KEYRATE"):
        try:
            val_dict = calculate_valuation_metrics(
                ref_obj, px, curve, calc_date,
                accrued_override=accrued_moex, periods=schedules.get(isin),
                amorts=amorts, offers=offers)
        except Exception as e:
            warnings.append(f"оценка: {e}")

    dirty = val_dict.get("dirty_price_rub")
    y = val_dict.get("yield_xirr_pct")
    if cfs and dirty is not None and y is not None:
        pv_sum = 0.0
        rows = []
        for c in cfs:
            pd = _parse_d(c.get("payment_date"))
            fut = pd is not None and pd > calc_date
            t = ((pd - calc_date).days / 365.0) if fut else None
            df = (1.0 + y / 100.0) ** (-t) if fut else None
            pv = c["amount_rub"] * df if fut else None
            if pv is not None:
                pv_sum += pv
            rows.append({
                "number": c.get("number"), "payment_date": _iso(c.get("payment_date")),
                "period_start": _iso(c.get("period_start")),
                "period_end": _iso(c.get("period_end")),
                "type": c.get("type"), "amount_rub": c.get("amount_rub"),
                "coupon_rate_pct": c.get("coupon_rate_pct"),
                "base_rate_pct": c.get("base_rate_pct"),
                "t_yrs": round(t, 4) if t is not None else None,
                "df": round(df, 6) if df is not None else None,
                "pv_rub": round(pv, 2) if pv is not None else None,
            })
        gap = pv_sum - dirty
        waterfall = {"rows": rows, "yield_pct": y,
                     "dirty_price_rub": dirty,
                     "pv_sum_rub": round(pv_sum, 2), "pv_gap_rub": round(gap, 2)}
        tol = (ref_obj.face_value or 1000.0) * _PV_GAP_TOL_FRAC
        st = "ok" if abs(gap) <= tol else "warn"
        check("pv_recon", "Σ PV потоков vs dirty (на решённом XIRR)", st,
              f"Σ PV {pv_sum:.2f}₽ vs dirty {dirty:.2f}₽ (Δ {gap:+.2f}₽)" +
              ("" if st == "ok" else " — display-cashflow разошёлся с pricing-потоками"))
    else:
        check("pv_recon", "Σ PV потоков vs dirty", "na",
              "нет цены/кривой/потоков для развёртки")

    # implausible-флаг из оценки
    for w in val_dict.get("warnings") or []:
        if "убыток" in str(w).lower() or "implausible" in str(w).lower():
            check("implausible", "Правдоподобие цены", "bad", str(w))

    return {
        "isin": isin,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calc_date": _iso(calc_date),
        "checks": checks,
        "registry": registry_block,
        "spec": spec_block,
        "backtest": backtest,
        "market": market_block,
        "valuation": val_dict,
        "waterfall": waterfall,
        "schedule": {"coupons": coupons, "amorts": amorts, "offers": offers,
                     "n_coupons": len(coupons)},
        "formula": formula,
        "warnings": warnings,
    }


# ── Дневная раскладка базы: ВСЕ будущие купоны одним списком ────────────────

def _day_pairs(mode: str, start: date, end: date, lag: int, unit: str,
               avg_window: int = None):
    """(день, дата наблюдения индекса) по семантике режима — 1:1 с
    coupon_calib.projected_ks_pct:
      avg_window  — явное окно [obs(start)−W, obs(start)); W=1 = бывший point;
      average     — дни дохода (start, end], obs = день − lag;
      avg_prev    — окно [start−period−lag, start−lag), obs = сам день;
      point       — легаси, одна дата obs = start − lag;
      month_start — одна дата obs = 1-е число месяца старта."""
    from services.coupon_calib import _obs_date
    if avg_window:
        w_hi = _obs_date(start, lag, unit)
        if int(avg_window) <= 1:
            return [(start, w_hi)]
        cur = w_hi - timedelta(days=int(avg_window))
        return [(d, d) for d in
                (cur + timedelta(days=k) for k in range(int(avg_window)))]
    if mode == "point":
        return [(start, _obs_date(start, lag, unit))]
    if mode == "month_start":
        return [(start, start.replace(day=1))]
    pairs = []
    if mode == "avg_prev":
        period = (end - start).days
        w_hi = _obs_date(start, lag, unit)
        cur = w_hi - timedelta(days=period)
        while cur < w_hi:
            pairs.append((cur, cur))
            cur += timedelta(days=1)
        return pairs
    cur = start + timedelta(days=1)     # average: дни дохода (start, end]
    while cur <= end:
        pairs.append((cur, _obs_date(cur, lag, unit)))
        cur += timedelta(days=1)
    return pairs


def accrue_index(rows: list, state: dict = None) -> dict:
    """Расчётный индекс базы на каждый день раскладки: пишет в строки поле
    "index", возвращает состояние накопления для СЛЕДУЮЩЕГО купона.

    Индекс СКВОЗНОЙ: 1.0 на первый день раскладки и дальше копится через все
    купоны подряд (state прокидывается из купона в купон). Сброс в 1.0 на каждом
    купоне ломал бы главное — видеть накопление базы за весь горизонт.

    Механика — ровно та, что у базы Y-IDX (core.valuation._RuoniaCompoundPath):
      • ФИКСИНГ ДНЯ ДАЁТ ПРИРОСТ СЛЕДУЮЩЕГО ДНЯ — прирост строки берётся по
        ставке ПРЕДЫДУЩЕЙ строки (пятничные 14.25% отрабатывают сб, вс и пн);
      • капитализация только когда ОПУБЛИКОВАН НОВЫЙ ФИКСИНГ — строка помечает
        это флагом "is_fixing"; внутри окна без фиксинга начисление простое от
        замороженного уровня.
    Поэтому приросты сб/вс/пн равны до последнего знака, а во вторник ступенька
    вверх — база капитализировалась в понедельник.

    ФЛАГ ФИКСИНГА, А НЕ КАЛЕНДАРЬ. Раньше капитализация шла по рабочим дням
    календаря MOEX, и в дни-переносы, когда биржа работает, а фиксинга нет
    (03.05.2010 и такие же «повторы» ЦБ), расчёт капитализировался лишний раз —
    индекс уезжал вверх на 0.5 bps за 16 лет. Строки без флага (будущее: фиксинга
    ещё не существует) падают на прежний календарный признак.

    Дни без ставки (истории нет и кривая молчит) индекс не двигают: уровень
    остаётся прежним, дыра видна по пустой ставке в той же строке.

    state: {"level","base","prev_rate","seen"}; None — начать с 1.0.
    Поле "end" в ответе — уровень на ДАТУ КОНЦА периода: последняя ставка
    периода отрабатывает уже первый день следующего, поэтому end равен index
    первой строки следующего купона (серия стыкуется без разрыва).
    """
    import calendar as _cal
    st = state or {"level": 1.0, "base": 1.0, "prev_rate": None, "seen": False}
    level, base = st["level"], st["base"]
    prev_rate, seen = st["prev_rate"], st["seen"]
    start = level
    # ACT/ACT: делитель — ФАКТИЧЕСКАЯ длина года ДНЯ НАЧИСЛЕНИЯ, а день начисления
    # на сутки РАНЬШЕ дня прироста (фиксинг дня T отрабатывает день T, а виден в
    # индексе на T+1). Именно так считает официальный индекс ЦБ: на 365 расчёт
    # уезжал вверх в високосные годы, а базис по году дня ПРИРОСТА врал каждое
    # 1 января (декабрьский день начисления делился на 366 нового года).
    _yb_at = lambda d: (366.0 if _cal.isleap((d - timedelta(days=1)).year) else 365.0) if d else 365.0
    for r in rows:
        day = _parse_d(r.get("day"))
        if prev_rate is not None:
            level += base * (prev_rate / 100.0) / _yb_at(day)
            seen = True
        r["index"] = round(level, 10)
        fixing = r.get("is_fixing")
        if fixing is None:      # будущее: флага нет — фолбэк на календарь MOEX
            obs = _parse_d(r.get("obs_date"))
            fixing = obs is not None and not _is_settlement_day_off(obs)
        if fixing:
            base = level        # новый фиксинг опубликован → капитализируем
        prev_rate = r.get("rate_pct")
    _last = _parse_d(rows[-1].get("day")) if rows else None
    _yb = _yb_at(_last + timedelta(days=1)) if _last else 365.0
    end = level + (base * (prev_rate / 100.0) / _yb if prev_rate is not None else 0.0)
    return {"level": level, "base": base, "prev_rate": prev_rate, "seen": seen,
            "start": round(start, 10) if rows else None,
            "end": round(end, 10) if (seen and rows) else None}


async def coupon_day_rates(isin: str, cache: dict) -> dict:
    """Полная дневная раскладка фиксинга по ВСЕМ неистёкшим купонам (текущий
    начавшийся + будущие до погашения/оферты): по каждому дню — дата наблюдения
    индекса, ставка и источник (факт истории ЦБ / форвард-СТУПЕНЬ кривой между
    тенорами, daily_forward). Периоды — из канонического display-cashflow
    (build_cashflow_from_moex: резка к оферте, достройка хвоста), так что
    список купонов совпадает с таблицей PV паспорта. Для каждого купона —
    среднее по дням, боевой projected_ks_pct (кросс-чек) и ставка из cashflow."""
    from services.coupon_calib import (index_history, projected_ks_pct,
                                       _rate_at, _realized)
    from services.ref_data import coupon_formula

    data = cache.get(isin)
    external = data is None
    res = await asyncio.gather(
        MarketDataService.get_curves(),                                             # 0
        MarketDataService.fetch_bond_schedule_full(isin),                           # 1
        MarketDataService.fetch_moex_securities([isin]) if external else _aempty(), # 2
        return_exceptions=True,
    )
    _ok = lambda x, d: d if isinstance(x, Exception) else x
    ruonia_curve, keyrate_curve, calc_date, rates_date = _ok(res[0], (None, None, None, None))
    sched_full = _ok(res[1], {"coupons": [], "amorts": [], "offers": []})
    mo_map = _ok(res[2], {})

    if data:
        ref_obj = create_bond_ref_data(data, isin)
        formula = data.get("FORMULA", "") or external_formula(ref_obj)
    else:
        mo = mo_map.get(isin, {})
        if not mo:
            raise NotFoundException(f"Bond {isin} not found on MOEX", {"isin": isin})
        ref_obj = build_ref_external(isin, mo)
        formula = external_formula(ref_obj)

    base = ref_obj.base
    if base not in ("KEYRATE", "RUONIA"):
        raise NotFoundException(f"База {base} — дневной фиксинг неприменим", {"isin": isin})
    if not calc_date:
        calc_date = rates_date or date.today()
    curve = ruonia_curve if base == "RUONIA" else keyrate_curve

    coupons = sched_full.get("coupons") or []
    amorts = sched_full.get("amorts") or []
    offers = sched_full.get("offers") or []
    margin_pct = (ref_obj.spread_issue_bps or 0) / 100.0
    idx = index_history(base)
    # даты, на которые ЦБ реально опубликовал фиксинг: только в такие дни
    # официальный индекс капитализируется (см. accrue_index)
    _fix_days = set(idx[0]) if idx and idx[0] else set()

    # номинал: остаток из графика амортизаций > кэш (стейл-кэш травит калибровку,
    # см. одноимённый блок в build_bond_audit)
    from services.bonds import amort_remaining_face
    _rem = amort_remaining_face(amorts, calc_date)
    if _rem is not None:
        ref_obj.face_value = _rem

    spec = coupon_formula(isin, coupons=coupons, margin_pct=margin_pct,
                          face=ref_obj.face_value or 1000.0, calc_date=calc_date,
                          amorts=amorts, idx=idx)
    mode = spec.get("coupon_mode")
    lag = spec.get("fixing_lag") if spec.get("fixing_lag") is not None else 0
    unit = spec.get("fixing_lag_unit") or "cal"
    avg_w = spec.get("avg_window_days")
    if mode is None:
        if base == "RUONIA":
            mode, lag, unit = "average", 0, "cal"
        else:
            mode, avg_w = "average", 1      # дефолт: точечный фиксинг (окно 1)
    cap, floor = spec.get("cap_pct"), spec.get("floor_pct")

    def _fwd_step(d: date):
        if curve is None:
            return None
        try:
            return curve.daily_forward(d) * 100.0
        except Exception:
            return None

    # история цены/Y-IDX для прошлых дней (spread_daily: вечерние снапшоты +
    # честный бэкфилл) — join по дате в строки раскладки
    hist_by_date = {}
    try:
        from services.spread_history import read_history
        hist_by_date = {r["date"]: r for r in read_history(isin, days=400)}
    except Exception as e:
        logger.warning(f"coupon-days history {isin}: {e}")

    # канонические периоды: как таблица PV (оферта, хвост, амортизация)
    cfs, _ = build_cashflow_from_moex(ref_obj, curve, calc_date,
                                      coupons, amorts, formula, offers=offers)
    groups = []
    idx_state = None        # сквозное накопление индекса через все купоны
    for c in cfs:
        if c.get("type") != "COUPON":
            continue
        s, e = _parse_d(c.get("period_start")), _parse_d(c.get("period_end"))
        if not s or not e or e <= calc_date:
            continue    # истёкшие — их дневная история уже в бэктесте спеки
        rows, vals = [], []
        for day, obs in _day_pairs(mode, s, e, lag, unit, avg_window=avg_w):
            fact = _realized(idx, obs, calc_date)
            rate = _rate_at(idx, obs) if fact else _fwd_step(obs)
            if rate is not None:
                vals.append(rate)
            # цена закрытия + Y-IDX as-of дня — ТЕ ЖЕ строки spread_daily
            # (honest/snap), что рисует график «Динамика DM»: сверка дневной
            # раскладки с калькулятором исторических спредов. Тяжёлый бэкфилл
            # отсюда не запускаем — только уже посчитанные точки.
            h = hist_by_date.get(_iso(day))
            rows.append({"day": _iso(day), "obs_date": _iso(obs),
                         "rate_pct": round(rate, 4) if rate is not None else None,
                         # капитализация индекса — в день ПУБЛИКАЦИИ фиксинга;
                         # для прошлого это точное попадание в историю ЦБ, для
                         # будущего флага нет и accrue_index берёт календарь
                         "is_fixing": (obs in _fix_days) if fact else None,
                         "src": "fact" if fact else "forward",
                         "close_pct": (h or {}).get("price_pct"),
                         "y_idx_bps": (h or {}).get("y_idx")})
        idx_state = accrue_index(rows, idx_state)
        index_start, index_end = idx_state["start"], idx_state["end"]
        mean_rows = round(sum(vals) / len(vals), 4) if vals else None
        pspec = {"mode": mode, "lag": lag, "lag_unit": unit, "base": base,
                 "avg_window_days": avg_w, "compounded": spec.get("compounded")}
        try:
            prod = round(projected_ks_pct(pspec, s, e, calc_date,
                                          fwd_pct=lambda d: _fwd_step(d) or 0.0,
                                          idx=idx), 4)
        except Exception:
            prod = None
        cpn = None
        if mean_rows is not None:
            cpn = mean_rows + margin_pct
            if cap is not None:
                cpn = min(cpn, float(cap))
            if floor is not None:
                cpn = max(cpn, float(floor))
            cpn = round(cpn, 4)
        groups.append({
            "n": c.get("number"), "start": _iso(s), "end": _iso(e),
            "pay_date": _iso(c.get("payment_date")),
            "mean_pct": mean_rows, "projected_pct": prod,
            "coupon_rate_pct": cpn,
            # индекс на границах периода + годовой эквивалент ИМЕННО этого
            # периода: для бумаг с compounded=1 купон считается ровно как
            # Index_end/Index_start − 1 (индекс сквозной, поэтому нужна пара)
            "index_start": index_start,
            "index_end": index_end,
            "index_rate_pct": (round((index_end / index_start - 1.0) * 365.0
                                     / max((e - s).days, 1) * 100.0, 4)
                               if index_end and index_start else None),
            "display_rate_pct": c.get("coupon_rate_pct"),  # ставка из таблицы PV
            "n_fact": sum(1 for r in rows if r["src"] == "fact"),
            "rows": rows,
        })

    return {
        "isin": isin, "calc_date": _iso(calc_date), "base": base,
        "spec": {"mode": mode, "lag": lag, "lag_unit": unit,
                 "avg_window_days": avg_w, "compounded": spec.get("compounded"),
                 "margin_bps": ref_obj.spread_issue_bps,
                 "cap_pct": cap, "floor_pct": floor},
        "coupons": groups,
        "n_days": sum(len(g["rows"]) for g in groups),
    }
