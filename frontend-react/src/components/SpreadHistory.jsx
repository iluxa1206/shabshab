import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchSpreadHistory } from "../api.js";
import { fmt } from "../format.js";
import { linearScale, linTicks, linePath, GridY, XTicks } from "../charts/index.js";

const RANGES = [[60, "3м"], [120, "6м"], [250, "1г"]];

// Динамика спреда: DM (флоатер) / g-спред (фикс) по историч. дневным ценам.
// Оценка (историч. цена × текущая модель), не точный историч. спред.
export default function SpreadHistory({ isin, kind, secid, board }) {
  const [days, setDays] = useState(120);
  const isFixed = kind === "fixed";
  const key = isFixed ? "g_spread_bps" : "dm_bps";
  const label = isFixed ? "G-спред" : "DM";

  const q = useQuery({
    queryKey: ["spread-hist", isin, kind, days],
    queryFn: () => fetchSpreadHistory(isin, { kind, secid, board, days }),
    staleTime: 300000,
  });

  const pts = (q.data?.points || []).filter((p) => p[key] != null);

  const ctl = (
    <span className="sh-range">
      {RANGES.map(([d, l]) => (
        <button key={d} className={"sh-rbtn" + (days === d ? " on" : "")} onClick={() => setDays(d)}>{l}</button>
      ))}
    </span>
  );

  if (q.isPending) return <div className="sh-box">{ctl}<div className="an-empty">загрузка…</div></div>;
  if (pts.length < 2) return <div className="sh-box">{ctl}<div className="an-empty">мало истории для графика</div></div>;

  const W = 460, H = 200, pad = { l: 46, r: 12, t: 12, b: 26 };
  const ys = pts.map((p) => p[key]);
  let ymin = Math.min(...ys), ymax = Math.max(...ys);
  if (ymin === ymax) { ymin -= 1; ymax += 1; }
  const sx = linearScale([0, pts.length - 1], [pad.l, W - pad.r]);
  const sy = linearScale([ymin, ymax], [H - pad.b, pad.t]);
  const path = linePath(pts.map((p, i) => ({ x: i, y: p[key] })), (d) => sx(d.x), (d) => sy(d.y));
  const last = pts[pts.length - 1], first = pts[0];
  const chg = last[key] - first[key];

  const nx = Math.min(6, pts.length);
  const xstep = Math.max(1, Math.floor((pts.length - 1) / (nx - 1)));
  const xticks = [];
  for (let i = 0; i < pts.length; i += xstep) xticks.push({ x: sx(i), label: pts[i].date.slice(5) });

  return (
    <div className="sh-box">
      <div className="sh-top">
        <span className="sh-val">{label} <b>{fmt.bps(last[key])}</b> bps
          <span className={"sh-chg " + (chg >= 0 ? "up" : "down")}>{chg >= 0 ? "▲" : "▼"} {fmt.bps(Math.abs(chg))} за период</span>
        </span>
        {ctl}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="an-svg" role="img" aria-label={`динамика ${label}`}>
        <GridY ticks={linTicks(ymin, ymax, 4)} y={sy} x1={pad.l} x2={W - pad.r}
          lineClass="an-grid" textClass="an-axis" label={(v) => Math.round(v)} />
        <XTicks ticks={xticks} y={H - pad.b + 14} textClass="an-axis" />
        <path d={path} className="sh-line" fill="none" />
        <circle cx={sx(pts.length - 1)} cy={sy(last[key])} r={3} className="sh-dot" />
      </svg>
      <div className="sh-note">Оценка: историч. цена × текущая модель (кривая/срок фиксированы).</div>
    </div>
  );
}
