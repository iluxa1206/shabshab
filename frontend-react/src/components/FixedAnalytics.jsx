import { useMemo, useState } from "react";
import { fmt } from "../format.js";
import { linearScale, linTicks, GridY, XTicks, MeasuredSvg } from "../charts/index.js";

// Аналитика фиксов — зеркало AnalyticsPanel флоатеров, но метрики к погашению:
// g-спред vs модифицированная дюрация, распределение g-спреда, срочность.

const BUCKETS = ["AAA", "AA", "A", "BBB", "BB", "B", "NR"];
const norm = (r) => (r && BUCKETS.includes(r) ? r : "NR");
const BCOLOR = {
  AAA: "var(--rt-aaa)", AA: "var(--rt-aa)", A: "var(--rt-a)", BBB: "var(--rt-bbb)",
  BB: "var(--rt-bb)", B: "var(--rt-b)", NR: "var(--mut-2)",
};

const plu = (n) => {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return "бумаг";
  if (b === 1) return "бумага";
  if (b >= 2 && b <= 4) return "бумаги";
  return "бумаг";
};

// g-спред для аналитики — бэнд отсекает мусор от стейл/тонких цен неликвида.
const gval = (b) => {
  const v = b.g_spread_bps;
  return v != null && v < 3000 && v > -500 ? v : null;
};

const emKey = (b) => b.issuer || b.name || null;
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

// Общая геометрия scatter'ов. Размер — по замеру контейнера (MeasuredSvg):
// фиксированный viewBox 460×260 растягивался CSS'ом и вместе с ним плыл шрифт осей.
const SC_PAD = { l: 44, r: 12, t: 12, b: 30 };
const SC_H = 260;

