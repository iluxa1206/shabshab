// Смоук аналитики: сборка НЕ ловит ошибки рантайма (обращение к удалённой
// функции падает белым экраном уже в браузере — так ушёл padFull 06.09.2026).
// Панели монтируются на живых данных в обоих режимах свитчера.
import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import AnalyticsPanel from "./AnalyticsPanel.jsx";
import FixedAnalytics from "./FixedAnalytics.jsx";
import CashflowChart from "./CashflowChart.jsx";

const mk = (i) => ({
  isin: "RU00000000" + i, short_name: "ТЕСТ " + i, name: "ТЕСТ " + i,
  issuer: i % 2 ? "Альфа" : "Бета", emitter_name: i % 2 ? "Альфа" : "Бета", rating: i % 3 ? "AA" : "BBB",
  maturity_date: "2028-01-1" + (i % 9), yield_over_index_bps: 100 + i * 7,
  y_idx_wap_bps: 100 + i * 7, g_spread_wap_bps: 100 + i * 7,
  cls: i % 4 ? "corp" : "ofz",
});
const rows = Array.from({ length: 12 }, (_, i) => mk(i + 1));

describe("smoke аналитики: панели монтируются в обоих режимах свитчера", () => {
  it("панель флоатеров рисуется и переживает свитчер ЭМИТЕНТ", async () => {
    render(<AnalyticsPanel rows={rows} onFocus={() => {}} />);
    fireEvent.click(screen.getAllByText("Эмитент")[0]);
    expect(screen.getAllByText(/Альфа/).length).toBeGreaterThan(0);
  });
  it("разброс переключается на распределение", () => {
    const view = render(<AnalyticsPanel rows={rows} onFocus={() => {}} />);
    const button = within(view.container).getByText("Распределение");
    fireEvent.click(button);
    expect(button.getAttribute("aria-pressed")).toBe("true");
  });
  it("панель фиксов рисуется и переживает свитчер ЭМИТЕНТ", () => {
    render(<FixedAnalytics rows={rows} />);
    fireEvent.click(screen.getAllByText("Эмитент")[0]);
    expect(screen.getAllByText(/Альфа/).length).toBeGreaterThan(0);
  });
  it("график купонов рисуется", () => {
    render(<CashflowChart today="2026-09-06" items={[
      { payment_date: "2026-06-20", amount_rub: 114.36, coupon_rate_pct: 22.93 },
      { payment_date: "2026-12-19", amount_rub: 97.6, coupon_rate_pct: 19.57 },
    ]} />);
    expect(document.querySelector(".cf-box")).toBeTruthy();
  });
});
