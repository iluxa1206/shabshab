"""has_call: флаг call-опциона эмитента из corpbonds в реестре.

MOEX bondization вид оферты не отдаёт (offertype = 'Оферта' / 'Оферта
(состоялось)' / 'Оферта/Погашение' на всём универсе), поэтому маркер `c` в
таблице живёт только на этом флаге.
"""
import importlib
import os

import pytest

from services.enrich_corpbonds import parse_corpbonds_html


def _page(rows):
    """Минимальный HTML в форме corpbonds: строка таблицы «метка / p.val»."""
    trs = "".join(f'<tr><td>{k}</td><td><p class="val">{v}</p></td></tr>'
                  for k, v in rows)
    return f"<table>{trs}</table>"


_BASE = [("Формула купона", "Ключевая ставка + 2%"), ("Тип купона", "Флоатер")]


@pytest.mark.parametrize("extra,expected", [
    # на самом сайте метка написана с КИРИЛЛИЧЕСКОЙ 'с' — основной ключ
    ([("Наличие сall-опциона", "Да")], True),
    ([("Наличие call-опциона", "Да")], True),
    ([("Наличие call-опциона", " Да ")], True),
    ([("Наличие call-опциона", "Нет")], False),
    # строки на странице не было — «не знаем», НЕ «колла нет»
    ([], None),
])
def test_parse_has_call_tristate(extra, expected):
    assert parse_corpbonds_html(_page(_BASE + extra))["has_call"] is expected


@pytest.fixture
def reg(tmp_path, monkeypatch):
    """Реестр на изолированной БД."""
    monkeypatch.setenv("INSTRUMENTS_DB", str(tmp_path / "instruments.db"))
    from services import instruments_registry as _reg
    importlib.reload(_reg)
    _reg.DB_PATH = tmp_path / "instruments.db"
    _reg._initialized = False
    _reg._ensure()
    _reg.upsert({"isin": "RU000TEST001", "base": "KEYRATE", "margin_bps": 200,
                 "maturity_date": "2030-01-01"}, source="test")
    return _reg


def _has_call(reg, isin):
    return {r["isin"]: r["has_call"] for r in reg.universe_rows()}[isin]


def test_set_has_call_roundtrip(reg):
    assert _has_call(reg, "RU000TEST001") is None       # не знаем по умолчанию
    reg.set_has_call("RU000TEST001", True)
    assert _has_call(reg, "RU000TEST001") is True
    reg.set_has_call("RU000TEST001", False)
    assert _has_call(reg, "RU000TEST001") is False      # False именно пишется


def test_none_does_not_clobber(reg):
    reg.set_has_call("RU000TEST001", True)
    reg.set_has_call("RU000TEST001", None)              # «не знаем» не затирает знание
    assert _has_call(reg, "RU000TEST001") is True


def test_writes_through_manual_lock(reg):
    """manual_locked не гейтит флаг: в проде 544 строки заморожены импортом xlsx,
    иначе у них has_call остался бы NULL навсегда."""
    reg.set_manual("RU000TEST001", {"margin_bps": 200}, lock=True)
    reg.set_has_call("RU000TEST001", True)
    assert _has_call(reg, "RU000TEST001") is True


def test_list_call_unknown_excludes_known(reg):
    assert [r["isin"] for r in reg.list_call_unknown()] == ["RU000TEST001"]
    reg.set_has_call("RU000TEST001", False)
    assert reg.list_call_unknown() == []


def test_catalog_exposes_flag(reg):
    reg.set_has_call("RU000TEST001", True)
    row = next(r for r in reg.list_catalog() if r["isin"] == "RU000TEST001")
    assert row["has_call"] == 1


def test_xlsx_template_excludes_has_call():
    """Флаг НЕ в шаблоне импорта: round-trip экспорт→импорт зовёт set_manual(lock)
    и заморозил бы строку (см. scripts/unfreeze_fixing_spec.py)."""
    from api.routes.instruments import _XLSX_COLS, _XLSX_EDITABLE
    assert "has_call" not in _XLSX_COLS
    assert "has_call" not in _XLSX_EDITABLE
