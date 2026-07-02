import { useCallback, useEffect, useState } from "react";
import { fmt, DASH } from "../../format.js";
import { fetchFundSummary, fetchFundRepos, patchFund, deleteFund, UnauthorizedError } from "../../api.js";
import FundPositionsTable from "./FundPositionsTable.jsx";
import RepoSection from "./RepoSection.jsx";
import SnapshotModal from "./SnapshotModal.jsx";
import FundCashflow from "./FundCashflow.jsx";
import ScenariosSection from "./ScenariosSection.jsx";
import { CcySwitch } from "./FundCards.jsx";
import { CCY_SIGN, dispRate, mlnCcy, numCcy, conv } from "./ccy.js";

// Экспорт позиций в CSV (Excel-RU: разделитель ;, десятичная запятая)
function exportPositionsCsv(s) {
  const cols = ["isin", "name", "label", "ccy", "qty", "price_pct", "mv_rub", "ytm_pct",
    "dm_bps", "z_bps", "g_spread_bps", "carry_bps", "mod_dur", "dv01_rub", "maturity"];
  const head = ["ISIN", "Имя", "Класс", "Валюта", "Кол-во", "Цена %", "MV ₽", "YTM %",
    "DM bps", "Z bps", "G-spread bps", "Carry bps", "Dur mod", "DV01 ₽", "Погашение"];
  const esc = (v) => {
    if (v == null) return "";
    if (typeof v === "number") return String(v).replace(".", ",");
    const t = String(v);
    return /[;"\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  };
  const lines = [head.join(";")];
  for (const p of s.positions || []) lines.push(cols.map((c) => esc(p[c])).join(";"));
  // BOM — иначе Excel открывает UTF-8 кракозябрами
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${s.code}_positions_${s.calc_date}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function Kpi({ label, value, sub, cls }) {
  return (
    <div className="kpi">
      <span className="kpi-label">{label}</span>
      <span className={"kpi-val sm" + (cls ? " " + cls : "")}>{value}</span>
      {sub && <span className="kpi-sub">{sub}</span>}
    </div>
  );
}

// Капитал: клик → инлайн-редактор → PATCH. Хранится и ВВОДИТСЯ в ₽,
// отображается в валюте отображения.
function CapitalKpi({ s, rate, sign, onSave }) {
  const [edit, setEdit] = useState(false);
  const [val, setVal] = useState("");
  const start = () => { setVal(s.capital_rub != null ? String(s.capital_rub) : ""); setEdit(true); };
  const save = async () => {
    const num = parseFloat(val.replace(/\s/g, "").replace(",", "."));
    await onSave(Number.isFinite(num) && num > 0 ? num : null);
    setEdit(false);
  };
  if (edit) {
    return (
      <div className="kpi">
        <span className="kpi-label">Капитал · ввод в ₽</span>
        <input
          className="add-input" style={{ width: "100%" }} autoFocus value={val}
          placeholder="₽ (пусто = derived)"
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setEdit(false); }}
          onBlur={save}
        />
      </div>
    );
  }
  return (
    <div className="kpi kpi-click" onClick={start} title="Клик — задать капитал фонда (в ₽)">
      <span className="kpi-label">Капитал</span>
      <span className="kpi-val sm">{mlnCcy(s.capital_rub, rate, sign)}</span>
      <span className="kpi-sub">{s.capital_is_derived ? "= NAV (derived)" : "задан вручную"}</span>
    </div>
  );
}

// Баланс процентного риска + валютная экспозиция — главный риск-взгляд хедж-фонда
function RiskStrip({ s, rate, sign }) {
  const rs = s.risk_split;
  if (!rs) return null;
  const mln = (v) => fmt.num(conv(v, rate) / 1e6, 1);
  const fxs = (s.fx_split || []).filter((x) => x.ccy !== "RUB");
  return (
    <div className="risk-strip muted">
      <span>RATE RISK: фиксы DV01 <b className="fg">{fmt.num(conv(rs.rub_fixed_dv01_rub, rate), 0)} {sign}</b>
        {" "}(MV {mln(rs.fixed_mv_rub)} млн)</span>
      <span>· флоатеры MV <b className="fg">{mln(rs.float_mv_rub)} млн</b> (rate dur ≈ 0, риск в carry)</span>
      {rs.fx_mv_rub > 0 && (
        <span>· валютные MV <b className="fg">{mln(rs.fx_mv_rub)} млн</b> DV01 {fmt.num(conv(rs.fx_dv01_rub, rate), 0)} {sign}</span>
      )}
      {fxs.map((x) => (
        <span key={x.ccy}>· FX {x.ccy}: нетто <b className="fg">{mln(x.net_rub)} млн</b> ({fmt.pct(x.share_pct, 1)}% MV)</span>
      ))}
    </div>
  );
}

function ClassBreakdown({ classes, rate, sign, clsFilter, onToggle }) {
  if (!classes.length) return null;
  return (
    <div className="fund-classes">
      <table className="admin-table">
        <thead>
          <tr>
            <th className="left">Класс</th><th>MV, млн {sign}</th><th>× капитала</th><th>n</th>
            <th>YTM %</th><th>DM bps</th><th>Z bps</th><th>Dur</th><th>DV01 {sign}</th>
          </tr>
        </thead>
        <tbody>
          {classes.map((c) => (
            <tr key={c.cls}
              className={clsFilter.includes(c.cls) ? "row-on" : ""}
              onClick={() => onToggle(c.cls)}
              title="Клик — фильтр позиций по классу">
              <td className="left">{c.label}</td>
              <td className="num">{fmt.num(conv(c.mv_rub, rate) / 1e6, 2)}</td>
              <td className="num">{c.x_capital != null ? fmt.num(c.x_capital, 2) + "×" : DASH}</td>
              <td className="num">{c.n}</td>
              <td className="num">{fmt.pct(c.ytm_pct) ?? DASH}</td>
              <td className="num">{fmt.bps(c.dm_bps) ?? DASH}</td>
              <td className="num">{fmt.bps(c.z_bps) ?? DASH}</td>
              <td className="num">{fmt.num(c.mod_dur) ?? DASH}</td>
              <td className="num">{c.dv01_rub != null ? fmt.num(conv(c.dv01_rub, rate), 0) : DASH}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function FundDetail({ code, dispCcy, onSetCcy, onBack, onLogout }) {
  const [summary, setSummary] = useState(null);
  const [repos, setRepos] = useState([]);
  const [status, setStatus] = useState("loading");
  const [errMsg, setErrMsg] = useState("");
  const [sort, setSort] = useState({ key: "mv_rub", dir: "desc" });
  const [clsFilter, setClsFilter] = useState([]);
  const [showSnapshot, setShowSnapshot] = useState(false);

  const reload = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([fetchFundSummary(code), fetchFundRepos(code)]);
      setSummary(s); setRepos(r);
      setStatus("ready");
    } catch (e) {
      if (e instanceof UnauthorizedError) { onLogout(); return; }
      setErrMsg(e.message); setStatus("error");
    }
  }, [code, onLogout]);

  useEffect(() => { setStatus("loading"); reload(); }, [reload]);

  const saveCapital = async (capital) => {
    try { await patchFund(code, { capital }); await reload(); }
    catch (e) { if (e instanceof UnauthorizedError) onLogout(); else alert(e.message); }
  };

  const removeFund = async () => {
    if (!confirm(`Удалить фонд ${code} со всеми позициями и РЕПО?`)) return;
    try { await deleteFund(code); onBack(); }
    catch (e) { if (e instanceof UnauthorizedError) onLogout(); else alert(e.message); }
  };

  const onSort = useCallback((key) => {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }));
  }, []);

  if (status === "loading" && !summary) return <div className="funds-state muted">Загрузка {code}…</div>;
  if (status === "error" && !summary) {
    return (
      <div className="funds-state">
        <div className="admin-err">{errMsg}</div>
        <button className="btn" onClick={reload}>Повторить</button>
        <button className="btn" onClick={onBack}>← Фонды</button>
      </div>
    );
  }
  const s = summary;
  const rate = dispRate(dispCcy, s.fx) ?? 1;
  const sign = rate === 1 && dispCcy !== "RUB" ? CCY_SIGN.RUB : CCY_SIGN[dispCcy];
  const carryCls = s.net_carry_rub == null ? "" : s.net_carry_rub >= 0 ? "pos" : "neg";

  return (
    <>
      <div className="fund-head">
        <button className="btn" onClick={onBack}>← Фонды</button>
        <span className="fund-code">{s.code}</span>
        <span className="fund-name-inline">{s.name}</span>
        <span className="badge">{s.base_ccy}</span>
        <span className="muted" style={{ fontSize: 12 }}>
          {s.snap_date ? "снапшот " + fmt.date(s.snap_date) : "снапшот не загружен"} · расчёт {fmt.date(s.calc_date)}
        </span>
        <CcySwitch dispCcy={dispCcy} onSetCcy={onSetCcy} rates={s.fx} fxLabel={s.fx_label} />
        <span style={{ flex: 1 }} />
        <button className="btn" onClick={reload}>Sync</button>
        <button className="btn" onClick={() => setShowSnapshot(true)}>Снапшот CSV</button>
        <button className="btn" onClick={() => exportPositionsCsv(s)}
          disabled={!s.positions?.length} title="Скачать позиции с метриками">Экспорт</button>
        <button className="btn btn-danger" onClick={removeFund}>Удалить</button>
      </div>

      <div className="kpis kpis-funds">
        <Kpi label="NAV" value={mlnCcy(s.nav_rub, rate, sign)} />
        <Kpi label="MV Gross" value={mlnCcy(s.mv_rub, rate, sign)} sub={s.n_positions + " позиций"} />
        <Kpi label="Leverage" value={s.leverage_gross != null ? fmt.num(s.leverage_gross, 2) + "×" : DASH} />
        <Kpi label="DV01" value={numCcy(s.dv01_rub, rate, sign)} sub="на 1 бп" />
        <Kpi label="Net Carry" cls={carryCls} value={mlnCcy(s.net_carry_rub, rate, sign)}
          sub={s.net_carry_pct_capital != null ? fmt.signed(s.net_carry_pct_capital) + "% капитала / год" : "годовой"} />
        <Kpi label="Funding" value={mlnCcy(s.repo_rub, rate, sign)}
          sub={s.repo_rate_wa != null ? "WA " + fmt.pct(s.repo_rate_wa) + "% · " + mlnCcy(s.funding_cost_rub, rate, sign) + "/год" : "нет РЕПО"} />
        <CapitalKpi s={s} rate={rate} sign={sign} onSave={saveCapital} />
      </div>

      <RiskStrip s={s} rate={rate} sign={sign} />

      <ClassBreakdown
        classes={s.classes || []}
        rate={rate} sign={sign}
        clsFilter={clsFilter}
        onToggle={(c) => setClsFilter((arr) => (arr.includes(c) ? arr.filter((x) => x !== c) : [...arr, c]))}
      />

      <FundPositionsTable
        code={code}
        positions={s.positions || []}
        rate={rate} sign={sign}
        clsFilter={clsFilter}
        sort={sort}
        onSort={onSort}
        onChanged={reload}
        onLogout={onLogout}
      />

      {(s.positions || []).length > 0 && (
        <ScenariosSection code={code} refreshKey={s.snap_date + ":" + s.n_positions}
          rate={rate} sign={sign} onLogout={onLogout} />
      )}

      {(s.positions || []).length > 0 && (
        <FundCashflow code={code} refreshKey={s.snap_date + ":" + s.n_positions}
          rate={rate} sign={sign} onLogout={onLogout} />
      )}

      <RepoSection code={code} repos={repos} onChanged={reload} onLogout={onLogout} />

      {showSnapshot && (
        <SnapshotModal
          code={code}
          onClose={() => setShowSnapshot(false)}
          onDone={() => { setShowSnapshot(false); reload(); }}
          onLogout={onLogout}
        />
      )}
    </>
  );
}
