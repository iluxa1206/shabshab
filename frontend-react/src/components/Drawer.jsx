import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { baseLabel, couponsPerYear, fmt, dmColor } from "../format.js";
import CouponFormula from "./CouponFormula.jsx";
import { fetchBondDetails, fetchFixedDetails, repriceBond, priceFromSpread, fetchRepricePast, UnauthorizedError, issueUrl, APP_BASENAME } from "../api.js";
import CashflowChart from "./CashflowChart.jsx";
import PriceChart from "./PriceChart.jsx";
import SpreadHistory from "./SpreadHistory.jsx";
import FixedCard from "./FixedCard.jsx";
import Orderbook from "./Orderbook.jsx";

function RefCell({ k, children }) {
  return (
    <div className="ref-cell">
      <div className="ref-k">{k}</div>
      <div className="ref-v">{children == null || children === "" ? <span className="dash">—</span> : children}</div>
    </div>
  );
}

// ЕДИНАЯ схема метрик флоатера: спред Y-IDX, обе доходности, цена (чистая с
// грязной в подписи), НКД и дата поставки. Один компонент на рыночный блок и на
// оба калькулятора (под введённую цену и на прошлую дату) — подписи и порядок
// не разъезжаются. Всё считается НА ДАТУ ПОСТАВКИ: цена — котировка своего дня,
// деньги и НКД — T+1 раб. DM/SM убраны из карточки (первичная метрика — Y-IDX).
// Метрики ГОРИЗОНТА поверх объекта оценки: цена/НКД/дата поставки от горизонта
// не зависят, а доходности и спред — зависят (поток режется к оферте, база
// Y-IDX роллируется до той же даты). sel: "auto" (правило цены на бэке) либо
// явный ключ "maturity"|"put"|"call" — ручной свитчер карточки.
const HZ_KEYS = ["sm_bps", "disc_margin_bps", "yield_xirr_pct",
                 "index_yield_pct", "yield_over_index_bps"];

export function horizonView(v, sel) {
  const hzs = (v && v.horizons) || {};
  let key = (!sel || sel === "auto") ? (v?.preferred_horizon || "maturity") : sel;
  if (!hzs[key]) key = hzs.maturity ? "maturity" : key;
  const h = hzs[key];
  if (!h) return { v, key: "maturity", date: null, pricePct: null };
  const out = { ...v };
  for (const k of HZ_KEYS) out[k] = h[k] ?? null;
  return { v: out, key, date: h.date || null, pricePct: h.price_pct ?? null };
}

const HZ_LABEL = { maturity: "к погашению", put: "к оферте (пут)", call: "к call" };

// Свитчер горизонта прайсинга. Кнопки «Авто» нет: подсвечен сразу тот горизонт,
// который выбрало правило цены (active = разрешённый ключ, не сырое состояние) —
// пользователь видит, к чему бумага прайсится, без лишнего клика. Показываем,
// только если у бумаги вообще есть альтернативный горизонт.
export function HorizonSwitch({ v, active, onChange }) {
  const hzs = (v && v.horizons) || {};
  const alts = ["put", "call"].filter((k) => hzs[k]);
  if (!alts.length) return null;
  const opts = [
    { k: "maturity", t: "Погашение", title: fmt.date(hzs.maturity?.date) || "" },
    ...alts.map((k) => ({
      k, t: k === "put" ? "Оферта" : "Call",
      title: fmt.date(hzs[k].date) || "",
    })),
  ];
  return (
    <div className="hz-switch">
      <span className="hz-switch-label">Считать к</span>
      {opts.map((o) => (
        <button key={o.k} type="button" title={o.title}
          className={"hz-btn" + (active === o.k ? " hz-btn-on" : "")}
          onClick={() => onChange(o.k)}>{o.t}</button>
      ))}
    </div>
  );
}

