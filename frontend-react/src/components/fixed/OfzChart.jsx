import { fmt } from "../../format.js";
import {
  linearScale, linTicks, linePath, GridY, GridX, XTicks, MeasuredSvg,
  termTicks, placeLabels, stackedBars,
} from "../../charts/index.js";

// ГРАФИК витрины ОФЗ: scatter «доходность × дюрация» поверх КБД, под ним полоса
// объёма торгов, поверх — сдвиг к прошлой дате (вторая кривая + «тени» точек).
//
// Компонент ЧИСТО рисующий: данные (список фиксов, КБД, объёмы, as-of) тянет и
// мёржит OfzDesk, здесь ни одного расчёта доходности/дюрации нет — только
// разность уже посчитанных чисел (ΔYTM в бп, Δ КБД по совпадающим тенорам).
// Оба SVG меряют один и тот же контейнер (MeasuredSvg), поэтому X-шкалы
// scatter'а и полосы объёма совпадают без общего состояния.

const BASE_LABEL = {
  wap: "СР.ВЗВЕС", last: "ПОСЛЕДНЯЯ", bid: "BID", ask: "OFFER",
};

// Поля одинаковые у scatter'а и полосы объёма — иначе столбик встал бы не под
// своей точкой.
const SC_PAD = { l: 46, r: 14, t: 14, b: 32 };
const VOL_PAD = { l: 46, r: 14, t: 6, b: 4 };
const VOL_H = 72;

// Теноры, по которым читается сдвиг кривой: стандартные точки zcyc, без
// интерполяции — берём ТОЛЬКО совпадающие теноры двух наборов points.
const DELTA_TENORS = [1, 2, 3, 5, 7, 10, 15];

// Подписи бордов для тултипа объёма. Бэк может прислать свой словарь
// (board_labels в ответе) — он приоритетнее; этот — запас на случай, когда его
// нет, чтобы в плашке не висел голый код.
const BOARD_FALLBACK = {
  TQOB: "стакан T+1", TQOY: "стакан, юани", TQOD: "стакан, доллары", TQOE: "стакан, евро",
  PSOB: "РПС", PTOB: "РПС T+", PSEU: "РПС, евро", PSOY: "РПС, юани",
};

// Ручка /gcurve исторически отдаёт points объектами {years, yield_pct}, ТЗ на
// date-вариант описывает пары [tau, yield]. Принимаем оба — фронт не должен
// падать от того, какой из двух форматов выбрал бэк.
export function normCurve(points) {
  return (points || []).map((p) => (
    Array.isArray(p) ? { years: +p[0], yield_pct: +p[1] }
      : { years: +p.years, yield_pct: +p.yield_pct }
  )).filter((p) => Number.isFinite(p.years) && Number.isFinite(p.yield_pct));
}

// Δ КБД по тенорам: разность двух наборов точек по СОВПАДАЮЩИМ тенорам. Это
// не интерполяция (правило страницы: кривая на фронте не считается) — тенор,
// которого нет в одном из наборов, просто не показываем.
export function curveDelta(now, prev, tenors = DELTA_TENORS) {
  const a = normCurve(now), b = normCurve(prev);
  const at = (arr, t) => arr.find((p) => Math.abs(p.years - t) < 1e-6);
  return tenors.map((t) => {
    const x = at(a, t), y = at(b, t);
    return x && y ? { tenor: t, bps: (x.yield_pct - y.yield_pct) * 100 } : null;
  }).filter(Boolean);
}

const tenorLbl = (t) => `${t}Y`;

// Строка «Δ КБД: 1Y +8 · 3Y +5 · 10Y −2 бп» над графиком.
export function CurveDeltaStrip({ now, prev, date, requested }) {
  const d = curveDelta(now, prev);
  return (
    <div className="ofz-dstrip" data-testid="ofz-dstrip">
      <span className="ofz-dstrip-k">Δ КБД{date ? ` к ${fmt.date(date)}` : ""}</span>
      {requested && date && requested !== date && (
        <span className="ofz-dstrip-note"> (ближайший торговый к {fmt.date(requested)})</span>
      )}
      {": "}
      {d.length === 0
        ? <span className="mut">нет совпадающих теноров</span>
        : d.map((x, i) => (
          <span key={x.tenor} className="ofz-dstrip-t">
            {i > 0 && " · "}
            {tenorLbl(x.tenor)}{" "}
            <span className={x.bps >= 0 ? "pos" : "neg"}>{fmt.devBps(x.bps)}</span>
          </span>
        ))}
      {d.length > 0 && " бп"}
    </div>
  );
}

// Тултип точки: то же, что раньше, плюс строка ΔYTM, когда включён сдвиг.
function pointTip(p, cmpDate) {
  let s = `${p.name}\nYTM ${fmt.pct(p.y)} · КБД ${fmt.pct(p.curve)}\n`
    + `отклонение ${fmt.devBps(p.g)} б.п. · дюрация ${fmt.yrs(p.x)}\n`
    + `цена ${fmt.pct(p.px) ?? "—"} (${BASE_LABEL[p.base] ?? p.base})`;
  if (p.y0 != null) {
    const d = (p.y - p.y0) * 100;
    s += `\nΔYTM ${fmt.devBps(d)} бп${cmpDate ? ` с ${fmt.date(cmpDate)}` : ""}`
      + ` (было ${fmt.pct(p.y0)}${p.x0 != null ? ` · дюрация ${fmt.yrs(p.x0)}` : ""})`;
  }
  return s;
}

