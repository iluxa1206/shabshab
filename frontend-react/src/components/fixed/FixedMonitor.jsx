import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { connectMarketWs, fetchDepth, fetchFixed, fetchFixedQuotes } from "../../api.js";
import { fmt, ratingMatches, ratingOptions, yearsToIso } from "../../format.js";
import { makeBondFilter } from "../../search.js";
import { applyVolume, FIXED_VOL_FIELDS } from "../../vwap.js";
import { usePageStatus } from "../../pageStatus.jsx";
import Toolbar from "../Toolbar.jsx";
import BondTable from "../BondTable.jsx";
import FixedAnalytics from "../FixedAnalytics.jsx";
import { FIXED_COLS, FIXED_COL_META, FIXED_DEFAULT_COLS } from "./fixedCols.jsx";

// МОНИТОР ФИКСОВ — та же витрина, что у флоатеров (App.Dashboard), но по
// облигациям с фиксированным купоном: вместо R-spread/DM первичны ДВЕ метрики —
// g-спред к КБД ОФЗ и доходность к погашению.
//
// Состояние СВОЁ, не общее с флоатерами: ключи URL с префиксом fx, ключи
// localStorage с суффиксом _fx. Иначе переключение типа бумаг тащило бы за
// собой чужой отбор (у фиксов нет ни базы купона, ни бумаг того же эмитента).
const FILTER_KEYS = ["fxq", "fxw", "fxrt", "fxem", "fxcls", "fxnosub", "fxnoam",
                     "fxmyf", "fxmyt", "fxgf", "fxgt", "fxyf", "fxyt", "fxtwo",
                     "fxvb", "fxva", "fxvm"];
// Суборды/перпы — по имени выпуска, тот же паттерн, что у флоатеров (App.jsx)
// и у скринера (services/screener_core.py::_SUBORD_RE).
const SUBORD_RE = /СУБ|SUB|ПЕРП|PERP|(?<![A-ZА-Я0-9])[TТ]1(?![0-9])/i;
const QUOTES_POLL_MS = 5000;
// Коалесцируем push'и: в ликвидной сессии их сотни в секунду, а перерисовывать
// таблицу чаще, чем видит глаз, незачем (тот же такт, что у монитора флоатеров).
const WS_FLUSH_MS = 400;
// Сколько бумага считается «живой» после последнего пуша. Замолчала (стрим лёг,
// упёрлись в лимит подписок, торгов нет) — строка снова живёт снапшотом
// котировок, иначе в ней навсегда застыли бы последние цифры стрима.
const LIVE_FRESH_MS = 15000;

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

// Метрики фикса, которые движок пересчитывает по цене и шлёт патчем. Явный
// null в патче СТИРАЕТ число (сторона ушла из книги, контекст остыл) — поэтому
// копируем по наличию ключа, а не по «не пусто».
const PATCH_METRIC_KEYS = [
  "ytm", "ytm_bid", "ytm_ask", "ytm_wap", "cur_yield",
  "g_spread_bps", "g_spread_bid_bps", "g_spread_ask_bps", "g_spread_wap_bps",
  "z_spread_bps", "dirty", "mod_dur", "mac_dur", "convexity", "dv01",
  "delta_to_prev_close", "price_stale",
  // средневзвес едет ВМЕСТЕ со своим спредом: пара «цена → спред» обязана быть
  // из одного расчёта, иначе в строке окажется спред по другой цене
  "wap_pct",
];

/** Новая цена в строку вместе с гашением того, что от неё производно: цена
 *  приезжает тиком, а метрики считает бэк своим тактом, и без гашения строка
 *  показывала бы свежую цену со спредом от прежней. full=true — цена стороны
 *  (полный снимок верха стакана: пусто значит «стороны нет»). */