export function ValCards({ v, priceDate, calc = false, hzKey, hzDate }) {
  const u = (t) => <span className="vc-u"> {t}</span>;
  // к чему посчитаны цифры плиток — иначе «R-spread 320» к оферте и к погашению
  // выглядят одинаково, а это разные числа
  const hzNote = hzKey && hzKey !== "maturity"
    ? `${HZ_LABEL[hzKey]}${hzDate ? " " + fmt.date(hzDate) : ""}`
    : "к погашению";
  // копейки только пока число короткое: у бумаг с номиналом в миллионы
  // (RU000A1034Q5 — НКД 385 000 ₽) дробная часть не влезала в плитку
  const money = (x) => (x == null ? null : Math.abs(x) >= 1e5 ? fmt.num(x, 0) : fmt.num(x, 2));
  return (
    <div className={"val-cards val-cards-6" + (calc ? " val-cards-calc" : "")}>
      <div className="vc">
        <div className="vc-label">R-spread</div>
        <div className="vc-val" style={{ color: dmColor(v.yield_over_index_bps).color }}>
          {fmt.bps(v.yield_over_index_bps) ?? "—"}{u("bps")}</div>
        <div className="vc-sub">спред: YTM − база · {hzNote}</div>
      </div>
      <div className="vc">
        <div className="vc-label">YTM</div>
        <div className="vc-val">{fmt.pct(v.yield_xirr_pct) ?? "—"}{u("%")}</div>
        <div className="vc-sub">XIRR бумаги · {hzNote}</div>
      </div>
      <div className="vc">
        <div className="vc-label">YTM база</div>
        <div className="vc-val">{fmt.pct(v.index_yield_pct) ?? "—"}{u("%")}</div>
        <div className="vc-sub">роллирование RUONIA</div>
      </div>
      <div className="vc">
        <div className="vc-label">Цена</div>
        <div className="vc-val">{fmt.pct(v.clean_price_pct) ?? "—"}{u("%")}</div>
        <div className="vc-sub">грязная {money(v.dirty_price_rub) ?? "—"} ₽
          {priceDate ? ` · ${fmt.date(priceDate)}` : ""}</div>
      </div>
      <div className="vc">
        <div className="vc-label">НКД (ACI)</div>
        <div className="vc-val">{money(v.accrued_settle_rub) ?? "—"}{u("₽")}</div>
        <div className="vc-sub">на дату поставки</div>
      </div>
      <div className="vc">
        <div className="vc-label">Дата поставки</div>
        <div className="vc-val vc-val-sm">{fmt.date(v.settlement_date) ?? "—"}</div>
        <div className="vc-sub">T+1 раб. день</div>
      </div>
    </div>
  );
}

// специфика флоатера: rate duration мала (до рефиксинга), spread duration = весь
// кредитный риск до погашения
function FloaterSection({ f, base }) {
  if (!f) return null;
  const baseLbl = baseLabel(base);
  return (
    <>
      <div className="section-title">Флоатер-риск</div>
      <div className="val-cards">
        <div className="vc">
          <div className="vc-label">Спред-дюрация</div>
          <div className="vc-val">{fmt.num(f.spread_duration_yrs, 2) ?? "—"}<span className="vc-u"> лет</span></div>
          <div className="vc-sub">риск ΔDM/Δz (до погашения)</div>
        </div>
        <div className="vc">
          <div className="vc-label">Дюрация к ставке</div>
          <div className="vc-val">{fmt.num(f.rate_duration_yrs, 2) ?? "—"}<span className="vc-u"> лет</span></div>
          <div className="vc-sub">до рефиксинга {f.days_to_refix != null ? f.days_to_refix + " дн" : "—"}</div>
        </div>
      </div>
      <div className="ref-grid">
        <RefCell k="Текущий купон">{f.current_coupon_pct != null ? fmt.pct(f.current_coupon_pct) + " %" : null}</RefCell>
        <RefCell k={`Уровень ${baseLbl}`}>{f.base_rate_pct != null ? fmt.pct(f.base_rate_pct) + " %" : null}</RefCell>
        <RefCell k="Mod dur (spread)">{fmt.num(f.mod_duration)}</RefCell>
        <RefCell k="Convexity">{fmt.num(f.convexity, 4)}</RefCell>
        <RefCell k="PVBP ₽/bp (spread)">{fmt.num(f.pvbp, 4)}</RefCell>
      </div>
    </>
  );
}

