import { fmt } from "../../format.js";
import {
  linearScale, linTicks, linePath, GridY, GridX, XTicks, MeasuredSvg,
  termTicks, placeLabels, stackedBars,
} from "../../charts/index.js";

// ГРАФИК витрины ОФЗ: scatter «доходность × дюрация» поверх СВОЕЙ кривой ОФЗ
// (Нельсон–Сигел–Свенссон, подогнана на бэке по этим же точкам —
// /api/fixed/ofz/curve), под точками столбики объёма, поверх — сдвиг к
// прошлой дате (кривая as-of + «тени» точек).
//
// Компонент ЧИСТО рисующий: линия — samples ручки, отклонение точки —
// готовый остаток по ISIN (resid, бп), объёмы и as-of тянет и мёржит OfzDesk.
// Здесь ни одной подгонки/интерполяции нет — только разность уже посчитанных
// чисел (ΔYTM в бп, Δ кривой по совпадающим ключевым тенорам).
// Оба SVG меряют один и тот же контейнер (MeasuredSvg), поэтому X-шкалы
// scatter'а и полосы объёма совпадают без общего состояния.

const BASE_LABEL = {
  wap: "СР.ВЗВЕС", last: "ПОСЛЕДНЯЯ", bid: "BID", ask: "OFFER",
};

// Поля одинаковые у scatter'а и полосы объёма — иначе столбик встал бы не под
// своей точкой.
const SC_PAD = { l: 46, r: 48, t: 14, b: 32 };
// Объём — на том же полотне, что и точки: столбик от нижней оси под своей
// бумагой, не выше VOL_SHARE высоты поля, шкала справа. Отдельная полоса под
// графиком читалась хуже: столбики были в 3 px и не соотносились с точками.
const VOL_SHARE = 0.42;

// Подписи бордов для тултипа объёма. Бэк может прислать свой словарь
// (board_labels в ответе) — он приоритетнее; этот — запас на случай, когда его
// нет, чтобы в плашке не висел голый код.
const BOARD_FALLBACK = {
  TQOB: "стакан T+1", TQOY: "стакан, юани", TQOD: "стакан, доллары", TQOE: "стакан, евро",
  PSOB: "РПС", PTOB: "РПС T+", PSEU: "РПС, евро", PSOY: "РПС, юани",
};

// Линия кривой: samples ручки — объекты {years, yield_pct}; пары [tau, yield]
// тоже принимаем — фронт не должен падать от формата, который выбрал бэк.
export function normCurve(points) {
  return (points || []).map((p) => (
    Array.isArray(p) ? { years: +p[0], yield_pct: +p[1] }
      : { years: +p.years, yield_pct: +p.yield_pct }
  )).filter((p) => Number.isFinite(p.years) && Number.isFinite(p.yield_pct));
}

// Δ кривой по ключевым тенорам: разность key_tenors двух ответов /ofz/curve
// по СОВПАДАЮЩИМ тенорам. Тенор с extrap (вне domain хотя бы одной из кривых)
// не показываем: хвост NSS за последней бумагой — экстраполяция, не рынок.
// Интерполяции нет (правило страницы: кривая на фронте не считается).
export function curveDelta(now, prev) {
  const ok = (arr) => (arr || []).filter((k) => !k.extrap
    && Number.isFinite(+k.years) && Number.isFinite(+k.yield_pct));
  const a = ok(now), b = ok(prev);
  return a.map((x) => {
    const y = b.find((p) => Math.abs(+p.years - +x.years) < 1e-6);
    return y ? { tenor: +x.years, bps: (+x.yield_pct - +y.yield_pct) * 100 } : null;
  }).filter(Boolean);
}

const tenorLbl = (t) => (t < 1 ? `${Math.round(t * 12)}M` : `${t}Y`);

