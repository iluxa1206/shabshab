import { useEffect, useMemo, useState } from "react";
import { fetchCurvePlot, fetchKsPath, fetchFloaterScenarios } from "../api.js";
import { fmt } from "../format.js";

// Вкладка кривых. Два вида:
//  «Кривая» — par-котировки СПФИ + построенная кривая (spot/forward).
//  «Путь КС» — рыночный форвард-путь ставки vs ручные сценарии ЦБ + факт
//              (реплика листа «КС-прогноз» из 502_504.xlsm).
// SVG без внешних либ, тема через CSS-переменные.
export default function CurvesModule() {
  const [view, setView] = useState("curve"); // curve | kspath
  return (
    <div className="curves-wrap" style={{ padding: "14px 18px", color: "var(--fg)" }}>
      <div style={{ marginBottom: 14 }}>
        <span className="seg" role="tablist" aria-label="Вид">
          <button className={"seg-btn" + (view === "curve" ? " active" : "")}
            onClick={() => setView("curve")}>Кривая</button>
          <button className={"seg-btn" + (view === "kspath" ? " active" : "")}
            onClick={() => setView("kspath")}>Путь КС</button>
          <button className={"seg-btn" + (view === "floater" ? " active" : "")}
            onClick={() => setView("floater")}>Флоатер / сценарии</button>
        </span>
      </div>
      {view === "curve" ? <CurveView /> : view === "kspath" ? <KsPathView /> : <FloaterScenariosView />}
    </div>
  );
}

function CurveView() {
  const [type, setType] = useState("ruonia");
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("loading");
  const [err, setErr] = useState("");
  const [hover, setHover] = useState(null);

  useEffect(() => {
    let alive = true;
    setStatus("loading");
    fetchCurvePlot(type)
      .then((d) => { if (alive) { setData(d); setStatus("ready"); } })
      .catch((e) => { if (alive) { setErr(String(e.message || e)); setStatus("error"); } });
    return () => { alive = false; };
  }, [type]);

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
          <Chart data={data} hover={hover} setHover={setHover} />
          <Legend />
          <QuoteTable data={data} />
        </>
      )}
    </>
  );
}

function Chart({ data, hover, setHover }) {
  const { quotes, samples } = data;
  const W = 900, H = 380, L = 46, R = 16, T = 16, B = 40;
  const iw = W - L - R, ih = H - T - B;

  const geom = useMemo(() => {
    const maxDays = Math.max(...samples.map((s) => s.days), ...quotes.map((q) => q.days), 1);
    const ys = [
      ...samples.map((s) => s.spot_pct), ...samples.map((s) => s.forward_pct),
      ...quotes.map((q) => q.value_pct),
    ];
    let ymin = Math.min(...ys), ymax = Math.max(...ys);
    const padY = (ymax - ymin) * 0.12 || 0.3;
    ymin -= padY; ymax += padY;
    // x лог по дням (короткий конец не слипается)
    const lx = (d) => Math.log(Math.max(d, 1));
    const lxmin = lx(7), lxmax = lx(maxDays);
    const X = (d) => L + ((lx(d) - lxmin) / (lxmax - lxmin)) * iw;
    const Y = (v) => T + (1 - (v - ymin) / (ymax - ymin)) * ih;
    return { X, Y, ymin, ymax, maxDays };
  }, [samples, quotes]);

  const { X, Y, ymin, ymax } = geom;
  const path = (arr, key) => arr.map((s, i) => `${i ? "L" : "M"}${X(s.days).toFixed(1)},${Y(s[key]).toFixed(1)}`).join(" ");

  // сетка Y (5 линий)
  const yticks = [];
  for (let i = 0; i <= 4; i++) yticks.push(ymin + ((ymax - ymin) * i) / 4);
  // сетка X по тенорам-котировкам
  const xticks = quotes.map((q) => ({ d: q.days, label: q.tenor }));

  return (
    <div style={{ position: "relative" }}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}
        onMouseLeave={() => setHover(null)}>
        {/* Y-сетка */}
        {yticks.map((v, i) => (
          <g key={i}>
            <line x1={L} y1={Y(v)} x2={W - R} y2={Y(v)} stroke="var(--line-2)" strokeWidth="1" />
            <text x={L - 6} y={Y(v) + 3} textAnchor="end" fontSize="10" fill="var(--mut)">{v.toFixed(2)}</text>
          </g>
        ))}
        {/* X-тики (теноры) */}
        {xticks.map((t, i) => (
          <text key={i} x={X(t.d)} y={H - B + 14} textAnchor="middle" fontSize="9" fill="var(--mut)">{t.label}</text>
        ))}
        {/* forward (пунктир) */}
        <path d={path(samples, "forward_pct")} fill="none" stroke="var(--down)" strokeWidth="1.5"
          strokeDasharray="4 3" opacity="0.85" />
        {/* spot (сплошная) */}
        <path d={path(samples, "spot_pct")} fill="none" stroke="var(--up)" strokeWidth="2" />
        {/* котировки (точки) */}
        {quotes.map((q, i) => (
          <circle key={i} cx={X(q.days)} cy={Y(q.value_pct)} r="3.5"
            fill="var(--fg)" stroke="var(--bg)" strokeWidth="1"
            onMouseEnter={() => setHover({
              x: X(q.days), y: Y(q.value_pct),
              label: `${q.tenor}: ${q.value_pct.toFixed(4)}% (par)`,
            })}>
            <title>{`${q.tenor} · ${q.value_pct.toFixed(4)}% · ${q.days}д`}</title>
          </circle>
        ))}
        {hover && (
          <g>
            <line x1={hover.x} y1={T} x2={hover.x} y2={H - B} stroke="var(--mut)" strokeDasharray="2 2" />
            <circle cx={hover.x} cy={hover.y} r="5" fill="none" stroke="var(--fg)" strokeWidth="1.5" />
          </g>
        )}
      </svg>
      {hover && (
        <div style={{
          position: "absolute", left: `${(hover.x / W) * 100}%`, top: 0,
          transform: "translate(-50%, -4px)", fontSize: 11, background: "var(--inv-bg)",
          color: "var(--inv-fg)", padding: "2px 6px", borderRadius: 4, whiteSpace: "nowrap",
          pointerEvents: "none",
        }}>{hover.label}</div>
      )}
    </div>
  );
}

