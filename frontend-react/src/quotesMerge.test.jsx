/**
 * Слияние котировок такта 5 с в строку монитора: числа НАБОРА НА ОБЪЁМ едут
 * этим же путём.
 *
 * Зачем: движок считает цену набора пачками (сетка, очередь сторон, догрев), и
 * число, появившееся через такт после включения фильтра по объёму, доезжало до
 * таблицы только следующим полным запросом /api/bonds — в колонках бида и
 * оффера всё это время стоял прочерк.
 */
import { describe, expect, it } from "vitest";
import { mergeStreamedQuote, quoteChanges, QUOTE_METRIC_FIELDS } from "./quotesMerge.js";

describe("quotesMerge: числа набора на объём", () => {
  it("цена набора и её Y-IDX доезжают до бумаги НА СТРИМЕ", () => {
    const row = { isin: "RU000A100001", last_price_pct: 100 };
    const n = mergeStreamedQuote(row, { vol_bid_px: 99.87, vol_bid_y: 214 });
    expect(n.vol_bid_price_pct).toBe(99.87);
    expect(n.y_idx_vol_bid_bps).toBe(214);
    expect(n.last_price_pct).toBe(100);          // цены у стримовой свои
  });

  it("те же числа в строке — та же ссылка (без ререндера таблицы)", () => {
    const row = { isin: "RU000A100001", vol_ask_price_pct: 100.2, y_idx_vol_ask_bps: 190 };
    expect(mergeStreamedQuote(row, { vol_ask_px: 100.2, vol_ask_y: 190 })).toBe(row);
  });

  it("прочерк в строке + число в котировке = изменение", () => {
    const row = { isin: "RU000A100001", y_idx_vol_bid_bps: null };
    expect(quoteChanges(row, { vol_bid_y: 214 }, QUOTE_METRIC_FIELDS)).toBe(true);
    // числа нет и у движка — строку не трогаем
    expect(quoteChanges(row, { vol_bid_y: null }, QUOTE_METRIC_FIELDS)).toBe(false);
  });
});
