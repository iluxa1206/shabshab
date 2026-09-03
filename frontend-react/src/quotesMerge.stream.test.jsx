import { describe, it, expect } from "vitest";
import { streamMetricPatch, mergeStreamedQuote, wapMetricPatch,
  applyWapQuote } from "./quotesMerge.js";

/**
 * Аудит конвейера 03.09, находка 2 (high). Буфер WS-патчей склеивает пуши одной
 * бумаги плоским спредом, и котировка, пришедшая ПОСЛЕ патча метрик, перетирала
 * цену, оставив спред от прежней — с унаследованным флагом metrics, то есть без
 * приглушения строки. Рассинхрон 27.08.2026 на главном (WS) пути.
 */
describe("streamMetricPatch", () => {
  const metrics = (extra = {}) => ({
    metrics: true,
    yield_over_index_bps: 214, yoi_px: 99.65,
    y_idx_bid_bps: 245, yoi_bid_px: 101.2,
    y_idx_wap_bps: 210, yoi_wap_px: 99.7,
    ...extra,
  });

  it("цена расчёта совпала — числа идут в строку, строка не dim", () => {
    const { fields, stale } = streamMetricPatch(metrics(), {
      last: 99.65, bid_price_pct: 101.2, ask_price_pct: null, wap_price_pct: 99.7,
    });
    expect(fields.yield_over_index_bps).toBe(214);
    expect(fields.y_idx_bid_bps).toBe(245);
    expect(fields.y_idx_wap_bps).toBe(210);
    expect(stale).toBe(false);
  });

  it("склейка увела цену сделки — спред не садится на чужую цену, строка dim", () => {
    // патч посчитан к 99.65, а в строку из котировки ляжет 99.80
    const { fields, stale } = streamMetricPatch(metrics(), {
      last: 99.8, bid_price_pct: 101.2, ask_price_pct: null, wap_price_pct: 99.7,
    });
    expect("yield_over_index_bps" in fields).toBe(false);
    expect(stale).toBe(true);
  });

  it("склейка увела цену стороны — спред стороны не садится на неё", () => {
    // bid в строке уже 101.45 (котировка), спред посчитан к 101.20
    const { fields } = streamMetricPatch(metrics(), {
      last: 99.65, bid_price_pct: 101.45, ask_price_pct: null, wap_price_pct: 99.7,
    });
    expect("y_idx_bid_bps" in fields).toBe(false);
    expect(fields.yield_over_index_bps).toBe(214);   // цена сделки та же — едет
  });

  it("явный null стирает число при любой цене", () => {
    const { fields } = streamMetricPatch(
      metrics({ y_idx_bid_bps: null, yoi_bid_px: null, yoi_px: 1.23 }),
      { last: 99.8, bid_price_pct: 101.45, ask_price_pct: null, wap_price_pct: 99.7 });
    expect(fields.y_idx_bid_bps).toBe(null);
  });

  it("цены расчёта в патче нет (старый бэк) — ведём себя как раньше", () => {
    const { fields, stale } = streamMetricPatch(
      { metrics: true, yield_over_index_bps: 214, y_idx_bid_bps: 245 },
      { last: 99.8, bid_price_pct: 101.45, ask_price_pct: null, wap_price_pct: null });
    expect(fields.yield_over_index_bps).toBe(214);
    expect(fields.y_idx_bid_bps).toBe(245);
    expect(stale).toBe(false);
  });

  it("незначащие знаки цены сверку не рушат (ключ цены — 3 знака)", () => {
    const { fields, stale } = streamMetricPatch(metrics({ yoi_px: 99.6500001 }), {
      last: 99.65, bid_price_pct: 101.2, ask_price_pct: null, wap_price_pct: 99.7,
    });
    expect(fields.yield_over_index_bps).toBe(214);
    expect(stale).toBe(false);
  });

  it("патч без metrics игнорируется целиком", () => {
    expect(streamMetricPatch({ bid: 101.45 }, { last: 99.8 })).toBe(null);
  });
});

