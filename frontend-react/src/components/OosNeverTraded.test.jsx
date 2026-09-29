/**
 * Две пустоты, которые не «нет данных» (разбор 29.09.2026, Самолёт).
 *
 *  - Y-IDX за верхом шкалы (доходность > 1000 %): число снято сторожем, но это
 *    дефолт/дистресс — вместо прочерка метка «>1000%».
 *  - Сделок на бирже не было ни разу: цена в строке — цена размещения, под
 *    ней подпись, спред приглушён; фильтр прячет такие строки.
 */
import { afterEach, describe, it, expect } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { COLS, Quote } from "./BondTable.jsx";
import { mergeStreamedQuote, streamMetricPatch } from "../quotesMerge.js";

afterEach(cleanup);

const td = (key, b) => {
  const col = COLS.find((c) => c.key === key);
  const { container } = render(<table><tbody><tr>{col.cell(b)}</tr></tbody></table>);
  return container.querySelector("td");
};

describe("Y-IDX за верхом шкалы", () => {
  it("SPREAD: метка вместо прочерка", () => {
    const c = td("yield_over_index_bps", { yield_over_index_bps: null, y_idx_oos: true });
    expect(c.textContent).toContain(">1000%");
    expect(c.querySelector(".dash")).toBeNull();
  });

  it("число есть — метка не нужна, даже если флаг устарел", () => {
    const c = td("yield_over_index_bps", { yield_over_index_bps: 21164, y_idx_oos: true });
    expect(c.textContent).not.toContain(">1000%");
  });

  it("пусто без флага — честный прочерк", () => {
    const c = td("yield_over_index_bps", { yield_over_index_bps: null });
    expect(c.querySelector(".dash")).not.toBeNull();
  });

  it("сторона стакана с ценой и без числа — та же метка", () => {
    const { container } = render(<table><tbody><tr>
      <Quote px={1.02} spread={null} side="bid" oos /></tr></tbody></table>);
    expect(container.querySelector(".q-sp").textContent).toContain(">1000%");
  });

  it("флаг едет котировками и патчем движка", () => {
    const row = { isin: "X", last_price_pct: 1.5, yield_over_index_bps: null, y_idx_oos: false };
    expect(mergeStreamedQuote(row, { yoi: null, yoi_oos: true }).y_idx_oos).toBe(true);
    const p = streamMetricPatch({ metrics: true, yield_over_index_bps: null, y_idx_oos: true,
      yoi_px: 1.5 }, { last: 1.5 });
    expect(p.fields.y_idx_oos).toBe(true);
  });
});

describe("сделок не было ни разу", () => {
  it("PRICE подписан «разм.» и приглушён", () => {
    const c = td("last_price_pct", { last_price_pct: 75, price_stale: true, never_traded: true });
    expect(c.textContent).toContain("75");
    expect(c.textContent).toContain("разм.");
    expect(c.className).toContain("px-stale");
    expect(c.getAttribute("title")).toMatch(/ни разу/);
  });

  it("SPREAD к цене размещения приглушён и подписан", () => {
    const c = td("yield_over_index_bps", { yield_over_index_bps: 1002, never_traded: true });
    expect(c.textContent).toContain("1");
    expect(c.className).toContain("q-sp-old");
    expect(c.getAttribute("title")).toMatch(/размещени/);
  });

  it("обычная бумага без подписи", () => {
    const c = td("last_price_pct", { last_price_pct: 99.5, never_traded: false });
    expect(c.textContent).not.toContain("разм.");
  });
});
