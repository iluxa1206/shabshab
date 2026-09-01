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
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

// Даты СЧИТАЕМ ОТ СЕГОДНЯ: окно срока меряется в годах от текущей даты, и
// зашитые «2029-05-16» через год стали бы значить другой срок — тест начал бы
// падать сам по себе.
const inYears = (y) => {
  const d = new Date();
  d.setFullYear(d.getFullYear() + Math.floor(y), d.getMonth() + Math.round((y % 1) * 12), 1);
  return d.toISOString().slice(0, 10);
};

// Бумага, чей СРОК определяет оферта, а не погашение: правило цены выбрало пут
// (preferred_horizon), поэтому по сроку она стоит рядом с двухлетками, хотя
// гасится через одиннадцать лет.
const BOND_PUT = {
  ...BOND, isin: "RU000A100003", short_name: "ТЕСТ 2Р-02",
  maturity_date: inYears(11), offer_date: inYears(2.5), offer_kind: "put",
  preferred_horizon: "put",
};

// Бумага без оферты и с дальним погашением — нужна, чтобы порядок по сроку
// отличался от порядка по дате погашения: по горизонту она идёт ПОСЛЕ бумаги с
// офертой, по погашению — раньше неё.
const BOND_LONG = {
  ...BOND, isin: "RU000A100004", short_name: "ТЕСТ 3Р-03",
  maturity_date: inYears(5), offer_date: null, preferred_horizon: "maturity",
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
  if (url.includes("/api/bonds?universe"))
    return { items: [BOND, BOND_PUT, BOND_LONG], total: 3, limit: 2000, offset: 0 };
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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  FIXED_ON = true;
  // Витрина помнит фильтры между заходами (localStorage), а хранилище в тестах
  // одно на файл: без чистки окно срока из соседнего теста доезжало сюда и
  // резало таблицу ещё до первой проверки.
  try { localStorage.clear(); } catch { /* приватный режим */ }
});

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

  it("окно срока меряет горизонт прайсинга, а не погашение", async () => {
    // Бумага с офертой через 2,5 года и погашением через одиннадцать: метрики
    // строки посчитаны к оферте (синие годы в колонке MATURITY), значит и
    // «срок» у неё этот. По дате погашения окно «2–3 года» выкинуло бы её, и
    // фильтр спорил бы с числом, которое сам же показывает.
    const back = window.location.pathname + window.location.search;
    window.history.pushState({}, "", "/app/floaters?myf=2&myt=3");
    stubNetwork();
    render(<App />);
    expect(await screen.findByText(/ТЕСТ 2Р-02/)).toBeTruthy();
    // соседняя бумага гасится через год — в окно «от 2 лет» не попадает
    await waitFor(() => expect(screen.queryByText(/ТЕСТ 1Р-01/)).toBeNull());
    window.history.pushState({}, "", back);
  });

  it("сортировка MATURITY идёт по горизонту, а не по дате погашения", async () => {
    // 1Р-01 гасится через год, 2Р-02 прайсится к оферте через 2,5 года и
    // гасится через одиннадцать, 3Р-03 гасится через пять. По СРОКУ порядок
    // 1 → 2 → 3; по дате погашения он был бы 1 → 3 → 2, и колонка спорила бы с
    // числом, которое сама же подсвечивает синим.
    stubNetwork();
    render(<App />);
    await screen.findByText(/ТЕСТ 2Р-02/);
    fireEvent.click(screen.getByText("MATURITY"));
    await waitFor(() => {
      const names = [...document.querySelectorAll(".bond-name")]
        .map((n) => n.textContent);
      // в имени рядом стоят класс и рейтинг («КОРПТЕСТ 2Р-02(AA)») — берём
      // только сам тикер
      expect(names.map((n) => (n.match(/ТЕСТ \dР-\d+/) || [])[0]).filter(Boolean))
        .toEqual(["ТЕСТ 1Р-01", "ТЕСТ 2Р-02", "ТЕСТ 3Р-03"]);
    });
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
