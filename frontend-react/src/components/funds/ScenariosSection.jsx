import { useEffect, useState } from "react";
import { fmt, DASH } from "../../format.js";
import { fetchFundScenarios, fetchFundAlerts, UnauthorizedError } from "../../api.js";
import { conv } from "./ccy.js";

const sgnCls = (v) => (v == null ? "" : v >= 0 ? "pos" : "neg");
const mln = (v, rate) => (v == null ? DASH : fmt.signed(conv(v, rate) / 1e6, 2));

// Сценарный анализ: КС ±100/±200, наклон кривой, FX ±10% → ΔNAV (MtM) и Δcarry.
// Плюс repricing-алерты D/D (Δz/Δdm из истории НРД) по бумагам фонда.
export default function ScenariosSection({ code, refreshKey, rate, sign, onLogout }) {
  const [data, setData] = useState(null);
  const [alerts, setAlerts] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    setErr("");
    Promise.all([fetchFundScenarios(code), fetchFundAlerts(code)])
      .then(([sc, al]) => { if (alive) { setData(sc); setAlerts(al); } })
      .catch((e) => {
        if (e instanceof UnauthorizedError) { onLogout(); return; }
        if (alive) setErr(e.message);
      });
    return () => { alive = false; };
  }, [code, refreshKey, onLogout]);

  const items = data?.items || [];
  return (
    <div className="admin-sec fund-scenarios">
      <div className="fund-sec-title">Сценарии · ΔNAV (MtM) и Δcarry</div>
      {err && <div className="admin-err">{err}</div>}
      {!data && !err && <div className="muted" style={{ fontSize: 12 }}>загрузка…</div>}
      {items.length > 0 && (
        <table className="admin-table scen-table">
          <thead>
            <tr>
              <th className="left">Сценарий</th>
              <th>ΔNAV, млн {sign}</th>
              <th>ΔNAV, % капитала</th>
              <th>Δcarry, млн {sign}/год</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr key={it.key}>
                <td className="left">{it.name}</td>
                <td className={"num " + sgnCls(it.dnav_rub)}>{mln(it.dnav_rub, rate)}</td>
                <td className={"num " + sgnCls(it.dnav_pct_capital)}>
                  {it.dnav_pct_capital != null ? fmt.signed(it.dnav_pct_capital) : DASH}</td>
                <td className={"num " + sgnCls(it.dcarry_rub)}>{mln(it.dcarry_rub, rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {alerts?.items?.length > 0 && (
        <div className="fund-alerts">
          <span className="warn-badge">REPRICING D/D</span>
          {alerts.items.map((a) => (
            <span key={a.isin} className="mono" style={{ fontSize: 11 }}>
              {a.isin}{a.dz_bps != null ? ` Δz ${fmt.bps(a.dz_bps)}` : ""}{a.ddm_bps != null ? ` Δdm ${fmt.bps(a.ddm_bps)}` : ""}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
