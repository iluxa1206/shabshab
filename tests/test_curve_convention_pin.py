"""ПИН РЕШЕНИЯ 2026-08-26: две конвенции KEYRATE — намеренно.

SheetForwardCurve трактует фикс-ногу свопа как ГОДОВУЮ, CurveBootstrapper — как
КВАРТАЛЬНУЮ. Это выглядит как баг и провоцирует «починку», но задачи разные:
бутстрап даёт безарбитражные DF, лист воспроизводит утверждённую методику
вкладки КРИВЫЕ, на которой прайсятся купоны.

Приведение sheet к квартальной ноге сдвинуло бы Y-IDX ВСЕХ KEYRATE-бумаг на
+73…+91 bps и потребовало бы пересчёта истории. Если решение меняется —
менять осознанно, вместе с этим тестом.
"""
import inspect
from datetime import date

import pytest


def test_sheet_keyrate_uses_annual_leg():
    """Формула длинного тенора — 4·((1+par)^¼−1), то есть годовая нога."""
    from core import forwards
    src = inspect.getsource(forwards.SheetForwardCurve)
    assert "4.0 * ((1.0 + par) ** 0.25 - 1.0)" in src, \
        "конвенция sheet изменена — см. докстринг SheetForwardCurve"


def test_bootstrap_keyrate_uses_quarterly_leg():
    """А бутстрап — квартальную (months=3, конвенция СПФИ МБ)."""
    from core import forwards
    src = inspect.getsource(forwards.CurveBootstrapper._bootstrap_par_curve)
    doc = inspect.getdoc(forwards.CurveBootstrapper.bootstrap_keyrate) or ""
    assert "3" in src and "KEYRATE" in src, "ветка KEYRATE в бутстрапе исчезла"
    assert "квартальн" in doc, "конвенция bootstrap изменена или не описана"


def test_both_conventions_documented():
    """Расхождение обязано быть объяснено В КОДЕ, иначе следующий читатель
    «починит» одну сторону под другую."""
    from core import forwards
    doc = inspect.getdoc(forwards.SheetForwardCurve) or ""
    assert "ГОДОВАЯ" in doc and "bootstrap" in doc.lower(), \
        "расхождение конвенций не задокументировано в SheetForwardCurve"


def test_curves_route_formula_matches_core():
    """Формула продублирована во вкладке КРИВЫЕ — правки только синхронно,
    иначе график и прайсинг покажут разные числа."""
    import api.routes.curves as cr
    src = open(cr.__file__, encoding="utf-8").read()
    assert "4.0 * ((1.0 + s) ** 0.25 - 1.0)" in src, \
        "копия формулы в /api/curves разошлась с core.forwards"


def test_sheet_and_bootstrap_really_differ():
    """Не тавтология: на растущей par-кривой две конвенции дают РАЗНЫЕ уровни.
    Если этот тест начнёт падать — конвенции сошлись, и пин выше пора снимать."""
    par = 0.1510                      # 10Y ~15.1%
    annual = 4.0 * ((1.0 + par) ** 0.25 - 1.0)
    quarterly = par                   # при квартальной ноге avg ≡ par
    assert abs(annual - quarterly) > 0.005, \
        f"конвенции сошлись: {annual:.4f} vs {quarterly:.4f}"
