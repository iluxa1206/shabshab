import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { calcCustomBond, fetchFixed } from "../api.js";
import { fmt, dmColor } from "../format.js";
import { linearScale, linTicks, GridY, XTicks, MeasuredSvg } from "../charts/index.js";

// КАЛЬКУЛЯТОР кастомной облигации: юзер вводит параметры выпуска (купон,
// частота, погашение, цена, номинал) + эмитента и рейтинг — бэк считает метрики
// тем же путём, что вкладка ФИКСЫ (/api/calc/custom), а сравнение с рынком
// строится по универсу фиксов (/api/fixed): scatter доходность×дюрация, где
// подсвечены выпуски того же эмитента и бумаги того же рейтинга.

const RT = ["AAA", "AA", "A", "BBB", "BB", "B", "NR"];
const RTCOLOR = {
  AAA: "var(--rt-aaa)", AA: "var(--rt-aa)", A: "var(--rt-a)", BBB: "var(--rt-bbb)",
  BB: "var(--rt-bb)", B: "var(--rt-b)", NR: "var(--mut-2)",
};
const norm = (r) => (r && RT.includes(r) ? r : "NR");
const D = () => <span className="dash">—</span>;

// та же отсечка мусора, что в аналитике фиксов
const okG = (v) => v != null && v < 3000 && v > -500;
const okY = (v) => v != null && v > 0 && v < 60;

const SC_PAD = { l: 46, r: 14, t: 14, b: 30 };

