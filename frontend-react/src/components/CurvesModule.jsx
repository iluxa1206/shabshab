import { useEffect, useMemo, useState } from "react";
import { fetchCurvePlot } from "../api.js";
import { fmt } from "../format.js";

// Вкладка кривых: par-котировки СПФИ (что запарсилось) + построенная кривая
// (spot = средняя ставка индекса на срок из DF; forward = мгновенный ~30д вперёд).
// SVG без внешних либ, тема через CSS-переменные.
export default function CurvesModule() {
  const [type, setType] = useState("ruonia");
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("loading"); // loading | ready | error
  const [err, setErr] = useState("");
  const [hover, setHover] = useState(null); // {x,y,label}

  useEffect(() => {
    let alive = true;
    setStatus("loading");
    fetchCurvePlot(type)
      .then((d) => { if (alive) { setData(d); setStatus("ready"); } })
      .catch((e) => { if (alive) { setErr(String(e.message || e)); setStatus("error"); } });
    return () => { alive = false; };
  }, [type]);

  return (
    <div className="curves-wrap" style={{ padding: "14px 18px", color: "var(--fg)" }}>
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
    </div>
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
