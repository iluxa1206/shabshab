import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchFixed, fetchGCurve } from "../../api.js";
import { fmt, dmColor, stripOfz } from "../../format.js";
import {
  linearScale, linTicks, linePath, GridY, GridX, XTicks, MeasuredSvg,
  termTicks, placeLabels,
} from "../../charts/index.js";

// ВИТРИНА ОФЗ — суверенная кривая крупным планом: те же строки, что в мониторе
// фиксов (cls=ofz), но в координатах «доходность × дюрация» поверх КБД МосБиржи.
// Смысл страницы — не список, а ОТКЛОНЕНИЕ бумаги от кривой: у ОФЗ нет ни
// кредитного, ни рейтингового разреза, весь сигнал в том, где выпуск сидит
// относительно КБД (выше кривой — дешевле рынка, ниже — дороже).
//
// Ни одного числа здесь не считается: YTM, дюрация и g-спред приезжают из
// движка метрик (services/fixed_income), КБД — тем же объектом, по которому
// движок и считает спред (/api/curves/gcurve). Своя интерполяция кривой в
// браузере разъехалась бы с колонкой G-SPRD в мониторе.

// База цены расчёта. Держим ЯВНО рядом с числами: YTM по последней сделке в
// неликвиде — случайный принт, по средневзвесу — взвешенная оборотом цена дня,
// по стороне стакана — то, по чему реально можно торгнуть.
const BASES = [
  ["wap", "СР.ВЗВЕС", "средневзвешенная цена дня — база по умолчанию: взвешена оборотом"],
  ["last", "ПОСЛЕДНЯЯ", "цена последней сделки (в неликвиде — один случайный принт)"],
  ["bid", "BID", "лучшая заявка на покупку: доходность продажи в бид"],
  ["ask", "OFFER", "лучшая заявка на продажу: доходность покупки с оффера"],
];
const BASE_LABEL = Object.fromEntries(BASES.map(([id, label]) => [id, label]));

// Тройка «цена → доходность → спред» ВСЕГДА из одного расчёта движка: смешивать
// YTM по средневзвесу со спредом по последней нельзя — это разные цены.
const byBase = (b, base) => (
  base === "bid" ? { px: b.bid, ytm: b.ytm_bid, g: b.g_spread_bid_bps }
    : base === "ask" ? { px: b.ask, ytm: b.ytm_ask, g: b.g_spread_ask_bps }
      : base === "last" ? { px: b.last_price_pct, ytm: b.ytm, g: b.g_spread_bps }
        : { px: b.wap_pct, ytm: b.ytm_wap, g: b.g_spread_wap_bps }
);

// Дюрация Маколея — тот тенор, по которому движок снимает КБД (см.
// fixed_income.fixed_metrics: «тенор КБД матчим по Маколею, как НРД»). По ней
// же кладём точку на график, иначе бумага стояла бы не над своей точкой кривой.
const tau = (b) => b.mac_dur ?? b.mod_dur ?? null;

// КБД под бумагой — не второй интерполяцией, а из тождества движка:
// g = (ytm − КБД(τ))·10000. Так число на странице не может разойтись со спредом.
const curveAt = (ytm, g) => (ytm == null || g == null ? null : ytm - g / 100.0);

const COLS = [
  { key: "name", label: "Выпуск", cls: "left" },
  { key: "maturity_date", label: "Погашение", cls: "left" },
  { key: "tau", label: "Дюрация", sub: "Маколея, лет", cls: "num" },
  { key: "coupon_pct", label: "Купон", sub: "%", cls: "num" },
  { key: "px", label: "Цена", sub: "% номинала", cls: "num" },
  { key: "ytm", label: "YTM", sub: "% годовых", cls: "num" },
  { key: "curve", label: "КБД", sub: "% на τ", cls: "num" },
  { key: "g", label: "Отклонение", sub: "б.п. к КБД", cls: "num" },
  { key: "val_today", label: "Оборот", sub: "сегодня, млн ₽", cls: "num" },
  { key: "adv_1m_rub", label: "ADV", sub: "1М, млн ₽", cls: "num" },
];

