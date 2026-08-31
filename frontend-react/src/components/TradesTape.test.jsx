/**
 * Вкладка СДЕЛКИ: биржевой оборот в статусной полосе.
 *
 * Ловит то, чего не видит бэкенд-тест: показатель «БИРЖА» публикуется через
 * usePageStatus в ОБЩУЮ полосу приложения, и увидеть его можно только собрав
 * страницу целиком. Отдельная ловушка — умолчание вкладки «от 10 млн»: пока
 * бэкенд гасил market_value под порогом, показатель не появлялся никогда.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { PageStatusProvider, usePageStatusItems } from "../pageStatus.jsx";
import TradesTape from "./TradesTape.jsx";

const TRADE = {
  trade_id: 1, isin: "RU000A0000A1", name: "Тест 1Р01",
  ts: "2026-08-28 12:00:00", price: 100.0, qty: 10, value: 12_000_000,
  side: "buy", board: "TQCB", negotiated: false, y_idx_bps: 150,
};

const SUMMARY = {
  n: 1, value: 12_000_000, market_value: 46_300_000,
  buy_value: 12_000_000, sell_value: 0,
  by_market: { bonds: { n: 1, value: 12_000_000 }, ndm: { n: 0, value: 0 } },
  top: [], archive_till: "2026-08-28 18:40:00",
};

function reply(url) {
  if (url.includes("/api/trades/issuers")) return { issuers: [] };
  if (url.includes("/api/trades/ratings")) return { ratings: [], buckets: [] };
  if (url.includes("/api/trades/boards")) return { boards: [] };
  if (url.includes("/api/trades/flags")) return { trades: [] };
  if (url.includes("/api/trades")) {
    return { trades: [TRADE], summary: SUMMARY, days: 1, has_more: false,
             truncated: false, scope: "float" };
  }
  return {};
}

function stubNetwork() {
  const calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input) => {
    const url = typeof input === "string" ? input : input.url;
    calls.push(url);
    return {
      ok: true, status: 200,
      headers: { get: () => "application/json" },
      json: async () => reply(url),
      text: async () => JSON.stringify(reply(url)),
    };
  }));
  vi.stubGlobal("WebSocket", class { constructor() { this.readyState = 0; } send() {} close() {} });
  return calls;
}

/** Мини-полоса: показывает ровно то, что вкладка опубликовала в статус. */
function StatusStrip() {
  const items = usePageStatusItems();
  return (
    <div data-testid="strip">
      {items.map((i) => <span key={i.k}>{`${i.k}=${i.v}`}</span>)}
    </div>
  );
}

function renderTape() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/trades"]}>
      <QueryClientProvider client={client}>
        <PageStatusProvider>
          <TradesTape />
          <StatusStrip />
        </PageStatusProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Вкладка СДЕЛКИ: биржевой оборот", () => {
  it("показывает БИРЖА рядом с оборотом ленты — при умолчании «от 10 млн»", async () => {
    const calls = stubNetwork();
    renderTape();

    await waitFor(() => expect(calls.some((u) => u.includes("/api/trades?"))).toBe(true));
    // умолчание вкладки — порог 10 млн: показатель обязан пережить его
    expect(calls.some((u) => u.includes("min_value=10000000"))).toBe(true);

    const strip = await screen.findByTestId("strip");
    await waitFor(() => expect(strip.textContent).toContain("ОБОРОТ=12"));
    // формат — млн ₽ с русской запятой (fmt.mln)
    expect(strip.textContent).toContain("БИРЖА=46,3");
  });
});
