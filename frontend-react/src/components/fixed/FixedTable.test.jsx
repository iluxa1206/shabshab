/**
 * Общая таблица фиксов: монитор ФИКСОВ и витрина ОФЗ рисуют одни строки одной
 * таблицей. Проверяем то, из-за чего её выделяли: хвост колонок хоста (ΔYTM у
 * ОФЗ) рендерится и предлагается в меню столбцов, а настройка колонок каждой
 * витрины лежит под своим префиксом localStorage и не задевает соседнюю.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import FixedTable, { useFixedCols } from "./FixedTable.jsx";
import { FIXED_DEFAULT_COLS, OFZ_EXTRA_COLS } from "./fixedCols.jsx";
import ColumnsMenu from "../ColumnsMenu.jsx";

afterEach(cleanup);
beforeEach(() => localStorage.clear());

const ROWS = [
  { isin: "RU000A0JX0J2", name: "ОФЗ 26240", short_name: "ОФЗ 26240", cls: "ofz", is_ofz: true,
    ytm: 15.2, d_ytm_cmp: 12.4 },
  { isin: "RU000A0ZZZZ9", name: "ОФЗ 26238", short_name: "ОФЗ 26238", cls: "ofz", is_ofz: true,
    ytm: 14.9, d_ytm_cmp: -7 },
  { isin: "RU000A0YYYY1", name: "ОФЗ 26243", short_name: "ОФЗ 26243", cls: "ofz", is_ofz: true,
    ytm: 15.0, d_ytm_cmp: null },
];

// Хост, как его напишет OfzDesk: хук + таблица + меню столбцов.
function Host({ storageKey, extraCols, rows = ROWS }) {
  const cols = useFixedCols({ storageKey, extraCols });
  return (
    <>
      <ColumnsMenu visibleCols={cols.visibleCols} meta={cols.colsMeta}
        onToggle={cols.onToggleCol} onReset={cols.onResetCols} onMove={cols.onMoveCol} />
      <FixedTable rows={rows} sort={{ key: "ytm", dir: "asc" }} onSort={() => {}}
        onOpen={() => {}} extraCols={extraCols} {...cols} />
    </>
  );
}

const headers = (c) => [...c.querySelectorAll("thead th")].map((th) => th.textContent);

describe("хвост колонок хоста", () => {
  it("ΔYTM рисуется последней колонкой со знаком и цветом", () => {
    const { container } = render(<Host storageKey="t_ofz" extraCols={OFZ_EXTRA_COLS} />);
    const hs = headers(container);
    expect(hs[hs.length - 2]).toContain("ΔYTM");      // после неё только филлер
    const cells = [...container.querySelectorAll("td[class*='num']")]
      .filter((td) => td.textContent === "+12" || td.textContent === "-7" || td.textContent === "−7");
    expect(cells).toHaveLength(2);
    expect(cells[0].className).toContain("pos");
    expect(cells[1].className).toContain("neg");
    // без сравнения — прочерк, а не ноль
    const rowNull = [...container.querySelectorAll("tbody tr")][2];
    expect(rowNull.lastElementChild.previousElementSibling.textContent).toBe("—");
  });

  it("без extraCols набор — ровно FIXED_COLS (монитор ФИКСОВ)", () => {
    const { container } = render(<Host storageKey="t_fx" />);
    expect(headers(container).join("|")).not.toContain("ΔYTM");
    expect(JSON.parse(localStorage.getItem("cols_t_fx"))).toEqual(FIXED_DEFAULT_COLS);
  });

  it("меню столбцов предлагает колонку хоста", () => {
    const { container } = render(<Host storageKey="t_ofz" extraCols={OFZ_EXTRA_COLS} />);
    fireEvent.click(container.querySelector(".colmenu .chip-btn"));
    const item = [...container.querySelectorAll(".colmenu-item")]
      .find((el) => el.textContent.includes("ΔYTM"));
    expect(item).toBeTruthy();
    // снять галку — колонка уходит из таблицы и из сохранённого набора
    act(() => { fireEvent.click(item.querySelector("input[type=checkbox]")); });
    expect(headers(container).join("|")).not.toContain("ΔYTM");
    expect(JSON.parse(localStorage.getItem("cols_t_ofz"))).not.toContain("d_ytm_cmp");
  });
});

describe("префикс localStorage", () => {
  it("настройка ОФЗ не трогает ключи монитора фиксов", () => {
    const { container, unmount } = render(<Host storageKey="ofz" extraCols={OFZ_EXTRA_COLS} />);
    fireEvent.click(container.querySelector(".colmenu .chip-btn"));
    const item = [...container.querySelectorAll(".colmenu-item")]
      .find((el) => el.textContent.includes("ΔYTM"));
    act(() => { fireEvent.click(item.querySelector("input[type=checkbox]")); });
    expect(localStorage.getItem("cols_ofz")).toBeTruthy();
    expect(localStorage.getItem("cols_known_ofz")).toContain("d_ytm_cmp");
    expect(localStorage.getItem("colw_ofz")).toBe("{}");
    expect(localStorage.getItem("cols_fx")).toBeNull();
    expect(localStorage.getItem("cols_known_fx")).toBeNull();
    unmount();

    // монитор под своим префиксом стартует с дефолта, а не с набора ОФЗ
    const m = render(<Host storageKey="fx" />);
    expect(JSON.parse(localStorage.getItem("cols_fx"))).toEqual(FIXED_DEFAULT_COLS);
    expect(JSON.parse(localStorage.getItem("cols_known_fx"))).toEqual(FIXED_DEFAULT_COLS);
    expect(headers(m.container).length).toBe(FIXED_DEFAULT_COLS.length + 2);
  });

  it("сохранённый набор с устаревшими ключами мигрирует (g_spread_bid_bps → ytm_bid)", () => {
    localStorage.setItem("cols_fx", JSON.stringify(["name", "g_spread_bid_bps"]));
    localStorage.setItem("cols_known_fx", JSON.stringify(FIXED_DEFAULT_COLS));
    localStorage.setItem("colw_fx", JSON.stringify({ g_spread_bid_bps: 120 }));
    const { container } = render(<Host storageKey="fx" />);
    expect(JSON.parse(localStorage.getItem("cols_fx"))).toEqual(["name", "ytm_bid"]);
    expect(JSON.parse(localStorage.getItem("colw_fx"))).toEqual({ ytm_bid: 120 });
    expect(container.querySelector("colgroup col:nth-child(3)").style.width).toBe("120px");
  });
});

describe("срок бессрочной", () => {
  it("MATURITY у перпа — PERP, а не «00.00.0000» и не прочерк", () => {
    const rows = [
      { isin: "RU000A103SB8", name: "ВТБСУБ1-11", cls: "corp", maturity_date: null,
        perpetual: true, ytm: 16.44 },
      { isin: "RU000A0JX0J2", name: "ОФЗ 26240", cls: "ofz", maturity_date: "2036-07-30",
        perpetual: false, ytm: 15.2 },
    ];
    const { container } = render(<Host storageKey="t_perp" rows={rows} />);
    const mats = [...container.querySelectorAll("td.mat-cell .mat-main")].map((d) => d.textContent);
    expect(mats[0]).toBe("PERP");
    expect(mats[1]).toContain("30.07.2036");
  });
});

describe("купон, продлённый до погашения", () => {
  it("под сроком — дата, с которой ставка не объявлена; у оферты и обычной бумаги строки нет", () => {
    const rows = [
      { isin: "RU000A103Q24", name: "Сбер2СУБ3R", cls: "corp", maturity_date: "2031-11-14",
        put_date: null, coupon_assumed_from: "2026-11-20", ytm: 10.06 },
      { isin: "RU000A1064G3", name: "ГПБ001P26P", cls: "corp", maturity_date: "2033-04-18",
        put_date: "2026-10-21", coupon_assumed_from: null, ytm: 14.57 },
    ];
    const { container } = render(<Host storageKey="t_assumed" rows={rows} />);
    const cells = [...container.querySelectorAll("td.mat-cell")];
    const assumed = cells[0].querySelector(".mat-assumed");
    expect(assumed.textContent).toContain("20.11.2026");
    expect(assumed.getAttribute("title")).toContain("продлён до погашения");
    expect(cells[1].querySelector(".mat-assumed")).toBeNull();
    expect(cells[1].querySelector(".mat-offer").textContent).toContain("21.10.2026");
  });
});

describe("сторона стакана фикса", () => {
  it("под ценой BID — спред Y-IDX, ниже YTM к той же цене", () => {
    const rows = [{ isin: "RU000A10RZD1", name: "РЖД 1Р-05R", cls: "corp", bid: 99.62,
                    y_idx_bid_bps: 155, ytm_bid: 15.1 }];
    const { container } = render(<Host storageKey="t_side" rows={rows} />);
    const cell = container.querySelector("td.q-bid");
    expect(cell.querySelector(".q-px").textContent).toContain("99");
    expect(cell.querySelector(".q-sp").textContent).toContain("155");
    expect(cell.querySelector(".q-ytm").textContent).toContain("15");
  });
});
