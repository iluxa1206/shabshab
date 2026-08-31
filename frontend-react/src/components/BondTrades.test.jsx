/**
 * Лента сделок выпуска — панель слева от стакана.
 *
 * Ловит то, чего не видит бэкенд-тест: панель обязана ПРОСИТЬ адресные сделки
 * (market=all) и показывать их отдельной меткой. Раньше ручка отдавала только
 * безадресные, и по бумаге, где весь дневной объём прошёл через РПС, панель
 * рядом со стаканом была пустой.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import BondTrades from "./BondTrades.jsx";

const ISIN = "RU000A0000A1";

const RESPONSE = {
  isin: ISIN, n: 2, total: 2, truncated: false, value: 11_000_000,
  ndm_n: 1, ndm_value: 9_000_000, vwap_pct: 100.1,
  trades: [
    { trade_id: 1, ts: "2026-08-31 10:00:00", price: 100.1, qty: 20,
      value: 2_000_000, side: "buy", board: "TQCB", negotiated: false,
      y_idx_bps: 150 },
    { trade_id: 2, ts: "2026-08-31 11:00:00", price: 99.0, qty: 90,
      value: 9_000_000, side: null, board: "PSOB", negotiated: true,
      y_idx_bps: 210 },
  ],
};

function stubNetwork() {
  const calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input) => {
    const url = typeof input === "string" ? input : input.url;
    calls.push(url);
    return {
      ok: true, status: 200,
      headers: { get: () => "application/json" },
      json: async () => RESPONSE,
      text: async () => JSON.stringify(RESPONSE),
    };
  }));
  return calls;
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <BondTrades isin={ISIN} kind="floater" onClose={() => {}} />
    </QueryClientProvider>,
  );
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Лента сделок выпуска", () => {
  it("просит адресные сделки и помечает их в таблице", async () => {
    const calls = stubNetwork();
    renderPanel();

    await waitFor(() => expect(
      calls.some((u) => u.includes(`/api/history/${ISIN}/trades`) && u.includes("market=all")),
    ).toBe(true));

    // адресная сделка в ленте: вместо агрессора — режим
    expect(await screen.findByText("РПС")).toBeTruthy();
    // и безадресная рядом, со своей стороной
    expect(await screen.findByText("buy")).toBeTruthy();
    // в шапке видно, сколько из оборота прошло адресно
    expect(await screen.findByText(/РПС 1 на 9/)).toBeTruthy();
  });
});