function Legend() {
  const item = (color, dash, label) => (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11 }}>
      <svg width="24" height="8"><line x1="0" y1="4" x2="24" y2="4" stroke={color}
        strokeWidth="2" strokeDasharray={dash} /></svg>{label}
    </span>
  );
  return (
    <div style={{ display: "flex", gap: 18, margin: "8px 2px 4px", color: "var(--mut)", flexWrap: "wrap" }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11 }}>
        <svg width="12" height="12"><circle cx="6" cy="6" r="3.5" fill="var(--fg)" /></svg>
        par-котировки СПФИ
      </span>
      {item("var(--up)", "", "spot (ср. ставка на срок)")}
      {item("var(--down)", "4 3", "forward (мгновенный ~30д)")}
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

// ── Путь КС: рыночный форвард vs сценарии ЦБ + факт ──────────────────────
function KsPathView() {
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("loading");
  const [err, setErr] = useState("");
  const [scenario, setScenario] = useState("base"); // flat | base | fast

  useEffect(() => {
    let alive = true;
    setStatus("loading");
    fetchKsPath()
      .then((d) => { if (alive) { setData(d); setStatus("ready"); } })
      .catch((e) => { if (alive) { setErr(String(e.message || e)); setStatus("error"); } });
    return () => { alive = false; };
  }, []);

  if (status === "loading") return <div className="muted">Загрузка…</div>;
  if (status === "error") return <div style={{ color: "var(--neg)" }}>Ошибка: {err}</div>;
  if (!data) return null;

  const scKey = scenario + "_pct";
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 12, flexWrap: "wrap" }}>
        <span className="seg" role="tablist" aria-label="Сценарий ЦБ">
          {Object.entries(data.scenario_labels).map(([k, label]) => (
            <button key={k} className={"seg-btn" + (scenario === k ? " active" : "")}
              onClick={() => setScenario(k)}>{label}</button>
          ))}
        </span>
        <span className="muted" style={{ fontSize: 11 }}>
          действующая КС: <b style={{ color: "var(--fg)" }}>{data.current_ks_pct ?? "—"}%</b> · calc {fmt.date(data.calc_date)}
        </span>
      </div>

      {data.warnings?.length > 0 && (
        <div style={{ color: "var(--neg)", fontSize: 12, marginBottom: 8 }}>⚠ {data.warnings.join(" · ")}</div>
      )}

      <KsPathChart points={data.points} scKey={scKey} calcDate={data.calc_date} />

      <div style={{ display: "flex", gap: 18, margin: "8px 2px 4px", color: "var(--mut)", flexWrap: "wrap" }}>
        <LegLine color="var(--fg)" dash="" label="Факт КС" />
        <LegLine color="var(--up)" dash="" label="Рынок (СПФИ форвард)" />
        <LegLine color="var(--down)" dash="5 4" label={`Сценарий: ${data.scenario_labels[scenario]}`} />
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 6, maxWidth: 720 }}>
        Ступенька по датам заседаний ЦБ. «Рынок» — форвард нашей bootstrap-кривой КС
        (что закладывают своп-котировки СПФИ); «Сценарий» — ручная траектория из
        502_504.xlsm. Расхождение = разница взгляда рынка и ручного прогноза.
      </div>
    </>
  );
}

