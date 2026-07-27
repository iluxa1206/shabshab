import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchSpreadHistory } from "../api.js";
import { fmt } from "../format.js";
import { linearScale, linTicks, linePath, GridY, XTicks, useNearestHover, Tooltip } from "../charts/index.js";

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

  // ВСЕ хуки до early-return (правила хуков). data/sx считаем безопасно даже пустыми.
  const W = 460, H = 200, pad = { l: 46, r: 12, t: 12, b: 26 };
  const data = pts.map((p, i) => ({ ...p, i, v: p[key] }));
  const sx = linearScale([0, Math.max(1, data.length - 1)], [pad.l, W - pad.r]);
  const { hover, handlers } = useNearestHover({ viewW: W, points: data, px: (p) => sx(p.i) });

  const ctl = (
    <span className="sh-range">
      {RANGES.map(([d, l]) => (
        <button key={d} className={"sh-rbtn" + (days === d ? " on" : "")} onClick={() => setDays(d)}>{l}</button>
      ))}
    </span>
  );

  if (q.isPending) return <div className="sh-box">{ctl}<div className="an-empty">загрузка…</div></div>;
  if (data.length < 2) return <div className="sh-box">{ctl}<div className="an-empty">мало истории для графика</div></div>;

  let ymin = Math.min(...data.map((p) => p.v)), ymax = Math.max(...data.map((p) => p.v));
  if (ymin === ymax) { ymin -= 1; ymax += 1; }
  const sy = linearScale([ymin, ymax], [H - pad.b, pad.t]);
  // точная история (сплошная) vs candle-оценка (пунктир). Оценка — префикс до
  // первого точного снапшота; последняя est-точка стыкует с exact для непрерывности.
  const fx = q.data?.exact_from || null;
  const estPart = data.filter((p) => p.src === "est");
  const exactPart = data.filter((p) => p.src === "exact");
  const estJoin = exactPart.length && estPart.length ? [...estPart, exactPart[0]] : estPart;
  const estPath = estJoin.length > 1 ? linePath(estJoin, (d) => sx(d.i), (d) => sy(d.v)) : null;
  const exactPath = exactPart.length > 1 ? linePath(exactPart, (d) => sx(d.i), (d) => sy(d.v)) : null;
  const last = data[data.length - 1], first = data[0];
  const chg = last.v - first.v;

  const nx = Math.min(6, data.length);
  const xstep = Math.max(1, Math.floor((data.length - 1) / (nx - 1)));
  const xticks = [];
  for (let i = 0; i < data.length; i += xstep) xticks.push({ x: sx(i), label: data[i].date.slice(5) });

  return (
    <div className="sh-box">
      <div className="sh-top">
        <span className="sh-val">{label} <b>{fmt.bps(last.v)}</b> bps
          <span className={"sh-chg " + (chg >= 0 ? "up" : "down")}>{chg >= 0 ? "▲" : "▼"} {fmt.bps(Math.abs(chg))} за период</span>
        </span>
        {ctl}
      </div>
      <div className="sh-chart">
        <svg viewBox={`0 0 ${W} ${H}`} className="an-svg" role="img" aria-label={`динамика ${label}`} {...handlers}>
          <GridY ticks={linTicks(ymin, ymax, 4)} y={sy} x1={pad.l} x2={W - pad.r}
            lineClass="an-grid" textClass="an-axis" label={(v) => Math.round(v)} />
          <XTicks ticks={xticks} y={H - pad.b + 14} textClass="an-axis" />
          {estPath && <path d={estPath} className="sh-line sh-est" fill="none" />}
          {exactPath && <path d={exactPath} className="sh-line" fill="none" />}
          {!estPath && !exactPath && <path d={linePath(data, (d) => sx(d.i), (d) => sy(d.v))} className="sh-line" fill="none" />}
          {hover ? (
            <g pointerEvents="none">
              <line x1={sx(hover.i)} x2={sx(hover.i)} y1={pad.t} y2={H - pad.b}
                stroke="var(--mut-2)" strokeWidth={1} strokeDasharray="3 3" />
              <line x1={pad.l} x2={W - pad.r} y1={sy(hover.v)} y2={sy(hover.v)}
                stroke="var(--mut-2)" strokeWidth={1} strokeDasharray="3 3" />
              <circle cx={sx(hover.i)} cy={sy(hover.v)} r={3.5} className="sh-dot" />
              {(() => {
                const lbl = String(Math.round(hover.v)), tw = lbl.length * 6 + 8, ty = sy(hover.v);
                return (
                  <g>
                    <rect x={pad.l - 3 - tw} y={ty - 7} width={tw} height={14} rx={2} fill="var(--inv-bg)" />
                    <text x={pad.l - 3 - tw / 2} y={ty + 3} textAnchor="middle" className="an-axis"
                      style={{ fill: "var(--inv-fg)" }}>{lbl}</text>
                  </g>
                );
              })()}
            </g>
          ) : (
            <circle cx={sx(last.i)} cy={sy(last.v)} r={3} className="sh-dot" />
          )}
        </svg>
        {hover && (
          <Tooltip x={sx(hover.i)} viewW={W} top={2}>
            {fmt.date(hover.date)} · {label} {fmt.bps(hover.v)} · цена {fmt.pct(hover.price)}%
            {hover.ytm != null && <> · YTM {fmt.pct(hover.ytm)}%</>}
          </Tooltip>
        )}
      </div>
      <div className="sh-note">
        {fx ? <>Сплошная — точная история с {fmt.date(fx)}. Пунктир (до неё) — оценка (историч. цена × текущая модель).</>
          : <>Оценка: историч. цена × текущая модель. Точная история копится с сегодня.</>}
      </div>
    </div>
  );
}
