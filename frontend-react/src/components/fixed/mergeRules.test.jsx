/**
 * Правила слияния живых котировок в строку МОНИТОРА ФИКСОВ.
 *
 * Цена и её производные (YTM, g-спред, dirty) ходят В РАЗНОМ ТЕМПЕ: цена — с
 * каждым тиком, метрики — тактом движка. Если их не развести, строка показывает
 * свежую цену со спредом от прежней — тот самый рассинхрон, из-за которого у
 * флоатеров 27.08.2026 в телеграм уехала вся лестница стакана.
 */
import { describe, expect, it } from "vitest";
import { applyPatch, applyPrice } from "./FixedMonitor.jsx";

describe("новая цена гасит производные", () => {
  it("сдвиг цены сделки стирает метрики, посчитанные по прежней", () => {
    const row = { last_price_pct: 100, ytm: 15, g_spread_bps: 60 };
    applyPrice(row, "last_price_pct", 100.5, ["ytm", "g_spread_bps"]);
    expect(row.last_price_pct).toBe(100.5);
    expect(row.ytm).toBeNull();
    expect(row.g_spread_bps).toBeNull();
  });

  it("та же цена ничего не трогает", () => {
    const row = { bid: 99, g_spread_bid_bps: 70 };
    applyPrice(row, "bid", 99, ["g_spread_bid_bps"], true);
    expect(row.g_spread_bid_bps).toBe(70);
  });

  it("ушедшая сторона стакана гасится вместе со спредом", () => {
    const row = { bid: 99, g_spread_bid_bps: 70 };
    applyPrice(row, "bid", undefined, ["g_spread_bid_bps"], true);
    expect(row.bid).toBeNull();
    expect(row.g_spread_bid_bps).toBeNull();
  });

  it("отсутствие поля в ответе — не «стороны нет»", () => {
    const row = { wap_pct: 100.2, g_spread_wap_bps: 55 };
    applyPrice(row, "wap_pct", undefined, ["g_spread_wap_bps"]);
    expect(row.wap_pct).toBe(100.2);
    expect(row.g_spread_wap_bps).toBe(55);
  });
});

describe("патч стрима", () => {
  it("котировка без метрик помечает строку несинхронной и гасит спреды сторон", () => {
    const n = applyPatch({ ytm: 15, g_spread_bid_bps: 70, ytm_bid: 15.1 },
                         { last_price_pct: 101, bid: 100.9, ask: 101.2 });
    expect(n._mstale).toBe(true);
    expect(n.g_spread_bid_bps).toBeNull();
    expect(n.ytm_bid).toBeNull();
    expect(n.last_price_pct).toBe(101);
  });

  it("патч движка снимает пометку и приносит числа", () => {
    const n = applyPatch({ _mstale: true }, {
      metrics: true, ytm: 16.2, g_spread_bps: 44, g_spread_bid_bps: 46, dirty: 1004,
    });
    expect(n._mstale).toBe(false);
    expect(n.ytm).toBe(16.2);
    expect(n.g_spread_bid_bps).toBe(46);
  });

  it("явный null в патче СТИРАЕТ число (сторона ушла из книги)", () => {
    const n = applyPatch({ g_spread_ask_bps: 50 }, { metrics: true, g_spread_ask_bps: null });
    expect(n.g_spread_ask_bps).toBeNull();
  });

  it("оборот дня назад не откатывается", () => {
    const n = applyPatch({ val_today: 5e8 }, { val_today: 1e8 });
    expect(n.val_today).toBe(5e8);
  });

  it("свой VWAP из потока перебивает биржевой средневзвес", () => {
    const n = applyPatch({ wap_pct: 100.1 }, { vwap_pct: 100.4 });
    expect(n.wap_pct).toBe(100.4);
  });
});

describe("пустая цена не стирает строку", () => {
  it("снапшот без цены сделки оставляет прежнюю (у неликвида это prev-close)", () => {
    const row = { last_price_pct: 99.4, ytm: 15 };
    applyPrice(row, "last_price_pct", null, ["ytm"]);
    expect(row.last_price_pct).toBe(99.4);
    expect(row.ytm).toBe(15);
  });

  it("патч без сделок сегодня цену не гасит", () => {
    const n = applyPatch({ last_price_pct: 99.4 }, { last_price_pct: null, bid: 99.3 });
    expect(n.last_price_pct).toBe(99.4);
    expect(n.bid).toBe(99.3);
  });
});

describe("пара «цена → спред» едет из одного расчёта", () => {
  it("средневзвес движка приходит вместе со своим g-спредом", () => {
    const n = applyPatch({ wap_pct: 100.1, g_spread_wap_bps: 40 },
                         { metrics: true, wap_pct: 100.6, g_spread_wap_bps: 33 });
    expect(n.wap_pct).toBe(100.6);
    expect(n.g_spread_wap_bps).toBe(33);
  });
});

describe("фильтр по объёму на витрине фиксов", () => {
  // Арифметика книги одна на две витрины (src/vwap.js), различаются только
  // имена чисел, которые она подменяет: у фикса это цена стороны и g-спред.
  const LADDER = { b: [[99.5, 100], [99.0, 5000]], a: [[100.5, 100], [101.0, 5000]] };
  const ROW = {
    isin: "RU000A1FIX01", face_value_rub: 1000, accrued_rub: 10,
    bid: 99.5, ask: 100.5, g_spread_bid_bps: 120, g_spread_ask_bps: 90,
    vol_bid_price_pct: 99.1, g_spread_vol_bid_bps: 135,
  };

  it("цена стороны и g-спред берутся на объём набора", async () => {
    const { applyVolume, FIXED_VOL_FIELDS } = await import("../../vwap.js");
    const n = applyVolume(ROW, LADDER, 3_000_000, 0, "and", FIXED_VOL_FIELDS);
    expect(n.bid).toBe(99.1);                 // число движка, не верх стакана
    expect(n.g_spread_bid_bps).toBe(135);
    expect(n.ask).toBe(100.5);                // сторона без фильтра не тронута
    expect(n._vwap_bid).toBeGreaterThan(0);
  });

  it("книги не хватило — строка уходит из выборки", async () => {
    const { applyVolume, FIXED_VOL_FIELDS } = await import("../../vwap.js");
    expect(applyVolume(ROW, LADDER, 900_000_000, 0, "and", FIXED_VOL_FIELDS)).toBeNull();
  });
});
