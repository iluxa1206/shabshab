import { useEffect, useMemo, useState } from "react";
import { fmt } from "../format.js";
import { fetchYidxHistory } from "../api.js";
import { linearScale, linTicks, linePath, GridY, XTicks } from "../charts/index.js";

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

// ── Scatter: DM vs spread duration, цвет = рейтинг ──
// sel/onPick: выбранный эмитент — его выпуски подсвечены, остальные пригашены;
// клик по точке выбирает/сбрасывает эмитента
function ScatterZDur({ rows, sel, onPick }) {
  const W = 460, H = 260, pad = { l: 44, r: 12, t: 12, b: 30 };
  const pts = rows
    .map((b) => ({ b, z: dmval(b) }))
    .filter(({ b, z }) => b.spread_dur_yrs != null && z != null)
    .map(({ b, z }) => ({ x: b.spread_dur_yrs, y: z, r: norm(b.rating), isin: b.isin, name: b.short_name, iss: emKey(b) }));
  if (pts.length < 2) return <div className="an-empty">мало данных для scatter</div>;
  const xmax = Math.max(...pts.map((p) => p.x), 1);
  const ymax = Math.max(...pts.map((p) => p.y), 100);
  const ymin = Math.min(...pts.map((p) => p.y), 0);
  const sx = linearScale([0, xmax], [pad.l, W - pad.r]);
  const sy = linearScale([ymin, ymax], [H - pad.b, pad.t]);
  const nx = Math.min(Math.ceil(xmax), 6);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="an-svg" role="img" aria-label="DM vs spread duration">
      <GridY ticks={linTicks(ymin, ymax, 4)} y={sy} x1={pad.l} x2={W - pad.r}
        lineClass="an-grid" textClass="an-axis" label={(v) => Math.round(v)} />
      <XTicks ticks={linTicks(0, xmax, nx).map((xv) => ({ x: sx(xv), label: fmt.yrs(xv) }))}
        y={H - pad.b + 14} textClass="an-axis" />
      {pts.map((p) => {
        const on = sel != null && p.iss === sel;
        return (
          <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={on ? 4.4 : 3.2} fill={BCOLOR[p.r]}
            fillOpacity={sel == null ? 0.72 : on ? 0.95 : 0.12}
            stroke={on ? "var(--fg)" : "none"} strokeWidth={on ? 1 : 0}
            className="an-pt" onClick={() => onPick && p.iss && onPick(p.iss)}>
            <title>{`${p.name} — ${p.iss || "без эмитента"}\nDM: ${p.y} bps (Fabozzi)\nspread duration: ${fmt.yrs(p.x)}\nрейтинг: ${p.r}\nклик — фильтр по эмитенту`}</title>
          </circle>
        );
      })}
      <text x={pad.l} y={H - 4} className="an-axis-lbl" textAnchor="start">спред-дюрация →</text>
      <text x={pad.l - 38} y={pad.t + 4} className="an-axis-lbl" transform={`rotate(-90 ${pad.l - 38} ${pad.t + 4})`}>DM, bps</text>
    </svg>
  );
}

// ── Scatter агрегированный по эмитенту: точка = (медиана spread dur, медиана DM),
//    размер = число бумаг, цвет = доминирующий рейтинг эмитента ──
function ScatterIssuer({ rows, sel, onPick }) {
  const W = 460, H = 260, pad = { l: 44, r: 12, t: 12, b: 30 };
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
  const sx = linearScale([0, xmax], [pad.l, W - pad.r]);
  const sy = linearScale([ymin, ymax], [H - pad.b, pad.t]);
  const nx = Math.min(Math.ceil(xmax), 6);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="an-svg" role="img" aria-label="DM vs spread duration по эмитентам">
      <GridY ticks={linTicks(ymin, ymax, 4)} y={sy} x1={pad.l} x2={W - pad.r}
        lineClass="an-grid" textClass="an-axis" label={(v) => Math.round(v)} />
      <XTicks ticks={linTicks(0, xmax, nx).map((xv) => ({ x: sx(xv), label: fmt.yrs(xv) }))}
        y={H - pad.b + 14} textClass="an-axis" />
      {pts.map((p) => {
        const on = sel != null && p.name === sel;
        return (
          <circle key={p.name} cx={sx(p.x)} cy={sy(p.y)} r={3 + Math.min(6, Math.sqrt(p.n)) + (on ? 1 : 0)}
            fill={BCOLOR[p.r]} fillOpacity={sel == null ? 0.55 : on ? 0.85 : 0.1}
            stroke={on ? "var(--fg)" : BCOLOR[p.r]} strokeOpacity={sel == null ? 0.9 : on ? 1 : 0.15}
            className="an-pt" onClick={() => onPick && onPick(p.name)}>
            <title>{`${p.name}\nмедиана DM: ${Math.round(p.y)} bps · медиана spread dur: ${fmt.yrs(p.x)}\n${p.n} ${plu(p.n)} · рейтинг: ${p.r}\nклик — фильтр по эмитенту`}</title>
          </circle>
        );
      })}
      <text x={pad.l} y={H - 4} className="an-axis-lbl" textAnchor="start">спред-дюрация →</text>
      <text x={pad.l - 38} y={pad.t + 4} className="an-axis-lbl" transform={`rotate(-90 ${pad.l - 38} ${pad.t + 4})`}>DM, bps</text>
    </svg>
  );
}

