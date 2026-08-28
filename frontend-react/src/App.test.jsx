/**
 * Smoke-проверка МОНТИРОВАНИЯ приложения.
 *
 * Зачем отдельный тест на «просто отрендерилось»: `vite build` проверяет, что
 * код собирается, и молчит про ошибки, которые случаются только в рантайме.
 * 27.08.2026 монитор ушёл в белый экран, потому что массив зависимостей хука
 * обращался к `const loadBonds` выше его объявления — мёртвая зона, ReferenceError
 * на первом же рендере. Сборка была зелёной, питон-тесты тоже; поймал это
 * пользователь. Здесь такой класс ломает тест.
 *
 * Сеть заглушена НА УРОВНЕ fetch/WebSocket, а не мока api.js: так проверяется
 * настоящий клиентский слой (сборка URL, разбор ответа, гварды), и тест не надо
 * править каждый раз, когда в приложении появляется новый вызов.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import App from "./App.jsx";

const USER = { email: "smoke@test", role: "admin", is_admin: true };

// Одна бумага юниверса: строка таблицы со всеми полями, которые витрина
// действительно читает (цены сторон, спреды, номинал/НКД для фильтра по объёму).
const BOND = {
  isin: "RU000A100001", short_name: "ТЕСТ 1Р-01", base_rate_type: "KEYRATE",
  formula: "КС + 1,2%", spread_issue_bps: 120, coupons_per_year: 12,
  maturity_date: "2028-06-01", next_coupon_date: "2026-09-01",
  last_price_pct: 100.1, bid_price_pct: 100.0, ask_price_pct: 100.3,
  y_idx_bid_bps: 190, y_idx_ask_bps: 175, y_idx_wap_bps: 182,
  face_value_rub: 1000, accrued_rub: 12.5, dirty_price_rub: 1013.5,
  dm_bps: 170, disc_margin_bps: 168, yield_xirr_pct: 18.2,
  index_yield_pct: 16.4, yield_over_index_bps: 180, wap_price_pct: 100.15,
  preferred_horizon: "maturity", rating: "AA", emitter_name: "ТЕСТ ЭМИТЕНТ",
  is_ofz: false, has_amort: false,
};

// Слой фиксов включён/выключен (см. services/feature_flags → /api/meta.features).
let FIXED_ON = true;

// Строка МОНИТОРА ФИКСОВ: те же поля, что читает витрина фиксов.
const FIXED = {
  isin: "RU000A100002", name: "ОФЗ 26999", secid: "SU26999RMFS1", issuer: "ОФЗ",
  cls: "ofz", rating: "AAA", maturity_date: "2031-05-14", coupon_pct: 12.25,
  last_price_pct: 85.7, bid: 85.6, ask: 85.8, wap_pct: 85.75,
  ytm: 16.1, ytm_bid: 16.2, ytm_ask: 16.0, cur_yield: 14.3, delta_ytm: -0.05,
  g_spread_bps: 25, g_spread_bid_bps: 27, g_spread_ask_bps: 23, g_spread_wap_bps: 26,
  z_spread_bps: 30, mod_dur: 3.4, mac_dur: 3.9, convexity: 16.2, dv01: 0.31,
  dirty: 904.05, delta_to_prev_close: 0.1, val_today: 1.2e9, adv_1m_rub: 8e8,
  has_amort: false, price_thin: false, price_stale: false,
};

/** Ответ на запрос по URL. Неизвестное — пустой объект: тест про рендер, не про данные. */
function replyFor(url) {
  if (url.includes("/api/me")) return USER;
  if (url.includes("/api/bonds?universe")) return { items: [BOND], total: 1, limit: 2000, offset: 0 };
  if (url.includes("/api/bonds/quotes")) return { ts: null, n: 0, items: [] };
  if (url.includes("/api/orderbook/depth/all")) return { depth: {} };
  if (url.includes("/api/fixed/quotes")) return { ts: null, n: 0, items: [] };
  if (url.includes("/api/fixed")) return { items: [FIXED], total: 1, calc_date: "2026-08-27" };
  if (url.includes("/api/meta")) return { calc_date: "2026-08-27", rates_date: "2026-08-27",
                                           features: { fixed: FIXED_ON } };
  if (url.includes("/api/signals")) return [];
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
      json: async () => replyFor(url),
      text: async () => JSON.stringify(replyFor(url)),
    };
  }));
  // WS в jsdom нет: подписки не должны валить монтирование
  vi.stubGlobal("WebSocket", class {
    constructor() { this.readyState = 0; }
    send() {}
    close() {}
  });
  return calls;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); FIXED_ON = true; });