// Калькулятор прошлых периодов: дата в прошлом + цена (пусто → close той даты)
// → SM/DM/y-idx/YTM как-на-дату: НКД/номинал — факт MOEX history, кривая as-of
// (архив котировок либо гибрид «реализованный факт + текущая кривая»).
function PastCalc({ isin }) {
  const [dateInput, setDateInput] = useState("");
  const [priceInput, setPriceInput] = useState("");
  const [res, setRes] = useState(null);
  useEffect(() => { setDateInput(""); setPriceInput(""); setRes(null); }, [isin]);

  const seqRef = useRef(0);
  const mut = useMutation({
    mutationFn: ({ date, price }) => fetchRepricePast(isin, { date, price }),
    onSuccess: (data, { seq }) => { if (seq === seqRef.current) setRes({ ok: true, data }); },
    onError: (e, { seq }) => {
      if (seq === seqRef.current && !(e instanceof UnauthorizedError))
        setRes({ ok: false, err: e?.message || "ошибка расчёта" });
    },
  });
  const mutate = mut.mutate;

  useEffect(() => {
    setRes(null);
    if (!dateInput) return;
    const today = new Date(Date.now() + 3 * 3600 * 1000).toISOString().slice(0, 10);
    if (dateInput >= today) return;
    const raw = priceInput.trim().replace(",", ".");
    const price = raw ? parseFloat(raw) : null;
    if (raw && (!Number.isFinite(price) || price <= 0 || price > 1000)) return;
    const t = setTimeout(() => mutate({ date: dateInput, price, seq: ++seqRef.current }), 400);
    return () => clearTimeout(t);
  }, [dateInput, priceInput, mutate]);

  const m = res?.ok ? res.data.metrics : null;
  return (
    <div className="past-calc">
      <div className="price-calc">
        <label className="pc-label" htmlFor="pc-past-date">Калькулятор прошлых периодов</label>
        <input
          id="pc-past-date" className="pc-input pc-date" type="date"
          max={new Date(Date.now() - 21 * 3600 * 1000).toISOString().slice(0, 10)}
          value={dateInput} onChange={(e) => setDateInput(e.target.value)}
        />
        <div className="pc-input-wrap">
          <input
            className="pc-input" inputMode="decimal"
            placeholder={res?.ok && res.data.close != null ? fmt.pct(res.data.close) : "close даты"}
            value={priceInput} onChange={(e) => setPriceInput(e.target.value)}
          />
          <span className="pc-unit">%</span>
        </div>
        <span className="pc-status">
          {mut.isPending ? "пересчёт…"
            : res?.ok ? `${res.data.stale_days > 0
                  ? `цена от ${fmt.date(res.data.trade_date)} (в этот день торгов не было) · `
                  : ""}кривая ${res.data.curve_mode === "market" ? "рыночная (архив)" : "факт+текущая"}`
            : res && !res.ok ? res.err
            : "дата в прошлом → метрики как-на-дату"}
        </span>
      </div>
      {m && (() => {
        // as-of карточка тоже уважает правило цены той даты (ручного свитчера
        // здесь нет — прошлые метрики смотрят в одном, авто-режиме)
        const hv = horizonView(m, "auto");
        return <ValCards v={hv.v} priceDate={dateInput} calc hzKey={hv.key} hzDate={hv.date} />;
      })()}
      {/* деградации входов на дату (доначисленный НКД, ex-coupon, отсутствие
          расписания) — без них цифра выглядит точнее, чем есть */}
      {m?.warnings?.length > 0 && <div className="warn-box">{m.warnings.join(" · ")}</div>}
    </div>
  );
}

// staleness: возраст каждого источника данных
function StaleChips({ m }) {
  // МСК-дата (UTC+3, без DST): иначе 00:00–03:00 МСК показывают «вчера»
  const today = new Date(Date.now() + 3 * 3600 * 1000).toISOString().slice(0, 10);
  const chip = (label, dateStr, liveOk) => {
    const stale = dateStr && dateStr < today;
    const cls = liveOk === false ? "off" : stale ? "stale" : "fresh";
    return (
      <span className={"stale-chip " + cls} key={label} title={dateStr || "нет даты"}>
        {label} {dateStr ? fmt.date(dateStr) : "—"}
      </span>
    );
  };
  return (
    <div className="stale-row">
      {chip("Цена", m.market_timestamp ? m.market_timestamp.slice(0, 10) : (m.calc_date || null), m.last_price_pct != null)}
      {chip("Ставки", m.rates_date || null)}
    </div>
  );
}