// Строка «Δ кривая ОФЗ к 11.09.2026: 1Y +8 · 3Y +5 · 10Y −2 бп» над графиком.
export function CurveDeltaStrip({ now, prev, date, requested }) {
  const d = curveDelta(now, prev);
  return (
    <div className="ofz-dstrip" data-testid="ofz-dstrip">
      <span className="ofz-dstrip-k">Δ кривая ОФЗ{date ? ` к ${fmt.date(date)}` : ""}</span>
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

// Тултип точки: YTM · кривая · откл. к своей кривой, плюс стакан и ΔYTM,
// когда включены чипы.
function pointTip(p, cmpDate, bidAsk) {
  const dev = p.resid == null ? "—" : `${fmt.devBps(p.resid)} бп`;
  let s = `${p.name}\nYTM ${fmt.pct(p.y)} · кривая ${fmt.pct(p.curve) ?? "—"} · откл. ${dev}`
    + (p.used === false ? " · вне подгонки" : "")
    + `\nдюрация ${fmt.yrs(p.x)} · цена ${fmt.pct(p.px) ?? "—"} (${BASE_LABEL[p.base] ?? p.base})`;
  if (bidAsk && (p.yb != null || p.ya != null)) {
    // ширина по доходности: бид (продать) даёт YTM выше, оффер (купить) — ниже
    const w = p.yb != null && p.ya != null ? ` · ширина ${fmt.devBps((p.yb - p.ya) * 100)} бп` : "";
    s += `\nбид ${fmt.pct(p.pb) ?? "—"} → YTM ${fmt.pct(p.yb) ?? "—"}`
      + ` · оффер ${fmt.pct(p.pa) ?? "—"} → YTM ${fmt.pct(p.ya) ?? "—"}${w}`;
  }
  if (p.y0 != null) {
    const d = (p.y - p.y0) * 100;
    s += `\nΔYTM ${fmt.devBps(d)} бп${cmpDate ? ` с ${fmt.date(cmpDate)}` : ""}`
      + ` (было ${fmt.pct(p.y0)}${p.x0 != null ? ` · дюрация ${fmt.yrs(p.x0)}` : ""})`;
  }
  return s;
}

// Цвет точки — по знаку отклонения к своей кривой: выше кривой — дешевле
// соседей, ниже — дороже. Точка вне подгонки — полый кружок того же цвета.
const ptClass = (p) => "ofz-pt" + (p.used === false ? " ofz-pt-out" : "")
  + (p.resid == null ? "" : p.resid >= 0 ? " cheap" : " rich");
// inline, а не класс: .ofz-pt красит fill через CSS, и презентационный
// атрибут fill="none" проиграл бы ему — полый кружок только стилем
const ptStyle = (p) => (p.used === false
  ? { fill: "none", strokeWidth: 1.2,
      stroke: p.resid == null ? "var(--mut)" : p.resid >= 0 ? "var(--up)" : "var(--down)" }
  : undefined);

// ── Scatter: YTM × дюрация поверх своей кривой (+ кривая as-of и тени точек) ──
function CurveScatter({ pts, curve, curveCmp, cmpDate, xmax, labels, volumes, bidAsk, onOpen }) {
  const cIn = curve.filter((c) => c.years <= xmax);
  const cCmpIn = (curveCmp || []).filter((c) => c.years <= xmax);
  // Домен Y — по бумагам, теням И обеим кривым в этом же окне сроков: кривая,
  // ушедшая за край, читалась бы как «все бумаги дорогие».
  const ys = [
    ...pts.map((p) => p.y),
    ...pts.filter((p) => p.y0 != null).map((p) => p.y0),
    ...(bidAsk ? pts.flatMap((p) => [p.yb, p.ya]).filter((v) => v != null) : []),
    ...cIn.map((c) => c.yield_pct),
    ...cCmpIn.map((c) => c.yield_pct),
  ];
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const pad = (hi - lo) * 0.08 || 0.2;
  return (
    <MeasuredSvg height={360} label="доходность ОФЗ, своя кривая и оборот по дюрации" cursor={onOpen ? "pointer" : "default"}>
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
            <VolumeBars pts={pts} volumes={volumes} sx={sx} W={W} H={H} bind={bind} />
            {/* прошлая кривая — ПОД сегодняшней, чтобы актуальная читалась первой */}
            {cCmpIn.length > 1 && (
              <path d={linePath(cCmpIn, (c) => sx(c.years), (c) => sy(c.yield_pct))}
                className="ofz-kbd-cmp" fill="none" data-testid="ofz-kbd-cmp" />
            )}
            {cIn.length > 1 && (
              <path d={linePath(cIn, (c) => sx(c.years), (c) => sy(c.yield_pct))}
                className="ofz-kbd" fill="none" data-testid="ofz-curve" />
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
            {/* полоска бид/оффер: от YTM по биду (выше) до YTM по офферу
                (ниже) с засечками на концах; точка средневзвеса — поверх */}
            {bidAsk && pts.filter((p) => p.yb != null || p.ya != null).map((p) => {
              const x = sx(p.x), y1 = sy(p.yb ?? p.y), y2 = sy(p.ya ?? p.y);
              return (
                <g key={"ba" + p.isin} className="ofz-ba" data-testid="ofz-ba">
                  <line x1={x} x2={x} y1={y1} y2={y2} className="ofz-ba-line" />
                  {p.yb != null && <line x1={x - 3} x2={x + 3} y1={y1} y2={y1} className="ofz-ba-cap ofz-ba-bid" />}
                  {p.ya != null && <line x1={x - 3} x2={x + 3} y1={y2} y2={y2} className="ofz-ba-cap ofz-ba-ask" />}
                </g>
              );
            })}
            {pts.map((p) => (
              <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={3.6}
                className={ptClass(p)} style={ptStyle(p)}
                onClick={onOpen ? (e) => onOpen(p.isin, e.currentTarget, "fixed") : undefined}
                {...bind(sx(p.x), sy(p.y), pointTip(p, cmpDate, bidAsk))} />
            ))}
            {labels && placeLabels(pts, sx, sy, W, SC_PAD.r, 9,
              (p, short) => `${short} ${p.resid == null ? "" : fmt.devBps(p.resid)}`).map((l) => (
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

// Столбики объёма ВНУТРИ scatter'а: стек стакан/РПС/прочее от нижней оси,
// шкала справа. Рисуются ПОД точками и кривой (первыми в SVG).
function VolumeBars({ pts, volumes, sx, W, H, bind }) {
  const items = volumes?.items || {};
  const bars = pts
    .map((p) => ({ p, v: items[p.isin] }))
    .filter((b) => b.v && b.v.total > 0);
  const base = H - SC_PAD.b;
  const top = SC_PAD.t + (base - SC_PAD.t) * (1 - VOL_SHARE);
  const vmax = Math.max(...bars.map((b) => b.v.total), 1);
  const sh = (v) => ((v || 0) / vmax) * (base - top);
  const labels = volumes?.board_labels;
  // ширина — от плотности точек, но 6…14 px: у соседних выпусков дюрации
  // отличаются на десятые, столбики шире слились бы в полосу
  const bw = Math.max(6, Math.min(14, (W - SC_PAD.l - SC_PAD.r) / Math.max(pts.length, 1) * 0.6));
  const ticks = linTicks(0, vmax, 3).filter((t) => t > 0 && t <= vmax);
  return (
    <>
      {bars.map(({ p, v }) => {
        const segs = stackedBars([
          { value: v.book, cls: "ofz-vol-book" },
          { value: v.rps, cls: "ofz-vol-rps" },
          { value: v.other, cls: "ofz-vol-other" },
        ], base, sh);
        const x = sx(p.x) - bw / 2;
        const yTop = Math.min(...segs.map((sg) => sg.y), base);
        return (
          <g key={"v" + p.isin} className="ofz-vol" {...bind(sx(p.x), yTop, volTip(p, v, labels))}>
            {segs.map((sg) => (
              <rect key={sg.cls} x={x} y={sg.y} width={bw} height={sg.h} className={sg.cls} />
            ))}
          </g>
        );
      })}
      {/* правая шкала объёма: тики только в занятой столбиками части поля */}
      {bars.length > 0 && ticks.map((t) => (
        <g key={"vt" + t}>
          <line x1={W - SC_PAD.r} x2={W - SC_PAD.r + 4} y1={base - sh(t)} y2={base - sh(t)} className="an-grid" />
          <text x={W - SC_PAD.r + 6} y={base - sh(t) + 3.5} className="an-axis ofz-vol-axis" textAnchor="start">
            {fmt.mln1(t)}
          </text>
        </g>
      ))}
      {bars.length > 0 && (
        <text x={W - 4} y={SC_PAD.t + 4} className="an-axis-lbl ofz-vol-axis" textAnchor="end"
          transform={`rotate(90 ${W - 4} ${SC_PAD.t + 4})`}>оборот, млн ₽</text>
      )}
    </>
  );
}

/**
 * pts       — точки из OfzDesk: {isin, name, x (tau), y (ytm), curve (y кривой
 *             под точкой), resid (откл. к своей кривой, бп), used (вошла ли
 *             в подгонку), px, base, x0/y0 (опц.) — дюрация/YTM на дату сравнения}
 * curve     — samples своей кривой сегодня ({years, yield_pct})
 * keyTenors — key_tenors сегодняшнего ответа (для стрипа Δ)
 * cmp       — null | {date, requested, curve: samples as-of, keyTenors,
 *             curveDate, curveRequested}; date — фактическая дата as-of точек,
 *             curveDate — фактическая дата кривой as-of
 * volumes   — ответ /ofz/volumes или null
 */
export default function OfzChart({ pts: ptsIn, curve, keyTenors, cmp, volumes, labels, bidAsk, onOpen }) {
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
        <CurveDeltaStrip now={keyTenors} prev={cmp.keyTenors}
          date={cmp.curveDate} requested={cmp.curveRequested} />
      )}
      <CurveScatter pts={pts} curve={cNow} curveCmp={cCmp} cmpDate={cmp?.date}
        xmax={xmax} labels={labels} volumes={volumes} bidAsk={bidAsk} onOpen={onOpen} />
    </div>
  );
}
