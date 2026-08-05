import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchCandles } from "../api.js";
import { fmt } from "../format.js";
import { linearScale, linTicks, linePath, ChartFrame, dateTickIdx, tickLabel, spanDays } from "../charts/index.js";

const TFS = [["5m", "5м"], ["1h", "1ч"], ["1d", "1д"], ["1w", "1н"]];
const PAD = { l: 46, r: 8, t: 8, b: 20 };

// подпись времени свечи в тултипе: внутридневные tf → дата+время, иначе дата
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
  const isLine = type === "line";
  const data = candles.map((c, i) => ({ ...c, i }));
  const n = data.length;
  const times = data.map((c) => c.t);
  const span = spanDays(times);
  const syncPoint = syncDate ? data.find((c) => c.t.slice(0, 10) === syncDate) : null;

  const build = (g) => {
    const bw = g.iw / n;
    const cx = (i) => g.x0 + i * bw + bw / 2;
    const ymax = Math.max(...(isLine ? data.map((c) => c.c) : data.map((c) => c.h)));
    const ymin = Math.min(...(isLine ? data.map((c) => c.c) : data.map((c) => c.l)));
    const padY = (ymax - ymin) * 0.08 || 0.1;
    const sy = linearScale([ymin - padY, ymax + padY], [g.y0, g.y1]);
    const nx = Math.max(3, Math.min(8, Math.round(g.iw / 70)));
    return {
      cx, bw, sy,
      bodyW: Math.max(1, bw * 0.6),
      yTicks: linTicks(ymin, ymax, 4),
      yFormat: (v) => v.toFixed(2),
      xTicks: dateTickIdx(times, nx).map((i) => ({ x: cx(i), label: tickLabel(times[i], span) })),
    };
  };

  return (
    <ChartFrame
      height={210} pad={PAD} label="график цены"
      data={data} build={build}
      px={(p, s) => s.cx(p.i)} py={(p, s) => s.sy(p.c)}
      yBadge={(p) => p.c.toFixed(2)}
      syncPoint={syncPoint}
      onHoverPoint={(p) => onHoverDate?.(p ? p.t.slice(0, 10) : null)}
      tooltip={(p) => (
        <>{tlabel(p.t, tf)} · O {fmt.pct(p.o)} H {fmt.pct(p.h)} L {fmt.pct(p.l)} C {fmt.pct(p.c)}</>
      )}
    >
      {(s) => (isLine ? (
        <path d={linePath(data, (c, i) => s.cx(i), (c) => s.sy(c.c))}
          fill="none" stroke="var(--accent)" strokeWidth={1.4} />
      ) : data.map((c, i) => {
        const col = c.c >= c.o ? "var(--up)" : "var(--down)";
        const yO = s.sy(c.o), yC = s.sy(c.c);
        return (
          <g key={i} stroke={col} fill={col}>
            <line x1={s.cx(i)} x2={s.cx(i)} y1={s.sy(c.h)} y2={s.sy(c.l)} strokeWidth={1} />
            <rect x={s.cx(i) - s.bodyW / 2} y={Math.min(yO, yC)} width={s.bodyW}
              height={Math.max(1, Math.abs(yO - yC))} />
          </g>
        );
      }))}
    </ChartFrame>
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