// Общий период графиков карточки — календарные дни. Раньше цене шли
// календарные (30), а истории спреда торговые (21) — окна не совпадали, и
// синхронный курсор это маскировал. Теперь оба чарта режутся одной датой.
const CHART_PERIODS = [[30, "1М"], [90, "3М"], [180, "6М"], [365, "1Г"]];
const isoBack = (days) => new Date(Date.now() - days * 864e5).toISOString().slice(0, 10);
// период карточки → код периода полноэкранной страницы /chart/:isin
const CHART_P_CODE = { 30: "1m", 90: "3m", 180: "6m", 365: "1y" };

// Графики карточки: цена (сверху) + динамика Y-IDX (снизу) на ОДНОМ периоде —
// видно, как двигались цена и спред в одинаковом окне. Рендерится в выездной
// панели слева от карточки (desktop) или инлайн в теле карточки (узкий экран).
function ChartsBody({ isin, period, setPeriod, onClose }) {
  // синхронный курсор: дата ховера одного графика — пунктирная вертикаль на другом
  const [hoverDate, setHoverDate] = useState(null);
  return (
    <div className="charts-body">
      <div className="charts-head">
        <span className="charts-title">Графики</span>
        <span className="sh-range" role="group" aria-label="Период графиков">
          {CHART_PERIODS.map(([cd, l]) => (
            <button key={cd} className={"sh-rbtn" + (period === cd ? " on" : "")}
              onClick={() => setPeriod(cd)}>{l}</button>
          ))}
        </span>
        {/* полноэкранный график в своей вкладке: гибкий период, zoom/pan */}
        <a className="btn charts-full" target="_blank" rel="noopener noreferrer"
          href={`${APP_BASENAME}/chart/${isin}?p=${CHART_P_CODE[period] || "1y"}`}
          title="Открыть на весь экран в новой вкладке">⤢</a>
        <button className="btn ob-close" onClick={onClose} aria-label="Закрыть графики">✕</button>
      </div>
      <div className="section-title">Цена · MOEX</div>
      <PriceChart isin={isin} periodDays={period} syncDate={hoverDate} onHoverDate={setHoverDate} />
      <div className="section-title">Динамика R-spread</div>
      <SpreadHistory isin={isin} kind="floater" board="TQCB" from={isoBack(period)}
        syncDate={hoverDate} onHoverDate={setHoverDate} />
    </div>
  );
}

