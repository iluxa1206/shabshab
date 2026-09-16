import { fmt } from "../../format.js";
import {
  linearScale, niceTicks, linePath, GridY, GridX, XTicks, MeasuredSvg, stackedBars,
  timeScale, dateTickIdx, tickLabel, spanDays,
} from "../../charts/index.js";

// ГРАФИКИ вкладки АУКЦИОН. Чисто рисующие компоненты: все числа (113-я,
// кумулятив, прямая плана, корзины) считает бэк (services/ofz_auctions), здесь
// только пиксели. Свой SVG на примитивах charts/ — как OfzChart; размеры по
// контейнеру (MeasuredSvg), цвета — CSS-классами под обе темы.

const PAD = { l: 50, r: 14, t: 14, b: 28 };

// Тип бумаги → класс столбика. ПД — основной объём, ПК/ИН — редкие вкрапления,
// и цветом должны отличаться от ПД, а не друг от друга.
const TYPE_CLS = { "ОФЗ-ПД": "au-t-pd", "ОФЗ-ПК": "au-t-pk", "ОФЗ-ИН": "au-t-in" };
const TYPE_ORDER = ["ОФЗ-ПД", "ОФЗ-ПК", "ОФЗ-ИН"];

// Корзина срока для цвета точки доходности: короткие/средние/длинные — те же
// границы, что у бэка в stats (STD_BUCKETS).
export const termBucket = (t) => (t == null ? null : t <= 5 ? "short" : t <= 10 ? "mid" : "long");
const BUCKET_LBL = { short: "до 5 лет", mid: "5–10 лет", long: "от 10 лет" };

const bln = (v, d = 1) => (v == null ? "—" : v.toLocaleString("ru-RU",
  { minimumFractionDigits: d, maximumFractionDigits: d }));
const dd = (d) => (d ? d.slice(8, 10) + "." + d.slice(5, 7) : "");

