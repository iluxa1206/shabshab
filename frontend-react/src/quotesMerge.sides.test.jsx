import { describe, it, expect } from "vitest";
import { sideMetricPatch, sideMetricChanges, mergeStreamedQuote } from "./quotesMerge.js";

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
