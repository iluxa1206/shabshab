import { useEffect, useMemo, useState } from "react";
import { fmt } from "../format.js";
import { fetchSpreadMulti } from "../api.js";
import {
  linearScale, niceTicks, linePath, ChartFrame,
  dateTickIdx, tickLabel, spanDays,
} from "../charts/index.js";

// Вкладка СРАВНЕНИЕ: несколько выпусков одной линией каждый. Набор строк —
// тот же отфильтрованный список, что и в МОНИТОРЕ (фильтры общие, живут в App),
// выбор на график — чекбоксами в таблице под графиком.

// Палитра линий: восемь различимых цветов. Цвет закреплён за ПОЗИЦИЕЙ в списке
// выбора, а не за бумагой: снял одну — остальные цвет не меняют (иначе взгляд
// каждый раз заново ищет свою линию).
const LINE_COLORS = ["#4f9cf9", "#f9a04f", "#3fbf7f", "#e05c66",
                     "#b07cf9", "#3fc6c6", "#d4b83f", "#f97cc0"];
export const CMP_MAX = LINE_COLORS.length;

const PERIODS = [["1м", 30], ["3м", 91], ["6м", 182], ["12м", 365]];
// СПРЕД — первичная метрика платформы (Y-IDX). Цена — вспомогательная, в двух
// видах: абсолютная (% номинала) и нормированная к началу окна (Δ% от первой
// точки) — только она позволяет сравнивать бумаги с разной ценой в одной шкале.
const METRICS = [["spread", "Спред"], ["price", "Цена %"], ["chg", "Цена Δ%"]];
const BASES = [["close", "Закрытие"], ["vwap", "Средневзвес"]];

const PAD = { l: 50, r: 14, t: 12, b: 30 };
const trunc = (s, n = 16) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");
const dmm = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;

// Домен по данным, а не от нуля: линии спредов ходят в узкой полосе (см.
// AnalyticsPanel — то же правило).
const padDomain = (vals, frac = 0.06) => {
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = hi - lo;
  const p = span > 0 ? span * frac : Math.max(Math.abs(hi) * 0.05, 1);
  return [lo - p, hi + p];
};

// Значение точки под выбранную метрику. chg считается от ПЕРВОЙ точки этой же
// серии: у каждой бумаги своя база, поэтому линии стартуют из нуля вместе.
function valueOf(p, metric, base0) {
  if (metric === "spread") return p.y_idx;
  if (p.price == null) return null;
  if (metric === "price") return p.price;
  return base0 ? (p.price / base0 - 1) * 100 : null;
}

const axisLabel = (m) => (m === "spread" ? "Y-IDX, bps"
  : m === "price" ? "цена, % номинала" : "Δ цены, %");
const fmtVal = (v, m) => (v == null ? "—"
  : m === "spread" ? String(Math.round(v))
  : m === "price" ? fmt.pct(v, 2)
  : fmt.signed(v, 2) + "%");

// ── График: одна линия на выпуск ──
function CompareChart({ series, names, metric, height, hi, onHi }) {
  // общая ось дат: точки у бумаг не совпадают (у каждой свои торговые дни)
  const dates = useMemo(() => {
    const s = new Set();
    for (const ser of series) for (const p of ser.points) s.add(p.date);
    return [...s].sort();
  }, [series]);

  const lines = useMemo(() => series.map((ser) => {
    const first = metric === "chg"
      ? ser.points.find((p) => p.price != null)?.price ?? null : null;
    return {
      isin: ser.isin,
      pts: ser.points
        .map((p) => ({ date: p.date, v: valueOf(p, metric, first), raw: p }))
        .filter((p) => p.v != null),
    };
  }), [series, metric]);

  const vals = lines.flatMap((l) => l.pts.map((p) => p.v));
  if (!dates.length || !vals.length) {
    return <div className="an-empty">нет истории за период — архив по этим выпускам пуст</div>;
  }

  const di = new Map(dates.map((d, i) => [d, i]));
  const span = spanDays(dates);
  const idxPts = dates.map((d, i) => ({ i, date: d }));
  const [ymin, ymax] = padDomain(vals);

  const build = (g) => {
    const sx = linearScale([0, Math.max(dates.length - 1, 1)], [g.x0, g.x1]);
    const sy = linearScale([ymin, ymax], [g.y0, g.y1]);
    const nx = Math.max(3, Math.min(8, Math.round(g.iw / 80)));
    return {
      sx, sy,
      yTicks: niceTicks(ymin, ymax, 5),
      yFormat: (v) => (metric === "spread" ? Math.round(v) : fmt.pct(v, 1)),
      xTicks: dateTickIdx(dates, nx).map((i) => ({ x: sx(i), label: tickLabel(dates[i], span) })),
    };
  };

  return (
    <ChartFrame
      height={height} pad={PAD} label={`сравнение: ${axisLabel(metric)}`}
      data={idxPts} build={build} px={(p, s) => s.sx(p.i)}
      tooltip={(p) => {
        const at = lines
          .map((l) => ({ l, pt: l.pts.find((q) => q.date === p.date) }))
          .filter((x) => x.pt)
          .sort((a, b) => b.pt.v - a.pt.v);
        return (
          <>
            <div className="an-tt-h">{fmt.date(p.date)}</div>
            {at.map(({ l, pt }) => (
              <div key={l.isin} style={{ color: LINE_COLORS[series.findIndex((s) => s.isin === l.isin) % LINE_COLORS.length] }}>
                {trunc(names[l.isin] || l.isin, 12)} {fmtVal(pt.v, metric)}
              </div>
            ))}
          </>
        );
      }}
      overlay={(s, g) => (
        <text x={g.x0 - 40} y={g.y1 + 4} className="an-axis-lbl"
          transform={`rotate(-90 ${g.x0 - 40} ${g.y1 + 4})`}>{axisLabel(metric)}</text>
      )}
    >
      {(s) => lines.map((l, i) => {
        const c = LINE_COLORS[i % LINE_COLORS.length];
        const pts = l.pts.map((p) => ({ x: di.get(p.date), y: p.v }));
        const on = hi == null ? null : l.isin === hi;
        const d = pts.length > 1 ? linePath(pts, (p) => s.sx(p.x), (p) => s.sy(p.y)) : null;
        return (
          <g key={l.isin} opacity={on === false ? 0.18 : 1}
            onMouseEnter={() => onHi(l.isin)} onMouseLeave={() => onHi(null)}>
            {d
              ? <>
                  {/* широкая прозрачная копия — попасть по линии мышью */}
                  <path d={d} fill="none" stroke="transparent" strokeWidth={10} />
                  <path d={d} fill="none" stroke={c} strokeWidth={on ? 2.4 : 1.6} />
                </>
              : pts.map((p) => <circle key={p.x} cx={s.sx(p.x)} cy={s.sy(p.y)} r={2.5} fill={c} />)}
          </g>
        );
      })}
    </ChartFrame>
  );
}