// ── Scatter: YTM × дюрация поверх КБД (+ вторая кривая и тени точек) ──
function CurveScatter({ pts, curve, curveCmp, cmpDate, xmax, labels, onOpen }) {
  const cIn = curve.filter((c) => c.years <= xmax);
  const cCmpIn = (curveCmp || []).filter((c) => c.years <= xmax);
  // Домен Y — по бумагам, теням И обеим кривым в этом же окне сроков: кривая,
  // ушедшая за край, читалась бы как «все бумаги дорогие».
  const ys = [
    ...pts.map((p) => p.y),
    ...pts.filter((p) => p.y0 != null).map((p) => p.y0),
    ...cIn.map((c) => c.yield_pct),
    ...cCmpIn.map((c) => c.yield_pct),
  ];
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const pad = (hi - lo) * 0.08 || 0.2;
  return (
    <MeasuredSvg height={330} label="доходность ОФЗ и КБД по дюрации" cursor={onOpen ? "pointer" : "default"}>
      {({ W, H, bind }) => {
        const sx = linearScale([0, xmax], [SC_PAD.l, W - SC_PAD.r]);
        const sy = linearScale([lo - pad, hi + pad], [H - SC_PAD.b, SC_PAD.t]);
        const nx = Math.max(3, Math.round((W - SC_PAD.l - SC_PAD.r) / 70));
        const xt = termTicks(0, xmax, nx).map((xv) => ({ x: sx(xv), label: fmt.yrs(xv) }));
        return (
          <>
            <GridY ticks={linTicks(lo - pad, hi + pad, 4)} y={sy} x1={SC_PAD.l} x2={W - SC_PAD.r}
              lineClass="an-grid" textClass="an-axis" label={(v) => v.toFixed(1).replace(".", ",")} />
            <GridX ticks={xt} y1={SC_PAD.t} y2={H - SC_PAD.b} lineClass="an-grid an-grid-v" />
            <XTicks ticks={xt} y={H - SC_PAD.b + 14} textClass="an-axis" />
            {/* прошлая кривая — ПОД сегодняшней, чтобы актуальная читалась первой */}
            {cCmpIn.length > 1 && (
              <path d={linePath(cCmpIn, (c) => sx(c.years), (c) => sy(c.yield_pct))}
                className="ofz-kbd-cmp" fill="none" data-testid="ofz-kbd-cmp" />
            )}
            {cIn.length > 1 && (
              <path d={linePath(cIn, (c) => sx(c.years), (c) => sy(c.yield_pct))}
                className="ofz-kbd" fill="none" />
            )}
            {/* тени: где бумага стояла на дату сравнения, и коннектор к текущей
                точке — направление сдвига видно без чтения тултипа */}
            {pts.filter((p) => p.y0 != null).map((p) => {
              const x0 = p.x0 ?? p.x;
              return (
                <g key={"g" + p.isin} className="ofz-ghost-g">
                  <line x1={sx(x0)} y1={sy(p.y0)} x2={sx(p.x)} y2={sy(p.y)} className="ofz-conn" />
                  <circle cx={sx(x0)} cy={sy(p.y0)} r={3.2} className="ofz-pt-ghost"
                    {...bind(sx(x0), sy(p.y0), pointTip(p, cmpDate))} />
                </g>
              );
            })}
            {pts.map((p) => (
              <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={3.6}
                className={"ofz-pt" + (p.g == null ? "" : p.g >= 0 ? " cheap" : " rich")}
                onClick={onOpen ? (e) => onOpen(p.isin, e.currentTarget, "fixed") : undefined}
                {...bind(sx(p.x), sy(p.y), pointTip(p, cmpDate))} />
            ))}
            {labels && placeLabels(pts, sx, sy, W, SC_PAD.r, 9,
              (p, short) => `${short} ${p.g == null ? "" : fmt.devBps(p.g)}`).map((l) => (
              <text key={l.key} x={l.x} y={l.y} className="an-pt-lbl">{l.txt}</text>
            ))}
            <text x={SC_PAD.l} y={H - 4} className="an-axis-lbl" textAnchor="start">дюрация, лет →</text>
            <text x={SC_PAD.l - 40} y={SC_PAD.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${SC_PAD.l - 40} ${SC_PAD.t + 4})`}>доходность, %</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

// Тултип столбика: итог, раскладка по режимам и построчно борды.
function volTip(p, v, labels) {
  const line = (k, val) => `${k} ${fmt.mln(val) ?? "—"}`;
  let s = `${p.name}\nоборот ${fmt.mln(v.total) ?? "—"} млн ₽\n`
    + `${line("стакан", v.book)} · ${line("РПС", v.rps)} · ${line("прочее", v.other)}`;
  const boards = Object.entries(v.boards || {}).sort((a, b) => b[1] - a[1]);
  if (boards.length) {
    s += "\n" + boards.map(([code, val]) => {
      const lbl = labels?.[code] || BOARD_FALLBACK[code];
      return `${code}${lbl ? ` (${lbl})` : ""} ${fmt.mln(val) ?? "—"}`;
    }).join("\n");
  }
  return s;
}

// ── Полоса объёма под scatter'ом: столбик на бумагу, стек стакан/РПС/прочее ──
function VolumePanel({ pts, volumes, xmax }) {
  const items = volumes?.items || {};
  const bars = pts
    .map((p) => ({ p, v: items[p.isin] }))
    .filter((b) => b.v && b.v.total > 0);
  const vmax = Math.max(...bars.map((b) => b.v.total), 1);
  const labels = volumes?.board_labels;
  return (
    <MeasuredSvg height={VOL_H} label="оборот ОФЗ за день по дюрации">
      {({ W, H, bind }) => {
        const sx = linearScale([0, xmax], [SC_PAD.l, W - SC_PAD.r]);
        const base = H - VOL_PAD.b;
        const sh = (v) => ((v || 0) / vmax) * (base - VOL_PAD.t);
        // ширина столбика — от плотности точек по X, но в разумных рамках:
        // у соседних выпусков дюрации отличаются на десятые, столбики шире 7px
        // слились бы в одну полосу
        const bw = Math.max(3, Math.min(7, (W - SC_PAD.l - SC_PAD.r) / Math.max(pts.length, 1) * 0.5));
        return (
          <>
            <line x1={SC_PAD.l} x2={W - SC_PAD.r} y1={base + 0.5} y2={base + 0.5} className="an-grid" />
            <text x={SC_PAD.l - 4} y={VOL_PAD.t + 8} className="an-axis" textAnchor="end">
              {fmt.mln1(vmax)}
            </text>
            <text x={SC_PAD.l - 4} y={base} className="an-axis" textAnchor="end">0</text>
            {bars.map(({ p, v }) => {
              const segs = stackedBars([
                { value: v.book, cls: "ofz-vol-book" },
                { value: v.rps, cls: "ofz-vol-rps" },
                { value: v.other, cls: "ofz-vol-other" },
              ], base, sh);
              const x = sx(p.x) - bw / 2;
              const top = Math.min(...segs.map((s) => s.y), base);
              return (
                <g key={p.isin} className="ofz-vol" {...bind(sx(p.x), top, volTip(p, v, labels))}>
                  {/* невидимая зона захвата шире столбика — тонкую полоску
                      мышью не поймать */}
                  <rect x={x - 3} y={VOL_PAD.t} width={bw + 6} height={base - VOL_PAD.t} fill="transparent" />
                  {segs.map((s) => (
                    <rect key={s.cls} x={x} y={s.y} width={bw} height={s.h} className={s.cls} />
                  ))}
                </g>
              );
            })}
            {bars.length === 0 && (
              <text x={(SC_PAD.l + W - SC_PAD.r) / 2} y={(VOL_PAD.t + base) / 2 + 4}
                className="an-axis" textAnchor="middle">оборота за день нет</text>
            )}
            <text x={SC_PAD.l - 40} y={VOL_PAD.t + 2} className="an-axis-lbl"
              transform={`rotate(-90 ${SC_PAD.l - 40} ${VOL_PAD.t + 2})`}>млн ₽</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

/**
 * pts     — точки из OfzDesk: {isin, name, x (tau), y (ytm), g, curve, px, base,
 *           x0/y0 (опц.) — дюрация/YTM на дату сравнения}
 * curve   — points КБД сегодня (любой из двух форматов)
 * cmp     — null | {date, requested, curve: points, curveDate, curveRequested}
 *           date — фактическая дата as-of, curveDate — фактическая дата КБД
 * volumes — ответ /ofz/volumes или null
 */
export default function OfzChart({ pts: ptsIn, curve, cmp, volumes, labels, onOpen }) {
  if (ptsIn.length < 2) return <div className="an-empty">мало данных: метрики ОФЗ ещё прогреваются</div>;
  // тени живут ТОЛЬКО в режиме сравнения: выключенный режим при ещё не
  // обновлённых строках не должен оставлять на графике прошлые точки
  const pts = cmp ? ptsIn : ptsIn.map((p) => ({ ...p, x0: null, y0: null }));
  const cNow = normCurve(curve);
  const cCmp = cmp ? normCurve(cmp.curve) : [];
  // общий X обоих полотен — с запасом под тени, если бумага стала короче
  const xmax = Math.max(...pts.map((p) => Math.max(p.x, p.x0 ?? 0)), 1) * 1.04;
  return (
    <div className="ofz-chart">
      {cmp && (
        <CurveDeltaStrip now={curve} prev={cmp.curve}
          date={cmp.curveDate} requested={cmp.curveRequested} />
      )}
      <CurveScatter pts={pts} curve={cNow} curveCmp={cCmp} cmpDate={cmp?.date}
        xmax={xmax} labels={labels} onOpen={onOpen} />
      <VolumePanel pts={pts} volumes={volumes} xmax={xmax} />
    </div>
  );
}
