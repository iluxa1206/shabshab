import { useMemo } from "react";
import { fmt } from "../format.js";

// рейтинг-бакеты и их цвет (градация риска)
const BUCKETS = ["AAA", "AA", "A", "BBB", "BB", "B", "NR"];
const norm = (r) => {
  if (!r) return "NR";
  if (["AAA", "AA", "A", "BBB"].includes(r)) return r;
  if (["BB", "B", "CCC", "CC", "C", "D"].includes(r)) return r === "BB" || r === "B" ? r : "B";
  return "NR";
};
const BCOLOR = {
  AAA: "#2f9e44", AA: "#66a80f", A: "#f08c00", BBB: "#e8590c",
  BB: "#e03131", B: "#c92a2a", NR: "var(--mut-2)",
};

// склонение «бумага/бумаги/бумаг» по числу
const plu = (n) => {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return "бумаг";
  if (b === 1) return "бумага";
  if (b >= 2 && b <= 4) return "бумаги";
  return "бумаг";
};

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

// ── Scatter: z-спред vs spread duration, цвет = рейтинг ──
function ScatterZDur({ rows }) {
  const W = 460, H = 260, pad = { l: 44, r: 12, t: 12, b: 30 };
  const pts = rows
    .filter((b) => b.spread_dur_yrs != null && b.z_spread_bps != null && b.z_spread_bps < 3000)
    .map((b) => ({ x: b.spread_dur_yrs, y: b.z_spread_bps, r: norm(b.rating), isin: b.isin, name: b.short_name }));
  if (pts.length < 2) return <div className="an-empty">мало данных для scatter</div>;
  const xmax = Math.max(...pts.map((p) => p.x), 1);
  const ymax = Math.max(...pts.map((p) => p.y), 100);
  const ymin = Math.min(...pts.map((p) => p.y), 0);
  const sx = (x) => pad.l + (x / xmax) * (W - pad.l - pad.r);
  const sy = (y) => H - pad.b - ((y - ymin) / (ymax - ymin || 1)) * (H - pad.t - pad.b);
  const yticks = 4, xticks = Math.min(Math.ceil(xmax), 6);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="an-svg" role="img" aria-label="z-спред vs spread duration">
      {Array.from({ length: yticks + 1 }, (_, i) => {
        const yv = ymin + ((ymax - ymin) * i) / yticks;
        return (
          <g key={i}>
            <line x1={pad.l} y1={sy(yv)} x2={W - pad.r} y2={sy(yv)} className="an-grid" />
            <text x={pad.l - 6} y={sy(yv) + 3} className="an-axis" textAnchor="end">{Math.round(yv)}</text>
          </g>
        );
      })}
      {Array.from({ length: xticks + 1 }, (_, i) => {
        const xv = (xmax * i) / xticks;
        return <text key={i} x={sx(xv)} y={H - pad.b + 14} className="an-axis" textAnchor="middle">{fmt.yrs(xv)}</text>;
      })}
      {pts.map((p) => (
        <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={3.2} fill={BCOLOR[p.r]} fillOpacity={0.72}>
          <title>{`${p.name} — один выпуск\nz-спред: ${p.y} bps (НРД, к кривой ОФЗ)\nspread duration: ${fmt.yrs(p.x)}\nрейтинг: ${p.r}`}</title>
        </circle>
      ))}
      <text x={pad.l} y={H - 4} className="an-axis-lbl" textAnchor="start">spread duration →</text>
      <text x={pad.l - 38} y={pad.t + 4} className="an-axis-lbl" transform={`rotate(-90 ${pad.l - 38} ${pad.t + 4})`}>z, bps</text>
    </svg>
  );
}

