"""Тесты реестра инструментов (НРД-независимый источник универса) и гейта НРД.
Изолированная БД во временном файле — прод data/ не трогаем.
"""
import os
import tempfile

import pytest


@pytest.fixture
def reg(monkeypatch):
    """Свежий реестр в temp-БД на каждый тест."""
    db = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("INSTRUMENTS_DB", db)
    import importlib
    import services.instruments_registry as m
    importlib.reload(m)
    yield m
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(db + suf)
        except OSError:
            pass


def test_upsert_new_then_update(reg):
    assert reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150}, "cbonds") == "new"
    assert reg.upsert({"isin": "RU1", "margin_bps": 200}, "cbonds") == "updated"
    r = reg.get("RU1")
    assert r["margin_bps"] == 200 and r["base"] == "KEYRATE"  # base не затёрт None-ом


def test_none_does_not_overwrite(reg):
    reg.upsert({"isin": "RU1", "base": "RUONIA", "margin_bps": 100}, "cbonds")
    reg.upsert({"isin": "RU1", "base": None, "margin_bps": None}, "nrd_frozen")
    r = reg.get("RU1")
    assert r["base"] == "RUONIA" and r["margin_bps"] == 100  # None не затирает известное


def test_manual_lock_protects_fields(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150}, "cbonds")
    reg.set_manual("RU1", {"margin_bps": 999}, lock=True)
    # sync-путь пытается перезаписать margin → у locked не должен
    reg.upsert({"isin": "RU1", "margin_bps": 150}, "cbonds")
    assert reg.get("RU1")["margin_bps"] == 999
    # но rating (не manual-поле) обновляется
    reg.upsert({"isin": "RU1", "rating": "AAA"}, "cbonds")
    assert reg.get("RU1")["rating"] == "AAA"


def test_universe_rows_shape_and_filter(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150,
                "maturity_date": "2030-01-01", "short_name": "Test1"}, "cbonds")
    reg.upsert({"isin": "RU2", "base": "FIXED", "margin_bps": 0}, "cbonds")  # не флоатер
    rows = reg.universe_rows(only_floaters=True)
    assert len(rows) == 1
    r = rows[0]
    # форма совместима с fetch_floater_universe
    for k in ("isin", "name", "base_rate_type", "spread_issue_bps", "maturity_date",
              "nrd_price_pct", "discount_margin_bps", "simple_margin_bps", "z_spread_bps"):
        assert k in r
    assert r["base_rate_type"] == "KEYRATE" and r["spread_issue_bps"] == 150
    assert r["nrd_price_pct"] is None  # NRD-поля пусты без НРД


def test_sync_merges_sources_cbonds_priority(reg):
    frozen = [{"isin": "RU1", "name": "FromNRD", "base_rate_type": "KEYRATE",
               "spread_issue_bps": 100, "maturity_date": "2030-01-01", "rating": "AA"}]
    cbonds = {"RU1": {"base": "KEYRATE", "margin_bps": 175, "name": "FromCbonds"},
              "RU2": {"base": "RUONIA", "margin_bps": 50, "name": "OnlyCbonds"}}
    stats = reg.sync_from_sources(frozen, cbonds, {})
    assert stats["total"] == 2
    r1 = reg.get("RU1")
    assert r1["margin_bps"] == 175              # Cbonds приоритетнее NRD
    assert r1["maturity_date"] == "2030-01-01"  # maturity из NRD (Cbonds не даёт)
    assert r1["rating"] == "AA"                 # rating из NRD
    assert reg.get("RU2") is not None           # discovered из Cbonds


def test_sync_skips_service_manual_keys(reg):
    # _README и не-dict значения в manual не должны падать/попадать
    reg.sync_from_sources([], {"RU1": {"base": "KEYRATE", "margin_bps": 100}},
                          {"_README": "текст", "RU1": {"fixing_lag": 7}})
    assert reg.get("RU1")["fixing_lag"] == 7
    assert reg.get("_README") is None


