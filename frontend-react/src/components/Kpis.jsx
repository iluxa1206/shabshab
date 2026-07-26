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

// carry: положительный = несём над базой = pos (зелёный)
const carryCls = (v) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");

export default function Kpis({ bonds }) {
  const k = useMemo(() => {
    // DM (discount margin, Fabozzi) — наш расчёт
    const dms = bonds.map((x) => x.disc_margin_bps).filter((v) => v != null);
    const avgDm = dms.length ? Math.round(dms.reduce((s, v) => s + v, 0) / dms.length) : null;
    const medDm = dms.length ? Math.round(median(dms)) : null;
    const ru = bonds.filter((x) => x.base_rate_type === "RUONIA").length;
    const kr = bonds.filter((x) => x.base_rate_type === "KEYRATE").length;
    // разброс DM: межквартиль p25–p75, bps (ширина возможностей рынка)
    const dmP25 = dms.length ? Math.round(quantile(dms, 0.25)) : null;
    const dmP75 = dms.length ? Math.round(quantile(dms, 0.75)) : null;
    // средний carry vs база по показанным бумагам, bps
    const carries = bonds.map((x) => x.carry_bps).filter((v) => v != null);
    const avgCarry = carries.length ? Math.round(carries.reduce((s, v) => s + v, 0) / carries.length) : null;
    // breadth: тон рынка за день по CHG (delta_to_prev_close, % от номинала)
    const deltas = bonds.map((x) => x.delta_to_prev_close).filter((v) => v != null);
    const up = deltas.filter((v) => v > 0).length;
    const down = deltas.filter((v) => v < 0).length;
    const medChg = deltas.length ? median(deltas) : null;
    return {
      count: bonds.length, avgDm, medDm, ru, kr,
      dmP25, dmP75, hasDm: dms.length > 0, avgCarry, nCarry: carries.length,
      up, down, medChg, nChg: deltas.length,
    };
  }, [bonds]);

  const cell = (label, value, opts = {}) => (
    <div className="kpi">
      <span className="kpi-label">{label}</span>
      <span className={"kpi-val" + (opts.sm ? " sm" : "") + (opts.cls ? " " + opts.cls : "")}>
        {value ?? "—"}
      </span>
      {opts.unit && <span className="kpi-unit">{opts.unit}</span>}
      {opts.sub && <span className="kpi-sub">{opts.sub}</span>}
    </div>
  );

  const sgn = (v, digits = 0) => (v > 0 ? "+" : "") + v.toFixed(digits);

  const breadth = k.nChg ? (
    <><span className="pos">▲{k.up}</span> <span className="kpi-sep">·</span> <span className="neg">▼{k.down}</span></>
  ) : null;

  return (
    <section className="kpis">
      {cell("INSTRUMENTS", k.count || "—", { sub: `RUONIA ${k.ru} · KEYRATE ${k.kr}` })}
      {cell("ДВИЖЕНИЕ", breadth, { unit: "ЗА ДЕНЬ", sub: k.medChg == null ? undefined : `med ${sgn(k.medChg, 2)}` })}
      {cell("MEDIAN DM", k.medDm, { unit: "BPS" })}
      {cell("AVG DM", k.avgDm, { unit: "BPS" })}
      {cell("DM РАЗБРОС", k.hasDm ? `${k.dmP25}–${k.dmP75}` : null, { unit: "P25–P75 BPS" })}
      {cell("AVG CARRY", k.avgCarry == null ? null : sgn(k.avgCarry), { unit: "BPS", cls: carryCls(k.avgCarry), sub: k.nCarry ? `${k.nCarry} бумаг` : undefined })}
    </section>
  );
}
