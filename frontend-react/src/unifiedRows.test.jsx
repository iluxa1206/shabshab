import { describe, expect, it } from "vitest";
import { durValue, passesFixedDefaults, unifiedSortValue, withFixedAliases } from "./unifiedRows.js";

const FX = { isin: "RU000A0FIX01", kind: "fixed", name: "ОФЗ 26238", issuer: "Минфин", cls: "ofz",
  bid: 60.4, ask: 60.6, wap_pct: 60.45, ytm: 14.2, put_date: null, maturity_date: "2041-05-15",
  coupon_pct: 7.1, mod_dur: 7.9, adv_1m_rub: 2_000_000 };
const FL = { isin: "RU000A0FLT01", kind: "floater", short_name: "Фл", maturity_date: "2029-01-01",
  spread_issue_bps: 120 };

describe("алиасы строки фикса", () => {
  it("докладывают имена флоатера, не трогая нативные", () => {
    const a = withFixedAliases(FX);
    expect(a.short_name).toBe("ОФЗ 26238");
    expect(a.emitter_name).toBe("Минфин");
    expect(a.is_ofz).toBe(true);
    expect(a.base_rate_type).toBe("FIXED");
    expect([a.bid_price_pct, a.ask_price_pct, a.wap_price_pct, a.yield_xirr_pct])
      .toEqual([60.4, 60.6, 60.45, 14.2]);
    expect(a.bid).toBe(60.4);                       // нативное поле осталось
  });

  it("оферта фикса становится offer_date/offer_kind", () => {
    const a = withFixedAliases({ ...FX, put_date: "2027-03-01" });
    expect(a.offer_date).toBe("2027-03-01");
    expect(a.offer_kind).toBe("put");
  });

  it("та же исходная строка — та же копия (memo строки таблицы)", () => {
    expect(withFixedAliases(FX)).toBe(withFixedAliases(FX));
  });

  it("новая исходная строка — свежие алиасы (живое слияние не устаревает)", () => {
    const a = withFixedAliases(FX);
    const b = withFixedAliases({ ...FX, bid: 61.0 });
    expect(b).not.toBe(a);
    expect(b.bid_price_pct).toBe(61.0);
  });

  it("флоатер не меняется", () => {
    expect(withFixedAliases(FL)).toBe(FL);
  });
});

describe("колонка DUR", () => {
  it("у фикса — модифицированная дюрация", () => {
    expect(durValue(FX)).toBe(7.9);
  });
  it("у флоатера — лет до горизонта, не дюрация", () => {
    const y = durValue({ ...FL, maturity_date: new Date(Date.now() + 2 * 365.25 * 864e5).toISOString().slice(0, 10) });
    expect(y).toBeGreaterThan(1.9);
    expect(y).toBeLessThan(2.1);
  });
  it("погашенная бумага не даёт отрицательный срок", () => {
    expect(durValue({ ...FL, maturity_date: "2020-01-01" })).toBeNull();
  });
});

describe("значение сортировки", () => {
  it("coupon: ставка фикса, у флоатера — маржа", () => {
    expect(unifiedSortValue(FX, "coupon")).toBe(7.1);
    expect(unifiedSortValue(FL, "coupon")).toBe(120);
  });
  it("dur — через durValue, прочее — поле строки", () => {
    expect(unifiedSortValue(FX, "dur")).toBe(7.9);
    expect(unifiedSortValue(FX, "ytm")).toBe(14.2);
  });
});

describe("дефолтные отсечки фиксов", () => {
  const D = { hideStructured: true, advMinRub: 1e6, currencies: ["RUB"] };
  it("флоатер не затрагивается отсечками фиксов (ADV, структурные)", () => {
    expect(passesFixedDefaults(FL, { ...D, advMinRub: 1e12 })).toBe(true);
  });
  it("валюта режет и флоатер: валютный номинал скрыт при выборе RUB", () => {
    const usd = { ...FL, face_unit: "USD" };
    expect(passesFixedDefaults(usd, D)).toBe(false);
    expect(passesFixedDefaults(usd, { ...D, currencies: ["USD"] })).toBe(true);
    expect(passesFixedDefaults({ ...FL, face_unit: undefined }, D)).toBe(true);   // нет поля — рубль
  });
  it("структурная нота скрыта, пока фильтр включён", () => {
    const n = { ...FX, structured: true };
    expect(passesFixedDefaults(n, D)).toBe(false);
    expect(passesFixedDefaults(n, { ...D, hideStructured: false })).toBe(true);
  });
  it("порог ADV: ниже порога и без ADV — скрыты", () => {
    expect(passesFixedDefaults({ ...FX, adv_1m_rub: 5e5 }, D)).toBe(false);
    expect(passesFixedDefaults({ ...FX, adv_1m_rub: undefined }, D)).toBe(false);
    expect(passesFixedDefaults({ ...FX, adv_1m_rub: 1e6 }, D)).toBe(true);
    expect(passesFixedDefaults({ ...FX, adv_1m_rub: undefined }, { ...D, advMinRub: 0 })).toBe(true);
  });
  it("валюта: по умолчанию только RUB, пустой набор — все", () => {
    const usd = { ...FX, face_unit: "USD" };
    expect(passesFixedDefaults(usd, D)).toBe(false);
    expect(passesFixedDefaults(usd, { ...D, currencies: [] })).toBe(true);
  });
});
