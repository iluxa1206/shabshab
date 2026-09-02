import { describe, it, expect } from "vitest";
import { sideMetricPatch, sideMetricChanges, mergeStreamedQuote,
  applySideQuote } from "./quotesMerge.js";

const row = (o = {}) => ({ isin: "X", bid_price_pct: 100, ask_price_pct: 101,
  y_idx_bid_bps: null, y_idx_ask_bps: null, ...o });

describe("спред стороны из котировок", () => {
  it("ставится, когда цена движка совпала с ценой строки", () => {
    const p = sideMetricPatch(row(), { yoi_bid: 250, yoi_bid_px: 100 });
    expect(p).toEqual({ y_idx_bid_bps: 250 });
  });

  it("НЕ ставится, когда движок считал по другой цене", () => {
    // ровно тот рассинхрон, ради которого цена едет вместе со спредом:
    // число от прошлой цены рядом с новой выглядит согласованным и врёт
    expect(sideMetricPatch(row(), { yoi_bid: 250, yoi_bid_px: 99.5 })).toBeNull();
  });

  it("НЕ ставится без цены расчёта", () => {
    expect(sideMetricPatch(row(), { yoi_bid: 250 })).toBeNull();
  });

  it("сверяется с ценой из ЭТОГО же ответа, а не с прежней", () => {
    // applySideQuote уже положила в патч новую цену 99.5 и погасила спред
    const patch = { bid_price_pct: 99.5, y_idx_bid_bps: null };
    expect(sideMetricPatch(row(), { yoi_bid: 250, yoi_bid_px: 100 }, patch)).toBeNull();
    expect(sideMetricPatch(row(), { yoi_bid: 260, yoi_bid_px: 99.5 }, patch))
      .toEqual({ y_idx_bid_bps: 260 });
  });

  it("не дёргает строку, если число уже стоит", () => {
    const r = row({ y_idx_bid_bps: 250 });
    expect(sideMetricPatch(r, { yoi_bid: 250, yoi_bid_px: 100 })).toBeNull();
    expect(sideMetricChanges(r, { yoi_bid: 251, yoi_bid_px: 100 })).toBe(true);
  });

  it("гашение стороны: цены нет — спред не ставится", () => {
    const r = row({ bid_price_pct: null });
    expect(sideMetricPatch(r, { yoi_bid: 250, yoi_bid_px: 100 })).toBeNull();
  });

  it("бумага на стриме получает спред стороны тем же путём", () => {
    const r = row();
    const n = mergeStreamedQuote(r, { yoi_ask: 300, yoi_ask_px: 101 });
    expect(n).not.toBe(r);
    expect(n.y_idx_ask_bps).toBe(300);
  });

  it("на стриме без изменений ссылка та же", () => {
    const r = row({ y_idx_ask_bps: 300 });
    expect(mergeStreamedQuote(r, { yoi_ask: 300, yoi_ask_px: 101 })).toBe(r);
  });
});

describe("последнее известное число стороны", () => {
  const apply = (row, px, hasKey = true, side = "bid") => {
    const n = { ...row };
    applySideQuote(row, n, side, px, hasKey);
    return n;
  };

  it("цена сдвинулась — спред гаснет, но число запоминается", () => {
    const n = apply(row({ y_idx_bid_bps: 250 }), 99.5);
    expect(n.bid_price_pct).toBe(99.5);
    expect(n.y_idx_bid_bps).toBeNull();      // расчётным его никто не считает
    expect(n.y_idx_bid_stale).toBe(250);     // но показать есть что
  });

  it("вторая подряд смена цены не теряет самое первое число", () => {
    const a = apply(row({ y_idx_bid_bps: 250 }), 99.5);
    const b2 = apply(a, 99.4);
    expect(b2.y_idx_bid_stale).toBe(250);
  });

  it("сторону сняли — запомненное стирается, показывать нечего", () => {
    const n = apply(row({ y_idx_bid_bps: 250, y_idx_bid_stale: 250 }), null);
    expect(n.bid_price_pct).toBeNull();
    expect(n.y_idx_bid_stale).toBeNull();
  });

  it("цена не менялась — строка не трогается вовсе", () => {
    const r = row({ y_idx_bid_bps: 250 });
    const n = { ...r };
    applySideQuote(r, n, "bid", 100, true);
    expect(n.y_idx_bid_bps).toBe(250);
    expect(n.y_idx_bid_stale).toBeUndefined();
  });

  it("оффер работает так же", () => {
    const n = apply(row({ y_idx_ask_bps: 310 }), 101.5, true, "ask");
    expect(n.y_idx_ask_stale).toBe(310);
    expect(n.y_idx_ask_bps).toBeNull();
  });
});
