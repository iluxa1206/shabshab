import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes, useNavigate, useSearchParams } from "react-router-dom";
import { fetchBonds, fetchDepth, fetchMeta, fetchQuotes, connectMarketWs, repriceBond, UnauthorizedError, APP_BASENAME } from "./api.js";
import { applyVolume } from "./vwap.js";
import { AuthProvider, queryClient, useAuth } from "./auth.jsx";
import Login from "./components/Login.jsx";
import AdminPanel from "./components/AdminPanel.jsx";
import Catalog from "./components/Catalog.jsx";
import Topbar from "./components/Topbar.jsx";
import Toolbar from "./components/Toolbar.jsx";
import BondTable, { DEFAULT_COLS } from "./components/BondTable.jsx";
import AnalyticsPanel from "./components/AnalyticsPanel.jsx";
import Drawer from "./components/Drawer.jsx";
import StatusBar from "./components/StatusBar.jsx";
import CurvesModule from "./components/CurvesModule.jsx";
import IssuerAggregates from "./components/IssuerAggregates.jsx";
import FixedModule from "./components/FixedModule.jsx";
import EuroStub from "./components/EuroStub.jsx";
import StatusPage from "./components/StatusPage.jsx";
import AlertsWatcher from "./components/AlertsWatcher.jsx";
import BondAudit from "./components/BondAudit.jsx";
import PaymentsCalendar from "./components/PaymentsCalendar.jsx";
import TradesTape from "./components/TradesTape.jsx";
// lightweight-charts тянет ~180 kB — грузим только на самой странице графика,
// а не в общий бандл дашборда
const ChartPage = lazy(() => import("./components/ChartPage.jsx"));

// Фильтры живут в query string: вид дашборда можно кинуть ссылкой коллеге и он
// переживает F5. Ключи короткие, мульти-значения — повторяющимися параметрами
// (?base=RUONIA&rt=AAA&rt=AA), чтобы не ломаться на именах эмитентов с запятыми.
// vol/mf/mt — старые ключи (единый объём, даты погашения), держим в списке,
// чтобы вычищать их из старых ссылок
const FILTER_KEYS = ["q", "watch", "base", "rt", "em", "two", "vol", "vb", "va", "vm", "mf", "mt", "myf", "myt"];
// Такт котировок рынка (бумаги вне избранного). Избранное идёт push-стримом.
const QUOTES_POLL_MS = 5000;
// Пол частоты reprice на бумагу: в push-потоке цена ликвидной бумаги двигается
// непрерывно, и без пола каждый тик заказывал бы пересчёт на бэке.
const REPRICE_MIN_MS = 2000;
// Сколько бумага считается «живой» после последнего WS-пуша. Пока живая —
// снапшот MOEX её не трогает (push свежее). Замолчала (упёрлась в потолок
// подписок Alor, стрим лёг, торгов нет) — снова обновляется тактом 5с.
const LIVE_FRESH_MS = 15000;
const initialParams = () => new URLSearchParams(window.location.search);

