import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fmt, DASH } from "../../format.js";
import { createFund, UnauthorizedError } from "../../api.js";
import { CCYS, CCY_SIGN, dispRate, mlnCcy, numCcy } from "./ccy.js";
import { CompareTable, BenchmarksRow, CalendarSection } from "./FundsOverviewExtras.jsx";

function Metric({ label, value, cls }) {
  return (
    <div className="fund-metric">
      <span className="kpi-label">{label}</span>
      <span className={"fund-metric-val" + (cls ? " " + cls : "")}>{value}</span>
    </div>
  );
}

function FundCard({ f, rate, sign, onSelect }) {
  const carryCls = f.net_carry_rub == null ? "" : f.net_carry_rub >= 0 ? "pos" : "neg";
  // NAV в базовой валюте фонда (D5→$, Y5→¥) — родная величина пая
  const baseRate = f.base_ccy !== "RUB" ? f.fx?.[f.base_ccy] : null;
  const navBase = baseRate ? mlnCcy(f.nav_rub, baseRate, CCY_SIGN[f.base_ccy]) : null;
  return (
    <div
      className="fund-card"
      role="button" tabIndex={0}
      onClick={() => onSelect(f.code)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(f.code); } }}
    >
      <div className="fund-card-head">
        <span className="fund-code">{f.code}</span>
        <span className="badge">{f.base_ccy}</span>
      </div>
      <div className="fund-name">{f.name}</div>
      <div className="fund-metrics">
        <Metric label={"NAV" + (navBase ? " · ≈" + navBase : "")} value={mlnCcy(f.nav_rub, rate, sign)} />
        <Metric label="MV Gross" value={mlnCcy(f.mv_rub, rate, sign)} />
        <Metric label="Leverage" value={f.leverage_gross != null ? fmt.num(f.leverage_gross, 2) + "×" : DASH} />
        <Metric label="DV01" value={f.dv01_rub != null ? numCcy(f.dv01_rub, rate, sign) : DASH} />
        <Metric label="Net Carry" cls={carryCls} value={mlnCcy(f.net_carry_rub, rate, sign)} />
        <Metric label="Funding" value={f.repo_rate_wa != null ? mlnCcy(f.repo_rub, rate, sign) + " @ " + fmt.pct(f.repo_rate_wa) + "%" : DASH} />
      </div>
      <div className="fund-card-foot muted">
        {f.n_positions} поз. {f.snap_date ? "· снапшот " + fmt.date(f.snap_date) : "· снапшот не загружен"}
        {f.n_no_price > 0 && <span className="warn-badge" style={{ marginLeft: 8 }}>{f.n_no_price} без цены</span>}
      </div>
    </div>
  );
}

// Тумблер валюты отображения: пересчёт по TOM-курсу из summary (rates)
export function CcySwitch({ dispCcy, onSetCcy, rates, fxLabel }) {
  return (
    <span className="ccy-switch">
      <span className="seg">
        {CCYS.map((c) => (
          <button key={c}
            className={"seg-btn" + (dispCcy === c ? " active" : "")}
            disabled={c !== "RUB" && !(rates?.[c] > 0)}
            title={c !== "RUB" && rates?.[c] ? `${c} ${fmt.num(rates[c], 2)}` : c}
            onClick={() => onSetCcy(c)}
          >{CCY_SIGN[c]}</button>
        ))}
      </span>
      {dispCcy !== "RUB" && rates?.[dispCcy] && (
        <span className="muted mono" style={{ fontSize: 11 }}>
          {dispCcy} {fmt.num(rates[dispCcy], 2)}{fxLabel ? " · " + fxLabel : ""}
        </span>
      )}
    </span>
  );
}

function CreateForm() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [ccy, setCcy] = useState("RUB");
  const [err, setErr] = useState("");

  const createMut = useMutation({
    mutationFn: () => createFund({ code: code.trim(), name: name.trim() || code.trim(), base_ccy: ccy }),
    onSuccess: () => {
      setOpen(false); setCode(""); setName("");
      qc.invalidateQueries({ queryKey: ["funds"] });
    },
    onError: (e) => { if (!(e instanceof UnauthorizedError)) setErr(e.message); },
  });
  const busy = createMut.isPending;

  const submit = (e) => { e.preventDefault(); setErr(""); createMut.mutate(); };

  if (!open) {
    return (
      <div className="fund-card fund-card-new" role="button" tabIndex={0}
        onClick={() => setOpen(true)}
        onKeyDown={(e) => { if (e.key === "Enter") setOpen(true); }}>
        <span className="fund-new-plus">+ Новый фонд</span>
      </div>
    );
  }
  return (
    <div className="fund-card">
      <form className="admin-form" onSubmit={submit}>
        <input placeholder="КОД (напр. X5)" value={code} maxLength={16}
          onChange={(e) => setCode(e.target.value.toUpperCase())} required autoFocus />
        <input placeholder="Название" value={name} onChange={(e) => setName(e.target.value)} />
        <select value={ccy} onChange={(e) => setCcy(e.target.value)}>
          <option value="RUB">RUB</option><option value="USD">USD</option><option value="CNY">CNY</option>
        </select>
        {err && <div className="admin-err">{err}</div>}
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn" type="submit" disabled={busy || !code.trim()}>Создать</button>
          <button className="btn" type="button" onClick={() => { setOpen(false); setErr(""); }}>Отмена</button>
        </div>
      </form>
    </div>
  );
}

export default function FundCards({ funds, status, errMsg, dispCcy, onSetCcy, onSelect, onReload }) {
  if (status === "loading" && !funds.length) {
    return <div className="funds-state muted">Загрузка фондов…</div>;
  }
  if (status === "error") {
    return (
      <div className="funds-state">
        <div className="admin-err">{errMsg}</div>
        <button className="btn" onClick={onReload}>Повторить</button>
      </div>
    );
  }
  const rates = funds[0]?.fx;
  const rate = dispRate(dispCcy, rates) ?? 1;
  const sign = rate === 1 && dispCcy !== "RUB" ? CCY_SIGN.RUB : CCY_SIGN[dispCcy];
  return (
    <>
      <div className="funds-toolbar">
        <CcySwitch dispCcy={dispCcy} onSetCcy={onSetCcy} rates={rates} fxLabel={funds[0]?.fx_label} />
        <span style={{ flex: 1 }} />
        <button className="btn" onClick={onReload}>Обновить</button>
      </div>
      <div className="fund-cards">
        {funds.map((f) => <FundCard key={f.code} f={f} rate={rate} sign={sign} onSelect={onSelect} />)}
        <CreateForm />
      </div>
      <CompareTable funds={funds} rate={rate} sign={sign} />
      <BenchmarksRow />
      <CalendarSection rate={rate} sign={sign} />
    </>
  );
}
