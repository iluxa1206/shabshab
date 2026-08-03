import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { baseLabel, shortFormula, fmt, dmColor } from "../format.js";
import { fetchBondDetails, fetchFixedDetails, repriceBond, fetchRepricePast, UnauthorizedError, cbondsUrl } from "../api.js";
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
      <div className="fnote">
        Rate duration мала — цена устойчива к параллельному сдвигу ставки; основной риск —
        spread duration ({fmt.num(f.spread_duration_yrs, 2) ?? "—"} лет) на изменение кредитного спреда.
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
                  : ""}НКД ${fmt.num(res.data.accint)} ₽ · кривая ${res.data.curve_mode === "market" ? "рыночная (архив)" : "факт+текущая"}`
            : res && !res.ok ? res.err
            : "дата в прошлом → метрики как-на-дату"}
        </span>
      </div>
      {m && (
        <div className="val-cards val-cards-calc">
          <div className="vc">
            <div className="vc-label">Y-IDX</div>
            <div className="vc-val" style={{ color: dmColor(m.yield_over_index_bps).color }}>{fmt.bps(m.yield_over_index_bps) ?? "—"}<span className="vc-u"> bps</span></div>
            <div className="vc-sub">на дату · цена {fmt.pct(res.data.price)}%</div>
          </div>
          <div className="vc">
            <div className="vc-label">YTM (XIRR)</div>
            <div className="vc-val">{fmt.pct(m.yield_xirr_pct) ?? "—"}<span className="vc-u"> %</span></div>
            <div className="vc-sub">индекс {fmt.pct(m.index_yield_pct) ?? "—"}%</div>
          </div>
          <div className="vc">
            <div className="vc-label">DM (дисконтная)</div>
            <div className="vc-val" style={{ color: dmColor(m.disc_margin_bps).color }}>{fmt.bps(m.disc_margin_bps) ?? "—"}<span className="vc-u"> bps</span></div>
            <div className="vc-sub">вспом.</div>
          </div>
          <div className="vc">
            <div className="vc-label">SM (простая)</div>
            <div className="vc-val" style={{ color: dmColor(m.sm_bps).color }}>{fmt.bps(m.sm_bps) ?? "—"}<span className="vc-u"> bps</span></div>
            <div className="vc-sub">вспом.</div>
          </div>
        </div>
      )}
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

// Общий период графиков карточки: [календарные дни, подпись, торговые дни
// для истории спреда (~250 торговых в году)]
const CHART_PERIODS = [[30, "1М", 21], [90, "3М", 60], [180, "6М", 120], [365, "1Г", 250]];

// Графики карточки: цена (сверху) + динамика Y-IDX (снизу) на ОДНОМ периоде —
// видно, как двигались цена и спред в одинаковом окне. Рендерится в выездной
// панели слева от карточки (desktop) или инлайн в теле карточки (узкий экран).
function ChartsBody({ isin, period, setPeriod, tradingDays, onClose }) {
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
        <button className="btn ob-close" onClick={onClose} aria-label="Закрыть графики">✕</button>
      </div>
      <div className="section-title">Цена · MOEX</div>
      <PriceChart isin={isin} periodDays={period} syncDate={hoverDate} onHoverDate={setHoverDate} />
      <div className="section-title">Динамика Y-IDX</div>
      <SpreadHistory isin={isin} kind="floater" board="TQCB" days={tradingDays}
        syncDate={hoverDate} onHoverDate={setHoverDate} />
    </div>
  );
}

function Content({ d, charts }) {
  const r = d.reference, m = d.market;
  const baseVal = d.valuation;

  // Калькулятор цены: пользователь вводит чистую цену → бэк пересчитывает все
  // метрики оценки под неё. Пусто → показываем рыночную (baseVal).
  const [priceInput, setPriceInput] = useState("");
  const [repriced, setRepriced] = useState(null);
  useEffect(() => { setPriceInput(""); setRepriced(null); }, [r.isin]);

  // guard от гонки: поздний ответ по бумаге A не должен красить открытую бумагу B.
  // Также клеймим только результат ПОСЛЕДНЕГО запроса (быстрый ввод → out-of-order).
  const isinRef = useRef(r.isin);
  const seqRef = useRef(0);
  useEffect(() => { isinRef.current = r.isin; }, [r.isin]);

  const repriceMut = useMutation({
    mutationFn: ({ isin, p }) => repriceBond(isin, p),
    onSuccess: (data, { isin, seq }) => {
      if (isin === isinRef.current && seq === seqRef.current) setRepriced(data);
    },
    onError: (e) => { if (!(e instanceof UnauthorizedError)) setRepriced(null); },
  });
  const mutate = repriceMut.mutate;

  useEffect(() => {
    const raw = priceInput.trim().replace(",", ".");
    if (!raw) { setRepriced(null); return; }
    const p = parseFloat(raw);
    if (!Number.isFinite(p) || p <= 0 || p > 1000) return;
    const t = setTimeout(() => mutate({ isin: r.isin, p, seq: ++seqRef.current }), 350);
    return () => clearTimeout(t);
  }, [priceInput, r.isin, mutate]);

  const isRepriced = repriced != null;
  const v = repriced || baseVal;
  const dc = dmColor(v.yield_over_index_bps);
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
        <label className="pc-label" htmlFor="pc-price">Калькулятор цены</label>
        <div className="pc-input-wrap">
          <input
            id="pc-price"
            className="pc-input"
            inputMode="decimal"
            placeholder={fmt.pct(baseVal.clean_price_pct) ?? "цена"}
            value={priceInput}
            onChange={(e) => setPriceInput(e.target.value)}
          />
          <span className="pc-unit">%</span>
        </div>
        {isRepriced && (
          <button className="pc-reset" onClick={() => { setPriceInput(""); setRepriced(null); }}>
            ↺ рынок {fmt.pct(baseVal.clean_price_pct)}%
          </button>
        )}
        <span className="pc-status">
          {repriceMut.isPending ? "пересчёт…"
            : isRepriced ? "под введённую цену"
            : "рыночная цена"}
        </span>
      </div>
      <div className={"val-cards" + (isRepriced ? " val-cards-calc" : "")}>
        <div className="vc">
          <div className="vc-label">Y-IDX</div>
          <div className="vc-val" style={{ color: dc.color }}>{fmt.bps(v.yield_over_index_bps) ?? "—"}<span style={{ fontSize: 12, color: "var(--mut)" }}> bps</span></div>
          <div className="vc-sub">IRR − индекс · основная</div>
        </div>
        <div className="vc">
          <div className="vc-label">YTM (XIRR)</div>
          <div className="vc-val">{fmt.pct(v.yield_xirr_pct) ?? "—"}<span style={{ fontSize: 12, color: "var(--mut)" }}> %</span></div>
          <div className="vc-sub">индекс {fmt.pct(v.index_yield_pct) ?? "—"}%</div>
        </div>
        <div className="vc">
          <div className="vc-label">Грязная цена</div>
          <div className="vc-val">{fmt.num(v.dirty_price_rub) ?? "—"}<span style={{ fontSize: 12, color: "var(--mut)" }}> ₽</span></div>
          <div className="vc-sub">clean {fmt.pct(v.clean_price_pct) ?? "—"}% + НКД</div>
        </div>
        <div className="vc">
          <div className="vc-label">DM / SM</div>
          <div className="vc-val" style={{ color: dmColor(v.disc_margin_bps).color }}>{fmt.bps(v.disc_margin_bps) ?? "—"}<span style={{ fontSize: 12, color: "var(--mut)" }}> bps</span></div>
          <div className="vc-sub">вспом. · SM {fmt.bps(v.sm_bps ?? v.dm_bps) ?? "—"} bps</div>
        </div>
      </div>

      {warnings.length > 0 && <div className="warn-box">{warnings.join(" · ")}</div>}

      <PastCalc isin={r.isin} />

      <div className="section-title">Референс</div>
      <div className="ref-grid">
        <RefCell k="Эмитент / имя">{r.short_name}</RefCell>
        <RefCell k="ISIN">{r.isin}</RefCell>
        <RefCell k="База">{baseLabel(r.base_rate_type)}</RefCell>
        <RefCell k="Формула купона">{shortFormula(r.formula)}</RefCell>
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

export default function Drawer({ isin, kind, autoOrderbook, onClose }) {
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
  const face = data?.reference?.face_value ?? null;

  // Панель графиков (флоатеры): цена + DM на общем периоде, слева от карточки.
  // Открывается кнопкой ГРАФИКИ (аналог СТАКАНА). Один элемент рендерится и в
  // панели (desktop), и инлайн (узкий экран) — видимость переключает CSS.
  const [showCharts, setShowCharts] = useState(false);
  useEffect(() => { setShowCharts(false); }, [isin]);
  const [period, setPeriod] = useState(90);
  const tradingDays = CHART_PERIODS.find(([cd]) => cd === period)?.[2] ?? 60;
  const charts = !isFixed && data && showCharts
    ? <ChartsBody isin={isin} period={period} setPeriod={setPeriod} tradingDays={tradingDays}
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
            <div className="drawer-head">
              <div className="dh-title">
                <h2 id="d-name">{data?.reference?.short_name || data?.reference?.name || "—"}</h2>
                <span className="mono muted">
                  {isin}
                  <a className="cat-ext" title="страница выпуска на cbonds"
                    href={cbondsUrl(isin, data?.reference?.cbonds_id)}
                    target="_blank" rel="noopener noreferrer">↗</a>
                </span>
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
                  <button
                    className="btn ob-toggle"
                    // только navigate: путь /audit/... без query → drawerIsin=null →
                    // штатная exit-анимация. onClose здесь нельзя — его setSearchParams
                    // батчится ПОСЛЕ navigate и перебивает переход
                    onClick={() => navigate(`/audit/${isin}`)}
                    title="Паспорт бумаги: провенанс данных, бэктест спеки фиксинга, развёртка PV"
                  >ПАСПОРТ</button>
                )}
              </div>
              <button ref={closeRef} className="btn ob-toggle" onClick={onClose}>ЗАКРЫТЬ</button>
            </div>
            <div className="drawer-body">
              {err ? <div className="warn-box">Ошибка: {err}</div>
                : !data ? <div className="loading">ЗАГРУЗКА</div>
                : isFixed ? <FixedCard d={data} />
                : <Content d={data} charts={charts} />}
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
              <Orderbook isin={isin} kind={kind} face={face} onClose={() => setShowOb(false)} />
            </motion.aside>
          )}
        </>
      )}
    </AnimatePresence>
  );
}
