import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { fmt } from "../format.js";

function Clock() {
  const [t, setT] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setT(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  const p = (n) => String(n).padStart(2, "0");
  const on = t.getSeconds() % 2 === 0;
  const sep = <span style={{ opacity: on ? 1 : 0.2 }}>:</span>;
  return (
    <span className="meta-chip" aria-label="Время">
      <span className="meta-v">{p(t.getHours())}{sep}{p(t.getMinutes())}{sep}{p(t.getSeconds())}</span>
    </span>
  );
}

const tabCls = ({ isActive }) => "seg-btn" + (isActive ? " active" : "");

export default function Topbar({ meta, live, onRefresh, theme, onToggleTheme, user, onLogout, onOpenSettings }) {
  return (
    <header className="menubar">
      <div className="brand-row">
        <span className="wordmark">DESK</span>
        <span className="seg module-seg" role="tablist" aria-label="Модуль">
          <NavLink className={tabCls} to="/floaters">Флоатеры</NavLink>
          <NavLink className={tabCls} to="/funds">Фонды</NavLink>
          <NavLink className={tabCls} to="/curves">Кривые</NavLink>
        </span>
      </div>
      <div className="topbar-right">
        <span className="meta-chip">
          <span className="meta-k">CALC</span><span className="meta-v">{fmt.date(meta.calc_date) || "—"}</span>
          <span className="meta-sep">/</span>
          <span className="meta-k">RATES</span><span className="meta-v">{fmt.date(meta.rates_date) || "—"}</span>
        </span>
        <Clock />
        <span className={"live " + (live ? "live-on" : "live-off")}>
          <span className="dot" />{live ? "LIVE" : "OFFLINE"}
        </span>
        <button className="btn" onClick={onToggleTheme}>{theme === "dark" ? "Light" : "Dark"}</button>
        <button className="btn" onClick={onRefresh}>Sync</button>
        {user && (
          <button className="btn" onClick={onOpenSettings} title="Настройки доступа">
            {user.email}{user.role === "admin" ? " ⚙" : ""}
          </button>
        )}
        {onLogout && <button className="btn" onClick={onLogout}>Выход</button>}
      </div>
    </header>
  );
}
