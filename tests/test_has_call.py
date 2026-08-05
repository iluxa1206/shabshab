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


# ── guard fetch_corpbonds и защита ветки EXOTIC ──────────────────────────────

class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


class _Client:
    """Отдаёт заранее заданный HTML вместо похода в сеть."""
    def __init__(self, resp):
        self._resp = resp

    async def get(self, url, **kw):
        return self._resp


def _bond_page(isin, rows):
    trs = "".join(f'<tr><td>{k}</td><td><p class="val">{v}</p></td></tr>'
                  for k, v in rows)
    return f"<html><table>{trs}</table></html>"


@pytest.mark.asyncio
async def test_guard_accepts_page_without_formula():
    """Карточка без «Формула купона» (corpbonds считает бумагу фиксом) больше не
    отбраковывается — has_call с неё нужен."""
    from services.enrich_corpbonds import fetch_corpbonds
    isin = "RU000A108LD8"
    html = _bond_page(isin, [("ISIN", isin), ("Тип купона", "Фикс"),
                             ("Дата погашения", "24.05.2034"),
                             ("Наличие сall-опциона", "Да")])
    r = await fetch_corpbonds(isin, client=_Client(_Resp(html)))
    assert r is not None and r["has_call"] is True and r.get("base") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("html,status", [
    ("<html>404 не найдено</html>", 200),          # не карточка облигации
    ("<html><td>Дата погашения</td></html>", 200),  # структура есть, ISIN чужой
    ("<html>что угодно</html>", 500),               # ошибка сервера
])
async def test_guard_rejects_non_bond(html, status):
    from services.enrich_corpbonds import fetch_corpbonds
    assert await fetch_corpbonds("RU000A108LD8",
                                 client=_Client(_Resp(html, status))) is None


@pytest.mark.asyncio
async def test_no_formula_does_not_mark_exotic(reg, monkeypatch):
    """Ослабленный guard пропускает карточки без формулы. Судить по ним об
    экзотике нельзя: иначе «Тип купона: Флоатер» + пустая формула выкидывали бы
    бумагу из универса (base='EXOTIC')."""
    import services.enrich_corpbonds as ec

    async def fake(isin, client=None):
        return {"has_call": True, "is_floater": True, "base": None,
                "margin_bps": None, "formula_text": None}
    monkeypatch.setattr(ec, "fetch_corpbonds", fake)

    res = await ec.enrich_registry(["RU000TEST001"], apply=True, delay=0)
    assert res["stats"]["exotic"] == 0
    row = next(r for r in reg.list_catalog() if r["isin"] == "RU000TEST001")
    assert row["base"] == "KEYRATE"      # из универса не выкинули
    assert row["has_call"] == 1          # флаг всё равно снят
    assert reg.enrich_info("RU000TEST001")["result"] == "nodata"


@pytest.mark.asyncio
async def test_formula_still_marks_exotic(reg, monkeypatch):
    """Старое поведение сохранено: формула ЕСТЬ, база из неё не выводится → EXOTIC."""
    import services.enrich_corpbonds as ec

    async def fake(isin, client=None):
        return {"has_call": None, "is_floater": True, "base": None,
                "margin_bps": None, "formula_text": "ИПЦ + 3%"}
    monkeypatch.setattr(ec, "fetch_corpbonds", fake)

    res = await ec.enrich_registry(["RU000TEST001"], apply=True, delay=0)
    assert res["stats"]["exotic"] == 1
    row = next(r for r in reg.list_catalog() if r["isin"] == "RU000TEST001")
    assert row["base"] == "EXOTIC"
