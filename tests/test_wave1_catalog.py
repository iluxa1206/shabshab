"""Регресс-щит волны 1 аудита 2026-08-26: разморозка Справочника.

Ловим три дефекта round-trip xlsx: заморозку неизменённых строк, обход
валидации диапазонов и потерю EXOTIC-строк."""
import pytest


# ─── diff-фильтр: неизменённая строка не трогается ──────────────────────────

def test_same_as_registry_numbers():
    """int vs float из xlsx — одно значение, а не «правка»."""
    from api.routes.instruments import _same_as_registry
    cur = {"margin_bps": 200, "face_value": 1000.0, "short_name": "ВЭБ 1Р-1"}
    assert _same_as_registry(cur, "margin_bps", 200.0) is True
    assert _same_as_registry(cur, "margin_bps", 200) is True
    assert _same_as_registry(cur, "face_value", 1000) is True
    assert _same_as_registry(cur, "margin_bps", 250) is False


def test_same_as_registry_strings_and_none():
    from api.routes.instruments import _same_as_registry
    cur = {"short_name": " ВЭБ 1Р-1 ", "coupon_mode": None}
    assert _same_as_registry(cur, "short_name", "ВЭБ 1Р-1") is True
    # пропуск в реестре: заполнение — это ПРАВКА, а не совпадение
    assert _same_as_registry(cur, "coupon_mode", "average") is False


# ─── валидация диапазонов, которую xlsx-путь раньше обходил ─────────────────

def test_validate_rejects_percent_instead_of_bps():
    """Ячейка «2,5» вместо 250 bps писалась как 2 bps и лочилась."""
    from api.routes.instruments import _validate_ranges
    _validate_ranges({"margin_bps": 250})            # норма
    with pytest.raises(ValueError, match="базисные пункты"):
        _validate_ranges({"margin_bps": 999999})


def test_validate_rejects_past_maturity():
    """maturity_date в прошлом → retire_matured убьёт бумагу следующим синком."""
    from api.routes.instruments import _validate_ranges
    with pytest.raises(ValueError, match="деактивирована"):
        _validate_ranges({"maturity_date": "2020-01-01"})


def test_validate_rejects_out_of_range_fields():
    from api.routes.instruments import _validate_ranges
    with pytest.raises(ValueError):
        _validate_ranges({"face_value": 0})           # gt=0
    with pytest.raises(ValueError):
        _validate_ranges({"coupon_period_days": 5000})  # le=1830
    with pytest.raises(ValueError):
        _validate_ranges({"cap_pct": 500})            # le=100


def test_validate_accepts_normal_row():
    from api.routes.instruments import _validate_ranges
    _validate_ranges({"base": "KEYRATE", "margin_bps": 200, "face_value": 1000.0,
                      "coupon_period_days": 91, "coupons_per_year": 4,
                      "maturity_date": "2035-01-01", "cap_pct": 25.0})


# ─── EXOTIC больше не теряется на round-trip ────────────────────────────────

def test_exotic_is_accepted_base():
    """Экспорт пишет base=EXOTIC (51 строка), импорт отбрасывал строку целиком
    вместе с правками cap/floor, а errors[:50] прятал хвост."""
    import inspect
    from api.routes import instruments as mod
    src = inspect.getsource(mod.catalog_import)
    assert '"EXOTIC"' in src, "EXOTIC не в списке допустимых баз импорта"


# ─── face_value следует за синком даже у залоченной строки ──────────────────

def test_face_value_not_frozen_by_lock():
    """Амортизация: залоченная строка обязана принимать свежий номинал MOEX."""
    from services import instruments_registry as reg
    assert "face_value" not in reg._MANUAL_FIELDS


def test_sync_skips_currency_face():
    """FACEVALUE замещающих/юаневых — в валюте: в реестр такой номинал не пишем."""
    import inspect
    from services import instruments_sync
    src = inspect.getsource(instruments_sync.sync_instruments) \
        if hasattr(instruments_sync, "sync_instruments") else \
        open(instruments_sync.__file__).read()
    assert "face_unit" in src, "guard по FACEUNIT снят"
