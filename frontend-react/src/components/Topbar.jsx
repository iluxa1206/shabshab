import { useEffect } from "react";
import { IconGear } from "./icons.jsx";
import { NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchNewIssues } from "../api.js";

// Тип облигаций (первая кнопка меню) + суб-навигация под выбранный тип
const TYPES = [
  { id: "floaters", label: "Флоатеры", home: "/floaters" },
  { id: "fixed", label: "Фиксы", home: "/fixed" },
  { id: "portfolio", label: "Портфель", home: "/portfolio" },
  { id: "curves", label: "Кривые", home: "/curves" },
  // «Справочник» (правка параметров реестра + импорт xlsx) — только админам
  { id: "reference", label: "Справочник", home: "/reference", admin: true },
  { id: "status", label: "Статус", home: "/status" },
];
const SUBNAV = {
  floaters: [["/floaters", "Монитор"], ["/compare", "Сравнение"],
             ["/trades", "Сделки"], ["/signals", "Сигналы"], ["/payments", "Выплаты"],
             ["/primary", "Первичка"], ["/calc/float", "Калькулятор"]],
  fixed: [["/fixed", "Монитор"], ["/primary", "Первичка"], ["/calc", "Калькулятор"]],
  portfolio: [],
  curves: [],
  reference: [],
  status: [],
};
// Пути, живущие СРАЗУ В ДВУХ разделах: анонс первички не знает своего класса
// (в одной выгрузке и флоатеры, и фиксы), поэтому /primary висит в обоих меню.
const SHARED_PATHS = ["/primary"];
const TYPE_KEY = "desk.lastType";

const typeFromPath = (p) =>
  p.startsWith("/portfolio") ? "portfolio"
    : p.startsWith("/calc/float") ? "floaters"
    : p.startsWith("/fixed") || p.startsWith("/calc") ? "fixed"
    : p.startsWith("/curves") ? "curves" : p.startsWith("/reference") ? "reference"
    : p.startsWith("/status") ? "status" : "floaters";

// Раздел по пути. На ОБЩЕМ пути тип не выводится из URL — держим последний
// явно выбранный, иначе клик «Первичка» из Фиксов перекидывал бы верхнее меню
// на Флоатеры. sessionStorage — чтобы перезагрузка прямо на /primary тоже
// возвращала в свой раздел (private mode может кидать — тогда дефолт).
function useCurrentType(pathname) {
  const shared = SHARED_PATHS.some((s) => pathname.startsWith(s));
  const resolved = shared ? null : typeFromPath(pathname);
  useEffect(() => {
    if (resolved === "floaters" || resolved === "fixed") {
      try { sessionStorage.setItem(TYPE_KEY, resolved); } catch { /* private mode */ }
    }
  }, [resolved]);
  if (!shared) return resolved;
  try {
    return sessionStorage.getItem(TYPE_KEY) === "fixed" ? "fixed" : "floaters";
  } catch {
    return "floaters";
  }
}

function TypeMenu({ type, isAdmin, features }) {
  // Выключенный слой (services/feature_flags → /api/meta.features) исчезает из
  // меню целиком: пустая витрина хуже отсутствующей. Флага нет — считаем, что
  // слой включён, иначе старый бэк прятал бы рабочие вкладки.
  const items = TYPES.filter((t) => (!t.admin || isAdmin)
    && (t.id !== "fixed" || features?.fixed !== false));
  const cur = items.find((t) => t.id === type) || items[0];
  // счётчик свежих выпусков без подтверждённых параметров: значок висит на самой
  // кнопке меню, чтобы «надо чекнуть новые» было видно с любой вкладки
  const nq = useQuery({
    queryKey: ["admin", "new-issues"],
    queryFn: fetchNewIssues,
    enabled: isAdmin,
    refetchInterval: 10 * 60 * 1000,
    staleTime: 5 * 60 * 1000,
  });
  const n = isAdmin ? (nq.data?.n || 0) : 0;
  const nTitle = `${n} новых выпусков без подтверждённых параметров — Справочник`;
  return (
    <div className="type-menu">
      <button type="button" className="seg-btn type-btn" aria-haspopup="true">
        {cur.label} ▾
        {n > 0 && <span className="nav-badge" title={nTitle}>{n}</span>}
      </button>
      <div className="type-drop" role="menu">
        {items.map((t) => (
          <NavLink key={t.id} to={t.home} role="menuitem"
            className={"type-opt" + (t.id === type ? " on" : "")}>
            {t.label}
            {t.id === "reference" && n > 0 && <span className="nav-badge" title={nTitle}>{n}</span>}
          </NavLink>
        ))}
      </div>
    </div>
  );
}

const tabCls = ({ isActive }) => "seg-btn" + (isActive ? " active" : "");

// extra — слот для инструментов раздела в самой верхней панели (сейчас там живёт
// «Аналитика» флоатеров): панель фильтров ниже — про отбор строк, а окна поверх
// таблицы — отдельный функционал, и им место в шапке рядом с навигацией.
export default function Topbar({ user, onLogout, onOpenSettings, extra, features }) {
  const type = useCurrentType(useLocation().pathname);
  const sub = SUBNAV[type] || [];
  return (
    <header className="menubar">
      <div className="brand-row">
        <span className="wordmark">DESK</span>
        <TypeMenu type={type} isAdmin={user?.role === "admin"} features={features} />
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
