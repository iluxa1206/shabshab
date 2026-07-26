import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { fetchCurvePlot, fetchKsPath, fetchFloaterYield } from "../api.js";
import { fmt } from "../format.js";
import {
  extent, logScale, linearScale, timeScale, linTicks, yearTicks,
  linePath, stepPath, GridY, XTicks, useNearestHover, Tooltip,
  Legend, LegendLine, LegendDot,
} from "../charts/index.js";

// Вкладка кривых. Виды (URL /curves/:view):
//  «Кривая» — par-котировки СПФИ + построенная кривая (spot/forward).
//  «Путь КС» — рыночный форвард-путь ставки vs ручные сценарии ЦБ + факт
//              (реплика листа «КС-прогноз» из 502_504.xlsm).
// SVG без внешних либ, тема через CSS-переменные. 401 ловит глобальный onError QueryClient.
export default function CurvesModule() {
  const navigate = useNavigate();
  const { view: viewParam } = useParams(); // undefined | curve | kspath | floater
  const view = viewParam === "kspath" || viewParam === "floater" ? viewParam : "curve";
  const setView = (v) => navigate(v === "curve" ? "/curves" : `/curves/${v}`);
  return (
    <div className="curves-wrap" style={{ padding: "14px 18px", color: "var(--fg)" }}>
      <div style={{ marginBottom: 14 }}>
        <span className="seg" role="tablist" aria-label="Вид">
          <button className={"seg-btn" + (view === "curve" ? " active" : "")}
            onClick={() => setView("curve")}>Кривая</button>
          <button className={"seg-btn" + (view === "kspath" ? " active" : "")}
            onClick={() => setView("kspath")}>Путь ставки</button>
          <button className={"seg-btn" + (view === "floater" ? " active" : "")}
            onClick={() => setView("floater")}>Флоатер YTM</button>
        </span>
      </div>
      {view === "curve" ? <CurveView />
        : view === "kspath" ? <KsPathView />
        : <FloaterScenariosView />}
    </div>
  );
}

function CurveView() {
  const [type, setType] = useState("ruonia");
  const q = useQuery({ queryKey: ["curvePlot", type], queryFn: () => fetchCurvePlot(type) });
  const data = q.data;
  const status = q.isPending ? "loading" : q.isError ? "error" : "ready";
  const err = q.error?.message || "";

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 12 }}>
        <span className="seg" role="tablist" aria-label="Кривая">
          <button className={"seg-btn" + (type === "ruonia" ? " active" : "")}
            onClick={() => setType("ruonia")}>RUONIA OIS</button>
          <button className={"seg-btn" + (type === "keyrate" ? " active" : "")}
            onClick={() => setType("keyrate")}>КС IRS</button>
        </span>
        {data && (
          <span className="muted" style={{ fontSize: 11 }}>
            котировок: {data.quotes.length} · calc {fmt.date(data.calc_date)} · rates {fmt.date(data.rates_date)}
          </span>
        )}
      </div>

      {data?.warnings?.length > 0 && (
        <div style={{ color: "var(--neg)", fontSize: 12, marginBottom: 8 }}>
          ⚠ {data.warnings.join(" · ")}
        </div>
      )}

      {status === "loading" && <div className="muted">Загрузка…</div>}
      {status === "error" && <div style={{ color: "var(--neg)" }}>Ошибка: {err}</div>}
      {status === "ready" && data && (
        <>
          <Chart data={data} />
          <Legend>
            <LegendDot color="var(--fg)" label="par-котировки СПФИ" />
            <LegendLine color="var(--up)" label="spot (ср. ставка на срок)" />
            <LegendLine color="var(--down)" dash="4 3" label="forward (мгновенный ~30д)" />
          </Legend>
          <QuoteTable data={data} />
        </>
      )}
    </>
  );
}

