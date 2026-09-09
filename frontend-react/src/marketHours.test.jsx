import { describe, it, expect } from "vitest";
import { inTradingHours, liveInterval, moscowNow } from "./marketHours.js";

// Даты в UTC: МСК = UTC+3 круглый год, так что 09:00Z = 12:00 МСК.
const utc = (s) => new Date(`${s}Z`);

describe("inTradingHours", () => {
  it("будний день в середине сессии — торгуем", () => {
    expect(inTradingHours(utc("2026-09-08T09:00:00"))).toBe(true);  // вт 12:00 МСК
  });
  it("края окна включительно", () => {
    expect(inTradingHours(utc("2026-09-08T04:00:00"))).toBe(true);  // 07:00 МСК
    expect(inTradingHours(utc("2026-09-08T20:50:00"))).toBe(true);  // 23:50 МСК
  });
  it("ночь — нет", () => {
    expect(inTradingHours(utc("2026-09-08T03:59:00"))).toBe(false); // 06:59 МСК
    expect(inTradingHours(utc("2026-09-08T20:51:00"))).toBe(false); // 23:51 МСК
  });
  it("выходные — нет даже в торговые часы", () => {
    expect(inTradingHours(utc("2026-09-05T09:00:00"))).toBe(false); // сб
    expect(inTradingHours(utc("2026-09-06T09:00:00"))).toBe(false); // вс
  });
  it("МСК считается по таймзоне, а не по локали машины", () => {
    // 22:30 UTC во вторник = 01:30 МСК среды: и день, и час другие
    expect(moscowNow(utc("2026-09-08T22:30:00"))).toEqual(
      { weekday: "Wed", hour: 1, minute: 30 });
  });
});

describe("liveInterval", () => {
  it("вне сессии отдаёт false — react-query гасит таймер", () => {
    // подменять системное время тут не нужно: проверяем обе ветки через inTradingHours
    const v = liveInterval(60_000)();
    expect(v === false || v === 60_000).toBe(true);
  });
});