function KsPathChart({ points, scKey, calcDate }) {
  const W = 900, H = 380, L = 46, R = 16, T = 16, B = 40;
  const iw = W - L - R, ih = H - T - B;
  const [hover, setHover] = useState(null);

  const g = useMemo(() => {
    const xs = points.map((p) => new Date(p.date).getTime());
    const xmin = Math.min(...xs), xmax = Math.max(...xs);
    const ys = [];
    points.forEach((p) => {
      if (p.actual_pct != null) ys.push(p.actual_pct);
      if (p.market_pct != null) ys.push(p.market_pct);
      ys.push(p[scKey]);
    });
    let ymin = Math.min(...ys), ymax = Math.max(...ys);
    const pad = (ymax - ymin) * 0.1 || 1;
    ymin -= pad; ymax += pad;
    const X = (t) => L + ((t - xmin) / (xmax - xmin)) * iw;
    const Y = (v) => T + (1 - (v - ymin) / (ymax - ymin)) * ih;
    return { X, Y, xmin, xmax, ymin, ymax };
  }, [points, scKey]);

  const { X, Y, ymin, ymax, xmin, xmax } = g;
  // ступенчатый путь: горизонт до след. точки, потом вертикаль
  const stepPath = (key) => {
    const pts = points.filter((p) => p[key] != null);
    if (!pts.length) return "";
    let d = "";
    pts.forEach((p, i) => {
      const x = X(new Date(p.date).getTime()), y = Y(p[key]);
      if (i === 0) d += `M${x.toFixed(1)},${y.toFixed(1)}`;
      else {
        const px = X(new Date(pts[i - 1].date).getTime());
        d += `L${x.toFixed(1)},${Y(pts[i - 1][key]).toFixed(1)} L${x.toFixed(1)},${y.toFixed(1)}`;
      }
    });
    return d;
  };

  const yticks = [];
  for (let i = 0; i <= 5; i++) yticks.push(ymin + ((ymax - ymin) * i) / 5);
  // X-годовые тики
  const years = [];
  const y0 = new Date(xmin).getFullYear(), y1 = new Date(xmax).getFullYear();
  for (let y = y0; y <= y1; y++) years.push(new Date(`${y}-01-01`).getTime());
  const todayX = X(new Date(calcDate).getTime());

  return (
    <div style={{ position: "relative" }}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const px = ((e.clientX - r.left) / r.width) * W;
          const t = xmin + ((px - L) / iw) * (xmax - xmin);
          let best = null, bd = 1e18;
          points.forEach((p) => { const d = Math.abs(new Date(p.date).getTime() - t); if (d < bd) { bd = d; best = p; } });
          if (best) setHover(best);
        }}>
        {yticks.map((v, i) => (
          <g key={i}>
            <line x1={L} y1={Y(v)} x2={W - R} y2={Y(v)} stroke="var(--line-2)" />
            <text x={L - 6} y={Y(v) + 3} textAnchor="end" fontSize="10" fill="var(--mut)">{v.toFixed(1)}</text>
          </g>
        ))}
        {years.map((t, i) => t >= xmin && t <= xmax && (
          <text key={i} x={X(t)} y={H - B + 14} textAnchor="middle" fontSize="9" fill="var(--mut)">{new Date(t).getFullYear()}</text>
        ))}
        {/* линия "сегодня" */}
        <line x1={todayX} y1={T} x2={todayX} y2={H - B} stroke="var(--mut)" strokeDasharray="2 3" />
        <text x={todayX + 3} y={T + 10} fontSize="9" fill="var(--mut)">сегодня</text>
        {/* сценарий (пунктир) */}
        <path d={stepPath(scKey)} fill="none" stroke="var(--down)" strokeWidth="1.5" strokeDasharray="5 4" opacity="0.9" />
        {/* рынок */}
        <path d={stepPath("market_pct")} fill="none" stroke="var(--up)" strokeWidth="2" />
        {/* факт */}
        <path d={stepPath("actual_pct")} fill="none" stroke="var(--fg)" strokeWidth="2" />
        {hover && (
          <line x1={X(new Date(hover.date).getTime())} y1={T} x2={X(new Date(hover.date).getTime())} y2={H - B}
            stroke="var(--mut)" strokeDasharray="1 2" />
        )}
      </svg>
      {hover && (
        <div style={{
          position: "absolute", left: `${(X(new Date(hover.date).getTime()) / W) * 100}%`, top: 4,
          transform: "translate(-50%, 0)", fontSize: 11, background: "var(--inv-bg)", color: "var(--inv-fg)",
          padding: "3px 7px", borderRadius: 4, whiteSpace: "nowrap", pointerEvents: "none",
        }}>
          {fmt.date(hover.date)} · {hover.actual_pct != null ? `факт ${hover.actual_pct}%` :
            `рынок ${hover.market_pct ?? "—"}% · сцен ${hover[scKey]}%`}
        </div>
      )}
    </div>
  );
}

