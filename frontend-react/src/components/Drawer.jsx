import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { fmt, dmColor, vsFairColor } from "../format.js";
import { fetchBondDetails, fetchFunds, putFundPosition, repriceBond, UnauthorizedError } from "../api.js";
import { invalidateFund } from "../queries.js";
import CashflowChart from "./CashflowChart.jsx";

// Добавление бумаги в фонд прямо из карточки: select фонда + qty → PUT position.
// Список фондов — из общего кэша ['funds'] (делится с модулем «Фонды»).
function AddToFund({ isin }) {
  const qc = useQueryClient();
  const { data: funds } = useQuery({ queryKey: ["funds"], queryFn: fetchFunds });
  const [code, setCode] = useState("");
  const [qty, setQty] = useState("");
  const [msg, setMsg] = useState(null); // {ok, text}

  useEffect(() => {
    if (!code && funds?.length) setCode(funds[0].code);
  }, [funds, code]);

  useEffect(() => { setMsg(null); setQty(""); }, [isin]);

  const putMut = useMutation({
    mutationFn: ({ fund, n }) => putFundPosition(fund, isin, n),
    onSuccess: (_d, { fund, n }) => {
      setMsg({ ok: true, text: `${fmt.num(n, 0)} шт → ${fund}` });
      setQty("");
      invalidateFund(qc, fund);
    },
    onError: (e) => { if (!(e instanceof UnauthorizedError)) setMsg({ ok: false, text: e.message }); },
  });
  const busy = putMut.isPending;

  if (!funds || !funds.length) return null;

  const submit = (e) => {
    e.preventDefault();
    const n = parseFloat(String(qty).replace(/\s/g, "").replace(",", "."));
    if (!Number.isFinite(n) || n <= 0) { setMsg({ ok: false, text: "Количество?" }); return; }
    setMsg(null);
    putMut.mutate({ fund: code, n });
  };

  return (
    <>
      <div className="section-title">В фонд</div>
      <form className="add-fund-row" onSubmit={submit}>
        <select value={code} onChange={(e) => setCode(e.target.value)}>
          {funds.map((f) => <option key={f.code} value={f.code}>{f.code}</option>)}
        </select>
        <input placeholder="Кол-во, шт" value={qty} onChange={(e) => setQty(e.target.value)} />
        <button className="btn" type="submit" disabled={busy || !qty.trim()}>Добавить</button>
        {msg && <span className={"admin-msg " + (msg.ok ? "admin-ok" : "admin-err")}>{msg.text}</span>}
      </form>
      <div className="fnote">Заменяет qty позиции в фонде (не суммирует).</div>
    </>
  );
}

function RefCell({ k, children }) {
  return (
    <div className="ref-cell">
      <div className="ref-k">{k}</div>
      <div className="ref-v">{children == null || children === "" ? <span className="dash">—</span> : children}</div>
    </div>
  );
}

