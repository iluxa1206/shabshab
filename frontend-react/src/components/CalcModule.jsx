import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { calcCustomBond, calcCustomFloater, fetchBonds, fetchFixed } from "../api.js";
import { fmt, dmColor } from "../format.js";
import { linearScale, linTicks, GridY, XTicks, MeasuredSvg } from "../charts/index.js";

// КАЛЬКУЛЯТОР кастомной облигации: юзер вводит параметры выпуска + эмитента и
// рейтинг — бэк считает метрики тем же путём, что таблицы (ФИКС →
// /api/calc/custom, YTM/G-спред; ФЛОАТЕР → /api/calc/custom_floater,
// Y-IDX/SM/DM), а сравнение с рынком строится по соответствующему универсу
// (/api/fixed или /api/bonds): scatter доходность×срок, где подсвечены выпуски
// того же эмитента и бумаги того же рейтинга.

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

// лет до погашения от сегодня (МСК не критична: точность оси — недели)
const yrsTo = (iso) => (iso ? (new Date(iso) - Date.now()) / (365.25 * 864e5) : null);

const SC_PAD = { l: 46, r: 14, t: 14, b: 30 };

// оси сравнения по типу бумаги: фиксы — метрика × мод.дюрация,
// флоатеры — метрика × лет до погашения
const AXES = {
  fixed: {
    x: (b) => b.mod_dur, xLabel: "мод. дюрация →",
    modes: {
      ytm: { label: "YTM", axis: "YTM, %", val: (b) => (okY(b.ytm) ? b.ytm : null),
             custom: (m) => m?.ytm_pct, fmt: (v) => "YTM: " + fmt.pct(v), tick: (v) => fmt.num(v, 1) },
      g: { label: "G-спред", axis: "g-спред, bps", val: (b) => (okG(b.g_spread_bps) ? b.g_spread_bps : null),
           custom: (m) => m?.g_spread_bps, fmt: (v) => "g-спред: " + Math.round(v) + " bps", tick: (v) => Math.round(v) },
    },
  },
  float: {
    x: (b) => yrsTo(b.maturity_date), xLabel: "лет до погашения →",
    modes: {
      yidx: { label: "R-spread", axis: "R-spread, bps", val: (b) => (okG(b.yield_over_index_bps) ? b.yield_over_index_bps : null),
              custom: (m) => m?.y_idx_bps, fmt: (v) => "R-spread: " + Math.round(v) + " bps", tick: (v) => Math.round(v) },
      sm: { label: "SM", axis: "SM, bps", val: (b) => (okG(b.dm_bps) ? b.dm_bps : null),
            custom: (m) => m?.sm_bps, fmt: (v) => "SM: " + Math.round(v) + " bps", tick: (v) => Math.round(v) },
    },
  },
};