describe("mergeStreamedQuote: стирание и метка stale", () => {
  const row = () => ({
    isin: "RU000A100001", yield_over_index_bps: 214, _yoi_stale: true,
    last_price_pct: 99.65, bid_price_pct: 101.2,
  });

  it("явный null стирает число, а не читается как «не изменилось»", () => {
    const n = mergeStreamedQuote(row(), { yoi: null });
    expect(n.yield_over_index_bps).toBe(null);
  });

  it("свежий Y-IDX снимает приглушение на потоковом пути", () => {
    const n = mergeStreamedQuote(row(), { yoi: 231 });
    expect(n.yield_over_index_bps).toBe(231);
    expect(n._yoi_stale).toBe(false);
  });

  it("поля в ответе нет — строка не трогается", () => {
    const r = row();
    expect(mergeStreamedQuote(r, {})).toBe(r);
  });
});

describe("wapMetricPatch", () => {
  const row = () => ({ wap_price_pct: 99.7, y_idx_wap_bps: 210 });

  it("цена расчёта совпала — число едет", () => {
    expect(wapMetricPatch(row(), { yoi_wap: 231, yoi_wap_px: 99.7 }))
      .toEqual({ y_idx_wap_bps: 231 });
  });

  it("средневзвес в строке другой — спред к нему не относится", () => {
    expect(wapMetricPatch(row(), { yoi_wap: 231, yoi_wap_px: 99.4 })).toBe(null);
  });

  it("цены расчёта нет (бумага вне движка) — ставим как раньше", () => {
    expect(wapMetricPatch(row(), { yoi_wap: 231 })).toEqual({ y_idx_wap_bps: 231 });
  });

  it("явный null стирает число при любой цене", () => {
    expect(wapMetricPatch(row(), { yoi_wap: null, yoi_wap_px: 99.4 }))
      .toEqual({ y_idx_wap_bps: null });
  });
});

describe("средневзвес и yoi: пара цена→спред целиком", () => {
  it("новая цена средневзвеса гасит спред и уводит его в stale", () => {
    const b = { wap_price_pct: 99.7, y_idx_wap_bps: 210 };
    const n = { ...b };
    applyWapQuote(b, n, 99.9);
    expect(n.wap_price_pct).toBe(99.9);
    expect(n.y_idx_wap_bps).toBe(null);
    expect(n.y_idx_wap_stale).toBe(210);      // таблица покажет приглушённым
  });

  it("та же цена — ничего не трогаем", () => {
    const b = { wap_price_pct: 99.7, y_idx_wap_bps: 210 };
    const n = { ...b };
    applyWapQuote(b, n, 99.7);
    expect(n.y_idx_wap_bps).toBe(210);
    expect("y_idx_wap_stale" in n).toBe(false);
  });

  it("yoi к чужой цене не садится в строку и не снимает приглушение", () => {
    const row = { last_price_pct: 99.8, yield_over_index_bps: 214, _yoi_stale: true };
    const n = mergeStreamedQuote(row, { yoi: 231, yoi_px: 99.65 });
    expect(n.yield_over_index_bps).toBe(214);   // прежнее число, не чужое
    expect(n._yoi_stale).toBe(true);
  });

  it("yoi к своей цене едет и снимает приглушение", () => {
    const row = { last_price_pct: 99.8, yield_over_index_bps: 214, _yoi_stale: true };
    const n = mergeStreamedQuote(row, { yoi: 231, yoi_px: 99.8 });
    expect(n.yield_over_index_bps).toBe(231);
    expect(n._yoi_stale).toBe(false);
  });

  it("WS-патч со своей ценой снимает приглушение yoi", () => {
    const { fields } = streamMetricPatch(
      { metrics: true, yield_over_index_bps: 231, yoi_px: 99.8 },
      { last: 99.8, bid_price_pct: null, ask_price_pct: null, wap_price_pct: null });
    expect(fields.yield_over_index_bps).toBe(231);
    expect(fields._yoi_stale).toBe(false);
  });
});