function Chart({ data }) {
  const { quotes, samples } = data;
  const W = 900, H = 380, L = 46, R = 16, T = 16, B = 40;

  const geom = useMemo(() => {
    const maxDays = Math.max(...samples.map((s) => s.days), ...quotes.map((q) => q.days), 1);
    const [ymin, ymax] = extent([
      ...samples.map((s) => s.spot_pct), ...samples.map((s) => s.forward_pct),
      ...quotes.map((q) => q.value_pct),
    ], 0.12, 0.3);
    const X = logScale([7, maxDays], [L, W - R]); // x лог по дням (короткий конец не слипается)
    const Y = linearScale([ymin, ymax], [H - B, T]);
    return { X, Y, ymin, ymax };
  }, [samples, quotes]);

  const { X, Y, ymin, ymax } = geom;
  // nearest-point hover по тенорам-котировкам (единый механизм, см. charts/hover)
  const { hover, handlers } = useNearestHover({ viewW: W, points: quotes, px: (q) => X(q.days) });

  return (
    <div style={{ position: "relative" }}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }} {...handlers}>
        <GridY ticks={linTicks(ymin, ymax, 4)} y={Y} x1={L} x2={W - R} label={(v) => v.toFixed(2)} />
        <XTicks ticks={quotes.map((q) => ({ x: X(q.days), label: q.tenor }))} y={H - B + 14} />
        {/* forward (пунктир) */}
        <path d={linePath(samples, (s) => X(s.days), (s) => Y(s.forward_pct))} fill="none" stroke="var(--down)"
          strokeWidth="1.5" strokeDasharray="4 3" opacity="0.85" />
        {/* spot (сплошная) */}
        <path d={linePath(samples, (s) => X(s.days), (s) => Y(s.spot_pct))} fill="none" stroke="var(--up)" strokeWidth="2" />
        {/* котировки (точки) */}
        {quotes.map((q, i) => (
          <circle key={i} cx={X(q.days)} cy={Y(q.value_pct)} r="3.5" fill="var(--fg)" stroke="var(--bg)" strokeWidth="1">
            <title>{`${q.tenor} · ${q.value_pct.toFixed(4)}% · ${q.days}д`}</title>
          </circle>
        ))}
        {hover && (
          <g>
            <line x1={X(hover.days)} y1={T} x2={X(hover.days)} y2={H - B} stroke="var(--mut)" strokeDasharray="2 2" />
            <circle cx={X(hover.days)} cy={Y(hover.value_pct)} r="5" fill="none" stroke="var(--fg)" strokeWidth="1.5" />
          </g>
        )}
      </svg>
      {hover && (
        <Tooltip x={X(hover.days)} viewW={W}>{`${hover.tenor}: ${hover.value_pct.toFixed(4)}% (par)`}</Tooltip>
      )}
    </div>
  );
}

