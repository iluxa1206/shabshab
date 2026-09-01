/**
 * Меню типов бумаг: выключенный слой не должен смотреть пустой витриной.
 *
 * Слои гасятся переменной окружения на бэке (services/feature_flags) и
 * приезжают во фронт в /api/meta.features. Флага нет — слой считается
 * включённым, иначе старый бэкенд прятал бы рабочие вкладки.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Topbar from "./Topbar.jsx";

const USER = { email: "t@test", role: "user" };

// DOM между тестами не убирается сам — иначе следующий рендер видит меню
// предыдущего и находит «Фиксы» там, где их уже не рисуют
afterEach(cleanup);

function show(features) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/floaters"]}>
        <Topbar user={USER} onLogout={() => {}} onOpenSettings={() => {}} features={features} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("вкладка типа бумаг", () => {
  it("слой фиксов включён — пункт на месте", () => {
    show({ fixed: true });
    expect(screen.getByText("Фиксы")).toBeTruthy();
  });

  it("слой выключен — пункта нет", () => {
    show({ fixed: false });
    expect(screen.queryByText("Фиксы")).toBeNull();
    expect(screen.getByText("Флоатеры")).toBeTruthy();
  });

  it("старый бэк без флагов — вкладка видна", () => {
    show(undefined);
    expect(screen.getByText("Фиксы")).toBeTruthy();
  });
});
