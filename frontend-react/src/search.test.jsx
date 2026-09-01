/**
 * Поиск по таблице монитора: токены, гомоглифы и ЧУЖАЯ РАСКЛАДКА.
 *
 * Имя выпуска набирают на бегу, между двумя окнами терминала, и запрос
 * регулярно приезжает не в той раскладке: «Ufpgy» вместо «Газпн». Символ в
 * символ это ровно то, что человек хотел набрать, — пустая таблица здесь
 * враньё.
 *
 * Таблицы раскладки и правила отбора обязаны совпадать с бэком
 * (services/text_search.py): иначе таблица монитора и пикер бумаг в СИГНАЛАХ
 * понимали бы один и тот же ввод по-разному.
 */
import { describe, it, expect } from "vitest";
import { filterBonds, filterByText, queryVariants, swapLayout,
         tokenize } from "./search.js";

const ROWS = [
  { isin: "RU000A109B33", short_name: "Газпн3P13R", emitter_name: "Газпром капитал" },
  { isin: "RU000A10AU99", short_name: "РЖД 1Р-52R", emitter_name: "РЖД" },
  { isin: "SU29014RMFS6", short_name: "ОФЗ 29014", emitter_name: "Минфин России" },
];

const names = (rows) => rows.map((b) => b.short_name);

describe("swapLayout — тот же набор в другой раскладке", () => {
  it("работает в обе стороны и посимвольно", () => {
    expect(swapLayout("Ufpgy")).toBe("газпн");
    expect(swapLayout("Газпн")).toBe("ufpgy");
    // цифры и знаки вне карты остаются собой
    expect(swapLayout("RU000A109B33")).toBe("кг000ф109и33");
  });
});

describe("queryVariants — набранное первым, догадка следом", () => {
  it("человек чаще всего набрал верно — догадка его не опережает", () => {
    expect(queryVariants("Ufpgy")[0]).toBe("Ufpgy");
    expect(queryVariants("Ufpgy")).toContain("газпн");
  });

  it("пунктуационный мусор отбрасывается: «РЖД» → «h;l»", () => {
    expect(queryVariants("РЖД")).toEqual(["РЖД"]);
  });

  it("пустой запрос — пусто, фильтровать нечем", () => {
    expect(queryVariants("   ")).toEqual([]);
  });
});

describe("filterBonds — отбор с запасной раскладкой", () => {
  it("находит по имени, набранному чужой раскладкой", () => {
    expect(names(filterBonds(ROWS, "Ufpgy"))).toEqual(["Газпн3P13R"]);
  });

  it("находит ISIN, набранный по-русски", () => {
    expect(names(filterBonds(ROWS, "кг000ф109и33"))).toEqual(["Газпн3P13R"]);
  });

  it("верный запрос НЕ разбавляется догадкой: побеждает первый вариант", () => {
    // «ржд» находится как есть; вариант чужой раскладки к выдаче не примешивается
    expect(names(filterBonds(ROWS, "ржд"))).toEqual(["РЖД 1Р-52R"]);
  });

  it("пустой запрос отдаёт рынок целиком, ненайденное — честно пусто", () => {
    expect(filterBonds(ROWS, "")).toHaveLength(3);
    expect(filterBonds(ROWS, "зюзюка")).toEqual([]);
  });

  it("прежние правила живы: токены, гомоглифы, поиск по эмитенту", () => {
    expect(names(filterBonds(ROWS, "ржд 52"))).toEqual(["РЖД 1Р-52R"]);
    expect(names(filterBonds(ROWS, "газпром"))).toEqual(["Газпн3P13R"]);
    // латинская «P» в тикере читается как кириллическая «р»
    expect(names(filterBonds(ROWS, "3р13"))).toEqual(["Газпн3P13R"]);
    expect(tokenize("ржд-2р3")).toEqual(["ржд", "2", "р", "3"]);
  });
});

describe("filterByText — простые списки (эмитенты, Справочник)", () => {
  const ISSUERS = [{ name: "Газпром капитал" }, { name: "РЖД" },
                   { name: "Минфин России" }];

  it("понимает чужую раскладку", () => {
    expect(filterByText(ISSUERS, "Ufpghjv", (i) => i.name))
      .toEqual([{ name: "Газпром капитал" }]);
  });

  it("регистр и латинские двойники не мешают", () => {
    expect(filterByText(ISSUERS, "ГАЗПРОМ", (i) => i.name)).toHaveLength(1);
    expect(filterByText(ISSUERS, "PЖД", (i) => i.name)).toEqual([{ name: "РЖД" }]);
  });

  it("пустой запрос отдаёт список целиком", () => {
    expect(filterByText(ISSUERS, "", (i) => i.name)).toHaveLength(3);
  });
});
