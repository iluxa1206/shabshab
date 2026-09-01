#!/usr/bin/env python3
"""Утренний прогон по свежим выпускам: что нашли и чего не хватает к торгам.

Идёт по ISIN моложе reg.NEW_ISSUE_DAYS и печатает таблицу «параметры есть /
нет», разбитую по источнику, из которого их можно добрать:

  bondsearch  — выпуск ЕСТЬ в свежей выгрузке, но в реестре пусто → достаточно
                прогнать синк (или положить свежий bondsearch_DD_MM_YYYY.xlsx);
  нет нигде   — ни выгрузка, ни MOEX, ни corpbonds его не знают → руками в
                СПРАВОЧНИКЕ (формула из проспекта на сайте эмитента).

--apply дополнительно заливает в реестр то, что нашлось в выгрузке (source=
cbonds, БЕЗ ручного lock — ночной синк потом уточнит из авторитетных источников).

Сеть не трогает: читает реестр + локальную выгрузку. Для полноценного добора
(MOEX/corpbonds) есть ночной services.instruments_sync.sync_instruments.
"""
import sys

sys.path.insert(0, ".")
from services import instruments_registry as reg, ref_data

APPLY = "--apply" in sys.argv


def main():
    cb = ref_data.load_cbonds()
    rows = reg.list_new_issues()
    if not rows:
        print(f"Свежих выпусков (моложе {reg.NEW_ISSUE_DAYS} дн) нет")
        return
    filled = 0
    print(f"Свежие выпуски (моложе {reg.NEW_ISSUE_DAYS} дн): {len(rows)}\n")
    print(f"{'ISIN':14} {'имя':14} {'размещ.':11} {'база':8} {'маржа':>6}  источник")
    for r in rows:
        c = cb.get(r["isin"]) or {}
        base = r["base"] or (c.get("base") and f"←{c['base']}") or "—"
        margin = r["margin_bps"] if r["margin_bps"] is not None else (
            f"←{c['margin_bps']}" if c.get("margin_bps") is not None else "—")
        if r["priceable"]:
            src = "ok"
        elif c.get("base") and c.get("margin_bps") is not None:
            src = "bondsearch"
        else:
            src = "НЕТ НИГДЕ — руками"
        print(f"{r['isin']:14} {(r['short_name'] or '')[:14]:14} "
              f"{(r['issue_date'] or '')[:10]:11} {str(base):8} {str(margin):>6}  {src}")
        if APPLY and src == "bondsearch":
            reg.upsert({"isin": r["isin"], "base": c["base"], "margin_bps": c["margin_bps"],
                        "day_count": c.get("day_count"), "coupon_text": c.get("coupon_text"),
                        "maturity_date": c.get("maturity_date"),
                        "issue_date": c.get("issue_date"),
                        "face_value": c.get("face_value"),
                        "var_type": c.get("var_type"),
                        # рейтинг из выгрузки — только в пропуск: это снимок на
                        # дату файла, живой драйн (set_rating) им перебивать нельзя
                        "rating": None if (reg.get(r["isin"]) or {}).get("rating")
                        else c.get("rating")},
                       source="cbonds", mark_new=False)
            filled += 1
    blind = [r for r in rows if not r["priceable"]]
    print(f"\nбез параметров: {len(blind)}" + (f", залито из выгрузки: {filled}" if APPLY else ""))
    if blind and not APPLY:
        print("подсказка: --apply зальёт то, что есть в bondsearch-выгрузке")


if __name__ == "__main__":
    main()