function NrdSection({ n, v }) {
  if (!n) {
    return (
      <>
        <div className="section-title">НРД Ценовой центр</div>
        <div className="nrd-note">
          <b>Данные НРД недоступны.</b> Источник не настроен или нет оценки по бумаге.
          Заполни <code>NRD_LOGIN</code> и <code>NRD_APIKEY</code> в <code>.env</code>.
        </div>
      </>
    );
  }
  const nrdPrice = n.fair_value_pct ?? n.nrd_price_pct;
  const priceLbl = n.fair_value_pct != null ? "Fair value НРД" : "Цена НРД (VWAP)";
  const vf = vsFairColor(n.price_vs_nrd_pct);
  const vfLabel = n.price_vs_nrd_pct == null ? "—" : n.price_vs_nrd_pct < 0 ? "рынок дешевле НРД" : "рынок дороже НРД";
  const ndc = dmColor(n.discount_margin_bps), odc = dmColor(v?.disc_margin_bps);
  const nsc = dmColor(n.simple_margin_bps), osc = dmColor(v?.sm_bps ?? v?.dm_bps);
  const r = n.ratings || {};
  const ragMap = { akra: "АКРА", expert_ra: "Эксперт РА", nkr: "НКР", nra: "НРА" };
  const lq = n.liquidity || {};

  return (
    <>
      <div className="section-title">НРД Ценовой центр</div>
      <div className="val-cards">
        <div className="vc">
          <div className="vc-label">{priceLbl}</div>
          <div className="vc-val">{fmt.pct(nrdPrice) ?? "—"}<span style={{ fontSize: 12, color: "var(--mut)" }}> %</span></div>
          <div className="vc-sub">close {fmt.pct(n.nrd_close_pct) ?? "—"}%</div>
        </div>
        <div className="vc">
          <div className="vc-label">vs рынок</div>
          <div className="vc-val" style={{ color: vf.color }}>{fmt.signed(n.price_vs_nrd_pct) ?? "—"}<span style={{ fontSize: 12, color: "var(--mut)" }}> %</span></div>
          <div className="vc-sub">{vfLabel}</div>
        </div>
        <div className="vc">
          <div className="vc-label">SM НРД vs наш</div>
          <div className="vc-val">
            <span style={{ color: nsc.color }}>{fmt.bps(n.simple_margin_bps) ?? "—"}</span>
            <span style={{ fontSize: 12, color: "var(--mut-2)" }}> / </span>
            <span style={{ color: osc.color }}>{fmt.bps(v?.sm_bps ?? v?.dm_bps) ?? "—"}</span>
          </div>
          <div className="vc-sub">simple margin, bps</div>
        </div>
        <div className="vc">
          <div className="vc-label">DM НРД vs наш</div>
          <div className="vc-val">
            <span style={{ color: ndc.color }}>{fmt.bps(n.discount_margin_bps) ?? "—"}</span>
            <span style={{ fontSize: 12, color: "var(--mut-2)" }}> / </span>
            <span style={{ color: odc.color }}>{v?.disc_margin_bps != null ? fmt.bps(v.disc_margin_bps) : "—"}</span>
          </div>
          <div className="vc-sub">discount margin, bps</div>
        </div>
      </div>
      <div className="ref-grid">
        <RefCell k="Duration / Mod">{`${fmt.num(n.duration) ?? "—"} / ${fmt.num(n.mod_duration) ?? "—"}`}</RefCell>
        <RefCell k="Convexity / PVBP">{`${fmt.num(n.convexity) ?? "—"} / ${fmt.num(n.pvbp) ?? "—"}`}</RefCell>
        <RefCell k="Z-spread">{n.z_spread_bps != null ? n.z_spread_bps + " bps" : null}</RefCell>
        <RefCell k="G-spread">{n.g_spread_bps != null ? n.g_spread_bps + " bps" : null}</RefCell>
        <RefCell k="Nominal margin">{n.nominal_margin_bps != null ? fmt.bps(n.nominal_margin_bps) + " bps" : null}</RefCell>
        <RefCell k="Simple margin">{n.simple_margin_bps != null ? fmt.bps(n.simple_margin_bps) + " bps" : null}</RefCell>
        <RefCell k="YTM НРД">{n.yield_maturity_pct != null ? fmt.pct(n.yield_maturity_pct) + " %" : null}</RefCell>
        <RefCell k="YTM к close">{n.ytm_close_pct != null ? fmt.pct(n.ytm_close_pct) + " %" : null}</RefCell>
        <RefCell k="Current yield">{n.current_yield_pct != null ? fmt.pct(n.current_yield_pct) + " %" : null}</RefCell>
        <RefCell k="База купона">{n.base_coupon_index}</RefCell>
        <RefCell k="Тип купона">{n.coupon_type}</RefCell>
        <RefCell k="Дата расчёта НРД">{n.nrd_calc_date}</RefCell>
      </div>
      {Object.keys(ragMap).some((kk) => r[kk]) && (
        <div className="rating-row">
          {Object.entries(ragMap).map(([kk, label]) =>
            r[kk] ? (
              <span className="rating-badge" key={kk}>
                <span className="rb-k">{label}</span><span className="rb-v">{r[kk]}</span>
              </span>
            ) : null
          )}
        </div>
      )}
      {Object.keys(lq).length > 0 && (
        <div className="ref-grid">
          {Object.entries(lq).map(([kk, val]) => <RefCell k={kk} key={kk}>{fmt.num(val)}</RefCell>)}
        </div>
      )}
    </>
  );
}