export function applyPrice(row, field, px, derived, full = false) {
  // Пусто в НЕсторонней цене — это «новости нет» (снапшот не знает цены), а не
  // «цены не стало»: затирать ею цену строки значит выбросить prev-close,
  // которым живёт неликвид. У сторон стакана наоборот — снимок полный.
  const next = full ? (px ?? null) : px;
  if (next == null && !full) return;
  if (next === row[field]) return;
  row[field] = next;
  for (const k of derived) row[k] = null;
}

/** Наложение патча стрима на прежнее наложение той же бумаги. */
export function applyPatch(cur, q) {
  const n = { ...(cur || {}), _live: true };
  if ("bid" in q) n.bid = q.bid ?? null;
  if ("ask" in q) n.ask = q.ask ?? null;
  if (q.vwap_pct != null) n.wap_pct = q.vwap_pct;
  // оборот назад не откатываем: патч может прийти от сокета, чей дневной
  // агрегат ещё догоняется архивом
  if (q.val_today != null && !(n.val_today > q.val_today)) n.val_today = q.val_today;
  // цена сделки: null в патче значит «сделок сегодня не было», а не «цены
  // больше нет» — строка остаётся на прежней (см. applyPrice)
  if (q.last_price_pct != null) n.last_price_pct = q.last_price_pct;
  if (q.metrics) {
    for (const k of PATCH_METRIC_KEYS) if (k in q) n[k] = q[k];
    n._mstale = false;
  } else {
    // патч без метрик — цены новее производных: гасим то, что от них зависит
    if ("bid" in q) { n.g_spread_bid_bps = null; n.ytm_bid = null; }
    if ("ask" in q) { n.g_spread_ask_bps = null; n.ytm_ask = null; }
    if (q.last_price_pct != null) n._mstale = true;
  }
  return n;
}

/** Числа набора на объём из ответа котировок (плоские ключи vol_bid_px/…) — в
 *  поля строки витрины. Пусто значит «движок ещё не посчитал»: прежнее число не
 *  трогаем, его гасит перезагрузка списка при смене размера. */
export function applyVolQuote(row, it) {
  for (const side of ["bid", "ask"]) {
    const px = it[`vol_${side}_px`], g = it[`vol_${side}_g`], y = it[`vol_${side}_ytm`];
    if (px != null) row[`vol_${side}_price_pct`] = px;
    if (g != null) row[`g_spread_vol_${side}_bps`] = g;
    if (y != null) row[`ytm_vol_${side}`] = y;
  }
}

/** Поверхностное сравнение строк: одинаковый набор ключей и значений. */
function sameRow(a, b) {
  if (!a || !b) return false;
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  for (const k of ka) if (a[k] !== b[k]) return false;
  return true;
}

