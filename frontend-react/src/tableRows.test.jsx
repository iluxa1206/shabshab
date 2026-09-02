import { describe, it, expect } from "vitest";
import { sortRows, filterByAdv, filterBySpread } from "./tableRows.js";

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

describe("окно спреда с памятью", () => {
  const rs = (...v) => v.map(([isin, y]) => ({ isin, yield_over_index_bps: y }));

  it("обычная фильтрация по границам", () => {
    const got = filterBySpread(rs(["A", 100], ["B", 300], ["C", 200]), 150, 250, new Map());
    expect(got.map((r) => r.isin)).toEqual(["C"]);
  });

  it("строка НЕ исчезает, пока спред пересчитывается", () => {
    const memo = new Map();
    filterBySpread(rs(["A", 100], ["B", 200]), 150, NaN, memo);
    const got = filterBySpread(rs(["A", 100], ["B", null]), 150, NaN, memo);
    expect(got.map((r) => r.isin)).toEqual(["B"]);
  });

  it("новый спред вне окна — строка уходит", () => {
    const memo = new Map();
    filterBySpread(rs(["A", 200]), 150, NaN, memo);
    filterBySpread(rs(["A", null]), 150, NaN, memo);
    expect(filterBySpread(rs(["A", 100]), 150, NaN, memo)).toEqual([]);
  });

  it("не считавшаяся ни разу не проходит окно", () => {
    expect(filterBySpread(rs(["A", null]), 150, NaN, new Map())).toEqual([]);
  });

  it("без границ список не трогается", () => {
    const src = rs(["A", null], ["B", 10]);
    expect(filterBySpread(src, NaN, NaN, new Map())).toBe(src);
  });

  it("ушедшая с экрана строка забывается", () => {
    const memo = new Map();
    filterBySpread(rs(["A", 200], ["B", 300]), 150, NaN, memo);
    filterBySpread(rs(["A", 200]), 150, NaN, memo);
    expect([...memo.keys()]).toEqual(["A"]);
  });
});

describe("порог ликвидности по ADV", () => {
  const av = (...xs) => xs.map(([isin, adv]) => ({ isin, adv_1m_rub: adv }));

  it("≥ отсекает неликвид, ≤ оставляет только тонкие", () => {
    const src = av(["A", 5e6], ["B", 50e6], ["C", 200e6]);
    expect(filterByAdv(src, 50, "gte").map((r) => r.isin)).toEqual(["B", "C"]);
    expect(filterByAdv(src, 50, "lte").map((r) => r.isin)).toEqual(["A", "B"]);
  });

  it("бумага без ADV при заданном пороге скрыта в обе стороны", () => {
    const src = av(["A", null]);
    expect(filterByAdv(src, 10, "gte")).toEqual([]);
    expect(filterByAdv(src, 10, "lte")).toEqual([]);
  });

  it("без порога список не трогается", () => {
    const src = av(["A", null], ["B", 1e6]);
    expect(filterByAdv(src, NaN, "gte")).toBe(src);
  });
});
