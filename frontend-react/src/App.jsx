import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchBonds, fetchMeta, connectMarketWs, fetchMe, logout, UnauthorizedError } from "./api.js";
import Login from "./components/Login.jsx";
import AdminPanel from "./components/AdminPanel.jsx";
import Topbar from "./components/Topbar.jsx";
import Kpis from "./components/Kpis.jsx";
import Toolbar from "./components/Toolbar.jsx";
import BondTable, { DEFAULT_COLS } from "./components/BondTable.jsx";
import AnalyticsPanel from "./components/AnalyticsPanel.jsx";
import Drawer from "./components/Drawer.jsx";
import StatusBar from "./components/StatusBar.jsx";
import { parsePortfolioCsv } from "./portfolio.js";

function Dashboard({ user, onLogout }) {
  const [bonds, setBonds] = useState([]);
  const [status, setStatus] = useState("loading"); // loading | ready | error
  const [errMsg, setErrMsg] = useState("");
  const [meta, setMeta] = useState({ calc_date: null, rates_date: null });
  const [live, setLive] = useState(false);

  // три независимые группы фильтров (AND-пересечение; пустая группа = все)
  const [onlyWatch, setOnlyWatch] = useState(false);
  const [basesSel, setBasesSel] = useState([]);    // KEYRATE / RUONIA
  const [ratingsSel, setRatingsSel] = useState([]); // AAA / AA / A / BBB / BELOW / NR
  const [query, setQuery] = useState("");
  const [showAnalytics, setShowAnalytics] = useState(false);
  const [sort, setSort] = useState({ key: "dm_bps", dir: "asc" });

  const [drawerIsin, setDrawerIsin] = useState(null);
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

  const addBond = useCallback((isin) => setWatch((w) => (w.includes(isin) ? w : [...w, isin])), []);
  const removeBond = useCallback((isin) => setWatch((w) => w.filter((x) => x !== isin)), []);

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

  // WS once
  useEffect(() => {
    const ctrl = connectMarketWs(
      () => subIsinsRef.current,
      setLive,
      (isin, price) => {
        if (price == null) return;
        setBonds((prev) =>
          prev.map((b) => (b.isin === isin ? { ...b, last_price_pct: price } : b))
        );
      }
    );
    wsRef.current = ctrl;
    return () => ctrl.close();
  }, []);

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
  }, [bonds, onlyWatch, basesSel, ratingsSel, query, sort, watch, positions]);

  const toggleIn = (setter) => (val) =>
    setter((arr) => (arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val]));

  const onSort = useCallback((key) => {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  }, []);

  const openDrawer = useCallback((isin, triggerEl) => {
    lastTriggerRef.current = triggerEl || document.activeElement;
    setDrawerIsin(isin);
  }, []);

  const closeDrawer = useCallback(() => {
    setDrawerIsin(null);
    const el = lastTriggerRef.current;
    if (el && el.focus) requestAnimationFrame(() => el.focus());
  }, []);

  return (
    <div id="app" className={theme === "dark" ? "theme-dark" : ""}>
      <Topbar
        meta={meta} live={live}
        onRefresh={() => { fetchMeta().then(setMeta).catch(() => {}); loadBonds(); }}
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        user={user}
        onLogout={onLogout}
        onOpenSettings={() => setShowSettings(true)}
      />
      <Kpis bonds={filtered} />
      <Toolbar
        onlyWatch={onlyWatch} setOnlyWatch={setOnlyWatch}
        basesSel={basesSel} toggleBase={toggleIn(setBasesSel)}
        ratingsSel={ratingsSel} toggleRating={toggleIn(setRatingsSel)}
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
        onToggleStar={(isin) => (watch.includes(isin) ? removeBond(isin) : addBond(isin))}
        filtered={onlyWatch || basesSel.length > 0 || ratingsSel.length > 0 || query !== ""}
        onClearFilters={() => { setOnlyWatch(false); setBasesSel([]); setRatingsSel([]); setQuery(""); }}
        onRetry={loadBonds}
        visibleCols={visibleCols}
      />
      <Drawer isin={drawerIsin} onClose={closeDrawer} />
      {showSettings && <AdminPanel user={user} onClose={() => setShowSettings(false)} />}
      <StatusBar count={bonds.length} live={live} sources={meta.source_status} />
    </div>
  );
}

// Гейт авторизации: пока не проверили сессию — заглушка; нет сессии — Login; есть — дашборд.
export default function App() {
  const [auth, setAuth] = useState("checking"); // checking | anon | authed
  const [user, setUser] = useState(null);
  const [theme] = useState(() => localStorage.getItem("theme") || "light");

  useEffect(() => {
    fetchMe()
      .then((u) => { setUser(u); setAuth("authed"); })
      .catch(() => setAuth("anon"));
  }, []);

  const onLogout = useCallback(async () => {
    try { await logout(); } catch { /* игнор — всё равно на логин */ }
    setUser(null);
    setAuth("anon");
  }, []);

  if (auth === "checking") {
    return <div className={"login-wrap" + (theme === "dark" ? " theme-dark" : "")} />;
  }
  if (auth === "anon") {
    return (
      <div className={theme === "dark" ? "theme-dark" : ""}>
        <Login onSuccess={(u) => { setUser(u); setAuth("authed"); }} />
      </div>
    );
  }
  return <Dashboard user={user} onLogout={onLogout} />;
}