function Dashboard() {
  const { user, onLogout } = useAuth();
  const [bonds, setBonds] = useState([]);
  const [status, setStatus] = useState("loading"); // loading | ready | error
  const [errMsg, setErrMsg] = useState("");
  const [meta, setMeta] = useState({ calc_date: null, rates_date: null });
  const [live, setLive] = useState(false);

  // три независимые группы фильтров (AND-пересечение; пустая группа = все).
  // Стартовое значение — из URL (ссылка/F5), иначе из localStorage (последняя
  // сессия) для тех фильтров, которые раньше и так persist'ились.
  const [onlyWatch, setOnlyWatch] = useState(() => initialParams().get("watch") === "1");
  const [basesSel, setBasesSel] = useState(() => initialParams().getAll("base"));    // KEYRATE / RUONIA
  const [ratingsSel, setRatingsSel] = useState(() => initialParams().getAll("rt")); // AAA / AA / A / BBB / BELOW / NR
  const [emittersSel, setEmittersSel] = useState(() => initialParams().getAll("em")); // имена эмитентов (мульти)
  const [twoSided, setTwoSided] = useState(() => initialParams().get("two") === "1");  // только двусторонние котировки
  // размеры тикета по сторонам, ₽ (0 = сторона не фильтруется): котировка стороны
  // пересчитывается в VWAP на этот объём по лестнице стакана
  const [volBid, setVolBid] = useState(() => Number(initialParams().get("vb"))
    || Number(localStorage.getItem("volBidRub") || 0) || 0);
  const [volAsk, setVolAsk] = useState(() => Number(initialParams().get("va"))
    || Number(localStorage.getItem("volAskRub") || 0) || 0);
  // связка условий bid/offer: "and" — обе стороны, "or" — хотя бы одна
  const [volMode, setVolMode] = useState(() => (initialParams().get("vm")
    || localStorage.getItem("volMode")) === "or" ? "or" : "and");
  // окно погашения [от, до] в ЛЕТ до погашения (строки как в инпуте, "" = не задано)
  const [matFrom, setMatFrom] = useState(() => initialParams().get("myf") || localStorage.getItem("matYrsFrom") || "");
  const [matTo, setMatTo] = useState(() => initialParams().get("myt") || localStorage.getItem("matYrsTo") || "");
  const [query, setQuery] = useState(() => initialParams().get("q") || "");
  const [showAnalytics, setShowAnalytics] = useState(false);
  const [sort, setSort] = useState({ key: "yield_over_index_bps", dir: "asc" });

  // drawer бумаги — в URL (?isin=): deep-link + back закрывает
  const [searchParams, setSearchParams] = useSearchParams();
  const drawerIsin = searchParams.get("isin");
  const [showSettings, setShowSettings] = useState(false);
  const [theme, setTheme] = useState(() => localStorage.getItem("theme") || "light");
  const [watch, setWatch] = useState(() => {
    try { return JSON.parse(localStorage.getItem("watch") || "[]"); } catch { return []; }
  });
  // видимые столбцы таблицы (persist)
  const [visibleCols, setVisibleCols] = useState(() => {
    try {
      const s = JSON.parse(localStorage.getItem("cols") || "null");
      if (!Array.isArray(s) || !s.length) return DEFAULT_COLS;
      // колонки, добавленные после того, как юзер последний раз сохранял набор,
      // показываем: иначе новая колонка невидима всем, кто хоть раз трогал меню
      const known = new Set(JSON.parse(localStorage.getItem("cols_known") || "[]"));
      const fresh = DEFAULT_COLS.filter((k) => !known.has(k) && !s.includes(k));
      let next = fresh.length ? [...s, ...fresh] : s;
      // Одноразовая нормализация: до появления drag-n-drop порядок в cols не имел
      // значения (таблица шла по COLS), и новые колонки просто дописывались в
      // конец. Теперь порядок — источник истины, поэтому первый заход на новую
      // версию раскладывает сохранённый набор по дефолтному порядку; дальше
      // порядок пользовательский и мы его не трогаем.
      // (флаг cols_ord выставляет эффект ниже: initializer обязан быть чистым —
      // в StrictMode он зовётся дважды, и запись отсюда съедала бы нормализацию)
      if (localStorage.getItem("cols_ord") !== "1") {
        const inSaved = new Set(next);
        next = [...DEFAULT_COLS.filter((k) => inSaved.has(k)),
                ...next.filter((k) => !DEFAULT_COLS.includes(k))];
      }
      return next;
    } catch { return DEFAULT_COLS; }
  });
  const lastTriggerRef = useRef(null);
  const searchRef = useRef(null);

  // «/» — фокус в поиск (терминальная привычка). Игнорируем, когда юзер уже
  // печатает в каком-нибудь поле, иначе слэш не набрать.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      e.preventDefault();
      searchRef.current?.focus();
      searchRef.current?.select();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Состояние фильтров → query string. replace: true — фильтр не должен плодить
  // записи в истории (иначе «назад» после набора «РЖД» жуёт шесть символов).
  // Чужие параметры (?isin= у drawer) не трогаем: чистим только свои ключи.
  useEffect(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      FILTER_KEYS.forEach((k) => next.delete(k));
      if (query) next.set("q", query);
      if (onlyWatch) next.set("watch", "1");
      basesSel.forEach((v) => next.append("base", v));
      ratingsSel.forEach((v) => next.append("rt", v));
      emittersSel.forEach((v) => next.append("em", v));
      if (twoSided) next.set("two", "1");
      if (volBid) next.set("vb", String(volBid));
      if (volAsk) next.set("va", String(volAsk));
      if (volMode === "or" && (volBid || volAsk)) next.set("vm", "or");
      if (matFrom) next.set("myf", matFrom);
      if (matTo) next.set("myt", matTo);
      return next;
    }, { replace: true });
  }, [query, onlyWatch, basesSel, ratingsSel, emittersSel, twoSided, volBid, volAsk, volMode, matFrom, matTo, setSearchParams]);

  useEffect(() => { localStorage.setItem("volBidRub", String(volBid)); }, [volBid]);
  useEffect(() => { localStorage.setItem("volAskRub", String(volAsk)); }, [volAsk]);
  useEffect(() => { localStorage.setItem("volMode", volMode); }, [volMode]);
  useEffect(() => { localStorage.setItem("matYrsFrom", matFrom); }, [matFrom]);
  useEffect(() => { localStorage.setItem("matYrsTo", matTo); }, [matTo]);
  useEffect(() => { localStorage.setItem("theme", theme); }, [theme]);
  useEffect(() => { localStorage.setItem("watch", JSON.stringify(watch)); }, [watch]);
  useEffect(() => { localStorage.setItem("cols", JSON.stringify(visibleCols)); }, [visibleCols]);
  // снимок известных на этой сборке колонок — база для авто-показа новых (см. выше)
  useEffect(() => { localStorage.setItem("cols_known", JSON.stringify(DEFAULT_COLS)); }, []);
  // порядок колонок нормализован — дальше он пользовательский, не трогаем
  useEffect(() => { localStorage.setItem("cols_ord", "1"); }, []);

  // стабильная ссылка (не inline-стрелка) — иначе memo(BondRow) бесполезен
  const toggleStar = useCallback((isin) =>
    setWatch((w) => (w.includes(isin) ? w.filter((x) => x !== isin) : [...w, isin])), []);

  const toggleCol = useCallback((key) => setVisibleCols((cs) =>
    cs.includes(key) ? cs.filter((k) => k !== key) : [...cs, key]), []);
  // ширины колонок, натянутые мышью: {key: px}. Пусто → дефолт из COLS.w
  const [colWidths, setColWidths] = useState(() => {
    try { return JSON.parse(localStorage.getItem("colw") || "{}") || {}; } catch { return {}; }
  });
  useEffect(() => { localStorage.setItem("colw", JSON.stringify(colWidths)); }, [colWidths]);
  const resizeCol = useCallback((key, px) => setColWidths((w) => ({ ...w, [key]: px })), []);
  const resetColWidth = useCallback((key) => setColWidths((w) => {
    const next = { ...w }; delete next[key]; return next;
  }), []);
  // «сброс» в меню столбцов возвращает и состав, и порядок, и ширины
  const resetCols = useCallback(() => { setVisibleCols(DEFAULT_COLS); setColWidths({}); }, []);
  // Перенос колонки: from встаёт НА МЕСТО to (порядок visibleCols = порядок в
  // таблице). to может быть "+1"/"-1" — сдвиг на шаг (Alt+←/→ на заголовке).
  const moveCol = useCallback((from, to) => setVisibleCols((cs) => {
    const i = cs.indexOf(from);
    if (i < 0) return cs;
    const j = to === "+1" ? i + 1 : to === "-1" ? i - 1 : cs.indexOf(to);
    if (j < 0 || j >= cs.length || i === j) return cs;
    const next = cs.slice();
    next.splice(j, 0, next.splice(i, 1)[0]);
    return next;
  }), []);

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
      // цены, под которые метрики уже посчитаны бэком: WS-тик с тем же числом
      // не должен заказывать reprice (см. wsPxRef ниже)
      wsPxRef.current = Object.fromEntries(
        items.filter((b) => b.last_price_pct != null).map((b) => [b.isin, b.last_price_pct]));
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

  // Цена, под которую строка уже посчитана (из /api/bonds или прошлого reprice).
  // Гвард против шторма: WS присылает last-price тактом, а не по изменению, и
  // reprice того же числа — чистый холостой пересчёт на бэке.
  const wsPxRef = useRef({});
  // isin → время последнего WS-пуша: им решается, чья цифра свежее (push или
  // снапшот MOEX) при мердже котировок рынка
  const liveTsRef = useRef({});
  // фолбэк-таймеры «движок не прислал производные» → одиночный reprice
  const repriceFallback = useRef({});

  // debounced live-пересчёт производных строки под новую цену (WS тикает только
  // цену). Reprice возвращает DM/SM/dirty/Y-IDX/z_model под введённой ценой.
  //
  // Дебаунс 500мс гасит серию тиков, но у ликвидной бумаги в push-потоке Alor
  // цена может двигаться непрерывно — тогда каждые полсекунды улетал бы новый
  // пересчёт. Пол в REPRICE_MIN_MS держит частоту на бумагу: последняя цена
  // всё равно доедет, просто следующим окном.
  const repriceTimers = useRef({});
  const repriceLastAt = useRef({});
  const scheduleReprice = useCallback((isin, price) => {
    const t = repriceTimers.current;
    if (t[isin]) clearTimeout(t[isin]);
    const since = Date.now() - (repriceLastAt.current[isin] || 0);
    const wait = Math.max(500, REPRICE_MIN_MS - since);
    t[isin] = setTimeout(async () => {
      delete t[isin];
      repriceLastAt.current[isin] = Date.now();
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
              yield_over_index_bps: r.yield_over_index_bps ?? b.yield_over_index_bps,
              yield_xirr_pct: r.yield_xirr_pct ?? b.yield_xirr_pct,
              index_yield_pct: r.index_yield_pct ?? b.index_yield_pct,
            };
          })
        );
      } catch { /* 401 → глобальный logout; прочее — строка остаётся dim */ }
    }, wait);
  }, []);

  // WS once
  useEffect(() => {
    const ctrl = connectMarketWs(
      () => subIsinsRef.current,
      setLive,
      // Патч строки из push-потока Alor: {last_price_pct, bid, ask, bid_qty,
      // ask_qty, vwap_pct, vwap_volume} — приходит частями (котировка несёт
      // верх стакана, сделка — цену и средневзвес), поэтому применяем то, что
      // пришло, а не весь набор. Патч с metrics:true несёт производные
      // (Y-IDX/DM/SM/dirty), пересчитанные движком бэка — тогда /reprice с
      // фронта не нужен вовсе.
      (isin, q) => {
        const price = q.last_price_pct;
        liveTsRef.current[isin] = Date.now();
        const priceNew = price != null && wsPxRef.current[isin] !== price;
        if (priceNew) wsPxRef.current[isin] = price;
        const METRIC_KEYS = ["yield_over_index_bps", "dm_bps", "disc_margin_bps",
          "z_model_bps", "yield_xirr_pct", "index_yield_pct", "dirty_price_rub",
          "delta_to_prev_close"];
        setBonds((prev) =>
          prev.map((b) => {
            if (b.isin !== isin) return b;
            const n = { ...b, _live: true };
            if (q.bid != null) n.bid_price_pct = q.bid;
            if (q.ask != null) n.ask_price_pct = q.ask;
            if (q.vwap_pct != null) n.wap_price_pct = q.vwap_pct;
            if (q.metrics) {
              for (const k of METRIC_KEYS) if (q[k] != null) n[k] = q[k];
              n._mstale = false;   // производные свежие — строка не dim
            }
            if (priceNew) {
              // CHG (vs пред. закрытие) пересчитываем СРАЗУ на клиенте: prev_close =
              // last − delta (инвариант дня) → delta_new = price − prev_close.
              let delta = b.delta_to_prev_close;
              if (delta != null && b.last_price_pct != null) {
                const prevClose = b.last_price_pct - delta;
                delta = Math.round((price - prevClose) * 10000) / 10000;
              }
              n.last_price_pct = price;
              n.delta_to_prev_close = delta;
              if (!q.metrics) n._mstale = true;   // производные приедут патчем движка
            }
            return n;
          })
        );
        // reprice не заказываем: движок бэка пришлёт производные патчем в
        // ближайший такт. Фолбэк — если патч не пришёл за 15с (движок лежит),
        // строка остаётся dim и один reprice всё же уходит.
        if (priceNew && !q.metrics) {
          const t = repriceFallback.current;
          if (t[isin]) clearTimeout(t[isin]);
          t[isin] = setTimeout(() => {
            delete t[isin];
            const b = bondsRef.current.find((x) => x.isin === isin);
            if (b && b._mstale) scheduleReprice(isin, wsPxRef.current[isin]);
          }, 15000);
        }
      }
    );
    wsRef.current = ctrl;
    return () => {
      ctrl.close();
      // гасим отложенные reprice-таймеры, иначе setBonds на размонтированном Dashboard
      const t = repriceTimers.current;
      Object.values(t).forEach(clearTimeout);
      repriceTimers.current = {};
      Object.values(repriceFallback.current).forEach(clearTimeout);
      repriceFallback.current = {};
    };
  }, [scheduleReprice]);

  // Котировки всего рынка тактом 5с: цена, верх стакана, средневзвес дня,
  // оборот. Избранное сюда не мерджим — по нему те же поля приходят push'ем
  // через WS, и снапшот MOEX только откатывал бы их назад.
  //
  // Расчётные метрики (Y-IDX/DM/SM) здесь НЕ пересчитываются: 540 reprice
  // каждые 5 секунд — ровно тот шторм, который убран в 9fc8980. Их обновляет
  // своим циклом бэкенд, поэтому у неизбранных бумаг спред может отставать от
  // цены на такт поллера юниверса.
  const quotesQ = useQuery({
    queryKey: ["quotes"], queryFn: ({ signal }) => fetchQuotes(signal),
    refetchInterval: QUOTES_POLL_MS, staleTime: QUOTES_POLL_MS - 1000,
  });
  useEffect(() => {
    const items = quotesQ.data?.items;
    if (!items?.length) return;
    const byIsin = new Map(items.map((q) => [q.isin, q]));
    // «на стриме» = реально пушилось только что, а не «есть в избранном»:
    // сверх потолка подписок Alor бумага остаётся в избранном, но живёт
    // снапшотом, и по списку избранного мы бы её потеряли совсем
    const now = Date.now();
    const streamed = new Set(
      Object.entries(liveTsRef.current)
        .filter(([, ts]) => now - ts < LIVE_FRESH_MS)
        .map(([isin]) => isin));
    setBonds((prev) => {
      let touched = false;
      const next = prev.map((b) => {
        if (streamed.has(b.isin)) return b;
        const q = byIsin.get(b.isin);
        if (!q) return b;
        const same = (q.last == null || q.last === b.last_price_pct)
          && (q.bid == null || q.bid === b.bid_price_pct)
          && (q.ask == null || q.ask === b.ask_price_pct)
          && (q.wap == null || q.wap === b.wap_price_pct)
          && (q.vol == null || q.vol === b.val_today)
          && (q.yoi == null || q.yoi === b.yield_over_index_bps);
        if (same) return b;              // без изменений — не трогаем ссылку
        touched = true;
        // снимаем метку live: дальше в строке цифры снапшота, и подпись
        // «наш VWAP по сделкам» на них была бы неправдой
        const n = { ...b, _live: false };
        if (q.last != null) {
          // CHG держим согласованным с новой ценой (prev_close — инвариант дня)
          if (b.delta_to_prev_close != null && b.last_price_pct != null) {
            const prevClose = b.last_price_pct - b.delta_to_prev_close;
            n.delta_to_prev_close = Math.round((q.last - prevClose) * 10000) / 10000;
          }
          n.last_price_pct = q.last;
        }
        if (q.bid != null) n.bid_price_pct = q.bid;
        if (q.ask != null) n.ask_price_pct = q.ask;
        if (q.wap != null) n.wap_price_pct = q.wap;
        if (q.vol != null) n.val_today = q.vol;
        // Y-IDX от событийного движка: спред торгуемой бумаги обновляется по
        // факту сделки, а не раз в 10 минут поллером
        if (q.yoi != null) n.yield_over_index_bps = q.yoi;
        return n;
      });
      return touched ? next : prev;      // ничего не поменялось — без ререндера
    });
  }, [quotesQ.data]);

  // лестницы стаканов по всему рынку — только когда фильтр по объёму включён.
  // Снимок на бэке обновляется раз в ~2 мин, чаще тянуть нечего.
  const volOn = volBid > 0 || volAsk > 0;
  const depthQ = useQuery({
    queryKey: ["depth"], queryFn: fetchDepth, refetchInterval: 60000,
    enabled: volOn, staleTime: 30000,
  });
  const depth = depthQ.data?.items;

  const filtered = useMemo(() => {
    const ORDER = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC", "CC", "C", "D"];
    const ratingMatch = (r) => ratingsSel.some((k) =>
      k === "NR" ? !r : k === "BELOW" ? (r && ORDER.indexOf(r) > ORDER.indexOf("BBB")) : k === r
    );
    let rows = bonds.slice();
    if (onlyWatch) rows = rows.filter((b) => watch.includes(b.isin));
    if (basesSel.length) rows = rows.filter((b) => basesSel.includes(b.base_rate_type));
    if (ratingsSel.length) rows = rows.filter((b) => ratingMatch(b.rating));
    if (emittersSel.length) rows = rows.filter((b) => emittersSel.includes(b.emitter_name));
    // двусторонняя котировка: обе стороны стакана на месте. Односторонний рынок
    // (только бид или только оффер) торговать нечем — прячем целиком.
    // окно погашения в ЛЕТ до погашения: границы переводим в даты-отсечки и
    // сравниваем ISO-строки. Строки без даты погашения (перп/дыра в справочнике)
    // при заданной границе прячем — иначе они молча пролезают в любой срок
    const yearsToIso = (y) => new Date(Date.now() + y * 365.25 * 86400e3).toISOString().slice(0, 10);
    const mFrom = parseFloat(matFrom), mTo = parseFloat(matTo);
    if (Number.isFinite(mFrom)) {
      const cut = yearsToIso(mFrom);
      rows = rows.filter((b) => b.maturity_date && b.maturity_date >= cut);
    }
    if (Number.isFinite(mTo)) {
      const cut = yearsToIso(mTo);
      rows = rows.filter((b) => b.maturity_date && b.maturity_date <= cut);
    }
    // ФИЛЬТР ПО ОБЪЁМУ. Котировка заполненной стороны становится VWAP на её тикет
    // по лестнице стакана, Y-IDX — спред к этой цене. volMode решает судьбу строки,
    // когда заполнены оба поля: "and" — обе стороны обязаны набрать объём, "or" —
    // достаточно одной (не набравшая гаснет в прочерк). Чип BID×OFFER применяется
    // следующим и может дополнительно потребовать обе стороны.
    if (volOn && depth) {
      rows = rows.map((b) => applyVolume(b, depth[b.isin], volBid, volAsk, volMode)).filter(Boolean);
    }
    if (twoSided) rows = rows.filter((b) => b.bid_price_pct != null && b.ask_price_pct != null);
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
  }, [bonds, onlyWatch, basesSel, ratingsSel, emittersSel, twoSided, query, sort, watch,
      matFrom, matTo, volOn, volBid, volAsk, volMode, depth]);

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

  const openDrawer = useCallback((isin, triggerEl, kind) => {
    lastTriggerRef.current = triggerEl || document.activeElement;
    setSearchParams((sp) => {
      const n = new URLSearchParams(sp);
      n.set("isin", isin);
      if (kind === "fixed") n.set("k", "fixed"); else n.delete("k");
      return n;
    });
  }, [setSearchParams]);

  const closeDrawer = useCallback(() => {
    setSearchParams((sp) => { const n = new URLSearchParams(sp); n.delete("isin"); n.delete("k"); n.delete("ob"); return n; });
    const el = lastTriggerRef.current;
    if (el && el.focus) requestAnimationFrame(() => el.focus());
  }, [setSearchParams]);

  const navigate = useNavigate();
  // из агрегатов эмитента → фильтр «Флоатеры» по этому эмитенту
  const pickIssuer = useCallback((name) => {
    setEmittersSel([name]);
    navigate("/floaters");
  }, [navigate]);

  // сколько фильтров активно (для бейджа на кнопке ФИЛЬТРЫ и пустого состояния таблицы)
  const activeFilters = (onlyWatch ? 1 : 0) + (basesSel.length ? 1 : 0) + (ratingsSel.length ? 1 : 0)
    + (emittersSel.length ? 1 : 0) + (twoSided ? 1 : 0) + (query !== "" ? 1 : 0)
    + (volBid > 0 || volAsk > 0 ? 1 : 0) + (matFrom !== "" ? 1 : 0) + (matTo !== "" ? 1 : 0);
  const resetFilters = useCallback(() => {
    setOnlyWatch(false); setBasesSel([]); setRatingsSel([]); setEmittersSel([]);
    setTwoSided(false); setQuery(""); setVolBid(0); setVolAsk(0); setMatFrom(""); setMatTo("");
  }, []);

  const floatersView = (
    <>
      <Toolbar
        onlyWatch={onlyWatch} setOnlyWatch={setOnlyWatch}
        basesSel={basesSel} toggleBase={toggleIn(setBasesSel)}
        ratingsSel={ratingsSel} toggleRating={toggleIn(setRatingsSel)}
        clearBases={() => setBasesSel([])}
        issuers={issuers} emittersSel={emittersSel} toggleEmitter={toggleIn(setEmittersSel)}
        clearEmitters={() => setEmittersSel([])}
        activeFilters={activeFilters} onResetFilters={resetFilters}
        twoSided={twoSided} setTwoSided={setTwoSided}
        volBid={volBid} setVolBid={setVolBid} volAsk={volAsk} setVolAsk={setVolAsk}
        volMode={volMode} setVolMode={setVolMode}
        depthTs={depthQ.data?.ts} depthLoading={volOn && depthQ.isLoading}
        matFrom={matFrom} setMatFrom={setMatFrom} matTo={matTo} setMatTo={setMatTo}
        query={query} setQuery={setQuery} searchRef={searchRef}
        watchCount={watch.length}
        shown={filtered.length} total={bonds.length}
        showAnalytics={showAnalytics} setShowAnalytics={setShowAnalytics}
        visibleCols={visibleCols} onToggleCol={toggleCol} onResetCols={resetCols} onMoveCol={moveCol}
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
        filtered={activeFilters > 0}
        onClearFilters={resetFilters}
        onRetry={loadBonds}
        visibleCols={visibleCols}
        onMoveCol={moveCol}
        colWidths={colWidths}
        onResizeCol={resizeCol}
        onResetColWidth={resetColWidth}
      />
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
        <Route path="/reference" element={<Catalog user={user} />} />
        <Route path="/fixed" element={<FixedModule onOpen={openDrawer} />} />
        <Route path="/euro" element={<EuroStub />} />
        <Route path="/trades" element={<TradesTape />} />
        <Route path="/payments" element={<PaymentsCalendar />} />
        <Route path="/curves" element={<CurvesModule />} />
        <Route path="/curves/:view" element={<CurvesModule />} />
        <Route path="/status" element={<StatusPage />} />
        <Route path="/audit/:isin" element={<BondAudit />} />
        <Route path="/chart/:isin" element={
          <Suspense fallback={<div className="cp-foot" style={{ padding: 16 }}>загрузка графика…</div>}>
            <ChartPage />
          </Suspense>} />
        <Route path="*" element={<Navigate to="/floaters" replace />} />
      </Routes>
      <Drawer isin={drawerIsin} kind={searchParams.get("k")} autoOrderbook={searchParams.get("ob") !== "0"} onClose={closeDrawer} />
      <StatusBar count={bonds.length} bonds={bonds} kpiBonds={filtered} live={live} sources={meta.source_status}
        theme={theme} onSetTheme={setTheme} />
      {showSettings && <AdminPanel user={user} onClose={() => setShowSettings(false)} />}
      <AlertsWatcher />
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
