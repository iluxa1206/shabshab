import { describe, it, expect } from "vitest";
import { bookMode, eventTag, maturityShort } from "./signalFormat.js";

describe("bookMode — место заявки в очереди", () => {
  it("лучшая заявка помечается best", () => {
    expect(bookMode({ reason: "spread", best: true, levels: 3 })).toBe("best");
  });

  it("не лучшая — молчим (счёт уровней убран)", () => {
    expect(bookMode({ reason: "spread", best: false, levels: 3 })).toBeNull();
    expect(bookMode({ reason: "new", levels: 5 })).toBeNull();
  });

  it("режим одной крупной заявки остаётся, best дописывается к нему", () => {
    expect(bookMode({ reason: "new", money_mode: "single", single_px: 100.04 }))
      .toBe("одна заявка 100,04%");
    expect(bookMode({ reason: "new", money_mode: "single", single_px: 100.04, best: true }))
      .toBe("одна заявка 100,04% · best");
  });

  it("у сделки очереди нет вовсе", () => {
    expect(bookMode({ reason: "block", best: true })).toBeNull();
  });
});

describe("eventTag — сторона в плашке", () => {
  it("заявка: сторона очереди, параметр повтора в скобках", () => {
    expect(eventTag({ reason: "new", side: "ask" })).toBe("оффер");
    expect(eventTag({ reason: "spread", side: "bid" })).toBe("бид (спред)");
  });

  it("сделка: агрессор словом, у адресной его нет", () => {
    expect(eventTag({ reason: "block", side: "buy" })).toBe("покупка");
    expect(eventTag({ reason: "block", side: "sell" })).toBe("продажа");
    expect(eventTag({ reason: "block", side: "buy", negotiated: true })).toBe("сделка");
  });
});

describe("maturityShort — срок к расчётной дате", () => {
  it("годы в скобках, из готового e.years (горизонт прайсинга)", () => {
    expect(maturityShort({ years: 2.74, maturity: "2032-03-17" })).toBe("(2,7 г)");
  });

  it("без срока — ничего", () => {
    expect(maturityShort({ maturity: "2032-03-17" })).toBeNull();
  });
});
