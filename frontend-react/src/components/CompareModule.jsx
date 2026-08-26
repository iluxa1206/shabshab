import { useEffect, useMemo, useState } from "react";
import { fmt, yearsTo } from "../format.js";
import { fetchSpreadMulti } from "../api.js";
import { OfferMarks, wapSpread } from "./BondTable.jsx";
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
const LINE_COLORS = ["#4f9cf9", "#f9a04f", "#3fbf7f", "#e05c66", "#b07cf9",
                     "#3fc6c6", "#d4b83f", "#f97cc0", "#8fbf3f", "#c07a4f"];
// Цвет — дефицитный ресурс: десять различимых оттенков есть, одиннадцатый уже
// путается с одним из первых. Всё сверх — СЕРЫМ ФОНОМ: линии видно (форма,
// уровень, разброс), но конкретную бумагу в них не ищут, для этого её выбирают
// в первую десятку.
const GREY = "var(--mut-2)";
const colorAt = (i) => (i < LINE_COLORS.length ? LINE_COLORS[i] : GREY);
// Потолок линий вообще: «выбрать все» на широком фильтре иначе шлёт на бэк
// сотни ISIN и рисует кашу. Двадцать — столько, сколько ещё читается глазом и
// не грузит ни запрос, ни отрисовку.
export const CMP_MAX = 20;
export const CMP_COLORS = LINE_COLORS.length;

const PERIODS = [["1м", 30], ["3м", 91], ["6м", 182], ["12м", 365]];
// СПРЕД — первичная метрика платформы (Y-IDX). Цена — вспомогательная, в двух
// видах: абсолютная (% номинала) и нормированная к началу окна (Δ% от первой
// точки) — только она позволяет сравнивать бумаги с разной ценой в одной шкале.
const METRICS = [["spread", "Спред"], ["price", "Цена %"], ["chg", "Цена Δ%"]];
// База дня. Средневзвес/закрытие — готовая свёртка часовых баров (bar_daily,
// считается ночным проходом и берётся как есть). As-of — вечерний снапшот
// spread_daily: глубже история, но цена дня там торговая последняя, не средняя.
const BASES = [["vwap", "Ср.взвес"], ["bar_close", "Закрытие"], ["close", "As-of"]];

const PAD = { l: 50, r: 14, t: 12, b: 30 };
const trunc = (s, n = 16) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");

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

