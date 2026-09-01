/**
 * КАЛЬКУЛЯТОР: сравнение кастомной бумаги с рынком.
 *
 * Два регресса, которые ловит этот файл:
 *  • имя выпуска бралось из одного поля (b.name), а у флоатеров оно живёт в
 *    short_name — весь список выпусков эмитента показывал «undefined»;
 *  • ось X скэттера меряла дюрацию; теперь это СРОК до горизонта, та же шкала,
 *    что в мониторе и на графике аналитики.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import CalcModule, { AXES } from "./CalcModule.jsx";
import { horizonYears } from "../horizon.js";

// имя у флоатера в short_name, у фикса — в name: разные источники витрин
const FLOATER = {
  isin: "RU000A109B33", short_name: "Газпн3P13R", emitter_name: "Газпром капитал",
  rating: "AAA", maturity_date: "2030-07-20", spread_dur_yrs: 1.4,
  yield_over_index_bps: 176, dm_bps: 168, last_price_pct: 99.49,
  preferred_horizon: "maturity",
};
const FIXED = {
  isin: "RU000A100002", name: "ОФЗ 26999", issuer: "ОФЗ", rating: "AAA",
  maturity_date: "2031-05-14", mod_dur: 3.4, ytm: 16.1, g_spread_bps: 25,
  last_price_pct: 85.7, coupon_pct: 12.25,
};

function mount(kind) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}><CalcModule initialKind={kind} /></QueryClientProvider>);
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (input) => {
    const url = typeof input === "string" ? input : input.url;
    const body = url.includes("/api/fixed")
      ? { items: [FIXED], total: 1 }
      : { items: [FLOATER], total: 1 };
    return {
      ok: true, status: 200, headers: { get: () => "application/json" },
      json: async () => body, text: async () => JSON.stringify(body),
    };
  }));
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** Вводит эмитента в форму — так открывается таблица его выпусков. */
async function pickIssuer(name) {
  const input = await screen.findByPlaceholderText("начните вводить…");
  fireEvent.change(input, { target: { value: name } });
}

describe("калькулятор", () => {
  it("показывает имя выпуска флоатера, а не undefined", async () => {
    mount("float");
    await pickIssuer("Газпром капитал");
    expect(await screen.findByText("Газпн3P13R")).toBeTruthy();
    expect(document.body.textContent).not.toContain("undefined");
  });

  it("показывает имя выпуска фикса", async () => {
    mount("fixed");
    await pickIssuer("ОФЗ");
    expect(await screen.findByText("ОФЗ 26999")).toBeTruthy();
    expect(document.body.textContent).not.toContain("undefined");
  });

  // Сам скэттер в jsdom не рисуется: он меряет контейнер, а размеров там нет.
  // Ось проверяем на её описании — оно и есть контракт графика.
  it("ось сравнения — СРОК до горизонта, у обоих типов", () => {
    expect(AXES.float.xLabel).toBe("срок, лет →");
    expect(AXES.fixed.xLabel).toBe("срок, лет →");
    // флоатер: 1,4 года спред-дюрации против срока до погашения — на графике
    // теперь второе
    expect(AXES.float.x(FLOATER)).toBeCloseTo(horizonYears(FLOATER), 6);
    expect(AXES.float.x(FLOATER)).not.toBeCloseTo(FLOATER.spread_dur_yrs, 1);
    expect(AXES.fixed.x(FIXED)).toBeCloseTo(horizonYears(FIXED), 6);
    expect(AXES.fixed.x(FIXED)).not.toBeCloseTo(FIXED.mod_dur, 1);
  });
});
