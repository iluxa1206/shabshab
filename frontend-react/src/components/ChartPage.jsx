import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  createChart, CandlestickSeries, LineSeries, HistogramSeries, AreaSeries, CrosshairMode,
  createSeriesMarkers,
} from "lightweight-charts";
import {
  fetchCandles, fetchSpreadHistory, fetchBondDetails, fetchBondRow,
  fetchHourlyBars, fetchTrades,
} from "../api.js";
import { fmt, baseLabel } from "../format.js";
import { Brush } from "../charts/index.js";

// Полноэкранный график выпуска (своя вкладка, /chart/:isin).
// Сверху — строка параметров бумаги, ниже — график на всю высоту окна:
// цена (свечи/линия), объём и Y-IDX отдельными панелями с общей осью времени.
// Рисует lightweight-charts: zoom колесом, pan драгом, реальная временная ось —
// свои SVG-графики этого не умеют, а писать заново дороже, чем взять готовое.

// ── периоды ─────────────────────────────────────────────────────────────────
// [ключ, подпись, календарных дней (null = «всё»), таймфрейм по умолчанию]
const PERIODS = [
  ["1d", "1Д", 1, "5m"],
  ["5d", "5Д", 5, "1h"],
  ["1m", "1М", 30, "1d"],
  ["3m", "3М", 90, "1d"],
  ["6m", "6М", 180, "1d"],
  ["ytd", "YTD", null, "1d"],
  ["1y", "1Г", 365, "1d"],
  ["3y", "3Г", 1095, "1w"],
  ["all", "ВСЁ", null, "1w"],
];
const TFS = [["5m", "5м"], ["1h", "1ч"], ["1d", "1д"], ["1w", "1н"]];
// глубина, которую отдаёт бэк по таймфрейму (services/market_data._CANDLE_TF)
const TF_DEPTH_DAYS = { "5m": 4, "1h": 45, "1d": 550, "1w": 365 * 4 };
const BRUSH_H = 54;   // высота полосы-обзора под графиком

// ── дополнительные слои (URL-параметр l=vwap,sides,big) ─────────────────────
// Данные слоёв — свой архив (bar_hourly / trade_tick), а не свечи MOEX:
// средневзвешенная цена часа, стороны сделок по агрессору и крупные принты.
const LAYERS = [
  ["vwap", "Средневзвес", "VWAP часа (свой архив) + Y-IDX по нему на внутридневном масштабе"],
  ["sides", "Покупки/продажи", "VWAP по агрессору: buy и sell отдельными линиями"],
  ["big", "Крупные сделки", "Маркеры отдельных сделок крупнее порога"],
];
const BIG_THRESHOLDS = [1, 5, 10, 50, 100];   // млн ₽
const LAYER_MAX_DAYS = 730;                   // потолок окна баров у бэка

const iso = (d) => d.toISOString().slice(0, 10);
const isoBack = (days) => iso(new Date(Date.now() - days * 864e5));

// начало окна периода в ISO (null — «с самого начала данных»)
function periodFrom(key) {
  if (key === "all") return null;
  if (key === "ytd") return `${new Date().getFullYear()}-01-01`;
  const p = PERIODS.find(([k]) => k === key);
  return p && p[2] ? isoBack(p[2]) : null;
}

// ── тема ────────────────────────────────────────────────────────────────────
// Цвета берём из тех же CSS-переменных, что и весь дашборд, чтобы график не
// жил своей палитрой. Смена темы ловится наблюдателем за class на #app.
function readTheme(el) {
  const cs = getComputedStyle(el);
  const v = (n, f) => ((cs.getPropertyValue(n) || "").trim() || f);
  return {
    bg: v("--bg", "#ffffff"),
    fg: v("--fg", "#111111"),
    line: v("--line", "#e6e6e6"),
    line2: v("--line-2", "#f0f0f0"),
    mut: v("--mut", "#888888"),
    up: v("--up", "#22a06b"),
    down: v("--down", "#e5484d"),
    accent: v("--accent", "#3b82f6"),
  };
}