const axisLabel = (m) => (m === "spread" ? "R-spread, bps"
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
        // В тултипе только ЦВЕТНЫЕ линии: сто строк серого фона перекрыли бы
        // весь график, а найти в них бумагу глазом всё равно нельзя.
        const at = lines
          .map((l, i) => ({ l, i, pt: l.pts.find((q) => q.date === p.date) }))
          .filter((x) => x.pt);
        const named = at.filter((x) => x.i < CMP_COLORS).sort((a, b) => b.pt.v - a.pt.v);
        const rest = at.length - named.length;
        return (
          <>
            <div className="an-tt-h">{fmt.date(p.date)}</div>
            {named.map(({ l, i, pt }) => (
              <div key={l.isin} style={{ color: colorAt(i) }}>
                {trunc(names[l.isin] || l.isin, 12)} {fmtVal(pt.v, metric)}
              </div>
            ))}
            {rest > 0 && <div className="an-tt-n">ещё {rest} серым</div>}
          </>
        );
      }}
      overlay={(s, g) => (
        <text x={g.x0 - 40} y={g.y1 + 4} className="an-axis-lbl"
          transform={`rotate(-90 ${g.x0 - 40} ${g.y1 + 4})`}>{axisLabel(metric)}</text>
      )}
    >
      {(s) => lines.map((l, i) => {
        const c = colorAt(i);
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
  const grey = Math.max(0, series.length - CMP_COLORS);
  return (
    <div className="an-legend">
      {series.slice(0, CMP_COLORS).map((ser, i) => {
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
            <span className="an-leg-swatch" style={{ background: colorAt(i) }} />
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
      {grey > 0 && (
        <span className="an-leg-item" title={`сверх ${CMP_COLORS} линий цвет не назначается — фон`}>
          <span className="an-leg-swatch" style={{ background: GREY }} />+{grey} серым
        </span>
      )}
    </div>
  );
}

// ── ISIN одним кликом в буфер: на СРАВНЕНИИ выпуски кидают в чат/терминал
//    чаще, чем открывают карточку, а выделять мышью моноширинную строку в
//    скроллящейся таблице неудобно ──
function IsinCell({ isin }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(isin); } catch { return; }
    setDone(true);
    setTimeout(() => setDone(false), 900);
  };
  return (
    <button type="button" className={"cmp-isin" + (done ? " ok" : "")} onClick={copy}
      title={done ? "скопировано" : `${isin} — клик: копировать`}>{done ? "скопировано" : isin}</button>
  );
}

// ── Витрина выбора: те же строки, что в МОНИТОРЕ, плюс чекбокс ──
// Колонки короткие: слева от графика места мало, и всё, что не помогает
// выбрать линию (оборот, рейтинг, дюрация), живёт в МОНИТОРЕ.
function PickTable({ rows, sel, onToggle, onSetAll, onClear, onOpen, hi, onHi }) {
  if (!rows.length) return <div className="an-empty">под фильтры не попала ни одна бумага</div>;
  const full = sel.length >= CMP_MAX;
  // «все» = все строки под текущими фильтрами уже на графике (с учётом потолка
  // линий: при широком фильтре «все» — это первые CMP_MAX строк).
  const capped = rows.slice(0, CMP_MAX).map((b) => b.isin);
  const allOn = capped.length > 0 && capped.every((i) => sel.includes(i));
  return (
    <div className="cmp-pickwrap">
      <div className="cmp-bar">
        <button type="button" className="cmp-all" onClick={() => onSetAll(allOn ? [] : capped)}>
          {allOn ? "снять все" : "выбрать все"}
        </button>
        {/* сброс рядом с выбором: снять одну галку — дело чекбокса, а снять ВСЁ
            иначе значит искать по списку, какие строки отмечены */}
        <button type="button" className="cmp-all" onClick={onClear} disabled={!sel.length}
          title="снять все отметки, включая бумаги вне текущего фильтра">сброс</button>
        <span className="cmp-mut">
          {sel.length} из {rows.length}
          {!allOn && rows.length > CMP_MAX && ` · «все» возьмёт первые ${CMP_MAX}`}
        </span>
      </div>
      <div className="cmp-pick">
      <table className="cmp-tbl">
        <thead>
          <tr>
            <th className="cmp-cb" />
            <th>выпуск</th><th>isin</th><th>эмитент</th>
            <th className="num">погашение <span className="cmp-mut">(лет)</span></th>
            <th className="num">цена ср</th><th className="num">R-spread ср</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((b) => {
            const i = sel.indexOf(b.isin);
            const on = i >= 0;
            const hz = b.preferred_horizon;
            // выбор горизонта есть только когда есть оферта/колл — иначе синяя
            // подсветка лет стояла бы у каждой строки и ничего не сообщала
            const hasChoice = !!b.offer_date || b.has_call === true;
            const wap = wapSpread(b);
            return (
              <tr key={b.isin} className={(on ? "on" : "") + (hi === b.isin ? " hi" : "")}
                onMouseEnter={() => on && onHi(b.isin)} onMouseLeave={() => on && onHi(null)}>
                <td className="cmp-cb">
                  <input type="checkbox" checked={on} disabled={!on && full}
                    onChange={() => onToggle(b.isin)}
                    aria-label={`сравнить ${b.short_name || b.isin}`}
                    title={!on && full ? `на графике максимум ${CMP_MAX} линий` : undefined}
                    style={on ? { accentColor: colorAt(i) } : undefined} />
                </td>
                <td>
                  <button type="button" className="cmp-name" onClick={(e) => onOpen(b.isin, e.currentTarget)}
                    title="карточка выпуска">{b.short_name || b.isin}</button>
                </td>
                <td><IsinCell isin={b.isin} /></td>
                <td className="cmp-mut">{trunc(b.emitter_name, 20)}</td>
                {/* Дата погашения, под ней — оферта с маркерами p/c. СИНИЕ годы
                    стоят у той даты, к которой посчитаны метрики строки
                    (горизонт прайсинга) — та же конвенция, что в МОНИТОРЕ. */}
                <td className="num cmp-mat">
                  <div>
                    {!b.offer_date && <OfferMarks b={b} />}
                    {fmt.date(b.maturity_date) ?? "—"}
                    {yearsTo(b.maturity_date) != null && (
                      <span className={"mat-yrs" + (hasChoice && hz === "maturity" ? " mat-hz" : "")}>
                        {" (" + yearsTo(b.maturity_date) + ")"}</span>
                    )}
                  </div>
                  {b.offer_date && (
                    <div className="mat-offer"
                      title={(b.offer_kind === "call" ? "call-оферта " : "пут-оферта ") + fmt.date(b.offer_date)}>
                      <OfferMarks b={b} />{fmt.date(b.offer_date)}
                      {yearsTo(b.offer_date) != null && (
                        <span className={"mat-yrs" + (hz === "put" || hz === "call" ? " mat-hz" : "")}>
                          {" (" + yearsTo(b.offer_date) + ")"}</span>
                      )}
                    </div>
                  )}
                </td>
                <td className="num" title="средневзвешенная цена дня (WAP биржи)">
                  {fmt.pct(b.wap_price_pct) ?? "—"}</td>
                <td className="num" title="R-spread по средневзвешенной цене (линеаризация от торгового якоря)">
                  {fmt.bps(wap) ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      </div>
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
export default function CompareModule({ rows, sel, onToggle, onSetAll, onClear, onOpen }) {
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


  return (
    <section className="cmp">
      {/* Витрина слева, график справа: отбор бумаги и линия видны одновременно,
          чекбокс не уезжает под сгиб при листании списка. */}
      <PickTable rows={rows} sel={sel} onToggle={onToggle} onSetAll={onSetAll}
        onClear={onClear} onOpen={onOpen} hi={hi} onHi={setHi} />

      <div className="an-card">
        <div className="an-title an-head">
          <span className="an-title-txt">
            СРАВНЕНИЕ · {sel.length} лин.
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
          {metric === "spread" ? "R-spread по цене дня — линия на выпуск"
            : metric === "price" ? "чистая цена, % номинала"
            : "изменение цены от первого дня окна, %"}
          {base === "vwap" ? " · средневзвешенная цена дня (архив часовых баров)"
            : base === "bar_close" ? " · цена закрытия дня (архив часовых баров)"
            : " · вечерний снапшот as-of (глубокая история)"}
        </div>

        {!sel.length ? (
          <div className="an-empty">
            отметьте выпуски слева — цветом идут первые {CMP_COLORS}, дальше серым фоном
          </div>
        ) : err ? (
          <div className="an-empty">не загрузилось: {err}</div>
        ) : !data ? (
          <div className="an-empty">загрузка…</div>
        ) : (
          <>
            <CompareChart series={series} names={names} metric={metric} height={420}
              hi={hi} onHi={setHi} />
            <CompareLegend series={series} names={names} metric={metric}
              hi={hi} onHi={setHi} onDrop={onToggle} />
          </>
        )}
      </div>
    </section>
  );
}
