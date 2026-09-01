/**
 * Горизонт прайсинга строки: та дата, к которой посчитаны её метрики.
 *
 * По ней меряют «срок» и окно фильтра, и сортировка колонки MATURITY — иначе
 * интерфейс спорит сам с собой: в строке синим стоит «2,7 г», а список
 * отсортирован так, будто бумага одиннадцатилетняя.
 */
import { describe, it, expect } from "vitest";
import { horizonDate } from "./horizon.js";

describe("horizonDate", () => {
  it("оферта, когда рынок прайсит к ней", () => {
    expect(horizonDate({ preferred_horizon: "put", offer_date: "2029-05-16",
                         maturity_date: "2038-03-26" })).toBe("2029-05-16");
    expect(horizonDate({ preferred_horizon: "call", offer_date: "2028-11-07",
                         maturity_date: "2030-10-22" })).toBe("2028-11-07");
  });

  it("погашение, когда правило цены выбрало его — оферта в строке есть, но не в счёт", () => {
    expect(horizonDate({ preferred_horizon: "maturity", offer_date: "2028-11-07",
                         maturity_date: "2030-10-22" })).toBe("2030-10-22");
  });

  it("горизонт назван офертой, а даты нет — падаем на погашение", () => {
    expect(horizonDate({ preferred_horizon: "put", maturity_date: "2030-10-22" }))
      .toBe("2030-10-22");
  });

  it("ни оферты, ни погашения (перп, дыра в справочнике) — null", () => {
    expect(horizonDate({ preferred_horizon: "maturity" })).toBeNull();
    expect(horizonDate(null)).toBeNull();
  });
});