function Content({ d, charts, hzSel = "auto", setHzSel = () => {} }) {
  const r = d.reference, m = d.market;
  const baseVal = d.valuation;

  // Калькулятор цены: пользователь вводит чистую цену → бэк пересчитывает все
  // метрики оценки под неё. Пусто → показываем рыночную (baseVal).
  // Второй вход — спред Y-IDX: обратная задача, бэк подбирает цену под спред,
  // цена подставляется в левое поле, карточка считается под неё.
  const [priceInput, setPriceInput] = useState("");
  const [spreadInput, setSpreadInput] = useState("");
  const [repriced, setRepriced] = useState(null);
  const [spreadErr, setSpreadErr] = useState("");
  // цена, подставленная решателем спреда: по ней эффект цены пропускает свой
  // (лишний и идентичный) запрос reprice
  const solvedPriceRef = useRef(null);
  const resetCalc = () => {
    setPriceInput(""); setSpreadInput(""); setRepriced(null); setSpreadErr("");
    solvedPriceRef.current = null;
  };
  useEffect(() => { resetCalc(); }, [r.isin]);

  // guard от гонки: поздний ответ по бумаге A не должен красить открытую бумагу B.
  // Также клеймим только результат ПОСЛЕДНЕГО запроса (быстрый ввод → out-of-order).
  const isinRef = useRef(r.isin);
  const seqRef = useRef(0);
  useEffect(() => { isinRef.current = r.isin; }, [r.isin]);

  // выбранный горизонт прайсинга (авто = правило цены с бэка). Считается до
  // эффектов: решателю спреда нужен РАЗРЕШЁННЫЙ ключ, а не "auto" — иначе он
  // подбирал бы цену под спред другого горизонта, чем показан в плитке.
  const hzView = horizonView(repriced || baseVal, hzSel);
  const v = hzView.v;
  const hzKey = hzView.key;

  const repriceMut = useMutation({
    mutationFn: ({ isin, p }) => repriceBond(isin, p),
    onSuccess: (data, { isin, seq }) => {
      if (isin === isinRef.current && seq === seqRef.current) setRepriced(data);
    },
    onError: (e) => { if (!(e instanceof UnauthorizedError)) setRepriced(null); },
  });
  const mutate = repriceMut.mutate;

  // Решатель спреда: bps → цена (бисекция на бэке). Цена уезжает в левое поле,
  // метрики ответа — те же, что дал бы reprice по этой цене.
  const spreadMut = useMutation({
    mutationFn: ({ isin, y, hz }) => priceFromSpread(isin, y, hz),
    onSuccess: (data, { isin, seq }) => {
      if (isin !== isinRef.current || seq !== seqRef.current) return;
      setSpreadErr("");
      setRepriced(data);
      const px = data.clean_price_pct;
      if (px != null) {
        solvedPriceRef.current = String(px);
        setPriceInput(String(px));
      }
    },
    onError: (e, { isin, seq }) => {
      if (isin !== isinRef.current || seq !== seqRef.current) return;
      if (e instanceof UnauthorizedError) return;
      setRepriced(null);
      setSpreadErr(e?.message || "спред недостижим");
    },
  });
  const solveSpread = spreadMut.mutate;

  useEffect(() => {
    const raw = priceInput.trim().replace(",", ".");
    // цену подставил решатель спреда — метрики уже пришли с ним, не дублируем запрос
    if (raw && raw === solvedPriceRef.current) return;
    if (!raw) { setRepriced(null); return; }
    const p = parseFloat(raw);
    if (!Number.isFinite(p) || p <= 0 || p > 1000) return;
    const t = setTimeout(() => mutate({ isin: r.isin, p, seq: ++seqRef.current }), 350);
    return () => clearTimeout(t);
  }, [priceInput, r.isin, mutate]);

  useEffect(() => {
    const raw = spreadInput.trim().replace(",", ".");
    if (!raw) { setSpreadErr(""); return; }
    const y = parseFloat(raw);
    if (!Number.isFinite(y) || y < -5000 || y > 20000) return;
    const t = setTimeout(() => solveSpread({ isin: r.isin, y, hz: hzKey, seq: ++seqRef.current }), 400);
    return () => clearTimeout(t);
  }, [spreadInput, r.isin, solveSpread, hzKey]);

  const isRepriced = repriced != null;
  const vRaw = repriced || baseVal;
  const warnings = [...(baseVal.warnings || []), ...(d.warnings || [])];
  const cf = d.cashflow || [];
  // МСК-дата (UTC+3, без DST): иначе 00:00–03:00 МСК показывают «вчера»
  const today = new Date(Date.now() + 3 * 3600 * 1000).toISOString().slice(0, 10);
  const coupons = cf.filter((c) => c.type === "COUPON"); // без погашения — оно в 20× больше
  // поток обрезан к оферте (pricing cut_at_offer): последняя выплата ≤ оферта < погашение.
  // Тогда таблица и метрики SM/z считаются к одному горизонту.
  const lastPay = cf.length ? cf[cf.length - 1].payment_date : null;
  const cutToOffer = !!(r.offer_date && lastPay && lastPay <= r.offer_date
    && (!r.maturity_date || r.offer_date < r.maturity_date));

  return (
    <>
      <StaleChips m={m} />
      <div className="price-calc">
        <label className="pc-label" htmlFor="pc-price">Калькулятор на дату поставки</label>
        <div className="pc-input-wrap">
          <input
            id="pc-price"
            className="pc-input"
            inputMode="decimal"
            placeholder={fmt.pct(baseVal.clean_price_pct) ?? "цена"}
            value={priceInput}
            onChange={(e) => {
              // ручная правка цены отвязывает поле спреда: его число относилось
              // к прежней цене и врало бы
              setSpreadInput(""); setSpreadErr(""); solvedPriceRef.current = null;
              setPriceInput(e.target.value);
            }}
          />
          <span className="pc-unit">%</span>
        </div>
        <div className="pc-input-wrap" title="Целевой R-spread: бэк подбирает чистую цену под него, цена встаёт в поле слева">
          <input
            id="pc-spread"
            className="pc-input pc-input-bps"
            inputMode="decimal"
            placeholder={fmt.bps(horizonView(baseVal, hzSel).v.yield_over_index_bps) ?? "спред"}
            value={spreadInput}
            onChange={(e) => setSpreadInput(e.target.value)}
          />
          <span className="pc-unit">bps</span>
        </div>
        {isRepriced && (
          <button className="pc-reset" onClick={resetCalc}>
            ↺ рынок {fmt.pct(baseVal.clean_price_pct)}%
          </button>
        )}
        <span className="pc-status">
          {repriceMut.isPending || spreadMut.isPending ? "пересчёт…"
            : spreadErr ? spreadErr
            : isRepriced ? (spreadInput ? "цена подобрана под спред" : "под введённую цену")
            : "рыночная цена"}
        </span>
      </div>
      <HorizonSwitch v={vRaw} active={hzView.key} onChange={setHzSel} />
      <ValCards v={v} priceDate={m.calc_date} calc={isRepriced}
        hzKey={hzView.key} hzDate={hzView.date} />

      {warnings.length > 0 && <div className="warn-box">{warnings.join(" · ")}</div>}

      <PastCalc isin={r.isin} />

      <div className="section-title">Референс</div>
      <div className="ref-grid">
        <RefCell k="Эмитент / имя">{r.short_name}</RefCell>
        <RefCell k="ISIN">{r.isin}</RefCell>
        <RefCell k="База">{baseLabel(r.base_rate_type)}</RefCell>
        <RefCell k="Формула купона">
          <CouponFormula base={r.base_rate_type} spreadBps={r.spread_bps} formula={r.formula}
            couponsPerYear={couponsPerYear(r.coupon_period_days, r.coupons_per_year)} />
        </RefCell>
        <RefCell k="Спред выпуска">{r.spread_bps != null ? "+" + r.spread_bps + " bps" : null}</RefCell>
        <RefCell k="Номинал">{fmt.num(r.face_value, 0) + " " + (r.face_unit || "")}</RefCell>
        <RefCell k="Размещение">{fmt.date(r.start_date)}</RefCell>
        <RefCell k="Погашение">{fmt.date(r.maturity_date)}</RefCell>
        {r.offer_date && <RefCell k={r.offer_kind === "call" ? "Оферта (эмитент/call)" : "Оферта (пут)"}>{fmt.date(r.offer_date) + (r.offer_type ? " · " + r.offer_type : "")}</RefCell>}
        <RefCell k="След. купон">{fmt.date(r.next_coupon_date)}</RefCell>
        <RefCell k="Период / год">{(r.coupon_period_days || "—") + " дн · " + (r.coupons_per_year || "—") + "×"}</RefCell>
        <RefCell k="НКД">{fmt.num(r.accrued_interest) + " ₽"}</RefCell>
        <RefCell k="Last price">{m.last_price_pct != null ? fmt.pct(m.last_price_pct) + " %" : "нет данных"}</RefCell>
      </div>

      {/* узкий экран: панель графиков не помещается — графики инлайн */}
      {charts && <div className="charts-inline">{charts}</div>}

      <FloaterSection f={d.floater} base={r.base_rate_type} />

      <div className="section-title">Купоны · факт + прогноз{cutToOffer ? " · до оферты" : ""}</div>
      <div className="chart-box"><CashflowChart items={coupons} today={today} /></div>

      <div className="section-title">Cashflow{cutToOffer ? " · до оферты " + fmt.date(r.offer_date) : ""} ({cf.length})</div>
      <div style={{ maxHeight: 340, overflow: "auto" }}>
        <table className="cf-table">
          <thead>
            <tr><th className="left">#</th><th className="left">Выплата</th><th>База %</th><th>Купон %</th><th>Сумма ₽</th><th className="left">Тип</th></tr>
          </thead>
          <tbody>
            {cf.map((c) => {
              const past = c.payment_date < today;
              const redem = c.type === "REDEMPTION";
              const offerBuyout = redem && cutToOffer && c.payment_date === r.offer_date;
              return (
                <tr key={c.number} className={redem ? "redemption" : past ? "past" : ""}>
                  <td className="left">{c.number}</td>
                  <td className="left">{fmt.date(c.payment_date)}</td>
                  <td>{fmt.pct(c.base_rate_pct)}</td>
                  <td>{fmt.pct(c.coupon_rate_pct)}</td>
                  <td>{fmt.num(c.amount_rub)}</td>
                  <td className="left">{offerBuyout ? "выкуп по оферте" : redem ? "погашение" : "купон"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

export default function Drawer({ isin, kind, autoOrderbook, sigVol, sigSide, sigPx, onClose }) {
  const isFixed = kind === "fixed";
  const navigate = useNavigate();
  const detailsQ = useQuery({
    queryKey: [isFixed ? "fixed-bond" : "bond", isin],
    queryFn: () => (isFixed ? fetchFixedDetails(isin) : fetchBondDetails(isin)),
    enabled: !!isin,
  });
  const data = detailsQ.data;
  const err = detailsQ.isError ? detailsQ.error?.message : null;
  const reduce = useReducedMotion();
  const panelRef = useRef(null);
  const closeRef = useRef(null);

  // Стакан: вторая панель слева от карточки. Открыт ПО УМОЛЧАНИЮ при открытии
  // бумаги и сбрасывается в открытое состояние при смене выпуска; закрыть можно
  // кнопкой СТАКАН или крестиком панели. Опт-аут — ?ob=0 в адресе (autoOrderbook).
  const [showOb, setShowOb] = useState(true);
  useEffect(() => { setShowOb(!!autoOrderbook); }, [isin, autoOrderbook]);
  // Горизонт прайсинга живёт ЗДЕСЬ, а не в Content: его уважают и плитки
  // карточки, и уровни стакана (соседняя панель) — иначе они считали бы разное.
  const [hzSel, setHzSel] = useState("auto");
  useEffect(() => { setHzSel("auto"); }, [isin]);
  const face = data?.reference?.face_value ?? null;
  // НКД на дату поставки — тот же, из которого бэк собрал грязную цену: подсветка
  // объёма сигнала в стакане должна считать деньги ровно как screener_core
  const accrued = data?.valuation?.accrued_settle_rub ?? data?.reference?.accrued_interest ?? 0;

  // Панель графиков (флоатеры): цена + DM на общем периоде, слева от карточки.
  // Открывается кнопкой ГРАФИКИ (аналог СТАКАНА). Один элемент рендерится и в
  // панели (desktop), и инлайн (узкий экран) — видимость переключает CSS.
  const [showCharts, setShowCharts] = useState(false);
  useEffect(() => { setShowCharts(false); }, [isin]);
  const [period, setPeriod] = useState(90);
  const charts = !isFixed && data && showCharts
    ? <ChartsBody isin={isin} period={period} setPeriod={setPeriod}
        onClose={() => setShowCharts(false)} />
    : null;

  // focus + esc + tab-trap
  useEffect(() => {
    if (!isin) return;
    requestAnimationFrame(() => closeRef.current?.focus());
    const onKey = (e) => {
      if (e.key === "Escape") { onClose(); return; }
      if (e.key === "Tab" && panelRef.current) {
        const f = panelRef.current.querySelectorAll('button, [href], input, [tabindex]:not([tabindex="-1"])');
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [isin, onClose]);

  const dur = reduce ? 0 : 0.2;

  return (
    <AnimatePresence>
      {isin && (
        <>
          <motion.div
            className="overlay"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            transition={{ duration: dur }}
            onClick={onClose}
          />
          <motion.aside
            ref={panelRef}
            className="drawer"
            role="dialog" aria-modal="true" aria-label={data?.reference?.short_name || isin}
            initial={{ x: reduce ? 0 : 40, opacity: reduce ? 1 : 0.4 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: reduce ? 0 : 30, opacity: 0 }}
            transition={{ duration: dur, ease: [0.2, 0.8, 0.2, 1] }}
          >
            {/* шапка в две строки: сверху ряд кнопок, под ним название и ISIN —
                длинные имена выпусков больше не жмут кнопки и не режутся */}
            <div className="drawer-head">
              <div className="dh-actions">
                <button
                  className={"btn ob-toggle" + (showOb ? " on" : "")}
                  onClick={() => setShowOb((v) => !v)}
                  aria-pressed={showOb}
                  title="Стакан выпуска"
                >СТАКАН</button>
                {!isFixed && (
                  <button
                    className={"btn ob-toggle" + (showCharts ? " on" : "")}
                    onClick={() => setShowCharts((v) => !v)}
                    aria-pressed={showCharts}
                    title="Цена и динамика DM на общем периоде"
                  >ГРАФИКИ</button>
                )}
                {!isFixed && (
                  // второй вход в те же графики, минуя панель карточки: полный
                  // экран в новой вкладке с гибким периодом и zoom/pan
                  <a
                    className="btn ob-toggle"
                    target="_blank" rel="noopener noreferrer"
                    href={`${APP_BASENAME}/chart/${isin}?p=${CHART_P_CODE[period] || "3m"}`}
                    title="Открыть графики на весь экран в новой вкладке"
                  >ГРАФИКИ ⤢</a>
                )}
                {!isFixed && (
                  <button
                    className="btn ob-toggle"
                    // только navigate: путь /audit/... без query → drawerIsin=null →
                    // штатная exit-анимация. onClose здесь нельзя — его setSearchParams
                    // батчится ПОСЛЕ navigate и перебивает переход
                    onClick={() => navigate(`/audit/${isin}`)}
                    title="Паспорт бумаги: провенанс данных, бэктест спеки фиксинга, развёртка PV"
                  >ПАСПОРТ</button>
                )}
                <button ref={closeRef} className="btn ob-toggle dh-close" onClick={onClose}>ЗАКРЫТЬ</button>
              </div>
              <div className="dh-title">
                <h2 id="d-name">{data?.reference?.short_name || data?.reference?.name || "—"}</h2>
                <span className="mono muted">
                  {isin}
                  {issueUrl(data?.reference?.cbonds_id, data?.reference?.moex_secid) && (
                    <a className="cat-ext"
                      title={data?.reference?.cbonds_id
                        ? "страница выпуска на cbonds" : "страница выпуска на MOEX"}
                      href={issueUrl(data.reference.cbonds_id, data.reference.moex_secid)}
                      target="_blank" rel="noopener noreferrer">↗</a>
                  )}
                </span>
                {/* срок бумаги прямо в шапке: погашение всегда, оферта — если она
                    есть (её дата и есть горизонт, к которому бумага прайсится) */}
                {data?.reference?.maturity_date && (
                  <span className="dh-dates mono">
                    <span title="Дата погашения">M {fmt.date(data.reference.maturity_date)}</span>
                    {data?.reference?.offer_date && (
                      <span className={"dh-offer" + (data.reference.offer_kind === "call" ? " dh-offer-call" : "")}
                        title={(data.reference.offer_kind === "call" ? "Call-оферта (опцион эмитента)" : "Пут-оферта")}>
                        {data.reference.offer_kind === "call" ? "C " : "P "}{fmt.date(data.reference.offer_date)}
                      </span>
                    )}
                  </span>
                )}
              </div>
            </div>
            <div className="drawer-body">
              {err ? <div className="warn-box">Ошибка: {err}</div>
                : !data ? <div className="loading">ЗАГРУЗКА</div>
                : isFixed ? <FixedCard d={data} />
                : <Content d={data} charts={charts} hzSel={hzSel} setHzSel={setHzSel} />}
            </div>
          </motion.aside>
          {charts && (
            <motion.aside
              className="charts-panel"
              key="charts-panel"
              initial={{ x: reduce ? 0 : 24, opacity: reduce ? 1 : 0 }}
              animate={{ x: 0, opacity: 1 }}
              exit={{ x: reduce ? 0 : 24, opacity: 0 }}
              transition={{ duration: dur, ease: [0.2, 0.8, 0.2, 1] }}
            >
              {charts}
            </motion.aside>
          )}
          {showOb && (
            <motion.aside
              className={"ob-panel" + (charts ? " ob-shifted" : "")}
              key="ob-panel"
              initial={{ x: reduce ? 0 : 24, opacity: reduce ? 1 : 0 }}
              animate={{ x: 0, opacity: 1 }}
              exit={{ x: reduce ? 0 : 24, opacity: 0 }}
              transition={{ duration: dur, ease: [0.2, 0.8, 0.2, 1] }}
            >
              <Orderbook isin={isin} kind={kind} face={face} accrued={accrued}
                sigVol={sigVol} sigSide={sigSide} sigPx={sigPx} horizon={hzSel}
                onClose={() => setShowOb(false)} />
            </motion.aside>
          )}
        </>
      )}
    </AnimatePresence>
  );
}
