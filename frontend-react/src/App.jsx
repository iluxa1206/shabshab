import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useSearchParams } from "react-router-dom";
import { fetchBonds, fetchDepth, fetchMeta, fetchQuotes, connectMarketWs, repriceBond, UnauthorizedError, APP_BASENAME } from "./api.js";
import { mergeStreamedQuote } from "./quotesMerge.js";
import { PageStatusProvider } from "./pageStatus.jsx";
import { applyVolume, yIdxAt } from "./vwap.js";
import { makeBondFilter } from "./search.js";
import { AuthProvider, queryClient, useAuth } from "./auth.jsx";
import Login from "./components/Login.jsx";
import AdminPanel from "./components/AdminPanel.jsx";
import Catalog from "./components/Catalog.jsx";
import Topbar from "./components/Topbar.jsx";
import { IconChart } from "./components/icons.jsx";
import Toolbar from "./components/Toolbar.jsx";
import BondTable, { DEFAULT_COLS } from "./components/BondTable.jsx";
import AnalyticsPanel, { focusMatch } from "./components/AnalyticsPanel.jsx";
import CompareModule, { CMP_MAX } from "./components/CompareModule.jsx";
import Drawer from "./components/Drawer.jsx";
import StatusBar from "./components/StatusBar.jsx";
import CurvesModule from "./components/CurvesModule.jsx";
import FixedModule from "./components/FixedModule.jsx";
import CalcModule from "./components/CalcModule.jsx";
import StatusPage from "./components/StatusPage.jsx";
import SignalsWatcher from "./components/SignalsWatcher.jsx";
import SignalsModule from "./components/SignalsModule.jsx";
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
const FILTER_KEYS = ["q", "watch", "base", "rt", "em", "two", "vol", "vb", "va", "vm", "mf", "mt", "myf", "myt", "sf", "st", "nosub", "noam", "cls"];
// Субординация — по имени бумаги: отдельного признака нет ни у MOEX, ни у
// corpbonds, а маркер в short_name — устойчивая конвенция («ВТБСУБ1-12»,
// «ВТБСУБТ1Р2»). Ловим и добавочный капитал (Т1/T1, перп).
// Тот же паттерн у скринера — services/screener_core.py::_SUBORD_RE.
const SUBORD_RE = /СУБ|SUB|ПЕРП|PERP|(?<![A-ZА-Я0-9])[TТ]1(?![0-9])/i;
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