// ── Легенда: тикер, значение на конец окна, изменение за окно ──
function CompareLegend({ series, names, metric, hi, onHi, onDrop }) {
  return (
    <div className="an-legend">
      {series.map((ser, i) => {
        const first = metric === "chg"
          ? ser.points.find((p) => p.price != null)?.price ?? null : null;
        const vs = ser.points.map((p) => valueOf(p, metric, first)).filter((v) => v != null);
        const last = vs.length ? vs[vs.length - 1] : null;
        const delta = vs.length > 1 ? last - vs[0] : null;
        const on = hi == null ? null : ser.isin === hi;
        return (
          <button key={ser.isin} type="button" className={"an-leg-btn" + (on === false ? " dim" : "")}
            onMouseEnter={() => onHi(ser.isin)} onMouseLeave={() => onHi(null)}
            onClick={() => onDrop(ser.isin)} title={`${names[ser.isin] || ser.isin} — клик: убрать с графика`}>
            <span className="an-leg-swatch" style={{ background: LINE_COLORS[i % LINE_COLORS.length] }} />
            {trunc(names[ser.isin] || ser.isin)}
            <span className="an-mut2">{fmtVal(last, metric)}</span>
            {delta != null && (
              <span className={delta > 0 ? "up" : delta < 0 ? "down" : "an-mut2"}>
                {metric === "spread" ? fmt.devBps(delta) : fmt.signed(delta, 2)}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

// ── Витрина выбора: те же строки, что в МОНИТОРЕ, плюс чекбокс ──
function PickTable({ rows, sel, onToggle, onOpen, hi, onHi }) {
  if (!rows.length) return <div className="an-empty">под фильтры не попала ни одна бумага</div>;
  const full = sel.length >= CMP_MAX;
  return (
    <div className="cmp-pick">
      <table className="cmp-tbl">
        <thead>
          <tr>
            <th className="cmp-cb" />
            <th>выпуск</th><th>эмитент</th><th>рейт</th>
            <th className="num">Y-IDX</th><th className="num">цена</th>
            <th className="num">срок</th><th className="num">оборот</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((b) => {
            const i = sel.indexOf(b.isin);
            const on = i >= 0;
            return (
              <tr key={b.isin} className={(on ? "on" : "") + (hi === b.isin ? " hi" : "")}
                onMouseEnter={() => on && onHi(b.isin)} onMouseLeave={() => on && onHi(null)}>
                <td className="cmp-cb">
                  <input type="checkbox" checked={on} disabled={!on && full}
                    onChange={() => onToggle(b.isin)}
                    aria-label={`сравнить ${b.short_name || b.isin}`}
                    title={!on && full ? `на графике максимум ${CMP_MAX} линий` : undefined}
                    style={on ? { accentColor: LINE_COLORS[i % LINE_COLORS.length] } : undefined} />
                </td>
                <td>
                  <button type="button" className="cmp-name" onClick={(e) => onOpen(b.isin, e.currentTarget)}
                    title="карточка выпуска">{b.short_name || b.isin}</button>
                </td>
                <td className="cmp-mut">{trunc(b.emitter_name, 22)}</td>
                <td className="cmp-mut">{b.rating || "—"}</td>
                <td className="num">{fmt.bps(b.yield_over_index_bps) ?? "—"}</td>
                <td className="num">{fmt.pct(b.last_price_pct) ?? "—"}</td>
                <td className="num">{fmt.yrs(b.spread_dur_yrs) ?? "—"}</td>
                <td className="num">{fmt.mln(b.val_today) ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Seg({ items, value, onChange, label }) {
  return (
    <span className="an-toggle" role="group" aria-label={label}>
      {items.map(([v, l]) => (
        <button key={v} type="button" className={"an-tgl-btn" + (value === v ? " on" : "")}
          aria-pressed={value === v} onClick={() => onChange(v)}>{l}</button>
      ))}
    </span>
  );
}

// rows — отфильтрованный набор МОНИТОРА; sel/onToggle — выбор линий (живёт в
// App, чтобы переживать F5 и уходить в ссылку).
export default function CompareModule({ rows, sel, onToggle, onClear, onOpen }) {
  const [metric, setMetric] = useState(() => localStorage.getItem("cmpMetric") || "spread");
  const [base, setBase] = useState(() => localStorage.getItem("cmpBase") || "close");
  const [days, setDays] = useState(() => Number(localStorage.getItem("cmpDays")) || 91);
  const [hi, setHi] = useState(null);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => { localStorage.setItem("cmpMetric", metric); }, [metric]);
  useEffect(() => { localStorage.setItem("cmpBase", base); }, [base]);
  useEffect(() => { localStorage.setItem("cmpDays", String(days)); }, [days]);

  const selKey = sel.join(",");
  useEffect(() => {
    if (!selKey) { setData({ series: [] }); return; }
    const ac = new AbortController();
    setErr(null);
    setData(null);
    fetchSpreadMulti(selKey.split(","), { days, base }, ac.signal)
      .then(setData)
      .catch((e) => { if (e.name !== "AbortError") setErr(e.message || "ошибка"); });
    return () => ac.abort();
  }, [selKey, days, base]);

  // имена для легенды/тултипа — из строк витрины (истории имени бэк не отдаёт)
  const names = useMemo(() => Object.fromEntries(
    rows.map((b) => [b.isin, b.short_name || b.isin])), [rows]);

  // серии в порядке выбора: цвет линии = позиция в sel
  const series = useMemo(() => {
    if (!data?.series) return [];
    const by = new Map(data.series.map((s) => [s.isin, s]));
    return sel.map((i) => by.get(i)).filter(Boolean);
  }, [data, sel]);

  const missing = sel.filter((i) => !series.some((s) => s.isin === i));

  return (
    <section className="cmp">
      <div className="an-card">
        <div className="an-title an-head">
          <span className="an-title-txt">
            СРАВНЕНИЕ · {sel.length}/{CMP_MAX}
            {sel.length > 0 && (
              <button type="button" className="an-sel-clear" onClick={onClear}
                style={{ marginLeft: 8 }}>сброс</button>
            )}
          </span>
          <span className="an-title-ctl">
            <Seg items={METRICS} value={metric} onChange={setMetric} label="метрика" />
            <Seg items={BASES} value={base} onChange={setBase} label="база дня" />
            <Seg items={PERIODS.map(([l, d]) => [d, l])} value={days}
              onChange={setDays} label="период" />
          </span>
        </div>
        <div className="an-hint an-sub">
          {metric === "spread" ? "Y-IDX по цене дня — линия на выпуск"
            : metric === "price" ? "чистая цена, % номинала"
            : "изменение цены от первого дня окна, %"}
          {base === "vwap" ? " · средневзвешенная цена дня (архив часовых баров)"
                           : " · цена закрытия дня"}
        </div>

        {!sel.length ? (
          <div className="an-empty">отметьте выпуски в таблице ниже — до {CMP_MAX} линий</div>
        ) : err ? (
          <div className="an-empty">не загрузилось: {err}</div>
        ) : !data ? (
          <div className="an-empty">загрузка…</div>
        ) : (
          <>
            <CompareChart series={series} names={names} metric={metric} height={300}
              hi={hi} onHi={setHi} />
            <CompareLegend series={series} names={names} metric={metric}
              hi={hi} onHi={setHi} onDrop={onToggle} />
            {missing.length > 0 && (
              <div className="an-note">
                нет истории за окно: {missing.map((i) => names[i] || i).join(", ")}
                {base === "vwap" && " — архив часовых баров мельче окна"}
              </div>
            )}
            {data.exact_from && (
              <div className="an-note">история копится с {dmm(data.exact_from)} — глубже данных нет</div>
            )}
          </>
        )}
      </div>

      <PickTable rows={rows} sel={sel} onToggle={onToggle} onOpen={onOpen}
        hi={hi} onHi={setHi} />
    </section>
  );
}
