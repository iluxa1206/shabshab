/**
 * Спред к прежней цене: показываем приглушённым, а не прочерком.
 *
 * Цена в ячейке уже новая, движок ещё считает. Пустая клетка говорила «спреда
 * нет», хотя порядок величины известен; выдавать старое число за посчитанное
 * тоже нельзя — отсюда полупрозрачность и подпись.
 */
import { afterEach, describe, it, expect } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { Quote } from "./BondTable.jsx";

afterEach(cleanup);

const cell = (props) => {
  const { container } = render(<table><tbody><tr><Quote {...props} /></tr></tbody></table>);
  return container.querySelector(".q-sp");
};

describe("устаревший спред стороны", () => {
  it("свежий спред рисуется в полную яркость", () => {
    const sp = cell({ px: 100.5, spread: 250, side: "bid" });
    expect(sp.textContent).toContain("250");
    expect(sp.className).not.toContain("q-sp-old");
  });

  it("цена ушла — последнее число видно, но приглушено и подписано", () => {
    const sp = cell({ px: 100.6, spread: null, stale: 250, side: "bid" });
    expect(sp.textContent).toContain("250");
    expect(sp.className).toContain("q-sp-old");
    expect(sp.getAttribute("title")).toMatch(/пересчит/);
  });

  it("свежее число побеждает запомненное", () => {
    const sp = cell({ px: 100.6, spread: 240, stale: 250, side: "bid" });
    expect(sp.textContent).toContain("240");
    expect(sp.className).not.toContain("q-sp-old");
  });

  it("не считалась ни разу — честный прочерк", () => {
    const sp = cell({ px: 100.6, spread: null, stale: null, side: "bid" });
    expect(sp.textContent).not.toMatch(/\d/);
  });

  it("стороны в книге нет — одна ячейка-прочерк, запомненное не всплывает", () => {
    const { container } = render(
      <table><tbody><tr><Quote px={null} spread={null} stale={250} side="bid" /></tr></tbody></table>);
    expect(container.querySelector(".q-sp")).toBeNull();
    expect(container.textContent).not.toContain("250");
  });

  it("нулевая цена — тоже «стороны нет»", () => {
    const { container } = render(
      <table><tbody><tr><Quote px={0} spread={null} stale={250} side="ask" /></tr></tbody></table>);
    expect(container.textContent).not.toContain("250");
  });

  it("у устаревшего числа не рисуется отклонение от семидневки", () => {
    // сравнивать было бы не с чем: спред относится к ПРЕЖНЕЙ цене
    const sp = cell({ px: 100.6, spread: null, stale: 250, base7: 180, side: "bid" });
    expect(sp.querySelector(".q-sp-dev")).toBeNull();
  });
});