export default function FixedMonitor({ onOpen, showAnalytics }) {
  const [searchParams, setSearchParams] = useSearchParams();

  const [query, setQuery] = useState(() => initialParams().get("fxq") || "");
  const [onlyWatch, setOnlyWatch] = useState(() => initialParams().get("fxw") === "1");
  const [ratingsSel, setRatingsSel] = useState(() => initialParams().getAll("fxrt"));
  const [emittersSel, setEmittersSel] = useState(() => initialParams().getAll("fxem"));
  const [clsSel, setClsSel] = useState(() => initialParams().getAll("fxcls"));
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
  const [sort, setSort] = useState({ key: "g_spread_bps", dir: "asc" });
  const [watch, setWatch] = useState(() => {
    try { return JSON.parse(localStorage.getItem("watch_fx") || "[]"); } catch { return []; }
  });
  const [visibleCols, setVisibleCols] = useState(() => {
    try {
      const s = JSON.parse(localStorage.getItem("cols_fx") || "null");
      if (!Array.isArray(s) || !s.length) return FIXED_DEFAULT_COLS;
      // колонки, добавленные после последнего сохранения набора, показываем
      const known = new Set(JSON.parse(localStorage.getItem("cols_known_fx") || "[]"));
      const fresh = FIXED_DEFAULT_COLS.filter((k) => !known.has(k) && !s.includes(k));
      return fresh.length ? [...s, ...fresh] : s;
    } catch { return FIXED_DEFAULT_COLS; }
  });
  const [colWidths, setColWidths] = useState(() => {
    try { return JSON.parse(localStorage.getItem("colw_fx") || "{}") || {}; } catch { return {}; }
  });
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
      return next;
    }, { replace: true });
  }, [query, onlyWatch, ratingsSel, emittersSel, clsSel, hideSub, hideAmort, twoSided,
      volBid, volAsk, volMode,
      matFrom, matTo, spreadFrom, spreadTo, ytmFrom, ytmTo, setSearchParams]);

  useEffect(() => { localStorage.setItem("hideSubord_fx", hideSub ? "1" : "0"); }, [hideSub]);
  useEffect(() => { localStorage.setItem("hideAmort_fx", hideAmort ? "1" : "0"); }, [hideAmort]);
  useEffect(() => { localStorage.setItem("matYrsFrom_fx", matFrom); }, [matFrom]);
  useEffect(() => { localStorage.setItem("matYrsTo_fx", matTo); }, [matTo]);
  useEffect(() => { localStorage.setItem("gSpreadFrom_fx", spreadFrom); }, [spreadFrom]);
  useEffect(() => { localStorage.setItem("gSpreadTo_fx", spreadTo); }, [spreadTo]);
  useEffect(() => { localStorage.setItem("ytmFrom_fx", ytmFrom); }, [ytmFrom]);
  useEffect(() => { localStorage.setItem("ytmTo_fx", ytmTo); }, [ytmTo]);
  useEffect(() => { localStorage.setItem("volBidRub_fx", String(volBid)); }, [volBid]);
  useEffect(() => { localStorage.setItem("volAskRub_fx", String(volAsk)); }, [volAsk]);
  useEffect(() => { localStorage.setItem("volMode_fx", volMode); }, [volMode]);
  useEffect(() => { localStorage.setItem("watch_fx", JSON.stringify(watch)); }, [watch]);
  useEffect(() => { localStorage.setItem("cols_fx", JSON.stringify(visibleCols)); }, [visibleCols]);
  useEffect(() => { localStorage.setItem("colw_fx", JSON.stringify(colWidths)); }, [colWidths]);
  useEffect(() => { localStorage.setItem("cols_known_fx", JSON.stringify(FIXED_DEFAULT_COLS)); }, []);

  // ── данные ──
  // метрики (YTM/спреды/дюрация) — свой цикл прогрева на бэке, тянем реже;
  // цены и стакан — тактом 5 с отдельной лёгкой ручкой
  const listQ = useQuery({
    // размеры тикета — часть ключа: сменил объём, значит нужны другие цены
    // наборов (их считает движок, ручка только выбирает нужный размер)
    queryKey: ["fixed", volBid, volAsk],
    queryFn: () => fetchFixed({ volBid, volAsk }),
    staleTime: 30_000, refetchInterval: 60_000, placeholderData: (prev) => prev,
  });
  // размер тикета — часть ключа: числа набора от прошлого размера относятся к
  // прошлому фильтру
  const quotesQ = useQuery({
    queryKey: ["fixed-quotes", volBid, volAsk],
    queryFn: () => fetchFixedQuotes(volBid, volAsk),
    refetchInterval: QUOTES_POLL_MS, staleTime: QUOTES_POLL_MS });

  // Лестницы стаканов — только когда фильтр по объёму включён: ответ тяжёлый
  // (весь рынок × 20 уровней), а без фильтра он не нужен. Бэк держит их
  // push-свежими (depth-пул universe_stream), поэтому такт частый.
  const volOn = volBid > 0 || volAsk > 0;
  const depthQ = useQuery({
    queryKey: ["depth"], queryFn: fetchDepth, refetchInterval: 15000,
    enabled: volOn, staleTime: 10000,
  });
  const depth = depthQ.data?.items;

  // ЖИВОЙ ПОТОК. Пул котировок и стаканов Alor покрывает и фиксы
  // (services/universe_stream), движок пересчитывает по цене YTM и g-спред и
  // шлёт их патчем — /reprice с фронта не нужен. Wildcard-подписка: живой должна
  // быть вся таблица, а не только избранное.
  //
  // Копим не сырые патчи, а НАЛОЖЕНИЕ на строку: у цены и её производных разный
  // темп (цена — каждый тик, метрики — тактом движка), и правила их слияния
  // применяются один раз здесь, а не при каждом рендере таблицы.
  const [overlay, setOverlay] = useState({});
  const bufRef = useRef({});
  const wsRef = useRef(null);
  const tsRef = useRef({});      // isin → monotonic-ish время последнего пуша
  const flushRef = useRef(null);
  // wildcard-подписка несёт ВЕСЬ пул, включая флоатеров: чужие патчи молча
  // отбрасываем, иначе каждый их флеш заставлял бы витрину пересобирать свои
  // семь сотен строк ради бумаг, которых в ней нет
  const mineRef = useRef(new Set());
  useEffect(() => {
    mineRef.current = new Set((listQ.data?.items || []).map((b) => b.isin));
  }, [listQ.data]);
  useEffect(() => {
    // Статус соединения показывает общий индикатор нижней полосы (его держит
    // сокет дашборда) — второй раз о том же не сообщаем.
    const conn = connectMarketWs(() => [], () => {}, (isin, data) => {
      const buf = bufRef.current;
      const prev = buf[isin];
      buf[isin] = prev ? { ...prev, ...data, metrics: prev.metrics || data.metrics } : data;
      if (flushRef.current) return;
      flushRef.current = setTimeout(() => {
        flushRef.current = null;
        const batch = bufRef.current;
        bufRef.current = {};
        // время последнего пуша держим ОТДЕЛЬНО от данных строки: иначе
        // каждый повтор той же котировки менял бы строку и заставлял таблицу
        // перерисовывать бумагу, в которой ничего не произошло
        const now = Date.now();
        const mine = mineRef.current;
        // фильтруем на ФЛЕШЕ, а не на приёме: подписка отдаёт снапшот всего
        // рынка сразу, и на первых миллисекундах список фиксов ещё грузится —
        // пустой набор значит «не знаем, чьё», и мы берём всё
        const own = Object.entries(batch).filter(([isin]) => !mine.size || mine.has(isin));
        if (!own.length) return;
        for (const [isin] of own) tsRef.current[isin] = now;
        setOverlay((prev) => {
          const out = { ...prev };
          for (const [isin, q] of own) out[isin] = applyPatch(out[isin], q);
          return out;
        });
      }, WS_FLUSH_MS);
    });
    wsRef.current = conn;
    return () => {
      wsRef.current = null;
      if (flushRef.current) { clearTimeout(flushRef.current); flushRef.current = null; }
      conn.close();
    };
  }, []);

  // Размеры тикета — в движок тем же сокетом: он считает цену набора и спред по
  // ней только по размерам, которые кто-то смотрит, и помнит их с TTL.
  useEffect(() => {
    wsRef.current?.setVolSizes([volBid, volAsk].filter((v) => v > 0));
  }, [volBid, volAsk]);

  // Ссылки НЕизменившихся строк держим стабильными: таблица пересобирается на
  // каждом такте котировок, а memo(BondRow) снимает ре-рендер тех ~700 строк,
  // в которых ничего не поменялось (то же, что точечное клонирование у
  // монитора флоатеров).
  const rowRef = useRef(new Map());
  const bonds = useMemo(() => {
    const q = new Map((quotesQ.data?.items || []).map((it) => [it.isin, it]));
    const kept = new Map();
    const rows = (listQ.data?.items || []).map((b) => {
      // Общие имена полей витрины: поиск, фильтр эмитента и таблица написаны
      // под строку флоатера (short_name / emitter_name / is_ofz).
      const row = { ...b, short_name: b.name, emitter_name: b.issuer, is_ofz: b.cls === "ofz" };
      const live = overlay[b.isin];
      const fresh = live && Date.now() - (tsRef.current[b.isin] || 0) < LIVE_FRESH_MS;
      // Бумага на стриме цены из снапшота не берёт: push свежее, снапшот
      // откатил бы строку назад (то же правило, что у флоатеров, quotesMerge).
      const qi = q.get(b.isin);
      // ЧИСЛА НАБОРА НА ОБЪЁМ — метрика движка, а не цена: у бумаги НА СТРИМЕ
      // их патч не несёт (см. applyPatch), а список витрины обновляется раз в
      // минуту — без этого прочерк в колонках бида и оффера жил всё это время.
      if (qi) applyVolQuote(row, qi);
      const it = fresh ? null : qi;
      if (it) {
        applyPrice(row, "last_price_pct", it.last, ["ytm", "g_spread_bps", "z_spread_bps",
          "dirty", "cur_yield", "mod_dur", "mac_dur", "convexity", "dv01"]);
        applyPrice(row, "bid", it.bid, ["g_spread_bid_bps", "ytm_bid"], true);
        applyPrice(row, "ask", it.ask, ["g_spread_ask_bps", "ytm_ask"], true);
        applyPrice(row, "wap_pct", it.wap, ["g_spread_wap_bps", "ytm_wap"]);
        if (it.vol != null) row.val_today = it.vol;
      }
      // Стрим замолчал — наложение выбрасываем целиком: его метрики посчитаны
      // по ЕГО ценам, а в строке теперь цены снапшота, и смешивать их значит
      // повторить ровно тот рассинхрон «свежая цена, старое число», от которого
      // гасятся производные выше. Движок пишет свои числа и в fixed_metrics,
      // поэтому они вернутся ближайшим обновлением списка.
      const next = fresh ? { ...row, ...live } : row;
      const prev = rowRef.current.get(b.isin);
      const out = sameRow(prev, next) ? prev : next;
      kept.set(b.isin, out);
      return out;
    });
    rowRef.current = kept;
    return rows;
  }, [listQ.data, quotesQ.data, overlay]);

  const issuers = useMemo(() => {
    const m = new Map();
    for (const b of bonds) {
      if (!b.emitter_name) continue;
      m.set(b.emitter_name, (m.get(b.emitter_name) || 0) + 1);
    }
    return [...m.entries()].map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }, [bonds]);

  const rows = useMemo(() => {
    let r = bonds.slice();
    if (onlyWatch) r = r.filter((b) => watch.includes(b.isin));
    if (ratingsSel.length) r = r.filter((b) => ratingMatches(b.rating, ratingsSel));
    if (emittersSel.length) r = r.filter((b) => emittersSel.includes(b.emitter_name));
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
    const hzDate = (b) => b.put_date || b.maturity_date;
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
    const match = makeBondFilter(query);
    if (match) r = r.filter(match);
    const { key, dir } = sort;
    const m = dir === "asc" ? 1 : -1;
    r.sort((a, b) => {
      const x = a[key], y = b[key];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      if (typeof x === "string") return x.localeCompare(y) * m;
      return (x - y) * m;
    });
    return r;
  }, [bonds, onlyWatch, watch, ratingsSel, emittersSel, hideSub, hideAmort, clsSel, twoSided,
      volOn, volBid, volAsk, volMode, depth,
      matFrom, matTo, spreadFrom, spreadTo, ytmFrom, ytmTo, query, sort]);

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
  const toggleCol = useCallback((key) => setVisibleCols((cs) =>
    cs.includes(key) ? cs.filter((k) => k !== key) : [...cs, key]), []);
  const moveCol = useCallback((from, to) => setVisibleCols((cs) => {
    const i = cs.indexOf(from);
    if (i < 0) return cs;
    const next = cs.slice();
    next.splice(i, 1);
    const j = to === "+1" ? Math.min(next.length, i + 1)
      : to === "-1" ? Math.max(0, i - 1) : next.indexOf(to);
    if (j < 0) return cs;
    next.splice(j, 0, from);
    return next;
  }), []);
  const resetCols = useCallback(() => { setVisibleCols(FIXED_DEFAULT_COLS); setColWidths({}); }, []);
  const resizeCol = useCallback((key, px) => setColWidths((w) => ({ ...w, [key]: px })), []);
  const resetColWidth = useCallback((key) => setColWidths((w) => {
    const next = { ...w }; delete next[key]; return next;
  }), []);

  const activeFilters = (onlyWatch ? 1 : 0) + (ratingsSel.length ? 1 : 0)
    + (emittersSel.length ? 1 : 0) + (clsSel.length ? 1 : 0) + (hideSub ? 0 : 1)
    + (hideAmort ? 1 : 0) + (query !== "" ? 1 : 0) + (twoSided ? 1 : 0)
    + (volBid > 0 || volAsk > 0 ? 1 : 0)
    + (matFrom !== "" ? 1 : 0) + (matTo !== "" ? 1 : 0)
    + (spreadFrom !== "" ? 1 : 0) + (spreadTo !== "" ? 1 : 0)
    + (ytmFrom !== "" ? 1 : 0) + (ytmTo !== "" ? 1 : 0);
  const resetFilters = useCallback(() => {
    setOnlyWatch(false); setRatingsSel([]); setEmittersSel([]); setClsSel([]);
    setHideSub(true); setHideAmort(false); setQuery(""); setTwoSided(false);
    setVolBid(0); setVolAsk(0);
    setMatFrom(""); setMatTo(""); setSpreadFrom(""); setSpreadTo("");
    setYtmFrom(""); setYtmTo("");
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
        twoSided={twoSided} setTwoSided={setTwoSided}
        volBid={volBid} setVolBid={setVolBid} volAsk={volAsk} setVolAsk={setVolAsk}
        volMode={volMode} setVolMode={setVolMode}
        depthTs={depthQ.data?.ts} depthLoading={volOn && depthQ.isLoading}
        matFrom={matFrom} setMatFrom={setMatFrom} matTo={matTo} setMatTo={setMatTo}
        spreadLabel="G-спред"
        spreadFrom={spreadFrom} setSpreadFrom={setSpreadFrom}
        spreadTo={spreadTo} setSpreadTo={setSpreadTo}
        ytmFrom={ytmFrom} setYtmFrom={setYtmFrom} ytmTo={ytmTo} setYtmTo={setYtmTo}
        query={query} setQuery={setQuery} searchRef={searchRef}
        watchCount={watch.length}
        shown={rows.length} total={bonds.length}
        visibleCols={visibleCols} colsMeta={FIXED_COL_META}
        onToggleCol={toggleCol} onResetCols={resetCols} onMoveCol={moveCol}
      />

      {showAnalytics && rows.length > 0 && <FixedAnalytics rows={rows} />}

      <BondTable
        rows={rows}
        status={status}
        errMsg={listQ.error ? String(listQ.error.message || listQ.error) : ""}
        sort={sort}
        onSort={onSort}
        onOpen={onOpen}
        rowKind="fixed"
        watch={watch}
        onToggleStar={toggleStar}
        filtered={activeFilters > 0}
        onClearFilters={resetFilters}
        onRetry={listQ.refetch}
        visibleCols={visibleCols}
        onMoveCol={moveCol}
        colWidths={colWidths}
        onResizeCol={resizeCol}
        onResetColWidth={resetColWidth}
        colsDef={FIXED_COLS}
        defaultCols={FIXED_DEFAULT_COLS}
      />
    </>
  );
}
