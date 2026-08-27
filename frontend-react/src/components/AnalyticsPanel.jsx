import { useEffect, useMemo, useState } from "react";
import { fmt, RT_BUCKETS, RT_BUCKET_COLOR, ratingBucket } from "../format.js";
import { fetchYidxHistory } from "../api.js";
import {
  linearScale, niceTicks, linePath, GridY, XTicks,
  MeasuredSvg, ChartFrame, dateTickIdx, tickLabel, spanDays,
} from "../charts/index.js";

// рейтинг-бакеты и палитра — общие для всего фронта (format.js), своей копии
// правила «куда падает CCC» тут быть не должно
const BUCKETS = RT_BUCKETS;
const norm = ratingBucket;
const BCOLOR = RT_BUCKET_COLOR;

// Y-IDX (доходность над индексом, bps) — первичная метрика панели: та же, что в
// таблице, в стакане и в истории снапшотов. Бэнд отсекает мусор от стейл/тонких
// цен неликвида (тот же, что на бэкенде для spread_daily).
const YBAND = [-1500, 3000];
const inBand = (v) => (v != null && v > YBAND[0] && v < YBAND[1] ? v : null);

// Спред бумаги для ДИАГРАММ — по СРЕДНЕВЗВЕСУ дня, а не по last price: цена
// последней сделки в неликвиде это один случайный принт (часто тонкий, часто на
// закрытии), и облако точек от него дрожало сильнее, чем реально двигался рынок.
// Средневзвес взвешен объёмом — устойчивая «цена дня».
// Число приходит с бэка (y_idx_wap_bps) посчитанным по методике — средневзвес
// для движка такая же альт-цена, как bid/ask. Фолбэк один: спред цены сделки,
// когда бумага сегодня не торговалась и средневзвеса нет.
const yval = (b) => {
  // спред средневзвеса приходит с бэка посчитанным по методике; линеаризация
  // от якоря убрана 27.08.2026 (уводила число вслед за уехавшим якорем).
  // Нет средневзвеса — берём спред цены сделки, тоже расчётный.
  const direct = inBand(b.y_idx_wap_bps);
  return direct != null ? direct : inBand(b.yield_over_index_bps);
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
const trunc = (s, n = 18) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

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

// Домен оси по данным, а НЕ от нуля: у Y-IDX разброс бывает 380…520 bps —
// шкала с нулём сплющивала всю кросс-секцию в полоску. Поля 6% размаха, чтобы
// крайние точки не липли к рамке; вырожденный набор (все значения равны)
// разводится на ±5% величины.
const padDomain = (vals, frac = 0.06) => {
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = hi - lo;
  const p = span > 0 ? span * frac : Math.max(Math.abs(hi) * 0.05, 1);
  return [lo - p, hi + p];
};

// ── Общие поля scatter-графиков ──
const SC_PAD = { l: 46, r: 14, t: 12, b: 30 };

// В полный экран график растёт, а текст — нет: viewBox равен пикселям 1:1
// (charts/useChartSize), поэтому ось остаётся 9,5 px на экране любой величины и
// на большом мониторе читается плохо. Шрифт поднимает CSS (.an-card.an-full), а
// поля под подписи обязаны вырасти вместе с ним — иначе крупное «−1500» уезжает
// за левый край, а подпись оси X налезает на цифры.
// Шаг между тиками оси X: не константа, а функция кегля. В полный экран
// подпись «1,0г» занимает ~30 px вместо ~22, а ширина графика утраивается —
// формула ширина/80 давала 20+ тиков, и они наезжали друг на друга.
const tickGap = (full) => (full ? 110 : 80);
const padFull = (pad, full) => (full ? {
  ...pad,
  l: Math.round(pad.l * 1.35),
  // справа множитель больше: крайняя подпись оси X центрирована по тику, и её
  // половина при 13 px (~19 px) шире исходного поля 14 px — «26.08» подрезалось
  r: Math.max(Math.round(pad.r * 1.8), 26),
  b: pad.b == null ? pad.b : Math.round(pad.b * 1.25),
} : pad);

// ── Scatter: Y-IDX vs spread duration, цвет = рейтинг ──
// focus: активный фильтр {type,key} — попавшие под него точки ярче, прочие гаснут;
// клик по точке ставит/снимает фильтр по эмитенту
function ScatterYidx({ rows, focus, onPick, height, full }) {
  const pts = rows
    .map((b) => ({ b, z: yval(b) }))
    .filter(({ b, z }) => b.spread_dur_yrs != null && z != null)
    .map(({ b, z }) => ({ x: b.spread_dur_yrs, y: z, r: norm(b.rating), isin: b.isin, name: b.short_name, iss: emKey(b) }));
  if (pts.length < 2) return <div className="an-empty">мало данных для scatter</div>;
  const [xlo, xmax] = padDomain(pts.map((p) => p.x));
  const xmin = Math.max(0, xlo);   // дюрация отрицательной не бывает
  const [ymin, ymax] = padDomain(pts.map((p) => p.y));
  const hit = (p) => (focus == null ? null : focus.type === "issuer" ? p.iss === focus.key : p.r === focus.key);
  return (
    <MeasuredSvg height={height} label="R-spread vs spread duration">
      {({ W, H, bind }) => {
        const P = padFull(SC_PAD, full);
        const sx = linearScale([xmin, xmax], [P.l, W - P.r]);
        const sy = linearScale([ymin, ymax], [H - P.b, P.t]);
        const nx = Math.max(3, Math.round((W - P.l - P.r) / tickGap(full)));
        return (
          <>
            <GridY ticks={niceTicks(ymin, ymax, 5)} y={sy} x1={P.l} x2={W - P.r}
              lineClass="an-grid" textClass="an-axis" label={(v) => Math.round(v)} />
            <XTicks ticks={niceTicks(xmin, xmax, nx).map((xv) => ({ x: sx(xv), label: fmt.yrs(xv) }))}
              y={H - P.b + 14} textClass="an-axis" />
            {pts.map((p) => {
              const on = hit(p);
              return (
                <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={on ? 4.4 : 3.2} fill={BCOLOR[p.r]}
                  fillOpacity={on == null ? 0.72 : on ? 0.95 : 0.1}
                  stroke={on ? "var(--fg)" : "none"} strokeWidth={on ? 1 : 0}
                  className="an-pt" onClick={() => onPick && p.iss && onPick(p.iss)}
                  {...bind(sx(p.x), sy(p.y),
                    `${p.name}\n${Math.round(p.y)} bps · ${fmt.yrs(p.x)} · ${p.r}`)} />
              );
            })}
            <text x={P.l} y={H - 4} className="an-axis-lbl" textAnchor="start">спред-дюрация →</text>
            <text x={P.l - 38} y={P.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${P.l - 38} ${P.t + 4})`}>R-spread, bps</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

// ── Scatter агрегированный по эмитенту: точка = (медиана spread dur, медиана
//    Y-IDX), размер = число бумаг, цвет = доминирующий рейтинг эмитента ──
function ScatterIssuer({ rows, focus, onPick, height, full }) {
  const pts = [];
  for (const [k, bonds] of byIssuer(rows)) {
    const zs = bonds.map(yval).filter((v) => v != null);
    const ds = bonds.map((b) => b.spread_dur_yrs).filter((v) => v != null);
    if (!zs.length || !ds.length) continue;
    pts.push({ x: median(ds), y: median(zs), r: modalBucket(bonds), n: bonds.length, name: String(k) });
  }
  if (pts.length < 2) return <div className="an-empty">мало данных для scatter</div>;
  const [xlo, xmax] = padDomain(pts.map((p) => p.x));
  const xmin = Math.max(0, xlo);   // дюрация отрицательной не бывает
  const [ymin, ymax] = padDomain(pts.map((p) => p.y));
  const hit = (p) => (focus == null ? null : focus.type === "issuer" ? p.name === focus.key : p.r === focus.key);
  return (
    <MeasuredSvg height={height} label="R-spread vs spread duration по эмитентам">
      {({ W, H, bind }) => {
        const P = padFull(SC_PAD, full);
        const sx = linearScale([xmin, xmax], [P.l, W - P.r]);
        const sy = linearScale([ymin, ymax], [H - P.b, P.t]);
        const nx = Math.max(3, Math.round((W - P.l - P.r) / tickGap(full)));
        return (
          <>
            <GridY ticks={niceTicks(ymin, ymax, 5)} y={sy} x1={P.l} x2={W - P.r}
              lineClass="an-grid" textClass="an-axis" label={(v) => Math.round(v)} />
            <XTicks ticks={niceTicks(xmin, xmax, nx).map((xv) => ({ x: sx(xv), label: fmt.yrs(xv) }))}
              y={H - P.b + 14} textClass="an-axis" />
            {pts.map((p) => {
              const on = hit(p);
              return (
                <circle key={p.name} cx={sx(p.x)} cy={sy(p.y)} r={3 + Math.min(6, Math.sqrt(p.n)) + (on ? 1 : 0)}
                  fill={BCOLOR[p.r]} fillOpacity={on == null ? 0.55 : on ? 0.85 : 0.08}
                  stroke={on ? "var(--fg)" : BCOLOR[p.r]} strokeOpacity={on == null ? 0.9 : on ? 1 : 0.12}
                  className="an-pt" onClick={() => onPick && onPick(p.name)}
                  {...bind(sx(p.x), sy(p.y),
                    `${trunc(p.name, 22)}\n${Math.round(p.y)} bps · ${fmt.yrs(p.x)} · ${p.n} шт`)} />
              );
            })}
            <text x={P.l} y={H - 4} className="an-axis-lbl" textAnchor="start">спред-дюрация →</text>
            <text x={P.l - 38} y={P.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${P.l - 38} ${P.t + 4})`}>R-spread, bps</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

// ── Общий рендер box-строк (p25–медиана–p75) для рейтингов/эмитентов ──
// kind — измерение строк ("issuer"/"rating"): гасим только когда активный фильтр
// того же измерения, иначе фильтр по эмитенту гасил бы весь рейтинг-график.
function BoxRows({ entries, note, label, kind, focus, onPick, rowH: rowHIn, full }) {
  // pad.l 140: подпись «Балтийский лизинг…» (18 симв.) не влезает в 100px
  const PAD = padFull({ l: 140, r: 44, t: 6 }, full);
  const rowH = rowHIn || (entries.length > 8 ? 22 : 30);
  const H = entries.length * rowH + PAD.t + 8 + (note ? 14 : 0);
  if (!entries.length) return <div className="an-empty">нет данных</div>;
  const all = entries.flatMap((e) => e.arr);
  const [zmin, zmax] = padDomain(all, 0.04);
  const act = focus && focus.type === kind ? focus.key : null;
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
              const on = act != null && e.key === act;
              const dim = act != null && !on;
              const tip = arr.length > 1
                ? `${trunc(e.label, 22)}\n${Math.round(md)} (${Math.round(q1)}–${Math.round(q3)}) · n${arr.length}`
                : `${trunc(e.label, 22)}\n${Math.round(md)} · n${arr.length}`;
              return (
                <g key={e.key} className={onPick ? "an-row-pick" : undefined} opacity={dim ? 0.25 : 1}
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
                  <text x={Math.min(sx(q3) + 6, W - PAD.r + 4)} y={y + 3} className="an-axis">
                    {Math.round(md)}<tspan className="an-mut"> ({arr.length})</tspan></text>
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
        const z = yval(b);
        const shown = z != null && b.spread_dur_yrs != null;
        const why = b.yield_over_index_bps == null ? "нет R-spread (нет цены)" : z == null ? "R-spread вне бэнда" : "нет дюрации";
        return (
          <span key={b.isin} className={"an-sel-chip" + (shown ? "" : " off")}
            title={shown ? `${b.short_name}: R-spread ${Math.round(z)} bps · ${fmt.yrs(b.spread_dur_yrs)}` : `${b.short_name}: не на графике — ${why}`}>
            {b.short_name}{shown ? ` ${Math.round(z)}` : " ✕"}
          </span>
        );
      })}
      <button type="button" className="an-sel-clear" onClick={onClear} aria-label="сбросить фильтр">сброс</button>
    </div>
  );
}

// ── Распределение Y-IDX по рейтинг-бакетам (p25–медиана–p75) ──
function RatingDist({ rows, focus, onPick, rowH, full }) {
  const entries = useMemo(() => {
    const g = {};
    for (const b of rows) {
      const z = yval(b);
      if (z == null) continue;
      (g[norm(b.rating)] ||= []).push(z);
    }
    return BUCKETS.filter((k) => g[k]?.length).map((k) => ({ key: k, label: k, arr: g[k], color: BCOLOR[k] }));
  }, [rows]);
  return <BoxRows entries={entries} label="распределение R-spread по рейтингам"
    kind="rating" focus={focus} onPick={onPick} rowH={rowH} full={full} />;
}

// ── Распределение Y-IDX по эмитентам (сорт по медиане, топ-N) ──
const ISSUER_CAP = 22;
function IssuerDist({ rows, focus, onPick, cap = ISSUER_CAP, rowH, full }) {
  const { entries, note } = useMemo(() => {
    // одиночные эмитенты тоже в списке: точка-медиана без полосы p25–p75
    // (раньше скрывались правилом ≥2 бумаг — watchlist из одиночек давал пустую панель)
    const arr = [];
    for (const [k, bonds] of byIssuer(rows)) {
      const zs = bonds.map(yval).filter((v) => v != null);
      if (!zs.length) continue;
      arr.push({ key: String(k), label: trunc(String(k)), arr: zs, color: BCOLOR[modalBucket(bonds)], md: median(zs) });
    }
    arr.sort((a, b) => b.md - a.md);
    const note = arr.length > cap ? `+${arr.length - cap} эмитентов ниже по R-spread скрыто` : null;
    return { entries: arr.slice(0, cap), note };
  }, [rows, cap]);
  if (!entries.length) return <div className="an-empty">нет эмитентов с валидным R-spread</div>;
  return <BoxRows entries={entries} note={note} label="распределение R-spread по эмитентам"
    kind="issuer" focus={focus} onPick={onPick} rowH={rowH} full={full} />;
}

// ── История медианного Y-IDX по рейтингам/эмитентам (точные дневные снапшоты) ──
const PERIODS = [["1м", 30], ["3м", 91], ["6м", 182], ["12м", 365]];
// палитра линий эмитентов (рейтинг-цвета заняты бакетами); РЫНОК — нейтральный
const ICOLORS = ["#4f9cf9", "#f9a04f", "#3fbf7f", "#e05c66", "#b07cf9", "#3fc6c6", "#d4b83f", "#f97cc0"];
const YH_PAD = { l: 46, r: 14, t: 12, b: 30 };
const MARKET = "РЫНОК";

// МСК-дата: снапшот спредов пишется вечером, поэтому днём история заканчивается
// вчера — сегодняшний день дорисовываем сами (см. withToday)
const mskToday = () =>
  new Date(Date.now() + 3 * 3600 * 1000).toISOString().slice(0, 10);

/**
 * Достраивает историю СЕГОДНЯШНЕЙ точкой из живых строк — медианой того же
 * спреда по средневзвесу, что показывают две другие диаграммы.
 *
 * История приезжает из дневной свёртки баров (bar_daily), которая закрывается
 * по итогам дня, поэтому в течение сессии линия обрывалась на вчера и «динамика»
 * не показывала того, что видно рядом на scatter. Точка считается по тем же
 * группам (рейтинг-бакет / эмитент) и только для серий, которые в истории уже
 * есть, — новых линий не появляется. База та же, что у истории: средневзвес.
 */
function withToday(data, rows, byIss) {
  const dates = data.dates || [], series = data.series || [];
  const today = mskToday();
  if (!dates.length || !series.length || dates[dates.length - 1] >= today) {
    return data;
  }
  const groups = new Map();
  for (const b of rows) {
    const key = byIss ? emKey(b) : norm(b.rating);
    const z = yval(b);
    if (!key || z == null) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(z);
  }
  const all = [...groups.values()].flat();
  const nextSeries = series.map((s) => {
    const vals = s.key === MARKET ? all : groups.get(s.key);
    if (!vals || !vals.length) return s;
    return { ...s, points: [...s.points, { date: today, med: median(vals), n: vals.length }] };
  });
  return { dates: [...dates, today], series: nextSeries };
}

function YidxHistory({ groupBy, rows, period, focus, onPick, height, full }) {
  const byIss = groupBy === "issuer";
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  // стабильный ключ фильтра: rows пересоздаются каждым поллом с тем же составом —
  // рефетч только при реальной смене набора ISIN
  const isinsKey = useMemo(() => rows.map((b) => b.isin).sort().join(","), [rows]);
  useEffect(() => {
    if (!isinsKey) { setData({ dates: [], series: [] }); return; }
    const ac = new AbortController();
    setErr(null);
    setData(null);
    fetchYidxHistory(period, byIss ? "issuer" : "rating", isinsKey.split(","), ac.signal)
      .then(setData)
      .catch((e) => { if (e.name !== "AbortError") setErr(e.message || "ошибка"); });
    return () => ac.abort();
  }, [period, byIss, isinsKey]);

  if (err) return <div className="an-empty">не загрузилось: {err}</div>;
  if (!data) return <div className="an-empty">загрузка…</div>;
  const { dates, series } = withToday(data, rows, byIss);
  if (!dates.length || !series.length) return <div className="an-empty">нет истории снапшотов за период</div>;

  const colorOf = (s, i) => (byIss
    ? (s.key === MARKET ? "var(--mut-2)" : ICOLORS[i % ICOLORS.length])
    : BCOLOR[s.key] || "var(--mut-2)");
  // активная серия текущего измерения: она яркая, остальные гаснут
  const act = focus && focus.type === (byIss ? "issuer" : "rating") ? focus.key : null;
  const di = new Map(dates.map((d, i) => [d, i]));
  const span = spanDays(dates);
  // точки для nearest-hover — индексы дат (crosshair общий для всех серий)
  const idxPts = dates.map((d, i) => ({ i, date: d }));

  const build = (g) => {
    const vals = series.flatMap((s) => s.points.map((p) => p.med));
    // домен по данным, не от нуля: медианы Y-IDX ходят в узкой полосе
    const [ymin, ymax] = padDomain(vals);
    const sx = linearScale([0, Math.max(dates.length - 1, 1)], [g.x0, g.x1]);
    const sy = linearScale([ymin, ymax], [g.y0, g.y1]);
    const nx = Math.max(3, Math.min(8, Math.round(g.iw / tickGap(full))));
    return {
      sx, sy,
      yTicks: niceTicks(ymin, ymax, 5),
      yFormat: (v) => Math.round(v),
      xTicks: dateTickIdx(dates, nx).map((i) => ({ x: sx(i), label: tickLabel(dates[i], span) })),
    };
  };

  return (
    <>
      <ChartFrame
        height={height} pad={padFull(YH_PAD, full)} label="динамика R-spread"
        data={idxPts} build={build} px={(p, s) => s.sx(p.i)}
        tooltip={(p) => {
          // компактный тултип: при активном фильтре — только его линия,
          // иначе топ-4 серии по значению (полный список перекрывал график)
          const vis = act != null ? series.filter((s) => s.key === act) : series;
          const at = vis
            .map((s) => ({ s, pt: s.points.find((q) => q.date === p.date) }))
            .filter((x) => x.pt)
            .sort((a, b) => b.pt.med - a.pt.med)
            .slice(0, act != null ? 1 : 4);
          return (
            <>
              <div className="an-tt-h">{fmt.date(p.date)}</div>
              {at.map(({ s, pt }) => (
                <div key={s.key} style={{ color: colorOf(s, series.indexOf(s)) }}>
                  {trunc(s.key, 10)} {Math.round(pt.med)} <span className="an-tt-n">n{pt.n}</span>
                </div>
              ))}
            </>
          );
        }}
        overlay={(s, g) => (
          <text x={g.x0 - 38} y={g.y1 + 4} className="an-axis-lbl"
            transform={`rotate(-90 ${g.x0 - 38} ${g.y1 + 4})`}>R-spread, bps</text>
        )}
      >
        {(s) => series.map((ser, gi) => {
          const pts = ser.points.map((p) => ({ x: di.get(p.date), y: p.med }));
          const c = colorOf(ser, gi);
          const on = act == null ? null : ser.key === act;
          const d = pts.length > 1 ? linePath(pts, (p) => s.sx(p.x), (p) => s.sy(p.y)) : null;
          const pickable = onPick && ser.key !== MARKET;
          return (
            <g key={ser.key} opacity={on === false ? 0.15 : 1}
              className={pickable ? "an-row-pick" : undefined}
              onClick={pickable ? () => onPick(ser.key) : undefined}>
              {d
                ? <>
                    {/* широкая прозрачная копия — попасть по линии мышью */}
                    {pickable && <path d={d} fill="none" stroke="transparent" strokeWidth={10} />}
                    <path d={d} fill="none" stroke={c}
                      strokeWidth={ser.key === MARKET ? 1 : on ? 2.4 : 1.6}
                      strokeDasharray={ser.key === MARKET ? "4 3" : undefined} />
                  </>
                : pts.map((p) => <circle key={p.x} cx={s.sx(p.x)} cy={s.sy(p.y)} r={2.5} fill={c} />)}
            </g>
          );
        })}
      </ChartFrame>
      <div className="an-legend">
        {series.map((s, i) => {
          const on = act == null ? null : s.key === act;
          return (
            <button key={s.key} type="button" className={"an-leg-btn" + (on === false ? " dim" : "")}
              onClick={() => onPick && s.key !== MARKET && onPick(s.key)}
              aria-pressed={on === true}
              title={s.key === MARKET ? s.key : `${s.key} — клик: фильтр`}>
              <span className="an-leg-swatch" style={{ background: colorOf(s, i) }} />
              {trunc(s.key)} <span className="an-mut2">{Math.round(s.points[s.points.length - 1].med)}</span>
            </button>
          );
        })}
      </div>
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

// Карточка графика: заголовок с контролами (переключатели живут ЗДЕСЬ, а не в
// потоке тела — там float перекрывался позиционированным .cf-box и клики по
// кнопкам периода не доходили) + кнопка «на весь экран».
function AnCard({ title, hint, ctl, full, onToggleFull, children }) {
  return (
    <div className={"an-card" + (full ? " an-full" : "")}>
      <div className="an-title an-head">
        <span className="an-title-txt">{title}</span>
        <span className="an-title-ctl">
          {ctl}
          <button type="button" className="an-full-btn" onClick={onToggleFull}
            aria-pressed={full} title={full ? "свернуть (Esc)" : "на весь экран"}
            aria-label={full ? "свернуть" : "на весь экран"}>{full ? "✕" : "⤢"}</button>
        </span>
      </div>
      {hint && <div className="an-hint an-sub">{hint}</div>}
      {children}
    </div>
  );
}

// высота графика: в обычной карточке фикс, в полноэкранной — по окну
function useViewportH() {
  const [h, setH] = useState(() => (typeof window === "undefined" ? 800 : window.innerHeight));
  useEffect(() => {
    const f = () => setH(window.innerHeight);
    window.addEventListener("resize", f);
    return () => window.removeEventListener("resize", f);
  }, []);
  return h;
}

const SC_H = 260;
const YH_H = 220;

// focus/onFocus — активный фильтр аналитики {type:"issuer"|"rating", key}.
// Живёт в App: по нему же фильтруется таблица под панелью; повторный клик
// снимает фильтр и таблица возвращается к прежнему набору.
export default function AnalyticsPanel({ rows, focus = null, onFocus }) {
  const [groupBy, setGroupBy] = useState("rating");
  const [period, setPeriod] = useState(91);
  const [full, setFull] = useState(null); // "scatter" | "dist" | "hist"
  const vh = useViewportH();
  const byIss = groupBy === "issuer";
  const set = (f) => onFocus && onFocus(f);
  const toggle = (type) => (key) =>
    set(focus && focus.type === type && focus.key === key ? null : { type, key });
  const pickIssuer = toggle("issuer");
  const pickRating = toggle("rating");

  // смена агрегации меняет измерение фильтра: старый выбор в новых графиках
  // не подсвечивается, а таблица оставалась бы сужённой «молча» — снимаем
  useEffect(() => { onFocus && onFocus(null); }, [groupBy]);   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!full) return;
    const f = (e) => { if (e.key === "Escape") setFull(null); };
    window.addEventListener("keydown", f);
    return () => window.removeEventListener("keydown", f);
  }, [full]);

  const fullBtn = (k) => ({ full: full === k, onToggleFull: () => setFull((s) => (s === k ? null : k)) });
  const bigH = Math.max(320, vh - 230);
  const scH = full === "scatter" ? bigH : SC_H;
  const yhH = full === "hist" ? bigH : YH_H;
  // box-график в полный экран: строки растягиваются по высоте окна (иначе
  // 3 рейтинг-бакета висели полоской вверху пустого экрана), эмитентов
  // показываем столько, сколько влезает при комфортной высоте строки
  const distN = Math.max(1, byIss
    ? Math.min(new Set(rows.map(emKey).filter(Boolean)).size, 40)
    : new Set(rows.map((b) => norm(b.rating))).size);
  const distRowH = full === "dist"
    ? Math.max(24, Math.min(120, Math.floor((vh - 200) / distN)))
    : undefined;
  const distCap = full === "dist" ? Math.max(ISSUER_CAP, distN) : ISSUER_CAP;

  const periodCtl = (
    <span className="an-toggle" role="group" aria-label="период">
      {PERIODS.map(([l, d]) => (
        <button key={d} type="button" className={"an-tgl-btn" + (period === d ? " on" : "")}
          aria-pressed={period === d} onClick={() => setPeriod(d)}>{l}</button>
      ))}
    </span>
  );
  const aggCtl = <AggToggle value={groupBy} onChange={setGroupBy} />;

  return (
    <section className={"analytics" + (full ? " has-full" : "")}>
      <AnCard title="R-spread vs SPREAD DURATION" ctl={aggCtl} {...fullBtn("scatter")}
        hint={byIss ? "спред по средневзвесу дня · точка = эмитент (медиана) · размер = число бумаг · клик = фильтр"
                    : "спред по средневзвесу дня · точка = выпуск · цвет = рейтинг · клик = фильтр по эмитенту"}>
        {byIss ? <ScatterIssuer rows={rows} focus={focus} onPick={pickIssuer} height={scH} full={full === "scatter"} />
               : <ScatterYidx rows={rows} focus={focus} onPick={pickIssuer} height={scH} full={full === "scatter"} />}
        {focus?.type === "issuer" && <IssuerDetail rows={rows} issuer={focus.key} onClear={() => set(null)} />}
        <RatingLegend />
      </AnCard>

      <AnCard title={byIss ? "R-spread по ЭМИТЕНТАМ" : "R-spread по РЕЙТИНГ-БАКЕТАМ"} ctl={aggCtl} {...fullBtn("dist")}
        hint="спред по средневзвесу дня · линия p25–p75 · точка = медиана · (n) · клик = фильтр">
        {byIss
          ? <IssuerDist rows={rows} focus={focus} onPick={pickIssuer} cap={distCap} rowH={distRowH}
              full={full === "dist"} />
          : <RatingDist rows={rows} focus={focus} onPick={pickRating} rowH={distRowH}
              full={full === "dist"} />}
        <RatingLegend />
      </AnCard>

      <AnCard title="R-spread ДИНАМИКА" ctl={<>{periodCtl}{aggCtl}</>} {...fullBtn("hist")}
        hint={byIss ? "спред по средневзвесу дня · медиана по топ-эмитентам · пунктир = рынок · клик по линии = фильтр"
                    : "спред по средневзвесу дня · медиана по рейтинг-бакетам · клик по линии = фильтр"}>
        <YidxHistory groupBy={groupBy} rows={rows} period={period} height={yhH}
          focus={focus} onPick={byIss ? pickIssuer : pickRating} full={full === "hist"} />
      </AnCard>
    </section>
  );
}

// матчер фильтра аналитики для таблицы (используется в App)
export const focusMatch = (focus) => {
  if (!focus) return null;
  return focus.type === "issuer"
    ? (b) => emKey(b) === focus.key
    : (b) => norm(b.rating) === focus.key;
};
