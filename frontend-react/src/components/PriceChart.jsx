import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchCandles } from "../api.js";
import { fmt } from "../format.js";
import { linearScale, linTicks, linePath, GridY, XTicks, useNearestHover, Tooltip } from "../charts/index.js";

const TFS = [["5m", "5м"], ["1h", "1ч"], ["1d", "1д"], ["1w", "1н"]];

// подпись времени свечи: внутридневные tf → дата+время, дневные/недельные → дата
function tlabel(t, tf) {
  const [d, hm = ""] = t.split(" ");
  const [Y, M, D] = d.split("-");
  if (tf === "5m" || tf === "1h") return `${D}.${M} ${hm.slice(0, 5)}`;
  return `${D}.${M}.${Y.slice(2)}`;
}

// syncDate/onHoverDate — синхронизация курсора с другими графиками карточки:
// свой ховер репортится наружу датой (YYYY-MM-DD), чужая дата рисуется
// пунктирной вертикалью, когда свой курсор вне графика.
function Chart({ candles, type, tf, syncDate, onHoverDate }) {
  const W = 480, H = 210, pad = { l: 46, r: 8, t: 8, b: 20 };
  const n = candles.length;
  const bw = (W - pad.l - pad.r) / n;
  const cx = (i) => pad.l + i * bw + bw / 2;
  const isLine = type === "line";
  const ymax = Math.max(...(isLine ? candles.map((c) => c.c) : candles.map((c) => c.h)));
  const ymin = Math.min(...(isLine ? candles.map((c) => c.c) : candles.map((c) => c.l)));
  const padY = (ymax - ymin) * 0.08 || 0.1;
  const sy = linearScale([ymin - padY, ymax + padY], [H - pad.b, pad.t]);

  const pts = candles.map((c, i) => ({ ...c, i }));
  const { hover, handlers } = useNearestHover({
    viewW: W, points: pts, px: (p) => cx(p.i),
    onHover: (p) => onHoverDate?.(p ? p.t.slice(0, 10) : null),
  });
  // чужой курсор (дата с соседнего графика) — только когда свой не активен
  const syncI = !hover && syncDate ? candles.findIndex((c) => c.t.slice(0, 10) === syncDate) : -1;

  const bodyW = Math.max(1, bw * 0.6);
  const step = Math.max(1, Math.floor(n / 4));
  const xticks = [];
  for (let i = 0; i < n; i += step) xticks.push({ x: cx(i), label: tlabel(candles[i].t, tf) });

  return (
    <div className="pchart-body">
      <svg viewBox={`0 0 ${W} ${H}`} className="an-svg" role="img" aria-label="график цены" {...handlers}>
        <GridY ticks={linTicks(ymin, ymax, 4)} y={sy} x1={pad.l} x2={W - pad.r}
          lineClass="an-grid" textClass="an-axis" label={(v) => v.toFixed(2)} />
        <XTicks ticks={xticks} y={H - pad.b + 13} textClass="an-axis" />
        {isLine ? (
          <path d={linePath(candles, (c, i) => cx(i), (c) => sy(c.c))}
            fill="none" stroke="var(--accent)" strokeWidth={1.4} />
        ) : candles.map((c, i) => {
          const col = c.c >= c.o ? "var(--up)" : "var(--down)";
          const yO = sy(c.o), yC = sy(c.c);
          return (
            <g key={i} stroke={col} fill={col}>
              <line x1={cx(i)} x2={cx(i)} y1={sy(c.h)} y2={sy(c.l)} strokeWidth={1} />
              <rect x={cx(i) - bodyW / 2} y={Math.min(yO, yC)} width={bodyW} height={Math.max(1, Math.abs(yO - yC))} />
            </g>
          );
        })}
        {syncI >= 0 && (
          <line x1={cx(syncI)} x2={cx(syncI)} y1={pad.t} y2={H - pad.b} pointerEvents="none"
            stroke="var(--mut-2)" strokeWidth={1} strokeDasharray="3 3" />
        )}
        {hover && (
          <g pointerEvents="none">
            <line x1={cx(hover.i)} x2={cx(hover.i)} y1={pad.t} y2={H - pad.b}
              stroke="var(--mut-2)" strokeWidth={1} strokeDasharray="3 3" />
            <line x1={pad.l} x2={W - pad.r} y1={sy(hover.c)} y2={sy(hover.c)}
              stroke="var(--mut-2)" strokeWidth={1} strokeDasharray="3 3" />
            {(() => {
              const label = hover.c.toFixed(2);
              const tw = label.length * 6 + 8, ty = sy(hover.c);
              return (
                <g>
                  <rect x={pad.l - 3 - tw} y={ty - 7} width={tw} height={14} rx={2} fill="var(--inv-bg)" />
                  <text x={pad.l - 3 - tw / 2} y={ty + 3} textAnchor="middle" className="an-axis"
                    style={{ fill: "var(--inv-fg)" }}>{label}</text>
                </g>
              );
            })()}
          </g>
        )}
      </svg>
      {hover && (
        <Tooltip x={cx(hover.i)} viewW={W} top={2}>
          {tlabel(hover.t, tf)} · О {fmt.pct(hover.o)} М {fmt.pct(hover.h)} Н {fmt.pct(hover.l)} З {fmt.pct(hover.c)}
        </Tooltip>
      )}
    </div>
  );
}

// Интерактивный график цены выпуска (MOEX): линия/свечи + таймфреймы.
// Для ОФЗ передавать secid + board="TQOB" (по ISIN candles не резолвятся).
// periodDays (опц.) — внешний период в календарных днях: tf фиксируется 1d,
// свечи режутся по дате, свой селектор таймфреймов скрыт (синхронизация
// с другими графиками карточки).
export default function PriceChart({ isin, secid, board, periodDays, syncDate, onHoverDate }) {
  const [tf, setTf] = useState("1d");
  const [type, setType] = useState("candles");
  const effTf = periodDays ? "1d" : tf;
  const q = useQuery({
    queryKey: ["candles", isin, secid, board, effTf],
    queryFn: () => fetchCandles(isin, effTf, { secid, board }),
    staleTime: 60_000,
  });
  const all = q.data?.candles;
  const candles = useMemo(() => {
    const rows = all || [];
    if (!periodDays) return rows;
    const from = new Date(Date.now() - periodDays * 864e5).toISOString().slice(0, 10);
    return rows.filter((c) => c.t.slice(0, 10) >= from);
  }, [all, periodDays]);

  return (
    <div className="pchart">
      <div className="pchart-ctl">
        {!periodDays && (
          <div className="pchart-tabs">
            {TFS.map(([v, l]) => (
              <button key={v} type="button" className={"pchart-tab" + (tf === v ? " on" : "")}
                onClick={() => setTf(v)}>{l}</button>
            ))}
          </div>
        )}
        <div className="pchart-tabs">
          {[["candles", "Свечи"], ["line", "Линия"]].map(([v, l]) => (
            <button key={v} type="button" className={"pchart-tab" + (type === v ? " on" : "")}
              onClick={() => setType(v)}>{l}</button>
          ))}
        </div>
      </div>
      {q.isPending ? <div className="pchart-empty">загрузка…</div>
        : q.error ? <div className="pchart-empty">ошибка загрузки</div>
        : candles.length < 2 ? <div className="pchart-empty">нет сделок за период</div>
        : <Chart candles={candles} type={type} tf={effTf} syncDate={syncDate} onHoverDate={onHoverDate} />}
    </div>
  );
}