// специфика флоатера: rate duration мала (до рефиксинга), spread duration = весь
// кредитный риск до погашения; carry — платит ли бумага больше стоимости базы
function FloaterSection({ f, base }) {
  if (!f) return null;
  const cc = f.carry_bps == null ? { color: "var(--mut-2)" } : dmColor(f.carry_bps);
  const baseLbl = base === "RUONIA" ? "RUONIA" : "КС";
  return (
    <>
      <div className="section-title">Флоатер-риск</div>
      <div className="val-cards">
        <div className="vc">
          <div className="vc-label">Spread duration</div>
          <div className="vc-val">{fmt.num(f.spread_duration_yrs, 2) ?? "—"}<span className="vc-u"> лет</span></div>
          <div className="vc-sub">риск ΔDM/Δz (до погашения)</div>
        </div>
        <div className="vc">
          <div className="vc-label">Rate duration</div>
          <div className="vc-val">{fmt.num(f.rate_duration_yrs, 2) ?? "—"}<span className="vc-u"> лет</span></div>
          <div className="vc-sub">до рефиксинга {f.days_to_refix != null ? f.days_to_refix + " дн" : "—"}</div>
        </div>
        <div className="vc">
          <div className="vc-label">Carry vs {baseLbl}</div>
          <div className="vc-val" style={{ color: cc.color }}>{fmt.bps(f.carry_bps) ?? "—"}<span className="vc-u"> bps</span></div>
          <div className="vc-sub">купон-дох − база</div>
        </div>
      </div>
      <div className="ref-grid">
        <RefCell k="Текущий купон">{f.current_coupon_pct != null ? fmt.pct(f.current_coupon_pct) + " %" : null}</RefCell>
        <RefCell k={`Уровень ${baseLbl}`}>{f.base_rate_pct != null ? fmt.pct(f.base_rate_pct) + " %" : null}</RefCell>
        <RefCell k="Breakeven базы">{f.breakeven_base_pct != null ? fmt.pct(f.breakeven_base_pct) + " %" : null}</RefCell>
        <RefCell k="Mod duration">{fmt.num(f.mod_duration)}</RefCell>
        <RefCell k="Convexity">{fmt.num(f.convexity, 4)}</RefCell>
        <RefCell k="PVBP">{fmt.num(f.pvbp, 4)}</RefCell>
      </div>
      <div className="fnote">
        Если {baseLbl} опустится ниже <b>{f.breakeven_base_pct != null ? fmt.pct(f.breakeven_base_pct) + "%" : "—"}</b>,
        купон станет меньше текущей купонной доходности. Rate duration мала — цена устойчива
        к параллельному сдвигу ставки; основной риск — spread duration ({fmt.num(f.spread_duration_yrs, 2) ?? "—"} лет) на изменение кредитного спреда.
      </div>
    </>
  );
}

// staleness: возраст каждого источника данных
function StaleChips({ m, n }) {
  const today = new Date().toISOString().slice(0, 10);
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
      {chip("НРД", n?.nrd_calc_date || null)}
    </div>
  );
}

