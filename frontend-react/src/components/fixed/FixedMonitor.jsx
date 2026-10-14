import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { fetchDepth } from "../../api.js";
import { useFixedFeed } from "../../useFixedFeed.js";
import { fmt, ratingMatches, ratingOptions, yearsToIso } from "../../format.js";
import { filterBonds } from "../../search.js";
import { filterByAdv } from "../../tableRows.js";
import { horizonDate } from "../../horizon.js";
import { applyVolume, FIXED_VOL_FIELDS } from "../../vwap.js";
import { sideProgress } from "../../spreadProgress.js";
import { usePageStatus } from "../../pageStatus.jsx";
import Toolbar from "../Toolbar.jsx";
import FixedTable, { useFixedCols } from "./FixedTable.jsx";
import FixedAnalytics from "../FixedAnalytics.jsx";
import { DEFAULT_FIXED_CURRENCIES, fixedCurrency, isDefaultFixedCurrencySelection } from "./currency.js";

// МОНИТОР ФИКСОВ — та же витрина, что у флоатеров (App.Dashboard), но по
// облигациям с фиксированным купоном: вместо spread/DM первичны ДВЕ метрики —
// g-спред к КБД ОФЗ и доходность к погашению.
//
// Состояние СВОЁ, не общее с флоатерами: ключи URL с префиксом fx, ключи
// localStorage с суффиксом _fx. Иначе переключение типа бумаг тащило бы за
// собой чужой отбор (у фиксов нет ни базы купона, ни бумаг того же эмитента).
const FILTER_KEYS = ["fxq", "fxw", "fxrt", "fxem", "fxcls", "fxnosub", "fxnoam",
                     "fxmyf", "fxmyt", "fxgf", "fxgt", "fxyf", "fxyt", "fxtwo",
                     "fxvb", "fxva", "fxvm", "fxadv", "fxadvm", "fxccy"];
// Суборды/перпы — по имени выпуска, тот же паттерн, что у флоатеров (App.jsx)
// и у скринера (services/screener_core.py::_SUBORD_RE).
const SUBORD_RE = /СУБ|SUB|ПЕРП|PERP|(?<![A-ZА-Я0-9])[TТ]1(?![0-9])/i;

const initialParams = () => new URLSearchParams(window.location.search);
const ls = (k, d = "") => localStorage.getItem(k) ?? d;

