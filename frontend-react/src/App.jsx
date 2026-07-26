import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes, useNavigate, useSearchParams } from "react-router-dom";
import { fetchBonds, fetchMeta, connectMarketWs, repriceBond, UnauthorizedError, APP_BASENAME } from "./api.js";
import { AuthProvider, queryClient, useAuth } from "./auth.jsx";
import Login from "./components/Login.jsx";
import AdminPanel from "./components/AdminPanel.jsx";
import Topbar from "./components/Topbar.jsx";
import Kpis from "./components/Kpis.jsx";
import Toolbar from "./components/Toolbar.jsx";
import BondTable, { DEFAULT_COLS } from "./components/BondTable.jsx";
import AnalyticsPanel from "./components/AnalyticsPanel.jsx";
import Drawer from "./components/Drawer.jsx";
import StatusBar from "./components/StatusBar.jsx";
import FundsModule from "./components/funds/FundsModule.jsx";
import CurvesModule from "./components/CurvesModule.jsx";
import IssuerAggregates from "./components/IssuerAggregates.jsx";
import { parsePortfolioCsv } from "./portfolio.js";

function Dashboard() {
  const { user, onLogout } = useAuth();
  const [bonds, setBonds] = useState([]);
  const [status, setStatus] = useState("loading"); // loading | ready | error
  const [errMsg, setErrMsg] = useState("");
  const [meta, setMeta] = useState({ calc_date: null, rates_date: null });
  const [live, setLive] = useState(false);

  // три независимые группы фильтров (AND-пересечение; пустая группа = все)
  const [onlyWatch, setOnlyWatch] = useState(false);
  const [basesSel, setBasesSel] = useState([]);    // KEYRATE / RUONIA
  const [ratingsSel, setRatingsSel] = useState([]); // AAA / AA / A / BBB / BELOW / NR
  const [emittersSel, setEmittersSel] = useState([]); // имена эмитентов (мульти)
  const [query, setQuery] = useState("");
  const [showAnalytics, setShowAnalytics] = useState(false);
  const [sort, setSort] = useState({ key: "disc_margin_bps", dir: "asc" });

  // drawer бумаги — в URL (?isin=): deep-link + back закрывает
  const [searchParams, setSearchParams] = useSearchParams();
  const drawerIsin = searchParams.get("isin");
  const [showSettings, setShowSettings] = useState(false);
  const [theme, setTheme] = useState(() => localStorage.getItem("theme") || "light");
  const [watch, setWatch] = useState(() => {
    try { return JSON.parse(localStorage.getItem("watch") || "[]"); } catch { return []; }
  });
  // портфельные позиции {isin: qty} из CSV-импорта
  const [positions, setPositions] = useState(() => {
    try { return JSON.parse(localStorage.getItem("positions") || "{}"); } catch { return {}; }
  });
  // видимые столбцы таблицы (persist); дефолт — без портфельных
  const [visibleCols, setVisibleCols] = useState(() => {
    try {
      const s = JSON.parse(localStorage.getItem("cols") || "null");
      return Array.isArray(s) && s.length ? s : DEFAULT_COLS;
    } catch { return DEFAULT_COLS; }
  });
  const lastTriggerRef = useRef(null);

  useEffect(() => { localStorage.setItem("theme", theme); }, [theme]);
  useEffect(() => { localStorage.setItem("watch", JSON.stringify(watch)); }, [watch]);
  useEffect(() => { localStorage.setItem("positions", JSON.stringify(positions)); }, [positions]);
  useEffect(() => { localStorage.setItem("cols", JSON.stringify(visibleCols)); }, [visibleCols]);

  // стабильная ссылка (не inline-стрелка) — иначе memo(BondRow) бесполезен
  const toggleStar = useCallback((isin) =>
    setWatch((w) => (w.includes(isin) ? w.filter((x) => x !== isin) : [...w, isin])), []);

  const toggleCol = useCallback((key) => setVisibleCols((cs) =>
    cs.includes(key) ? cs.filter((k) => k !== key) : [...cs, key]), []);
  const resetCols = useCallback(() => setVisibleCols(DEFAULT_COLS), []);

  // Импорт CSV: добавляем ISIN в watchlist, сохраняем позиции, включаем портфельные колонки.
  const importCsv = useCallback((text) => {
    const { positions: pos, isins, errors } = parsePortfolioCsv(text);
    if (!isins.length) { alert("В файле не найдено строк ISIN,количество" + (errors.length ? "\n" + errors.join("\n") : "")); return; }
    setPositions((prev) => ({ ...prev, ...pos }));
    setWatch((w) => [...w, ...isins.filter((x) => !w.includes(x))]);
    setVisibleCols((cs) => [...new Set([...cs, "qty", "pos_value"])]);
    if (errors.length) alert(`Импортировано ${isins.length}. Пропущено:\n${errors.join("\n")}`);
  }, []);

  const clearPositions = useCallback(() => setPositions({}), []);

  const bondsRef = useRef(bonds);
  bondsRef.current = bonds;
  // WS подписка только на watchlist (весь рынок — 453 бумаги, всех не подписать)
  const subIsinsRef = useRef([]);
  subIsinsRef.current = watch;
  const wsRef = useRef(null);
  const abortRef = useRef(null);
  // весь рынок (universe); watchlist обогащается live-ценой + нашим DM
  const paramsRef = useRef({});
  paramsRef.current = { watch };

  const loadBonds = useCallback(async () => {
    const { watch } = paramsRef.current;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setStatus("loading");
    try {
      const r = await fetchBonds({ universe: true, extra: watch, signal: ctrl.signal });
      const items = r.items || [];
      setBonds(items);
      bondsRef.current = items;
      setStatus("ready");
      wsRef.current?.resubscribe();
    } catch (e) {
      if (e.name === "AbortError") return;
      if (e instanceof UnauthorizedError) { onLogout(); return; }
      setErrMsg(e.message);
      setStatus("error");
    }
  }, [onLogout]);

  useEffect(() => { fetchMeta().then(setMeta).catch(() => {}); }, []);
  useEffect(() => { loadBonds(); }, [loadBonds]);
  // watchlist меняет обогащение (live + наш DM) — перезагружаем (юниверс из кэша, быстро)
  const firstWatch = useRef(true);
  useEffect(() => {
    if (firstWatch.current) { firstWatch.current = false; return; }
    loadBonds();
  }, [watch, loadBonds]);

  // debounced live-пересчёт производных строки под новую цену (WS тикает только
  // цену). Reprice возвращает DM/SM/dirty/Y-IDX/z_model/carry под введённой ценой.
  const repriceTimers = useRef({});
  const scheduleReprice = useCallback((isin, price) => {
    const t = repriceTimers.current;
    if (t[isin]) clearTimeout(t[isin]);
    t[isin] = setTimeout(async () => {
      delete t[isin];
      try {
        const r = await repriceBond(isin, price);
        setBonds((prev) =>
          prev.map((b) => {
            // новее тикнуло за время запроса — этот reprice устарел, не затираем
            if (b.isin !== isin || b.last_price_pct !== price) return b;
            return {
              ...b, _mstale: false,
              dirty_price_rub: r.dirty_price_rub ?? b.dirty_price_rub,
              dm_bps: r.dm_bps ?? b.dm_bps,
              disc_margin_bps: r.disc_margin_bps ?? b.disc_margin_bps,
              z_model_bps: r.z_model_bps ?? b.z_model_bps,
              carry_bps: r.carry_bps ?? b.carry_bps,   // не стираем при транзиентном None
              yield_over_index_bps: r.yield_over_index_bps ?? b.yield_over_index_bps,
              yield_xirr_pct: r.yield_xirr_pct ?? b.yield_xirr_pct,
              index_yield_pct: r.index_yield_pct ?? b.index_yield_pct,
            };
          })
        );
      } catch { /* 401 → глобальный logout; прочее — строка остаётся dim */ }
    }, 500);
  }, []);

  // WS once
  useEffect(() => {
    const ctrl = connectMarketWs(
      () => subIsinsRef.current,
      setLive,
      (isin, price) => {
        if (price == null) return;
        setBonds((prev) =>
          prev.map((b) => {
            if (b.isin !== isin || b.last_price_pct === price) return b;
            // CHG (vs пред. закрытие) пересчитываем СРАЗУ на клиенте: prev_close =
            // last − delta (инвариант дня) → delta_new = price − prev_close.
            let delta = b.delta_to_prev_close;
            if (delta != null && b.last_price_pct != null) {
              const prevClose = b.last_price_pct - delta;
              delta = Math.round((price - prevClose) * 10000) / 10000;
            }
            // DM/SM/z/carry/dirty/Y-IDX — от прошлого расчёта → dim до reprice
            return { ...b, last_price_pct: price, delta_to_prev_close: delta, _mstale: true };
          })
        );
        scheduleReprice(isin, price);
      }
    );
    wsRef.current = ctrl;
    return () => {
      ctrl.close();
      // гасим отложенные reprice-таймеры, иначе setBonds на размонтированном Dashboard
      const t = repriceTimers.current;
      Object.values(t).forEach(clearTimeout);
      repriceTimers.current = {};
    };
  }, [scheduleReprice]);

  const filtered = useMemo(() => {
    const ORDER = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC", "CC", "C", "D"];
    const ratingMatch = (r) => ratingsSel.some((k) =>
      k === "NR" ? !r : k === "BELOW" ? (r && ORDER.indexOf(r) > ORDER.indexOf("BBB")) : k === r
    );
    // портфельные поля: qty из позиций, стоимость = qty × dirty price (RUB)
    let rows = bonds.map((b) => {
      const qty = positions[b.isin];
      if (qty == null) return b;
      const pos_value = b.dirty_price_rub != null ? qty * b.dirty_price_rub : null;
      return { ...b, qty, pos_value };
    });
    if (onlyWatch) rows = rows.filter((b) => watch.includes(b.isin));
    if (basesSel.length) rows = rows.filter((b) => basesSel.includes(b.base_rate_type));
    if (ratingsSel.length) rows = rows.filter((b) => ratingMatch(b.rating));
    if (emittersSel.length) rows = rows.filter((b) => emittersSel.includes(b.emitter_name));
    const q = query.trim().toLowerCase();
    if (q) {
      rows = rows.filter((b) =>
        (b.isin + " " + b.short_name + " " + b.formula).toLowerCase().includes(q)
      );
    }
    const { key, dir } = sort;
    const m = dir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      let x = a[key], y = b[key];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      if (typeof x === "string") return x.localeCompare(y) * m;
      return (x - y) * m;
    });
    return rows;
  }, [bonds, onlyWatch, basesSel, ratingsSel, emittersSel, query, sort, watch, positions]);

  // список эмитентов (имя + число бумаг) для фильтра/агрегатов — по всему юниверсу
  const issuers = useMemo(() => {
    const m = new Map();
    for (const b of bonds) {
      if (!b.emitter_name) continue;
      m.set(b.emitter_name, (m.get(b.emitter_name) || 0) + 1);
    }
    return [...m.entries()].map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }, [bonds]);

  const toggleIn = (setter) => (val) =>
    setter((arr) => (arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val]));

  const onSort = useCallback((key) => {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  }, []);

  const openDrawer = useCallback((isin, triggerEl) => {
    lastTriggerRef.current = triggerEl || document.activeElement;
    setSearchParams((sp) => { const n = new URLSearchParams(sp); n.set("isin", isin); return n; });
  }, [setSearchParams]);

  const closeDrawer = useCallback(() => {
    setSearchParams((sp) => { const n = new URLSearchParams(sp); n.delete("isin"); return n; });
    const el = lastTriggerRef.current;
    if (el && el.focus) requestAnimationFrame(() => el.focus());
  }, [setSearchParams]);

  const navigate = useNavigate();
  // из агрегатов эмитента → фильтр «Флоатеры» по этому эмитенту
  const pickIssuer = useCallback((name) => {
    setEmittersSel([name]);
    navigate("/floaters");
  }, [navigate]);

  const floatersView = (
    <>
      <Kpis bonds={filtered} />
      <Toolbar
        onlyWatch={onlyWatch} setOnlyWatch={setOnlyWatch}
        basesSel={basesSel} toggleBase={toggleIn(setBasesSel)}
        ratingsSel={ratingsSel} toggleRating={toggleIn(setRatingsSel)}
        issuers={issuers} emittersSel={emittersSel} toggleEmitter={toggleIn(setEmittersSel)}
        clearEmitters={() => setEmittersSel([])}
        query={query} setQuery={setQuery}
        watchCount={watch.length}
        shown={filtered.length} total={bonds.length}
        showAnalytics={showAnalytics} setShowAnalytics={setShowAnalytics}
        onImportCsv={importCsv}
        posCount={Object.keys(positions).length} onClearPositions={clearPositions}
        visibleCols={visibleCols} onToggleCol={toggleCol} onResetCols={resetCols}
      />
      {showAnalytics && <AnalyticsPanel rows={filtered} />}
      <BondTable
        rows={filtered}
        status={status}
        errMsg={errMsg}
        sort={sort}
        onSort={onSort}
        onOpen={openDrawer}
        watch={watch}
        onToggleStar={toggleStar}
        filtered={onlyWatch || basesSel.length > 0 || ratingsSel.length > 0 || emittersSel.length > 0 || query !== ""}
        onClearFilters={() => { setOnlyWatch(false); setBasesSel([]); setRatingsSel([]); setEmittersSel([]); setQuery(""); }}
        onRetry={loadBonds}
        visibleCols={visibleCols}
      />
      <Drawer isin={drawerIsin} onClose={closeDrawer} />
      <StatusBar count={bonds.length} live={live} sources={meta.source_status}
        theme={theme} onSetTheme={setTheme} />
    </>
  );

  return (
    <div id="app" className={theme === "light" ? "" : "theme-" + theme}>
      <Topbar
        meta={meta} live={live}
        onRefresh={() => { fetchMeta().then(setMeta).catch(() => {}); loadBonds(); }}
        user={user}
        onLogout={onLogout}
        onOpenSettings={() => setShowSettings(true)}
      />
      <Routes>
        <Route path="/" element={<Navigate to="/floaters" replace />} />
        <Route path="/floaters" element={floatersView} />
        <Route path="/issuers" element={<IssuerAggregates bonds={bonds} onPickIssuer={pickIssuer} />} />
        <Route path="/funds" element={<FundsModule />} />
        <Route path="/funds/:code" element={<FundsModule />} />
        <Route path="/curves" element={<CurvesModule />} />
        <Route path="/curves/:view" element={<CurvesModule />} />
        <Route path="*" element={<Navigate to="/floaters" replace />} />
      </Routes>
      {showSettings && <AdminPanel user={user} onClose={() => setShowSettings(false)} />}
    </div>
  );
}

// Гейт авторизации: пока не проверили сессию — заглушка; нет сессии — Login; есть — дашборд.
function AuthGate() {
  const { auth, onLogin } = useAuth();
  const [theme] = useState(() => localStorage.getItem("theme") || "light");

  const themeCls = theme === "light" ? "" : "theme-" + theme;
  if (auth === "checking") {
    return <div className={("login-wrap " + themeCls).trim()} />;
  }
  if (auth === "anon") {
    return (
      <div className={themeCls}>
        <Login onSuccess={onLogin} />
      </div>
    );
  }
  return <Dashboard />;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BrowserRouter basename={APP_BASENAME}>
          <AuthGate />
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  );
}
