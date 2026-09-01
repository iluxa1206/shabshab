/**
 * Фильтр рейтинга на МОНИТОРЕ: чипы держат грейды, ступени живут в меню «▾».
 *
 * Проверяем ровно то, ради чего это делалось:
 *  - в ряду фильтра только крупная шкала (AAA/AA/A/BBB/BB↓/NR), ступени в него
 *    не лезут — иначе появление AA+/AA− в справочниках разносит панель;
 *  - клик по чипу «AA» оставляет ВСЮ группу: AA, AA+, AA−;
 *  - в меню «▾» можно выбрать конкретную ступень, и тогда остаётся только она.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import App from "../App.jsx";

const USER = { email: "smoke@test", role: "admin", is_admin: true };

const bond = (isin, name, rating) => ({
  isin, short_name: name, rating, base_rate_type: "KEYRATE",
  formula: "КС + 1,2%", spread_issue_bps: 120, coupons_per_year: 12,
  maturity_date: "2028-06-01", next_coupon_date: "2026-09-01",
  last_price_pct: 100.1, bid_price_pct: 100.0, ask_price_pct: 100.3,
  y_idx_bid_bps: 190, y_idx_ask_bps: 175, y_idx_wap_bps: 182,
  face_value_rub: 1000, accrued_rub: 12.5, dirty_price_rub: 1013.5,
  dm_bps: 170, disc_margin_bps: 168, yield_xirr_pct: 18.2,
  index_yield_pct: 16.4, yield_over_index_bps: 180, wap_price_pct: 100.15,
  preferred_horizon: "maturity", emitter_name: "ТЕСТ ЭМИТЕНТ",
  is_ofz: false, has_amort: false,
});

const ROWS = [
  bond("RU000A100001", "ГРУППА АА", "AA"),
  bond("RU000A100002", "ГРУППА АА ПЛЮС", "AA+"),
  bond("RU000A100003", "ГРУППА АА МИНУС", "ruAA-"),   // ещё и суффикс агентства
  bond("RU000A100004", "ТРИ А", "AAA"),
];

function reply(url) {
  if (url.includes("/api/auth/me")) return USER;
  if (url.includes("/api/bonds?universe")) {
    return { items: ROWS, total: ROWS.length, limit: 2000, offset: 0 };
  }
  if (url.includes("/api/bonds/quotes")) return { ts: null, n: 0, items: [] };
  if (url.includes("/api/orderbook/depth/all")) return { depth: {} };
  if (url.includes("/api/meta")) return { calc_date: "2026-08-27", rates_date: "2026-08-27" };
  if (url.includes("/api/signals")) return [];
  return {};
}

function stubNetwork() {
  vi.stubGlobal("fetch", vi.fn(async (input) => {
    const url = typeof input === "string" ? input : input.url;
    return {
      ok: true, status: 200,
      headers: { get: () => "application/json" },
      json: async () => reply(url),
      text: async () => JSON.stringify(reply(url)),
    };
  }));
  vi.stubGlobal("WebSocket", class { constructor() { this.readyState = 0; } send() {} close() {} });
}

const names = () => ROWS.map((b) => b.short_name).filter((n) => screen.queryByText(n));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("фильтр рейтинга: грейды в чипах, ступени в меню", () => {
  it("чип грейда забирает всю группу, ступень из меню — только себя", async () => {
    window.history.pushState({}, "", "/app/floaters");
    stubNetwork();
    render(<App />);
    expect(await screen.findByText("ГРУППА АА")).toBeTruthy();

    // в ряду фильтра ступеней нет — только крупная шкала
    expect(screen.queryByRole("button", { name: "AA+" })).toBeNull();
    expect(screen.queryByRole("button", { name: "AA-" })).toBeNull();

    // чип «AA» = AA, AA+, AA− (и никакого AAA)
    fireEvent.click(screen.getByRole("button", { name: "AA" }));
    expect(names().sort()).toEqual(["ГРУППА АА", "ГРУППА АА МИНУС", "ГРУППА АА ПЛЮС"]);

    // снимаем грейд, берём точечную ступень из меню «▾»
    fireEvent.click(screen.getByRole("button", { name: "AA" }));
    fireEvent.click(screen.getByTitle(/Все рейтинги со ступенями/));
    const menu = document.querySelector(".rtmenu-pop");   // role="menu" есть и у меню разделов
    fireEvent.click(within(menu).getByText("AA-"));
    expect(names()).toEqual(["ГРУППА АА МИНУС"]);
  });
});
