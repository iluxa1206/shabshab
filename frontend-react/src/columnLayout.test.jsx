import { describe, it, expect } from "vitest";
import { mergeColumnLayout } from "./columnLayout.js";

const D = ["date", "name", "price", "yidx", "dev7", "devpx", "yld", "act"];

describe("mergeColumnLayout", () => {
  it("пусто/мусор → набор по умолчанию", () => {
    expect(mergeColumnLayout(null, D)).toEqual(D);
    expect(mergeColumnLayout([], D)).toEqual(D);
    expect(mergeColumnLayout(["zzz"], D)).toEqual(D);
    expect(mergeColumnLayout("nope", D)).toEqual(D);
  });

  it("новая колонка встаёт за соседом из порядка по умолчанию, порядок юзера цел", () => {
    const saved = ["name", "date", "price", "yidx", "yld", "act"];
    expect(mergeColumnLayout(saved, D)).toEqual(["name", "date", "price", "yidx", "dev7", "devpx", "yld", "act"]);
  });

  it("сосед скрыт — за ближайшим присутствующим слева; снесённые ключи выпадают", () => {
    // name скрыт пользователем (есть в known), old — снесённый ключ
    const saved = ["date", "old", "price", "yld", "act"];
    const known = ["date", "name", "price", "yld", "act"];
    expect(mergeColumnLayout(saved, D, { known }))
      .toEqual(["date", "price", "yidx", "dev7", "devpx", "yld", "act"]);
  });

  it("tail: раскладка старого правила «в конец» чинится, кнопки снова последние", () => {
    const legacy = ["date", "name", "price", "yidx", "yld", "act", "dev7"];
    expect(mergeColumnLayout(legacy, D, { tail: "act" }))
      .toEqual(["date", "name", "price", "yidx", "yld", "dev7", "devpx", "act"]);
  });

  it("known: колонка, скрытая пользователем, не возвращается; незнакомая — показывается", () => {
    const saved = ["date", "price", "act"];
    const known = ["date", "name", "price", "yidx", "act"];   // yld/dev7/devpx появились позже
    expect(mergeColumnLayout(saved, D, { known }))
      .toEqual(["date", "price", "dev7", "devpx", "yld", "act"]);
  });

  it("valid: включённые сверх дефолта колонки сохраняются", () => {
    const saved = ["date", "extra", "act"];
    expect(mergeColumnLayout(saved, ["date", "act"], { valid: ["date", "act", "extra"] }))
      .toEqual(["date", "extra", "act"]);
  });
});
