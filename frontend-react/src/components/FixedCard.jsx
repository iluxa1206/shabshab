import { fmt, dmColor } from "../format.js";
import PriceChart from "./PriceChart.jsx";

const D = "—";
function Cell({ k, children }) {
  return (
    <div className="ref-cell">
      <div className="ref-k">{k}</div>
      <div className="ref-v">{children == null || children === "" ? <span className="dash">{D}</span> : children}</div>
    </div>
  );
}

// Карточка фикс-бумаги: метрики к погашению + график цены + поток платежей.
export default function FixedCard({ d }) {
  const r = d.reference || {}, m = d.metrics || {}, mk = d.market || {};
  const cf = d.cashflow || [];
  const gc = m.g_spread_bps != null ? dmColor(m.g_spread_bps) : {};
  const putHint = m.put_date ? ` (к оферте ${fmt.date(m.put_date)})` : "";

  return (
    <>
      <div className="section-title">Оценка к погашению{putHint}</div>
      <div className="val-cards">
        <div className="vc">
          <div className="vc-label">YTM</div>
          <div className="vc-val">{m.ytm_pct != null ? fmt.pct(m.ytm_pct) : D}<span className="vc-u"> %</span></div>
          <div className="vc-sub">тек. доходн. {m.cur_yield_pct != null ? fmt.pct(m.cur_yield_pct) + "%" : D}</div>
        </div>
        <div className="vc">
          <div className="vc-label">G-спред</div>
          <div className="vc-val" style={gc}>{m.g_spread_bps != null ? fmt.bps(m.g_spread_bps) : D}<span className="vc-u"> bps</span></div>
          <div className="vc-sub">к КБД ОФЗ</div>
        </div>
        <div className="vc">
          <div className="vc-label">Z-спред</div>
          <div className="vc-val" style={m.z_spread_bps != null ? dmColor(m.z_spread_bps) : {}}>{m.z_spread_bps != null ? fmt.bps(m.z_spread_bps) : D}<span className="vc-u"> bps</span></div>
          <div className="vc-sub">над кривой ОФЗ</div>
        </div>
        <div className="vc">
          <div className="vc-label">Мод. дюрация</div>
          <div className="vc-val">{m.mod_dur != null ? fmt.num(m.mod_dur, 2) : D}<span className="vc-u"> лет</span></div>
          <div className="vc-sub">DV01 {m.dv01 != null ? fmt.num(m.dv01, 2) + " ₽" : D}</div>
        </div>
      </div>

      <div className="ref-grid">
        <Cell k="Цена">{mk.last_price_pct != null ? fmt.pct(mk.last_price_pct) + " %" : null}{mk.price_stale ? " (пред.)" : ""}</Cell>
        <Cell k="Dirty">{mk.dirty_rub != null ? fmt.num(mk.dirty_rub) + " ₽" : null}</Cell>
        <Cell k="НКД">{mk.accrued_rub != null ? fmt.num(mk.accrued_rub) + " ₽" : null}</Cell>
        <Cell k="Купон">{r.coupon_pct != null ? fmt.pct(r.coupon_pct) + " %" : null}</Cell>
        <Cell k="Погашение">{r.maturity_date ? fmt.date(r.maturity_date) : null}</Cell>
        <Cell k="Convexity">{m.convexity != null ? fmt.num(m.convexity, 1) : null}</Cell>
        <Cell k="Дюрация Маколея">{m.mac_dur != null ? fmt.num(m.mac_dur, 2) + " лет" : null}</Cell>
        <Cell k="Номинал">{r.face != null ? fmt.num(r.face, 0) + " ₽" : null}</Cell>
        <Cell k="Оборот">{mk.val_today != null ? (mk.val_today / 1e6).toFixed(1) + " млн ₽" : null}</Cell>
        <Cell k="SECID">{r.secid}</Cell>
      </div>

      <div className="section-title">Цена · MOEX</div>
      <PriceChart isin={r.isin} secid={r.secid} board={r.board} />

      <div className="section-title">Поток платежей ({cf.length})</div>
      <div style={{ maxHeight: 340, overflow: "auto" }}>
        <table className="cf-table">
          <thead><tr><th className="left">Дата</th><th>Тип</th><th className="num">Ставка</th><th className="num">Сумма, ₽</th></tr></thead>
          <tbody>
            {cf.map((c, i) => (
              <tr key={i}>
                <td className="left">{fmt.date(c.date)}</td>
                <td>{c.type === "COUPON" ? "купон" : c.type === "MATURITY" ? "погашение" : "аморт."}</td>
                <td className="num">{c.rate_pct != null ? fmt.pct(c.rate_pct) + "%" : D}</td>
                <td className="num">{fmt.num(c.amount)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
