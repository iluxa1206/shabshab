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
// Спред внутри дня даёт ещё и OHLC (y_o/y_h/y_l/y_c) — из часовых значений в
// порядке времени: так спред можно рисовать теми же свечами, что и цену.
//
// В O/H/L/C идут не все часы: одиночная сделка на утренней/вечерней сессии
// уводит спред на сотни б.п. (RU000A1025B5 2026-08-03 06:00 — 126 бумаг из
// 126 603 за день, −266 б.п. против −42…−58 весь день) и рисует фитиль во весь
// экран. Час учитывается, если дал хотя бы Y_OHLC_MIN_SHARE дневного объёма.
// На средневзвешенное значение это не влияет: там вес такого часа и так 0.1%.
const Y_OHLC_MIN_SHARE = 0.01;
// Доля не спасает, когда тонкий весь день: RU000A10A4N8 2026-07-08 — две сделки
// (1 и 3 бумаги), спред +552 и −1177 б.п. Размах такого дня — не движение
// рынка, а два случайных принта, поэтому ниже порога оборота свеча схлопывается
// в плоскую по средневзвесу: серия не рвётся, ложного диапазона нет.
const Y_OHLC_MIN_DAY_VALUE = 1e6;   // ₽ оборота за день (без НКД)
const Y_OHLC_MIN_HOUR_VALUE = 1e5;  // то же для часа на внутридневном масштабе
const Y_OHLC_MIN_HOUR_SHARE = 0.2;  // и не меньше доли от медианного часа окна

// Метрика спреда зависит от типа бумаги: у флоатера Y-IDX, у фикса G-спред.
// Бар несёт оба поля, заполнено то, что считается для этой бумаги.
const barSpread = (b) => (b.y_idx_bps != null ? b.y_idx_bps : b.g_spread_bps);
const spreadKindOf = (bars) =>
  (bars?.some((b) => b.y_idx_bps != null) ? "y" : bars?.some((b) => b.g_spread_bps != null) ? "g" : "y");
const SPREAD_LABEL = { y: "Y-IDX", g: "G-спред" };

// Спред по ценам бара (бэкенд считает его тем же reprice, что и по vwap).
// Спред обратен цене, поэтому «максимум спреда» — это y по МИНИМАЛЬНОЙ цене:
// сравниваем значения, а не полагаемся на имена полей.
const barSpreadOHLC = (b) => {
  const o = b.y_open_bps, c = b.y_close_bps;
  const all = [o, b.y_high_bps, b.y_low_bps, c].filter((x) => x != null);
  if (all.length < 2 || o == null || c == null) return null;
  return { o, c, h: Math.max(...all), l: Math.min(...all) };
};

