import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams, useNavigate, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  createChart, CandlestickSeries, LineSeries, HistogramSeries, AreaSeries, CrosshairMode,
} from "lightweight-charts";
import {
  fetchCandles, fetchSpreadHistory, fetchBondDetails, fetchBondRow,
  fetchHourlyBars, fetchTrades, fetchBlocksByIsin,
} from "../api.js";
import { fmt, baseLabel } from "../format.js";
import { Brush } from "../charts/index.js";

// Полноэкранный график выпуска (своя вкладка, /chart/:isin).
// Сверху — строка параметров бумаги, ниже — график на всю высоту окна:
// цена (свечи/линия) с объёмом внутри той же зоны и Y-IDX отдельной панелью
// под ней — с общей осью времени.
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
  ["vwap", "Средневзвес", "VWAP часа (свой архив) + R-spread по нему на внутридневном масштабе"],
  ["sides", "Покупки/продажи", "VWAP по агрессору: buy и sell отдельными линиями"],
  ["big", "Крупные сделки", "Маркеры отдельных сделок крупнее порога"],
  ["rps", "РПС/блоки", "Адресные сделки (РПС, РПС с ЦК, размещения, выкупы) — "
          + "в стакане и обезличенной ленте их нет вообще"],
];
const BIG_THRESHOLDS = [1, 5, 10, 50, 100];   // млн ₽
// Крупные сделки — треугольники по цене принта: вверх покупка (агрессор брал по
// аску), вниз продажа. Цвет НЕОНОВЫЙ и намеренно свой, а не тема: приглушённые
// --up/--down совпадают со свечами, и на зелёной свече зелёный маркер пропадал;
// белая заливка (как было раньше) терялась среди линий слоёв. Форма дублирует
// сторону, поэтому маркер читается и без цвета.
// Адресные (РПС) остаются точкой: у них агрессора нет, направление рисовать нечем.
const TRADE_DOT = { rps: "#a855f7" };
const TRADE_TRI = { buy: "#00e676", sell: "#ff1f4b" };
const TRADE_DOT_R = 4.5;
const TRI_W = 4.5;    // половина основания треугольника, px
const TRI_H = 7;      // высота, px