function Content({ d }) {
  const r = d.reference, m = d.market;
  const baseVal = d.valuation;

  // Калькулятор цены: пользователь вводит чистую цену → бэк пересчитывает все
  // метрики оценки под неё. Пусто → показываем рыночную (baseVal).
  const [priceInput, setPriceInput] = useState("");
  const [repriced, setRepriced] = useState(null);
  useEffect(() => { setPriceInput(""); setRepriced(null); }, [r.isin]);

  const repriceMut = useMutation({
    mutationFn: (p) => repriceBond(r.isin, p),
    onSuccess: (data) => setRepriced(data),
    onError: (e) => { if (!(e instanceof UnauthorizedError)) setRepriced(null); },
  });
  const mutate = repriceMut.mutate;

  useEffect(() => {
    const raw = priceInput.trim().replace(",", ".");
    if (!raw) { setRepriced(null); return; }
    const p = parseFloat(raw);
    if (!Number.isFinite(p) || p <= 0 || p > 1000) return;
    const t = setTimeout(() => mutate(p), 350);
    return () => clearTimeout(t);
  }, [priceInput, r.isin, mutate]);

  const isRepriced = repriced != null;
  const v = repriced || baseVal;
  const dc = dmColor(v.disc_margin_bps);
  const warnings = [...(baseVal.warnings || []), ...(d.warnings || [])];
  const cf = d.cashflow || [];
  const today = new Date().toISOString().slice(0, 10);
  const coupons = cf.filter((c) => c.type === "COUPON"); // без погашения — оно в 20× больше
  // поток обрезан к оферте (pricing cut_at_offer): последняя выплата ≤ оферта < погашение.
  // Тогда таблица и метрики SM/z считаются к одному горизонту.
  const lastPay = cf.length ? cf[cf.length - 1].payment_date : null;
  const cutToOffer = !!(r.offer_date && lastPay && lastPay <= r.offer_date
    && (!r.maturity_date || r.offer_date < r.maturity_date));

  return (
    <>
      <StaleChips m={m} n={d.nrd} />
      <div className="price-calc">
        <label className="pc-label">Калькулятор цены</label>
        <div className="pc-input-wrap">
          <input
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
          <div className="vc-label">DM (discount)</div>
          <div className="vc-val" style={{ color: dc.color }}>{fmt.bps(v.disc_margin_bps) ?? "—"}<span style={{ fontSize: 12, color: "var(--mut)" }}> bps</span></div>
          <div className="vc-sub">discount margin · основная</div>
        </div>
        <div className="vc">
          <div className="vc-label">SM (simple)</div>
          <div className="vc-val" style={{ color: dmColor(v.sm_bps ?? v.dm_bps).color }}>{fmt.bps(v.sm_bps ?? v.dm_bps) ?? "—"}<span style={{ fontSize: 12, color: "var(--mut)" }}> bps</span></div>
          <div className="vc-sub">simple margin · вспом.</div>
        </div>
        <div className="vc">
          <div className="vc-label">Dirty price</div>
          <div className="vc-val">{fmt.num(v.dirty_price_rub) ?? "—"}<span style={{ fontSize: 12, color: "var(--mut)" }}> ₽</span></div>
          <div className="vc-sub">clean {fmt.pct(v.clean_price_pct) ?? "—"}% + НКД</div>
        </div>
        <div className="vc">
          <div className="vc-label">YTM (XIRR)</div>
          <div className="vc-val">{fmt.pct(v.yield_xirr_pct) ?? "—"}<span style={{ fontSize: 12, color: "var(--mut)" }}> %</span></div>
          <div className="vc-sub">индекс {fmt.pct(v.index_yield_pct) ?? "—"}% · спред {fmt.bps(v.yield_over_index_bps) ?? "—"}</div>
        </div>
      </div>

      {warnings.length > 0 && <div className="warn-box">{warnings.join(" · ")}</div>}

      <div className="section-title">Референс</div>
      <div className="ref-grid">
        <RefCell k="Эмитент / имя">{r.short_name}</RefCell>
        <RefCell k="ISIN">{r.isin}</RefCell>
        <RefCell k="База">{r.base_rate_type}</RefCell>
        <RefCell k="Формула купона">{r.formula}</RefCell>
        <RefCell k="Спред выпуска">{r.spread_bps != null ? "+" + r.spread_bps + " bps" : null}</RefCell>
        <RefCell k="Номинал">{fmt.num(r.face_value, 0) + " " + (r.face_unit || "")}</RefCell>
        <RefCell k="Размещение">{fmt.date(r.start_date)}</RefCell>
        <RefCell k="Погашение">{fmt.date(r.maturity_date)}</RefCell>
        {r.offer_date && <RefCell k="Оферта">{fmt.date(r.offer_date) + (r.offer_type ? " · " + r.offer_type : "")}</RefCell>}
        <RefCell k="След. купон">{fmt.date(r.next_coupon_date)}</RefCell>
        <RefCell k="Период / год">{(r.coupon_period_days || "—") + " дн · " + (r.coupons_per_year || "—") + "×"}</RefCell>
        <RefCell k="НКД">{fmt.num(r.accrued_interest) + " ₽"}</RefCell>
        <RefCell k="Last price">{m.last_price_pct != null ? fmt.pct(m.last_price_pct) + " %" : "нет данных"}</RefCell>
      </div>

      <AddToFund isin={r.isin} />

      <FloaterSection f={d.floater} base={r.base_rate_type} />

      <div className="section-title">Купоны · факт + прогноз{cutToOffer ? " · до оферты" : ""}</div>
      <div className="chart-box"><CashflowChart items={coupons} today={today} /></div>

      <NrdSection n={d.nrd} v={v} />

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

export default function Drawer({ isin, onClose }) {
  const detailsQ = useQuery({
    queryKey: ["bond", isin],
    queryFn: () => fetchBondDetails(isin),
    enabled: !!isin,
  });
  const data = detailsQ.data;
  const err = detailsQ.isError ? detailsQ.error?.message : null;
  const reduce = useReducedMotion();
  const panelRef = useRef(null);
  const closeRef = useRef(null);

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
                <h2 id="d-name">{data?.reference?.short_name || "—"}</h2>
                <span className="mono muted">{isin}</span>
              </div>
              <button ref={closeRef} className="btn" onClick={onClose}>CLOSE</button>
            </div>
            <div className="drawer-body">
              {err ? <div className="warn-box">Ошибка: {err}</div>
                : !data ? <div className="loading">LOADING</div>
                : <Content d={data} />}
            </div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}