// ── Scatter: g-спред vs mod duration, цвет = рейтинг ──
function ScatterGDur({ rows }) {
  const pts = rows
    .map((b) => ({ b, g: gval(b) }))
    .filter(({ b, g }) => b.mod_dur != null && g != null)
    .map(({ b, g }) => ({ x: b.mod_dur, y: g, r: norm(b.rating), isin: b.isin, name: b.name }));
  if (pts.length < 2) return <div className="an-empty">мало данных для scatter</div>;
  const xmax = Math.max(...pts.map((p) => p.x), 1);
  const ymax = Math.max(...pts.map((p) => p.y), 100);
  const ymin = Math.min(...pts.map((p) => p.y), 0);
  return (
    <MeasuredSvg height={SC_H} label="g-спред vs дюрация">
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
            {pts.map((p) => (
              <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={3.2} fill={BCOLOR[p.r]} fillOpacity={0.72}
                {...bind(sx(p.x), sy(p.y),
                  `${p.name}\ng-спред: ${Math.round(p.y)} bps\nмод. дюрация: ${fmt.yrs(p.x)} · рейтинг: ${p.r}`)} />
            ))}
            <text x={SC_PAD.l} y={H - 4} className="an-axis-lbl" textAnchor="start">мод. дюрация →</text>
            <text x={SC_PAD.l - 38} y={SC_PAD.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${SC_PAD.l - 38} ${SC_PAD.t + 4})`}>g-спред, bps</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

// ── Scatter агрегированный по эмитенту: (медиана дюрации, медиана g-спреда) ──
function ScatterIssuer({ rows }) {
  const pts = [];
  for (const [k, bonds] of byIssuer(rows)) {
    const gs = bonds.map(gval).filter((v) => v != null);
    const ds = bonds.map((b) => b.mod_dur).filter((v) => v != null);
    if (!gs.length || !ds.length) continue;
    pts.push({ x: median(ds), y: median(gs), r: modalBucket(bonds), n: bonds.length, name: String(k) });
  }
  if (pts.length < 2) return <div className="an-empty">мало данных для scatter</div>;
  const xmax = Math.max(...pts.map((p) => p.x), 1);
  const ymax = Math.max(...pts.map((p) => p.y), 100);
  const ymin = Math.min(...pts.map((p) => p.y), 0);
  return (
    <MeasuredSvg height={SC_H} label="g-спред vs дюрация по эмитентам">
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
            {pts.map((p) => (
              <circle key={p.name} cx={sx(p.x)} cy={sy(p.y)} r={3 + Math.min(6, Math.sqrt(p.n))}
                fill={BCOLOR[p.r]} fillOpacity={0.55} stroke={BCOLOR[p.r]} strokeOpacity={0.9}
                {...bind(sx(p.x), sy(p.y),
                  `${p.name}\nмедиана g-спреда: ${Math.round(p.y)} bps · медиана дюрации: ${fmt.yrs(p.x)}\n${p.n} ${plu(p.n)} · рейтинг: ${p.r}`)} />
            ))}
            <text x={SC_PAD.l} y={H - 4} className="an-axis-lbl" textAnchor="start">мод. дюрация →</text>
            <text x={SC_PAD.l - 38} y={SC_PAD.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${SC_PAD.l - 38} ${SC_PAD.t + 4})`}>g-спред, bps</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

// ── box-строки (p25–медиана–p75) для рейтингов/эмитентов ──
function BoxRows({ entries, note, label }) {
  if (!entries.length) return <div className="an-empty">нет данных</div>;
  const all = entries.flatMap((e) => e.arr);
  const zmax = Math.max(...all), zmin = Math.min(0, ...all);
  const rowH = entries.length > 8 ? 22 : 30, PAD = { l: 100, r: 40, t: 6 };
  const H = entries.length * rowH + PAD.t + 8 + (note ? 14 : 0);
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
              return (
                <g key={e.key} {...bind(sx(md), y,
                  `${e.label}\nмедиана g-спреда ${Math.round(md)} bps · p25–p75 ${Math.round(q1)}–${Math.round(q3)}\n${arr.length} ${plu(arr.length)}`)}>
                  <rect x={0} y={y - rowH / 2} width={W} height={rowH} fill="transparent" />
                  <text x={PAD.l - 6} y={y + 3} className="an-axis" textAnchor="end">{e.label}</text>
                  <line x1={sx(q1)} y1={y} x2={sx(q3)} y2={y} stroke={e.color} strokeWidth={7}
                    strokeOpacity={0.35} strokeLinecap="round" />
                  <circle cx={sx(md)} cy={y} r={4} fill={e.color} />
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

function RatingDist({ rows }) {
  const entries = useMemo(() => {
    const g = {};
    for (const b of rows) {
      const z = gval(b);
      if (z == null) continue;
      (g[norm(b.rating)] ||= []).push(z);
    }
    return BUCKETS.filter((k) => g[k]?.length).map((k) => ({ key: k, label: k, arr: g[k], color: BCOLOR[k] }));
  }, [rows]);
  return <BoxRows entries={entries} label="распределение g-спреда по рейтингам" />;
}

const ISSUER_CAP = 22;
function IssuerDist({ rows }) {
  const { entries, note } = useMemo(() => {
    const arr = [];
    for (const [k, bonds] of byIssuer(rows)) {
      const zs = bonds.map(gval).filter((v) => v != null);
      if (zs.length < 2) continue;
      arr.push({ key: String(k), label: trunc(String(k)), arr: zs, color: BCOLOR[modalBucket(bonds)], md: median(zs) });
    }
    arr.sort((a, b) => b.md - a.md);
    const note = arr.length > ISSUER_CAP ? `+${arr.length - ISSUER_CAP} эмитентов ниже по g-спреду скрыто` : null;
    return { entries: arr.slice(0, ISSUER_CAP), note };
  }, [rows]);
  if (!entries.length) return <div className="an-empty">нет эмитентов с ≥2 бумагами (с валидным g-спредом)</div>;
  return <BoxRows entries={entries} note={note} label="распределение g-спреда по эмитентам" />;
}

// ── Гистограмма срочности (лет до погашения) ──
function MaturityProfile({ rows }) {
  const bins = [
    { lbl: "<1г", lo: 0, hi: 1 }, { lbl: "1–3", lo: 1, hi: 3 },
    { lbl: "3–5", lo: 3, hi: 5 }, { lbl: "5–10", lo: 5, hi: 10 },
    { lbl: ">10", lo: 10, hi: 1e9 },
  ];
  // МСК-дата (UTC+3)
  const today = new Date(Date.now() + 3 * 3600 * 1000);
  const yrs = rows
    .map((b) => (b.maturity_date ? (new Date(b.maturity_date) - today) / (365.25 * 864e5) : null))
    .filter((v) => v != null && v > 0);
  if (!yrs.length) return <div className="an-empty">нет дат погашения</div>;
  const counts = bins.map((bin) => yrs.filter((v) => v >= bin.lo && v < bin.hi).length);
  const cmax = Math.max(...counts, 1);
  const PAD = { l: 30, r: 10, t: 10, b: 24 };
  return (
    <MeasuredSvg height={150} label="профиль срочности">
      {({ W, H, bind }) => {
        const bw = (W - PAD.l - PAD.r) / bins.length;
        const sh = linearScale([0, cmax], [0, H - PAD.t - PAD.b]);
        return (
          <>
            {bins.map((bin, i) => {
              const h = sh(counts[i]);
              const x = PAD.l + i * bw + bw * 0.15;
              return (
                <g key={bin.lbl}>
                  <rect x={x} y={H - PAD.b - h} width={bw * 0.7} height={h} className="an-bar"
                    {...bind(x + bw * 0.35, H - PAD.b - h, `погашение через ${bin.lbl}\n${counts[i]} ${plu(counts[i])}`)} />
                  {counts[i] > 0 && <text x={x + bw * 0.35} y={H - PAD.b - h - 3} className="an-axis" textAnchor="middle">{counts[i]}</text>}
                </g>
              );
            })}
            <XTicks ticks={bins.map((bin, i) => ({ x: PAD.l + i * bw + bw * 0.5, label: bin.lbl }))}
              y={H - PAD.b + 14} textClass="an-axis" />
          </>
        );
      }}
    </MeasuredSvg>
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

export default function FixedAnalytics({ rows }) {
  const [groupBy, setGroupBy] = useState("rating");
  const byIss = groupBy === "issuer";
  return (
    <section className="analytics">
      <div className="an-card">
        <div className="an-title">G-СПРЕД vs ДЮРАЦИЯ
          <span className="an-hint">{byIss ? "точка = эмитент (медиана) · размер = число бумаг · цвет = рейтинг" : "точка = выпуск · цвет = рейтинг · наведи для деталей"}</span>
          <AggToggle value={groupBy} onChange={setGroupBy} />
        </div>
        {byIss ? <ScatterIssuer rows={rows} /> : <ScatterGDur rows={rows} />}
        <RatingLegend />
      </div>
      <div className="an-card">
        <div className="an-title">{byIss ? "G-СПРЕД по ЭМИТЕНТАМ" : "G-СПРЕД по РЕЙТИНГ-БАКЕТАМ"}
          <span className="an-hint">линия p25–p75 · точка = медиана · (n)</span>
          <AggToggle value={groupBy} onChange={setGroupBy} />
        </div>
        {byIss ? <IssuerDist rows={rows} /> : <RatingDist rows={rows} />}
        <RatingLegend />
      </div>
      <div className="an-card">
        <div className="an-title">ПРОФИЛЬ СРОЧНОСТИ <span className="an-hint">лет до погашения · бар = число бумаг</span></div>
        <MaturityProfile rows={rows} />
      </div>
    </section>
  );
}
