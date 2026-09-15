import { describe, expect, it } from "vitest";
import { fixedCurrency, isDefaultFixedCurrencySelection } from "./currency.js";

describe("валюта номинала фиксов", () => {
  it("сводит старые рублёвые и CNH-коды к единому фильтру", () => {
    expect(fixedCurrency({ face_unit: "SUR" })).toBe("RUB");
    expect(fixedCurrency({ faceunit: "cnh" })).toBe("CNY");
  });

  it("считает активным только отклонение от стартового RUB-отбора", () => {
    expect(isDefaultFixedCurrencySelection(["RUB"])).toBe(true);
    expect(isDefaultFixedCurrencySelection(["USD"])).toBe(false);
    expect(isDefaultFixedCurrencySelection([])).toBe(false);
  });
});
