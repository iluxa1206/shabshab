import { useMemo } from "react";

const median = (arr) => {
  if (!arr.length) return null;
  const s = arr.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

// линейно-интерполированный квантиль (q в [0,1])
const quantile = (arr, q) => {
  if (!arr.length) return null;
  const s = arr.slice().sort((a, b) => a - b);
  const pos = (s.length - 1) * q;
  const b = Math.floor(pos);
  const rest = pos - b;
  return s[b + 1] !== undefined ? s[b] + rest * (s[b + 1] - s[b]) : s[b];
};

// Компактные KPI для нижнего статусбара (вместо верхнего блока-сетки).
export default function KpisInline({ bonds }) {
  const k = useMemo(() => {
    // Y-IDX (IRR − доходность индекса) — первичная метрика; DM — вспом.
    const yis = bonds.map((x) => x.yield_over_index_bps).filter((v) => v != null);
    const avgYi = yis.length ? Math.round(yis.reduce((s, v) => s + v, 0) / yis.length) : null;
    const medYi = yis.length ? Math.round(median(yis)) : null;
    const dms = bonds.map((x) => x.disc_margin_bps).filter((v) => v != null);
    const medDm = dms.length ? Math.round(median(dms)) : null;
    const ru = bonds.filter((x) => x.base_rate_type === "RUONIA").length;
    const kr = bonds.filter((x) => x.base_rate_type === "KEYRATE").length;
    // разброс Y-IDX: межквартиль p25–p75, bps (ширина возможностей рынка)
    const yiP25 = yis.length ? Math.round(quantile(yis, 0.25)) : null;
    const yiP75 = yis.length ? Math.round(quantile(yis, 0.75)) : null;
    // breadth: тон рынка за день по CHG (delta_to_prev_close, % от номинала)
    const deltas = bonds.map((x) => x.delta_to_prev_close).filter((v) => v != null);
    const up = deltas.filter((v) => v > 0).length;
    const down = deltas.filter((v) => v < 0).length;
    const medChg = deltas.length ? median(deltas) : null;
    return {
      avgYi, medYi, medDm, ru, kr,
      yiP25, yiP75, hasYi: yis.length > 0,
      up, down, medChg, nChg: deltas.length,
    };
  }, [bonds]);

  const sgn = (v, digits = 0) => (v > 0 ? "+" : "") + v.toFixed(digits);

  const cell = (label, value, title) => (
    <span className="status-cell kpi-cell" title={title}>
      {label} <span className="kpi-cell-val">{value ?? "—"}</span>
    </span>
  );

  return (
    <>
      {cell("RUONIA", k.ru, "бумаг с базой RUONIA")}
      {cell("КС", k.kr, "бумаг с базой KEYRATE")}
      <span className="status-cell kpi-cell" title="тон рынка за день по CHG (к закрытию)">
        ДВИЖ <span className="kpi-cell-val pos">▲{k.nChg ? k.up : "—"}</span>
        <span className="kpi-cell-val neg">▼{k.nChg ? k.down : "—"}</span>
        {k.medChg != null && <span className="kpi-cell-sub">med {sgn(k.medChg, 2)}</span>}
      </span>
      {cell("MED Y-IDX", k.medYi, "медианный Y-IDX (IRR − индекс), б.п.")}
      {cell("AVG Y-IDX", k.avgYi, "средний Y-IDX (IRR − индекс), б.п.")}
      {cell("Y-IDX P25–P75", k.hasYi ? `${k.yiP25}–${k.yiP75}` : null, "межквартильный разброс Y-IDX, б.п.")}
      {cell("MED DM", k.medDm, "медианный discount margin (вспом.), б.п.")}
    </>
  );
}