// ── Распределение z по рейтинг-бакетам (p25–медиана–p75) ──
function RatingDist({ rows }) {
  const groups = useMemo(() => {
    const g = {};
    for (const b of rows) {
      if (b.z_spread_bps == null || b.z_spread_bps >= 3000) continue;
      (g[norm(b.rating)] ||= []).push(b.z_spread_bps);
    }
    return g;
  }, [rows]);
  const present = BUCKETS.filter((k) => groups[k]?.length);
  if (!present.length) return <div className="an-empty">нет данных</div>;
  const all = present.flatMap((k) => groups[k]);
  const zmax = Math.max(...all), zmin = Math.min(0, ...all);
  const W = 460, rowH = 30, pad = { l: 44, r: 40, t: 6 };
  const sx = (z) => pad.l + ((z - zmin) / (zmax - zmin || 1)) * (W - pad.l - pad.r);
  return (
    <svg viewBox={`0 0 ${W} ${present.length * rowH + pad.t + 8}`} className="an-svg" role="img" aria-label="распределение z по рейтингам">
      {present.map((k, i) => {
        const arr = groups[k];
        const q1 = quantile(arr, 0.25), md = median(arr), q3 = quantile(arr, 0.75);
        const y = pad.t + i * rowH + rowH / 2;
        return (
          <g key={k}>
            <text x={pad.l - 6} y={y + 3} className="an-axis" textAnchor="end">{k}</text>
            <line x1={sx(q1)} y1={y} x2={sx(q3)} y2={y} stroke={BCOLOR[k]} strokeWidth={7} strokeOpacity={0.35} strokeLinecap="round">
              <title>{`${k}: линия = разброс z-спреда, p25–p75 = ${Math.round(q1)}–${Math.round(q3)} bps`}</title>
            </line>
            <circle cx={sx(md)} cy={y} r={4} fill={BCOLOR[k]}>
              <title>{`${k}: точка = медиана z-спреда ${Math.round(md)} bps · ${arr.length} ${plu(arr.length)}`}</title>
            </circle>
            <text x={sx(q3) + 6} y={y + 3} className="an-axis">{Math.round(md)}<tspan className="an-mut"> ({arr.length})</tspan></text>
          </g>
        );
      })}
    </svg>
  );
}

// ── Профиль рефиксинга портфеля (watch): когда бумаги поймают новую ставку ──
function RefixProfile({ rows }) {
  const bins = [
    { lbl: "<7д", lo: 0, hi: 7 }, { lbl: "7–30", lo: 7, hi: 30 },
    { lbl: "30–90", lo: 30, hi: 90 }, { lbl: "90–180", lo: 90, hi: 180 },
    { lbl: ">180", lo: 180, hi: 1e9 },
  ];
  const vals = rows.map((b) => b.days_to_refix).filter((v) => v != null);
  if (!vals.length) return <div className="an-empty">добавьте бумаги в watchlist (★) — профиль по вашему портфелю</div>;
  const counts = bins.map((bin) => vals.filter((v) => v >= bin.lo && v < bin.hi).length);
  const cmax = Math.max(...counts, 1);
  const W = 460, H = 150, pad = { l: 30, r: 10, t: 10, b: 24 };
  const bw = (W - pad.l - pad.r) / bins.length;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="an-svg" role="img" aria-label="профиль рефиксинга">
      {bins.map((bin, i) => {
        const h = (counts[i] / cmax) * (H - pad.t - pad.b);
        const x = pad.l + i * bw + bw * 0.15;
        return (
          <g key={bin.lbl}>
            <rect x={x} y={H - pad.b - h} width={bw * 0.7} height={h} className="an-bar">
              <title>{`рефиксинг через ${bin.lbl} дн.: ${counts[i]} ${plu(counts[i])}`}</title>
            </rect>
            {counts[i] > 0 && <text x={x + bw * 0.35} y={H - pad.b - h - 3} className="an-axis" textAnchor="middle">{counts[i]}</text>}
            <text x={x + bw * 0.35} y={H - pad.b + 14} className="an-axis" textAnchor="middle">{bin.lbl}</text>
          </g>
        );
      })}
    </svg>
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

export default function AnalyticsPanel({ rows }) {
  return (
    <section className="analytics">
      <div className="an-card">
        <div className="an-title">z-спред vs SPREAD DURATION <span className="an-hint">точка = выпуск · цвет = рейтинг · наведи для деталей</span></div>
        <ScatterZDur rows={rows} />
        <RatingLegend />
      </div>
      <div className="an-card">
        <div className="an-title">z по РЕЙТИНГ-БАКЕТАМ <span className="an-hint">линия p25–p75 · точка = медиана · (n)</span></div>
        <RatingDist rows={rows} />
        <RatingLegend />
      </div>
      <div className="an-card">
        <div className="an-title">ПРОФИЛЬ РЕФИКСИНГА <span className="an-hint">дни до новой ставки · бар = число бумаг</span></div>
        <RefixProfile rows={rows} />
      </div>
    </section>
  );
}