function LegLine({ color, dash, label }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11 }}>
      <svg width="24" height="8"><line x1="0" y1="4" x2="24" y2="4" stroke={color} strokeWidth="2" strokeDasharray={dash} /></svg>{label}
    </span>
  );
}

// ── Флоатер / сценарии: YTM бумаги под рынок vs сценарии ЦБ (метод 502_504) ──
function FloaterScenariosView() {
  const [isin, setIsin] = useState("");
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("idle"); // idle | loading | ready | error
  const [err, setErr] = useState("");

  const run = () => {
    const v = isin.trim().toUpperCase();
    if (!v) return;
    setStatus("loading"); setErr("");
    fetchFloaterScenarios(v)
      .then((d) => { setData(d); setStatus("ready"); })
      .catch((e) => { setErr(String(e.message || e)); setStatus("error"); });
  };

  const mkt = data?.scenarios?.find((s) => s.key === "market")?.ytm_pct;

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
        Купон = среднее прогноза КС по окну рефиксинга + спред (метод листа Floater spread).
        YTM (XIRR) под рынок (форвард СПФИ) и под ручные сценарии ЦБ — справедливая
        доходность флоатера зависит от траектории ставки.
      </div>

      {status === "idle" && <div className="muted">Введи ISIN и нажми «Оценить».</div>}
      {status === "loading" && <div className="muted">Считаю…</div>}
      {status === "error" && <div style={{ color: "var(--neg)" }}>Ошибка: {err}</div>}
      {status === "ready" && data && (
        <div style={{ display: "flex", gap: 32, flexWrap: "wrap", alignItems: "flex-start" }}>
          <table style={{ borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ color: "var(--mut)", textAlign: "left" }}>
                <th style={{ padding: "4px 14px 4px 0" }}>Сценарий КС</th>
                <th style={{ padding: "4px 0", textAlign: "right" }}>YTM, %</th>
                <th style={{ padding: "4px 0 4px 14px", textAlign: "right" }}>vs рынок</th>
              </tr>
            </thead>
            <tbody>
              {data.scenarios.map((s) => {
                const isMkt = s.key === "market";
                const diff = (mkt != null && s.ytm_pct != null && !isMkt) ? (s.ytm_pct - mkt) : null;
                return (
                  <tr key={s.key} style={{ borderTop: "1px solid var(--line-2)" }}>
                    <td style={{ padding: "5px 14px 5px 0", fontWeight: isMkt ? 700 : 400 }}>{s.label}</td>
                    <td style={{ padding: "5px 0", textAlign: "right", fontFamily: "var(--mono)", fontWeight: isMkt ? 700 : 400 }}>
                      {s.ytm_pct != null ? s.ytm_pct.toFixed(2) : "—"}
                    </td>
                    <td style={{ padding: "5px 0 5px 14px", textAlign: "right", fontFamily: "var(--mono)",
                      color: diff == null ? "var(--mut-2)" : diff >= 0 ? "var(--pos)" : "var(--neg)" }}>
                      {diff == null ? "—" : `${diff >= 0 ? "+" : ""}${diff.toFixed(2)}`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

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
      )}
    </>
  );
}