function QuoteTable({ data }) {
  return (
    <div style={{ marginTop: 14, overflowX: "auto" }}>
      <table className="curve-quotes" style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
        <thead>
          <tr style={{ textAlign: "right", color: "var(--mut)" }}>
            <th style={{ textAlign: "left", padding: "4px 8px" }}>Тенор</th>
            <th style={{ padding: "4px 8px" }}>Дней</th>
            <th style={{ padding: "4px 8px" }}>Par-котировка, %</th>
            <th style={{ textAlign: "left", padding: "4px 8px" }}>Инструмент</th>
          </tr>
        </thead>
        <tbody>
          {data.quotes.map((q, i) => (
            <tr key={i} style={{ borderTop: "1px solid var(--line-2)" }}>
              <td style={{ padding: "3px 8px", fontFamily: "var(--mono)" }}>{q.tenor}</td>
              <td style={{ padding: "3px 8px", textAlign: "right", color: "var(--mut)" }}>{q.days}</td>
              <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "var(--mono)" }}>{q.value_pct.toFixed(4)}</td>
              <td style={{ padding: "3px 8px", color: "var(--mut)", fontSize: 11 }}>{q.name}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Путь ставки: факт (ЦБ РФ) + рыночный форвард (СПФИ) ───────────────────
function KsPathView() {
  const [series, setSeries] = useState("ks"); // ks | ruonia
  const q = useQuery({ queryKey: ["ksPath", series], queryFn: () => fetchKsPath(series) });
  const data = q.data;
  const status = q.isPending ? "loading" : q.isError ? "error" : "ready";
  const err = q.error?.message || "";

  const label = series === "ks" ? "КС" : "RUONIA";
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 12, flexWrap: "wrap" }}>
        <span className="seg" role="tablist" aria-label="Ставка">
          <button className={"seg-btn" + (series === "ks" ? " active" : "")}
            onClick={() => setSeries("ks")}>Ключевая</button>
          <button className={"seg-btn" + (series === "ruonia" ? " active" : "")}
            onClick={() => setSeries("ruonia")}>RUONIA</button>
        </span>
        {data && (
          <span className="muted" style={{ fontSize: 11 }}>
            действующая {label}: <b style={{ color: "var(--fg)" }}>{data.current_ks_pct ?? "—"}%</b> · calc {fmt.date(data.calc_date)}
            {data.decided_rate_pct != null && (
              <b style={{ color: "var(--accent)", marginLeft: 8 }}>
                → решение {fmt.date(data.decided_decision)}: {data.decided_rate_pct}% с {fmt.date(data.decided_effective)}
              </b>
            )}
          </span>
        )}
      </div>

      {status === "loading" && <div className="muted">Загрузка…</div>}
      {status === "error" && <div style={{ color: "var(--neg)" }}>Ошибка: {err}</div>}
      {status === "ready" && data && (
        <>
          {data.warnings?.length > 0 && (
            <div style={{ color: "var(--neg)", fontSize: 12, marginBottom: 8 }}>⚠ {data.warnings.join(" · ")}</div>
          )}
          <KsPathChart points={data.points} calcDate={data.calc_date} />
          <Legend>
            <LegendLine color="var(--fg)" label={`Факт ${label} (ЦБ РФ)`} />
            <LegendLine color="var(--up)" label="Рынок (форвард СПФИ)" />
            {series === "ks" && data.points.some((p) => p.nrd_pril3_pct != null) && (
              <LegendLine color="var(--accent)" label="Ожидание (НРД Прил.3)" />
            )}
            {series === "ks" && data.points.some((p) => p.forecast_pct != null) && (
              <LegendLine color="var(--down)" dash="5 4" label="Прогноз ЦБ (средняя КС)" />
            )}
          </Legend>
          <div className="muted" style={{ fontSize: 11, marginTop: 6, maxWidth: 760 }}>
            Слева от «сегодня» — исторический факт с ЦБ РФ. «Рынок» — арбитражный форвард
            из СПФИ-свопов (та же кривая, что в прайсинге SM/z).
            {series === "ks" && " «Ожидание (НРД Прил.3)» — сплайн свопов + экспо-затухание к нейтрали ЦБ за последним тенором (10Y): методика met_float Прил.3, отдельный взгляд «куда пойдёт КС» с реверсией, не совпадает с форвардом. «Прогноз ЦБ» — среднесрочный прогноз средней КС (cbr_forecast.json)."}
            {series === "ruonia" && " История RUONIA до 2025-08 — из сида, далее живьём с ЦБ."}
          </div>
        </>
      )}
    </>
  );
}

