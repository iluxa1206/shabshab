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

function show(features, path = "/floaters") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
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

// «Первичка» — общий путь двух разделов: анонс не знает своего класса. Тип на
// нём не выводится из URL, иначе переход из Фиксов ронял бы меню в Флоатеры.
describe("общий путь /primary", () => {
  afterEach(() => sessionStorage.clear());

  it("из Фиксов остаётся в Фиксах", () => {
    show({ fixed: true }, "/fixed");     // раздел запомнился
    cleanup();
    show({ fixed: true }, "/primary");
    expect(screen.queryByText("Сделки")).toBeNull();   // подменю флоатеров не всплыло
    expect(screen.getByText("Первичка")).toBeTruthy();
  });

  it("из Флоатеров остаётся во Флоатерах", () => {
    show({ fixed: true }, "/trades");
    cleanup();
    show({ fixed: true }, "/primary");
    expect(screen.getByText("Сделки")).toBeTruthy();
  });

  it("холодный вход прямо на /primary — дефолт Флоатеры", () => {
    show({ fixed: true }, "/primary");
    expect(screen.getByText("Сделки")).toBeTruthy();
  });
});
