import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchSpreadHistory } from "../api.js";
import { fmt } from "../format.js";
import { linearScale, linTicks, linePath, GridY, XTicks, useNearestHover, Tooltip } from "../charts/index.js";

const RANGES = [[60, "3м"], [120, "6м"], [250, "1г"]];

// Динамика спреда: Y-IDX (флоатер, первичная метрика) / g-спред (фикс).
// Флоатер: прошлое бэк считает ЧЕСТНЫМ движком (as-of кривая/НКД/номинал
// каждого дня, персистится в spread_daily) — сплошная линия; пунктир — только
// хвосты candle-оценки (сегодня до вечернего снапшота, выходные сессии,
// фолбэк при сбое бэкфилла). days (опц., торговые дни) — внешний период:
// свой селектор скрыт (синхронизация с графиком цены в карточке).
// syncDate/onHoverDate — синхронизация курсора с графиком цены (см. PriceChart)
export default function SpreadHistory({ isin, kind, secid, board, days: daysProp, syncDate, onHoverDate }) {
  const [daysState, setDays] = useState(120);
  const days = daysProp ?? daysState;
  const isFixed = kind === "fixed";
  const key = isFixed ? "g_spread_bps" : "y_idx_bps";
  const label = isFixed ? "G-спред" : "Y-IDX";

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
  const { hover, handlers } = useNearestHover({
    viewW: W, points: data, px: (p) => sx(p.i),
    onHover: (p) => onHoverDate?.(p ? p.date : null),
  });
  // чужой курсор (дата с графика цены) — только когда свой не активен
  const syncP = !hover && syncDate ? data.find((p) => p.date === syncDate) : null;

  const ctl = daysProp != null ? null : (
    <span className="sh-range">
      {RANGES.map(([d, l]) => (
        <button key={d} className={"sh-rbtn" + (days === d ? " on" : "")} onClick={() => setDays(d)}>{l}</button>
      ))}
    </span>
  );

  if (q.isPending) return <div className="sh-box">{ctl}<div className="an-empty">{isFixed ? "загрузка…" : "загрузка… (первое открытие — честный пересчёт истории, до минуты)"}</div></div>;
  if (data.length < 2) return <div className="sh-box">{ctl}<div className="an-empty">мало истории для графика</div></div>;

  let ymin = Math.min(...data.map((p) => p.v)), ymax = Math.max(...data.map((p) => p.v));
  if (ymin === ymax) { ymin -= 1; ymax += 1; }
  const sy = linearScale([ymin, ymax], [H - pad.b, pad.t]);
  // сплошная — честная/точная история (src honest|exact); пунктир — candle-оценка
  // (хвосты вне точного окна). Крайние est-точки стыкуем с точной линией.
  const solidPart = data.filter((p) => p.src !== "est");
  const estPre = data.filter((p) => p.src === "est" && (!solidPart.length || p.i < solidPart[0].i));
  const estPost = data.filter((p) => p.src === "est" && solidPart.length && p.i > solidPart[solidPart.length - 1].i);
  const preJoin = estPre.length && solidPart.length ? [...estPre, solidPart[0]] : estPre;
  const postJoin = estPost.length && solidPart.length ? [solidPart[solidPart.length - 1], ...estPost] : estPost;
  const estPaths = [preJoin, postJoin]
    .filter((seg) => seg.length > 1)
    .map((seg) => linePath(seg, (d) => sx(d.i), (d) => sy(d.v)));
  const solidPath = solidPart.length > 1 ? linePath(solidPart, (d) => sx(d.i), (d) => sy(d.v)) : null;
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
          {estPaths.map((d, i) => <path key={i} d={d} className="sh-line sh-est" fill="none" />)}
          {solidPath && <path d={solidPath} className="sh-line" fill="none" />}
          {!solidPath && !estPaths.length && <path d={linePath(data, (d) => sx(d.i), (d) => sy(d.v))} className="sh-line" fill="none" />}
          {syncP && (
            <line x1={sx(syncP.i)} x2={sx(syncP.i)} y1={pad.t} y2={H - pad.b} pointerEvents="none"
              stroke="var(--mut-2)" strokeWidth={1} strokeDasharray="3 3" />
          )}
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
        {isFixed
          ? <>Сплошная — точная история (дневные снапшоты); пунктир — оценка (историч. цена × текущая модель).</>
          : <>Сплошная — честный расчёт: каждый день своим calc_date, as-of кривой и фактическими НКД/номиналом MOEX. Пунктир — оценка текущей моделью (сегодня/выходные сессии).</>}
      </div>
    </div>
  );
}
