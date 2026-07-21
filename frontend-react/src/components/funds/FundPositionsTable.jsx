import { useMemo } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fmt, dmColor } from "../../format.js";
import { putFundPosition, UnauthorizedError } from "../../api.js";
import { invalidateFund } from "../../queries.js";
import { conv } from "./ccy.js";

const D = () => <span className="dash">—</span>;

// Колонки: одна таблица на все классы, DASH где метрика неприменима.
// cell(p, ctx): ctx = {rate, sign, totalMv} — валюта отображения и вес позиции.
const COLS = [
  { key: "name", label: "INSTRUMENT", align: "left",
    cell: (p) => (
      <td className="left" key="name">
        <div className="bond-name">
          {p.name || p.isin}
          {p.warn && <span className="warn-badge" title={p.warn}>{p.warn.startsWith("оценка") ? "ОФЕРТА" : "NO PX"}</span>}
        </div>
        <div className="mono muted" style={{ fontSize: 11 }}>{p.isin}</div>
      </td>
    ) },
  { key: "label", label: "КЛАСС", align: "left",
    cell: (p) => <td className="left" key="label" style={{ fontSize: 12 }}>{p.label}</td> },
  { key: "ccy", label: "CCY",
    cell: (p) => <td key="ccy"><span className="badge">{p.ccy}</span></td> },
  { key: "qty", label: "QTY", sub: "ШТ", align: "num",
    cell: (p) => <td className="num" key="qty">{fmt.num(p.qty, 0)}</td> },
  { key: "price_pct", label: "PRICE", sub: "CLN %", align: "num",
    cell: (p) => <td className="num" key="price_pct">{fmt.pct(p.price_pct) ?? <D />}</td> },
  { key: "mv_rub", label: "MV", sub: "МЛН", align: "num", sep: true,
    cell: (p, ctx) => <td className="num col-sep" key="mv_rub">{p.mv_rub != null ? fmt.num(conv(p.mv_rub, ctx.rate) / 1e6, 2) : <D />}</td> },
  { key: "wgt", label: "WGT", sub: "% MV", align: "num",
    cell: (p, ctx) => <td className="num" key="wgt">{p.mv_rub != null && ctx.totalMv ? fmt.pct(p.mv_rub / ctx.totalMv * 100, 1) : <D />}</td> },
  { key: "ytm_pct", label: "YTM", sub: "%", align: "num",
    cell: (p) => <td className="num" key="ytm_pct">{fmt.pct(p.ytm_pct) ?? <D />}</td> },
  { key: "dm_bps", label: "DM", sub: "BPS", align: "num",
    cell: (p) => <td className="num" key="dm_bps" style={dmColor(p.dm_bps)}>{fmt.bps(p.dm_bps) ?? <D />}</td> },
  { key: "z_bps", label: "Z", sub: "BPS", align: "num",
    cell: (p) => <td className="num" key="z_bps" style={dmColor(p.z_bps)}>{fmt.bps(p.z_bps) ?? <D />}</td> },
  { key: "g_spread_bps", label: "G-SPR", sub: "BPS", align: "num",
    cell: (p) => <td className="num" key="g_spread_bps">{fmt.bps(p.g_spread_bps) ?? <D />}</td> },
  { key: "carry_bps", label: "CARRY", sub: "BPS", align: "num",
    cell: (p) => {
      const cls = p.carry_bps == null ? "" : p.carry_bps >= 0 ? "pos" : "neg";
      return <td className={"num " + cls} key="carry_bps">{fmt.bps(p.carry_bps) ?? <D />}</td>;
    } },
  { key: "mod_dur", label: "DUR", sub: "MOD", align: "num",
    cell: (p) => <td className="num" key="mod_dur">{fmt.num(p.mod_dur) ?? <D />}</td> },
  { key: "dv01_rub", label: "DV01", align: "num",
    cell: (p, ctx) => <td className="num" key="dv01_rub">{p.dv01_rub != null ? fmt.num(conv(p.dv01_rub, ctx.rate), 0) : <D />}</td> },
  { key: "maturity", label: "MATURITY",
    cell: (p) => <td className="num" key="maturity" style={{ fontSize: 12 }}>{fmt.date(p.maturity) ?? <D />}</td> },
];

export default function FundPositionsTable({ code, positions, rate, sign, clsFilter, sort, onSort }) {
  const qc = useQueryClient();
  const totalMv = useMemo(
    () => positions.reduce((a, p) => a + (p.mv_rub || 0), 0), [positions]);

  const rows = useMemo(() => {
    let rs = positions;
    if (clsFilter.length) rs = rs.filter((p) => clsFilter.includes(p.cls));
    const { key, dir } = sort;
    const m = dir === "asc" ? 1 : -1;
    const val = (p) => (key === "wgt" ? p.mv_rub : p[key]);
    return [...rs].sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      if (typeof x === "string") return x.localeCompare(y) * m;
      return (x - y) * m;
    });
  }, [positions, clsFilter, sort]);

  const removeMut = useMutation({
    mutationFn: (isin) => putFundPosition(code, isin, 0),
    onSuccess: () => invalidateFund(qc, code),
    onError: (e) => { if (!(e instanceof UnauthorizedError)) alert(e.message); },
  });

  const removePos = (isin) => {
    if (!confirm(`Убрать ${isin} из портфеля?`)) return;
    removeMut.mutate(isin);
  };

  if (!positions.length) {
    return <div className="funds-state muted">Позиции не загружены — «Снапшот CSV» вверху.</div>;
  }

  const ctx = { rate, sign, totalMv };
  return (
    <div className="table-wrap fund-table-wrap">
      <table className="grid">
        <thead>
          <tr>
            {COLS.map((c) => (
              <th key={c.key}
                className={(c.align === "left" ? "left " : "") +
                  (c.sep ? "col-sep " : "") +
                  (sort.key === c.key ? "sorted " + sort.dir : "")}
                onClick={() => onSort(c.key)}>
                {c.label}<small>{c.key === "mv_rub" || c.key === "dv01_rub" ? (c.sub ? c.sub + " " : "") + sign : c.sub}</small>
              </th>
            ))}
            <th aria-label="Действия" />
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.isin} className={p.mv_rub == null ? "muted" : ""}>
              {COLS.map((c) => c.cell(p, ctx))}
              <td className="num">
                <button className="row-x" title="Убрать позицию"
                  onClick={(e) => { e.stopPropagation(); removePos(p.isin); }}>×</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
