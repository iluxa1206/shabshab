import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchSpreadHistory } from "../api.js";
import { fmt } from "../format.js";
import { linearScale, linTicks, linePath, ChartFrame, dateTickIdx, tickLabel, spanDays } from "../charts/index.js";

const RANGES = [[60, "3м"], [120, "6м"], [250, "1г"]];
const PAD = { l: 46, r: 12, t: 12, b: 26 };

// Динамика спреда: Y-IDX (флоатер, первичная метрика) / g-спред (фикс).
// Флоатер: прошлое бэк считает ЧЕСТНЫМ движком (as-of кривая/НКД/номинал
// каждого дня, персистится в spread_daily) — сплошная линия; пунктир — только
// хвосты candle-оценки (сегодня до вечернего снапшота, выходные сессии,
// фолбэк при сбое бэкфилла). days (опц., торговые дни) — внешний период:
// свой селектор скрыт (синхронизация с графиком цены в карточке).
// syncDate/onHoverDate — синхронизация курсора с графиком цены (см. PriceChart)
export default function SpreadHistory({ isin, kind, secid, board, days: daysProp, from, syncDate, onHoverDate }) {
  const [daysState, setDays] = useState(120);
  const days = daysProp ?? daysState;
  const isFixed = kind === "fixed";
  const key = isFixed ? "g_spread_bps" : "y_idx_bps";
  const label = isFixed ? "G-спред" : "Y-IDX";

  const q = useQuery({
    queryKey: ["spread-hist", isin, kind, from || days],
    queryFn: () => fetchSpreadHistory(isin, { kind, secid, board, days, from }),
    staleTime: 300000,
  });

  const pts = (q.data?.points || []).filter((p) => p[key] != null);
  const data = pts.map((p, i) => ({ ...p, i, v: p[key] }));

  // свой селектор периода прячем, когда окно задано снаружи (карточка держит
  // цену и спред на одном диапазоне)
  const ctl = daysProp != null || from ? null : (
    <span className="sh-range">
      {RANGES.map(([d, l]) => (
        <button key={d} className={"sh-rbtn" + (days === d ? " on" : "")} onClick={() => setDays(d)}>{l}</button>
      ))}
    </span>
  );

  if (q.isPending) return <div className="sh-box">{ctl}<div className="an-empty">{isFixed ? "загрузка…" : "загрузка… (первое открытие — честный пересчёт истории, до минуты)"}</div></div>;
  if (data.length < 2) return <div className="sh-box">{ctl}<div className="an-empty">мало истории для графика</div></div>;

  const last = data[data.length - 1], first = data[0];
  const chg = last.v - first.v;
  // сплошная — честная/точная история (src honest|exact); пунктир — candle-оценка
  // (хвосты вне точного окна). Крайние est-точки стыкуем с точной линией.
  const solidPart = data.filter((p) => p.src !== "est");
  const estPre = data.filter((p) => p.src === "est" && (!solidPart.length || p.i < solidPart[0].i));
  const estPost = data.filter((p) => p.src === "est" && solidPart.length && p.i > solidPart[solidPart.length - 1].i);
  const preJoin = estPre.length && solidPart.length ? [...estPre, solidPart[0]] : estPre;
  const postJoin = estPost.length && solidPart.length ? [solidPart[solidPart.length - 1], ...estPost] : estPost;
  const estSegs = [preJoin, postJoin].filter((seg) => seg.length > 1);
  const hasSolid = solidPart.length > 1;

  const dates = data.map((p) => p.date);
  const span = spanDays(dates);
  const syncPoint = syncDate ? data.find((p) => p.date === syncDate) : null;

  const build = (g) => {
    let ymin = Math.min(...data.map((p) => p.v)), ymax = Math.max(...data.map((p) => p.v));
    if (ymin === ymax) { ymin -= 1; ymax += 1; }
    const sx = linearScale([0, Math.max(1, data.length - 1)], [g.x0, g.x1]);
    const sy = linearScale([ymin, ymax], [g.y0, g.y1]);
    // тиков тем больше, чем шире график (на узкой панели подписи слипались)
    const nx = Math.max(3, Math.min(8, Math.round(g.iw / 70)));
    return {
      sx, sy,
      yTicks: linTicks(ymin, ymax, 4),
      yFormat: (v) => Math.round(v),
      xTicks: dateTickIdx(dates, nx).map((i) => ({ x: sx(i), label: tickLabel(dates[i], span) })),
    };
  };

  return (
    <div className="sh-box">
      <div className="sh-top">
        <span className="sh-val">{label} <b>{fmt.bps(last.v)}</b> bps
          <span className={"sh-chg " + (chg >= 0 ? "up" : "down")}>{chg >= 0 ? "▲" : "▼"} {fmt.bps(Math.abs(chg))} за период</span>
        </span>
        {ctl}
      </div>
      <ChartFrame
        height={200} pad={PAD} label={`динамика ${label}`}
        data={data} build={build}
        px={(p, s) => s.sx(p.i)} py={(p, s) => s.sy(p.v)}
        yBadge={(p) => String(Math.round(p.v))}
        syncPoint={syncPoint}
        onHoverPoint={(p) => onHoverDate?.(p ? p.date : null)}
        tooltip={(p) => (
          <>
            {fmt.date(p.date)} · {label} {fmt.bps(p.v)} · цена {fmt.pct(p.price)}%
            {p.ytm != null && <> · YTM {fmt.pct(p.ytm)}%</>}
          </>
        )}
      >
        {(s, g, hover) => (
          <>
            {estSegs.map((seg, i) => (
              <path key={i} d={linePath(seg, (d) => s.sx(d.i), (d) => s.sy(d.v))} className="sh-line sh-est" fill="none" />
            ))}
            {hasSolid && (
              <path d={linePath(solidPart, (d) => s.sx(d.i), (d) => s.sy(d.v))} className="sh-line" fill="none" />
            )}
            {!hasSolid && !estSegs.length && (
              <path d={linePath(data, (d) => s.sx(d.i), (d) => s.sy(d.v))} className="sh-line" fill="none" />
            )}
            <circle cx={s.sx(hover ? hover.i : last.i)} cy={s.sy(hover ? hover.v : last.v)}
              r={hover ? 3.5 : 3} className="sh-dot" />
          </>
        )}
      </ChartFrame>
      <div className="sh-note">
        {isFixed
          ? <>Сплошная — точная история (дневные снапшоты); пунктир — оценка (историч. цена × текущая модель).</>
          : <>Сплошная — честный расчёт: каждый день своим calc_date, as-of кривой и фактическими НКД/номиналом MOEX. Пунктир — оценка текущей моделью (сегодня/выходные сессии).</>}
      </div>
    </div>
  );
}
