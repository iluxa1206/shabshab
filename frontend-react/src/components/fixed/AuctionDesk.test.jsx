// Смоук вкладки АУКЦИОН: все три вида рисуются на форме ответов /api/auctions,
// включая пограничные случаи, ради которых код и написан: несостоявшийся
// аукцион (цены NULL), ДРПА под своим аукционом, будущие даты графика без
// факта, квартал без плана. Графики — свой SVG, в jsdom они рисуются только
// после замера контейнера (мок getBoundingClientRect, как в PrimarySmoke).
import { render, screen, fireEvent, cleanup, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import AuctionDesk from "./AuctionDesk.jsx";

const QUARTERS = { rows: [
  { quarter: "2026Q3", has_plan: true, plan_bln: 1500, fact_nominal_bln: 1101.0, fact_113_bln: 996.7, n: 6 },
  { quarter: "2026Q2", has_plan: false, plan_bln: null, fact_nominal_bln: 900, fact_113_bln: 800, n: 20 },
] };

const PLAN = {
  quarter: "2026Q3", from: "2026-07-01", to: "2026-09-30", has_plan: true, plan_bln: 1500,
  fact_nominal_bln: 1101.025, fact_113_bln: 996.708, pct_113: 66.4, pct_nominal: 73.4,
  auctions_held: 4, auctions_planned: 14, dates_passed: 11, remaining: 3, next_date: "2026-09-16",
  need_per_auction_bln: 167.8, avg_per_auction_bln: 249.2, pace: 0.846, n_rows: 6, n_failed: 1, n_drpa: 1,
  drpa_113_bln: 2.3, src_url: "https://minfin.gov.ru/x",
  buckets: [
    { bucket: "до 10 лет включительно", lo_y: null, hi_y: 10, plan_bln: 900, fact_nominal_bln: 61.9, fact_113_bln: 48.9, pct_113: 5.4, n: 2 },
    { bucket: "от 10 лет", lo_y: 10, hi_y: null, plan_bln: 600, fact_nominal_bln: 1039.2, fact_113_bln: 947.8, pct_113: 158.0, n: 3 },
  ],
  by_date: [
    { date: "2026-07-01", planned: true, held: true, n: 1, n_failed: 0, placed_bln: 10.4, demand_bln: 25.6,
      proceeds_113_bln: 8.8, cum_113_bln: 8.8, plan_line_bln: 107.1, by_type: { "ОФЗ-ПД": 10.4 },
      issues: [{ code: "26251RMFS", fmt: "auction", status: "ok", placed_mln: 10364, wap_yield: 14.96, sec_type: "ОФЗ-ПД" }] },
    { date: "2026-07-15", planned: true, held: true, n: 1, n_failed: 1, placed_bln: 0, demand_bln: 144.3,
      proceeds_113_bln: 0, cum_113_bln: 8.8, plan_line_bln: 321.4, by_type: { "ОФЗ-ПК": 0 },
      issues: [{ code: "29028RMFS", fmt: "auction", status: "failed", placed_mln: 0, wap_yield: null, sec_type: "ОФЗ-ПК" }] },
    { date: "2026-09-02", planned: true, held: true, n: 1, n_failed: 0, placed_bln: 1000, demand_bln: 1424.7,
      proceeds_113_bln: 925.1, cum_113_bln: 933.9, plan_line_bln: 1071.4, by_type: { "ОФЗ-ПК": 1000 }, issues: [] },
    { date: "2026-09-16", planned: true, held: false, n: 0, n_failed: 0, placed_bln: 0, demand_bln: 0,
      proceeds_113_bln: 0, cum_113_bln: null, plan_line_bln: 1285.7, by_type: {}, issues: [] },
    { date: "2026-09-30", planned: true, held: false, n: 0, n_failed: 0, placed_bln: 0, demand_bln: 0,
      proceeds_113_bln: 0, cum_113_bln: null, plan_line_bln: 1500, by_type: {}, issues: [] },
  ],
  dates: ["2026-07-01", "2026-07-15", "2026-09-02", "2026-09-16", "2026-09-30"], today: "2026-09-15",
};

const RESULTS = { from: "2026-07-01", to: null, sync: { rows: 66, from: "2026-01-14", to: "2026-09-09" }, rows: [
  { date: "2026-07-01", code: "26251RMFS", fmt: "auction", secid: "SU26251RMFS4", isin: "RU000A108Y11",
    shortname: "ОФЗ 26251", sec_type: "ОФЗ-ПД", maturity: "2030-08-28", days_to_mat: 1519, term_y: 4.16,
    offered_mln: 110046, cut_price: 84.9032, wap_price: 84.9218, cut_yield: 14.96, wap_yield: 14.96,
    demand_mln: 25591.5, placed_mln: 10364.9, revenue_mln: 9125.7, fill_ratio: 0.405, status: "ok",
    bid_cover: 2.47, proceeds_113_mln: 8802.0, premium_bps: 12.5, secondary_ytm: 14.835 },
  { date: "2026-07-15", code: "29028RMFS", fmt: "auction", secid: "SU29028RMFS6", isin: "RU000A10D4Z9",
    shortname: "ОФЗ 29028", sec_type: "ОФЗ-ПК", maturity: "2039-10-22", days_to_mat: 4847, term_y: 13.27,
    offered_mln: 123807, cut_price: null, wap_price: null, cut_yield: null, wap_yield: null,
    demand_mln: 144290, placed_mln: 0, revenue_mln: 0, fill_ratio: 0, status: "failed",
    bid_cover: null, proceeds_113_mln: 0, premium_bps: null },
  { date: "2026-09-09", code: "26230RMFS", fmt: "auction", secid: "SU26230RMFS1", isin: "RU000A100EF5",
    shortname: "ОФЗ 26230", sec_type: "ОФЗ-ПД", maturity: "2039-03-16", days_to_mat: 4571, term_y: 12.51,
    offered_mln: 220436, cut_price: 57.8152, wap_price: 57.8526, cut_yield: 15.98, wap_yield: 15.97,
    demand_mln: 72718, placed_mln: 35215, revenue_mln: 21576, fill_ratio: 0.484, status: "ok",
    bid_cover: 2.06, proceeds_113_mln: 20373, premium_bps: null },
  { date: "2026-09-09", code: "26230RMFS", fmt: "drpa", secid: "SU26230RMFS1", isin: "RU000A100EF5",
    shortname: "ОФЗ 26230", sec_type: "ОФЗ-ПД", maturity: "2039-03-16", days_to_mat: 4571, term_y: 12.51,
    offered_mln: 21641, cut_price: 57.8526, wap_price: 57.8526, cut_yield: 15.97, wap_yield: 15.97,
    demand_mln: null, placed_mln: 3949, revenue_mln: 2419.5, fill_ratio: null, status: "drpa",
    bid_cover: null, proceeds_113_mln: 2284.6, premium_bps: null },
] };

const ISSUES = { rows: [
  { code: "26230RMFS", secid: "SU26230RMFS1", isin: "RU000A100EF5", shortname: "ОФЗ 26230", sec_type: "ОФЗ-ПД",
    maturity: "2039-03-16", n_rows: 6, n: 5, n_failed: 0, n_drpa: 1, first_date: "2026-01-21", last_date: "2026-09-09",
    placed_mln: 158000, demand_mln: 300000, proceeds_113_mln: 96000, wap_yield_w: 14.9, price_min: 57.85, price_max: 62.85,
    last_wap_yield: 15.97, last_wap_price: 57.85, last_placed_mln: 35215 },
  { code: "29028RMFS", secid: null, isin: null, shortname: null, sec_type: "ОФЗ-ПК", maturity: "2039-10-22",
    n_rows: 1, n: 1, n_failed: 1, n_drpa: 0, first_date: "2026-07-15", last_date: "2026-07-15",
    placed_mln: 0, demand_mln: 144290, proceeds_113_mln: 0, wap_yield_w: null, price_min: null, price_max: null,
    last_wap_yield: null, last_wap_price: null, last_placed_mln: 0 },
] };

const route = (url) => {
  const u = String(url);
  if (u.includes("/auctions/quarters")) return QUARTERS;
  if (u.includes("/auctions/plan")) return PLAN;
  if (u.includes("/auctions/results")) return RESULTS;
  if (u.includes("/auctions/issues")) return ISSUES;
  if (u.includes("/auctions/sync")) return { results: {}, plans: {} };
  throw new Error("unexpected url " + u);
};

const wrap = (node) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter>{node}</MemoryRouter>
  </QueryClientProvider>
);