// useVwap: спред точки по средневзвесу (слой СРЕДНЕВЗВЕС включён) или по цене
// закрытия (выключен) — линия спреда следует тому же представлению цены, что
// выбрано на ценовом графике. У старых баров y_close_bps может не быть —
// откатываемся на средневзвес, чтобы серия не рвалась.
function layerPoints(bars, tf, useVwap = true) {
  if (!bars?.length) return [];
  const ptSpread = (b) => (useVwap ? barSpread(b) : (b.y_close_bps ?? barSpread(b)));
  if (tf === "5m" || tf === "1h") {
    // На часовой сетке «тонкий» — сам час: одиночная сделка на утренней сессии
    // даёт спред в сотни б.п. и так же травит статистику, как тонкий день.
    // Порог относительный (доля от медианного часа окна): абсолютный пришлось бы
    // подбирать под каждую бумагу — обороты различаются на три порядка.
    const vals = bars.map((b) => b.value || 0).filter((v) => v > 0).sort((a, b) => a - b);
    const med = vals.length ? vals[Math.floor(vals.length / 2)] : 0;
    const min = Math.max(Y_OHLC_MIN_HOUR_VALUE, med * Y_OHLC_MIN_HOUR_SHARE);
    return bars.map((b) => {
      const s = barSpreadOHLC(b);
      // на часовой сетке свеча спреда — сам бар: у часа есть свои O/H/L/C цены
      return { ...b, time: toTime(b.ts, "1h"), y_idx_bps: ptSpread(b),
               y_o: s?.o ?? null, y_h: s?.h ?? null, y_l: s?.l ?? null, y_c: s?.c ?? null,
               thin: (b.value || 0) < min };
    });
  }
  const by = new Map();
  for (const b of [...bars].sort((x, y) => (x.ts < y.ts ? -1 : 1))) {
    const d = b.ts.slice(0, 10);
    const a = by.get(d) || { time: d, vol: 0, val: 0, yNum: 0, yDen: 0,
                             bq: 0, bv: 0, sq: 0, sv: 0, face: 1000, hours: [] };
    a.vol += b.volume || 0;
    a.val += b.value || 0;
    a.face = b.face || a.face;
    const y = barSpread(b);
    if (y != null && b.volume) {
      a.yNum += y * b.volume; a.yDen += b.volume;
      // ohlc — спред по ценам самого часа (может не быть у старых баров,
      // налитых до появления полей: тогда останется прежняя склейка по vwap)
      a.hours.push({ y, v: b.volume, ohlc: barSpreadOHLC(b) });
    }
    if (b.buy_volume) { a.bq += b.buy_volume; a.bv += (b.buy_vwap || 0) * b.buy_volume; }
    if (b.sell_volume) { a.sq += b.sell_volume; a.sv += (b.sell_vwap || 0) * b.sell_volume; }
    by.set(d, a);
  }
  return [...by.values()].map((a) => {
    // порог отбрасывает все часы только в совсем пустой день — тогда откатываемся
    // на все часы, иначе свеча пропала бы там, где точка на линии есть
    const min = a.yDen * Y_OHLC_MIN_SHARE;
    const hh = a.hours.filter((x) => x.v >= min);
    const src = hh.length ? hh : a.hours;
    const wavg = a.yDen ? a.yNum / a.yDen : null;
    // спред по цене закрытия дня — close последнего часа с OHLC (та же цена,
    // что закрытие дневной свечи); старые бары без OHLC → средневзвес
    const withC = src.filter((x) => x.ohlc);
    const closeVal = withC.length ? withC[withC.length - 1].ohlc.c : wavg;
    const dayVal = useVwap ? wavg : (closeVal ?? wavg);
    const thin = a.val < Y_OHLC_MIN_DAY_VALUE;

    // Дневная свеча спреда: open — по цене открытия первого часа, close — по
    // цене закрытия последнего, экстремумы — по всем ценам всех часов. Каждый
    // час даёт 4 точки, поэтому свеча полноценна даже в день с одной торговой
    // сессией. Часы без ohlc (старые бары) вносят одну точку по vwap.
    let cndl = null;
    if (thin) {
      cndl = dayVal != null ? { o: dayVal, c: dayVal, h: dayVal, l: dayVal } : null;
    } else {
      const withOhlc = src.filter((x) => x.ohlc);
      const pts = src.flatMap((x) => (x.ohlc ? [x.ohlc.o, x.ohlc.h, x.ohlc.l, x.ohlc.c] : [x.y]));
      if (pts.length) {
        cndl = {
          o: withOhlc.length ? withOhlc[0].ohlc.o : src[0].y,
          c: withOhlc.length ? withOhlc[withOhlc.length - 1].ohlc.c : src[src.length - 1].y,
          h: Math.max(...pts),
          l: Math.min(...pts),
        };
      }
    }
    return {
      time: a.time,
      vwap_pct: a.vol ? a.val / a.vol / a.face * 100 : null,
      volume: a.vol,
      value: a.val,
      thin,
      y_idx_bps: dayVal,
      y_o: cndl?.o ?? null,
      y_c: cndl?.c ?? null,
      y_h: cndl?.h ?? null,
      y_l: cndl?.l ?? null,
      buy_vwap: a.bq ? a.bv / a.bq : null,
      sell_vwap: a.sq ? a.sv / a.sq : null,
    };
  }).sort((x, y) => (x.time < y.time ? -1 : 1));
}