// Треугольники рисуем своим примитивом: маркеры серии садятся НАД/ПОД баром, а
// нужна ровно цена сделки; у point markers формы нет, только круг.
function tradeMarksPrimitive(getMarks) {
  let chart = null, series = null;
  const view = {
    zOrder: () => "top",
    renderer: () => ({
      draw: (target) => target.useMediaCoordinateSpace(({ context: ctx }) => {
        if (!chart || !series) return;
        const ts = chart.timeScale();
        const { stroke, marks } = getMarks();
        ctx.save();
        ctx.strokeStyle = stroke;
        ctx.lineWidth = 1.2;
        for (const m of marks) {
          ctx.fillStyle = m.side === "sell" ? TRADE_TRI.sell : TRADE_TRI.buy;
          const x = ts.timeToCoordinate(m.time);
          const y = series.priceToCoordinate(m.price);
          if (x == null || y == null) continue;
          const dir = m.side === "sell" ? -1 : 1;   // вниз / вверх
          ctx.beginPath();
          ctx.moveTo(x, y - dir * TRI_H / 2);           // вершина
          ctx.lineTo(x - TRI_W, y + dir * TRI_H / 2);
          ctx.lineTo(x + TRI_W, y + dir * TRI_H / 2);
          ctx.closePath();
          ctx.fill();
          ctx.stroke();
        }
        ctx.restore();
      }),
    }),
  };
  return {
    attached: (p) => { chart = p.chart; series = p.series; },
    detached: () => { chart = null; series = null; },
    updateAllViews: () => {},
    paneViews: () => [view],
  };
}
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
// hex → rgba с заданной альфой: сетка должна быть еле заметной, а CSS-переменные
// отдают непрозрачный цвет. Не-hex значения оставляем как есть.
function alpha(c, a) {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((c || "").trim());
  if (!m) return c;
  const h = m[1].length === 3 ? m[1].split("").map((x) => x + x).join("") : m[1];
  const n = parseInt(h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

// затемнение + альфа: тело свечи глушим, а контур и фитили остаются исходного
// цвета — так свеча читается силуэтом, а не пятном, и линии слоёв поверх неё
// не тонут в заливке
const shade = (c, k, a) => {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((c || "").trim());
  if (!m) return c;
  const h = m[1].length === 3 ? m[1].split("").map((x) => x + x).join("") : m[1];
  const n = parseInt(h, 16);
  const d = (v) => Math.round(v * k);
  return `rgba(${d((n >> 16) & 255)}, ${d((n >> 8) & 255)}, ${d(n & 255)}, ${a})`;
};
const BODY_K = 0.62;    // насколько темнее исходного цвета
const BODY_A = 0.55;    // прозрачность тела

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
    // средневзвес — неон, а не тусклый акцент: линия идёт поверх свечей и на
    // тёмной теме тонула в них
    vwapC: v("--vwap", "#00d5ff"),
    // спред — своя серия со своим цветом: раньше линия шла цветом текста и
    // сливалась с ценой закрытия/HLC, а в легенде цифры спреда невозможно было
    // отличить глазом от цифр цены
    spread: v("--spread", "#e0932f"),
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
const SPREAD_LABEL = { y: "R-spread", g: "G-спред" };
// спред по цене самой сделки (бэк считает его тем же reprice, что уровни стакана)
const tradeSpread = (t) => (t?.y_idx_bps != null ? t.y_idx_bps
  : t?.g_spread_bps != null ? t.g_spread_bps : null);

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
      // px — сами цены часа: по ним видно, ИЗ ЧЕГО получилась цифра спреда
      a.hours.push({ y, v: b.volume, ohlc: barSpreadOHLC(b), close: b.close });
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
      // цена закрытия дня — закрытие последнего часа, попавшего в склейку:
      // база спреда, когда слой СРЕДНЕВЗВЕС выключен
      close: src.length ? src[src.length - 1].close : null,
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
  const nav = useNavigate();

  // «Назад» = НАЗАД ПО ИСТОРИИ, а не «на карточку выпуска»: на график приходят
  // из ленты сделок, из списка, из сигналов — и возвращаться человек хочет
  // туда же, со своими фильтрами. Прямая ссылка/F5 истории не имеет
  // (router держит индекс записи в history.state.idx) — там прежний переход
  // на карточку остаётся единственным осмысленным.
  const canGoBack = (window.history.state?.idx ?? 0) > 0;
  const goBack = () => (canGoBack ? nav(-1) : nav(`/floaters?isin=${isin}`));

  const period = sp.get("p") || "3m";
  const custom = { from: sp.get("from"), to: sp.get("to") };
  const isCustom = !!(custom.from || custom.to);
  const defTf = PERIODS.find(([k]) => k === period)?.[3] || "1d";
  const tf = sp.get("tf") || (isCustom ? "1d" : defTf);
  const type = sp.get("type") || "candles";
  // панель спреда: линия по средневзвесу / HLC / выкл (RVD-режим).
  // Свечи спреда убраны: дневная свеча склеивалась из часовых значений и на
  // редких днях выдавала диапазон, которого на рынке не было. Старые ссылки с
  // sm=candles открываются линией.
  const smodeRaw = sp.get("sm") || "line";
  const smode = smodeRaw === "candles" ? "line" : smodeRaw;
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
  // refresh: false — дренить тики Alor тут не надо: тот же архив уже дотянул
  // запрос часовых баров выше (он делает ta.drain на те же 30 дней), а оба
  // запроса на бэке идут под одним замком по ISIN — и слой сделок ЖДАЛ конца
  // второго, уже лишнего, дрейна. Порог берём в ключ, но старые точки держим на
  // экране (placeholderData), чтобы переключение «крупнее, млн ₽» не мигало.
  // Тип метрики спреда узнаём по самим барам: страница открывается по ISIN и
  // обслуживает и флоатеры (Y-IDX), и фиксы (G-спред) — у фикса y_idx пуст во
  // всех барах, и панель молча оставалась пустой.
  const sKind = spreadKindOf(qBars.data?.bars);
  const sLabel = SPREAD_LABEL[sKind];
  const keepPrev = (prev) => prev;
  const qTrades = useQuery({
    queryKey: ["big-trades", isin, layerDays, bigMln, sKind],
    // order=value: с сортировкой по времени лимит срезал дальнюю половину окна
    // и маркеры обрывались на середине графика без всякого следа
    queryFn: () => fetchTrades(isin, { days: Math.min(layerDays, 400),
                                       minValue: bigMln * 1e6, limit: 400,
                                       order: "value", refresh: !barsOn,
                                       kind: sKind === "g" ? "fixed" : "floater" }),
    enabled: on("big"),
    staleTime: 300_000,
    placeholderData: keepPrev,
  });
  const qBlocks = useQuery({
    queryKey: ["rps-blocks", isin, layerDays, bigMln],
    queryFn: () => fetchBlocksByIsin(isin, { days: Math.min(layerDays, 400),
                                             minValue: bigMln * 1e6, limit: 400 }),
    enabled: on("rps"),
    staleTime: 300_000,
    placeholderData: keepPrev,
  });

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

  const rpsTrades = useMemo(() => {
    // только адресные: безадресный крупняк уже рисует слой «Крупные сделки»,
    // иначе одна и та же сделка получила бы два маркера
    const rows = (qBlocks.data?.blocks || []).filter((t) => {
      if (!t.negotiated) return false;
      const d = t.ts.slice(0, 10);
      return (!from || d >= from) && (!to || d <= to);
    });
    return rows;
  }, [qBlocks.data, from, to]);

  // панель спреда есть либо от дневной истории, либо от часовых баров
  const spreadPaneOn = smode !== "off" && (spreadOn || (barsOn && layersOk));

  // Видимое окно графика во ВРЕМЕНИ: по нему режется выборка распределения
  // (зум в месяц — статистика месяца). Ставится подпиской на timeScale ниже.
  const [visTimes, setVisTimes] = useState(null);

  // Точки спреда для панели и распределения. Приоритет — свой архив (спред по
  // средневзвешенной цене), резерв — дневной снапшот spread_daily.
  const spreadPts = useMemo(() => {
    if (!spreadPaneOn) return [];
    // px — ЦЕНА, по которой посчитана точка спреда (средневзвешенная или
    // закрытие — смотря включён ли слой СРЕДНЕВЗВЕС). Без неё цифру спреда
    // нельзя сверить с ценовым графиком: непонятно, из чего она получилась.
    const bars = layerPts.filter((p) => p.y_idx_bps != null).map((p) => ({
      time: p.time, value: p.y_idx_bps, thin: p.thin, src: "bars",
      o: p.y_o, h: p.y_h, l: p.y_l, c: p.y_c,
      px: on("vwap") ? p.vwap_pct : (p.close ?? p.vwap_pct),
    }));
    // Тонкий день у снапшота: своего оборота строка spread_daily не несёт, но
    // дневная свеча MOEX за ту же дату — несёт. Без этой пометки снапшот-часть
    // тащила в распределение дни с парой принтов (пример: одиночная сделка даёт
    // спред в 400+ б.п. там, где весь месяц ~100) — у баров такие дни отсекались,
    // а у снапшота нет, и статистика панели ехала.
    const dayVal = new Map(candles.map((c) => [c.t.slice(0, 10), (c.v || 0) * 1000 * (c.c || 100) / 100]));
    const daily = (qSpread.data?.points || [])
      .map((p) => ({ time: p.date, value: sKind === "g" ? p.g_spread_bps : p.y_idx_bps,
                     px: p.price ?? null, src: "daily",
                     thin: (dayVal.get(p.date) ?? Infinity) < Y_OHLC_MIN_DAY_VALUE }))
      .filter((p) => p.value != null);
    if (bars.length < 2) return daily;
    // на внутридневной сетке дневной снапшот не годится: одна точка на день
    // легла бы поверх часовых баров, да ещё по другой цене (закрытие vs VWAP)
    if (tf === "5m" || tf === "1h") return bars;
    if (smode === "hlc" || daily.length < 2) return bars;
    // СКЛЕЙКА, а не «или-или». Бары лучше (спред по средневзвесу/закрытию, честные
    // значения по выходным сессиям), но архив мельче окна: раньше при нехватке
    // глубины ВСЯ панель падала на дневной снапшот — и слой СРЕДНЕВЗВЕС переставал
    // влиять на спред даже там, где бары есть. Теперь снапшот закрывает только
    // хвост слева, до первой даты архива, а дальше идут бары.
    const barsFrom = String(bars[0].time).slice(0, 10);
    const head = daily.filter((p) => p.time < barsFrom);
    return head.length ? [...head, ...bars] : bars;
  }, [spreadPaneOn, layerPts, qSpread.data, smode, sKind, tf, from, candles]);

  // HLC у спреда возможен только там, где в баре есть внутридневной разброс:
  // дневная сетка склеена из часов. На 5м/1ч и на снапшотах — линия.
  const spreadOHLC = spreadPts.some((p) => p.o != null);   // см. shownPts ниже
  // В распределение идут ВСЕ дни, включая тонкие: сделка есть сделка, а отбор
  // «нормальных» оборотов делал статистику несравнимой с самой линией.
  // время у точек — либо ISO-дата (дневная сетка), либо UNIX-секунды (внутри дня);
  // видимое окно приходит в том же виде, поэтому сравниваем как есть
  const distVis = useMemo(
    () => (visTimes
      ? spreadPts.filter((p) => p.time >= visTimes.from && p.time <= visTimes.to)
      : spreadPts),
    [spreadPts, visTimes]);
  const dist = useMemo(
    () => (distOn ? distStats(distVis.map((p) => p.value)) : null),
    [distOn, distVis]);
  // Линия рисует ВСЕ дни, включая выходные сессии: сделки там настоящие, и
  // спред по ним — тоже рынок, пусть и тонкий. Тонкие дни выключены только из
  // СТАТИСТИКИ распределения: одна такая точка перекашивает среднее и σ, по
  // которым читается «дорого/дёшево».
  const shownPts = spreadPts;

  // Чем прайсится линия/свеча спреда — на самом деле, а не по умолчанию.
  // Источник виден по метке точки: свой архив баров или дневной снапшот.
  // Дата стыка склейки: с неё точки идут из своего архива баров, левее — снапшот.
  const spreadJoin = useMemo(() => {
    if (!shownPts.length || shownPts[0].src !== "daily") return null;
    const first = shownPts.find((p) => p.src === "bars");
    return first ? String(first.time).slice(0, 10) : null;
  }, [shownPts]);

  const spreadBase = useMemo(() => {
    const daily = shownPts.length && shownPts[0].src === "daily";
    if (daily && !spreadJoin) {
      return { label: "закр. дня (снапшот)",
               hint: "дневной снапшот spread_daily: цена закрытия дня, вечерний расчёт. "
                     + "Берётся, когда архив часовых баров не покрывает окно" };
    }
    if (daily) {
      return { label: `снапшот → ${on("vwap") ? "VWAP" : "закр."}`,
               hint: `склейка: до ${spreadJoin} — дневной снапшот spread_daily (цена `
                     + "закрытия, вечерний расчёт), дальше — свой архив часовых баров "
                     + `(спред по ${on("vwap") ? "средневзвешенной цене" : "цене закрытия"}). `
                     + "База каждой точки — в строке под курсором" };
    }
    if (smode === "hlc") {
      return { label: "O/H/L/C часов",
               hint: "свеча спреда: открытие — по цене открытия первого часа дня, "
                     + "закрытие — по закрытию последнего, макс/мин — по всем ценам "
                     + "всех часов (часы тоньше 1% дневного объёма отброшены)" };
    }
    return on("vwap")
      ? { label: "VWAP",
          hint: "спред по средневзвешенной цене часа (value/volume/номинал), "
                + "на дневной сетке — взвешенной по объёму за день. "
                + "Выключи слой СРЕДНЕВЗВЕС, чтобы считать по цене закрытия" }
      : { label: "закр.",
          hint: "спред по цене закрытия часа/дня. Включи слой СРЕДНЕВЗВЕС, "
                + "чтобы считать по средневзвешенной цене" };
  }, [shownPts, spreadJoin, smode, layers, layersOk]);   // eslint-disable-line

  // ── график ────────────────────────────────────────────────────────────────
  const wrapRef = useRef(null);
  const hostRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef({});
  // сделки под точками: время бара → сама сделка (цена, оборот, режим) для легенды
  const tradeAtRef = useRef({ buy: new Map(), sell: new Map(), rps: new Map() });
  const theme = useThemeVars(wrapRef);
  const [legend, setLegend] = useState(null);
  // счётчик пересозданий графика — см. эффект создания ниже
  const [chartVer, setChartVer] = useState(0);
  const [hasYidx, setHasYidx] = useState(false);
  // окно brush'а — в логических индексах баров (тех же, что у timeScale)
  const [visRange, setVisRange] = useState(null);
  // ширина ценовой шкалы справа: полоса-обзор должна кончаться там же, где
  // область данных графика, иначе окно не совпадает с тем, что видно
  const [rightPad, setRightPad] = useState(0);

  const [height, setHeight] = useState(420);
  // Высоту графика раздаёт РАСКЛАДКА, а не арифметика: .chart-page — колонка на
  // всю доступную высоту, .cp-row забирает остаток после шапки, полосы-обзора и
  // подвала. Прежний расчёт вычитал слагаемые вручную (34 на подвал, отступы) и
  // регулярно промахивался: подвал уезжал под статус-бар, а после подгрузки
  // данных страница оставалась длиннее окна до первого ресайза.
  // Здесь же измеряем ФАКТ — он нужен панели распределения и геометрии.
  const rowRef = useRef(null);
  useLayoutEffect(() => {
    const el = rowRef.current;
    if (!el) return undefined;
    const measure = () => setHeight(Math.max(260, Math.round(el.clientHeight)));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
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
      // сетка почти прозрачная: она ориентир, а не рисунок
      grid: { vertLines: { color: alpha(theme.line2, 0.35) },
              horzLines: { color: alpha(theme.line2, 0.35) } },
      rightPriceScale: { borderColor: theme.line },
      timeScale: { borderColor: theme.line, timeVisible: tf === "5m" || tf === "1h", secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      localization: { locale: "ru-RU" },
    });
    chartRef.current = chart;
    // Версия инстанса: эффекты, которые ПОДПИСЫВАЮТСЯ на график (легенда под
    // курсором, слежение за видимым окном), обязаны перезапуститься после
    // каждого пересоздания. Без неё подписка терялась, когда данные приходили
    // из кэша раньше темы: эффект отрабатывал при chartRef.current === null и
    // больше не звался, потому что его собственные зависимости не менялись —
    // крестик рисовался, а строка цифр бара оставалась пустой.
    setChartVer((v) => v + 1);
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
        chart.removeSeries(s);
      } catch { /* серия уже снята */ }
    }
    seriesRef.current = {};

    // priceLineVisible: false везде — пунктирный уровень последней цены/спреда
    // тянулся через весь график и мешал читать сами серии
    const pxFmt = { type: "price", precision: 2, minMove: 0.01 };
    let price = null;
    if (type === "off") {
      // ценовой график скрыт: пане 0 остаются слои (средневзвес/стороны);
      // маркеры крупных сделок цепляются к средневзвесу ниже
    } else if (type === "line") {
      price = chart.addSeries(AreaSeries, {
        lineColor: theme.accent, lineWidth: 2,
        topColor: theme.accent + "33", bottomColor: theme.accent + "05",
        priceFormat: pxFmt, priceLineVisible: false,
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
          priceLineVisible: false, lastValueVisible: key === "price",
        }, 0);
        s.setData(candles.map((c) => ({ time: toTime(c.t, tf), value: c[field] })));
        seriesRef.current[key] = s;
      }
      price = seriesRef.current.price;   // маркеры и легенда цепляются к close
    } else {
      price = chart.addSeries(CandlestickSeries, {
        // тело — приглушённая заливка, контур и фитили прежним цветом
        upColor: shade(theme.up, BODY_K, BODY_A),
        downColor: shade(theme.down, BODY_K, BODY_A),
        borderUpColor: theme.up, borderDownColor: theme.down,
        wickUpColor: theme.up, wickDownColor: theme.down,
        priceFormat: pxFmt, priceLineVisible: false,
      }, 0);
      price.setData(candles.map((c) => ({
        time: toTime(c.t, tf), open: c.o, high: c.h, low: c.l, close: c.c })));
    }
    if (price) seriesRef.current.price = price;

    // ── объём: не отдельная зона, а оверлей в НИЖНЕЙ ЧЕТВЕРТИ ценовой панели ──
    // Своя невидимая шкала ("vol"): масштаб объёма не трогает шкалу цены, а
    // scaleMargins прижимают гистограмму к низу. Цена сверху ужимается тем же
    // способом — снизу остаётся полоса под столбики.
    const vol = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false,
      priceScaleId: "vol",
    }, 0);
    vol.setData(candles.map((c) => ({
      time: toTime(c.t, tf), value: c.v || 0,
      color: (c.c >= c.o ? theme.up : theme.down) + "66",
    })));
    vol.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    seriesRef.current.vol = vol;
    const pxHost = seriesRef.current.price || seriesRef.current.hi;
    if (pxHost) pxHost.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.24 } });

    // ── слой «средневзвес»: VWAP часа поверх свечей ──────────────────────────
    if (on("vwap")) {
      const pts = layerPts.filter((p) => p.vwap_pct != null);
      if (pts.length > 1) {
        const vwap = chart.addSeries(LineSeries, {
          color: theme.vwapC, lineWidth: 2, lineStyle: 0,
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

    // ── отметки сделок: крупные безадресные + адресные (РПС) ────────────────
    // Не маркеры серии (те садятся НАД/ПОД баром), а свои серии на той же
    // ценовой шкале: отметка стоит ровно на цене сделки — видно, по какой
    // стороне диапазона прошёл принт. Цифры сделки — в строке под курсором.
    // dots — кружки (РПС), tri — белые треугольники (крупные, сторона формой).
    const mkSeries = (key, rows, opts) => {
      const pts = rows
        .filter((t) => t.price != null)
        .map((t) => ({ time: t.time, value: t.price }))
        .sort((a, b) => (a.time > b.time ? 1 : a.time < b.time ? -1 : 0));
      if (!pts.length) return null;
      const s = chart.addSeries(LineSeries, {
        lineVisible: false, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false, priceFormat: pxFmt, ...opts,
      }, 0);
      s.setData(pts);
      seriesRef.current[key] = s;
      return s;
    };
    const dots = (key, rows, color) =>
      mkSeries(key, rows, { color, pointMarkersVisible: true,
                            pointMarkersRadius: TRADE_DOT_R });
    // одна точка на бар: в час приходит несколько крупных принтов, кружки легли
    // бы друг на друга — берём самый крупный (у безадресных — по каждой стороне)
    const pickBest = (rows, keyOf) => {
      const best = new Map();
      for (const t of rows) {
        const time = tradeTime(t.ts, tf);
        const k = keyOf(time, t);
        const prev = best.get(k);
        if (!prev || (t.value || 0) > (prev.value || 0)) best.set(k, { ...t, time });
      }
      return [...best.values()];
    };
    const bigDots = on("big") ? pickBest(bigTrades, (time, t) => `${time}|${t.side}`) : [];
    const rpsDots = on("rps") ? pickBest(rpsTrades, (time) => time) : [];
    // Крупные: серия-якорь без своей отрисовки (нужна ради автомасштаба и
    // перевода цены в пиксели), поверх неё примитив рисует треугольники.
    // у серии на одно время может быть только одна точка, а в баре есть и
    // покупка, и продажа: якорю оставляем одну (обе цены внутри диапазона бара,
    // масштаб от этого не меняется), треугольники рисуются по полному списку
    const anchor = [...new Map(bigDots.map((t) => [String(t.time), t])).values()];
    const bigHost = mkSeries("dotBig", anchor, { color: "rgba(0,0,0,0)" });
    if (bigHost) {
      const marks = bigDots.filter((t) => t.price != null)
        .map((t) => ({ time: t.time, price: t.price, side: t.side }));
      // обводка цветом ФОНА: неон на любой свече отбивается своей же тёмной/
      // светлой каймой и не сливается с телом бара
      bigHost.attachPrimitive(tradeMarksPrimitive(
        () => ({ stroke: theme.bg, marks })));
    }
    dots("dotRps", rpsDots, TRADE_DOT.rps);
    // сделка под курсором — по времени бара: в легенде показываем цену, оборот
    // и спред по цене принта. Точка на графике одна (самая крупная в баре по
    // стороне), поэтому к ней же клеим «сколько всего таких сделок в баре и на
    // какую сумму» — иначе крупная серия принтов выглядела бы как одна сделка.
    const rollup = (rows, keyOf) => {
      const agg = new Map();
      for (const t of rows) {
        const k = keyOf(String(tradeTime(t.ts, tf)), t);
        const a = agg.get(k) || { n: 0, value: 0 };
        a.n += 1; a.value += t.value || 0;
        agg.set(k, a);
      }
      return agg;
    };
    const bigAgg = on("big") ? rollup(bigTrades, (time, t) => `${time}|${t.side}`) : new Map();
    const rpsAgg = on("rps") ? rollup(rpsTrades, (time) => time) : new Map();
    const withAgg = (t, agg, k) => ({ ...t, bar: agg.get(k) });
    tradeAtRef.current = {
      buy: new Map(bigDots.filter((t) => t.side !== "sell")
        .map((t) => [String(t.time), withAgg(t, bigAgg, `${t.time}|${t.side}`)])),
      sell: new Map(bigDots.filter((t) => t.side === "sell")
        .map((t) => [String(t.time), withAgg(t, bigAgg, `${t.time}|${t.side}`)])),
      rps: new Map(rpsDots.map((t) => [String(t.time), withAgg(t, rpsAgg, String(t.time))])),
    };

    let yidxDrawn = false;
    if (spreadPaneOn && shownPts.length > 1) {
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
          ["yidx", "c", theme.spread, 2],
        ]) {
          const s = chart.addSeries(LineSeries, {
            color, lineWidth: w, priceFormat: spFmt,
            priceLineVisible: false, lastValueVisible: key === "yidx",
          }, 1);
          s.setData(shownPts.filter((p) => p[field] != null)
            .map((p) => ({ time: p.time, value: p[field] })));
          seriesRef.current[key] = s;
        }
      } else {
        // Заливка под линией (AreaSeries) вместо голой линии: панель спреда —
        // половина экрана, и одна нитка цветом текста на ней терялась. Градиент
        // относительный (от самой линии вниз), поэтому работает и на
        // отрицательных значениях спреда.
        const yidx = chart.addSeries(AreaSeries, {
          lineColor: theme.spread, lineWidth: 2,
          topColor: alpha(theme.spread, 0.28), bottomColor: alpha(theme.spread, 0.02),
          crosshairMarkerRadius: 4, crosshairMarkerBorderColor: theme.bg,
          priceFormat: spFmt, priceLineVisible: false,
        }, 1);
        yidx.setData(shownPts.map((p) => ({ time: p.time, value: p.value })));
        seriesRef.current.yidx = yidx;
        // средний уровень окна — пунктиром: без него «дорого/дёшево» на глаз не
        // читается, приходилось лезть в панель распределения
        const fin = shownPts.map((p) => p.value).filter((x) => Number.isFinite(x));
        if (fin.length > 2) {
          const avg = fin.reduce((s, x) => s + x, 0) / fin.length;
          yidx.createPriceLine({
            price: Math.round(avg), color: alpha(theme.spread, 0.55),
            lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "ср.",
          });
        }
        // стык склейки: слева снапшот, справа свой архив — вертикали у
        // lightweight-charts нет, помечаем сменой цвета точки на первом баре
        if (spreadJoin) {
          const mark = chart.addSeries(LineSeries, {
            color: alpha(theme.spread, 0.9), lineVisible: false,
            pointMarkersVisible: true, pointMarkersRadius: 3,
            priceLineVisible: false, lastValueVisible: false,
            crosshairMarkerVisible: false, priceFormat: spFmt,
          }, 1);
          const at = shownPts.find((p) => p.src === "bars");
          if (at) mark.setData([{ time: at.time, value: at.value }]);
          seriesRef.current.yjoin = mark;
        }
      }
    }

    setHasYidx(yidxDrawn);
    // RVD-раскладка: цена (с объёмом внутри той же зоны) сверху, спред — второй
    // полноценный график под ней, по половине экрана каждый
    const panes = chart.panes();
    // цена выключена и слоёв нет — верхняя панель пустая, схлопываем её
    const pane0Empty = !seriesRef.current.price && !seriesRef.current.vwap
      && !seriesRef.current.buy && !seriesRef.current.sell;
    // при выключенной цене в верхней зоне остаётся один объём — узкая полоса
    if (panes[0]) panes[0].setStretchFactor(pane0Empty ? 0.7 : 4);
    if (panes[1]) panes[1].setStretchFactor(4);
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
  }, [candles, type, tf, theme, spreadPaneOn, shownPts, spreadJoin, smode, spreadOHLC,
      layerPts, bigTrades, rpsTrades, layers, layersOk]);

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
    const pane = chart?.panes?.()[1];
    if (!s || !pane) { setDistGeom(null); return; }
    const ph = pane.getHeight();
    const vTop = s.coordinateToPrice(0);
    const vBot = s.coordinateToPrice(ph);
    if (!ph || ph < 40 || vTop == null || vBot == null || vTop === vBot) return;
    const panes = chart.panes();
    const top = (panes[0]?.getHeight() || 0) + 1;   // +1 сепаратор
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
      // распределение считается по ВИДИМОЙ части: зум в месяц должен показывать
      // статистику месяца, а не всего загруженного окна
      const vr = ts.getVisibleRange();
      setVisTimes((prev) => (prev && vr && prev.from === vr.from && prev.to === vr.to)
        ? prev : (vr ? { from: vr.from, to: vr.to } : null));
    };
    ts.subscribeVisibleLogicalRangeChange(onRange);
    onRange(ts.getVisibleLogicalRange());
    return () => ts.unsubscribeVisibleLogicalRangeChange(onRange);
  }, [candles, type, spreadPaneOn, theme, tf, chartVer]);

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
        // база спреда: цена, по которой он посчитан, и какая именно это цена
        yPx: pt?.px, yPxKind: pt?.src === "daily" ? "закр. дня (снапшот)"
          : on("vwap") ? "ср.взвес" : "закр.",
        w: val(s.vwap), b: val(s.buy), sl: val(s.sell),
        // сделки под точками этого бара: цена и оборот — подписей на графике
        // больше нет, цифры живут здесь
        trades: [
          tradeAtRef.current.buy.get(String(param.time)),
          tradeAtRef.current.sell.get(String(param.time)),
          tradeAtRef.current.rps.get(String(param.time)),
        ].filter(Boolean),
      });
    };
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
  }, [candles, type, spreadPaneOn, spreadPts, chartVer]);

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

  // подписи в легенде красим в цвета соответствующих серий графика
  const cHi = { color: theme?.up };
  const cLo = { color: theme?.down };
  const cAcc = { color: theme?.vwapC };
  const cSp = { color: theme?.spread };

  const tfDepth = TF_DEPTH_DAYS[tf];
  const wantDays = isCustom || !from ? null : Math.round((Date.now() - Date.parse(from)) / 864e5);
  const depthShort = wantDays != null && tfDepth != null && wantDays > tfDepth;

  return (
    <div className="chart-page" ref={wrapRef}>
      <div className="cp-top">
      <div className="cp-head">
        <div className="cp-id">
          <button type="button" className="cp-back" onClick={goBack}
            title={canGoBack ? "Назад — туда, откуда пришли" : "К карточке выпуска"}>←</button>
          <span className="cp-name" title={row?.emitter_name || ""}>{name}</span>
          <span className="cp-isin">{isin}</span>
          {row?.rating && <span className="cp-rating">{row.rating}</span>}
          {/* карточка выпуска со стаканом — тот же Drawer, что в МОНИТОРЕ:
              он живёт вне Routes и открывается параметром ?isin= на любой
              странице, поэтому график под ним остаётся на месте */}
          <button type="button" className="cp-btn cp-reset cp-ob"
            title="Карточка выпуска: стакан, параметры, купоны"
            onClick={() => setParam({ isin, k: sKind === "g" ? "fixed" : null })}>
            Стакан
          </button>
        </div>
        <div className="cp-stats">
          {row?.emitter_name && stat("эмитент", row.emitter_name)}
          {stat("формула", r ? `${baseLabel(r.base_rate_type)} + ${r.spread_bps}` : null)}
          {stat("цена", m?.last_price_pct != null ? fmt.pct(m.last_price_pct) + "%" : null)}
          {/* цветом линии спреда на панели: та же метрика — тот же цвет */}
          {stat("R-spread", v?.yield_over_index_bps != null
            ? <span style={cSp}>{v.yield_over_index_bps} bps</span> : null, "hi")}
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
        {(on("big") || on("rps")) && (
          <label className="cp-date">крупнее, млн ₽
            <select value={bigMln} onChange={(e) => setParam({ mv: e.target.value })}>
              {BIG_THRESHOLDS.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </label>
        )}
        {/* что означает отметка: треугольник вверх/вниз — сторона агрессора */}
        {(on("big") || on("rps")) && (
          <span className="cp-layers-k cp-dot-key">
            {on("big") && <i style={{ color: TRADE_TRI.buy }}>▲ покупка</i>}
            {on("big") && <i style={{ color: TRADE_TRI.sell }}>▼ продажа</i>}
            {on("rps") && <i style={{ color: TRADE_DOT.rps }}>● РПС</i>}
          </span>
        )}
        <span className="cp-layers-k" style={cSp}>спред</span>
        <span className="cp-group" role="group" aria-label="Панель спреда">
          {[["line", "Линия", `${sLabel}: по средневзвесу при включённом слое СРЕДНЕВЗВЕС, иначе по цене закрытия`],
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
        {/* база спреда видна ВСЕГДА, а не только под курсором: одна и та же
            бумага даёт разную цифру по средневзвесу и по закрытию */}
        {smode !== "off" && (
          <span className="cp-layers-k cp-base" title={spreadBase.hint}>
            по цене: {spreadBase.label}
          </span>
        )}
        <span className="cp-hint">
          {(qBars.isPending && barsOn) || (qTrades.isPending && on("big"))
            || (qBlocks.isPending && on("rps"))
            ? "загрузка архива…"
            : barsOn || on("big") || on("rps")
              ? [
                  barsOn && layerPts.length ? `${layerPts.length} баров` : null,
                  on("big")
                    ? `${bigTrades.length} крупных сделок`
                      + (qTrades.data?.truncated
                          ? ` (из ${qTrades.data.total} — показаны самые крупные)` : "")
                    : null,
                  on("rps") ? `${rpsTrades.length} адресных (РПС)` : null,
                  qTrades.data?.eff_spread_bps != null
                    ? `эфф. спред по крупным ${qTrades.data.eff_spread_bps} б.п. цены` : null,
                ].filter(Boolean).join(" · ")
              : "средневзвес и сделки — из своего архива"}
        </span>
      </div>

      {/* Строка под курсором рендерится ВСЕГДА: раньше она появлялась только
          при наведении — блок возникал и исчезал, остаток высоты под график
          пересчитывался, и график прыгал вместе с масштабом. Пустая строка
          держит место. */}
      <div className="cp-legend">
        {legend && candles.length > 0 ? (
          <>
            <b>{typeof legend.time === "string" ? fmt.date(legend.time)
              : new Date(legend.time * 1000).toISOString().slice(0, 16).replace("T", " ")}</b>
            {/* Цвет подписи = цвет серии на графике: глазами видно, к чему цифра
                относится, без чтения самих слов (ср.взвес — акцент, покупки/
                продажи — up/down, спред — своя охра) */}
            {legend.o != null && <> · O {fmt.pct(legend.o)} <span style={cHi}>H {fmt.pct(legend.h)}</span> <span style={cLo}>L {fmt.pct(legend.l)}</span> C {fmt.pct(legend.c)}</>}
            {/* HLC: открытия нет, но коридор дня показать надо */}
            {legend.o == null && legend.h != null && legend.l != null &&
              <> · <span style={cHi}>H {fmt.pct(legend.h)}</span> <span style={cLo}>L {fmt.pct(legend.l)}</span> C {fmt.pct(legend.c)}</>}
            {legend.o == null && legend.h == null && legend.c != null &&
              <> · цена {fmt.pct(legend.c)}</>}
            {legend.v ? <> · объём {fmt.num(legend.v, 0)}</> : null}
            {legend.w != null && <span style={cAcc}> · ср.взвес {fmt.pct(legend.w)}</span>}
            {legend.b != null && <span style={cHi}> · покупки {fmt.pct(legend.b)}</span>}
            {legend.sl != null && <span style={cLo}> · продажи {fmt.pct(legend.sl)}</span>}
            {/* всё про спред одной группой: иначе «ср.взвес» цены и «ср.взвес»
                спреда стоят рядом в строке и читаются как одно и то же */}
            {legend.y != null && (legend.yClose
              ? <span style={cSp}> · {sLabel} bps:{legend.yo != null && <> откр. {Math.round(legend.yo)} ·</>} закр. {Math.round(legend.y)}
                  {legend.yAvg != null && <> · ср. {Math.round(legend.yAvg)}</>}
                  {legend.yh != null && legend.yl != null && legend.yh !== legend.yl &&
                    <> · день {Math.round(legend.yl)} … {Math.round(legend.yh)}</>}</span>
              : <span style={cSp}> · {sLabel} {Math.round(legend.y)} bps</span>)}
            {/* по какой цене посчитан спред: без этого цифру не сверить с
                ценовым графиком (средневзвес / закрытие / снапшот дня) */}
            {legend.y != null && legend.yPx != null && (
              // цену базы не дублируем, когда она уже стоит в строке слоем
              // СРЕДНЕВЗВЕС — тогда достаточно назвать саму базу
              legend.yPxKind === "ср.взвес" && legend.w != null
                ? <> (по ср.взвесу)</>
                : <> (по цене {fmt.pct(legend.yPx)}, {legend.yPxKind})</>)}
            {/* сделки, отмеченные точками на этом баре: цена принта и оборот */}
            {legend.trades?.map((t, i) => (
              <span key={i} className="cp-legend-trade"
                style={{ color: t.negotiated ? TRADE_DOT.rps
                  : t.side === "sell" ? TRADE_TRI.sell : TRADE_TRI.buy }}>
                {" · "}{t.negotiated ? "● " : t.side === "sell" ? "▼ " : "▲ "}
                {t.negotiated ? (t.board_title || "РПС")
                  : t.side === "sell" ? "продажа" : "покупка"}
                {" "}{fmt.pct(t.price)} · {fmt.mln(t.value)} млн ₽
                {/* спред ПО ЦЕНЕ ПРИНТА — тем же reprice, что уровни стакана */}
                {tradeSpread(t) != null && <> · {sLabel} {tradeSpread(t)}</>}
                {/* маркер один на бар (самый крупный), остальные — числом */}
                {t.bar && t.bar.n > 1 &&
                  <> · ещё {t.bar.n - 1} на {fmt.mln(t.bar.value - (t.value || 0))} млн ₽</>}
              </span>
            ))}
          </>
        ) : <span className="cp-legend-hint">наведи курсор на график — здесь будут цифры бара</span>}
      </div>
      </div>

      <div className="cp-row" ref={rowRef}>
        <div className="cp-chart" ref={hostRef}>
          {distOn && <SpreadDist dist={dist} theme={theme}
            geom={distGeom} rightPad={rightPad} />}
        </div>
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
            {spreadPaneOn && hasYidx && (
              <span style={cSp}>
                {shownPts[0]?.src === "bars"
                  ? (on("vwap")
                      ? " · спред по средневзвешенной цене (свой архив)"
                      : " · спред по цене закрытия (свой архив)")
                  : spreadJoin
                    ? ` · спред: дневной снапшот (закр.) до ${fmt.date(spreadJoin)},`
                      + ` дальше свой архив (${on("vwap") ? "средневзвес" : "закр."})`
                    : " · спред по цене закрытия (дневной снапшот)"}
              </span>
            )}
            {spreadPaneOn && !hasYidx && ` · история ${sLabel} за период пуста`}
            {spreadPaneOn && hasYidx && !spreadOHLC && smode === "hlc" &&
              " · HLC спреда нет: в архиве только спред по средневзвесу (бары до пересчёта)"}
            {!spreadPaneOn && " · панель спреда выключена"}
            {on("big") && !bigTrades.length &&
              ` · сделок крупнее ${bigMln} млн ₽ в архиве нет (глубина тиков ~30 дней)`}
            {on("rps") && !rpsTrades.length &&
              ` · адресных сделок от ${bigMln} млн ₽ нет: поштучный сбор РПС идёт вперёд`
              + " с момента запуска, за прошлые дни у ISS только дневные обороты (вкладка КРУПНЫЕ)"}
          </>
        )}
      </div>
    </div>
  );
}

