/**
 * График витрины ОФЗ на фикстурах: полоса объёма, тени точек и строка Δ КБД.
 *
 * MeasuredSvg рисует только по реальной ширине контейнера, а в jsdom она ноль —
 * подменяем getBoundingClientRect, иначе тест проверял бы пустой div. Бэк-ручки
 * здесь не нужны: компонент чисто рисующий, данные — по контракту ТЗ.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import OfzChart, { curveDelta, normCurve } from "./OfzChart.jsx";

const PTS = [
  { isin: "RU000A26238", name: "26238", x: 6.2, y: 15.1, g: 12, curve: 14.98, px: 55.2, base: "wap",
    x0: 6.25, y0: 15.0 },
  { isin: "RU000A26240", name: "26240", x: 4.1, y: 15.4, g: -8, curve: 15.48, px: 61.0, base: "wap",
    x0: null, y0: null },
  { isin: "RU000A26247", name: "26247", x: 7.9, y: 14.9, g: 3, curve: 14.87, px: 82.3, base: "wap",
    x0: 7.95, y0: 15.15 },
];
// сегодняшняя кривая — объектами (как /gcurve сейчас), прошлая — парами (как в ТЗ)
const CURVE_NOW = [{ years: 1, yield_pct: 16.5 }, { years: 3, yield_pct: 15.5 },
  { years: 5, yield_pct: 15.2 }, { years: 10, yield_pct: 15.0 }];
const CURVE_PREV = [[1, 16.42], [3, 15.45], [5, 15.2], [10, 15.02], [15, 14.9]];
const VOLUMES = {
  date: "2026-09-15", live: true,
  items: {
    RU000A26238: { total: 3.5e9, book: 2.0e9, rps: 1.5e9, other: 0, boards: { TQOB: 2.0e9, PSOB: 1.5e9 } },
    RU000A26247: { total: 8e8, book: 8e8, rps: 0, other: 0, boards: { TQOB: 8e8 } },
  },
};
const CMP = { date: "2026-09-11", requested: "2026-09-12", curve: CURVE_PREV,
  curveDate: "2026-09-11", curveRequested: "2026-09-12" };

beforeEach(() => {
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    width: 800, height: 330, top: 0, left: 0, right: 800, bottom: 330, x: 0, y: 0,
  });
});
afterEach(cleanup);

describe("Δ КБД по тенорам", () => {
  it("считает разность только по совпадающим тенорам, без интерполяции", () => {
    const d = curveDelta(CURVE_NOW, CURVE_PREV);
    expect(d.map((x) => x.tenor)).toEqual([1, 3, 5, 10]);   // 15Y есть только в прошлой
    expect(d[0].bps).toBeCloseTo(8, 6);
    expect(d[3].bps).toBeCloseTo(-2, 6);
  });
  it("нормализует оба формата points", () => {
    expect(normCurve(CURVE_PREV)[0]).toEqual({ years: 1, yield_pct: 16.42 });
    expect(normCurve(CURVE_NOW)[1]).toEqual({ years: 3, yield_pct: 15.5 });
  });
});

describe("OfzChart", () => {
  it("рисует стек-столбики объёма под точками", () => {
    const { container } = render(
      <OfzChart pts={PTS} curve={CURVE_NOW} cmp={null} volumes={VOLUMES} labels={false} />);
    // две бумаги с оборотом → два столбика; у первой стакан И РПС, у второй только стакан
    expect(container.querySelectorAll("g.ofz-vol").length).toBe(2);
    expect(container.querySelectorAll("rect.ofz-vol-book").length).toBe(2);
    expect(container.querySelectorAll("rect.ofz-vol-rps").length).toBe(1);
    expect(container.querySelectorAll("rect.ofz-vol-other").length).toBe(0);
    // без сравнения ни теней, ни строки Δ
    expect(container.querySelectorAll(".ofz-pt-ghost").length).toBe(0);
    expect(screen.queryByTestId("ofz-dstrip")).toBeNull();
  });

  it("в режиме сравнения рисует вторую кривую, тени с коннекторами и строку Δ КБД", () => {
    const { container } = render(
      <OfzChart pts={PTS} curve={CURVE_NOW} cmp={CMP} volumes={null} labels={false} />);
    expect(container.querySelectorAll("path.ofz-kbd-cmp").length).toBe(1);
    // тень только у бумаг с as-of доходностью (у 26240 её нет)
    expect(container.querySelectorAll("circle.ofz-pt-ghost").length).toBe(2);
    expect(container.querySelectorAll("line.ofz-conn").length).toBe(2);
    expect(container.querySelectorAll("circle.ofz-pt").length).toBe(3);
    const strip = screen.getByTestId("ofz-dstrip");
    expect(strip.textContent).toMatch(/Δ КБД к 11\.09\.2026/);
    expect(strip.textContent).toMatch(/ближайший торговый к 12\.09\.2026/);
    expect(strip.textContent).toMatch(/1Y \+8/);
    expect(strip.textContent).toMatch(/10Y -2/);
    expect(strip.textContent).not.toMatch(/15Y/);
  });

  it("оборота нет — столбиков и шкалы объёма нет, график не падает", () => {
    const { container } = render(
      <OfzChart pts={PTS} curve={CURVE_NOW} cmp={null} volumes={{ items: {} }} labels={false} />);
    expect(container.querySelectorAll(".ofz-vol").length).toBe(0);
    expect(container.textContent).not.toMatch(/оборот, млн/);
    expect(container.querySelectorAll(".ofz-pt").length).toBe(PTS.length);
  });
});
