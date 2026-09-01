import { describe, it, expect } from "vitest";
import { bookMode, eventTag, maturityShort, moveTone,
         reasonTone } from "./signalFormat.js";

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

describe("moveTone — хорошо/плохо по стороне, а не по знаку", () => {
  it("оффер: спред вверх и цена вниз — в нашу пользу", () => {
    expect(moveTone("spread", "ask", +15)).toBe("pos");
    expect(moveTone("spread", "ask", -15)).toBe("neg");
    expect(moveTone("price", "ask", -0.5)).toBe("pos");
    expect(moveTone("price", "ask", +0.5)).toBe("neg");
  });

  it("бид — зеркально: там мы продаём", () => {
    expect(moveTone("spread", "bid", +15)).toBe("neg");
    expect(moveTone("price", "bid", +0.5)).toBe("pos");
  });

  it("объём вне симметрии: больше денег — лучше обеим сторонам", () => {
    expect(moveTone("money", "ask", +1e6)).toBe("pos");
    expect(moveTone("money", "bid", -1e6)).toBe("neg");
  });

  it("красить нечего: нулевая дельта, сделка, неизвестная причина", () => {
    expect(moveTone("spread", "ask", 0)).toBeNull();
    expect(moveTone("spread", "buy", +15)).toBeNull();
    expect(moveTone("что-то", "ask", +15)).toBeNull();
  });
});

describe("reasonTone — тон самого события ленты", () => {
  it("берёт причину и сторону из строки", () => {
    expect(reasonTone({ reason: "spread", side: "ask", val_bps: 180, prev_val_bps: 150 }))
      .toBe("pos");
    expect(reasonTone({ reason: "spread", side: "bid", val_bps: 180, prev_val_bps: 150 }))
      .toBe("neg");
    expect(reasonTone({ reason: "money", side: "ask",
                        money_ok_rub: 5e6, prev_money_ok_rub: 8e6 })).toBe("neg");
  });

  it("нечего сравнивать — тона нет", () => {
    expect(reasonTone({ reason: "new", side: "ask" })).toBeNull();
    expect(reasonTone({ reason: "block", side: "buy" })).toBeNull();
    expect(reasonTone({ reason: "spread", side: "ask", val_bps: 180 })).toBeNull();
  });
});