const median = (a) => {
  if (!a.length) return null;
  const s = a.slice().sort((x, y) => x - y);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const quantile = (a, p) => {
  if (!a.length) return null;
  const s = a.slice().sort((x, y) => x - y);
  return s[Math.min(s.length - 1, Math.floor(p * s.length))];
};

// Правила слияния живых данных живут в fixedLive.js; реэкспорт — для тестов и
// внешних потребителей, импортировавших их отсюда.
export { applyPatch, applyPrice, applyVolQuote, mergeLive } from "../../fixedLive.js";

export default function FixedMonitor({ onOpen, showAnalytics }) {
  const [searchParams, setSearchParams] = useSearchParams();

  const [query, setQuery] = useState(() => initialParams().get("fxq") || "");
  const [onlyWatch, setOnlyWatch] = useState(() => initialParams().get("fxw") === "1");
  const [ratingsSel, setRatingsSel] = useState(() => initialParams().getAll("fxrt"));
  const [emittersSel, setEmittersSel] = useState(() => initialParams().getAll("fxem"));
  const [clsSel, setClsSel] = useState(() => initialParams().getAll("fxcls"));
  // Витрина стартует с рублёвых выпусков. Явный URL имеет приоритет над
  // сохранённым выбором, чтобы ссылкой можно было открыть конкретную валюту.
  const [currenciesSel, setCurrenciesSel] = useState(() => {
    const p = initialParams();
    if (p.has("fxccy")) return p.getAll("fxccy");
    try {
      const saved = JSON.parse(localStorage.getItem("currencies_fx") || "null");
      return Array.isArray(saved) && saved.length ? saved : DEFAULT_FIXED_CURRENCIES;
    } catch { return DEFAULT_FIXED_CURRENCIES; }
  });
  // односторонний рынок торговать нечем — тот же чип BID×OFFER, что у флоатеров
  const [twoSided, setTwoSided] = useState(() => initialParams().get("fxtwo") === "1");
  // размеры тикета по сторонам, ₽ (0 = сторона не фильтруется): котировка
  // стороны пересчитывается в средневзвешенную цену набора по лестнице стакана,
  // а g-спред к этой цене считает движок (см. universe_stream._crunch_fixed)
  const [volBid, setVolBid] = useState(() => Number(initialParams().get("fxvb"))
    || Number(ls("volBidRub_fx", "0")) || 0);
  const [volAsk, setVolAsk] = useState(() => Number(initialParams().get("fxva"))
    || Number(ls("volAskRub_fx", "0")) || 0);
  const [volMode, setVolMode] = useState(() => (initialParams().get("fxvm")
    || ls("volMode_fx")) === "or" ? "or" : "and");
  // суборды вон по умолчанию — как у флоатеров: другой класс риска, спред к ним
  // не сравним со старшим долгом
  const [hideSub, setHideSub] = useState(() => {
    const p = initialParams();
    if (p.has("fxnosub")) return p.get("fxnosub") === "1";
    const v = localStorage.getItem("hideSubord_fx");
    return v === null ? true : v === "1";
  });
  const [hideAmort, setHideAmort] = useState(() => {
    const p = initialParams();
    if (p.has("fxnoam")) return p.get("fxnoam") === "1";
    return ls("hideAmort_fx") === "1";
  });
  const [matFrom, setMatFrom] = useState(() => initialParams().get("fxmyf") || ls("matYrsFrom_fx"));
  const [matTo, setMatTo] = useState(() => initialParams().get("fxmyt") || ls("matYrsTo_fx"));
  const [spreadFrom, setSpreadFrom] = useState(() => initialParams().get("fxgf") || ls("gSpreadFrom_fx"));
  const [spreadTo, setSpreadTo] = useState(() => initialParams().get("fxgt") || ls("gSpreadTo_fx"));
  const [ytmFrom, setYtmFrom] = useState(() => initialParams().get("fxyf") || ls("ytmFrom_fx"));
  const [ytmTo, setYtmTo] = useState(() => initialParams().get("fxyt") || ls("ytmTo_fx"));
  // порог ликвидности по ADV, млн ₽ (как в колонке); сторона сравнения — gte/lte
  const [advMin, setAdvMin] = useState(() => initialParams().get("fxadv") || ls("advMin_fx"));
  const [advMode, setAdvMode] = useState(() => ((initialParams().get("fxadvm")
    || ls("advMode_fx")) === "lte" ? "lte" : "gte"));
  const [sort, setSort] = useState({ key: "g_spread_bps", dir: "asc" });
  const [watch, setWatch] = useState(() => {
    try { return JSON.parse(localStorage.getItem("watch_fx") || "[]"); } catch { return []; }
  });
  // Колонки (видимость/порядок/ширины) живут в общей таблице фиксов: ключи
  // localStorage те же, что были здесь до её выделения (cols_fx/colw_fx/
  // cols_known_fx), витрина ОФЗ держит свои под другим префиксом.
  const cols = useFixedCols({ storageKey: "fx" });
  const searchRef = useRef(null);

  // «/» — фокус в поиск (терминальная привычка, как на мониторе флоатеров).
  // Игнорируем, когда юзер уже печатает в поле, иначе слэш не набрать.
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

  // фильтры → query string (свои ключи; чужие параметры, включая ?isin= карточки
  // и фильтры флоатеров, не трогаем). replace — набор в поиске не должен
  // плодить записи в истории.
  useEffect(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      FILTER_KEYS.forEach((k) => next.delete(k));
      if (query) next.set("fxq", query);
      if (onlyWatch) next.set("fxw", "1");
      ratingsSel.forEach((v) => next.append("fxrt", v));
      emittersSel.forEach((v) => next.append("fxem", v));
      clsSel.forEach((v) => next.append("fxcls", v));
      currenciesSel.forEach((v) => next.append("fxccy", v));
      if (hideSub) next.set("fxnosub", "1");
      if (hideAmort) next.set("fxnoam", "1");
      if (twoSided) next.set("fxtwo", "1");
      if (volBid) next.set("fxvb", String(volBid));
      if (volAsk) next.set("fxva", String(volAsk));
      if (volMode === "or" && (volBid || volAsk)) next.set("fxvm", "or");
      if (matFrom) next.set("fxmyf", matFrom);
      if (matTo) next.set("fxmyt", matTo);
      if (spreadFrom) next.set("fxgf", spreadFrom);
      if (spreadTo) next.set("fxgt", spreadTo);
      if (ytmFrom) next.set("fxyf", ytmFrom);
      if (ytmTo) next.set("fxyt", ytmTo);
      if (advMin) next.set("fxadv", advMin);
      if (advMin && advMode === "lte") next.set("fxadvm", "lte");
      return next;
    }, { replace: true });
  }, [query, onlyWatch, ratingsSel, emittersSel, clsSel, currenciesSel, hideSub, hideAmort, twoSided,
      volBid, volAsk, volMode,
      matFrom, matTo, spreadFrom, spreadTo, ytmFrom, ytmTo, advMin, advMode, setSearchParams]);

  useEffect(() => { localStorage.setItem("hideSubord_fx", hideSub ? "1" : "0"); }, [hideSub]);
  useEffect(() => { localStorage.setItem("hideAmort_fx", hideAmort ? "1" : "0"); }, [hideAmort]);
  useEffect(() => { localStorage.setItem("matYrsFrom_fx", matFrom); }, [matFrom]);
  useEffect(() => { localStorage.setItem("matYrsTo_fx", matTo); }, [matTo]);
  useEffect(() => { localStorage.setItem("gSpreadFrom_fx", spreadFrom); }, [spreadFrom]);
  useEffect(() => { localStorage.setItem("gSpreadTo_fx", spreadTo); }, [spreadTo]);
  useEffect(() => { localStorage.setItem("ytmFrom_fx", ytmFrom); }, [ytmFrom]);
  useEffect(() => { localStorage.setItem("ytmTo_fx", ytmTo); }, [ytmTo]);
  useEffect(() => { localStorage.setItem("advMin_fx", advMin); }, [advMin]);
  useEffect(() => { localStorage.setItem("advMode_fx", advMode); }, [advMode]);
  useEffect(() => { localStorage.setItem("volBidRub_fx", String(volBid)); }, [volBid]);
  useEffect(() => { localStorage.setItem("volAskRub_fx", String(volAsk)); }, [volAsk]);
  useEffect(() => { localStorage.setItem("volMode_fx", volMode); }, [volMode]);
  useEffect(() => { localStorage.setItem("currencies_fx", JSON.stringify(currenciesSel)); }, [currenciesSel]);
  useEffect(() => { localStorage.setItem("watch_fx", JSON.stringify(watch)); }, [watch]);

  // ── данные ──
  const { bonds, listQ, quotesQ } = useFixedFeed({ volBid, volAsk });

  // Лестницы стаканов — только когда фильтр по объёму включён: ответ тяжёлый
  // (весь рынок × 20 уровней), а без фильтра он не нужен. Бэк держит их
  // push-свежими (depth-пул universe_stream), поэтому такт частый.
  const volOn = volBid > 0 || volAsk > 0;
  const depthQ = useQuery({
    queryKey: ["depth"], queryFn: fetchDepth, refetchInterval: 15000,
    enabled: volOn, staleTime: 10000,
  });
  const depth = depthQ.data?.items;

  // Живая лента фиксов (список, котировки, сокет, наложение) — в useFixedFeed:
  // те же правила слияния обслуживают и общий монитор облигаций.

  const issuers = useMemo(() => {
    const m = new Map();
    for (const b of bonds) {
      if (!b.emitter_name) continue;
      m.set(b.emitter_name, (m.get(b.emitter_name) || 0) + 1);
    }
    return [...m.entries()].map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }, [bonds]);

  const currencies = useMemo(() => {
    const all = new Set(["RUB", ...currenciesSel]);
    bonds.forEach((b) => all.add(fixedCurrency(b)));
    return [...all].sort((a, b) => (a === "RUB" ? -1 : b === "RUB" ? 1 : a.localeCompare(b)));
  }, [bonds, currenciesSel]);

  const rows = useMemo(() => {
    let r = bonds.slice();
    if (onlyWatch) r = r.filter((b) => watch.includes(b.isin));
    if (ratingsSel.length) r = r.filter((b) => ratingMatches(b.rating, ratingsSel));
    if (emittersSel.length) r = r.filter((b) => emittersSel.includes(b.emitter_name));
    // Пустой набор — явный «все валюты». Первый вход и сброс — только RUB.
    if (currenciesSel.length) r = r.filter((b) => currenciesSel.includes(fixedCurrency(b)));
    if (hideSub) r = r.filter((b) => !SUBORD_RE.test(b.short_name || ""));
    if (hideAmort) r = r.filter((b) => !b.has_amort);
    if (clsSel.length) r = r.filter((b) => clsSel.includes(b.is_ofz ? "OFZ" : "CORP"));
    // ФИЛЬТР ПО ОБЪЁМУ: котировка заполненной стороны становится VWAP на её
    // тикет по лестнице, g-спред — числом движка к этой цене. volMode решает
    // судьбу строки, когда заполнены оба поля. Чип BID×OFFER применяется
    // следующим и может дополнительно потребовать обе стороны.
    if (volOn && depth) {
      r = r.map((b) => applyVolume(b, depth[b.isin], volBid, volAsk, volMode, FIXED_VOL_FIELDS))
           .filter(Boolean);
    }
    if (twoSided) r = r.filter((b) => b.bid != null && b.ask != null);
    // Окно срока — до ГОРИЗОНТА, к которому посчитаны метрики строки: оферта,
    // если поток обрезан на ней (put_date), иначе погашение.
    // срок — до той даты, к которой посчитаны метрики строки (у фикса поток
    // обрывается на оферте). Правило общее с монитором флоатеров, см. horizon.js
    const hzDate = horizonDate;
    const mFrom = parseFloat(matFrom), mTo = parseFloat(matTo);
    if (Number.isFinite(mFrom)) {
      const cut = yearsToIso(mFrom);
      r = r.filter((b) => hzDate(b) && hzDate(b) >= cut);
    }
    if (Number.isFinite(mTo)) {
      const cut = yearsToIso(mTo);
      r = r.filter((b) => hzDate(b) && hzDate(b) <= cut);
    }
    // окна первичных метрик: бумаги без посчитанного числа при заданной границе
    // скрыты — прочерк не должен молча пролезать в любой диапазон
    const win = (key, from, to) => {
      const f = parseFloat(from), t = parseFloat(to);
      if (Number.isFinite(f)) r = r.filter((b) => b[key] != null && b[key] >= f);
      if (Number.isFinite(t)) r = r.filter((b) => b[key] != null && b[key] <= t);
    };
    win("g_spread_bps", spreadFrom, spreadTo);
    win("ytm", ytmFrom, ytmTo);
    // порог ликвидности по ADV — то же правило, что у флоатеров (tableRows.js)
    r = filterByAdv(r, parseFloat(advMin), advMode);
    r = filterBonds(r, query);
    const { key, dir } = sort;
    const m = dir === "asc" ? 1 : -1;
    r.sort((a, b) => {
      // СРОК сортируется по горизонту — по той же дате, к которой посчитаны
      // метрики строки и по которой отбирает окно срока (см. horizon.js).
      // По дате погашения список спорил бы с числом, подсвеченным в строке.
      const x = key === "maturity_date" ? hzDate(a) : a[key];
      const y = key === "maturity_date" ? hzDate(b) : b[key];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      if (typeof x === "string") return x.localeCompare(y) * m;
      return (x - y) * m;
    });
    return r;
  }, [bonds, onlyWatch, watch, ratingsSel, emittersSel, currenciesSel, hideSub, hideAmort, clsSel, twoSided,
      volOn, volBid, volAsk, volMode, depth,
      matFrom, matTo, spreadFrom, spreadTo, ytmFrom, ytmTo, advMin, advMode, query, sort]);

  // ПРОГРЕСС ЗАПОЛНЕНИЯ g-спредов — заливкой в заголовках колонок BID/OFFER, как
  // у флоатеров: движок общий, ждать приходится одинаково. Имена полей у фикса
  // свои (FIXED_VOL_FIELDS).
  const F = FIXED_VOL_FIELDS;
  const colProgress = useMemo(() => ({
    [F.bidSpread]: sideProgress(rows, F.bidPx, F.bidSpread),
    [F.askSpread]: sideProgress(rows, F.askPx, F.askSpread),
  }), [rows, F.bidPx, F.bidSpread, F.askPx, F.askSpread]);

  // Итоги выборки — в общую нижнюю полосу, как у флоатеров (Kpis/StatusBar),
  // а не отдельным блоком-сеткой над таблицей: полоса в приложении одна.
  const ys = rows.map((b) => b.ytm).filter((v) => v != null);
  const gs = rows.map((b) => b.g_spread_bps).filter((v) => v != null);
  usePageStatus([
    { k: "ОФЗ", v: rows.filter((b) => b.is_ofz).length, title: "гособлигаций в выборке" },
    { k: "КОРП", v: rows.filter((b) => !b.is_ofz).length, title: "корпоративных выпусков в выборке" },
    { k: "MED YTM", v: ys.length ? fmt.pct(median(ys)) : null,
      title: "медианная доходность к погашению выборки, % годовых" },
    { k: "MED G-SPRD", v: gs.length ? fmt.bps(median(gs)) : null,
      title: "медианный g-спред к КБД ОФЗ, б.п." },
    { k: "P25–P75", v: gs.length ? `${fmt.bps(quantile(gs, 0.25))}–${fmt.bps(quantile(gs, 0.75))}` : null,
      title: "межквартильный разброс g-спреда, б.п.", cls: "sec" },
  ]);

  const toggleIn = (setter) => (val) =>
    setter((arr) => (arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val]));
  const onSort = useCallback((key) => {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  }, []);
  const toggleStar = useCallback((isin) =>
    setWatch((w) => (w.includes(isin) ? w.filter((x) => x !== isin) : [...w, isin])), []);

  const activeFilters = (onlyWatch ? 1 : 0) + (ratingsSel.length ? 1 : 0)
    + (emittersSel.length ? 1 : 0) + (clsSel.length ? 1 : 0) + (hideSub ? 0 : 1)
    + (isDefaultFixedCurrencySelection(currenciesSel) ? 0 : 1)
    + (hideAmort ? 1 : 0) + (query !== "" ? 1 : 0) + (twoSided ? 1 : 0)
    + (volBid > 0 || volAsk > 0 ? 1 : 0)
    + (matFrom !== "" ? 1 : 0) + (matTo !== "" ? 1 : 0)
    + (spreadFrom !== "" ? 1 : 0) + (spreadTo !== "" ? 1 : 0)
    + (ytmFrom !== "" ? 1 : 0) + (ytmTo !== "" ? 1 : 0) + (advMin !== "" ? 1 : 0);
  const resetFilters = useCallback(() => {
    setOnlyWatch(false); setRatingsSel([]); setEmittersSel([]); setClsSel([]);
    setCurrenciesSel(DEFAULT_FIXED_CURRENCIES);
    setHideSub(true); setHideAmort(false); setQuery(""); setTwoSided(false);
    setVolBid(0); setVolAsk(0);
    setMatFrom(""); setMatTo(""); setSpreadFrom(""); setSpreadTo("");
    setYtmFrom(""); setYtmTo(""); setAdvMin("");
  }, []);

  // ступени рейтинга витрины (меню «▾» рядом с чипами грейдов) — до фильтров
  const ratingOpts = useMemo(() => ratingOptions(bonds.map((b) => b.rating)), [bonds]);

  const status = listQ.isPending ? "loading" : listQ.error ? "error" : "ready";

  return (
    <>
      <Toolbar
        onlyWatch={onlyWatch} setOnlyWatch={setOnlyWatch}
        ratingsSel={ratingsSel} toggleRating={toggleIn(setRatingsSel)} ratingOpts={ratingOpts}
        issuers={issuers} emittersSel={emittersSel} toggleEmitter={toggleIn(setEmittersSel)}
        clearEmitters={() => setEmittersSel([])}
        activeFilters={activeFilters} onResetFilters={resetFilters}
        hideSub={hideSub} setHideSub={setHideSub}
        hideAmort={hideAmort} setHideAmort={setHideAmort}
        clsSel={clsSel} toggleCls={toggleIn(setClsSel)}
        currencies={currencies} currenciesSel={currenciesSel}
        toggleCurrency={toggleIn(setCurrenciesSel)}
        resetCurrencies={() => setCurrenciesSel(DEFAULT_FIXED_CURRENCIES)}
        twoSided={twoSided} setTwoSided={setTwoSided}
        volBid={volBid} setVolBid={setVolBid} volAsk={volAsk} setVolAsk={setVolAsk}
        volMode={volMode} setVolMode={setVolMode}
        depthTs={depthQ.data?.ts} depthLoading={volOn && depthQ.isLoading}
        matFrom={matFrom} setMatFrom={setMatFrom} matTo={matTo} setMatTo={setMatTo}
        spreadLabel="G-спред"
        spreadFrom={spreadFrom} setSpreadFrom={setSpreadFrom}
        spreadTo={spreadTo} setSpreadTo={setSpreadTo}
        ytmFrom={ytmFrom} setYtmFrom={setYtmFrom} ytmTo={ytmTo} setYtmTo={setYtmTo}
        advMin={advMin} setAdvMin={setAdvMin} advMode={advMode} setAdvMode={setAdvMode}
        query={query} setQuery={setQuery} searchRef={searchRef}
        watchCount={watch.length}
        shown={rows.length} total={bonds.length}
        visibleCols={cols.visibleCols} colsMeta={cols.colsMeta}
        onToggleCol={cols.onToggleCol} onResetCols={cols.onResetCols} onMoveCol={cols.onMoveCol}
      />

      {showAnalytics && rows.length > 0 && <FixedAnalytics rows={rows} />}

      <FixedTable
        rows={rows}
        status={status}
        errMsg={listQ.error ? String(listQ.error.message || listQ.error) : ""}
        sort={sort}
        onSort={onSort}
        onOpen={onOpen}
        watch={watch}
        onToggleStar={toggleStar}
        filtered={activeFilters > 0}
        onClearFilters={resetFilters}
        onRetry={listQ.refetch}
        colProgress={colProgress}
        {...cols}
      />
    </>
  );
}