// ── ПЛАН/ФАКТ: кумулятив 113-й по датам квартала против прямой плана ──
// X — даты графика квартала (категориально, равным шагом: аукционы идут раз в
// неделю, и календарная шкала ничего бы не добавила, а дырки от пропущенных
// сред читались бы как «ничего не было»).
function CumulativeChart({ byDate, planBln }) {
  const n = byDate.length;
  const ys = [planBln || 0, ...byDate.map((d) => d.cum_113_bln ?? 0),
              ...byDate.map((d) => d.plan_line_bln ?? 0)];
  const ymax = Math.max(...ys, 1) * 1.05;
  const held = byDate.filter((d) => d.held);
  return (
    <MeasuredSvg height={220} label="кумулятив привлечения по 113-й против плана квартала">
      {({ W, H, bind }) => {
        const sx = linearScale([-0.5, n - 0.5], [PAD.l, W - PAD.r]);
        const sy = linearScale([0, ymax], [H - PAD.b, PAD.t]);
        const idx = (d) => byDate.indexOf(d);
        const xt = byDate.map((d, i) => ({ x: sx(i), label: dd(d.date) }))
          .filter((_, i) => n <= 8 || i % Math.ceil(n / 8) === 0);
        const plan = byDate.filter((d) => d.plan_line_bln != null);
        return (
          <>
            <GridY ticks={niceTicks(0, ymax, 4)} y={sy} x1={PAD.l} x2={W - PAD.r}
              lineClass="an-grid" textClass="an-axis" label={(v) => bln(v, 0)} />
            <XTicks ticks={xt} y={H - PAD.b + 14} textClass="an-axis" />
            {/* равномерная прямая плана: где надо быть к каждой дате графика */}
            {plan.length > 1 && (
              <path className="au-plan-line" fill="none"
                d={linePath([{ i: -0.5, v: 0 }, ...plan.map((d) => ({ i: idx(d), v: d.plan_line_bln }))],
                  (p) => sx(p.i), (p) => sy(p.v))} />
            )}
            {planBln != null && (
              <line x1={PAD.l} x2={W - PAD.r} y1={sy(planBln)} y2={sy(planBln)} className="au-plan-total" />
            )}
            {/* факт: ступень с нуля по прошедшим аукционам */}
            {held.length > 0 && (
              <path className="au-cum-line" fill="none" data-testid="au-cum"
                d={linePath([{ i: -0.5, v: 0 }, ...held.map((d) => ({ i: idx(d), v: d.cum_113_bln }))],
                  (p) => sx(p.i), (p) => sy(p.v))} />
            )}
            {held.map((d) => (
              <circle key={d.date} cx={sx(idx(d))} cy={sy(d.cum_113_bln)} r={3.4}
                className={"au-cum-pt" + (d.n_failed ? " failed" : "")}
                {...bind(sx(idx(d)), sy(d.cum_113_bln),
                  `${fmt.date(d.date)}\nза день ${bln(d.proceeds_113_bln)} млрд (113-я) · номинал ${bln(d.placed_bln)}`
                  + `\nнакоплено ${bln(d.cum_113_bln)} из ${bln(planBln)} млрд`
                  + (d.plan_line_bln != null ? ` · по прямой плана ${bln(d.plan_line_bln)}` : "")
                  + (d.n_failed ? `\nнесостоявшихся: ${d.n_failed}` : ""))} />
            ))}
            {/* будущие даты графика — засечки на оси */}
            {byDate.filter((d) => !d.held && d.planned).map((d) => (
              <line key={"f" + d.date} x1={sx(idx(d))} x2={sx(idx(d))}
                y1={H - PAD.b} y2={H - PAD.b - 5} className="au-future-tick" />
            ))}
            <text x={PAD.l - 44} y={PAD.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${PAD.l - 44} ${PAD.t + 4})`}>млрд ₽, по 113-й</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

// ── столбики размещения по датам (стек по типу) + линия спроса ──
function DailyBars({ byDate }) {
  const n = byDate.length;
  const ymax = Math.max(...byDate.map((d) => Math.max(d.placed_bln || 0, d.demand_bln || 0)), 1) * 1.08;
  return (
    <MeasuredSvg height={180} label="размещение по датам аукционов по типу бумаги и спрос">
      {({ W, H, bind }) => {
        const sx = linearScale([-0.5, n - 0.5], [PAD.l, W - PAD.r]);
        const sy = linearScale([0, ymax], [H - PAD.b, PAD.t]);
        const base = H - PAD.b;
        const sh = (v) => base - sy(v || 0);
        const bw = Math.max(6, Math.min(26, (W - PAD.l - PAD.r) / Math.max(n, 1) * 0.55));
        const xt = byDate.map((d, i) => ({ x: sx(i), label: dd(d.date) }))
          .filter((_, i) => n <= 8 || i % Math.ceil(n / 8) === 0);
        const demand = byDate.map((d, i) => ({ i, v: d.demand_bln })).filter((p) => p.v > 0);
        return (
          <>
            <GridY ticks={niceTicks(0, ymax, 3)} y={sy} x1={PAD.l} x2={W - PAD.r}
              lineClass="an-grid" textClass="an-axis" label={(v) => bln(v, 0)} />
            <XTicks ticks={xt} y={H - PAD.b + 14} textClass="an-axis" />
            {byDate.map((d, i) => {
              const segs = stackedBars(
                TYPE_ORDER.filter((t) => d.by_type?.[t]).map((t) => ({ value: d.by_type[t], cls: TYPE_CLS[t], t })),
                base, sh);
              const tip = `${fmt.date(d.date)}\nразмещено ${bln(d.placed_bln)} млрд · спрос ${bln(d.demand_bln)}`
                + (d.placed_bln ? ` · bid/cover ${bln(d.demand_bln / d.placed_bln, 2)}` : "")
                + "\n" + (d.issues || []).map((x) => `${x.code.replace("RMFS", "")}${x.fmt === "drpa" ? " ДРПА" : ""}`
                  + `${x.status === "failed" ? " — не состоялся" : ` ${bln((x.placed_mln || 0) / 1000)}`}`
                  + (x.wap_yield != null ? ` @ ${fmt.pct(x.wap_yield)}%` : "")).join("\n");
              return (
                <g key={d.date} {...bind(sx(i), Math.min(...segs.map((s) => s.y), base), tip)}>
                  {segs.map((s) => (
                    <rect key={s.t} x={sx(i) - bw / 2} y={s.y} width={bw} height={s.h} className={s.cls} />
                  ))}
                  {d.n_failed > 0 && d.placed_bln === 0 && (
                    <text x={sx(i)} y={base - 4} textAnchor="middle" className="an-axis au-fail-mark">×</text>
                  )}
                  {/* невидимая зона ховера над пустым днём */}
                  <rect x={sx(i) - bw / 2} y={PAD.t} width={bw} height={base - PAD.t} fill="transparent" />
                </g>
              );
            })}
            {demand.length > 1 && (
              <path className="au-demand-line" fill="none"
                d={linePath(demand, (p) => sx(p.i), (p) => sy(p.v))} />
            )}
            {demand.map((p) => (
              <circle key={p.i} cx={sx(p.i)} cy={sy(p.v)} r={2.4} className="au-demand-pt" />
            ))}
            <text x={PAD.l - 44} y={PAD.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${PAD.l - 44} ${PAD.t + 4})`}>млрд ₽, номинал</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

export function PlanFactChart({ byDate, planBln }) {
  if (!byDate?.length) return <div className="an-empty">по кварталу нет ни дат графика, ни аукционов</div>;
  return (
    <div className="au-chart an-card">
      <div className="au-chart-title">
        Накопленное привлечение по 113-й против равномерной прямой плана
        <span className="au-legend">
          <i className="au-lg au-lg-cum" /> факт <i className="au-lg au-lg-plan" /> прямая плана
          <i className="au-lg au-lg-total" /> план квартала
        </span>
      </div>
      <CumulativeChart byDate={byDate} planBln={planBln} />
      <div className="au-chart-title">
        Размещение по датам (номинал, стек по типу) и спрос
        <span className="au-legend">
          <i className="au-lg au-t-pd" /> ПД <i className="au-lg au-t-pk" /> ПК
          <i className="au-lg au-t-in" /> ИН <i className="au-lg au-lg-demand" /> спрос
        </span>
      </div>
      <DailyBars byDate={byDate} />
    </div>
  );
}

// ── ИСТОРИЯ: «где Минфин занимал» — wap-доходность аукционов по датам ──
// Точка = аукцион с доходностью (ПД; ИН — реальная, ПК — нет вовсе), цвет —
// корзина срока. Календарная шкала: здесь важны именно паузы (недели без
// аукционов длинных бумаг видно как разрыв).
export function YieldScatter({ rows, onOpen }) {
  const pts = (rows || []).filter((r) => r.wap_yield != null && r.fmt === "auction");
  if (pts.length < 2) return <div className="an-empty">мало аукционов с доходностью за период</div>;
  const dates = [...new Set(pts.map((p) => p.date))].sort();
  const t0 = dates[0], t1 = dates[dates.length - 1];
  const ys = pts.map((p) => p.wap_yield);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const pad = (hi - lo) * 0.1 || 0.25;
  const span = spanDays(dates);
  return (
    <div className="au-chart an-card">
      <div className="au-chart-title">
        Доходность размещения (средневзвешенная) по датам аукционов
        <span className="au-legend">
          <i className="au-lg au-b-short" /> до 5 лет <i className="au-lg au-b-mid" /> 5–10 лет
          <i className="au-lg au-b-long" /> от 10 лет <span className="au-lg-x">×</span> несостоявшийся
        </span>
      </div>
      <MeasuredSvg height={240} label="доходность размещения ОФЗ по датам аукционов" cursor={onOpen ? "pointer" : "default"}>
        {({ W, H, bind }) => {
          const sx = timeScale([t0 + "T00:00:00Z", t1 + "T00:00:00Z"], [PAD.l + 6, W - PAD.r - 6]);
          const sy = linearScale([lo - pad, hi + pad], [H - PAD.b, PAD.t]);
          const ti = dateTickIdx(dates, Math.max(3, Math.round((W - PAD.l) / 90)));
          const xt = ti.map((i) => ({ x: sx(dates[i] + "T00:00:00Z"), label: tickLabel(dates[i], span) }));
          const failed = (rows || []).filter((r) => r.status === "failed");
          return (
            <>
              <GridY ticks={niceTicks(lo - pad, hi + pad, 4)} y={sy} x1={PAD.l} x2={W - PAD.r}
                lineClass="an-grid" textClass="an-axis" label={(v) => fmt.pct(v, 1)} />
              <GridX ticks={xt} y1={PAD.t} y2={H - PAD.b} lineClass="an-grid an-grid-v" />
              <XTicks ticks={xt} y={H - PAD.b + 14} textClass="an-axis" />
              {failed.map((r) => (
                <text key={"x" + r.date + r.code} x={sx(r.date + "T00:00:00Z")} y={H - PAD.b - 3}
                  textAnchor="middle" className="an-axis au-fail-mark"
                  {...bind(sx(r.date + "T00:00:00Z"), H - PAD.b - 8,
                    `${fmt.date(r.date)} · ${r.shortname || r.code} — аукцион не состоялся`
                    + (r.demand_mln ? `\nспрос ${fmt.mln(r.demand_mln * 1e6)} млн` : ""))}>×</text>
              ))}
              {pts.map((p) => {
                const b = termBucket(p.term_y);
                const x = sx(p.date + "T00:00:00Z"), y = sy(p.wap_yield);
                // радиус — по размещению (√, чтобы 1 трлн не съел полотно)
                const r = 2.6 + Math.sqrt((p.placed_mln || 0) / 1000) * 0.55;
                return (
                  <circle key={p.date + p.code} cx={x} cy={y} r={Math.min(r, 12)}
                    className={"au-pt au-b-" + (b || "mid")}
                    onClick={onOpen && p.isin ? (e) => onOpen(p.isin, e.currentTarget, "fixed") : undefined}
                    {...bind(x, y, `${fmt.date(p.date)} · ${p.shortname || p.code} (${p.sec_type}`
                      + `${b ? ", " + BUCKET_LBL[b] : ""})\nYTM ${fmt.pct(p.wap_yield)}% по ср.взв. ${fmt.pct(p.wap_price)}`
                      + ` · отсечение ${fmt.pct(p.cut_yield) ?? "—"}%`
                      + `\nразмещено ${bln((p.placed_mln || 0) / 1000)} млрд · спрос ${bln((p.demand_mln || 0) / 1000)}`
                      + (p.bid_cover != null ? ` · b/c ${bln(p.bid_cover, 2)}` : "")
                      + (p.premium_bps != null ? `\nпремия к вторичке ${fmt.devBps(p.premium_bps)} бп` : ""))} />
                );
              })}
              <text x={PAD.l - 44} y={PAD.t + 4} className="an-axis-lbl"
                transform={`rotate(-90 ${PAD.l - 44} ${PAD.t + 4})`}>доходность, %</text>
            </>
          );
        }}
      </MeasuredSvg>
    </div>
  );
}