// ── Scatter: метрика доходности × срок ──
function CompareScatter({ peers, custom, kind, mode, issuer, issuerOf }) {
  const ax = AXES[kind];
  const md = ax.modes[mode] || Object.values(ax.modes)[0];
  const pts = peers
    .map((b) => ({ b, x: ax.x(b), y: md.val(b) }))
    .filter(({ x, y }) => x != null && x > 0 && y != null)
    .map(({ b, x, y }) => ({
      x, y, r: norm(b.rating), isin: b.isin, name: b.name,
      mine: issuer && issuerOf(b) === issuer,
    }));
  const cy = md.custom(custom);
  const cpt = custom && custom._x != null && cy != null ? { x: custom._x, y: cy } : null;
  if (!pts.length && !cpt) return <div className="an-empty">нет данных для сравнения</div>;
  const xs = pts.map((p) => p.x).concat(cpt ? [cpt.x] : []);
  const ys = pts.map((p) => p.y).concat(cpt ? [cpt.y] : []);
  const xmax = Math.max(...xs, 1);
  const ymax = Math.max(...ys) * 1.02;
  const ymin = Math.min(...ys) * 0.98;
  return (
    <MeasuredSvg height={300} label="сравнение с рынком">
      {({ W, H, bind }) => {
        const sx = linearScale([0, xmax], [SC_PAD.l, W - SC_PAD.r]);
        const sy = linearScale([ymin, ymax], [H - SC_PAD.b, SC_PAD.t]);
        const nx = Math.min(Math.ceil(xmax), Math.max(3, Math.round((W - SC_PAD.l - SC_PAD.r) / 70)));
        const tip = (p, pre) => `${pre}\n${md.fmt(p.y)}\n${ax.xLabel.replace(" →", "")}: ${fmt.yrs(p.x)}`;
        return (
          <>
            <GridY ticks={linTicks(ymin, ymax, 5)} y={sy} x1={SC_PAD.l} x2={W - SC_PAD.r}
              lineClass="an-grid" textClass="an-axis" label={md.tick} />
            <XTicks ticks={linTicks(0, xmax, nx).map((xv) => ({ x: sx(xv), label: fmt.yrs(xv) }))}
              y={H - SC_PAD.b + 14} textClass="an-axis" />
            {pts.filter((p) => !p.mine).map((p) => (
              <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={3.2}
                fill={RTCOLOR[p.r]} fillOpacity={0.55}
                {...bind(sx(p.x), sy(p.y), tip(p, `${p.name} · рейтинг: ${p.r}`))} />
            ))}
            {pts.filter((p) => p.mine).map((p) => (
              <circle key={p.isin} cx={sx(p.x)} cy={sy(p.y)} r={4.5}
                fill={RTCOLOR[p.r]} stroke="var(--fg)" strokeWidth={1.4}
                {...bind(sx(p.x), sy(p.y), tip(p, `${p.name} (этот эмитент) · рейтинг: ${p.r}`))} />
            ))}
            {cpt && (
              <g {...bind(sx(cpt.x), sy(cpt.y), tip(cpt, "Ваша облигация"))}>
                <path transform={`translate(${sx(cpt.x)} ${sy(cpt.y)})`}
                  d="M0 -7 L7 0 L0 7 L-7 0 Z" fill="var(--accent)" stroke="var(--bg)" strokeWidth={1.5} />
              </g>
            )}
            <text x={SC_PAD.l} y={H - 4} className="an-axis-lbl" textAnchor="start">{ax.xLabel}</text>
            <text x={SC_PAD.l - 38} y={SC_PAD.t + 4} className="an-axis-lbl"
              transform={`rotate(-90 ${SC_PAD.l - 38} ${SC_PAD.t + 4})`}>{md.axis}</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

const spreadStyle = (v) => (v != null ? dmColor(v) : undefined);
const METRICS = {
  fixed: [
    ["ytm_pct", "YTM", "%", (v) => fmt.pct(v)],
    ["cur_yield_pct", "ТЕК. ДОХ", "%", (v) => fmt.pct(v)],
    ["g_spread_bps", "G-СПРЕД", "bps", (v) => fmt.bps(v), spreadStyle],
    ["z_spread_bps", "Z-СПРЕД", "bps", (v) => fmt.bps(v), spreadStyle],
    ["mod_dur", "ДЮРАЦИЯ МОД", "лет", (v) => fmt.num(v, 2)],
    ["mac_dur", "ДЮРАЦИЯ МАК", "лет", (v) => fmt.num(v, 2)],
    ["convexity", "ВЫПУКЛОСТЬ", "", (v) => fmt.num(v, 1)],
    ["dv01", "DV01", "₽/бум", (v) => fmt.num(v, 2)],
    ["accrued_rub", "НКД", "₽", (v) => fmt.num(v, 2)],
    ["dirty_rub", "ГРЯЗНАЯ", "₽", (v) => fmt.num(v, 2)],
  ],
  float: [
    ["y_idx_bps", "R-spread", "bps", (v) => fmt.bps(v), spreadStyle],
    ["sm_bps", "SM", "bps", (v) => fmt.bps(v), spreadStyle],
    ["dm_bps", "DM", "bps", (v) => fmt.bps(v), spreadStyle],
    ["yield_xirr_pct", "YTM МОДЕЛЬ", "%", (v) => fmt.pct(v)],
    ["index_yield_pct", "ДОХ. ИНДЕКСА", "%", (v) => fmt.pct(v)],
    ["accrued_rub", "НКД", "₽", (v) => fmt.num(v, 2)],
    ["dirty_rub", "ГРЯЗНАЯ", "₽", (v) => fmt.num(v, 2)],
  ],
};

export default function CalcModule({ initialKind = "fixed" }) {
  const [kind, setKind] = useState(initialKind);   // fixed | float
  const [form, setForm] = useState({
    coupon: "", base: "KEYRATE", spread: "", freq: "4", maturity: "",
    price: "100", face: "1000", issuer: "", rating: "",
  });
  const [res, setRes] = useState(null);       // { kind, ...ответ бэка }
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState(initialKind === "float" ? "yidx" : "ytm"); // ось Y сравнения

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const isFloat = kind === "float";
  const switchKind = (k) => {
    setKind(k); setRes(null); setErr("");
    setMode(k === "float" ? "yidx" : "ytm");
  };

  // универс сравнения: фиксы или флоатеры; из него же эмитенты формы
  const fixedQ = useQuery({ queryKey: ["fixed"], queryFn: fetchFixed,
    staleTime: 60000, enabled: !isFloat });
  const floatQ = useQuery({ queryKey: ["calc-floaters"],
    queryFn: ({ signal }) => fetchBonds({ universe: true, signal }),
    staleTime: 60000, enabled: isFloat });
  const uniQ = isFloat ? floatQ : fixedQ;
  const all = uniQ.data?.items || [];
  const issuerOf = isFloat ? (b) => b.emitter_name : (b) => b.issuer;

  const issuers = useMemo(() => {
    const s = new Set();
    for (const b of all) { const n = issuerOf(b); if (n) s.add(n); }
    return [...s].sort((a, b) => a.localeCompare(b));
  }, [all, isFloat]);

  // пиры для графика: тот же рейтинг (если выбран) ∪ тот же эмитент
  const peers = useMemo(() => {
    if (!form.rating && !form.issuer) return all;
    return all.filter((b) =>
      (form.rating && norm(b.rating) === form.rating) || (form.issuer && issuerOf(b) === form.issuer));
  }, [all, form.rating, form.issuer, isFloat]);
  const sameIssuer = useMemo(
    () => (form.issuer ? all.filter((b) => issuerOf(b) === form.issuer) : []),
    [all, form.issuer, isFloat]);

  const canCalc = (isFloat ? form.spread !== "" : form.coupon !== "")
    && form.maturity && form.price !== "";
  const onCalc = async (e) => {
    e?.preventDefault();
    if (!canCalc || busy) return;
    setBusy(true); setErr("");
    try {
      const common = { freq: form.freq, maturity: form.maturity,
        price: form.price, face: form.face || 1000 };
      const r = isFloat
        ? await calcCustomFloater({ base: form.base, spread: form.spread, ...common })
        : await calcCustomBond({ coupon: form.coupon, ...common });
      setRes({ kind, ...r });
    } catch (ex) {
      setRes(null);
      setErr(ex.message || "ошибка расчёта");
    } finally {
      setBusy(false);
    }
  };

  // метрики показываем только если посчитаны для ТЕКУЩЕГО типа
  const m = res?.kind === kind ? res.metrics : null;
  // точка «своей» бумаги на скэттере: x-координата по типу
  const customPt = m ? { ...m, _x: isFloat ? yrsTo(form.maturity) : m.mod_dur } : null;
  const modes = AXES[kind].modes;
  return (
    <div className="calc-page">
      <form className="an-card calc-form" onSubmit={onCalc}>
        <div className="an-title">ПАРАМЕТРЫ ОБЛИГАЦИИ
          <span className="an-hint">{isFloat ? "спред в bps, цена в %" : "купон и цена — в %"} · расчёт на дату {res?.kind === kind ? fmt.date(res.calc_date) : "последних торгов"}</span>
          <span className="an-toggle" role="group" aria-label="тип купона">
            {[["fixed", "Фикс"], ["float", "Флоатер"]].map(([v, l]) => (
              <button key={v} type="button" className={"an-tgl-btn" + (kind === v ? " on" : "")}
                aria-pressed={kind === v} onClick={() => switchKind(v)}>{l}</button>
            ))}
          </span>
        </div>
        <div className="calc-grid">
          {isFloat ? (
            <>
              <label className="calc-f">
                <span className="calc-k">База</span>
                <select value={form.base} onChange={set("base")}>
                  <option value="KEYRATE">КС (KEYRATE)</option>
                  <option value="RUONIA">RUONIA</option>
                </select>
              </label>
              <label className="calc-f">
                <span className="calc-k">Спред к базе, bps</span>
                <input type="number" step="1" min="-1000" max="10000" required
                  value={form.spread} onChange={set("spread")} placeholder="250" />
              </label>
            </>
          ) : (
            <label className="calc-f">
              <span className="calc-k">Купон, % годовых</span>
              <input type="number" step="0.01" min="0" max="100" required
                value={form.coupon} onChange={set("coupon")} placeholder="14.5" />
            </label>
          )}
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
        {res?.kind === kind && res.warnings?.length > 0 && (
          <div className="calc-err">{res.warnings.join(" · ")}</div>
        )}
      </form>

      {m && (
        <div className="an-card">
          <div className="an-title">МЕТРИКИ</div>
          <div className="calc-metrics">
            {METRICS[kind].map(([k, label, unit, f, style]) => (
              <div className="calc-m" key={k}>
                <span className="kpi-label">{label}</span>
                <span className="calc-m-v" style={style ? style(m[k]) : undefined}>
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
              : `весь универс ${isFloat ? "флоатеров" : "фиксов"} — задайте эмитента/рейтинг для фокуса`}
            {" · ромб = ваша облигация"}
          </span>
          <span className="an-toggle" role="group" aria-label="ось Y">
            {Object.entries(modes).map(([v, mm]) => (
              <button key={v} type="button" className={"an-tgl-btn" + (mode === v ? " on" : "")}
                aria-pressed={mode === v} onClick={() => setMode(v)}>{mm.label}</button>
            ))}
          </span>
        </div>
        {uniQ.isLoading
          ? <div className="an-empty">загрузка универса…</div>
          : <CompareScatter peers={peers} custom={customPt} kind={kind} mode={mode}
              issuer={form.issuer} issuerOf={issuerOf} />}
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
          <div className="an-title">ВЫПУСКИ ЭМИТЕНТА <span className="an-hint">{form.issuer} · {sameIssuer.length} шт</span></div>
          <div className="fx-table-wrap calc-table-wrap">
            <table className="grid packed">
              <thead>
                <tr>
                  <th className="left">ВЫПУСК</th><th className="num">ЦЕНА</th>
                  {isFloat
                    ? <><th className="num">R-spread</th><th className="num">SM</th></>
                    : <><th className="num">YTM</th><th className="num">G-СПРЕД</th><th className="num">ДЮР</th><th className="num">КУПОН</th></>}
                  <th className="num">ПОГАШЕНИЕ</th><th className="num">РЕЙТИНГ</th>
                </tr>
              </thead>
              <tbody>
                {sameIssuer.slice().sort((a, b) => (a.maturity_date || "9") < (b.maturity_date || "9") ? -1 : 1).map((b) => (
                  <tr key={b.isin}>
                    <td className="left">{b.name}</td>
                    <td className="num">{fmt.pct(b.last_price_pct) ?? <D />}</td>
                    {isFloat ? (
                      <>
                        <td className="num" style={spreadStyle(b.yield_over_index_bps)}>
                          {b.yield_over_index_bps == null ? <D /> : fmt.bps(b.yield_over_index_bps)}</td>
                        <td className="num" style={spreadStyle(b.dm_bps)}>
                          {b.dm_bps == null ? <D /> : fmt.bps(b.dm_bps)}</td>
                      </>
                    ) : (
                      <>
                        <td className="num">{b.ytm == null ? <D /> : fmt.pct(b.ytm)}</td>
                        <td className="num" style={spreadStyle(b.g_spread_bps)}>
                          {b.g_spread_bps == null ? <D /> : fmt.bps(b.g_spread_bps)}</td>
                        <td className="num">{b.mod_dur == null ? <D /> : fmt.num(b.mod_dur, 2)}</td>
                        <td className="num">{b.coupon_pct == null ? <D /> : fmt.pct(b.coupon_pct)}</td>
                      </>
                    )}
                    <td className="num">{b.maturity_date ? fmt.date(b.maturity_date) : <D />}</td>
                    <td className="num">{b.rating
                      ? <span className="fx-rt" style={{ color: RTCOLOR[norm(b.rating)] }}>{b.rating}</span> : <D />}</td>
                  </tr>
                ))}
                {m && (
                  <tr className="calc-row-mine">
                    <td className="left">Ваша облигация</td>
                    <td className="num">{fmt.pct(Number(form.price))}</td>
                    {isFloat ? (
                      <>
                        <td className="num" style={spreadStyle(m.y_idx_bps)}>
                          {m.y_idx_bps == null ? <D /> : fmt.bps(m.y_idx_bps)}</td>
                        <td className="num" style={spreadStyle(m.sm_bps)}>
                          {m.sm_bps == null ? <D /> : fmt.bps(m.sm_bps)}</td>
                      </>
                    ) : (
                      <>
                        <td className="num">{m.ytm_pct == null ? <D /> : fmt.pct(m.ytm_pct)}</td>
                        <td className="num" style={spreadStyle(m.g_spread_bps)}>
                          {m.g_spread_bps == null ? <D /> : fmt.bps(m.g_spread_bps)}</td>
                        <td className="num">{m.mod_dur == null ? <D /> : fmt.num(m.mod_dur, 2)}</td>
                        <td className="num">{form.coupon === "" ? <D /> : fmt.pct(Number(form.coupon))}</td>
                      </>
                    )}
                    <td className="num">{fmt.date(form.maturity)}</td>
                    <td className="num">{form.rating || <D />}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {res?.kind === kind && res.cashflow?.length > 0 && (
        <div className="an-card">
          <div className="an-title">ПОТОК ПЛАТЕЖЕЙ
            <span className="an-hint">будущие выплаты на одну бумагу{isFloat ? " · купоны по форвардной кривой базы" : ""}</span>
          </div>
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
