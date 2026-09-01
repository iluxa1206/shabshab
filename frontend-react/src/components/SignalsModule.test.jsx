/**
 * Smoke-проверка МОНТИРОВАНИЯ вкладки СИГНАЛЫ.
 *
 * Сборка молчит про ошибки рантайма (мёртвая зона const, обращение к полю
 * undefined в разметке), а форма фильтра — самое густое место вкладки: два
 * вида сигналов, два столбца селекторов и режимы повтора. Здесь проверяется,
 * что обе формы рисуются и что поля отбора стоят парами «наблюдать» /
 * «исключить».
 *
 * Сеть глушим на уровне fetch, как в App.test.jsx: так тест проверяет
 * настоящий клиентский слой и не переписывается на каждый новый вызов.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import SignalsModule from "./SignalsModule.jsx";

const EMPTY = { filters: [], events: [], unseen: 0, targets: [] };

function mountModule() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}><SignalsModule /></QueryClientProvider>);
}

/** Обе формы прячутся за «+ Новый сигнал» — раскрываем их, как это делает
 *  человек: одна кнопка в колонке стакана, вторая в колонке сделок. */
async function openForms() {
  mountModule();
  const buttons = await screen.findAllByText(/Новый сигнал/);
  for (const b of buttons) fireEvent.click(b);
  await waitFor(() => expect(screen.getAllByText("Название").length).toBe(2));
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200, json: async () => EMPTY, text: async () => "{}",
  })));
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("вкладка СИГНАЛЫ", () => {
  it("монтируется и рисует обе формы", async () => {
    await openForms();
    // форма стакана и форма крупных сделок — рядом; диапазон спреда есть у
    // обеих, а порог объёма сделки только у второй
    expect(screen.getAllByText("Диапазон R-spread, бп")).toHaveLength(2);
    expect(screen.getByText("Объём сделки от, млн ₽")).toBeTruthy();
    expect(screen.getByText("Сторона стакана")).toBeTruthy();
  });

  it("селекторы отбора идут парами: наблюдать и исключить", async () => {
    await openForms();
    // по одной паре на форму — стакан и сделки
    expect(screen.getAllByText("Эмитенты")).toHaveLength(2);
    expect(screen.getAllByText("Кроме эмитентов")).toHaveLength(2);
    expect(screen.getAllByText("Отдельные бумаги")).toHaveLength(2);
    expect(screen.getAllByText("Кроме бумаг")).toHaveLength(2);
  });

  it("режим планки включён по умолчанию и прячет пороги повтора", async () => {
    await openForms();
    const box = screen.getByLabelText(/только при улучшении/);
    expect(box.checked).toBe(true);
    // пока держится планка, «сдвиг в %» и повтор по объёму не при чём
    expect(screen.queryByText(/и при изменении объёма/)).toBeNull();
    expect(screen.getByText(/Прятать в стакане заявки мельче/)).toBeTruthy();
  });
});