function useThemeVars(ref) {
  const [vars, setVars] = useState(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const sync = () => setVars(readTheme(el));
    sync();
    const app = document.getElementById("app");
    if (!app) return;
    const mo = new MutationObserver(sync);
    mo.observe(app, { attributes: true, attributeFilter: ["class"] });
    return () => mo.disconnect();
  }, [ref]);
  return vars;
}

// ── свечи → формат lightweight-charts ───────────────────────────────────────
// Дневные/недельные — бизнес-дни строкой. Внутридневные — UNIX-секунды, но
// намеренно из МСК-времени «как если бы это был UTC»: библиотека рисует ось в
// UTC, и такой сдвиг даёт на подписях ровно биржевое московское время.
const toTime = (t, tf) => (tf === "5m" || tf === "1h"
  ? Math.floor(Date.parse(t.replace(" ", "T") + "Z") / 1000)
  : t.slice(0, 10));

// ── слои: часовые бары под сетку текущего таймфрейма ────────────────────────
// На внутридневном масштабе бар остаётся часовым, на дневном — схлопывается по
// календарной дате (VWAP взвешен по объёму, спред — по объёму же). Дневная
// склейка может на копейки расходиться с WAPRICE биржи: вечернюю сессию MOEX
// относит к следующему торговому дню, а здесь она лежит по своей дате.
function layerPoints(bars, tf) {
  if (!bars?.length) return [];
  if (tf === "5m" || tf === "1h") {
    return bars.map((b) => ({ ...b, time: toTime(b.ts, "1h") }));
  }
  const by = new Map();
  for (const b of bars) {
    const d = b.ts.slice(0, 10);
    const a = by.get(d) || { time: d, vol: 0, val: 0, yNum: 0, yDen: 0,
                             bq: 0, bv: 0, sq: 0, sv: 0, face: 1000 };
    a.vol += b.volume || 0;
    a.val += b.value || 0;
    a.face = b.face || a.face;
    if (b.y_idx_bps != null && b.volume) { a.yNum += b.y_idx_bps * b.volume; a.yDen += b.volume; }
    if (b.buy_volume) { a.bq += b.buy_volume; a.bv += (b.buy_vwap || 0) * b.buy_volume; }
    if (b.sell_volume) { a.sq += b.sell_volume; a.sv += (b.sell_vwap || 0) * b.sell_volume; }
    by.set(d, a);
  }
  return [...by.values()].map((a) => ({
    time: a.time,
    vwap_pct: a.vol ? a.val / a.vol / a.face * 100 : null,
    volume: a.vol,
    y_idx_bps: a.yDen ? a.yNum / a.yDen : null,
    buy_vwap: a.bq ? a.bv / a.bq : null,
    sell_vwap: a.sq ? a.sv / a.sq : null,
  })).sort((x, y) => (x.time < y.time ? -1 : 1));
}

// Сделка → время бара, в который она попадает: маркер должен сесть на сетку
// графика, иначе lightweight-charts ставит его на соседний бар.
function tradeTime(ts, tf) {
  if (tf === "1d") return ts.slice(0, 10);
  const sec = Math.floor(Date.parse(ts.replace(" ", "T") + "Z") / 1000);
  const step = tf === "5m" ? 300 : 3600;
  return Math.floor(sec / step) * step;
}

