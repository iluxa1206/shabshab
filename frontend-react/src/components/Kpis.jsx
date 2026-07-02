import { useMemo } from "react";
import { fmt } from "../format.js";

const median = (arr) => {
  if (!arr.length) return null;
  const s = arr.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

// динамика спреда: расширение (+) = цена упала = neg (красный); сужение = pos (зелёный)
const dzCls = (v) => (v == null ? "" : v > 0 ? "neg" : v < 0 ? "pos" : "");

export default function Kpis({ bonds }) {
  const k = useMemo(() => {
    // единый DM: наш расчётный, иначе НРД discount margin
    const dmOf = (x) => (x.dm_bps != null ? x.dm_bps : x.discount_margin_bps);
    const dms = bonds.map(dmOf).filter((v) => v != null);
    const avgDm = dms.length ? Math.round(dms.reduce((s, v) => s + v, 0) / dms.length) : null;
    const medDm = dms.length ? Math.round(median(dms)) : null;
    const ru = bonds.filter((x) => x.base_rate_type === "RUONIA").length;
    const kr = bonds.filter((x) => x.base_rate_type === "KEYRATE").length;
    // динамика z-спреда рынка (медиана по бумагам с историей)
    const dod = bonds.map((x) => x.delta_z_dod).filter((v) => v != null);
    const mom = bonds.map((x) => x.delta_z_mom).filter((v) => v != null);
    const medDod = dod.length ? Math.round(median(dod)) : null;
    const medMom = mom.length ? Math.round(median(mom)) : null;
    // repricing: сколько бумаг сдвинули z за день > 15bps (сигнал переоценки рынка)
    const reprice = dod.filter((v) => Math.abs(v) >= 15).length;
    return { count: bonds.length, avgDm, medDm, ru, kr, medDod, medMom, reprice, hasDod: dod.length > 0, hasMom: mom.length > 0 };
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

  const dz = (v, has) => (has ? (v > 0 ? "+" : "") + v : "нет истории");

  return (
    <section className="kpis">
      {cell("INSTRUMENTS", k.count || "—", { sub: `RUONIA ${k.ru} · KEYRATE ${k.kr}` })}
      {cell("MEDIAN DM", k.medDm, { unit: "BPS" })}
      {cell("AVG DM", k.avgDm, { unit: "BPS" })}
      {cell("Δz D/D", k.medDod == null ? null : dz(k.medDod, k.hasDod), { unit: "MED BPS", cls: dzCls(k.medDod), sm: !k.hasDod })}
      {cell("Δz M/M", k.medMom == null ? null : dz(k.medMom, k.hasMom), { unit: "MED BPS", cls: dzCls(k.medMom), sm: !k.hasMom })}
      {cell("REPRICED D/D", k.hasDod ? k.reprice : null, { unit: "≥15BPS" })}
    </section>
  );
}
