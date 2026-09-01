/**
 * Аналитика фиксов: срок вместо дюрации.
 *
 * Регресс саморевью 01.09: после перевода монитора и панели флоатеров на
 * горизонт эта панель осталась на mod_dur, а профиль срочности считал годы до
 * ПОГАШЕНИЯ своей арифметикой — бумага с офертой через год попадала в корзину
 * «5–10 лет», хотя деньги вернутся через год.
 *
 * Сами scatter'ы в jsdom не рисуются (меряют контейнер, размеров нет),
 * поэтому проверяем то, что видно без размеров: заголовки и корзины
 * гистограммы.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import FixedAnalytics, { maturityBuckets } from "./FixedAnalytics.jsx";

const inYears = (y) => {
  const d = new Date();
  d.setDate(d.getDate() + Math.round(y * 365.25));
  return d.toISOString().slice(0, 10);
};

// гасится через одиннадцать лет, но оферта через год — срок у неё годовой
const WITH_PUT = {
  isin: "RU000A100001", name: "ОФЗ 26998", issuer: "ОФЗ", rating: "AAA",
  maturity_date: inYears(11), put_date: inYears(1), mod_dur: 0.9,
  g_spread_wap_bps: 120, last_price_pct: 99.4,
};
const PLAIN = {
  isin: "RU000A100002", name: "ОФЗ 26999", issuer: "ОФЗ", rating: "AAA",
  maturity_date: inYears(7), mod_dur: 5.1, g_spread_wap_bps: 60,
  last_price_pct: 85.7,
};

afterEach(cleanup);

describe("аналитика фиксов", () => {
  it("карточка называет ось сроком, а не дюрацией", () => {
    render(<FixedAnalytics rows={[WITH_PUT, PLAIN]} />);
    expect(screen.getByText(/G-СПРЕД vs СРОК/)).toBeTruthy();
    expect(screen.queryByText(/G-СПРЕД vs ДЮРАЦИЯ/)).toBeNull();
  });

  it("профиль срочности кладёт бумагу по ОФЕРТЕ, а не по погашению", () => {
    const byLabel = Object.fromEntries(
      maturityBuckets([WITH_PUT, PLAIN]).map((b) => [b.lbl, b.n]));
    // оферта через год → корзина «<1г» либо «1–3» (округление дня в дату),
    // но точно не «>10», куда её клало погашение
    expect(byLabel["<1г"] + byLabel["1–3"]).toBe(1);
    expect(byLabel[">10"]).toBe(0);
    // соседняя бумага без оферты остаётся семилетней
    expect(byLabel["5–10"]).toBe(1);
  });
});
