import { useEffect, useMemo, useState } from "react";
import { fetchCurvePlot, fetchKsPath, fetchFloaterYield, UnauthorizedError } from "../api.js";
import { fmt } from "../format.js";

// 401 → на экран логина (единственный модуль, где это не перехватывалось:
// протухшая сессия показывала «Ошибка: unauthorized» вместо логина)
const failOr401 = (onLogout, setErr, setStatus) => (e) => {
  if (e instanceof UnauthorizedError) { onLogout?.(); return; }
  setErr(String(e.message || e));
  setStatus("error");
};

// Вкладка кривых. Два вида:
//  «Кривая» — par-котировки СПФИ + построенная кривая (spot/forward).
//  «Путь КС» — рыночный форвард-путь ставки vs ручные сценарии ЦБ + факт
//              (реплика листа «КС-прогноз» из 502_504.xlsm).
// SVG без внешних либ, тема через CSS-переменные.
export default function CurvesModule({ onLogout }) {
  const [view, setView] = useState("curve"); // curve | kspath
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
      {view === "curve" ? <CurveView onLogout={onLogout} />
        : view === "kspath" ? <KsPathView onLogout={onLogout} />
        : <FloaterScenariosView onLogout={onLogout} />}
    </div>
  );
}

function CurveView({ onLogout }) {
  const [type, setType] = useState("ruonia");
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("loading");
  const [err, setErr] = useState("");
  const [hover, setHover] = useState(null);

  useEffect(() => {
    let alive = true;
    setStatus("loading");
    const fail = failOr401(onLogout, setErr, setStatus);
    fetchCurvePlot(type)
      .then((d) => { if (alive) { setData(d); setStatus("ready"); } })
      .catch((e) => { if (alive) fail(e); });
    return () => { alive = false; };
  }, [type, onLogout]);

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

// ── Путь ставки: факт (ЦБ РФ) + рыночный форвард (СПФИ) ───────────────────
function KsPathView({ onLogout }) {
  const [series, setSeries] = useState("ks"); // ks | ruonia
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("loading");
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    setStatus("loading");
    const fail = failOr401(onLogout, setErr, setStatus);
    fetchKsPath(series)
      .then((d) => { if (alive) { setData(d); setStatus("ready"); } })
      .catch((e) => { if (alive) fail(e); });
    return () => { alive = false; };
  }, [series, onLogout]);

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
          <div style={{ display: "flex", gap: 18, margin: "8px 2px 4px", color: "var(--mut)", flexWrap: "wrap" }}>
            <LegLine color="var(--fg)" dash="" label={`Факт ${label} (ЦБ РФ)`} />
            <LegLine color="var(--up)" dash="" label="Рынок (форвард СПФИ)" />
            {series === "ks" && data.points.some((p) => p.nrd_pril3_pct != null) && (
              <LegLine color="var(--accent)" dash="" label="Ожидание (НРД Прил.3)" />
            )}
            {series === "ks" && data.points.some((p) => p.forecast_pct != null) && (
              <LegLine color="var(--down)" dash="5 4" label="Прогноз ЦБ (средняя КС)" />
            )}
          </div>
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
  const iw = W - L - R, ih = H - T - B;
  const [hover, setHover] = useState(null);

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
    let ymin = Math.min(...ys), ymax = Math.max(...ys);
    const pad = (ymax - ymin) * 0.1 || 1;
    ymin -= pad; ymax += pad;
    const X = (t) => L + ((t - xmin) / (xmax - xmin)) * iw;
    const Y = (v) => T + (1 - (v - ymin) / (ymax - ymin)) * ih;
    return { X, Y, xmin, xmax, ymin, ymax };
  }, [points]);

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
  // гладкая полилиния (для НРД Прил.3 — плавная кривая, не ступени)
  const linePath = (key) => {
    const pts = points.filter((p) => p[key] != null);
    if (!pts.length) return "";
    return pts.map((p, i) => {
      const x = X(new Date(p.date).getTime()), y = Y(p[key]);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
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
        {/* прогноз ЦБ (пунктир) */}
        <path d={stepPath("forecast_pct")} fill="none" stroke="var(--down)" strokeWidth="1.5" strokeDasharray="5 4" opacity="0.9" />
        {/* ожидание НРД Прил.3 (гладкая) */}
        <path d={linePath("nrd_pril3_pct")} fill="none" stroke="var(--accent)" strokeWidth="1.8" opacity="0.95" />
        {/* рынок (форвард) */}
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
            `${hover.market_pct != null ? `рынок ${hover.market_pct}%` : ""}${hover.nrd_pril3_pct != null ? ` · Прил.3 ${hover.nrd_pril3_pct}%` : ""}${hover.forecast_pct != null ? ` · ЦБ ${hover.forecast_pct}%` : ""}`}
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
function FloaterScenariosView({ onLogout }) {
  const [isin, setIsin] = useState("");
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("idle"); // idle | loading | ready | error
  const [err, setErr] = useState("");

  const run = () => {
    const v = isin.trim().toUpperCase();
    if (!v) return;
    setStatus("loading"); setErr("");
    fetchFloaterYield(v)
      .then((d) => { setData(d); setStatus("ready"); })
      .catch(failOr401(onLogout, setErr, setStatus));
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
      )}
    </>
  );
}
