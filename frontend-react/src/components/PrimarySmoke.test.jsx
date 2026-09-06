// Смоук вкладки ПЕРВИЧКА: сборка не ловит ошибки рантайма, а обе новые панели
// рисуют SVG руками (шкалы, пути, ховер) — там легко обратиться к полю,
// которого в ответе нет, и получить белый экран уже в браузере.
//
// Данные — форма ответа /api/primary/placements и /api/primary/slices, включая
// пограничные случаи, ради которых код и написан: строка без спреда (не флоатер
// реестра), структурная бумага с несопоставимой ценой вторички (debut_odd),
// пустой кросс-срез (спред ещё не считался — линий нет, но панель обязана
// нарисоваться).
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import PlacementHistory from "./PlacementHistory.jsx";
import PrimarySlices from "./PrimarySlices.jsx";

const PLACEMENTS = {
  rows: [
    { secid: "RU000A100001", isin: "RU000A100001", shortname: "ТЕСТ 1Р-01",
      emitter: "Альфа", rating: "AA", base: "KEYRATE", margin_bps: 150,
      coupons_per_year: 12, coupon_text: "КС + 1,5%", in_registry: true,
      first_date: "2026-08-12", last_date: "2026-08-12", days: 1, numtrades: 42,
      value_rub: 5e9, volume: 5e6, wa_price: 100, price_min: 100, price_max: 100,
      spread_bps: 171, premium_bps: -7, after_date: "2026-09-11",
      placed_pct: 100, placed_src: "moex", debut_pct: 0.35,
      debut_price: 100.35, debut_date: "2026-08-13", active: 0, is_ofz: false },
    { secid: "RU000A100002", isin: "RU000A100002", shortname: "НОТА 1",
      emitter: "Бета", base: null, in_registry: false,
      first_date: "2026-07-01", last_date: "2026-07-30", days: 12, numtrades: 3,
      value_rub: 2e8, volume: 2e5, wa_price: 100, price_min: 100, price_max: 100,
      spread_bps: null, premium_bps: null, placed_pct: 40, placed_src: "own",
      debut_pct: null, debut_odd: true, debut_price: 54, debut_date: "2026-07-02",
      active: 1, is_ofz: false },
  ],
  stats: { issues: 2, d_min: "2025-09-08" }, truncated: false,
};

const SLICES = {
  months: [{ month: "2026-08", issues: 2, value_rub: 5.2e9, margin_med_bps: 150,
             spread_med_bps: 171, premium_med_bps: -7, priced: 1 }],
  grades: [{ grade: "AA", issues: 2, value_rub: 5.2e9, margin_med_bps: 150,
             spread_med_bps: 171, premium_med_bps: -7, priced: 1 }],
  bases: [{ base: "KEYRATE", issues: 2, value_rub: 5.2e9, margin_med_bps: 150,
            spread_med_bps: 171, premium_med_bps: -7, priced: 1 }],
  month_grade: [
    { month: "2026-06", grade: "AAA", issues: 3, spread_med_bps: 140, value_rub: 1e9 },
    { month: "2026-07", grade: "AAA", issues: 2, spread_med_bps: 152, value_rub: 1e9 },
    { month: "2026-08", grade: "AAA", issues: 4, spread_med_bps: 168, value_rub: 1e9 },
    { month: "2026-08", grade: "AA", issues: 1, spread_med_bps: 210, value_rub: 1e9 },
  ],
  premiums: [-120, -40, -7, 0, 15, 86, 1183],
  issues: 2, only_floaters: true,
};

const wrap = (node) => (
  <QueryClientProvider client={new QueryClient({
    defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter>{node}</MemoryRouter>
  </QueryClientProvider>
);

// В jsdom у элементов нулевая ширина, а графики рисуют SVG только после замера
// контейнера (useChartSize: measured = w > 0). Без этой заглушки тест проверял
// бы пустые <div> и не увидел бы ошибку внутри самого рисования.
beforeEach(() => {
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(
    { width: 640, height: 220, top: 0, left: 0, right: 640, bottom: 220, x: 0, y: 0 });
  vi.stubGlobal("fetch", vi.fn(async (url) => ({
    ok: true, status: 200,
    json: async () => (String(url).includes("/slices") ? SLICES : PLACEMENTS),
  })));
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("смоук вкладки ПЕРВИЧКА", () => {
  it("таблица размещений рисуется со всеми колонками", async () => {
    render(wrap(<PlacementHistory />));
    expect(await screen.findByText("ТЕСТ 1Р-01")).toBeTruthy();
    // дебют со знаком и «≠» у структурной бумаги — обе ветки ячейки
    expect(screen.getByText("+0,35")).toBeTruthy();
    expect(screen.getByText("≠")).toBeTruthy();
    // полоска объёма живёт под числом
    expect(document.querySelector(".pl-bar")).toBeTruthy();
    // эмитент кликабелен (фильтрует список)
    expect(screen.getByTitle("Все выпуски: Альфа")).toBeTruthy();
  });

  it("срезы рисуют график динамики и гистограмму премии", async () => {
    render(wrap(<PrimarySlices />));
    expect(await screen.findByText("Динамика спреда")).toBeTruthy();
    // линии грейдов и столбики гистограммы реально нарисованы
    expect(document.querySelectorAll("svg path[stroke]").length).toBeGreaterThan(0);
    expect(document.querySelectorAll("rect.pm-wider, rect.pm-tighter").length)
      .toBeGreaterThan(2);
  });

  it("срезы не падают, когда спред ещё не посчитан", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({ ...SLICES, month_grade: [], premiums: [] }),
    })));
    render(wrap(<PrimarySlices />));
    expect(await screen.findByText(/Спред ещё не посчитан/)).toBeTruthy();
  });
});