export default function ChartPage() {
  const { isin } = useParams();
  const [sp, setSp] = useSearchParams();

  const period = sp.get("p") || "3m";
  const custom = { from: sp.get("from"), to: sp.get("to") };
  const isCustom = !!(custom.from || custom.to);
  const defTf = PERIODS.find(([k]) => k === period)?.[3] || "1d";
  const tf = sp.get("tf") || (isCustom ? "1d" : defTf);
  const type = sp.get("type") || "candles";

  const setParam = (patch) => setSp((prev) => {
    const n = new URLSearchParams(prev);
    for (const [k, v] of Object.entries(patch)) {
      if (v == null) n.delete(k); else n.set(k, v);
    }
    return n;
  }, { replace: true });

  // границы окна
  const from = isCustom ? custom.from : periodFrom(period);
  const to = isCustom ? custom.to : null;

  // ── данные ────────────────────────────────────────────────────────────────
  const qCandles = useQuery({
    queryKey: ["candles", isin, tf],
    queryFn: () => fetchCandles(isin, tf),
    staleTime: 60_000,
  });
  const qDetails = useQuery({ queryKey: ["bond", isin], queryFn: () => fetchBondDetails(isin) });
  const qRow = useQuery({ queryKey: ["bond-row", isin], queryFn: () => fetchBondRow(isin) });
  // Y-IDX только на дневном/недельном масштабе — внутридневной истории спреда нет
  const spreadOn = tf === "1d" || tf === "1w";
  const qSpread = useQuery({
    queryKey: ["spread-hist", isin, "floater", from || "all"],
    queryFn: () => fetchSpreadHistory(isin, { kind: "floater", from: from || isoBack(400), days: 400 }),
    enabled: spreadOn,
    staleTime: 300_000,
  });

  const candles = useMemo(() => {
    const rows = qCandles.data?.candles || [];
    return rows.filter((c) => {
      const d = c.t.slice(0, 10);
      return (!from || d >= from) && (!to || d <= to);
    });
  }, [qCandles.data, from, to]);

  // ── слои поверх свечей ────────────────────────────────────────────────────
  // Недельный масштаб пропускаем: часовые бары не ложатся на недельную сетку —
  // дневные точки навесили бы на ось лишние деления и сдвинули бы маркеры.
  const layersOk = tf !== "1w";
  const layers = useMemo(
    () => new Set((sp.get("l") ?? "vwap").split(",").filter(Boolean)),
    [sp]);
  const on = (k) => layersOk && layers.has(k);
  const bigMln = Number(sp.get("mv") || 10);
  const toggleLayer = (k) => {
    const n = new Set(layers);
    n.has(k) ? n.delete(k) : n.add(k);
    setParam({ l: n.size ? [...n].join(",") : "-" });
  };

  // окно слоёв в днях (у баров потолок 730, у тиков реально ~30 + наш архив)
  const layerDays = useMemo(() => {
    if (!from) return LAYER_MAX_DAYS;
    return Math.min(LAYER_MAX_DAYS,
      Math.max(1, Math.ceil((Date.now() - Date.parse(from)) / 864e5)));
  }, [from]);

  const barsOn = on("vwap") || on("sides");
  const qBars = useQuery({
    queryKey: ["hbars", isin, layerDays],
    queryFn: () => fetchHourlyBars(isin, { days: layerDays }),
    enabled: barsOn,
    staleTime: 300_000,
  });
  const qTrades = useQuery({
    queryKey: ["big-trades", isin, layerDays, bigMln],
    queryFn: () => fetchTrades(isin, { days: Math.min(layerDays, 400),
                                       minValue: bigMln * 1e6, limit: 400 }),
    enabled: on("big"),
    staleTime: 300_000,
  });

  const layerPts = useMemo(() => {
    const rows = (qBars.data?.bars || []).filter((b) => {
      const d = b.ts.slice(0, 10);
      return (!from || d >= from) && (!to || d <= to);
    });
    return layerPoints(rows, tf);
  }, [qBars.data, tf, from, to]);

  const bigTrades = useMemo(() => {
    const rows = (qTrades.data?.trades || []).filter((t) => {
      const d = t.ts.slice(0, 10);
      return (!from || d >= from) && (!to || d <= to);
    });
    return rows;
  }, [qTrades.data, from, to]);

  // панель спреда есть либо от дневной истории, либо от часовых баров
  const spreadPaneOn = spreadOn || (barsOn && layersOk);

  // ── график ────────────────────────────────────────────────────────────────
  const wrapRef = useRef(null);
  const hostRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef({});
  const theme = useThemeVars(wrapRef);
  const [legend, setLegend] = useState(null);
  const [hasYidx, setHasYidx] = useState(false);
  // окно brush'а — в логических индексах баров (тех же, что у timeScale)
  const [visRange, setVisRange] = useState(null);
  // ширина ценовой шкалы справа: полоса-обзор должна кончаться там же, где
  // область данных графика, иначе окно не совпадает с тем, что видно
  const [rightPad, setRightPad] = useState(0);

  // Высота графика — всё, что осталось до низа окна: страница не должна
  // скроллиться. Шапка переносится на 2–3 строки при узком окне и «дорастает»
  // после загрузки данных, поэтому следим за её размером, а не считаем один раз.
  const topRef = useRef(null);
  const [height, setHeight] = useState(420);
  useLayoutEffect(() => {
    const calc = () => {
      const el = hostRef.current;
      if (!el) return;
      const top = el.getBoundingClientRect().top;
      // под графиком: полоса-обзор (BRUSH_H + отступ) и строка-подвал (34px)
      // 34 подвал + BRUSH_H + 8 отступ + 2 рамка полосы
      setHeight(Math.max(260, Math.round(window.innerHeight - top - 34 - BRUSH_H - 10)));
    };
    calc();
    window.addEventListener("resize", calc);
    const ro = new ResizeObserver(calc);
    if (topRef.current) ro.observe(topRef.current);
    return () => { window.removeEventListener("resize", calc); ro.disconnect(); };
  }, []);

  // создание графика — один раз; данные и цвета обновляются отдельными эффектами
  useEffect(() => {
    const el = hostRef.current;
    if (!el || !theme) return;
    const chart = createChart(el, {
      width: Math.max(320, Math.round(el.clientWidth)),
      height: Math.max(260, Math.round(el.clientHeight)),
      layout: { background: { color: theme.bg }, textColor: theme.mut, fontSize: 11,
        panes: { separatorColor: theme.line, separatorHoverColor: theme.line2 } },
      grid: { vertLines: { color: theme.line2 }, horzLines: { color: theme.line2 } },
      rightPriceScale: { borderColor: theme.line },
      timeScale: { borderColor: theme.line, timeVisible: tf === "5m" || tf === "1h", secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      localization: { locale: "ru-RU" },
    });
    chartRef.current = chart;
    return () => { chart.remove(); chartRef.current = null; seriesRef.current = {}; };
    // пересоздаём при смене типа графика/наличия панели спреда: так проще и
    // надёжнее, чем снимать и добавлять серии в живом графике
  }, [theme && theme.bg, type, spreadPaneOn, tf === "5m" || tf === "1h"]); // eslint-disable-line

  // серии + данные
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !theme || !candles.length) return;

    for (const [k, s] of Object.entries(seriesRef.current)) {
      try {
        if (k === "marks") s.detach();     // плагин маркеров, не серия
        else chart.removeSeries(s);
      } catch { /* серия уже снята */ }
    }
    seriesRef.current = {};

    const price = type === "line"
      ? chart.addSeries(AreaSeries, {
          lineColor: theme.accent, lineWidth: 2,
          topColor: theme.accent + "33", bottomColor: theme.accent + "05",
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        }, 0)
      : chart.addSeries(CandlestickSeries, {
          upColor: theme.up, downColor: theme.down,
          borderUpColor: theme.up, borderDownColor: theme.down,
          wickUpColor: theme.up, wickDownColor: theme.down,
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        }, 0);
    price.setData(candles.map((c) => (type === "line"
      ? { time: toTime(c.t, tf), value: c.c }
      : { time: toTime(c.t, tf), open: c.o, high: c.h, low: c.l, close: c.c })));
    seriesRef.current.price = price;

    const vol = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false,
    }, 1);
    vol.setData(candles.map((c) => ({
      time: toTime(c.t, tf), value: c.v || 0,
      color: (c.c >= c.o ? theme.up : theme.down) + "80",
    })));
    seriesRef.current.vol = vol;

    // ── слой «средневзвес»: VWAP часа поверх свечей ──────────────────────────
    if (on("vwap")) {
      const pts = layerPts.filter((p) => p.vwap_pct != null);
      if (pts.length > 1) {
        const vwap = chart.addSeries(LineSeries, {
          color: theme.accent, lineWidth: 2, lineStyle: 0,
          priceLineVisible: false, lastValueVisible: false,
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        }, 0);
        vwap.setData(pts.map((p) => ({ time: p.time, value: p.vwap_pct })));
        seriesRef.current.vwap = vwap;
      }
    }

    // ── слой «покупки/продажи»: VWAP по агрессору ────────────────────────────
    if (on("sides")) {
      for (const [key, field, color] of [["buy", "buy_vwap", theme.up],
                                         ["sell", "sell_vwap", theme.down]]) {
        const pts = layerPts.filter((p) => p[field] != null);
        if (pts.length < 2) continue;
        const s = chart.addSeries(LineSeries, {
          color, lineWidth: 1, lineStyle: 2,   // пунктир: вспомогательные линии
          priceLineVisible: false, lastValueVisible: false,
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        }, 0);
        s.setData(pts.map((p) => ({ time: p.time, value: p[field] })));
        seriesRef.current[key] = s;
      }
    }

    // ── слой «крупные сделки»: маркеры на ценовой серии ──────────────────────
    if (on("big") && bigTrades.length) {
      // одна цена — один маркер: в час может прийти несколько крупных принтов,
      // накладываясь друг на друга; берём самый крупный в баре
      const best = new Map();
      for (const t of bigTrades) {
        const time = tradeTime(t.ts, tf);
        const key = `${time}|${t.side}`;
        const prev = best.get(key);
        if (!prev || (t.value || 0) > (prev.value || 0)) best.set(key, { ...t, time });
      }
      const marks = [...best.values()]
        .sort((a, b) => (a.time > b.time ? 1 : a.time < b.time ? -1 : 0))
        .map((t) => ({
          time: t.time,
          position: t.side === "sell" ? "aboveBar" : "belowBar",
          shape: t.side === "sell" ? "arrowDown" : "arrowUp",
          color: t.side === "sell" ? theme.down : theme.up,
          text: `${Math.round((t.value || 0) / 1e6)}М`,
        }));
      if (marks.length) seriesRef.current.marks = createSeriesMarkers(price, marks);
    }

    let yidxDrawn = false;
    if (spreadPaneOn) {
      // Внутридневной масштаб: спред берём по средневзвешенной цене часа (своя
      // база), дневной/недельный — из истории спредов. Смешивать нельзя: у них
      // разная сетка времени и разный источник цены.
      const useBars = (tf === "5m" || tf === "1h" || barsOn) && layerPts.some((p) => p.y_idx_bps != null);
      const pts = useBars
        ? layerPts.filter((p) => p.y_idx_bps != null).map((p) => ({ time: p.time, value: p.y_idx_bps }))
        : (qSpread.data?.points || []).filter((p) => p.y_idx_bps != null)
            .map((p) => ({ time: p.date, value: p.y_idx_bps }));
      if (pts.length > 1) {
        yidxDrawn = true;
        const yidx = chart.addSeries(LineSeries, {
          color: theme.fg, lineWidth: 2,
          priceFormat: { type: "price", precision: 0, minMove: 1 },
        }, 2);
        yidx.setData(pts);
        seriesRef.current.yidx = yidx;
      }
    }

    setHasYidx(yidxDrawn);
    // объём и спред — узкими панелями под ценой
    const panes = chart.panes();
    if (panes[0]) panes[0].setStretchFactor(yidxDrawn ? 3.2 : 4);
    if (panes[1]) panes[1].setStretchFactor(0.9);
    if (panes[2]) panes[2].setStretchFactor(1.4);
    // autoSize подхватывает ширину контейнера уже после текущего кадра — fitContent
    // сразу посчитал бы масштаб по нулевой ширине и данные сжались бы к правому краю
    chart.timeScale().fitContent();
    const raf = requestAnimationFrame(() => {
      const c = chartRef.current;
      if (!c) return;
      c.timeScale().fitContent();
      // ширину поля под ценовой шкалой меряем здесь: в момент подписки шкала
      // ещё не отрисована и полоса-обзор получалась во всю ширину контейнера
      measureRightPad(c, candles.length);
    });
    return () => cancelAnimationFrame(raf);
  }, [candles, type, tf, theme, spreadPaneOn, qSpread.data, layerPts, bigTrades,
      layers, layersOk]);

  // при смене окна/таймфрейма старая строка легенды осталась бы висеть от
  // предыдущего курсора — гасим
  useEffect(() => { setLegend(null); }, [tf, type, from, to]);

  // Правый отступ полосы-обзора = поле под ценовой шкалой. Прямые API его не
  // дают: chart.priceScale('right').width() возвращает 0, а вариант через
  // panes()[0] — 2px, потому что в v5 шкала рисуется поверх общего холста.
  // Меряем по координате крайнего бара при ПОЛНОМ окне: правее него область
  // данных не идёт. Вызывается и после fitContent (шкала уже отрисована), и на
  // каждом изменении видимого диапазона.
  const measureRightPad = (chart, n, range) => {
    const el = hostRef.current;
    const full = el ? el.getBoundingClientRect().width : 0;
    if (!full || n < 2) return;
    const ts = chart.timeScale();
    const r = range ?? ts.getVisibleLogicalRange();
    if (!r || r.to < n - 1) return;          // окно неполное — крайний бар вне вида
    const xLast = ts.logicalToCoordinate(n - 1);
    if (xLast != null) setRightPad(Math.max(0, Math.round(full - xLast - 3)));
  };

  // Размер графика задаём вручную (autoSize в v5 ломал императивные методы
  // timeScale: setVisibleLogicalRange/scrollToPosition молча не применялись).
  useEffect(() => {
    const el = hostRef.current, chart = chartRef.current;
    if (!el || !chart) return;
    const apply = () => chart.resize(Math.max(320, Math.round(el.clientWidth)),
      Math.max(260, Math.round(el.clientHeight)));
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => ro.disconnect();
  }, [theme, type, spreadPaneOn, height]);

  // видимое окно графика → рамка на полосе-обзоре (зум колесом и pan драгом
  // двигают её так же, как перетаскивание самой рамки)
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const n = candles.length;
    const ts = chart.timeScale();
    const onRange = (r) => {
      if (r) setVisRange({ from: r.from, to: r.to });
      measureRightPad(chart, n, r);
    };
    ts.subscribeVisibleLogicalRangeChange(onRange);
    onRange(ts.getVisibleLogicalRange());
    return () => ts.unsubscribeVisibleLogicalRangeChange(onRange);
  }, [candles, type, spreadPaneOn, theme, tf]);

  // рамка → график
  const applyBrush = (r) => chartRef.current?.timeScale().setVisibleLogicalRange(r);

  // легенда под курсором (crosshair) — цифры вместо гадания по пикселям
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const onMove = (param) => {
      const s = seriesRef.current;
      if (!param.time || !s.price) { setLegend(null); return; }
      const p = param.seriesData.get(s.price);
      if (!p) { setLegend(null); return; }
      const val = (ser) => (ser ? param.seriesData.get(ser)?.value : null);
      setLegend({
        time: param.time,
        o: p.open, h: p.high, l: p.low, c: p.close ?? p.value,
        v: param.seriesData.get(s.vol)?.value,
        y: val(s.yidx), w: val(s.vwap), b: val(s.buy), sl: val(s.sell),
      });
    };
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
  }, [candles, type, spreadPaneOn]);

  // ── шапка ─────────────────────────────────────────────────────────────────
  const r = qDetails.data?.reference;
  const v = qDetails.data?.valuation;
  const m = qDetails.data?.market;
  const f = qDetails.data?.floater;
  const row = qRow.data;
  const name = r?.short_name || row?.short_name || isin;
  useEffect(() => { document.title = `${name} · график`; }, [name]);

  const stat = (k, val, cls) => (
    <span className="cp-stat">
      <span className="cp-stat-k">{k}</span>
      <span className={"cp-stat-v" + (cls ? " " + cls : "")}>{val ?? "—"}</span>
    </span>
  );

  const tfDepth = TF_DEPTH_DAYS[tf];
  const wantDays = isCustom || !from ? null : Math.round((Date.now() - Date.parse(from)) / 864e5);
  const depthShort = wantDays != null && tfDepth != null && wantDays > tfDepth;

  return (
    <div className="chart-page" ref={wrapRef}>
      <div className="cp-top" ref={topRef}>
      <div className="cp-head">
        <div className="cp-id">
          <Link className="cp-back" to={`/floaters?isin=${isin}`} title="К карточке выпуска">←</Link>
          <span className="cp-name" title={row?.emitter_name || ""}>{name}</span>
          <span className="cp-isin">{isin}</span>
          {row?.rating && <span className="cp-rating">{row.rating}</span>}
        </div>
        <div className="cp-stats">
          {row?.emitter_name && stat("эмитент", row.emitter_name)}
          {stat("формула", r ? `${baseLabel(r.base_rate_type)} + ${r.spread_bps}` : null)}
          {stat("цена", m?.last_price_pct != null ? fmt.pct(m.last_price_pct) + "%" : null)}
          {stat("Y-IDX", v?.yield_over_index_bps != null ? v.yield_over_index_bps + " bps" : null, "hi")}
          {stat("DM", v?.disc_margin_bps != null ? v.disc_margin_bps + " bps" : null)}
          {stat("SM", v?.sm_bps != null ? v.sm_bps + " bps" : null)}
          {stat("спред-дюр.", f?.spread_duration_yrs != null ? fmt.yrs(f.spread_duration_yrs) : null)}
          {stat("погашение", r?.maturity_date ? fmt.date(r.maturity_date) : null)}
          {stat("расчёт", m?.calc_date ? fmt.date(m.calc_date) : null)}
        </div>
      </div>

      <div className="cp-ctl">
        <span className="cp-group" role="group" aria-label="Период">
          {PERIODS.map(([k, l]) => (
            <button key={k} type="button" className={"cp-btn" + (!isCustom && period === k ? " on" : "")}
              onClick={() => setParam({ p: k, from: null, to: null, tf: null })}>{l}</button>
          ))}
        </span>
        <span className="cp-group" role="group" aria-label="Таймфрейм">
          {TFS.map(([k, l]) => (
            <button key={k} type="button" className={"cp-btn" + (tf === k ? " on" : "")}
              onClick={() => setParam({ tf: k })}>{l}</button>
          ))}
        </span>
        <span className="cp-group" role="group" aria-label="Вид">
          {[["candles", "Свечи"], ["line", "Линия"]].map(([k, l]) => (
            <button key={k} type="button" className={"cp-btn" + (type === k ? " on" : "")}
              onClick={() => setParam({ type: k })}>{l}</button>
          ))}
        </span>
        <label className="cp-date">с
          <input type="date" value={custom.from || ""} max={custom.to || iso(new Date())}
            onChange={(e) => setParam({ from: e.target.value || null, p: null })} />
        </label>
        <label className="cp-date">по
          <input type="date" value={custom.to || ""} min={custom.from || ""} max={iso(new Date())}
            onChange={(e) => setParam({ to: e.target.value || null, p: null })} />
        </label>
        {isCustom && (
          <button type="button" className="cp-btn cp-reset"
            onClick={() => setParam({ from: null, to: null, p: "3m", tf: null })}>сброс дат</button>
        )}
        <span className="cp-hint">колесо — зум · драг — сдвиг · двойной клик по оси — сброс</span>
      </div>

      <div className="cp-ctl cp-ctl-layers">
        <span className="cp-layers-k">слои</span>
        <span className="cp-group" role="group" aria-label="Слои">
          {LAYERS.map(([k, l, hint]) => (
            <button key={k} type="button" title={layersOk ? hint : "недоступно на недельном масштабе"}
              disabled={!layersOk}
              className={"cp-btn" + (on(k) ? " on" : "") + (layersOk ? "" : " off")}
              onClick={() => toggleLayer(k)}>{l}</button>
          ))}
        </span>
        {on("big") && (
          <label className="cp-date">крупнее
            <select value={bigMln} onChange={(e) => setParam({ mv: e.target.value })}>
              {BIG_THRESHOLDS.map((v) => <option key={v} value={v}>{v} млн ₽</option>)}
            </select>
          </label>
        )}
        <span className="cp-hint">
          {(qBars.isPending && barsOn) || (qTrades.isPending && on("big"))
            ? "загрузка архива…"
            : barsOn || on("big")
              ? [
                  barsOn && layerPts.length ? `${layerPts.length} баров` : null,
                  on("big") ? `${bigTrades.length} крупных сделок` : null,
                  qTrades.data?.eff_spread_bps != null
                    ? `эфф. спред ${qTrades.data.eff_spread_bps} б.п. цены` : null,
                ].filter(Boolean).join(" · ")
              : "средневзвес и сделки — из своего архива"}
        </span>
      </div>

      {/* Строка под курсором рендерится ВСЕГДА: раньше она появлялась только
          при наведении, а за высотой .cp-top следит ResizeObserver — блок
          возникал/исчезал, высота графика пересчитывалась, и график прыгал
          вместе с масштабом. Пустая строка держит место. */}
      <div className="cp-legend">
        {legend && candles.length > 0 ? (
          <>
            <b>{typeof legend.time === "string" ? fmt.date(legend.time)
              : new Date(legend.time * 1000).toISOString().slice(0, 16).replace("T", " ")}</b>
            {legend.o != null && <> · О {fmt.pct(legend.o)} М {fmt.pct(legend.h)} Н {fmt.pct(legend.l)} З {fmt.pct(legend.c)}</>}
            {legend.o == null && legend.c != null && <> · цена {fmt.pct(legend.c)}</>}
            {legend.v ? <> · объём {fmt.num(legend.v, 0)}</> : null}
            {legend.w != null && <> · ср.взвес {fmt.pct(legend.w)}</>}
            {legend.b != null && <> · покупки {fmt.pct(legend.b)}</>}
            {legend.sl != null && <> · продажи {fmt.pct(legend.sl)}</>}
            {legend.y != null && <> · Y-IDX {Math.round(legend.y)} bps</>}
          </>
        ) : <span className="cp-legend-hint">наведи курсор на график — здесь будут цифры бара</span>}
      </div>
      </div>

      <div className="cp-chart" ref={hostRef} style={{ height }} />

      {candles.length > 1 && (
        <Brush values={candles.map((c) => c.c)} range={visRange} onChange={applyBrush}
          theme={theme} height={BRUSH_H} rightPad={rightPad}
          label={`Обзор периода: ${candles[0].t.slice(0, 10)} — ${candles[candles.length - 1].t.slice(0, 10)}`} />
      )}

      <div className="cp-foot">
        {qCandles.isPending && "загрузка свечей…"}
        {!qCandles.isPending && !candles.length && (depthShort
          ? <span className="cp-warn">пусто: MOEX отдаёт по «{tf}» только {tfDepth} дней назад — окно шире доступной глубины</span>
          : "нет сделок за выбранный период")}
        {!!candles.length && (
          <>
            {candles.length} свечей · {tf}
            {depthShort && <span className="cp-warn"> · MOEX отдаёт по {tf} только {tfDepth} дней — окно урезано</span>}
            {spreadPaneOn && !hasYidx && " · история Y-IDX за период пуста"}
            {!spreadPaneOn && " · Y-IDX: включите слой «Средневзвес» или дневной масштаб"}
            {on("big") && !bigTrades.length &&
              ` · сделок крупнее ${bigMln} млн ₽ в архиве нет (глубина тиков ~30 дней)`}
          </>
        )}
      </div>
    </div>
  );
}
