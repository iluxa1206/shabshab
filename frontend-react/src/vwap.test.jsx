import { describe, it, expect } from "vitest";
import { applyVolume, vwapFor } from "./vwap.js";

// Лестница: цена% и количество бумаг. Номинал 1000 ₽, НКД 0 — деньги уровня
// считаются как qty × номинал × цена/100.
const ladder = {
  b: [[99.62, 100], [99.10, 20000]],
  a: [[100.10, 100], [100.80, 20000]],
};
const row = {
  isin: "RU000A100001", face_value_rub: 1000, accrued_rub: 0,
  bid_price_pct: 99.62, ask_price_pct: 100.10,
  y_idx_bid_bps: 214, y_idx_ask_bps: 208,
  // память ячейки: спред к цене ВЕРХА СТАКАНА, набитый по сырой строке
  y_idx_bid_stale: 214, y_idx_ask_stale: 208,
};

describe("фильтр по объёму", () => {
  it("цена стороны — VWAP набора, а не верх стакана", () => {
    const got = applyVolume(row, ladder, 5_000_000, 0);
    expect(got.bid_price_pct).toBeLessThan(99.62);
    expect(got._vwap_bid).toBeGreaterThan(0);
  });

  it("память ячейки гаснет вместе с ценой: под ценой набора не показываем спред верха стакана", () => {
    // движок ещё не досчитал спред набора — ячейка обязана дать прочерк, а не
    // приглушённые 214 бп (это спред к ДРУГОЙ цене, и разница между лучшей
    // ценой и глубиной тикета читалась бы как нулевая)
    const got = applyVolume(row, ladder, 5_000_000, 0);
    expect(got.y_idx_bid_bps).toBe(null);
    expect(got.y_idx_bid_stale).toBe(null);
  });

  it("нефильтруемая сторона память сохраняет", () => {
    const got = applyVolume(row, ladder, 5_000_000, 0);
    expect(got.y_idx_ask_bps).toBe(208);
    expect(got.y_idx_ask_stale).toBe(208);
  });

  it("спред набора с бэкенда доезжает в ячейку", () => {
    const got = applyVolume({ ...row, y_idx_vol_bid_bps: 260 }, ladder, 5_000_000, 0);
    expect(got.y_idx_bid_bps).toBe(260);
    expect(got.y_idx_bid_stale).toBe(null);
  });

  it("VWAP взвешен деньгами: набор глубже верха книги", () => {
    const v = vwapFor(ladder.b, 5_000_000, 1000, 0);
    expect(v.px).toBeGreaterThan(99.10);
    expect(v.px).toBeLessThan(99.62);
    expect(v.partial).toBe(false);
  });
});
