import { IconGear } from "./icons.jsx";
import { NavLink, useLocation } from "react-router-dom";

// Тип облигаций (первая кнопка меню) + суб-навигация под выбранный тип
const TYPES = [
  { id: "floaters", label: "Флоатеры", home: "/floaters" },
  { id: "fixed", label: "Фиксы", home: "/fixed" },
  { id: "euro", label: "Евробонды", home: "/euro" },
  { id: "status", label: "Статус", home: "/status" },
];
const SUBNAV = {
  floaters: [["/floaters", "Список"], ["/issuers", "Эмитенты"], ["/trades", "Сделки"],
             ["/blocks", "Крупные"], ["/signals", "Сигналы"], ["/payments", "Выплаты"],
             ["/curves", "Кривые"], ["/calc/float", "Калькулятор"]],
  fixed: [["/fixed", "Список"], ["/calc", "Калькулятор"]],
  euro: [],
  status: [],
};
const currentType = (p) =>
  p.startsWith("/calc/float") ? "floaters"
    : p.startsWith("/fixed") || p.startsWith("/calc") ? "fixed" : p.startsWith("/euro") ? "euro"
    : p.startsWith("/status") ? "status" : "floaters";

function TypeMenu({ type }) {
  const cur = TYPES.find((t) => t.id === type) || TYPES[0];
  return (
    <div className="type-menu">
      <button type="button" className="seg-btn type-btn" aria-haspopup="true">{cur.label} ▾</button>
      <div className="type-drop" role="menu">
        {TYPES.map((t) => (
          <NavLink key={t.id} to={t.home} role="menuitem"
            className={"type-opt" + (t.id === type ? " on" : "")}>{t.label}</NavLink>
        ))}
      </div>
    </div>
  );
}

const tabCls = ({ isActive }) => "seg-btn" + (isActive ? " active" : "");

// extra — слот для инструментов раздела в самой верхней панели (сейчас там живёт
// «Аналитика» флоатеров): панель фильтров ниже — про отбор строк, а окна поверх
// таблицы — отдельный функционал, и им место в шапке рядом с навигацией.
export default function Topbar({ user, onLogout, onOpenSettings, extra }) {
  const type = currentType(useLocation().pathname);
  let sub = SUBNAV[type] || [];
  // «Справочник» (правка параметров реестра + импорт xlsx) — только админам
  if (type === "floaters" && user?.role === "admin") {
    sub = [...sub, ["/reference", "Справочник"]];
  }
  return (
    <header className="menubar">
      <div className="brand-row">
        <span className="wordmark">DESK</span>
        <TypeMenu type={type} />
        {sub.length > 0 && (
          <span className="seg module-seg" role="tablist" aria-label="Раздел">
            {sub.map(([to, label]) => (
              <NavLink key={to} className={tabCls} to={to} end>{label}</NavLink>
            ))}
          </span>
        )}
        {extra && <span className="menubar-tools">{extra}</span>}
      </div>
      <div className="topbar-right">
        {user && (
          <button className="btn" onClick={onOpenSettings} title="Настройки доступа">
            {user.email}{user.role === "admin" && <> <IconGear size={11} /></>}
          </button>
        )}
        {onLogout && <button className="btn" onClick={onLogout}>Выход</button>}
      </div>
    </header>
  );
}