// ── распределение значений спреда (гистограмма + сводка, как RATIO SUMMARY) ──
function distStats(values, bins = 24) {
  const v = values.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  const n = v.length;
  if (n < 3) return null;
  const last = values[values.length - 1];
  const mean = v.reduce((s, x) => s + x, 0) / n;
  const median = n % 2 ? v[(n - 1) / 2] : (v[n / 2 - 1] + v[n / 2]) / 2;
  const sd = Math.sqrt(v.reduce((s, x) => s + (x - mean) ** 2, 0) / Math.max(1, n - 1));
  const lo = v[0], hi = v[n - 1];
  const step = (hi - lo) / bins || 1;
  const hist = Array.from({ length: bins }, (_, i) => ({ lo: lo + i * step, hi: lo + (i + 1) * step, n: 0 }));
  for (const x of v) hist[Math.min(bins - 1, Math.floor((x - lo) / step))].n += 1;
  const pct = v.filter((x) => x <= last).length / n * 100;
  return { n, last, mean, median, sd, lo, hi, hist,
           z: sd ? (last - mean) / sd : null, offAvg: last - mean, pct };
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
  // панель спреда: линия по средневзвесу / свечи спреда / выкл (RVD-режим)
  const smode = sp.get("sm") || "line";
  const distOn = sp.get("dist") === "1";

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
  // дневная история спреда (spread_daily) — резерв, когда часовых баров нет
  const spreadOn = smode !== "off" && (tf === "1d" || tf === "1w");

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

  // бары нужны и панели спреда: спред считается по средневзвешенной цене
  const barsOn = on("vwap") || on("sides") || (smode !== "off" && layersOk);
  const qBars = useQuery({
    queryKey: ["hbars", isin, layerDays],
    queryFn: () => fetchHourlyBars(isin, { days: layerDays }),
    enabled: barsOn,
    staleTime: 300_000,
  });
  const qTrades = useQuery({
    queryKey: ["big-trades", isin, layerDays, bigMln],
    // order=value: с сортировкой по времени лимит срезал дальнюю половину окна
    // и маркеры обрывались на середине графика без всякого следа
    queryFn: () => fetchTrades(isin, { days: Math.min(layerDays, 400),
                                       minValue: bigMln * 1e6, limit: 400,
                                       order: "value" }),
    enabled: on("big"),
    staleTime: 300_000,
  });

  // Тип метрики спреда узнаём по самим барам: страница открывается по ISIN и
  // обслуживает и флоатеры (Y-IDX), и фиксы (G-спред) — у фикса y_idx пуст во
  // всех барах, и панель молча оставалась пустой.
  const sKind = spreadKindOf(qBars.data?.bars);
  const sLabel = SPREAD_LABEL[sKind];
  const qSpread = useQuery({
    queryKey: ["spread-hist", isin, sKind, from || "all"],
    queryFn: () => fetchSpreadHistory(isin, { kind: sKind === "g" ? "fixed" : "floater",
                                              from: from || isoBack(400), days: 400 }),
    enabled: spreadOn,
    staleTime: 300_000,
  });

  const layerPts = useMemo(() => {
    const rows = (qBars.data?.bars || []).filter((b) => {
      const d = b.ts.slice(0, 10);
      return (!from || d >= from) && (!to || d <= to);
    });
    return layerPoints(rows, tf, on("vwap"));
  }, [qBars.data, tf, from, to, layers, layersOk]); // eslint-disable-line

  const bigTrades = useMemo(() => {
    const rows = (qTrades.data?.trades || []).filter((t) => {
      const d = t.ts.slice(0, 10);
      return (!from || d >= from) && (!to || d <= to);
    });
    return rows;
  }, [qTrades.data, from, to]);

  // панель спреда есть либо от дневной истории, либо от часовых баров
  const spreadPaneOn = smode !== "off" && (spreadOn || (barsOn && layersOk));

  // Точки спреда для панели и распределения. Приоритет — свой архив (спред по
  // средневзвешенной цене), резерв — дневной снапшот spread_daily.
  const spreadPts = useMemo(() => {
    if (!spreadPaneOn) return [];
    const bars = layerPts.filter((p) => p.y_idx_bps != null).map((p) => ({
      time: p.time, value: p.y_idx_bps, thin: p.thin, src: "bars",
      o: p.y_o, h: p.y_h, l: p.y_l, c: p.y_c,
    }));
    const daily = (qSpread.data?.points || [])
      .map((p) => ({ time: p.date, value: sKind === "g" ? p.g_spread_bps : p.y_idx_bps }))
      .filter((p) => p.value != null);
    if (bars.length < 2) return daily;
    // на внутридневной сетке дневной снапшот не годится: одна точка на день
    // легла бы поверх часовых баров, да ещё по другой цене (закрытие vs VWAP)
    if (tf === "5m" || tf === "1h") return bars;
    if (smode === "candles" || smode === "hlc" || daily.length < 2) return bars;
    // Бары предпочтительнее: спред по средневзвесу/закрытию и честные значения
    // по выходным сессиям (вечерний снапшот воскресенья у короткой бумаги давал
    // выброс по тонкому клоузу). Daily — только когда бары НЕ покрывают окно
    // (глубина архива меньше периода). «Покрывают» — с допуском в неделю:
    // строгое сравнение дат проигрывало из-за выходных на границе окна.
    const barsFrom = String(bars[0].time).slice(0, 10);
    const covered = from
      ? barsFrom <= new Date(Date.parse(from) + 7 * 864e5).toISOString().slice(0, 10)
      : barsFrom <= daily[0].time;
    return covered ? bars : daily;
  }, [spreadPaneOn, layerPts, qSpread.data, smode, sKind, tf, from]);

  // Свечи и HLC у спреда возможны только там, где в баре есть внутридневной
  // разброс: дневная сетка склеена из часов. На 5м/1ч и на снапшотах — линия.
  const spreadOHLC = spreadPts.some((p) => p.o != null);
  // В распределение тонкие дни не идут: два случайных принта дают спред в
  // сотни б.п. и в одиночку растягивают шкалу, среднее и σ (см. Y_OHLC_MIN_DAY_VALUE)
  const fatPts = useMemo(() => spreadPts.filter((p) => !p.thin), [spreadPts]);
  // когда ликвидных дней почти нет (сплошной неликвид), фильтр не применяем —
  // иначе панель осталась бы пустой там, где данные всё-таки есть
  const distFiltered = fatPts.length > 5;
  const dist = useMemo(
    () => (distOn ? distStats((distFiltered ? fatPts : spreadPts).map((p) => p.value)) : null),
    [distOn, distFiltered, fatPts, spreadPts]);
  const distThin = distFiltered ? spreadPts.length - fatPts.length : 0;

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

    const pxFmt = { type: "price", precision: 2, minMove: 0.01 };
    let price = null;
    if (type === "off") {
      // ценовой график скрыт: пане 0 остаются слои (средневзвес/стороны);
      // маркеры крупных сделок цепляются к средневзвесу ниже
    } else if (type === "line") {
      price = chart.addSeries(AreaSeries, {
        lineColor: theme.accent, lineWidth: 2,
        topColor: theme.accent + "33", bottomColor: theme.accent + "05",
        priceFormat: pxFmt,
      }, 0);
      price.setData(candles.map((c) => ({ time: toTime(c.t, tf), value: c.c })));
    } else if (type === "hlc") {
      // HLC как в TradingView: тот же диапазон бара, что у свечи, но тремя
      // линиями — на длинном окне свечи сливаются в кашу, а огибающие high/low
      // и линия закрытия читаются. Близко к close-линии, только с коридором.
      // close — цветом текста, а не акцентом: акцентом идёт линия средневзвеса,
      // и на графике они ложились друг на друга неразличимо
      for (const [key, field, color, w] of [
        ["hi", "h", theme.up, 1],
        ["lo", "l", theme.down, 1],
        ["price", "c", theme.fg, 2],
      ]) {
        const s = chart.addSeries(LineSeries, {
          color, lineWidth: w, priceFormat: pxFmt,
          priceLineVisible: key === "price", lastValueVisible: key === "price",
        }, 0);
        s.setData(candles.map((c) => ({ time: toTime(c.t, tf), value: c[field] })));
        seriesRef.current[key] = s;
      }
      price = seriesRef.current.price;   // маркеры и легенда цепляются к close
    } else {
      price = chart.addSeries(CandlestickSeries, {
        upColor: theme.up, downColor: theme.down,
        borderUpColor: theme.up, borderDownColor: theme.down,
        wickUpColor: theme.up, wickDownColor: theme.down,
        priceFormat: pxFmt,
      }, 0);
      price.setData(candles.map((c) => ({
        time: toTime(c.t, tf), open: c.o, high: c.h, low: c.l, close: c.c })));
    }
    if (price) seriesRef.current.price = price;

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
      // ценовой график может быть выключен — маркеры сажаем на средневзвес
      const host = price || seriesRef.current.vwap;
      if (marks.length && host) seriesRef.current.marks = createSeriesMarkers(host, marks);
    }

    let yidxDrawn = false;
    if (spreadPaneOn && spreadPts.length > 1) {
      // O/H/L/C спреда собраны из часовых значений дня, поэтому и «тело» свечи,
      // и коридор HLC — это реальный разброс спреда внутри дня, а не пересчёт
      // цены свечи. Линия — средневзвешенное за день.
      const spFmt = { type: "price", precision: 0, minMove: 1 };
      yidxDrawn = true;
      if (spreadOHLC && smode === "hlc") {
        // Цвета синхронизированы с ЦЕНОВЫМ графиком, а не со своей осью: спред
        // обратен цене, минимум спреда — это максимум цены (зелёный), и наоборот.
        for (const [key, field, color, w] of [
          ["yhi", "h", theme.down, 1],
          ["ylo", "l", theme.up, 1],
          ["yidx", "c", theme.fg, 2],
        ]) {
          const s = chart.addSeries(LineSeries, {
            color, lineWidth: w, priceFormat: spFmt,
            priceLineVisible: key === "yidx", lastValueVisible: key === "yidx",
          }, 2);
          s.setData(spreadPts.filter((p) => p[field] != null)
            .map((p) => ({ time: p.time, value: p[field] })));
          seriesRef.current[key] = s;
        }
      } else if (spreadOHLC && smode === "candles") {
        const yidx = chart.addSeries(CandlestickSeries, {
          upColor: theme.up, downColor: theme.down,
          borderUpColor: theme.up, borderDownColor: theme.down,
          wickUpColor: theme.up, wickDownColor: theme.down,
          priceFormat: spFmt,
        }, 2);
        yidx.setData(spreadPts.filter((p) => p.o != null)
          .map((p) => ({ time: p.time, open: p.o, high: p.h, low: p.l, close: p.c })));
        seriesRef.current.yidx = yidx;
      } else {
        const yidx = chart.addSeries(LineSeries, {
          color: theme.fg, lineWidth: 2, priceFormat: spFmt,
        }, 2);
        yidx.setData(spreadPts.map((p) => ({ time: p.time, value: p.value })));
        seriesRef.current.yidx = yidx;
      }
    }

    setHasYidx(yidxDrawn);
    // RVD-раскладка: цена сверху, спред — второй полноценный график под ней,
    // объём между ними тонкой полосой (динамику спреда в узкой панели не видно)
    const panes = chart.panes();
    // цена выключена и слоёв нет — верхняя панель пустая, схлопываем её
    const pane0Empty = !seriesRef.current.price && !seriesRef.current.vwap
      && !seriesRef.current.buy && !seriesRef.current.sell;
    // раскладка по умолчанию: (цена+объём) и спред — по половине экрана
    // (3.3 + 0.7 сверху против 4 снизу), чтобы спред открывался сразу в
    // читаемом масштабе, а не узкой полоской
    if (panes[0]) panes[0].setStretchFactor(pane0Empty ? 0.15 : (yidxDrawn ? 3.3 : 4));
    if (panes[1]) panes[1].setStretchFactor(0.7);
    if (panes[2]) panes[2].setStretchFactor(4);
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
      measureDistGeom(c);
    });
    return () => cancelAnimationFrame(raf);
  }, [candles, type, tf, theme, spreadPaneOn, spreadPts, smode, spreadOHLC, layerPts,
      bigTrades, layers, layersOk]);

  // при смене окна/таймфрейма старая строка легенды осталась бы висеть от
  // предыдущего курсора — гасим
  useEffect(() => { setLegend(null); }, [tf, type, from, to]);

  // Правый отступ полосы-обзора = поле под ценовой шкалой. Прямые API его не
  // дают: chart.priceScale('right').width() возвращает 0, а вариант через
  // panes()[0] — 2px, потому что в v5 шкала рисуется поверх общего холста.
  // Меряем по координате крайнего бара при ПОЛНОМ окне: правее него область
  // данных не идёт. Вызывается и после fitContent (шкала уже отрисована), и на
  // каждом изменении видимого диапазона.
  // Геометрия панели спреда для гистограммы распределения: гистограмма должна
  // жить в ТОЙ ЖЕ системе координат (пиксель ↔ bps), что и панель спреда слева,
  // иначе связь уровней на графике и в распределении не видна глазом.
  const [distGeom, setDistGeom] = useState(null);
  const measureDistGeom = (chart) => {
    const s = seriesRef.current.yidx;
    const pane = chart?.panes?.()[2];
    if (!s || !pane) { setDistGeom(null); return; }
    const ph = pane.getHeight();
    const vTop = s.coordinateToPrice(0);
    const vBot = s.coordinateToPrice(ph);
    if (!ph || ph < 40 || vTop == null || vBot == null || vTop === vBot) return;
    const panes = chart.panes();
    let top = 0;
    for (let i = 0; i < 2; i++) top += (panes[i]?.getHeight() || 0) + 1; // +1 сепаратор
    setDistGeom((g) => (g && g.top === top && g.h === ph
                        && g.vTop === vTop && g.vBot === vBot)
      ? g : { top, h: ph, vTop, vBot });
  };

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
    const apply = () => {
      chart.resize(Math.max(320, Math.round(el.clientWidth)),
        Math.max(260, Math.round(el.clientHeight)));
      requestAnimationFrame(() => chartRef.current && measureDistGeom(chartRef.current));
    };
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
      // автоскейл спреда пересчитывается на пан/зум — двигаем и гистограмму
      measureDistGeom(chart);
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
      if (!param.time) { setLegend(null); return; }
      // ценовой график может быть выключен — легенда живёт на остальных сериях
      const p = (s.price ? param.seriesData.get(s.price) : null) || {};
      if (s.price && !param.seriesData.get(s.price)) { setLegend(null); return; }
      const val = (ser) => (ser ? param.seriesData.get(ser)?.value : null);
      // Спред может быть свечой — тогда в seriesData лежит open/high/low/close,
      // и close — это ПОСЛЕДНИЙ час дня, а не средневзвешенный за день. Их надо
      // разделять: подписать close как «Y-IDX закр.», а средневзвес взять из
      // самой точки (то, что рисует линия в режиме «линия»).
      const yd = s.yidx ? param.seriesData.get(s.yidx) : null;
      const pt = yd ? spreadPts.find((x) => x.time === param.time) : null;
      // в режиме HLC максимум/минимум лежат в отдельных сериях, а не в баре
      setLegend({
        time: param.time,
        o: p.open, h: p.high ?? val(s.hi), l: p.low ?? val(s.lo), c: p.close ?? p.value,
        v: param.seriesData.get(s.vol)?.value,
        y: yd ? (yd.value ?? yd.close) : null,
        yClose: !!(yd && (yd.value == null || s.yhi)),
        yAvg: pt?.value,
        yo: yd?.open ?? pt?.o,
        yh: yd?.high ?? val(s.yhi), yl: yd?.low ?? val(s.ylo),
        w: val(s.vwap), b: val(s.buy), sl: val(s.sell),
      });
    };
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
  }, [candles, type, spreadPaneOn, spreadPts]);

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
          {[["candles", "Свечи", "OHLC свечами"],
            ["hlc", "HLC", "Три линии: максимум, минимум, закрытие"],
            ["line", "Линия", "Только цена закрытия"],
            ["off", "Выкл", "Скрыть ценовой график (остаются слои и спред)"]].map(([k, l, hint]) => (
            <button key={k} type="button" title={hint}
              className={"cp-btn" + (type === k ? " on" : "")}
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
        <span className="cp-layers-k">спред</span>
        <span className="cp-group" role="group" aria-label="Панель спреда">
          {[["line", "Линия", `${sLabel}: по средневзвесу при включённом слое СРЕДНЕВЗВЕС, иначе по цене закрытия`],
            ["candles", "Свечи", "O/H/L/C спреда из часовых значений дня"],
            ["hlc", "HLC", "Три линии: макс/мин спреда за день и закрытие"],
            ["off", "Выкл", "убрать панель спреда"]].map(([k, l, hint]) => (
            <button key={k} type="button" title={hint}
              className={"cp-btn" + (smode === k ? " on" : "")}
              onClick={() => setParam({ sm: k })}>{l}</button>
          ))}
        </span>
        <button type="button" className={"cp-btn cp-reset" + (distOn ? " on" : "")}
          disabled={smode === "off"}
          title="Гистограмма распределения спреда за период + сводка"
          onClick={() => setParam({ dist: distOn ? null : "1" })}>Распределение</button>
        <span className="cp-hint">
          {(qBars.isPending && barsOn) || (qTrades.isPending && on("big"))
            ? "загрузка архива…"
            : barsOn || on("big")
              ? [
                  barsOn && layerPts.length ? `${layerPts.length} баров` : null,
                  on("big")
                    ? `${bigTrades.length} крупных сделок`
                      + (qTrades.data?.truncated
                          ? ` (из ${qTrades.data.total} — показаны самые крупные)` : "")
                    : null,
                  qTrades.data?.eff_spread_bps != null
                    ? `эфф. спред по крупным ${qTrades.data.eff_spread_bps} б.п. цены` : null,
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
            {legend.o != null && <> · O {fmt.pct(legend.o)} H {fmt.pct(legend.h)} L {fmt.pct(legend.l)} C {fmt.pct(legend.c)}</>}
            {/* HLC: открытия нет, но коридор дня показать надо */}
            {legend.o == null && legend.h != null && legend.l != null &&
              <> · H {fmt.pct(legend.h)} L {fmt.pct(legend.l)} C {fmt.pct(legend.c)}</>}
            {legend.o == null && legend.h == null && legend.c != null &&
              <> · цена {fmt.pct(legend.c)}</>}
            {legend.v ? <> · объём {fmt.num(legend.v, 0)}</> : null}
            {legend.w != null && <> · ср.взвес {fmt.pct(legend.w)}</>}
            {legend.b != null && <> · покупки {fmt.pct(legend.b)}</>}
            {legend.sl != null && <> · продажи {fmt.pct(legend.sl)}</>}
            {/* всё про спред одной группой: иначе «ср.взвес» цены и «ср.взвес»
                спреда стоят рядом в строке и читаются как одно и то же */}
            {legend.y != null && (legend.yClose
              ? <> · {sLabel} bps:{legend.yo != null && <> откр. {Math.round(legend.yo)} ·</>} закр. {Math.round(legend.y)}
                  {legend.yAvg != null && <> · ср. {Math.round(legend.yAvg)}</>}
                  {legend.yh != null && legend.yl != null && legend.yh !== legend.yl &&
                    <> · день {Math.round(legend.yl)} … {Math.round(legend.yh)}</>}</>
              : <> · {sLabel} {Math.round(legend.y)} bps</>)}
          </>
        ) : <span className="cp-legend-hint">наведи курсор на график — здесь будут цифры бара</span>}
      </div>
      </div>

      <div className="cp-row" style={{ height }}>
        <div className="cp-chart" ref={hostRef} />
        {distOn && <SpreadDist dist={dist} theme={theme} height={height} skipped={distThin}
          label={sLabel} geom={distGeom} />}
      </div>

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
            {/* источник смотрим по метке точки, а не по наличию OHLC: у часовых
                баров OHLC спреда нет, и подпись врала про «снапшот» */}
            {spreadPaneOn && hasYidx &&
              (spreadPts[0]?.src === "bars"
                ? (on("vwap")
                    ? " · спред по средневзвешенной цене (свой архив)"
                    : " · спред по цене закрытия (свой архив)")
                : " · спред по цене закрытия (дневной снапшот)")}
            {spreadPaneOn && !hasYidx && ` · история ${sLabel} за период пуста`}
            {spreadPaneOn && hasYidx && !spreadOHLC && (smode === "candles" || smode === "hlc") &&
              ` · ${smode === "hlc" ? "HLC" : "свечи"} спреда нет: в архиве только спред по средневзвесу (бары до пересчёта)`}
            {!spreadPaneOn && " · панель спреда выключена"}
            {on("big") && !bigTrades.length &&
              ` · сделок крупнее ${bigMln} млн ₽ в архиве нет (глубина тиков ~30 дней)`}
          </>
        )}
      </div>
    </div>
  );
}

