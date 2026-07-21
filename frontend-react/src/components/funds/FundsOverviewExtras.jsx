import { useQuery } from "@tanstack/react-query";
import { fmt, DASH } from "../../format.js";
import { fetchFundsCalendar, fetchBenchmarks } from "../../api.js";
import { conv } from "./ccy.js";

const EV_LABEL = {
  coupon: "купон", amort: "амортизация", maturity: "погашение", put: "оферта",
};

// Сравнение фондов: метрики строками, фонды колонками
export function CompareTable({ funds, rate, sign }) {
  if (funds.length < 2) return null;
  const mln = (v) => (v == null ? DASH : fmt.num(conv(v, rate) / 1e6, 2));
  const rows = [
    ["NAV, млн " + sign, (f) => mln(f.nav_rub)],
    ["MV gross, млн " + sign, (f) => mln(f.mv_rub)],
    ["Leverage", (f) => (f.leverage_gross != null ? fmt.num(f.leverage_gross, 2) + "×" : DASH)],
    ["DV01, " + sign, (f) => (f.dv01_rub != null ? fmt.num(conv(f.dv01_rub, rate), 0) : DASH)],
    ["Net carry, млн " + sign + "/год", (f) => mln(f.net_carry_rub)],
    ["Funding, млн " + sign, (f) => mln(f.repo_rub)],
    ["Ставка РЕПО WA, %", (f) => fmt.pct(f.repo_rate_wa) ?? DASH],
    ["Позиций", (f) => f.n_positions],
  ];
  return (
    <div className="admin-sec">
      <div className="fund-sec-title">Сравнение фондов</div>
      <table className="admin-table compare-table">
        <thead>
          <tr><th className="left">Метрика</th>{funds.map((f) => <th key={f.code}>{f.code}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map(([label, get]) => (
            <tr key={label}>
              <td className="left muted">{label}</td>
              {funds.map((f) => <td className="num" key={f.code}>{get(f)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Бенчмарки: RGBITR / RUCBTRNS / RUCEU — last close + 1д/30д/YTD
export function BenchmarksRow() {
  const { data: bm } = useQuery({ queryKey: ["benchmarks"], queryFn: () => fetchBenchmarks() });
  if (!bm) return null;
  const cell = (v) => (
    <td className={"num " + (v == null ? "" : v >= 0 ? "pos" : "neg")}>
      {v != null ? fmt.signed(v) + "%" : DASH}
    </td>
  );
  return (
    <div className="admin-sec">
      <div className="fund-sec-title">Бенчмарки</div>
      <table className="admin-table">
        <thead>
          <tr><th className="left">Индекс</th><th>Close</th><th>1д</th><th>30д</th><th>YTD</th></tr>
        </thead>
        <tbody>
          {Object.entries(bm).map(([code, v]) => (
            <tr key={code}>
              <td className="left">{v.label}</td>
              <td className="num">{v.last ? fmt.num(v.last.close) : DASH}</td>
              {cell(v.perf_1d)}{cell(v.perf_30d)}{cell(v.perf_ytd)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Календарь событий по всем фондам: купоны/амортизации/погашения/оферты
export function CalendarSection({ rate, sign }) {
  const { data: cal } = useQuery({ queryKey: ["fundsCalendar", 90], queryFn: () => fetchFundsCalendar(90) });
  const items = cal?.items || [];
  if (!cal || !items.length) return null;
  return (
    <div className="admin-sec">
      <div className="fund-sec-title">Календарь событий · 90 дн</div>
      <div style={{ maxHeight: 260, overflow: "auto" }}>
        <table className="admin-table">
          <thead>
            <tr><th className="left">Дата</th><th>Фонд</th><th className="left">Бумага</th>
              <th className="left">Событие</th><th>Сумма, {sign}</th></tr>
          </thead>
          <tbody>
            {items.map((e, i) => (
              <tr key={i} className={e.type === "put" || e.type === "maturity" ? "ev-hot" : ""}>
                <td className="left mono">{fmt.date(e.date)}</td>
                <td style={{ textAlign: "center" }}>{e.fund}</td>
                <td className="left">{e.name}<span className="mono muted" style={{ fontSize: 10, marginLeft: 6 }}>{e.isin}</span></td>
                <td className="left">{EV_LABEL[e.type] || e.type}</td>
                <td className="num">{e.amount_rub != null ? fmt.num(conv(e.amount_rub, rate), 0) : DASH}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