// ── распределение спреда: гистограмма ВНУТРИ панели спреда, у правого края ──
// Профиль объёма, только по спреду: бары растут влево от ценовой шкалы, в той же
// системе координат (пиксель ↔ bps), что и сама панель — уровень концентрации
// читается прямо напротив линии. Выборка — ВИДИМАЯ часть окна: зум перестраивает
// профиль. Ни колонки справа, ни плашки со сводкой: цифры среднего/медианы
// человек и так читает по самой панели, а плашка перекрывала линию.
const DIST_W = 132;          // ширина поля гистограммы, px

function SpreadDist({ dist, theme, geom = null, rightPad = 0 }) {
  const aligned = !!(geom && geom.h > 40 && geom.vTop !== geom.vBot);
  if (!dist || !theme || !aligned) return null;

  const y = (v) => geom.top + (geom.vTop - v) / (geom.vTop - geom.vBot) * geom.h;
  const maxN = Math.max(...dist.hist.map((b) => b.n)) || 1;
  const barW = (n) => Math.max(1, n / maxN * (DIST_W - 10));
  const yLast = y(dist.last);
  // бары рисуем от правого края поля влево
  const right = DIST_W;

  return (
      <svg className="cp-dist-svg" width={DIST_W} height={geom.top + geom.h}
        style={{ right: rightPad + 2 }} aria-hidden="true">
        <clipPath id="cp-dist-clip">
          <rect x="0" y={geom.top} width={DIST_W} height={geom.h} />
        </clipPath>
        <g clipPath="url(#cp-dist-clip)">
          {dist.hist.map((b, i) => {
            const y1 = y(b.hi);
            const hh = Math.max(1.5, y(b.lo) - y1 - 1);
            const cur = b.lo <= dist.last && dist.last <= b.hi;
            return (
              <rect key={i} x={right - barW(b.n)} y={y1} width={barW(b.n)} height={hh}
                fill={theme.spread} opacity={cur ? 0.85 : 0.32} />
            );
          })}
          {/* уровень текущего значения — та же координата, что у линии слева */}
          <line x1={0} x2={right} y1={yLast} y2={yLast}
            stroke={theme.spread} strokeWidth="1" strokeDasharray="3 2" opacity="0.9" />
        </g>
      </svg>
  );
}