// ── распределение спреда: сводка + горизонтальная гистограмма ────────────────
// Ось значений вертикальная — та же ориентация, что у панели спреда слева:
// глазом видно, в какой части своего диапазона сидит текущий уровень.
const SUMMARY_H = 150;

function SpreadDist({ dist, theme, height, skipped = 0, label = "Y-IDX", geom = null }) {
  if (!dist || !theme) {
    return <div className="cp-dist"><div className="cp-dist-empty">мало точек спреда</div></div>;
  }
  // aligned: гистограмма живёт в системе координат ПАНЕЛИ СПРЕДА (пиксель↔bps
  // как у шкалы графика, та же вертикальная позиция) — уровни совпадают глазом.
  // Без геометрии (панель спреда не отрисована) — прежний автономный масштаб.
  const aligned = !!(geom && geom.h > 40 && geom.vTop !== geom.vBot);
  const h = aligned ? height : Math.max(120, height - SUMMARY_H);
  const w = 208, padL = 6, padR = 44, padT = 8, padB = 16;
  const maxN = Math.max(...dist.hist.map((b) => b.n)) || 1;
  const span = (dist.hi - dist.lo) || 1;
  const y = aligned
    ? (v) => geom.top + (geom.vTop - v) / (geom.vTop - geom.vBot) * geom.h
    : (v) => padT + (dist.hi - v) / span * (h - padT - padB);
  const barW = (n) => n / maxN * (w - padL - padR);
  const fixedRowH = Math.max(2, (h - padT - padB) / dist.hist.length - 1);
  const yLast = y(dist.last);
  const row = (k, v, cls) => (
    <div className="cp-dist-row"><span>{k}</span><b className={cls}>{v}</b></div>
  );
  const n0 = (x) => Math.round(x).toLocaleString("ru-RU");

  return (
    <div className="cp-dist">
      <div className="cp-dist-top">
      <div className="cp-dist-head" title={skipped ? "тонкие дни (оборот < 1 млн ₽) в статистику не идут" : ""}>
        Распределение {label} · {dist.n} набл.{skipped ? ` · −${skipped} тонк.` : ""}
      </div>
      <div className="cp-dist-sum">
        {row("Текущий", n0(dist.last), "hi")}
        {row("Среднее", n0(dist.mean))}
        {row("От среднего", (dist.offAvg >= 0 ? "+" : "") + n0(dist.offAvg),
             dist.offAvg >= 0 ? "up" : "down")}
        {row("Медиана", n0(dist.median))}
        {row("Ст.откл.", n0(dist.sd))}
        {row("σ от среднего", dist.z != null ? dist.z.toFixed(2) : "—")}
        {row("Перцентиль", dist.pct.toFixed(1) + "%")}
      </div>
      </div>
      <svg width={w} height={h} role="img" aria-label={`Гистограмма распределения ${label}`}
        style={aligned ? { position: "absolute", left: 0, top: 0 } : undefined}>
        {aligned && (
          <defs>
            {/* всё, что вылезает за вертикаль панели спреда, срезаем */}
            <clipPath id="cp-dist-clip">
              <rect x="0" y={geom.top} width={w} height={geom.h} />
            </clipPath>
          </defs>
        )}
        <g clipPath={aligned ? "url(#cp-dist-clip)" : undefined}>
          {dist.hist.map((b, i) => {
            const y1 = y(b.hi);
            const hh = aligned ? Math.max(1.5, y(b.lo) - y1 - 1) : fixedRowH;
            return (
              <rect key={i} x={padL} y={y1} width={barW(b.n)} height={hh}
                fill={theme.accent} opacity={b.lo <= dist.last && dist.last <= b.hi ? 0.95 : 0.45} />
            );
          })}
          <line x1={padL} x2={w - padR + 4} y1={yLast} y2={yLast}
            stroke={theme.fg} strokeWidth="1" strokeDasharray="3 2" />
          <text x={w - padR + 6} y={yLast + 3} fontSize="10" fill={theme.fg}
            fontFamily="var(--mono)">{n0(dist.last)}</text>
          {/* границы прячем, когда они налезают на подпись текущего: на узком
              диапазоне (несколько наблюдений) три числа сливались в кашу */}
          {Math.abs(y(dist.hi) + 8 - yLast) > 11 && (
            <text x={w - padR + 6} y={y(dist.hi) + 8} fontSize="9" fill={theme.mut}
              fontFamily="var(--mono)">{n0(dist.hi)}</text>
          )}
          {Math.abs(y(dist.lo) - yLast) > 11 && (
            <text x={w - padR + 6} y={y(dist.lo)} fontSize="9" fill={theme.mut}
              fontFamily="var(--mono)">{n0(dist.lo)}</text>
          )}
        </g>
      </svg>
    </div>
  );
}