def test_manual_reflects_in_universe_and_source(reg):
    """Ручной ввод параметров → виден в universe_rows, source='manual', reviewed=1."""
    reg.upsert({"isin": "RU1", "base": "RUONIA", "margin_bps": 100}, "cbonds")
    reg.set_manual("RU1", {"base": "KEYRATE", "margin_bps": 250,
                           "maturity_date": "2029-06-01"})
    r = reg.get("RU1")
    assert r["source"] == "manual" and r["manual_locked"] == 1 and r["reviewed"] == 1
    u = [x for x in reg.universe_rows() if x["isin"] == "RU1"][0]
    assert u["base_rate_type"] == "KEYRATE" and u["spread_issue_bps"] == 250
    assert u["maturity_date"] == "2029-06-01"


def test_mark_reviewed_removes_from_unreviewed(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 100}, "cbonds")
    assert any(x["isin"] == "RU1" for x in reg.list_unreviewed())
    reg.mark_reviewed("RU1")
    assert not any(x["isin"] == "RU1" for x in reg.list_unreviewed())


def test_priceable_filter_excludes_incomplete(reg):
    """only_priceable=True (дефолт): в универс только бумаги с base+margin+maturity."""
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150,
                "maturity_date": "2030-01-01"}, "cbonds")            # полная
    reg.upsert({"isin": "RU2", "base": "KEYRATE", "margin_bps": 150}, "cbonds")  # нет maturity
    reg.upsert({"isin": "RU3", "base": "RUONIA", "maturity_date": "2030-01-01"}, "cbonds")  # нет margin
    priceable = {x["isin"] for x in reg.universe_rows()}
    assert priceable == {"RU1"}
    allrows = {x["isin"] for x in reg.universe_rows(only_priceable=False)}
    assert allrows == {"RU1", "RU2", "RU3"}
    inc = {x["isin"] for x in reg.list_incomplete()}
    assert inc == {"RU2", "RU3"}
    c = reg.count()
    assert c["priceable"] == 1 and c["incomplete"] == 2


def test_retire_matured(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150,
                "maturity_date": "2020-01-01"}, "cbonds")   # погашена
    reg.upsert({"isin": "RU2", "base": "KEYRATE", "margin_bps": 150,
                "maturity_date": "2030-01-01"}, "cbonds")   # живая
    n = reg.retire_matured("2026-07-22")
    assert n == 1
    live = {x["isin"] for x in reg.universe_rows(only_priceable=False)}
    assert live == {"RU2"}                                  # погашенная выпала


def test_sync_active_set_deactivates_non_traded(reg):
    """Только MOEX-торгуемые: не-торгуемые → active=0, вернувшиеся → active=1."""
    for i in range(600):  # набор >min_expected, чтобы sanity-guard пропустил
        reg.upsert({"isin": f"RU{i:010d}", "base": "KEYRATE", "margin_bps": 100,
                    "maturity_date": "2030-01-01"}, "cbonds")
    reg.upsert({"isin": "RUCOMMERCIAL0", "base": "KEYRATE", "margin_bps": 100,
                "maturity_date": "2030-01-01"}, "cbonds")   # не на MOEX
    traded = {f"RU{i:010d}" for i in range(600)}            # без RUCOMMERCIAL0
    st = reg.sync_active_set(traded)
    assert st["deactivated"] == 1
    assert reg.get("RUCOMMERCIAL0")["active"] == 0
    assert "RUCOMMERCIAL0" not in {x["isin"] for x in reg.universe_rows(only_priceable=False)}
    # вернулась в листинг → реактивация
    st2 = reg.sync_active_set(traded | {"RUCOMMERCIAL0"})
    assert st2["reactivated"] == 1
    assert reg.get("RUCOMMERCIAL0")["active"] == 1


def test_sync_active_set_guards_small_listing(reg):
    """Куцый листинг (сбой сети) → массовой деактивации НЕТ (не обнуляем универс)."""
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 100,
                "maturity_date": "2030-01-01"}, "cbonds")
    st = reg.sync_active_set({"RU_OTHER"}, min_expected=500)  # набор мал
    assert st.get("deactivated") == 0
    assert reg.get("RU1")["active"] == 1                    # не тронут


def test_nrd_disabled_by_default(monkeypatch):
    monkeypatch.setenv("NRD_CONFIG_FILE", tempfile.mktemp(suffix=".json"))
    import importlib
    import services.nrd_config as cfg
    importlib.reload(cfg)
    assert cfg.is_enabled() is False           # базово ВЫКЛЮЧЕН
    cfg.set_enabled(True)
    assert cfg.is_enabled() is True
    cfg.set_enabled(False)
    assert cfg.is_enabled() is False