// ── Scatter: доходность (YTM или G-спред) × мод. дюрация ──
function CompareScatter({ peers, custom, mode, issuer }) {
  const useG = mode === "g";
  const val = (b) => (useG ? (okG(b.g_spread_bps) ? b.g_spread_bps : null)
    : (okY(b.ytm) ? b.ytm : null));
  const pts = peers
    .map((b) => ({ b, y: val(b) }))
    .filter(({ b, y }) => b.mod_dur != null && y != null)
    .map(({ b, y }) => ({
      x: b.mod_dur, y, r: norm(b.rating), isin: b.isin, name: b.name,
      mine: issuer && b.issuer === issuer,
    }));
  const cy = useG ? custom?.g_spread_bps : custom?.ytm_pct;
  const cpt = custom && custom.mod_dur != null && cy != null ? { x: custom.mod_dur, y: cy } : null;
  if (!pts.length && !cpt) return <div className="an-empty">нет данных для сравнения</div>;
  const xs = pts.map((p) => p.x).concat(cpt ? [cpt.x] : []);
  const ys = pts.map((p) => p.y).concat(cpt ? [cpt.y] : []);
  const xmax = Math.max(...xs, 1);
  const ymax = Math.max(...ys) * 1.02;
  const ymin = Math.min(...ys, useG ? 0 : Math.min(...ys)) * (useG ? 1 : 0.98);
  return (
    <MeasuredSvg height={300} label="сравнение с рынком">
      {({ W, H, bind }) => {
        const sx = linearScale([0, xmax], [SC_PAD.l, W - SC_PAD.r]);
        const sy = linearScale([ymin, ymax], [H - SC_PAD.b, SC_PAD.t]);
        const nx = Math.min(Math.ceil(xmax), Math.max(3, Math.round((W - SC_PAD.l - SC_PAD.r) / 70)));
        return (
          <>
            <GridY ticks={linTicks(ymin, ymax, 5)} y={sy} x1={SC_PAD.l} x2={W - SC_PAD.r}
              lineClass="an-grid" textClass="an-axis"
              label={(v) => (useG ? Math.round(v) : fmt.num(v, 1))} />
            <XTicks ticks={linTicks(0, xmax, nx).map((xv) => ({ x: sx(xv), label: fmt.yrs(xv) }))}
              y={H - SC_PAD.b + 14} textClass="an-axis" />
            {pts.filter((p) => !p.mine).map((p) => (
              <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={3.2}
                fill={RTCOLOR[p.r]} fillOpacity={0.55}
                {...bind(sx(p.x), sy(p.y),
                  `${p.name}\n${useG ? "g-спред: " + Math.round(p.y) + " bps" : "YTM: " + fmt.pct(p.y)}\nмод. дюрация: ${fmt.yrs(p.x)} · рейтинг: ${p.r}`)} />
            ))}
            {pts.filter((p) => p.mine).map((p) => (
              <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={4.5}
                fill={RTCOLOR[p.r]} stroke="var(--fg)" strokeWidth={1.4}
                {...bind(sx(p.x), sy(p.y),
                  `${p.name} (этот эмитент)\n${useG ? "g-спред: " + Math.round(p.y) + " bps" : "YTM: " + fmt.pct(p.y)}\nмод. дюрация: ${fmt.yrs(p.x)} · рейтинг: ${p.r}`)} />
            ))}
            {cpt && (
              <g {...bind(sx(cpt.x), sy(cpt.y),
                `Ваша облигация\n${useG ? "g-спред: " + Math.round(cpt.y) + " bps" : "YTM: " + fmt.pct(cpt.y)}\nмод. дюрация: ${fmt.yrs(cpt.x)}`)}>
                <path transform={`translate(${sx(cpt.x)} ${sy(cpt.y)})`}
                  d="M0 -7 L7 0 L0 7 L-7 0 Z" fill="var(--accent)" stroke="var(--bg)" strokeWidth={1.5} />
              </g>
            )}
            <text x={SC_PAD.l} y={H - 4} className="an-axis-lbl" textAnchor="start">мод. дюрация →</text>
            <text x={SC_PAD.l - 38} y={SC_PAD.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${SC_PAD.l - 38} ${SC_PAD.t + 4})`}>
              {useG ? "g-спред, bps" : "YTM, %"}</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

const METRICS = [
  ["ytm_pct", "YTM", "%", (v) => fmt.pct(v)],
  ["cur_yield_pct", "ТЕК. ДОХ", "%", (v) => fmt.pct(v)],
  ["g_spread_bps", "G-СПРЕД", "bps", (v) => fmt.bps(v)],
  ["z_spread_bps", "Z-СПРЕД", "bps", (v) => fmt.bps(v)],
  ["mod_dur", "ДЮРАЦИЯ МОД", "лет", (v) => fmt.num(v, 2)],
  ["mac_dur", "ДЮРАЦИЯ МАК", "лет", (v) => fmt.num(v, 2)],
  ["convexity", "ВЫПУКЛОСТЬ", "", (v) => fmt.num(v, 1)],
  ["dv01", "DV01", "₽/бум", (v) => fmt.num(v, 2)],
  ["accrued_rub", "НКД", "₽", (v) => fmt.num(v, 2)],
  ["dirty_rub", "ГРЯЗНАЯ", "₽", (v) => fmt.num(v, 2)],
];

export default function CalcModule() {
  const [form, setForm] = useState({
    coupon: "", freq: "4", maturity: "", price: "100", face: "1000",
    issuer: "", rating: "",
  });
  const [res, setRes] = useState(null);       // ответ /api/calc/custom
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("ytm");    // ось Y сравнения: ytm | g

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  // универс фиксов — база сравнения; рейтинги/эмитенты формы тоже из него
  const fixedQ = useQuery({ queryKey: ["fixed"], queryFn: fetchFixed, staleTime: 60000 });
  const all = fixedQ.data?.items || [];
  const issuers = useMemo(() => {
    const m = new Map();
    for (const b of all) if (b.issuer) m.set(b.issuer, (m.get(b.issuer) || 0) + 1);
    return [...m.keys()].sort((a, b) => a.localeCompare(b));
  }, [all]);

  // пиры для графика: тот же рейтинг (если выбран) ∪ тот же эмитент
  const peers = useMemo(() => {
    if (!form.rating && !form.issuer) return all;
    return all.filter((b) =>
      (form.rating && norm(b.rating) === form.rating) || (form.issuer && b.issuer === form.issuer));
  }, [all, form.rating, form.issuer]);
  const sameIssuer = useMemo(
    () => (form.issuer ? all.filter((b) => b.issuer === form.issuer) : []),
    [all, form.issuer]);

  const canCalc = form.coupon !== "" && form.maturity && form.price !== "";
  const onCalc = async (e) => {
    e?.preventDefault();
    if (!canCalc || busy) return;
    setBusy(true); setErr("");
    try {
      const r = await calcCustomBond({
        coupon: form.coupon, freq: form.freq, maturity: form.maturity,
        price: form.price, face: form.face || 1000,
      });
      setRes(r);
    } catch (ex) {
      setRes(null);
      setErr(ex.message || "ошибка расчёта");
    } finally {
      setBusy(false);
    }
  };

  const m = res?.metrics;
  return (
    <div className="calc-page">
      <form className="an-card calc-form" onSubmit={onCalc}>
        <div className="an-title">ПАРАМЕТРЫ ОБЛИГАЦИИ
          <span className="an-hint">купон и цена — в % · расчёт на дату {res ? fmt.date(res.calc_date) : "последних торгов"}</span>
        </div>
        <div className="calc-grid">
          <label className="calc-f">
            <span className="calc-k">Купон, % годовых</span>
            <input type="number" step="0.01" min="0" max="100" required
              value={form.coupon} onChange={set("coupon")} placeholder="14.5" />
          </label>
          <label className="calc-f">
            <span className="calc-k">Выплат в год</span>
            <select value={form.freq} onChange={set("freq")}>
              <option value="12">12 (ежемесячно)</option>
              <option value="4">4 (квартал)</option>
              <option value="2">2 (полугодие)</option>
              <option value="1">1 (год)</option>
            </select>
          </label>
          <label className="calc-f">
            <span className="calc-k">Погашение</span>
            <input type="date" required value={form.maturity} onChange={set("maturity")} />
          </label>
          <label className="calc-f">
            <span className="calc-k">Чистая цена, %</span>
            <input type="number" step="0.01" min="1" max="1000" required
              value={form.price} onChange={set("price")} />
          </label>
          <label className="calc-f">
            <span className="calc-k">Номинал, ₽</span>
            <input type="number" step="1" min="1" value={form.face} onChange={set("face")} />
          </label>
          <label className="calc-f">
            <span className="calc-k">Эмитент (для сравнения)</span>
            <input list="calc-issuers" value={form.issuer} onChange={set("issuer")}
              placeholder="начните вводить…" />
            <datalist id="calc-issuers">
              {issuers.map((n) => <option key={n} value={n} />)}
            </datalist>
          </label>
          <label className="calc-f">
            <span className="calc-k">Рейтинг (для сравнения)</span>
            <select value={form.rating} onChange={set("rating")}>
              <option value="">— не задан —</option>
              {RT.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <button className="btn calc-go" type="submit" disabled={!canCalc || busy}>
            {busy ? "считаю…" : "Рассчитать"}
          </button>
        </div>
        {err && <div className="calc-err">{err}</div>}
      </form>

      {m && (
        <div className="an-card">
          <div className="an-title">МЕТРИКИ</div>
          <div className="calc-metrics">
            {METRICS.map(([k, label, unit, f]) => (
              <div className="calc-m" key={k}>
                <span className="kpi-label">{label}</span>
                <span className="calc-m-v"
                  style={k === "g_spread_bps" || k === "z_spread_bps"
                    ? (m[k] != null ? dmColor(m[k]) : undefined) : undefined}>
                  {m[k] == null ? <D /> : f(m[k])}
                  {unit && m[k] != null && <span className="kpi-unit"> {unit}</span>}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="an-card">
        <div className="an-title">СРАВНЕНИЕ С РЫНКОМ
          <span className="an-hint">
            {form.issuer || form.rating
              ? "точки = " + [form.rating && `рейтинг ${form.rating}`, form.issuer && `эмитент ${form.issuer} (с обводкой)`].filter(Boolean).join(" + ")
              : "весь универс фиксов — задайте эмитента/рейтинг для фокуса"}
            {" · ромб = ваша облигация"}
          </span>
          <span className="an-toggle" role="group" aria-label="ось Y">
            {[["ytm", "YTM"], ["g", "G-спред"]].map(([v, l]) => (
              <button key={v} type="button" className={"an-tgl-btn" + (mode === v ? " on" : "")}
                aria-pressed={mode === v} onClick={() => setMode(v)}>{l}</button>
            ))}
          </span>
        </div>
        {fixedQ.isLoading
          ? <div className="an-empty">загрузка универса фиксов…</div>
          : <CompareScatter peers={peers} custom={m} mode={mode} issuer={form.issuer} />}
        <div className="an-legend">
          <span className="an-leg-lbl">цвет:</span>
          {RT.map((k) => (
            <span key={k} className="an-leg-item">
              <span className="an-leg-swatch" style={{ background: RTCOLOR[k] }} />{k}
            </span>
          ))}
        </div>
      </div>

      {sameIssuer.length > 0 && (
        <div className="an-card">
          <div className="an-title">ВЫПУСКИ ЭМИТЕНТА <span className="an-hint">{form.issuer} · {sameIssuer.length} шт · сортировка по дюрации</span></div>
          <div className="fx-table-wrap calc-table-wrap">
            <table className="grid packed">
              <thead>
                <tr>
                  <th className="left">ВЫПУСК</th><th className="num">ЦЕНА</th>
                  <th className="num">YTM</th><th className="num">G-СПРЕД</th>
                  <th className="num">ДЮР</th><th className="num">КУПОН</th>
                  <th className="num">ПОГАШЕНИЕ</th><th className="num">РЕЙТИНГ</th>
                </tr>
              </thead>
              <tbody>
                {sameIssuer.slice().sort((a, b) => (a.mod_dur ?? 99) - (b.mod_dur ?? 99)).map((b) => (
                  <tr key={b.isin}>
                    <td className="left">{b.name}</td>
                    <td className="num">{fmt.pct(b.last_price_pct) ?? <D />}</td>
                    <td className="num">{b.ytm == null ? <D /> : fmt.pct(b.ytm)}</td>
                    <td className="num" style={b.g_spread_bps != null ? dmColor(b.g_spread_bps) : undefined}>
                      {b.g_spread_bps == null ? <D /> : fmt.bps(b.g_spread_bps)}</td>
                    <td className="num">{b.mod_dur == null ? <D /> : fmt.num(b.mod_dur, 2)}</td>
                    <td className="num">{b.coupon_pct == null ? <D /> : fmt.pct(b.coupon_pct)}</td>
                    <td className="num">{b.maturity_date ? fmt.date(b.maturity_date) : <D />}</td>
                    <td className="num">{b.rating
                      ? <span className="fx-rt" style={{ color: RTCOLOR[b.rating] }}>{b.rating}</span> : <D />}</td>
                  </tr>
                ))}
                {res && m?.mod_dur != null && (
                  <tr className="calc-row-mine">
                    <td className="left">Ваша облигация</td>
                    <td className="num">{fmt.pct(Number(form.price))}</td>
                    <td className="num">{m.ytm_pct == null ? <D /> : fmt.pct(m.ytm_pct)}</td>
                    <td className="num" style={m.g_spread_bps != null ? dmColor(m.g_spread_bps) : undefined}>
                      {m.g_spread_bps == null ? <D /> : fmt.bps(m.g_spread_bps)}</td>
                    <td className="num">{fmt.num(m.mod_dur, 2)}</td>
                    <td className="num">{fmt.pct(Number(form.coupon))}</td>
                    <td className="num">{fmt.date(form.maturity)}</td>
                    <td className="num">{form.rating || <D />}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {res?.cashflow?.length > 0 && (
        <div className="an-card">
          <div className="an-title">ПОТОК ПЛАТЕЖЕЙ <span className="an-hint">будущие выплаты на одну бумагу</span></div>
          <div className="calc-table-wrap">
            <table className="grid packed">
              <thead><tr><th className="left">ДАТА</th><th className="left">ТИП</th><th className="num">СТАВКА</th><th className="num">₽</th></tr></thead>
              <tbody>
                {res.cashflow.map((c, i) => (
                  <tr key={i}>
                    <td className="left">{fmt.date(c.date)}</td>
                    <td className="left">{c.type === "COUPON" ? "купон" : "погашение"}</td>
                    <td className="num">{c.rate_pct == null ? <D /> : fmt.pct(c.rate_pct)}</td>
                    <td className="num">{fmt.num(c.amount, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
