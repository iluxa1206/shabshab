import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { UNIFIED_COLS, UNIFIED_COL_META, UNIFIED_DEFAULT_COLS } from "./unifiedCols.jsx";
import { withFixedAliases } from "../unifiedRows.js";

const FLOATER = {
  isin: "RU000A0FLT01", kind: "floater", short_name: "РЖД БО 1Р-05", base_rate_type: "KEYRATE",
  formula: "КС + 1,2%", spread_issue_bps: 120, coupons_per_year: 4, maturity_date: "2029-06-01",
  next_coupon_date: "2026-11-01", issue_date: "2024-06-01", issue_volume: 10e9, rating: "AA",
  bid_price_pct: 99.1, ask_price_pct: 99.4, y_idx_bid_bps: 210, y_idx_ask_bps: 190,
  last_price_pct: 99.3, wap_price_pct: 99.25, y_idx_wap_bps: 200, delta_to_prev_close: 0.1,
  dirty_price_rub: 1005.2, val_today: 5e6, adv_1m_rub: 3e6, yield_over_index_bps: 205,
  dm_bps: 220, disc_margin_bps: 218, z_model_bps: 150, yield_xirr_pct: 16.2, index_yield_pct: 15.0,
  preferred_horizon: "maturity",
};
const FIXED = withFixedAliases({
  isin: "RU000A0FIX01", kind: "fixed", name: "ОФЗ 26238", issuer: "Минфин", cls: "ofz",
  coupon_pct: 7.1, maturity_date: "2041-05-15", issue_date: "2021-06-01", issue_volume: 500e9,
  bid: 60.4, ask: 60.6, wap_pct: 60.45, last_price_pct: 60.5, ytm: 14.2, ytm_bid: 14.25,
  ytm_ask: 14.1, ytm_wap: 14.2, y_idx_bid_bps: -148, y_idx_ask_bps: -150, y_idx_wap_bps: -151,
  yield_over_index_bps: -150, dirty: 612.3, face_unit: "RUB", val_today: 2e8, adv_1m_rub: 5e9,
  delta_to_prev_close: -0.2, mod_dur: 7.9, g_spread_bps: -12, z_spread_bps: -10, cur_yield: 11.7,
  delta_ytm: 0.05, convexity: 90, dv01: 0.5, rating: "AAA",
});

const cells = (cols, row) => {
  const { container } = render(<table><tbody><tr>{cols.map((c) => c.cell(row))}</tr></tbody></table>);
  return [...container.querySelectorAll("td")];
};

describe("колонки общего монитора", () => {
  it("ключи уникальны, а набор по умолчанию — подмножество", () => {
    const keys = UNIFIED_COLS.map((c) => c.key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(UNIFIED_DEFAULT_COLS.every((k) => keys.includes(k))).toBe(true);
    expect(UNIFIED_COL_META.length).toBe(UNIFIED_COLS.length);
  });

  it("по умолчанию из спец-колонок видна только Δ YTM", () => {
    const only = UNIFIED_COLS.filter((c) => c.only).map((c) => c.key);
    expect(only.length).toBeGreaterThan(5);
    expect(only.filter((k) => UNIFIED_DEFAULT_COLS.includes(k))).toEqual(["delta_ytm"]);
  });

  it("каждая колонка рисует одну ячейку и для флоатера, и для фикса", () => {
    for (const row of [FLOATER, FIXED]) {
      for (const c of UNIFIED_COLS) {
        const tds = cells([c], row);
        expect(tds.length, `${c.key} / ${row.kind}`).toBe(1);
      }
    }
  });

  it("первая ячейка несёт полосу класса", () => {
    expect(cells([UNIFIED_COLS[0]], FLOATER)[0].className).toContain("cls-float");
    expect(cells([UNIFIED_COLS[0]], FIXED)[0].className).toContain("cls-fixed");
  });

  it("колонка чужого класса пуста, а не прочерк", () => {
    const g = UNIFIED_COLS.find((c) => c.key === "g_spread_bps");
    expect(cells([g], FLOATER)[0].textContent).toBe("");
    expect(cells([g], FIXED)[0].textContent).not.toBe("");
    const dm = UNIFIED_COLS.find((c) => c.key === "disc_margin_bps");
    expect(cells([dm], FIXED)[0].textContent).toBe("");
    expect(cells([dm], FLOATER)[0].textContent).not.toBe("");
  });

  it("BID у фикса: цена, Y-IDX и YTM по этой цене", () => {
    const bid = UNIFIED_COLS.find((c) => c.key === "y_idx_bid_bps");
    const t = cells([bid], FIXED)[0].textContent;
    expect(t).toContain("60,40");
    expect(t).toContain("-148");
    expect(t).toContain("14,25");
  });

  it("КУПОН: формула у флоатера, ставка у фикса", () => {
    const cpn = UNIFIED_COLS.find((c) => c.key === "coupon");
    expect(cells([cpn], FLOATER)[0].textContent).toMatch(/КС|1,2|120/);
    expect(cells([cpn], FIXED)[0].textContent).toContain("7,10");
  });

  it("КЛАСС: ФИКС у фикса, база у флоатера", () => {
    const k = UNIFIED_COLS.find((c) => c.key === "base_rate_type");
    expect(cells([k], FIXED)[0].textContent).toBe("ФИКС");
    expect(cells([k], FLOATER)[0].textContent).toBe("КС");
  });

  it("DUR: у флоатера срок с пометкой, у фикса дюрация", () => {
    const d = UNIFIED_COLS.find((c) => c.key === "dur");
    expect(cells([d], FIXED)[0].textContent).toContain("7,90");
    expect(cells([d], FLOATER)[0].textContent).toContain("срок");
  });
});
