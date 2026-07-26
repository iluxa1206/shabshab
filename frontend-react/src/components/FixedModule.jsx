import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchFixed } from "../api.js";
import { fmt, dmColor } from "../format.js";
import IssuerFilter from "./IssuerFilter.jsx";

const RT = ["AAA", "AA", "A", "BBB", "BB", "B", "NR"];
const RTCOLOR = {
  AAA: "var(--rt-aaa)", AA: "var(--rt-aa)", A: "var(--rt-a)", BBB: "var(--rt-bbb)",
  BB: "var(--rt-bb)", B: "var(--rt-b)", NR: "var(--mut-2)",
};
const D = () => <span className="dash">—</span>;
const median = (a) => {
  if (!a.length) return null;
  const s = a.slice().sort((x, y) => x - y);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const mln = (v) => (v == null ? null : v >= 1e9 ? (v / 1e9).toFixed(1) + "млрд" : (v / 1e6).toFixed(1));

// колонки: key, label, sub, доступ к значению, рендер ячейки
const COLS = [
  { key: "name", label: "INSTRUMENT", align: "left",
    cell: (b) => (
      <td className="left" key="name">
        <span className={"fx-cls fx-" + b.cls}>{b.cls === "ofz" ? "ОФЗ" : "КОРП"}</span>
        {b.name}
      </td>
    ) },
  { key: "last_price_pct", label: "PRICE", sub: "CLN %", align: "num", sep: true,
    get: (b) => b.last_price_pct,
    cell: (b) => <td className={"num col-sep" + (b.price_stale ? " px-stale" : "")} key="p"
      title={b.price_stale ? "пред. закрытие — нет сделок сегодня" : undefined}>{fmt.pct(b.last_price_pct) ?? <D />}</td> },
  { key: "ytm", label: "YTM", sub: "%", align: "num",
    get: (b) => b.ytm, cell: (b) => <td className="num" key="y">{b.ytm == null ? <D /> : fmt.pct(b.ytm)}</td> },
  { key: "delta_ytm", label: "Δ YTM", sub: "D/D пп", align: "num",
    get: (b) => b.delta_ytm,
    cell: (b) => <td className="num" style={b.delta_ytm != null ? dmColor(-b.delta_ytm) : undefined} key="dy">{b.delta_ytm == null ? <D /> : (b.delta_ytm > 0 ? "+" : "") + fmt.num(b.delta_ytm, 2)}</td> },
  { key: "cur_yield", label: "CUR Y", sub: "%", align: "num",
    get: (b) => b.cur_yield, cell: (b) => <td className="num" key="cy">{b.cur_yield == null ? <D /> : fmt.pct(b.cur_yield)}</td> },
  { key: "g_spread_bps", label: "G-SPRD", sub: "vs ОФЗ", align: "num",
    get: (b) => b.g_spread_bps,
    cell: (b) => <td className="num" style={b.g_spread_bps != null ? dmColor(b.g_spread_bps) : undefined} key="g">{b.g_spread_bps == null ? <D /> : fmt.bps(b.g_spread_bps)}</td> },
  { key: "z_spread_bps", label: "Z-SPRD", sub: "bps", align: "num",
    get: (b) => b.z_spread_bps,
    cell: (b) => <td className="num" style={b.z_spread_bps != null ? dmColor(b.z_spread_bps) : undefined} key="z">{b.z_spread_bps == null ? <D /> : fmt.bps(b.z_spread_bps)}</td> },
  { key: "mod_dur", label: "DUR", sub: "МОД, лет", align: "num",
    get: (b) => b.mod_dur, cell: (b) => <td className="num" key="d">{b.mod_dur == null ? <D /> : fmt.num(b.mod_dur, 2)}</td> },
  { key: "convexity", label: "CONV", sub: "выпукл.", align: "num",
    get: (b) => b.convexity, cell: (b) => <td className="num" key="cx">{b.convexity == null ? <D /> : fmt.num(b.convexity, 1)}</td> },
  { key: "rating", label: "РЕЙТИНГ", sub: "", align: "num",
    get: (b) => (b.rating ? RT.indexOf(b.rating) : 99),
    cell: (b) => <td className="num" key="r">{b.rating
      ? <span className="fx-rt" style={{ color: RTCOLOR[b.rating] }}>{b.rating}</span> : <D />}</td> },
  { key: "coupon_pct", label: "COUPON", sub: "%", align: "num",
    get: (b) => b.coupon_pct, cell: (b) => <td className="num" key="c">{b.coupon_pct == null ? <D /> : fmt.pct(b.coupon_pct)}</td> },
  { key: "maturity_date", label: "MATURITY", sub: "", align: "num",
    get: (b) => b.maturity_date, cell: (b) => <td className="num" key="m">{b.maturity_date ? fmt.date(b.maturity_date) : <D />}</td> },
  { key: "val_today", label: "ОБОРОТ", sub: "млн ₽", align: "num",
    get: (b) => b.val_today, cell: (b) => <td className="num mut" key="v">{mln(b.val_today) ?? <D />}</td> },
];

function Kpi({ label, value, unit }) {
  return (
    <div className="kpi">
      <span className="kpi-label">{label}</span>
      <span className="kpi-val">{value ?? "—"}</span>
      {unit && <span className="kpi-unit">{unit}</span>}
    </div>
  );
}

export default function FixedModule({ onOpen }) {
  const q = useQuery({ queryKey: ["fixed"], queryFn: fetchFixed, staleTime: 60_000, refetchInterval: 120_000 });
  const [clsF, setClsF] = useState("all");     // all | ofz | corp
  const [query, setQuery] = useState("");
  const [emittersSel, setEmittersSel] = useState([]);
  const [ratingsSel, setRatingsSel] = useState([]);
  const [sort, setSort] = useState({ key: "val_today", dir: "desc" });

  const all = q.data?.items || [];
  const issuers = useMemo(() => {
    const m = new Map();
    for (const b of all) { const k = b.issuer || b.name; if (k) m.set(k, (m.get(k) || 0) + 1); }
    return [...m.entries()].map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count);
  }, [all]);
  const rows = useMemo(() => {
    let r = all;
    if (clsF !== "all") r = r.filter((b) => b.cls === clsF);
    if (ratingsSel.length) { const set = new Set(ratingsSel); r = r.filter((b) => set.has(b.rating || "NR")); }
    if (emittersSel.length) { const set = new Set(emittersSel); r = r.filter((b) => set.has(b.issuer || b.name)); }
    if (query.trim()) {
      const s = query.trim().toLowerCase();
      r = r.filter((b) => (b.name || "").toLowerCase().includes(s) || (b.isin || "").toLowerCase().includes(s));
    }
    const col = COLS.find((c) => c.key === sort.key);
    const get = col?.get || ((b) => b[sort.key]);
    const dir = sort.dir === "asc" ? 1 : -1;
    return r.slice().sort((a, b) => {
      const va = get(a), vb = get(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      return va > vb ? dir : va < vb ? -dir : 0;
    });
  }, [all, clsF, ratingsSel, emittersSel, query, sort]);

  const k = useMemo(() => {
    const ys = rows.map((b) => b.ytm).filter((v) => v != null);
    const gs = rows.map((b) => b.g_spread_bps).filter((v) => v != null);
    return {
      n: rows.length,
      ofz: rows.filter((b) => b.cls === "ofz").length,
      corp: rows.filter((b) => b.cls === "corp").length,
      medY: ys.length ? median(ys).toFixed(1) : null,
      medG: gs.length ? Math.round(median(gs)) : null,
    };
  }, [rows]);

  const onSort = (key) => setSort((s) => s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" });

  return (
    <section className="fixed-mod">
      <section className="kpis">
        <Kpi label="ВЫПУСКОВ" value={k.n || "—"} />
        <Kpi label="ОФЗ / КОРП" value={`${k.ofz} / ${k.corp}`} />
        <Kpi label="MEDIAN YTM" value={k.medY} unit="%" />
        <Kpi label="MEDIAN G-SPRD" value={k.medG} unit="BPS vs ОФЗ" />
      </section>

      <div className="fx-toolbar">
        <span className="seg">
          {[["all", "Все"], ["ofz", "ОФЗ"], ["corp", "Корпораты"]].map(([v, l]) => (
            <button key={v} className={"seg-btn" + (clsF === v ? " active" : "")} onClick={() => setClsF(v)}>{l}</button>
          ))}
        </span>
        <span className="fx-rt-filter">
          {RT.map((rt) => (
            <button key={rt} className={"fx-rt-chip" + (ratingsSel.includes(rt) ? " on" : "")}
              style={ratingsSel.includes(rt) ? { background: RTCOLOR[rt], borderColor: RTCOLOR[rt] } : { color: RTCOLOR[rt] }}
              onClick={() => setRatingsSel((s) => s.includes(rt) ? s.filter((x) => x !== rt) : [...s, rt])}>{rt}</button>
          ))}
        </span>
        <IssuerFilter issuers={issuers} selected={emittersSel}
          onToggle={(name) => setEmittersSel((s) => s.includes(name) ? s.filter((x) => x !== name) : [...s, name])}
          onClear={() => setEmittersSel([])} />
        <input className="fx-search" placeholder="Поиск ISIN / имя" value={query}
          onChange={(e) => setQuery(e.target.value)} />
        <span className="fx-count">{rows.length}</span>
      </div>

      <div className="fx-table-wrap">
        {q.isPending ? <div className="an-empty">загрузка…</div>
          : q.error ? <div className="an-empty">ошибка загрузки</div>
          : !rows.length ? <div className="an-empty">нет данных (прогрев метрик — до минуты после старта)</div>
          : (
            <table className="grid fx-grid">
              <thead>
                <tr>
                  {COLS.map((c) => (
                    <th key={c.key}
                      className={(c.align === "left" ? "left " : "num ") + (c.sep ? "col-sep " : "") + (sort.key === c.key ? "sorted " + (sort.dir === "asc" ? "asc" : "") : "")}
                      role="button" onClick={() => onSort(c.key)}>
                      {c.label}{c.sub && <small>{c.sub}</small>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((b) => (
                  <tr key={b.isin} tabIndex={0} role="button"
                    onClick={(e) => onOpen(b.isin, e.currentTarget, "fixed")}
                    onKeyDown={(e) => { if (e.key === "Enter") onOpen(b.isin, e.currentTarget, "fixed"); }}>
                    {COLS.map((c) => c.cell(b))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div>
    </section>
  );
}