beforeEach(() => {
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(
    { width: 800, height: 240, top: 0, left: 0, right: 800, bottom: 240, x: 0, y: 0 });
  vi.stubGlobal("fetch", vi.fn(async (url) => ({ ok: true, status: 200, json: async () => route(url) })));
  try { localStorage.removeItem("auctionView"); } catch { /* jsdom */ }
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("смоук вкладки АУКЦИОН", () => {
  it("план/факт: KPI, корзины и оба графика", async () => {
    render(wrap(<AuctionDesk user={{ role: "user" }} />));
    expect(await screen.findByText("1 500,0")).toBeTruthy();          // план
    expect(screen.getByText("996,7")).toBeTruthy();                   // факт по 113-й
    expect(screen.getByText(/по номиналу 1 101,0/)).toBeTruthy();     // номинал рядом
    expect(screen.getByText("66,4%")).toBeTruthy();
    expect(screen.getByText("4 / 14")).toBeTruthy();
    expect(screen.getByText("16.09.2026")).toBeTruthy();              // следующий
    expect(screen.getByText("167,8")).toBeTruthy();                   // надо на аукцион
    // прогресс-бары по корзинам
    expect(screen.getAllByRole("progressbar").length).toBe(2);
    expect(screen.getByText("от 10 лет")).toBeTruthy();
    // кумулятив и столбики нарисованы (SVG после замера контейнера)
    expect(document.querySelector('[data-testid="au-cum"]')).toBeTruthy();
    expect(document.querySelectorAll("rect.au-t-pd, rect.au-t-pk").length).toBeGreaterThan(0);
    expect(document.querySelectorAll("path.au-plan-line").length).toBe(1);
    // не-админ кнопки синка не видит
    expect(screen.queryByText("обновить")).toBeNull();
  });

  it("история: несостоявшийся приглушён, ДРПА под аукционом, суммы в футере, клик открывает карточку", async () => {
    const onOpen = vi.fn();
    render(wrap(<AuctionDesk user={{ role: "admin" }} onOpen={onOpen} />));
    fireEvent.click(await screen.findByText("История"));
    expect(await screen.findByText("не состоялся")).toBeTruthy();
    const failedRow = screen.getByText("не состоялся").closest("tr");
    expect(failedRow.className).toContain("au-failed");
    // ДРПА — строка под своим аукционом (порядок при сортировке по дате)
    const rows = [...document.querySelectorAll("table.au-tab tbody tr")];
    const drpaIdx = rows.findIndex((r) => r.className.includes("au-drpa"));
    expect(drpaIdx).toBeGreaterThan(0);
    expect(within(rows[drpaIdx - 1]).getAllByText("ОФЗ 26230").length).toBe(1);
    // футер: сумма размещения 10 364,9 + 0 + 35 215 + 3 949 = 49 528,9 млн → 49,5 млрд
    const foot = document.querySelector("table.au-tab tfoot");
    expect(within(foot).getByText("49,5")).toBeTruthy();
    expect(within(foot).getByText(/не состоялось 1/)).toBeTruthy();
    // премия со знаком, прочерк там, где вторички нет
    expect(screen.getAllByText("+13").length).toBeGreaterThan(0);   // в строке и средняя в футере
    // точки доходности и крестик несостоявшегося на графике
    expect(document.querySelectorAll("circle.au-pt").length).toBe(2);
    expect(document.querySelectorAll("text.au-fail-mark").length).toBeGreaterThan(0);
    // клик по выпуску → карточка фикса
    fireEvent.click(screen.getByText("ОФЗ 26251"));
    expect(onOpen).toHaveBeenCalledWith("RU000A108Y11", expect.anything(), "fixed");
    // фильтр статуса убирает состоявшиеся
    fireEvent.click(screen.getByText("Не состоялись"));
    expect(document.querySelectorAll("table.au-tab tbody tr").length).toBe(1);
    // админ видит кнопку синка
    expect(screen.getByText("обновить")).toBeTruthy();
  });

  it("выпуски: таблица и клик ведёт в историю с фильтром по выпуску", async () => {
    render(wrap(<AuctionDesk user={{ role: "user" }} />));
    fireEvent.click(await screen.findByText("Выпуски"));
    expect(await screen.findByText("57,9–62,9")).toBeTruthy();         // диапазон цен
    expect(screen.getByText("ОФЗ 29028")).toBeTruthy();                // имя из кода, когда sec_ref не знает
    fireEvent.click(screen.getByText("ОФЗ 29028").closest("tr"));
    // переключились в историю: поиск заполнен кодом, в таблице одна строка
    const search = await screen.findByPlaceholderText("Выпуск");
    expect(search.value).toBe("29028RMFS");
    expect(await screen.findByText("не состоялся")).toBeTruthy();
    expect(document.querySelectorAll("table.au-tab tbody tr").length).toBe(1);
  });

  it("пустая база: подсказка и кнопка синка только у админа", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url) => ({ ok: true, status: 200,
      json: async () => (String(url).includes("/quarters") ? { rows: [] } : route(url)) })));
    render(wrap(<AuctionDesk user={{ role: "admin" }} />));
    expect(await screen.findByText("Итоги аукционов ещё не загружены")).toBeTruthy();
    expect(screen.getByText("обновить с Минфина")).toBeTruthy();
    cleanup();
    render(wrap(<AuctionDesk user={{ role: "user" }} />));
    expect(await screen.findByText("Итоги аукционов ещё не загружены")).toBeTruthy();
    expect(screen.queryByText("обновить с Минфина")).toBeNull();
  });

  it("квартал без плана: только факт, без процентов и прогресса", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url) => ({ ok: true, status: 200,
      json: async () => (String(url).includes("/plan") ? {
        ...PLAN, quarter: "2026Q2", has_plan: false, plan_bln: null, pct_113: null, pct_nominal: null,
        auctions_planned: 0, remaining: 0, next_date: null, need_per_auction_bln: null, pace: null,
        buckets: [], dates: [],
        by_date: PLAN.by_date.filter((d) => d.held).map((d) => ({ ...d, plan_line_bln: null })),
      } : route(url)) })));
    render(wrap(<AuctionDesk user={{ role: "user" }} />));
    expect(await screen.findByText("графика на квартал нет")).toBeTruthy();
    expect(screen.queryAllByRole("progressbar").length).toBe(0);
    expect(document.querySelector('[data-testid="au-cum"]')).toBeTruthy();
    expect(document.querySelectorAll("path.au-plan-line").length).toBe(0);
  });
});
