/**
 * Справочник: очередь свежих выпусков.
 *
 * Ловит две ошибки, которые сборка и питон-тесты не видят:
 *  1) значок «надо чекнуть» на кнопке меню не появляется (число приходит
 *     отдельным запросом /api/instruments/new-issues);
 *  2) фильтр «новые выпуски» показывает пусто, потому что серверный фильтр
 *     «только флоатеры» (base IN KEYRATE/RUONIA) прячет ровно тех свежих, у
 *     кого базы ещё нет — то есть очередь проверки скрывает свою же работу.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "../App.jsx";

const USER = { email: "smoke@test", role: "admin", is_admin: true };

// свежий выпуск: базы нет (источники ещё не отдали) — он и есть работа админа
const FRESH = {
  isin: "RU000A10FYM2", short_name: "СибурХ1Р11", base: null, margin_bps: null,
  maturity_date: "2029-08-11", issue_date: "2026-08-27", priceable: false,
  reviewed: 0, source: "moex", active: 1,
};
const OLD = {
  isin: "RU000A109PP1", short_name: "АБЗ-1 2Р01", base: "KEYRATE", margin_bps: 400,
  maturity_date: "2027-09-24", issue_date: "2024-10-09", priceable: true,
  reviewed: 1, source: "cbonds", active: 1,
};

function reply(url) {
  if (url.includes("/api/auth/me")) return USER;
  if (url.includes("/api/instruments/new-issues")) {
    return { items: [FRESH], days: 30, n: 1, blind: 1 };
  }
  if (url.includes("/api/instruments/catalog")) {
    return {
      items: [FRESH, OLD],
      count: { total: 2, floaters: 1, priceable: 1, incomplete: 1, suspect: 0,
               offer_reset: 0, unreviewed: 1, new_issues: 1 },
      new_issues: [FRESH.isin], new_issue_days: 30,
      offers_no_spec: [], spec_mismatch: [], sl_mismatch: [],
    };
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

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Справочник: свежие выпуски", () => {
  it("значок с числом, метка NEW и фильтр, снимающий «только флоатеры»", async () => {
    window.history.pushState({}, "", "/app/reference");
    const calls = stubNetwork();

    render(<App />);

    // строка справочника доехала
    expect(await screen.findByText("СибурХ1Р11")).toBeTruthy();
    // значок на кнопке меню (счётчик из /new-issues) и метка на строке
    await waitFor(() => expect(calls.some((u) => u.includes("/api/instruments/new-issues"))).toBe(true));
    expect((await screen.findAllByTitle(/новых выпусков без подтверждённых параметров/)).length)
      .toBeGreaterThan(0);
    expect(await screen.findByText("NEW")).toBeTruthy();

    // по умолчанию справочник просит только флоатеров — свежий без базы туда не попадёт
    expect(calls.some((u) => u.includes("/api/instruments/catalog?floaters_only=true"))).toBe(true);

    // клик по «новые выпуски» обязан снять серверный фильтр по базе
    fireEvent.click(await screen.findByText(/новые выпуски/i));
    await waitFor(() => expect(
      calls.some((u) => u.includes("/api/instruments/catalog") && !u.includes("floaters_only"))
    ).toBe(true));

    // в списке остался только свежий выпуск
    expect(await screen.findByText("СибурХ1Р11")).toBeTruthy();
    expect(screen.queryByText("АБЗ-1 2Р01")).toBeNull();
  });
});