const SC_PAD = { l: 46, r: 14, t: 14, b: 32 };

// ── Scatter: YTM × дюрация поверх КБД ──
function CurveScatter({ pts, curve, labels, onOpen }) {
  if (pts.length < 2) return <div className="an-empty">мало данных: метрики ОФЗ ещё прогреваются</div>;
  const xmax = Math.max(...pts.map((p) => p.x), 1) * 1.04;
  // Домен Y — по бумагам И по кривой в этом же окне сроков: кривая, ушедшая за
  // край, читалась бы как «все бумаги дорогие».
  const cIn = curve.filter((c) => c.years <= xmax);
  const ys = [...pts.map((p) => p.y), ...cIn.map((c) => c.yield_pct)];
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
            {cIn.length > 1 && (
              <path d={linePath(cIn, (c) => sx(c.years), (c) => sy(c.yield_pct))}
                className="ofz-kbd" fill="none" />
            )}
            {pts.map((p) => (
              <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={3.6}
                className={"ofz-pt" + (p.g == null ? "" : p.g >= 0 ? " cheap" : " rich")}
                onClick={onOpen ? (e) => onOpen(p.isin, e.currentTarget, "fixed") : undefined}
                {...bind(sx(p.x), sy(p.y),
                  `${p.name}\nYTM ${fmt.pct(p.y)} · КБД ${fmt.pct(p.curve)}\n`
                  + `отклонение ${fmt.devBps(p.g)} б.п. · дюрация ${fmt.yrs(p.x)}\n`
                  + `цена ${fmt.pct(p.px) ?? "—"} (${BASE_LABEL[p.base]})`)} />
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

export default function OfzDesk({ onOpen }) {
  const [base, setBase] = useState(() => localStorage.getItem("ofzBase") || "wap");
  const [labels, setLabels] = useState(() => localStorage.getItem("ofzLabels") !== "0");
  const [sort, setSort] = useState({ key: "tau", dir: "asc" });

  // Список фиксов — тот же кэш, что у монитора (ключ совпадает с react-query
  // ключом FixedMonitor нарочно: переход между вкладками не перезапрашивает).
  const listQ = useQuery({
    queryKey: ["fixed", 0, 0],
    queryFn: () => fetchFixed({}),
    refetchInterval: 30000,
    staleTime: 15000,
  });
  // КБД публикуется раз в день, но при сбое фетча сервис отдаёт вчерашнюю —
  // перепрашиваем раз в 10 минут, чтобы страница подхватила свежую сама.
  const curveQ = useQuery({
    queryKey: ["gcurve"],
    queryFn: fetchGCurve,
    refetchInterval: 600000,
    staleTime: 300000,
  });

  const rows = useMemo(() => {
    const out = (listQ.data?.items || [])
      .filter((b) => b.cls === "ofz")
      .map((b) => {
        const v = byBase(b, base);
        return {
          ...b, ...v, base,
          name: stripOfz(b.name) || b.isin,
          tau: tau(b),
          curve: curveAt(v.ytm, v.g),
        };
      });
    const { key, dir } = sort;
    const k = dir === "asc" ? 1 : -1;
    return out.sort((a, b) => {
      const x = a[key], y = b[key];
      if (x == null && y == null) return 0;
      if (x == null) return 1;          // прочерки всегда внизу
      if (y == null) return -1;
      if (typeof x === "string") return k * x.localeCompare(y);
      return k * (x - y);
    });
  }, [listQ.data, base, sort]);

  const pts = useMemo(() => rows
    .filter((b) => b.tau != null && b.tau > 0 && b.ytm != null)
    .map((b) => ({ x: b.tau, y: b.ytm, g: b.g, curve: b.curve, px: b.px,
                   isin: b.isin, name: b.name, base: b.base })), [rows]);

  const setBaseSaved = (id) => { setBase(id); localStorage.setItem("ofzBase", id); };
  const toggleLabels = () => setLabels((v) => {
    localStorage.setItem("ofzLabels", v ? "0" : "1");
    return !v;
  });
  const onSort = (key) => setSort((s) => (
    s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
      : { key, dir: key === "name" || key === "maturity_date" ? "asc" : "desc" }
  ));

  const cheapest = pts.reduce((a, p) => (p.g != null && (a == null || p.g > a.g) ? p : a), null);
  const richest = pts.reduce((a, p) => (p.g != null && (a == null || p.g < a.g) ? p : a), null);
  const curveDate = curveQ.data?.curve_date;

  return (
    <div className="issuer-agg ofz-desk">
      <div className="ia-head">
        <h2 className="ia-title">ОФЗ</h2>
        <div className="ia-filters">
          <span className="seg" role="tablist" aria-label="База цены расчёта">
            {BASES.map(([id, label, title]) => (
              <button key={id} className={"seg-btn" + (base === id ? " active" : "")}
                onClick={() => setBaseSaved(id)} title={title}>{label}</button>
            ))}
          </span>
          <button className={"chip-btn" + (labels ? " on" : "")} onClick={toggleLabels}
            title="подписи выпусков на графике (имя и отклонение от КБД)">Подписи</button>
        </div>
      </div>

      <div className="ia-head">
        <span className="ia-hint">
          доходность к погашению против КБД МосБиржи; точка выше кривой — выпуск ДЕШЕВЛЕ
          суверенной кривой, ниже — дороже. Отклонение — g-спред движка (тенор КБД по
          дюрации Маколея, как у НРД), цена расчёта — {BASE_LABEL[base].toLowerCase()}
          {" · "}{pts.length} из {rows.length} выпусков с метриками
          {curveDate && <> · КБД от {fmt.date(curveDate)}</>}
          {curveQ.data?.stale && <span className="ofz-stale"> кривая не сегодняшняя</span>}
          {cheapest && richest && (
            <> · дешевле всех {cheapest.name} ({fmt.devBps(cheapest.g)} б.п.),
              дороже всех {richest.name} ({fmt.devBps(richest.g)} б.п.)</>
          )}
        </span>
      </div>

      {listQ.isPending && <div className="ia-hint">Загрузка…</div>}
      {listQ.error && <div className="ia-hint">Не удалось загрузить список фиксов</div>}
      {curveQ.error && <div className="ia-hint">КБД недоступна — кривая на графике не построена</div>}

      {!listQ.isPending && !listQ.error && (
        <>
          <CurveScatter pts={pts} curve={curveQ.data?.points || []} labels={labels} onOpen={onOpen} />

          <table className="grid packed ofz-tbl">
            <thead>
              <tr>
                {/* стрелку сортировки рисует CSS (.grid thead th.sorted/.asc) —
                    как в остальных таблицах витрины */}
                {COLS.map((c) => (
                  <th key={c.key} onClick={() => onSort(c.key)} title={c.sub || undefined}
                    className={c.cls + (sort.key === c.key ? " sorted" + (sort.dir === "asc" ? " asc" : "") : "")}>
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={b.isin} onClick={onOpen ? (e) => onOpen(b.isin, e.currentTarget, "fixed") : undefined}>
                  <td className="left">{b.name}</td>
                  <td className="left">{fmt.date(b.maturity_date) || "—"}</td>
                  <td className="num">{b.tau == null ? "—" : fmt.num(b.tau, 2)}</td>
                  <td className="num">{fmt.pct(b.coupon_pct) ?? "—"}</td>
                  <td className="num">{fmt.pct(b.px) ?? "—"}</td>
                  <td className="num">{fmt.pct(b.ytm) ?? "—"}</td>
                  <td className="num mut">{fmt.pct(b.curve) ?? "—"}</td>
                  <td className="num" style={dmColor(b.g)}>{fmt.devBps(b.g) ?? "—"}</td>
                  <td className="num">{fmt.mln(b.val_today) ?? "—"}</td>
                  <td className="num">{fmt.mln1(b.adv_1m_rub) ?? "—"}</td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr><td colSpan={COLS.length} className="left mut">ОФЗ в витрине фиксов не найдены</td></tr>
              )}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
