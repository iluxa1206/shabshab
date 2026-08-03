import { useEffect, useMemo, useState } from "react";
import { fmt } from "../format.js";
import { fetchYidxHistory } from "../api.js";
import {
  linearScale, linTicks, linePath, GridY, XTicks,
  MeasuredSvg, ChartFrame, dateTickIdx, tickLabel, spanDays,
} from "../charts/index.js";

// рейтинг-бакеты и их цвет (градация риска) — CSS-переменные, тема-aware
const BUCKETS = ["AAA", "AA", "A", "BBB", "BB", "B", "NR"];
const norm = (r) => {
  if (!r) return "NR";
  if (["AAA", "AA", "A", "BBB"].includes(r)) return r;
  if (["BB", "B", "CCC", "CC", "C", "D"].includes(r)) return r === "BB" || r === "B" ? r : "B";
  return "NR";
};
const BCOLOR = {
  AAA: "var(--rt-aaa)", AA: "var(--rt-aa)", A: "var(--rt-a)", BBB: "var(--rt-bbb)",
  BB: "var(--rt-bb)", B: "var(--rt-b)", NR: "var(--mut-2)",
};

// склонение «бумага/бумаги/бумаг» по числу
const plu = (n) => {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return "бумаг";
  if (b === 1) return "бумага";
  if (b >= 2 && b <= 4) return "бумаги";
  return "бумаг";
};

// DM (discount margin, Fabozzi) для аналитики — поле disc_margin_bps (наш расчёт).
// Бэнд отсекает мусор от стейл/тонких цен неликвида (напр. DM −2167).
const dmval = (b) => {
  const v = b.disc_margin_bps;
  return v != null && v < 3000 && v > -1500 ? v : null;
};

// ключ эмитента (имя первично, id — фолбэк) и группировка строк по эмитенту
const emKey = (b) => b.emitter_name || (b.emitter_id ? `#${b.emitter_id}` : null);
const byIssuer = (rows) => {
  const g = new Map();
  for (const b of rows) {
    const k = emKey(b);
    if (!k) continue;
    if (!g.has(k)) g.set(k, []);
    g.get(k).push(b);
  }
  return g;
};
// доминирующий рейтинг-бакет группы бумаг → её цвет
const modalBucket = (bonds) => {
  const c = {};
  for (const b of bonds) { const k = norm(b.rating); c[k] = (c[k] || 0) + 1; }
  let best = "NR", bn = -1;
  for (const k of Object.keys(c)) if (c[k] > bn) { bn = c[k]; best = k; }
  return best;
};
const trunc = (s) => (s.length > 18 ? s.slice(0, 17) + "…" : s);

