"""Спека фиксинга не должна зависеть от одного bondsearch-xlsx.

Регресс 26.08.2026: xlsx лежит вне git и вне тома данных (приезжает только
rsync'ом деплоя). Когда его не оказывалось, params() не отдавал coupon_text,
parse_prospectus_formula не звался, coupon_mode оставался None — и
period_index_pct молча уходил на легаси форвард-проекцию кривой. Один и тот же
выпуск по одной и той же цене того же дня показывал разные метрики: ВЭБ2Р-50
при 100.35% давал 191 bps R-spread со спекой и 182 bps без неё.
"""
import pytest

from services import ref_data
from services import coupon_calib


# текст проспекта ВЭБ2Р-50 (compounded index-ratio, лаг 7 календарных дней)
VEB_TEXT = (
    "1-8 купоны: Rj = (max (((Index Endj-7/Index Startj-7) - 1) ; 0) * B/Tj * 100%) + S, "
    "где: IndexStart j-7 - значение индекса RUONIA для 7-го календарного дня, "
    "предшествующего Start j. IndexEnd j-7 - значение индекса RUONIA для 7-го "
    "календарного дня, предшествующего End j. S - спред, в процентах годовых. S=2.3%"
)
ISIN = "RU000A10B8D9"


@pytest.fixture
def no_xlsx(monkeypatch):
    """bondsearch-выгрузки нет; реестр знает текст формулы."""
    monkeypatch.setattr(ref_data, "_cbonds_cache", {})
    monkeypatch.setattr(ref_data, "load_manual", lambda: {})
    monkeypatch.setattr(ref_data, "_registry_overrides", lambda: {})
    monkeypatch.setattr(ref_data, "_registry_fallback",
                        lambda: {ISIN: {"coupon_text": VEB_TEXT}})


def test_params_берёт_coupon_text_из_реестра_без_xlsx(no_xlsx):
    assert ref_data.params(ISIN).get("coupon_text") == VEB_TEXT


def test_спека_фиксинга_резолвится_без_xlsx(no_xlsx):
    f = ref_data.coupon_formula(ISIN)
    assert f["coupon_mode"] == "average", "режим потерян → прайсинг съедет на форвард"
    assert f["fixing_lag"] == 7
    assert f["fixing_lag_unit"] == "cal"
    assert f["compounded"] == 1, "без капитализации купон занижается на 0.3-0.5 пп"


def test_свой_coupon_text_не_затирается_реестровым(monkeypatch):
    monkeypatch.setattr(ref_data, "_cbonds_cache", {ISIN: {"coupon_text": "из xlsx"}})
    monkeypatch.setattr(ref_data, "load_manual", lambda: {})
    monkeypatch.setattr(ref_data, "_registry_overrides", lambda: {})
    monkeypatch.setattr(ref_data, "_registry_fallback",
                        lambda: {ISIN: {"coupon_text": VEB_TEXT}})
    assert ref_data.params(ISIN)["coupon_text"] == "из xlsx"


def test_var_type_из_реестра_сохраняет_обрезку_по_оферте(monkeypatch):
    """Без var_type поток считается к погашению вместо оферты — спред падает вдвое."""
    monkeypatch.setattr(ref_data, "_cbonds_cache", {})
    monkeypatch.setattr(ref_data, "load_manual", lambda: {})
    monkeypatch.setattr(ref_data, "_registry_overrides", lambda: {})
    monkeypatch.setattr(ref_data, "_registry_fallback",
                        lambda: {ISIN: {"var_type": "Определяется решением эмитента"}})
    assert ref_data.cut_at_offer(ISIN) is True


def test_битый_xlsx_не_роняет_params(monkeypatch, tmp_path):
    """Нечитаемый файл → пустая выгрузка + лог, а не исключение наверх."""
    bad = tmp_path / "bondsearch_01_01_2026.xlsx"
    bad.write_bytes(b"not a zip")
    monkeypatch.setattr(ref_data, "_cbonds_cache", None)
    assert ref_data.load_cbonds(str(bad)) == {}


def test_потеря_спеки_логируется(monkeypatch, caplog):
    """Тихая деградация запрещена: без спеки у флоатера обязан быть warning."""
    monkeypatch.setattr(coupon_calib, "_SPEC_LOST_SEEN", set())
    monkeypatch.setattr(ref_data, "coupon_formula",
                        lambda *a, **k: {"coupon_mode": None, "avg_window_days": None})
    import datetime
    with caplog.at_level("WARNING"):
        coupon_calib.period_index_pct(
            ISIN, "RUONIA", [], 1000.0,
            datetime.date(2026, 7, 7), datetime.date(2026, 10, 6),
            datetime.date(2026, 8, 26), lambda d: 14.0)
    assert any("спека фиксинга потеряна" in r.getMessage() for r in caplog.records)
