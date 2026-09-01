/**
 * Прогресс заполнения спредов: сколько чисел движок уже посчитал.
 *
 * Считается по ЦЕНЕ стороны — иначе полоса не доходила бы до конца: у стороны
 * без цены (нет заявок, а при фильтре по объёму — книги не хватило на тикет)
 * прочерк в спреде окончательный, и ждать там нечего.
 */
import { describe, expect, it } from "vitest";
import { sideProgress, spreadProgress } from "./spreadProgress.js";

const SIDES = [["bid_price_pct", "y_idx_bid_bps"], ["ask_price_pct", "y_idx_ask_bps"]];

describe("прогресс по одной стороне", () => {
  it("в знаменателе только строки с ценой стороны", () => {
    const rows = [
      { bid_price_pct: 99, y_idx_bid_bps: 200 },     // посчитана
      { bid_price_pct: 98, y_idx_bid_bps: null },    // ждём движок
      { bid_price_pct: null, y_idx_bid_bps: null },  // бида нет — не ждём
    ];
    expect(sideProgress(rows, "bid_price_pct", "y_idx_bid_bps")).toEqual({ done: 1, total: 2 });
  });

  it("пустая таблица — ноль из нуля, а не деление на ноль", () => {
    expect(sideProgress([], "bid_price_pct", "y_idx_bid_bps")).toEqual({ done: 0, total: 0 });
  });
});

describe("общий прогресс обеих сторон", () => {
  it("слоты «строка × сторона» складываются", () => {
    const rows = [
      { bid_price_pct: 99, ask_price_pct: 101, y_idx_bid_bps: 200, y_idx_ask_bps: null },
      { bid_price_pct: 99, ask_price_pct: null, y_idx_bid_bps: null, y_idx_ask_bps: null },
    ];
    expect(spreadProgress(rows, SIDES)).toEqual({ done: 1, total: 3 });
  });

  it("всё посчитано — done равен total (полоса гаснет)", () => {
    const rows = [{ bid_price_pct: 99, ask_price_pct: 101, y_idx_bid_bps: 200, y_idx_ask_bps: 190 }];
    const p = spreadProgress(rows, SIDES);
    expect(p.done).toBe(p.total);
  });

  it("поля фикса — те же правила, другие имена", () => {
    const rows = [{ bid: 99, ask: 101, g_spread_bid_bps: 120, g_spread_ask_bps: null }];
    expect(spreadProgress(rows, [["bid", "g_spread_bid_bps"], ["ask", "g_spread_ask_bps"]]))
      .toEqual({ done: 1, total: 2 });
  });
});
