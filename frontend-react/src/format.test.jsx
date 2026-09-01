/**
 * Единые правила фронта: рейтинговый бакет и срок в годах.
 *
 * Копий было по три-четыре (AnalyticsPanel / CalcModule / FixedAnalytics /
 * ratingColor; format / CalcModule / App), и они отвечали по-разному: CCC падал
 * то в B, то в NR, то в BELOW. Тест держит одно правило на весь фронт.
 */
import { describe, expect, it } from "vitest";
import { ratingBucket, ratingMatches, ratingColor, yearsToNum, yearsToIso, RT_BUCKETS } from "./format.js";

describe("рейтинговый бакет", () => {
  it("узнаёт свои корзины и схлопывает всё ниже B", () => {
    for (const r of RT_BUCKETS) expect(ratingBucket(r)).toBe(r === "NR" ? "NR" : r);
    expect(ratingBucket("CCC")).toBe("B");
    expect(ratingBucket("D")).toBe("B");
    expect(ratingBucket("")).toBe("NR");
    expect(ratingBucket(null)).toBe("NR");
    // Суффикс агентства — это формат записи, а не другой рейтинг: выгрузка
    // отдаёт «ruAA» / «AA(RU)» / «AA|ru|» вперемешку, и раньше все они уезжали
    // в NR (бумага с рейтингом показывалась как «без рейтинга»).
    expect(ratingBucket("ruAA")).toBe("AA");
  });

  it("чип «BB↓» ловит ровно то, что бакет считает ниже BBB", () => {
    expect(ratingMatches("BB", ["BELOW"])).toBe(true);
    expect(ratingMatches("CCC", ["BELOW"])).toBe(true);
    expect(ratingMatches("BBB", ["BELOW"])).toBe(false);
    expect(ratingMatches(null, ["NR"])).toBe(true);
    expect(ratingMatches("AA", [])).toBe(true, "пустой выбор = без фильтра");
  });

  it("цвет строится поверх того же бакета", () => {
    expect(ratingColor("CCC")).toBe(ratingColor("BB"));
    expect(ratingColor("AAA")).not.toBe(ratingColor("BB"));
    expect(ratingColor("мусор")).toBe(ratingColor(""));
  });
});

describe("срок в годах", () => {
  it("yearsToIso — обратная операция к yearsToNum", () => {
    const iso = yearsToIso(2);
    expect(Math.abs(yearsToNum(iso) - 2)).toBeLessThan(0.01);
  });

  it("прошедшая дата отрицательна, пустая — null", () => {
    expect(yearsToNum("2000-01-01")).toBeLessThan(0);
    expect(yearsToNum(null)).toBe(null);
    expect(yearsToNum("не дата")).toBe(null);
  });
});

describe("ступени рейтинга (+/−)", () => {
  it("ступень и суффикс агентства не выкидывают бумагу в NR", () => {
    for (const r of ["AA-", "ruAA-", "AA-(RU)", "AA-|ru|", "AA+", "AA.ru"]) {
      expect(ratingBucket(r)).toBe("AA");
    }
    expect(ratingBucket("BBB+")).toBe("BBB");
    expect(ratingBucket("CCC-")).toBe("B");
    expect(ratingBucket("WITHDRAWN")).toBe("NR");
  });

  it("выбор бакета забирает всю группу, выбор ступени — только её", () => {
    expect(ratingMatches("AA-", ["AA"])).toBe(true);
    expect(ratingMatches("AA+", ["AA"])).toBe(true);
    expect(ratingMatches("AA-", ["AA-"])).toBe(true);
    expect(ratingMatches("AA+", ["AA-"])).toBe(false);
    expect(ratingMatches("BB-", ["BELOW"])).toBe(true);
    expect(ratingMatches("AAA", ["BELOW"])).toBe(false);
  });
});