// Новая цена стороны в строку вместе с её Y-IDX. Спред стороны считает бэк своим
// тактом (событийный движок / поллер юниверса), а цена верха стакана приезжает
// каждым тиком — поэтому цену без сдвига Y-IDX класть нельзя: строка покажет
// свежий бид/оффер со спредом от прежней цены. Сдвиг — тем же наклоном dY/dP,
// которым фронт считает Y-IDX VWAP-цены тикета; якорь берётся из ДОпатченной
// строки, т.е. пара «цена → спред» остаётся согласованной.
// Авторитетные числа бэка (патч метрик, полный refetch) мерджатся ПОСЛЕ и
// перекрывают эту линеаризацию.
function applySideQuote(b, n, side, px) {
  if (px == null || px === b[side === "bid" ? "bid_price_pct" : "ask_price_pct"]) return;
  const y = yIdxAt(b, px, side);
  if (side === "bid") {
    n.bid_price_pct = px;
    if (y != null) n.y_idx_bid_bps = y;
  } else {
    n.ask_price_pct = px;
    if (y != null) n.y_idx_ask_bps = y;
  }
}

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
  // суборды/перпы вон: другой класс риска, в сравнении спредов ломают картину.
  // ВКЛЮЧЁН ПО УМОЛЧАНИЮ — чистый рынок старшего долга; выключается вручную и
  // тогда запоминается (localStorage "0"), поэтому null-проверка, а не === "1".
  const [hideSub, setHideSub] = useState(() => {
    const p = initialParams();
    if (p.has("nosub")) return p.get("nosub") === "1";
    const ls = localStorage.getItem("hideSubord");
    return ls === null ? true : ls === "1";
  });
  // амортизируемые вон: у них падающий номинал, спред-дюрация и сам спред не
  // сопоставимы с bullet-выпусками. Признак has_amort — из графика MOEX.
  const [hideAmort, setHideAmort] = useState(() => {
    const p = initialParams();
    if (p.has("noam")) return p.get("noam") === "1";
    return localStorage.getItem("hideAmort") === "1";
  });
  // класс эмитента: OFZ (суверен Минфина) / CORP (всё остальное, включая субфеды).
  // Пустой список = оба, как и у прочих групп-мультивыборов.
  const [clsSel, setClsSel] = useState(() => initialParams().getAll("cls"));
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
  // окно спреда Y-IDX [от, до] в bps (строки как в инпуте, "" = не задано).
  // Бумаги без посчитанного спреда при заданной границе скрыты — иначе строки
  // с прочерком молча пролезали бы в любой диапазон
  const [spreadFrom, setSpreadFrom] = useState(() => initialParams().get("sf") || localStorage.getItem("spreadFrom") || "");
  const [spreadTo, setSpreadTo] = useState(() => initialParams().get("st") || localStorage.getItem("spreadTo") || "");
  const [query, setQuery] = useState(() => initialParams().get("q") || "");
  const [showAnalytics, setShowAnalytics] = useState(false);
  // выбор на графике аналитики ({type:"issuer"|"rating", key}) — временный фильтр
  // ТОЛЬКО таблицы: сами графики считаются по полному отфильтрованному набору,
  // повторный клик снимает выбор и таблица возвращается к прежнему составу
  const [anFocus, setAnFocus] = useState(null);
  const [sort, setSort] = useState({ key: "yield_over_index_bps", dir: "asc" });

  // drawer бумаги — в URL (?isin=): deep-link + back закрывает
  const [searchParams, setSearchParams] = useSearchParams();
  const drawerIsin = searchParams.get("isin");
  const [showSettings, setShowSettings] = useState(false);
  const [theme, setTheme] = useState(readTheme);
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
      if (hideSub) next.set("nosub", "1");
      if (hideAmort) next.set("noam", "1");
      clsSel.forEach((v) => next.append("cls", v));
      if (volBid) next.set("vb", String(volBid));
      if (volAsk) next.set("va", String(volAsk));
      if (volMode === "or" && (volBid || volAsk)) next.set("vm", "or");
      if (matFrom) next.set("myf", matFrom);
      if (matTo) next.set("myt", matTo);
      if (spreadFrom) next.set("sf", spreadFrom);
      if (spreadTo) next.set("st", spreadTo);
      return next;
    }, { replace: true });
  }, [query, onlyWatch, basesSel, ratingsSel, emittersSel, twoSided, hideSub, hideAmort, clsSel,
      volBid, volAsk, volMode, matFrom, matTo, spreadFrom, spreadTo, setSearchParams]);

  useEffect(() => { localStorage.setItem("hideSubord", hideSub ? "1" : "0"); }, [hideSub]);
  useEffect(() => { localStorage.setItem("hideAmort", hideAmort ? "1" : "0"); }, [hideAmort]);
  useEffect(() => { localStorage.setItem("volBidRub", String(volBid)); }, [volBid]);
  useEffect(() => { localStorage.setItem("volAskRub", String(volAsk)); }, [volAsk]);
  useEffect(() => { localStorage.setItem("volMode", volMode); }, [volMode]);
  useEffect(() => { localStorage.setItem("matYrsFrom", matFrom); }, [matFrom]);
  useEffect(() => { localStorage.setItem("matYrsTo", matTo); }, [matTo]);
  useEffect(() => { localStorage.setItem("spreadFrom", spreadFrom); }, [spreadFrom]);
  useEffect(() => { localStorage.setItem("spreadTo", spreadTo); }, [spreadTo]);
  useEffect(() => { localStorage.setItem("theme", theme); }, [theme]);
  // жёлтые подсказки эпохи — только в теме win; за собой всё снимают
  useEffect(() => {
    if (theme !== "win") return undefined;
    // модуль грузится асинхронно, а тему могут переключить раньше — без флага
    // слушатели повисли бы навсегда (cleanup отработал бы до init)
    let stop = null, cancelled = false;
    import("./retroTips").then((m) => {
      if (cancelled) return;
      stop = m.initRetroTips();
    });
    return () => { cancelled = true; if (stop) stop(); };
  }, [theme]);
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
  // буфер WS-патчей до флаша (коалесцирование пушей всего юниверса)
  const patchBufRef = useRef({});

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
      // верх стакана, сделка — цену и средневзвес). Патч с metrics:true несёт
      // производные (Y-IDX/DM/SM/dirty), пересчитанные движком бэка — тогда
      // /reprice с фронта не нужен вовсе.
      //
      // Пуши идут по ВСЕМ бумагам (wildcard) — применять каждый отдельным
      // setBonds значит ререндерить таблицу на каждое сообщение. Копим патчи в
      // буфере и мерджим одним проходом (флаш ниже, раз в 400мс).
      (isin, q) => {
        liveTsRef.current[isin] = Date.now();
        const buf = patchBufRef.current;
        const prev = buf[isin];
        buf[isin] = prev
          ? { ...prev, ...q, metrics: prev.metrics || q.metrics }
          : q;
      }
    );
    wsRef.current = ctrl;

    // флаш буфера: все накопленные патчи одним setBonds
    const METRIC_KEYS = ["yield_over_index_bps", "dm_bps", "disc_margin_bps",
      "z_model_bps", "yield_xirr_pct", "index_yield_pct", "dirty_price_rub",
      "delta_to_prev_close", "y_idx_bid_bps", "y_idx_ask_bps", "y_idx_wap_bps"];
    const flushId = setInterval(() => {
      const buf = patchBufRef.current;
      const isins = Object.keys(buf);
      if (!isins.length) return;
      patchBufRef.current = {};
      // priceNew решаем ДО мерджа: wsPxRef — цена, под которую строка посчитана
      const priceNew = {};
      for (const isin of isins) {
        const price = buf[isin].last_price_pct;
        if (price != null && wsPxRef.current[isin] !== price) {
          wsPxRef.current[isin] = price;
          priceNew[isin] = price;
        }
      }
      setBonds((prev) =>
        prev.map((b) => {
          const q = buf[b.isin];
          if (!q) return b;
          const n = { ...b, _live: true };
          // цена стороны и её Y-IDX ходят ПАРОЙ: цена приезжает каждым тиком, а
          // спред считает бэк своим тактом — без сдвига наклоном строка показывала
          // бы свежую цену со спредом от прежнего верха стакана.
          applySideQuote(b, n, "bid", q.bid);
          applySideQuote(b, n, "ask", q.ask);
          if (q.vwap_pct != null) n.wap_price_pct = q.vwap_pct;
          // оборот дня по тикам: биржевой VALTODAY из снапшота отстаёт, а свой
          // счёт растёт сделка в сделку. Назад не откатываем — патч может
          // прийти от сокета, чей агрегат ещё догоняется архивом.
          if (q.val_today != null && !(b.val_today > q.val_today)) n.val_today = q.val_today;
          if (q.metrics) {
            for (const k of METRIC_KEYS) if (q[k] != null) n[k] = q[k];
            n._mstale = false;   // производные свежие — строка не dim
          }
          const price = priceNew[b.isin];
          if (price != null) {
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
      // Фолбэк-reprice: движок молчит 15с → одна ходка. Только для избранного —
      // на весь юниверс фолбэк при лежачем движке сам стал бы штормом.
      const watchSet = new Set(paramsRef.current.watch || []);
      for (const isin of isins) {
        if (priceNew[isin] == null || buf[isin].metrics || !watchSet.has(isin)) continue;
        const t = repriceFallback.current;
        if (t[isin]) clearTimeout(t[isin]);
        t[isin] = setTimeout(() => {
          delete t[isin];
          const b = bondsRef.current.find((x) => x.isin === isin);
          if (b && b._mstale) scheduleReprice(isin, wsPxRef.current[isin]);
        }, 15000);
      }
    }, 400);

    return () => {
      ctrl.close();
      clearInterval(flushId);
      patchBufRef.current = {};
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
        const q = byIsin.get(b.isin);
        if (!q) return b;
        // Бумага на стриме: цены у неё свои (снапшот MOEX откатил бы их назад),
        // но РАСЧЁТНЫЕ метрики приходят только этим путём — см. quotesMerge.js.
        // Раньше строка отсекалась целиком, и у ликвидной бумаги без сделок
        // R-spread застревал на значении, приехавшем при загрузке страницы.
        if (streamed.has(b.isin)) {
          const m = mergeStreamedQuote(b, q);
          if (m !== b) touched = true;
          return m;
        }
        const same = (q.last == null || q.last === b.last_price_pct)
          && (q.bid == null || q.bid === b.bid_price_pct)
          && (q.ask == null || q.ask === b.ask_price_pct)
          && (q.wap == null || q.wap === b.wap_price_pct)
          && (q.yoi_wap == null || q.yoi_wap === b.y_idx_wap_bps)
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
        applySideQuote(b, n, "bid", q.bid);
        applySideQuote(b, n, "ask", q.ask);
        if (q.wap != null) n.wap_price_pct = q.wap;
        if (q.yoi_wap != null) n.y_idx_wap_bps = q.yoi_wap;
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
  // бэк держит лестницы push-свежими (depth-пул universe_stream) — тянем чаще,
  // чем при старом 120-секундном батч-снимке
  const depthQ = useQuery({
    queryKey: ["depth"], queryFn: fetchDepth, refetchInterval: 15000,
    enabled: volOn, staleTime: 10000,
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
    // суборды/перпы вон — распознаём по имени выпуска (см. SUBORD_RE)
    if (hideSub) rows = rows.filter((b) => !SUBORD_RE.test(b.short_name || ""));
    // амортизация — факт графика MOEX (больше одного транша погашения), флаг с бэка
    if (hideAmort) rows = rows.filter((b) => !b.has_amort);
    // ОФЗ / КОРП: класс считает бэк (is_ofz), фронт только сопоставляет выбор
    if (clsSel.length) rows = rows.filter((b) => clsSel.includes(b.is_ofz ? "OFZ" : "CORP"));
    // двусторонняя котировка: обе стороны стакана на месте. Односторонний рынок
    // (только бид или только оффер) торговать нечем — прячем целиком.
    // Окно срока в ЛЕТ до ГОРИЗОНТА ПРАЙСИНГА — той даты, к которой посчитаны
    // метрики строки (оферта/колл по правилу цены, иначе погашение). Фильтровать
    // по погашению было бы враньём в обе стороны: бумага с офертой через год
    // пряталась бы из окна «до 2 лет», хотя и торгуется, и считается к ней.
    // Границы переводим в даты-отсечки и сравниваем ISO-строки. Строки без даты
    // (перп/дыра в справочнике) при заданной границе прячем — иначе они молча
    // пролезают в любой срок.
    const yearsToIso = (y) => new Date(Date.now() + y * 365.25 * 86400e3).toISOString().slice(0, 10);
    const hzDate = (b) => ((b.preferred_horizon === "put" || b.preferred_horizon === "call")
      && b.offer_date) || b.maturity_date;
    const mFrom = parseFloat(matFrom), mTo = parseFloat(matTo);
    if (Number.isFinite(mFrom)) {
      const cut = yearsToIso(mFrom);
      rows = rows.filter((b) => hzDate(b) && hzDate(b) >= cut);
    }
    if (Number.isFinite(mTo)) {
      const cut = yearsToIso(mTo);
      rows = rows.filter((b) => hzDate(b) && hzDate(b) <= cut);
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
    // окно спреда Y-IDX, bps — считается ПОСЛЕ фильтра по объёму: там спред
    // строки уже пересчитан к VWAP-цене тикета, и границы применяются к тому же
    // числу, что видно в таблице
    const sFrom = parseFloat(spreadFrom), sTo = parseFloat(spreadTo);
    if (Number.isFinite(sFrom)) {
      rows = rows.filter((b) => b.yield_over_index_bps != null && b.yield_over_index_bps >= sFrom);
    }
    if (Number.isFinite(sTo)) {
      rows = rows.filter((b) => b.yield_over_index_bps != null && b.yield_over_index_bps <= sTo);
    }
    // умный поиск: токены запроса ищутся по имени/эмитенту/ISIN с допуском
    // опечатки — «РЖД 3» вытаскивает все похожие выпуски эмитента
    const match = makeBondFilter(query);
    if (match) rows = rows.filter(match);
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
  }, [bonds, onlyWatch, basesSel, ratingsSel, emittersSel, hideSub, hideAmort, clsSel, twoSided,
      query, sort, watch, matFrom, matTo, spreadFrom, spreadTo, volOn, volBid, volAsk, volMode, depth]);

  // набор строк таблицы: отфильтрованный + сужение выбором на графике аналитики
  const tableRows = useMemo(() => {
    const m = showAnalytics ? focusMatch(anFocus) : null;
    return m ? filtered.filter(m) : filtered;
  }, [filtered, anFocus, showAnalytics]);

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
      n.delete("sigvol"); n.delete("sigside"); n.delete("sigpx");  // подсветка — только из ленты сигналов
      return n;
    });
  }, [setSearchParams]);

  const closeDrawer = useCallback(() => {
    setSearchParams((sp) => {
      const n = new URLSearchParams(sp);
      ["isin", "k", "ob", "sigvol", "sigside", "sigpx"].forEach((k) => n.delete(k));
      return n;
    });
    const el = lastTriggerRef.current;
    if (el && el.focus) requestAnimationFrame(() => el.focus());
  }, [setSearchParams]);

  // Выбор линий вкладки СРАВНЕНИЕ — в URL (?cmp=ISIN&cmp=…): набор графика
  // уходит в ссылку целиком, вместе с фильтрами. Ключ не в FILTER_KEYS: его
  // чистит только «сброс» самой вкладки, а не панель фильтров.
  const cmpSelKey = searchParams.getAll("cmp").join(",");
  const cmpSel = useMemo(() => (cmpSelKey ? cmpSelKey.split(",") : []), [cmpSelKey]);
  const toggleCmp = useCallback((isin) => {
    setSearchParams((sp) => {
      const n = new URLSearchParams(sp);
      const cur = n.getAll("cmp");
      n.delete("cmp");
      const next = cur.includes(isin)
        ? cur.filter((x) => x !== isin)
        : [...cur, isin].slice(0, CMP_MAX);
      next.forEach((v) => n.append("cmp", v));
      return n;
    }, { replace: true });
  }, [setSearchParams]);
  // массовая установка набора («выбрать все» / «снять все» на вкладке)
  const setCmpAll = useCallback((isins) => {
    setSearchParams((sp) => {
      const n = new URLSearchParams(sp);
      n.delete("cmp");
      isins.slice(0, CMP_MAX).forEach((v) => n.append("cmp", v));
      return n;
    }, { replace: true });
  }, [setSearchParams]);
  const clearCmp = useCallback(() => {
    setSearchParams((sp) => {
      const n = new URLSearchParams(sp);
      n.delete("cmp");
      return n;
    }, { replace: true });
  }, [setSearchParams]);

  const onFloaters = useLocation().pathname.startsWith("/floaters");

  // сколько фильтров активно (для бейджа на кнопке ФИЛЬТРЫ и пустого состояния таблицы)
  // Считаем ОТКЛОНЕНИЯ от дефолтного вида: «без субордов» включён по умолчанию,
  // поэтому в бейдж он попадает только когда СНЯТ (рынок показан целиком).
  const activeFilters = (onlyWatch ? 1 : 0) + (basesSel.length ? 1 : 0) + (ratingsSel.length ? 1 : 0)
    + (emittersSel.length ? 1 : 0) + (twoSided ? 1 : 0) + (hideSub ? 0 : 1) + (query !== "" ? 1 : 0)
    + (hideAmort ? 1 : 0) + (clsSel.length ? 1 : 0)
    + (volBid > 0 || volAsk > 0 ? 1 : 0) + (matFrom !== "" ? 1 : 0) + (matTo !== "" ? 1 : 0)
    + (spreadFrom !== "" ? 1 : 0) + (spreadTo !== "" ? 1 : 0);
  const resetFilters = useCallback(() => {
    setOnlyWatch(false); setBasesSel([]); setRatingsSel([]); setEmittersSel([]);
    setTwoSided(false); setHideSub(true); setHideAmort(false); setClsSel([]);
    setQuery(""); setVolBid(0); setVolAsk(0); setMatFrom(""); setMatTo("");
    setSpreadFrom(""); setSpreadTo("");
  }, []);

  // Панель фильтров общая у МОНИТОРА и СРАВНЕНИЯ: состояние одно, значит
  // переключение вкладок не сбрасывает отбор. withCols=false прячет меню
  // столбцов — на СРАВНЕНИИ витрина своя.
  const toolbar = (withCols) => (
      <Toolbar
        onlyWatch={onlyWatch} setOnlyWatch={setOnlyWatch}
        basesSel={basesSel} toggleBase={toggleIn(setBasesSel)}
        ratingsSel={ratingsSel} toggleRating={toggleIn(setRatingsSel)}
        clearBases={() => setBasesSel([])}
        issuers={issuers} emittersSel={emittersSel} toggleEmitter={toggleIn(setEmittersSel)}
        clearEmitters={() => setEmittersSel([])}
        activeFilters={activeFilters} onResetFilters={resetFilters}
        twoSided={twoSided} setTwoSided={setTwoSided}
        hideSub={hideSub} setHideSub={setHideSub}
        hideAmort={hideAmort} setHideAmort={setHideAmort}
        clsSel={clsSel} toggleCls={toggleIn(setClsSel)}
        volBid={volBid} setVolBid={setVolBid} volAsk={volAsk} setVolAsk={setVolAsk}
        volMode={volMode} setVolMode={setVolMode}
        depthTs={depthQ.data?.ts} depthLoading={volOn && depthQ.isLoading}
        matFrom={matFrom} setMatFrom={setMatFrom} matTo={matTo} setMatTo={setMatTo}
        spreadFrom={spreadFrom} setSpreadFrom={setSpreadFrom}
        spreadTo={spreadTo} setSpreadTo={setSpreadTo}
        query={query} setQuery={setQuery} searchRef={searchRef}
        watchCount={watch.length}
        shown={tableRows.length} total={bonds.length}
        visibleCols={withCols ? visibleCols : null}
        onToggleCol={toggleCol} onResetCols={resetCols} onMoveCol={moveCol}
      />
  );

  const floatersView = (
    <>
      {toolbar(true)}
      {showAnalytics && <AnalyticsPanel rows={filtered} focus={anFocus} onFocus={setAnFocus} />}
      <BondTable
        rows={tableRows}
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

  const compareView = (
    <>
      {toolbar(false)}
      <CompareModule rows={filtered} sel={cmpSel} onToggle={toggleCmp} onSetAll={setCmpAll}
        onClear={clearCmp} onOpen={openDrawer} />
    </>
  );

  return (
    <PageStatusProvider>
    <div id="app" className={theme === "light" ? "" : "theme-" + theme}>
      <Topbar
        user={user}
        onLogout={onLogout}
        onOpenSettings={() => setShowSettings(true)}
        extra={onFloaters && (
          <button className={"seg-btn tool-btn" + (showAnalytics ? " active" : "")}
            onClick={() => { setShowAnalytics(!showAnalytics); setAnFocus(null); }}
            aria-pressed={showAnalytics}
            title="Аналитика — кросс-секция рынка над таблицей">
            <IconChart size={12} /> Аналитика
          </button>
        )}
      />
      <Routes>
        <Route path="/" element={<Navigate to="/floaters" replace />} />
        <Route path="/floaters" element={floatersView} />
        <Route path="/compare" element={compareView} />
        {/* Вкладка «Эмитенты» снята: агрегаты повторяли фильтр по эмитенту в
            «Мониторе», а разрез по медианам спреда даёт «Аналитика» (распределение
            R-spread по эмитентам). Старый путь ведёт в Монитор — на вкладку могли
            остаться закладки. */}
        <Route path="/issuers" element={<Navigate to="/floaters" replace />} />
        <Route path="/reference" element={<Catalog user={user} />} />
        <Route path="/fixed" element={<FixedModule onOpen={openDrawer} />} />
        <Route path="/calc" element={<CalcModule />} />
        <Route path="/calc/float" element={<CalcModule initialKind="float" />} />
        <Route path="/trades" element={<TradesTape />} />
        {/* КРУПНЫЕ слиты в СДЕЛКИ: лента одна, крупняк отбирается порогом суммы.
            Старый путь отдаёт ту же ленту — на вкладку успели наставить закладок.
            Именно КОМПОНЕНТ, а не <Navigate>: редирект здесь проигрывает гонку
            эффекту, который синхронизирует фильтры дашборда с query string, и
            путь тут же возвращается назад — страница остаётся пустой. */}
        <Route path="/blocks" element={<TradesTape />} />
        <Route path="/signals" element={<SignalsModule />} />
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
      <Drawer isin={drawerIsin} kind={searchParams.get("k")} autoOrderbook={searchParams.get("ob") !== "0"}
        sigVol={Number(searchParams.get("sigvol")) || 0} sigSide={searchParams.get("sigside") || "ask"}
        sigPx={Number(searchParams.get("sigpx")) || 0}
        // фильтр по объёму с МОНИТОРА — в стакане карточки подсвечиваются
        // уровни, набирающие ровно этот тикет по своей стороне
        volBid={volBid} volAsk={volAsk}
        onClose={closeDrawer} />
      <StatusBar count={bonds.length} kpiBonds={tableRows} live={live} sources={meta.source_status}
        theme={theme} onSetTheme={setTheme} meta={meta}
        onRefresh={() => { fetchMeta().then(setMeta).catch(() => {}); loadBonds(); }} />
      {showSettings && <AdminPanel user={user} onClose={() => setShowSettings(false)} />}
      <SignalsWatcher />
    </div>
    </PageStatusProvider>
  );
}

// Тема из localStorage. Серая тема заменена на win (old internet) — у кого в
// хранилище остался "grey", молча переезжает, иначе класса .theme-grey больше
// нет и интерфейс падает в светлую.
function readTheme() {
  const t = localStorage.getItem("theme") || "light";
  return t === "grey" ? "win" : t;
}

// Гейт авторизации: пока не проверили сессию — заглушка; нет сессии — Login; есть — дашборд.
function AuthGate() {
  const { auth, onLogin } = useAuth();
  const [theme] = useState(readTheme);

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
