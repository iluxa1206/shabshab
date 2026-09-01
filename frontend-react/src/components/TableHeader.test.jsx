/**
 * Полоса прогресса в заголовке колонки: сколько её чисел движок уже посчитал.
 * Спред стороны считает бэкенд пачками (сетка цен, очередь сторон, догрев), и
 * без подложки прочерк в колонке неотличим от «числа не будет».
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { HeaderCell } from "./TableHeader.jsx";

afterEach(cleanup);

const COL = { key: "y_idx_bid_bps", label: "BID", sub: "% / R-spread", align: "num" };

function show(progress) {
  const { container } = render(
    <table><thead><tr>
      <HeaderCell col={COL} sort={{ key: "isin", dir: "desc" }} onSort={() => {}}
        dragRef={{ current: null }} setDragKey={() => {}} setOverKey={() => {}}
        progress={progress} />
    </tr></thead></table>
  );
  return container.querySelector(".th-progress");
}

describe("полоса прогресса колонки", () => {
  it("ширина — доля посчитанных чисел", () => {
    expect(show({ done: 120, total: 480 }).style.width).toBe("25%");
  });

  it("на нуле видна засечкой: «считается, чисел ещё нет»", () => {
    expect(show({ done: 0, total: 480 }).style.width).toBe("2%");
  });

  it("всё посчитано — полосы нет", () => {
    expect(show({ done: 480, total: 480 })).toBeNull();
  });

  it("ждать нечего (ни одной строки со стороной) — полосы нет", () => {
    expect(show({ done: 0, total: 0 })).toBeNull();
  });

  it("прогресс не передан — заголовок как раньше", () => {
    expect(show(undefined)).toBeNull();
  });
});
