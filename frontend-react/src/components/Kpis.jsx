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

// расширение спреда / цена выше fair = дорого = neg (красный); наоборот = pos (зелёный)
const richCls = (v) => (v == null ? "" : v > 0 ? "neg" : v < 0 ? "pos" : "");
// carry: положительный = несём над базой = pos (зелёный)
const carryCls = (v) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");

export default function Kpis({ bonds }) {
  const k = useMemo(() => {
    // единый DM: наш расчётный, иначе НРД discount margin
    const dmOf = (x) => (x.dm_bps != null ? x.dm_bps : x.discount_margin_bps);
    const dms = bonds.map(dmOf).filter((v) => v != null);
    const avgDm = dms.length ? Math.round(dms.reduce((s, v) => s + v, 0) / dms.length) : null;
    const medDm = dms.length ? Math.round(median(dms)) : null;
    const ru = bonds.filter((x) => x.base_rate_type === "RUONIA").length;
    const kr = bonds.filter((x) => x.base_rate_type === "KEYRATE").length;
    // mispricing: медиана цена − НРД fair, % (минус = торгуется ниже оценки = дёшево)
    const pvn = bonds.map((x) => x.price_vs_nrd_pct).filter((v) => v != null);
    const medPvn = pvn.length ? median(pvn) : null;
    // разброс DM: межквартиль p25–p75, bps (ширина возможностей рынка)
    const dmP25 = dms.length ? Math.round(quantile(dms, 0.25)) : null;
    const dmP75 = dms.length ? Math.round(quantile(dms, 0.75)) : null;
    // средний carry vs база по показанным бумагам, bps
    const carries = bonds.map((x) => x.carry_bps).filter((v) => v != null);
    const avgCarry = carries.length ? Math.round(carries.reduce((s, v) => s + v, 0) / carries.length) : null;
    return {
      count: bonds.length, avgDm, medDm, ru, kr,
      medPvn, dmP25, dmP75, hasDm: dms.length > 0, avgCarry, nCarry: carries.length,
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

  return (
    <section className="kpis">
      {cell("INSTRUMENTS", k.count || "—", { sub: `RUONIA ${k.ru} · KEYRATE ${k.kr}` })}
      {cell("MEDIAN DM", k.medDm, { unit: "BPS" })}
      {cell("AVG DM", k.avgDm, { unit: "BPS" })}
      {cell("MISPRICING", k.medPvn == null ? null : sgn(k.medPvn, 2), { unit: "MED vs НРД %", cls: richCls(k.medPvn) })}
      {cell("DM РАЗБРОС", k.hasDm ? `${k.dmP25}–${k.dmP75}` : null, { unit: "P25–P75 BPS" })}
      {cell("AVG CARRY", k.avgCarry == null ? null : sgn(k.avgCarry), { unit: "BPS", cls: carryCls(k.avgCarry), sub: k.nCarry ? `${k.nCarry} бумаг` : undefined })}
    </section>
  );
}