// ── Общий рендер box-строк (p25–медиана–p75) для рейтингов/эмитентов ──
function BoxRows({ entries, note, label, sel, onPick }) {
  if (!entries.length) return <div className="an-empty">нет данных</div>;
  const all = entries.flatMap((e) => e.arr);
  const zmax = Math.max(...all), zmin = Math.min(0, ...all);
  // pad.l 140: подпись «Балтийский лизинг…» (18 симв.) не влезала в 100px и
  // клипалась слева viewBox'ом («тийский лизинг…»)
  const W = 460, rowH = entries.length > 8 ? 22 : 30, pad = { l: 140, r: 40, t: 6 };
  const H = entries.length * rowH + pad.t + 8 + (note ? 14 : 0);
  const sx = linearScale([zmin, zmax], [pad.l, W - pad.r]);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="an-svg" role="img" aria-label={label}>
      {entries.map((e, i) => {
        const arr = e.arr;
        const q1 = quantile(arr, 0.25), md = median(arr), q3 = quantile(arr, 0.75);
        const y = pad.t + i * rowH + rowH / 2;
        const on = sel != null && e.key === sel;
        const dim = sel != null && !on;
        return (
          <g key={e.key} className={onPick ? "an-row-pick" : undefined} opacity={dim ? 0.3 : 1}
            onClick={onPick ? () => onPick(e.key) : undefined}>
            {/* невидимая подложка — кликабельна вся строка, не только элементы */}
            {onPick && <rect x={0} y={y - rowH / 2} width={W} height={rowH} fill="transparent" />}
            <text x={pad.l - 6} y={y + 3} className="an-axis" textAnchor="end"
              fontWeight={on ? 700 : undefined} fill={on ? "var(--fg)" : undefined}>{e.label}</text>
            {arr.length > 1 && (
              <line x1={sx(q1)} y1={y} x2={sx(q3)} y2={y} stroke={e.color} strokeWidth={7} strokeOpacity={0.35} strokeLinecap="round">
                <title>{`${e.label}: линия = разброс DM, p25–p75 = ${Math.round(q1)}–${Math.round(q3)} bps`}</title>
              </line>
            )}
            <circle cx={sx(md)} cy={y} r={on ? 5 : 4} fill={e.color} stroke={on ? "var(--fg)" : "none"} strokeWidth={on ? 1 : 0}>
              <title>{`${e.label}: точка = медиана DM ${Math.round(md)} bps · ${arr.length} ${plu(arr.length)}`}</title>
            </circle>
            <text x={sx(q3) + 6} y={y + 3} className="an-axis">{Math.round(md)}<tspan className="an-mut"> ({arr.length})</tspan></text>
          </g>
        );
      })}
      {note && <text x={W - pad.r} y={H - 4} className="an-mut" textAnchor="end" fontSize="9">{note}</text>}
    </svg>
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

function YidxHistory({ groupBy }) {
  const byIss = groupBy === "issuer";
  const [period, setPeriod] = useState(91);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    const ac = new AbortController();
    setErr(null);
    fetchYidxHistory(period, byIss ? "issuer" : "rating", ac.signal)
      .then(setData)
      .catch((e) => { if (e.name !== "AbortError") setErr(e.message || "ошибка"); });
    return () => ac.abort();
  }, [period, byIss]);

  const body = useMemo(() => {
    if (err) return <div className="an-empty">не загрузилось: {err}</div>;
    if (!data) return <div className="an-empty">загрузка…</div>;
    const { dates, series } = data;
    if (!dates.length || !series.length) return <div className="an-empty">нет истории снапшотов за период</div>;
    const W = 460, H = 220, pad = { l: 44, r: 12, t: 12, b: 30 };
    const di = new Map(dates.map((d, i) => [d, i]));
    const vals = series.flatMap((s) => s.points.map((p) => p.med));
    const ymax = Math.max(...vals), ymin = Math.min(...vals, 0);
    const sx = linearScale([0, Math.max(dates.length - 1, 1)], [pad.l, W - pad.r]);
    const sy = linearScale([ymin, ymax], [H - pad.b, pad.t]);
    const nTicks = Math.min(dates.length, 5);
    const tickIdx = Array.from({ length: nTicks }, (_, i) => Math.round((i * (dates.length - 1)) / Math.max(nTicks - 1, 1)));
    const colorOf = (s, i) => (byIss
      ? (s.key === "РЫНОК" ? "var(--mut-2)" : ICOLORS[i % ICOLORS.length])
      : BCOLOR[s.key] || "var(--mut-2)");
    return (
      <>
        <svg viewBox={`0 0 ${W} ${H}`} className="an-svg" role="img" aria-label="динамика Y-IDX">
          <GridY ticks={linTicks(ymin, ymax, 4)} y={sy} x1={pad.l} x2={W - pad.r}
            lineClass="an-grid" textClass="an-axis" label={(v) => Math.round(v)} />
          <XTicks ticks={[...new Set(tickIdx)].map((i) => ({ x: sx(i), label: dmm(dates[i]) }))}
            y={H - pad.b + 14} textClass="an-axis" />
          {series.map((s, i) => {
            const pts = s.points.map((p) => ({ x: di.get(p.date), y: p.med }));
            const c = colorOf(s, i);
            const last = s.points[s.points.length - 1];
            return (
              <g key={s.key}>
                {pts.length > 1
                  ? <path d={linePath(pts, (p) => sx(p.x), (p) => sy(p.y))} fill="none" stroke={c}
                      strokeWidth={s.key === "РЫНОК" ? 1 : 1.6} strokeDasharray={s.key === "РЫНОК" ? "4 3" : undefined} />
                  : pts.map((p) => <circle key={p.x} cx={sx(p.x)} cy={sy(p.y)} r={2.5} fill={c} />)}
                <title>{`${s.key}: медиана Y-IDX, последняя ${Math.round(last.med)} bps (${last.n} ${plu(last.n)})`}</title>
              </g>
            );
          })}
          <text x={pad.l - 38} y={pad.t + 4} className="an-axis-lbl" transform={`rotate(-90 ${pad.l - 38} ${pad.t + 4})`}>Y-IDX, bps</text>
        </svg>
        <div className="an-legend">
          {series.map((s, i) => (
            <span key={s.key} className="an-leg-item" title={s.key}>
              <span className="an-leg-swatch" style={{ background: colorOf(s, i) }} />
              {trunc(s.key)} <span className="an-mut2">{Math.round(s.points[s.points.length - 1].med)}</span>
            </span>
          ))}
        </div>
        {data.exact_from && <div className="an-note">точная история копится с {dmm(data.exact_from)} — глубже данных пока нет</div>}
      </>
    );
  }, [data, err, byIss]);

  return (
    <>
      <span className="an-toggle" role="group" aria-label="период">
        {PERIODS.map(([l, d]) => (
          <button key={d} type="button" className={"an-tgl-btn" + (period === d ? " on" : "")}
            aria-pressed={period === d} onClick={() => setPeriod(d)}>{l}</button>
        ))}
      </span>
      {body}
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
          <span className="an-hint">{byIss ? "медиана по топ-эмитентам · пунктир = рынок" : "медиана по рейтинг-бакетам"}</span>
          <AggToggle value={groupBy} onChange={setGroupBy} />
        </div>
        <YidxHistory groupBy={groupBy} />
      </div>
    </section>
  );
}