const median = (a) => {
  if (!a.length) return null;
  const s = a.slice().sort((x, y) => x - y);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const quantile = (a, q) => {
  if (!a.length) return null;
  const s = a.slice().sort((x, y) => x - y);
  const pos = (s.length - 1) * q, b = Math.floor(pos);
  return s[b] + (s[b + 1] - s[b] || 0) * (pos - b);
};

// Общие поля scatter-графиков. Размер берётся замером контейнера (см.
// useChartSize): раньше viewBox 460×260 растягивался CSS'ом и вместе с
// геометрией плыл шрифт подписей осей.
const SC_PAD = { l: 44, r: 12, t: 12, b: 30 };
const SC_H = 260;

// ── Scatter: DM vs spread duration, цвет = рейтинг ──
// sel/onPick: выбранный эмитент — его выпуски подсвечены, остальные пригашены;
// клик по точке выбирает/сбрасывает эмитента
function ScatterZDur({ rows, sel, onPick }) {
  const pts = rows
    .map((b) => ({ b, z: dmval(b) }))
    .filter(({ b, z }) => b.spread_dur_yrs != null && z != null)
    .map(({ b, z }) => ({ x: b.spread_dur_yrs, y: z, r: norm(b.rating), isin: b.isin, name: b.short_name, iss: emKey(b) }));
  if (pts.length < 2) return <div className="an-empty">мало данных для scatter</div>;
  const xmax = Math.max(...pts.map((p) => p.x), 1);
  const ymax = Math.max(...pts.map((p) => p.y), 100);
  const ymin = Math.min(...pts.map((p) => p.y), 0);
  return (
    <MeasuredSvg height={SC_H} label="DM vs spread duration">
      {({ W, H, bind }) => {
        const sx = linearScale([0, xmax], [SC_PAD.l, W - SC_PAD.r]);
        const sy = linearScale([ymin, ymax], [H - SC_PAD.b, SC_PAD.t]);
        const nx = Math.min(Math.ceil(xmax), Math.max(3, Math.round((W - SC_PAD.l - SC_PAD.r) / 70)));
        return (
          <>
            <GridY ticks={linTicks(ymin, ymax, 4)} y={sy} x1={SC_PAD.l} x2={W - SC_PAD.r}
              lineClass="an-grid" textClass="an-axis" label={(v) => Math.round(v)} />
            <XTicks ticks={linTicks(0, xmax, nx).map((xv) => ({ x: sx(xv), label: fmt.yrs(xv) }))}
              y={H - SC_PAD.b + 14} textClass="an-axis" />
            {pts.map((p) => {
              const on = sel != null && p.iss === sel;
              return (
                <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={on ? 4.4 : 3.2} fill={BCOLOR[p.r]}
                  fillOpacity={sel == null ? 0.72 : on ? 0.95 : 0.12}
                  stroke={on ? "var(--fg)" : "none"} strokeWidth={on ? 1 : 0}
                  className="an-pt" onClick={() => onPick && p.iss && onPick(p.iss)}
                  {...bind(sx(p.x), sy(p.y),
                    `${p.name} — ${p.iss || "без эмитента"}\nDM: ${Math.round(p.y)} bps (Fabozzi)\nspread duration: ${fmt.yrs(p.x)} · рейтинг: ${p.r}\nклик — фильтр по эмитенту`)} />
              );
            })}
            <text x={SC_PAD.l} y={H - 4} className="an-axis-lbl" textAnchor="start">спред-дюрация →</text>
            <text x={SC_PAD.l - 38} y={SC_PAD.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${SC_PAD.l - 38} ${SC_PAD.t + 4})`}>DM, bps</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

// ── Scatter агрегированный по эмитенту: точка = (медиана spread dur, медиана DM),
//    размер = число бумаг, цвет = доминирующий рейтинг эмитента ──
function ScatterIssuer({ rows, sel, onPick }) {
  const pts = [];
  for (const [k, bonds] of byIssuer(rows)) {
    const zs = bonds.map(dmval).filter((v) => v != null);
    const ds = bonds.map((b) => b.spread_dur_yrs).filter((v) => v != null);
    if (!zs.length || !ds.length) continue;
    pts.push({ x: median(ds), y: median(zs), r: modalBucket(bonds), n: bonds.length, name: String(k) });
  }
  if (pts.length < 2) return <div className="an-empty">мало данных для scatter</div>;
  const xmax = Math.max(...pts.map((p) => p.x), 1);
  const ymax = Math.max(...pts.map((p) => p.y), 100);
  const ymin = Math.min(...pts.map((p) => p.y), 0);
  return (
    <MeasuredSvg height={SC_H} label="DM vs spread duration по эмитентам">
      {({ W, H, bind }) => {
        const sx = linearScale([0, xmax], [SC_PAD.l, W - SC_PAD.r]);
        const sy = linearScale([ymin, ymax], [H - SC_PAD.b, SC_PAD.t]);
        const nx = Math.min(Math.ceil(xmax), Math.max(3, Math.round((W - SC_PAD.l - SC_PAD.r) / 70)));
        return (
          <>
            <GridY ticks={linTicks(ymin, ymax, 4)} y={sy} x1={SC_PAD.l} x2={W - SC_PAD.r}
              lineClass="an-grid" textClass="an-axis" label={(v) => Math.round(v)} />
            <XTicks ticks={linTicks(0, xmax, nx).map((xv) => ({ x: sx(xv), label: fmt.yrs(xv) }))}
              y={H - SC_PAD.b + 14} textClass="an-axis" />
            {pts.map((p) => {
              const on = sel != null && p.name === sel;
              return (
                <circle key={p.name} cx={sx(p.x)} cy={sy(p.y)} r={3 + Math.min(6, Math.sqrt(p.n)) + (on ? 1 : 0)}
                  fill={BCOLOR[p.r]} fillOpacity={sel == null ? 0.55 : on ? 0.85 : 0.1}
                  stroke={on ? "var(--fg)" : BCOLOR[p.r]} strokeOpacity={sel == null ? 0.9 : on ? 1 : 0.15}
                  className="an-pt" onClick={() => onPick && onPick(p.name)}
                  {...bind(sx(p.x), sy(p.y),
                    `${p.name}\nмедиана DM: ${Math.round(p.y)} bps · медиана spread dur: ${fmt.yrs(p.x)}\n${p.n} ${plu(p.n)} · рейтинг: ${p.r}\nклик — фильтр по эмитенту`)} />
              );
            })}
            <text x={SC_PAD.l} y={H - 4} className="an-axis-lbl" textAnchor="start">спред-дюрация →</text>
            <text x={SC_PAD.l - 38} y={SC_PAD.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${SC_PAD.l - 38} ${SC_PAD.t + 4})`}>DM, bps</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

// ── Общий рендер box-строк (p25–медиана–p75) для рейтингов/эмитентов ──
function BoxRows({ entries, note, label, sel, onPick }) {
  // pad.l 140: подпись «Балтийский лизинг…» (18 симв.) не влезает в 100px
  const PAD = { l: 140, r: 40, t: 6 };
  const rowH = entries.length > 8 ? 22 : 30;
  const H = entries.length * rowH + PAD.t + 8 + (note ? 14 : 0);
  if (!entries.length) return <div className="an-empty">нет данных</div>;
  const all = entries.flatMap((e) => e.arr);
  const zmax = Math.max(...all), zmin = Math.min(0, ...all);
  return (
    <MeasuredSvg height={H} label={label}>
      {({ W, bind }) => {
        const sx = linearScale([zmin, zmax], [PAD.l, W - PAD.r]);
        return (
          <>
            {entries.map((e, i) => {
              const arr = e.arr;
              const q1 = quantile(arr, 0.25), md = median(arr), q3 = quantile(arr, 0.75);
              const y = PAD.t + i * rowH + rowH / 2;
              const on = sel != null && e.key === sel;
              const dim = sel != null && !on;
              const pick = onPick ? "\nклик — фильтр по эмитенту" : "";
              const tip = arr.length > 1
                ? `${e.label}\nмедиана DM ${Math.round(md)} bps · p25–p75 ${Math.round(q1)}–${Math.round(q3)}\n${arr.length} ${plu(arr.length)}${pick}`
                : `${e.label}\nDM ${Math.round(md)} bps · ${arr.length} ${plu(arr.length)}${pick}`;
              return (
                <g key={e.key} className={onPick ? "an-row-pick" : undefined} opacity={dim ? 0.3 : 1}
                  onClick={onPick ? () => onPick(e.key) : undefined} {...bind(sx(md), y, tip)}>
                  {/* невидимая подложка — ховер/клик по всей строке, не только по элементам */}
                  <rect x={0} y={y - rowH / 2} width={W} height={rowH} fill="transparent" />
                  <text x={PAD.l - 6} y={y + 3} className="an-axis" textAnchor="end"
                    fontWeight={on ? 700 : undefined} fill={on ? "var(--fg)" : undefined}>{e.label}</text>
                  {arr.length > 1 && (
                    <line x1={sx(q1)} y1={y} x2={sx(q3)} y2={y} stroke={e.color} strokeWidth={7}
                      strokeOpacity={0.35} strokeLinecap="round" />
                  )}
                  <circle cx={sx(md)} cy={y} r={on ? 5 : 4} fill={e.color}
                    stroke={on ? "var(--fg)" : "none"} strokeWidth={on ? 1 : 0} />
                  <text x={sx(q3) + 6} y={y + 3} className="an-axis">{Math.round(md)}<tspan className="an-mut"> ({arr.length})</tspan></text>
                </g>
              );
            })}
            {note && <text x={W - PAD.r} y={H - 4} className="an-mut" textAnchor="end" fontSize="9">{note}</text>}
          </>
        );
      }}
    </MeasuredSvg>
  );
}

// ── Панель выбранного эмитента: какие его бумаги на графике, какие срезаны ──
function IssuerDetail({ rows, issuer, onClear }) {
  const bonds = rows.filter((b) => emKey(b) === issuer);
  if (!bonds.length) return null;
  return (
    <div className="an-selbar">
      <span className="an-sel-name" title={issuer}>{issuer}</span>
      {bonds.map((b) => {
        const z = dmval(b);
        const shown = z != null && b.spread_dur_yrs != null;
        const why = b.disc_margin_bps == null ? "нет DM (нет цены)" : z == null ? "DM вне бэнда" : "нет дюрации";
        return (
          <span key={b.isin} className={"an-sel-chip" + (shown ? "" : " off")}
            title={shown ? `${b.short_name}: DM ${Math.round(z)} bps · ${fmt.yrs(b.spread_dur_yrs)}` : `${b.short_name}: не на графике — ${why}`}>
            {b.short_name}{shown ? ` ${Math.round(z)}` : " ✕"}
          </span>
        );
      })}
      <button type="button" className="an-sel-clear" onClick={onClear} aria-label="сбросить фильтр">сброс</button>
    </div>
  );
}

// ── Распределение DM по рейтинг-бакетам (p25–медиана–p75) ──
function RatingDist({ rows }) {
  const entries = useMemo(() => {
    const g = {};
    for (const b of rows) {
      const z = dmval(b);
      if (z == null) continue;
      (g[norm(b.rating)] ||= []).push(z);
    }
    return BUCKETS.filter((k) => g[k]?.length).map((k) => ({ key: k, label: k, arr: g[k], color: BCOLOR[k] }));
  }, [rows]);
  return <BoxRows entries={entries} label="распределение DM по рейтингам" />;
}

// ── Распределение DM по эмитентам (сорт по медиане DM, топ-N) ──
const ISSUER_CAP = 22;
function IssuerDist({ rows, sel, onPick }) {
  const { entries, note } = useMemo(() => {
    // одиночные эмитенты тоже в списке: точка-медиана без полосы p25–p75
    // (раньше скрывались правилом ≥2 бумаг — watchlist из одиночек давал пустую панель)
    const arr = [];
    for (const [k, bonds] of byIssuer(rows)) {
      const zs = bonds.map(dmval).filter((v) => v != null);
      if (!zs.length) continue;
      arr.push({ key: String(k), label: trunc(String(k)), arr: zs, color: BCOLOR[modalBucket(bonds)], md: median(zs) });
    }
    arr.sort((a, b) => b.md - a.md);
    const note = arr.length > ISSUER_CAP ? `+${arr.length - ISSUER_CAP} эмитентов ниже по DM скрыто` : null;
    return { entries: arr.slice(0, ISSUER_CAP), note };
  }, [rows]);
  if (!entries.length) return <div className="an-empty">нет эмитентов с валидным DM</div>;
  return <BoxRows entries={entries} note={note} label="распределение DM по эмитентам" sel={sel} onPick={onPick} />;
}

// ── История медианного Y-IDX по рейтингам/эмитентам (точные дневные снапшоты) ──
const PERIODS = [["1м", 30], ["3м", 91], ["6м", 182], ["12м", 365]];
// палитра линий эмитентов (рейтинг-цвета заняты бакетами); РЫНОК — нейтральный
const ICOLORS = ["#4f9cf9", "#f9a04f", "#3fbf7f", "#e05c66", "#b07cf9", "#3fc6c6", "#d4b83f", "#f97cc0"];
const dmm = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
const YH_PAD = { l: 44, r: 12, t: 12, b: 30 };

function YidxHistory({ groupBy, rows }) {
  const byIss = groupBy === "issuer";
  const [period, setPeriod] = useState(91);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  // серии, скрытые кликом по легенде (8+ линий эмитентов — иначе не разобрать)
  const [off, setOff] = useState(() => new Set());
  // стабильный ключ фильтра: rows пересоздаются каждым поллом с тем же составом —
  // рефетч только при реальной смене набора ISIN
  const isinsKey = useMemo(() => rows.map((b) => b.isin).sort().join(","), [rows]);
  useEffect(() => {
    if (!isinsKey) { setData({ dates: [], series: [] }); return; }
    const ac = new AbortController();
    setErr(null);
    fetchYidxHistory(period, byIss ? "issuer" : "rating", isinsKey.split(","), ac.signal)
      .then(setData)
      .catch((e) => { if (e.name !== "AbortError") setErr(e.message || "ошибка"); });
    return () => ac.abort();
  }, [period, byIss, isinsKey]);
  useEffect(() => { setOff(new Set()); }, [byIss]);

  const ctl = (
    <span className="an-toggle" role="group" aria-label="период">
      {PERIODS.map(([l, d]) => (
        <button key={d} type="button" className={"an-tgl-btn" + (period === d ? " on" : "")}
          aria-pressed={period === d} onClick={() => setPeriod(d)}>{l}</button>
      ))}
    </span>
  );

  if (err) return <>{ctl}<div className="an-empty">не загрузилось: {err}</div></>;
  if (!data) return <>{ctl}<div className="an-empty">загрузка…</div></>;
  const { dates, series } = data;
  if (!dates.length || !series.length) return <>{ctl}<div className="an-empty">нет истории снапшотов за период</div></>;

  const colorOf = (s, i) => (byIss
    ? (s.key === "РЫНОК" ? "var(--mut-2)" : ICOLORS[i % ICOLORS.length])
    : BCOLOR[s.key] || "var(--mut-2)");
  const shown = series.filter((s) => !off.has(s.key));
  const di = new Map(dates.map((d, i) => [d, i]));
  const span = spanDays(dates);
  // точки для nearest-hover — индексы дат (crosshair общий для всех серий)
  const idxPts = dates.map((d, i) => ({ i, date: d }));

  const build = (g) => {
    const vals = (shown.length ? shown : series).flatMap((s) => s.points.map((p) => p.med));
    const ymax = Math.max(...vals), ymin = Math.min(...vals, 0);
    const sx = linearScale([0, Math.max(dates.length - 1, 1)], [g.x0, g.x1]);
    const sy = linearScale([ymin, ymax], [g.y0, g.y1]);
    const nx = Math.max(3, Math.min(7, Math.round(g.iw / 70)));
    return {
      sx, sy,
      yTicks: linTicks(ymin, ymax, 4),
      yFormat: (v) => Math.round(v),
      xTicks: dateTickIdx(dates, nx).map((i) => ({ x: sx(i), label: tickLabel(dates[i], span) })),
    };
  };

  const toggle = (k) => setOff((s) => {
    const n = new Set(s);
    n.has(k) ? n.delete(k) : n.add(k);
    return n;
  });

  return (
    <>
      {ctl}
      <ChartFrame
        height={220} pad={YH_PAD} label="динамика Y-IDX"
        data={idxPts} build={build} px={(p, s) => s.sx(p.i)}
        tooltip={(p) => (
          <>
            {fmt.date(p.date)}
            {shown.map((s, i) => {
              const pt = s.points.find((q) => q.date === p.date);
              if (!pt) return null;
              const gi = series.indexOf(s);
              return <div key={s.key} style={{ color: colorOf(s, gi) }}>{trunc(s.key)} {Math.round(pt.med)}</div>;
            })}
          </>
        )}
        overlay={(s, g) => (
          <text x={g.x0 - 38} y={g.y1 + 4} className="an-axis-lbl"
            transform={`rotate(-90 ${g.x0 - 38} ${g.y1 + 4})`}>Y-IDX, bps</text>
        )}
      >
        {(s) => shown.map((ser) => {
          const gi = series.indexOf(ser);
          const pts = ser.points.map((p) => ({ x: di.get(p.date), y: p.med }));
          const c = colorOf(ser, gi);
          return (
            <g key={ser.key}>
              {pts.length > 1
                ? <path d={linePath(pts, (p) => s.sx(p.x), (p) => s.sy(p.y))} fill="none" stroke={c}
                    strokeWidth={ser.key === "РЫНОК" ? 1 : 1.6}
                    strokeDasharray={ser.key === "РЫНОК" ? "4 3" : undefined} />
                : pts.map((p) => <circle key={p.x} cx={s.sx(p.x)} cy={s.sy(p.y)} r={2.5} fill={c} />)}
            </g>
          );
        })}
      </ChartFrame>
      <div className="an-legend">
        {series.map((s, i) => (
          <button key={s.key} type="button" className={"an-leg-btn" + (off.has(s.key) ? " off" : "")}
            onClick={() => toggle(s.key)} aria-pressed={!off.has(s.key)}
            title={`${s.key} — клик скрывает/показывает линию`}>
            <span className="an-leg-swatch" style={{ background: colorOf(s, i) }} />
            {trunc(s.key)} <span className="an-mut2">{Math.round(s.points[s.points.length - 1].med)}</span>
          </button>
        ))}
      </div>
      {data.exact_from && <div className="an-note">точная история копится с {dmm(data.exact_from)} — глубже данных пока нет</div>}
    </>
  );
}

function RatingLegend() {
  return (
    <div className="an-legend">
      <span className="an-leg-lbl">цвет:</span>
      {BUCKETS.map((k) => (
        <span key={k} className="an-leg-item">
          <span className="an-leg-swatch" style={{ background: BCOLOR[k] }} />{k}
        </span>
      ))}
    </div>
  );
}

function AggToggle({ value, onChange }) {
  return (
    <span className="an-toggle" role="group" aria-label="агрегация">
      {[["rating", "Рейтинг"], ["issuer", "Эмитент"]].map(([v, l]) => (
        <button key={v} type="button" className={"an-tgl-btn" + (value === v ? " on" : "")}
          aria-pressed={value === v} onClick={() => onChange(v)}>{l}</button>
      ))}
    </span>
  );
}

export default function AnalyticsPanel({ rows }) {
  const [groupBy, setGroupBy] = useState("rating");
  // фильтр по эмитенту: клик по строке box-chart / точке scatter — подсветка его
  // бумаг на графиках + список; повторный клик или «сброс» — снять
  const [selIssuer, setSelIssuer] = useState(null);
  const pick = (k) => setSelIssuer((s) => (s === k ? null : k));
  const byIss = groupBy === "issuer";
  return (
    <section className="analytics">
      <div className="an-card">
        <div className="an-title">DM vs SPREAD DURATION
          <span className="an-hint">{byIss ? "точка = эмитент (медиана) · размер = число бумаг · цвет = рейтинг" : "точка = выпуск · цвет = рейтинг · наведи для деталей"}</span>
          <AggToggle value={groupBy} onChange={setGroupBy} />
        </div>
        {byIss ? <ScatterIssuer rows={rows} sel={selIssuer} onPick={pick} />
               : <ScatterZDur rows={rows} sel={selIssuer} onPick={pick} />}
        {selIssuer && <IssuerDetail rows={rows} issuer={selIssuer} onClear={() => setSelIssuer(null)} />}
        <RatingLegend />
      </div>
      <div className="an-card">
        <div className="an-title">{byIss ? "DM по ЭМИТЕНТАМ" : "DM по РЕЙТИНГ-БАКЕТАМ"}
          <span className="an-hint">{byIss ? "линия p25–p75 · точка = медиана · (n) · клик = фильтр" : "линия p25–p75 · точка = медиана · (n)"}</span>
          <AggToggle value={groupBy} onChange={setGroupBy} />
        </div>
        {byIss ? <IssuerDist rows={rows} sel={selIssuer} onPick={pick} /> : <RatingDist rows={rows} />}
        <RatingLegend />
      </div>
      <div className="an-card">
        <div className="an-title">Y-IDX ДИНАМИКА
          <span className="an-hint">{byIss ? "медиана по топ-эмитентам · пунктир = рынок · клик по легенде скрывает линию" : "медиана по рейтинг-бакетам · клик по легенде скрывает линию"}</span>
          <AggToggle value={groupBy} onChange={setGroupBy} />
        </div>
        <YidxHistory groupBy={groupBy} rows={rows} />
      </div>
    </section>
  );
}