function KsPathChart({ points, calcDate }) {
  const W = 900, H = 380, L = 46, R = 16, T = 16, B = 40;

  const g = useMemo(() => {
    const xs = points.map((p) => new Date(p.date).getTime());
    const xmin = Math.min(...xs), xmax = Math.max(...xs);
    const ys = [];
    points.forEach((p) => {
      if (p.actual_pct != null) ys.push(p.actual_pct);
      if (p.market_pct != null) ys.push(p.market_pct);
      if (p.forecast_pct != null) ys.push(p.forecast_pct);
      if (p.nrd_pril3_pct != null) ys.push(p.nrd_pril3_pct);
    });
    const [ymin, ymax] = extent(ys, 0.1, 1);
    const X = timeScale([xmin, xmax], [L, W - R]);
    const Y = linearScale([ymin, ymax], [H - B, T]);
    return { X, Y, xmin, xmax, ymin, ymax };
  }, [points]);

  const { X, Y, ymin, ymax, xmin, xmax } = g;
  const { hover, handlers } = useNearestHover({ viewW: W, points, px: (p) => X(p.date) });
  // ступени (факт/рынок/прогноз) и гладкая линия (НРД Прил.3) через общие path-билдеры
  const step = (key) => stepPath(points.filter((p) => p[key] != null), (p) => X(p.date), (p) => Y(p[key]));
  const line = (key) => linePath(points.filter((p) => p[key] != null), (p) => X(p.date), (p) => Y(p[key]));
  const todayX = X(calcDate);

  return (
    <div style={{ position: "relative" }}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }} {...handlers}>
        <GridY ticks={linTicks(ymin, ymax, 5)} y={Y} x1={L} x2={W - R} label={(v) => v.toFixed(1)} />
        <XTicks ticks={yearTicks(xmin, xmax).filter((t) => t >= xmin && t <= xmax).map((t) => ({ x: X(t), label: new Date(t).getFullYear() }))}
          y={H - B + 14} />
        {/* линия "сегодня" */}
        <line x1={todayX} y1={T} x2={todayX} y2={H - B} stroke="var(--mut)" strokeDasharray="2 3" />
        <text x={todayX + 3} y={T + 10} fontSize="9" fill="var(--mut)">сегодня</text>
        {/* прогноз ЦБ (пунктир) */}
        <path d={step("forecast_pct")} fill="none" stroke="var(--down)" strokeWidth="1.5" strokeDasharray="5 4" opacity="0.9" />
        {/* ожидание НРД Прил.3 (гладкая) */}
        <path d={line("nrd_pril3_pct")} fill="none" stroke="var(--accent)" strokeWidth="1.8" opacity="0.95" />
        {/* рынок (форвард) */}
        <path d={step("market_pct")} fill="none" stroke="var(--up)" strokeWidth="2" />
        {/* факт */}
        <path d={step("actual_pct")} fill="none" stroke="var(--fg)" strokeWidth="2" />
        {hover && (
          <line x1={X(hover.date)} y1={T} x2={X(hover.date)} y2={H - B} stroke="var(--mut)" strokeDasharray="1 2" />
        )}
      </svg>
      {hover && (
        <Tooltip x={X(hover.date)} viewW={W} top={4} dy={0} padding="3px 7px">
          {fmt.date(hover.date)} · {hover.actual_pct != null ? `факт ${hover.actual_pct}%` :
            `${hover.market_pct != null ? `рынок ${hover.market_pct}%` : ""}${hover.nrd_pril3_pct != null ? ` · Прил.3 ${hover.nrd_pril3_pct}%` : ""}${hover.forecast_pct != null ? ` · ЦБ ${hover.forecast_pct}%` : ""}`}
        </Tooltip>
      )}
    </div>
  );
}

