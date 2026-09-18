// Фильтры анонсов ПЕРВИЧКИ: рейтинг и срок. Главное правило — рейтинг анонса
// это СПИСОК оценок агентств и читается как «и»: «AA+ / AAA» обязан проходить
// и под чип AAA, и под ступень AA+. Плюс окно срока прячет анонс без срока.
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import PrimaryCalendar from "./PrimaryCalendar.jsx";

const CAL = {
  source_name: "test", source_url: "https://example.org", fetched_at: null,
  rows: [
    { issuer: "Дом", ratings: ["AA+", "AAA", "AA+"], term_years: 7, is_floater: true,
      book_date: "2026-09-15", issue_date: "2026-09-18", coupon_guide: "КС + не выше 400 бп" },
    { issuer: "Заслон", ratings: ["BBB+", "A-"], term_years: 3, is_floater: false,
      book_date: "2026-09-09", issue_date: "2026-09-15", coupon_guide: "17,5%" },
    { issuer: "Секьюритизация", ratings: [], term_years: null, is_floater: false,
      book_date: "2026-09-20", issue_date: "2026-09-25", coupon_guide: "будет определен позднее" },
  ],
};

const wrap = (node) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter>{node}</MemoryRouter>
  </QueryClientProvider>
);

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200, headers: { get: () => "application/json" },
    json: async () => CAL, text: async () => JSON.stringify(CAL),
  })));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const issuers = () => ["Дом", "Заслон", "Секьюритизация"].filter((n) => screen.queryByText(n));

describe("фильтры анонсов ПЕРВИЧКИ", () => {
  it("рейтинг: список оценок читается как «и» — подходит под любую из них", async () => {
    render(wrap(<PrimaryCalendar />));
    expect(await screen.findByText("Дом")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "AAA" }));
    expect(issuers()).toEqual(["Дом"]);
    // снять AAA, взять грейд AA — та же строка проходит по AA+
    fireEvent.click(screen.getByRole("button", { name: "AAA" }));
    fireEvent.click(screen.getByRole("button", { name: "AA" }));
    expect(issuers()).toEqual(["Дом"]);
    // BBB — Заслон (BBB+), NR — анонс без оценок; вместе — обе строки
    fireEvent.click(screen.getByRole("button", { name: "AA" }));
    fireEvent.click(screen.getByRole("button", { name: "BBB" }));
    expect(issuers()).toEqual(["Заслон"]);
    fireEvent.click(screen.getByRole("button", { name: "NR" }));
    expect(issuers()).toEqual(["Заслон", "Секьюритизация"]);
  });

  it("ступень из меню «▾» ловит ту же строку, что и её грейд", async () => {
    render(wrap(<PrimaryCalendar />));
    expect(await screen.findByText("Дом")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "▾" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: /AA\+/ }));
    expect(issuers()).toEqual(["Дом"]);
  });

  it("окно срока: границы включительно, анонс без срока при границе скрыт", async () => {
    render(wrap(<PrimaryCalendar />));
    expect(await screen.findByText("Дом")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Срок, лет — до"), { target: { value: "3" } });
    expect(issuers()).toEqual(["Заслон"]);
    fireEvent.change(screen.getByLabelText("Срок, лет — от"), { target: { value: "5" } });
    expect(issuers()).toEqual([]);
    fireEvent.click(screen.getByTitle("Сбросить окно срока"));
    expect(issuers()).toEqual(["Дом", "Заслон", "Секьюритизация"]);
  });
});