describe("монтирование приложения", () => {
  it("гость видит форму входа", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false, status: 401,
      headers: { get: () => "application/json" },
      json: async () => ({ detail: "unauthorized" }),
      text: async () => '{"detail":"unauthorized"}',
    })));
    vi.stubGlobal("WebSocket", class { send() {} close() {} });
    render(<App />);
    expect(await screen.findByText(/ДОСТУП ПО АККАУНТУ/i)).toBeTruthy();
  });

  it("монитор монтируется и запрашивает юниверс", async () => {
    const errors = [];
    const spy = vi.spyOn(console, "error").mockImplementation((...a) => errors.push(a.join(" ")));
    const calls = stubNetwork();

    render(<App />);

    // Именно этот запрос и пропал в проде, когда Dashboard падал на первом
    // рендере: строки не грузились, а в логах бэка не было ни одного universe=true.
    await waitFor(() => expect(calls.some((u) => u.includes("universe=true"))).toBe(true));
    // дошли до отрисовки строки, а не просто до эффектов
    expect(await screen.findByText(/ТЕСТ 1Р-01/)).toBeTruthy();

    // React про упавший рендер сообщает через console.error — молчание значит,
    // что дерево смонтировалось целиком, без проглоченного исключения
    const fatal = errors.filter((e) => /ReferenceError|is not defined|before initialization|Cannot read/.test(e));
    expect(fatal).toEqual([]);
    spy.mockRestore();
  });

  it("монитор фиксов монтируется и рисует строку", async () => {
    // Витрина фиксов — клон монитора флоатеров на общих компонентах (BondTable,
    // Toolbar), поэтому ошибка обобщения ломает её так же тихо: сборка зелёная,
    // на странице белый экран.
    const back = window.location.pathname;
    window.history.pushState({}, "", "/app/fixed");
    const errors = [];
    const spy = vi.spyOn(console, "error").mockImplementation((...a) => errors.push(a.join(" ")));
    const calls = stubNetwork();

    render(<App />);

    await waitFor(() => expect(calls.some((u) => u.includes("/api/fixed"))).toBe(true));
    expect(await screen.findByText(/ОФЗ 26999/)).toBeTruthy();
    // первичные метрики витрины — на месте (g-спред и доходность к погашению)
    expect((await screen.findAllByText("25")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("16,10")).length).toBeGreaterThan(0);

    // витрина фиксов: стакан впереди последней сделки, обе первичные метрики
    // и watchlist — то же, что у монитора флоатеров
    for (const th of ["BID", "OFFER", "G-SPRD", "YTM", "ADV"]) {
      expect((await screen.findAllByText(th)).length).toBeGreaterThan(0);
    }
    expect(screen.getByTitle("Watchlist")).toBeTruthy();
    expect(screen.getByLabelText("Столбцы")).toBeTruthy();

    const fatal = errors.filter((e) => /ReferenceError|is not defined|before initialization|Cannot read/.test(e));
    expect(fatal).toEqual([]);
    spy.mockRestore();
    window.history.pushState({}, "", back);
  });

  it("фильтры фиксов поднимаются из ссылки", async () => {
    // Вид витрины должен переживать F5 и уезжать коллеге ссылкой: окно g-спреда
    // из query string обязано примениться ДО первой отрисовки таблицы.
    const back = window.location.pathname + window.location.search;
    window.history.pushState({}, "", "/app/fixed?fxgf=500");   // g-спред от 500 бп
    stubNetwork();
    render(<App />);
    // бумага витрины даёт 25 бп — под окно не попадает, таблица пуста
    // (данные приезжают из кэша react-query или из сети — витрине всё равно)
    await waitFor(() => expect(screen.queryByText(/ОФЗ 26999/)).toBeNull());
    expect(await screen.findByText(/Ничего не найдено по фильтру/)).toBeTruthy();
    window.history.pushState({}, "", back);
  });
});