// ── Флоатер / сценарии: YTM бумаги под рынок vs сценарии ЦБ (метод 502_504) ──
function FloaterScenariosView() {
  const [isin, setIsin] = useState("");
  const [submitted, setSubmitted] = useState(""); // ISIN, по которому запущен расчёт
  const q = useQuery({
    queryKey: ["floaterYield", submitted],
    queryFn: () => fetchFloaterYield(submitted),
    enabled: !!submitted,
  });
  const data = q.data;
  const status = !submitted ? "idle" : q.isPending || q.isFetching ? "loading" : q.isError ? "error" : "ready";
  const err = q.error?.message || "";

  const run = () => {
    const v = isin.trim().toUpperCase();
    if (!v) return;
    setSubmitted(v);
  };

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12, flexWrap: "wrap" }}>
        <input className="search" style={{ width: 200 }} placeholder="ISIN (KEYRATE-флоатер)"
          value={isin} onChange={(e) => setIsin(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()} />
        <button className="btn" onClick={run}>Оценить</button>
        {data && (
          <span className="muted" style={{ fontSize: 11 }}>
            {data.name} · спред {data.spread_bps} бп · цена {data.price_flat_pct} · КС {data.current_ks_pct}%
          </span>
        )}
      </div>

      <div className="muted" style={{ fontSize: 11, marginBottom: 12, maxWidth: 720 }}>
        Купон = среднее рыночного пути КС (форвард СПФИ) по окну рефиксинга + спред
        (метод листа Floater spread). YTM — XIRR по спроецированным потокам.
      </div>

      {status === "idle" && <div className="muted">Введи ISIN и нажми «Оценить».</div>}
      {status === "loading" && <div className="muted">Считаю…</div>}
      {status === "error" && <div style={{ color: "var(--neg)" }}>Ошибка: {err}</div>}
      {status === "ready" && data && (
        <>
        {data.rate_series?.length > 0 && (
          <div style={{ marginBottom: 18 }}>
            <BondVsIndexChart series={data.rate_series} calcDate={data.calc_date} />
            <Legend>
              <LegendLine color="var(--fg)" label="Купон бумаги (индекс + спред)" />
              <LegendLine color="var(--up)" label="Индекс КС (форвард СПФИ)" />
            </Legend>
            <div className="muted" style={{ fontSize: 11, marginTop: 4, maxWidth: 720 }}>
              Зазор между линиями = спред выпуска ({data.spread_bps} бп). Индекс — среднее
              рыночного пути КС по окну рефиксинга каждого купона.
            </div>
          </div>
        )}
        <div style={{ display: "flex", gap: 32, flexWrap: "wrap", alignItems: "flex-start" }}>
          <div>
            <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>YTM (рынок, СПФИ)</div>
            <div style={{ fontSize: 30, fontWeight: 700, fontFamily: "var(--mono)", letterSpacing: "-0.02em" }}>
              {data.ytm_pct != null ? `${data.ytm_pct.toFixed(2)}%` : "—"}
            </div>
          </div>

          <div>
            <div className="muted" style={{ fontSize: 11, marginBottom: 6 }}>Купоны (рынок), % от номинала</div>
            <table style={{ borderCollapse: "collapse", fontSize: 12 }}>
              <tbody>
                {data.coupons_market.map((c, i) => (
                  <tr key={i} style={{ borderTop: "1px solid var(--line-2)" }}>
                    <td style={{ padding: "3px 14px 3px 0", color: "var(--mut)" }}>{fmt.date(c.date)}</td>
                    <td style={{ padding: "3px 0", textAlign: "right", fontFamily: "var(--mono)" }}>{c.amount_pct.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        </>
      )}
    </>
  );
}

// bond-vs-index: ступени ставки купона бумаги vs пути индекса КС по датам купонов
function BondVsIndexChart({ series, calcDate }) {
  const W = 900, H = 320, L = 46, R = 16, T = 16, B = 40;
  const g = useMemo(() => {
    const pts = series.map((s) => ({ ...s, t: new Date(s.date).getTime() }));
    const xs = pts.map((p) => p.t);
    const xmin = Math.min(...xs, new Date(calcDate).getTime());
    const xmax = Math.max(...xs);
    const ys = pts.flatMap((p) => [p.base_pct, p.coupon_pct]);
    const [ymin, ymax] = extent(ys, 0.15, 0.4);
    const X = timeScale([xmin, xmax], [L, W - R]);
    const Y = linearScale([ymin, ymax], [H - B, T]);
    return { pts, X, Y, xmin, xmax, ymin, ymax };
  }, [series, calcDate]);

  const { pts, X, Y, ymin, ymax, xmin, xmax } = g;
  const { hover, handlers } = useNearestHover({ viewW: W, points: pts, px: (p) => X(p.t) });
  const step = (key) => stepPath(pts, (p) => X(p.t), (p) => Y(p[key]));

  return (
    <div style={{ position: "relative" }}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }} {...handlers}>
        <GridY ticks={linTicks(ymin, ymax, 5)} y={Y} x1={L} x2={W - R} label={(v) => v.toFixed(1)} />
        <XTicks ticks={yearTicks(xmin, xmax).filter((t) => t >= xmin && t <= xmax).map((t) => ({ x: X(t), label: new Date(t).getFullYear() }))}
          y={H - B + 14} />
        {/* купон бумаги (индекс + спред) */}
        <path d={step("coupon_pct")} fill="none" stroke="var(--fg)" strokeWidth="2" />
        {/* индекс КС */}
        <path d={step("base_pct")} fill="none" stroke="var(--up)" strokeWidth="2" />
        {hover && (
          <line x1={X(hover.t)} y1={T} x2={X(hover.t)} y2={H - B} stroke="var(--mut)" strokeDasharray="1 2" />
        )}
      </svg>
      {hover && (
        <Tooltip x={X(hover.t)} viewW={W} top={4} dy={0} padding="3px 7px">
          {fmt.date(hover.date)} · купон {hover.coupon_pct}% · индекс {hover.base_pct}%
        </Tooltip>
      )}
    </div>
  );
}
