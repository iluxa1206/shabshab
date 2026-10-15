/**
 * Меню: «Рубли» (флоатеры и фиксы — общий монитор) и «Валютные».
 *
 * Слой фиксов гасится переменной окружения на бэке (services/feature_flags) и
 * приезжает во фронт в /api/meta.features. Выключенный слой не должен смотреть
 * пустой витриной: вкладки ОФЗ и Аукцион исчезают. Флага нет — слой считается
 * включённым, иначе старый бэкенд прятал бы рабочие вкладки.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Topbar from "./Topbar.jsx";

const USER = { email: "t@test", role: "user" };

// DOM между тестами не убирается сам
afterEach(cleanup);

function show(features, path = "/bonds") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Topbar user={USER} onLogout={() => {}} onOpenSettings={() => {}} features={features} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("раздел «Облигации»", () => {
  it("отдельных пунктов «Флоатеры» и «Фиксы» больше нет", () => {
    show({ fixed: true });
    expect(screen.queryByText("Фиксы")).toBeNull();
    expect(screen.queryByText("Флоатеры")).toBeNull();
    expect(screen.getAllByText("Рубли").length).toBeGreaterThan(0);
    expect(screen.queryByText(/^Облигации/)).toBeNull();
  });

  it("слой фиксов включён — ОФЗ и Аукцион в подменю", () => {
    show({ fixed: true });
    expect(screen.getByText("ОФЗ")).toBeTruthy();
    expect(screen.getByText("Аукцион")).toBeTruthy();
  });

  it("слой выключен — вкладок фиксов нет, остальное на месте", () => {
    show({ fixed: false });
    expect(screen.queryByText("ОФЗ")).toBeNull();
    expect(screen.queryByText("Аукцион")).toBeNull();
    expect(screen.getByText("Монитор")).toBeTruthy();
    expect(screen.getByText("Сделки")).toBeTruthy();
  });

  it("старый бэк без флагов — вкладки видны", () => {
    show(undefined);
    expect(screen.getByText("ОФЗ")).toBeTruthy();
  });
});

describe("раздел «Валютные»", () => {
  it("в выпадающем меню отдельным пунктом", () => {
    show({ fixed: true });
    expect(screen.getByText("Валютные")).toBeTruthy();
  });

  it("слой выключен — пункта «Валютные» нет", () => {
    show({ fixed: false });
    expect(screen.queryByText("Валютные")).toBeNull();
  });
});

describe("подменю по адресу", () => {
  it.each(["/bonds", "/floaters", "/fixed", "/fixed/ofz", "/calc", "/calc/float", "/primary"])(
    "%s — подменю облигаций", (path) => {
      show({ fixed: true }, path);
      expect(screen.getByText("Сделки")).toBeTruthy();
    });

  it("портфель — своё меню без подменю облигаций", () => {
    show({ fixed: true }, "/portfolio");
    expect(screen.queryByText("Сделки")).toBeNull();
  });
});
