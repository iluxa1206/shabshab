import { describe, it, expect } from "vitest";
import { sortRows } from "./sortRows.js";

const rows = (...v) => v.map(([isin, y]) => ({ isin, y_idx_ask_bps: y }));
const val = (r) => r.y_idx_ask_bps;
const fresh = () => ({ key: null, map: new Map() });
const order = (rs) => rs.map((r) => r.isin);

describe("сортировка с памятью позиции", () => {
  it("обычный порядок по убыванию", () => {
    const got = sortRows(rows(["A", 100], ["B", 300], ["C", 200]), "y_idx_ask_bps", "desc", fresh(), val);
    expect(order(got)).toEqual(["B", "C", "A"]);
  });

  it("строка с погасшим спредом ДЕРЖИТ место, а не улетает вниз", () => {
    const memo = fresh();
    sortRows(rows(["A", 100], ["B", 300], ["C", 200]), "y_idx_ask_bps", "desc", memo, val);
    // цена тикнула у B: спред погашен до пересчёта
    const got = sortRows(rows(["A", 100], ["B", null], ["C", 200]), "y_idx_ask_bps", "desc", memo, val);
    expect(order(got)).toEqual(["B", "C", "A"]);
  });

  it("пересчёт по новой цене двигает строку на новое место", () => {
    const memo = fresh();
    sortRows(rows(["A", 100], ["B", 300], ["C", 200]), "y_idx_ask_bps", "desc", memo, val);
    sortRows(rows(["A", 100], ["B", null], ["C", 200]), "y_idx_ask_bps", "desc", memo, val);
    const got = sortRows(rows(["A", 100], ["B", 150], ["C", 200]), "y_idx_ask_bps", "desc", memo, val);
    expect(order(got)).toEqual(["C", "B", "A"]);
  });

  it("не считавшаяся ни разу уходит в конец при любом направлении", () => {
    const memo = fresh();
    expect(order(sortRows(rows(["A", 100], ["B", null], ["C", 200]), "y_idx_ask_bps", "desc", memo, val)))
      .toEqual(["C", "A", "B"]);
    expect(order(sortRows(rows(["A", 100], ["B", null], ["C", 200]), "y_idx_ask_bps", "asc", memo, val)))
      .toEqual(["A", "C", "B"]);
  });

  it("смена столбца сбрасывает память: она была не о нём", () => {
    const memo = fresh();
    sortRows(rows(["A", 100], ["B", 300]), "y_idx_ask_bps", "desc", memo, val);
    const byBid = [{ isin: "A", y_idx_bid_bps: 5 }, { isin: "B", y_idx_bid_bps: null }];
    const got = sortRows(byBid, "y_idx_bid_bps", "desc", memo, (r) => r.y_idx_bid_bps);
    expect(order(got)).toEqual(["A", "B"]);
    expect(memo.map.has("B")).toBe(false);
  });

  it("ушедшая из фильтра строка забывается — карта не копит весь рынок", () => {
    const memo = fresh();
    sortRows(rows(["A", 100], ["B", 300], ["C", 200]), "y_idx_ask_bps", "desc", memo, val);
    sortRows(rows(["A", 100]), "y_idx_ask_bps", "desc", memo, val);
    expect([...memo.map.keys()]).toEqual(["A"]);
  });

  it("строки сортируются как текст (эмитент, название)", () => {
    const memo = fresh();
    const rs = [{ isin: "A", name: "Яндекс" }, { isin: "B", name: "Аэрофлот" }];
    const got = sortRows(rs, "name", "asc", memo, (r) => r.name);
    expect(order(got)).toEqual(["B", "A"]);
  });
});
