/**
 * График витрины ОФЗ на фикстурах: своя кривая (samples), полоса объёма, тени
 * точек, строка Δ кривой по key_tenors и полые точки вне подгонки.
 *
 * MeasuredSvg рисует только по реальной ширине контейнера, а в jsdom она ноль —
 * подменяем getBoundingClientRect, иначе тест проверял бы пустой div. Бэк-ручки
 * здесь не нужны: компонент чисто рисующий, данные — по контракту ТЗ
 * (docs/ofz_curve_tz.md).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import OfzChart, { curveDelta, normCurve } from "./OfzChart.jsx";

const PTS = [
  { isin: "RU000A26238", name: "26238", x: 6.2, y: 15.1, resid: 12, used: true, curve: 14.98, px: 55.2, base: "wap",
    x0: 6.25, y0: 15.0, yb: 15.18, ya: 15.02, pb: 55.0, pa: 55.4 },
  { isin: "RU000A26240", name: "26240", x: 4.1, y: 15.4, resid: -8, used: true, curve: 15.48, px: 61.0, base: "wap",
    x0: null, y0: null },
  { isin: "RU000A26247", name: "26247", x: 7.9, y: 14.9, resid: 3, used: true, curve: 14.87, px: 82.3, base: "wap",
    x0: 7.95, y0: 15.15 },
];
// короткая бумага: остаток есть, но в подгонку не вошла (τ < 0.25)
const PT_OUT = { isin: "RU000A26234", name: "26234", x: 0.2, y: 16.9, resid: 40, used: false,
  curve: 16.5, px: 99.1, base: "wap", x0: null, y0: null };

// сегодняшняя кривая — samples ручки объектами, прошлая — парами (оба формата
// принимаются, как и раньше)
const CURVE_NOW = [{ years: 1, yield_pct: 16.5 }, { years: 3, yield_pct: 15.5 },
  { years: 5, yield_pct: 15.2 }, { years: 10, yield_pct: 15.0 }];
const CURVE_PREV = [[1, 16.42], [3, 15.45], [5, 15.2], [10, 15.02], [15, 14.9]];
// key_tenors двух ответов: 0.5Y сегодня — экстраполяция, 15Y — вчера
const KT_NOW = [
  { years: 0.5, yield_pct: 17.0, extrap: true }, { years: 1, yield_pct: 16.5, extrap: false },
  { years: 3, yield_pct: 15.5, extrap: false }, { years: 5, yield_pct: 15.2, extrap: false },
  { years: 10, yield_pct: 15.0, extrap: false }, { years: 15, yield_pct: 14.95, extrap: false },
];
const KT_PREV = [
  { years: 0.5, yield_pct: 16.8, extrap: false }, { years: 1, yield_pct: 16.42, extrap: false },
  { years: 3, yield_pct: 15.45, extrap: false }, { years: 5, yield_pct: 15.2, extrap: false },
  { years: 10, yield_pct: 15.02, extrap: false }, { years: 15, yield_pct: 14.9, extrap: true },
];
const VOLUMES = {
  date: "2026-09-15", live: true,
  items: {
    RU000A26238: { total: 3.5e9, book: 2.0e9, rps: 1.5e9, other: 0, boards: { TQOB: 2.0e9, PSOB: 1.5e9 } },
    RU000A26247: { total: 8e8, book: 8e8, rps: 0, other: 0, boards: { TQOB: 8e8 } },
  },
};
const CMP = { date: "2026-09-11", requested: "2026-09-12", curve: CURVE_PREV, keyTenors: KT_PREV,
  curveDate: "2026-09-11", curveRequested: "2026-09-12" };

beforeEach(() => {
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    width: 800, height: 330, top: 0, left: 0, right: 800, bottom: 330, x: 0, y: 0,
  });
});
afterEach(cleanup);

describe("Δ кривой ОФЗ по ключевым тенорам", () => {
  it("считает разность по совпадающим тенорам, экстраполированные не показывает", () => {
    const d = curveDelta(KT_NOW, KT_PREV);
    expect(d.map((x) => x.tenor)).toEqual([1, 3, 5, 10]);   // 0.5Y extrap сегодня, 15Y — вчера
    expect(d[0].bps).toBeCloseTo(8, 6);
    expect(d[3].bps).toBeCloseTo(-2, 6);
  });
  it("без прошлых теноров — пусто, не падает", () => {
    expect(curveDelta(KT_NOW, null)).toEqual([]);
    expect(curveDelta(undefined, KT_PREV)).toEqual([]);
  });
  it("нормализует оба формата samples", () => {
    expect(normCurve(CURVE_PREV)[0]).toEqual({ years: 1, yield_pct: 16.42 });
    expect(normCurve(CURVE_NOW)[1]).toEqual({ years: 3, yield_pct: 15.5 });
  });
});

describe("OfzChart", () => {
  it("рисует свою кривую и стек-столбики объёма под точками", () => {
    const { container } = render(
      <OfzChart pts={PTS} curve={CURVE_NOW} keyTenors={KT_NOW} cmp={null} volumes={VOLUMES} labels={false} />);
    expect(screen.getByTestId("ofz-curve")).toBeTruthy();
    // две бумаги с оборотом → два столбика; у первой стакан И РПС, у второй только стакан
    expect(container.querySelectorAll("g.ofz-vol").length).toBe(2);
    expect(container.querySelectorAll("rect.ofz-vol-book").length).toBe(2);
    expect(container.querySelectorAll("rect.ofz-vol-rps").length).toBe(1);
    expect(container.querySelectorAll("rect.ofz-vol-other").length).toBe(0);
    // цвет точки — по знаку отклонения к своей кривой
    expect(container.querySelectorAll("circle.ofz-pt.cheap").length).toBe(2);
    expect(container.querySelectorAll("circle.ofz-pt.rich").length).toBe(1);
    // без сравнения ни теней, ни строки Δ
    expect(container.querySelectorAll(".ofz-pt-ghost").length).toBe(0);
    expect(screen.queryByTestId("ofz-dstrip")).toBeNull();
  });

  it("в режиме сравнения рисует кривую as-of, тени с коннекторами и строку Δ кривой", () => {
    const { container } = render(
      <OfzChart pts={PTS} curve={CURVE_NOW} keyTenors={KT_NOW} cmp={CMP} volumes={null} labels={false} />);
    expect(container.querySelectorAll("path.ofz-kbd-cmp").length).toBe(1);
    // тень только у бумаг с as-of доходностью (у 26240 её нет)
    expect(container.querySelectorAll("circle.ofz-pt-ghost").length).toBe(2);
    expect(container.querySelectorAll("line.ofz-conn").length).toBe(2);
    expect(container.querySelectorAll("circle.ofz-pt").length).toBe(3);
    const strip = screen.getByTestId("ofz-dstrip");
    expect(strip.textContent).toMatch(/Δ кривая ОФЗ к 11\.09\.2026/);
    expect(strip.textContent).not.toMatch(/КБД/);
    expect(strip.textContent).toMatch(/ближайший торговый к 12\.09\.2026/);
    expect(strip.textContent).toMatch(/1Y \+8/);
    expect(strip.textContent).toMatch(/10Y -2/);
    expect(strip.textContent).not.toMatch(/15Y/);
    expect(strip.textContent).not.toMatch(/6M/);
  });

  it("точка вне подгонки (used=false) рисуется полым кружком с пометкой в тултипе", () => {
    const { container } = render(
      <OfzChart pts={[...PTS, PT_OUT]} curve={CURVE_NOW} keyTenors={KT_NOW} cmp={null} volumes={null} labels={false} />);
    expect(container.querySelectorAll("circle.ofz-pt").length).toBe(4);
    const out = container.querySelectorAll("circle.ofz-pt.ofz-pt-out");
    expect(out.length).toBe(1);
    expect(out[0].style.fill).toBe("none");
    // заполненные точки inline-стиля не получают — их красит CSS
    const filled = container.querySelector("circle.ofz-pt:not(.ofz-pt-out)");
    expect(filled.style.fill).toBe("");
    expect(container.textContent).not.toMatch(/КБД/);
  });

  it("бид/оффер: полоска только по чипу и только у бумаг со стаканом", () => {
    const off = render(
      <OfzChart pts={PTS} curve={CURVE_NOW} keyTenors={KT_NOW} cmp={null} volumes={null} labels={false} bidAsk={false} />);
    expect(off.container.querySelectorAll("g.ofz-ba").length).toBe(0);
    cleanup();
    const on = render(
      <OfzChart pts={PTS} curve={CURVE_NOW} keyTenors={KT_NOW} cmp={null} volumes={null} labels={false} bidAsk={true} />);
    expect(on.container.querySelectorAll("g.ofz-ba").length).toBe(1);   // стакан есть только у 26238
    expect(on.container.querySelectorAll("line.ofz-ba-bid").length).toBe(1);
    expect(on.container.querySelectorAll("line.ofz-ba-ask").length).toBe(1);
  });

  it("оборота нет — столбиков и шкалы объёма нет, график не падает", () => {
    const { container } = render(
      <OfzChart pts={PTS} curve={CURVE_NOW} keyTenors={KT_NOW} cmp={null} volumes={{ items: {} }} labels={false} />);
    expect(container.querySelectorAll(".ofz-vol").length).toBe(0);
    expect(container.textContent).not.toMatch(/оборот, млн/);
    expect(container.querySelectorAll(".ofz-pt").length).toBe(PTS.length);
  });

  it("подписи точек — отклонение к своей кривой, не g-спред", () => {
    const { container } = render(
      <OfzChart pts={PTS} curve={CURVE_NOW} keyTenors={KT_NOW} cmp={null} volumes={null} labels={true} />);
    const lbls = [...container.querySelectorAll("text.an-pt-lbl")].map((t) => t.textContent);
    expect(lbls.some((t) => /26238 \+12/.test(t))).toBe(true);
    expect(lbls.some((t) => /26240 -8/.test(t))).toBe(true);
  });
});
