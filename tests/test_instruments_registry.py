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
    # форма universe-строки (совместима с fetch_floater_universe)
    for k in ("isin", "name", "base_rate_type", "spread_issue_bps", "maturity_date",
              "rating", "emitter_id", "emitter_name"):
        assert k in r
    assert r["base_rate_type"] == "KEYRATE" and r["spread_issue_bps"] == 150


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


def test_reclassify_fixed_leaves_universe(reg):
    """Фикс-бумага (base=FIXED) уходит из флоатер-универса."""
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150,
                "maturity_date": "2030-01-01"}, "cbonds")
    assert "RU1" in {x["isin"] for x in reg.universe_rows()}
    reg.reclassify_fixed("RU1")
    assert reg.get("RU1")["base"] == "FIXED"
    assert "RU1" not in {x["isin"] for x in reg.universe_rows()}


def test_reclassify_fixed_respects_manual_lock(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150,
                "maturity_date": "2030-01-01"}, "cbonds")
    reg.set_manual("RU1", {"base": "KEYRATE"}, lock=True)
    reg.reclassify_fixed("RU1")
    assert reg.get("RU1")["base"] == "KEYRATE"        # locked — не тронут


def test_margin_check_and_suspect(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150,
                "maturity_date": "2030-01-01"}, "cbonds")
    reg.upsert({"isin": "RU2", "base": "KEYRATE", "margin_bps": 999,
                "maturity_date": "2030-01-01"}, "cbonds")
    reg.set_margin_check("RU1", 0.3)    # ок
    reg.set_margin_check("RU2", 5.0)    # подозрит.
    suspect = {r["isin"] for r in reg.list_suspect()}
    assert suspect == {"RU2"}
    assert reg.count()["suspect"] == 1


def test_discovery_pending_excludes_known_and_decided(reg):
    """Кандидат исключается, если он уже в реестре ИЛИ помечен значимо (флоатер/фикс)."""
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150}, "moex")
    reg.mark_discovery_seen("RU2", True)    # найден флоатером
    reg.mark_discovery_seen("RU3", False)   # фикс — значимо, не перечекивать
    pending = reg.discovery_pending(["RU1", "RU2", "RU3", "RU4"], limit=10)
    assert pending == ["RU4"]               # только непроверенный


def test_discovery_pending_preserves_priority_and_cap(reg):
    """Порядок candidates сохраняется (приоритет), limit режет."""
    pending = reg.discovery_pending(["A", "B", "C", "D"], limit=2)
    assert pending == ["A", "B"]


def test_discovery_null_rechecked_after_ttl(reg, monkeypatch):
    """is_floater=NULL (нет bondization) перечекивается после TTL, decided — никогда."""
    from datetime import datetime, timedelta, timezone
    reg.mark_discovery_seen("RU_NULL", None)   # нет данных графика
    reg.mark_discovery_seen("RU_FIX", False)   # решено
    # свежий NULL — пропускается (в skip)
    assert reg.discovery_pending(["RU_NULL", "RU_FIX"], 10) == []
    # состарим checked_at NULL-записи за пределы TTL → снова кандидат
    old = (datetime.now(timezone.utc) - timedelta(days=reg._DISCOVERY_NULL_TTL_DAYS + 1)).isoformat()
    with reg._conn() as c:
        c.execute("UPDATE discovery_seen SET checked_at=? WHERE isin=?", (old, "RU_NULL"))
        c.execute("UPDATE discovery_seen SET checked_at=? WHERE isin=?", (old, "RU_FIX"))
    pending = reg.discovery_pending(["RU_NULL", "RU_FIX"], 10)
    assert pending == ["RU_NULL"]              # NULL вернулся, decided(False) — нет


def test_list_catalog_incomplete_first_and_flags(reg):
    reg.upsert({"isin": "RU000A10FM37", "short_name": "Каширская", "base": "KEYRATE",
                "margin_bps": 135, "maturity_date": "2029-07-05"}, "moex")
    reg.upsert({"isin": "RU000A10FP91", "short_name": "Атом", "base": None,
                "margin_bps": None, "maturity_date": "2029-01-01"}, "moex")
    cat = reg.list_catalog()
    assert len(cat) == 2
    assert cat[0]["isin"] == "RU000A10FP91" and cat[0]["priceable"] is False  # непрайсуемый вперёд
    assert cat[1]["priceable"] is True
    assert "coupon_mode" in cat[0] and "rating" in cat[0]                     # полный набор колонок


def test_list_catalog_floaters_only(reg):
    reg.upsert({"isin": "RU000A10FM37", "base": "KEYRATE", "margin_bps": 135,
                "maturity_date": "2029-07-05"}, "moex")
    reg.upsert({"isin": "RU000A10FE11", "base": "EXOTIC", "maturity_date": "2031-01-01"}, "moex")
    only_fl = {r["isin"] for r in reg.list_catalog(floaters_only=True)}
    assert only_fl == {"RU000A10FM37"}                                        # EXOTIC отсеян


def test_coupon_overrides_only_locked(reg):
    # ручная правка (locked) — попадает в мост
    reg.set_manual("RU000A10FM37", {"base": "KEYRATE", "margin_bps": 200,
                                     "floor_pct": 13.0, "coupon_text": "MAX(КС+2%;13%)"}, lock=True)
    # авто-sync (не locked) — НЕ должен попасть в мост (иначе регрессия прайсинга)
    reg.upsert({"isin": "RU000A10FP91", "base": "KEYRATE", "margin_bps": 150,
                "coupon_mode": "average"}, "corpbonds")
    ov = reg.coupon_overrides_all()
    assert "RU000A10FM37" in ov
    assert ov["RU000A10FM37"]["floor_pct"] == 13.0
    assert ov["RU000A10FM37"]["coupon_text"] == "MAX(КС+2%;13%)"
    assert "RU000A10FP91" not in ov            # не locked → вне моста


def test_manual_cap_floor_survive_sync(reg):
    reg.set_manual("RU000A10FM37", {"base": "KEYRATE", "cap_pct": 25.0, "floor_pct": 13.0}, lock=True)
    # sync-апдейт не-manual полей не должен затирать cap/floor (они в _MANUAL_FIELDS)
    reg.upsert({"isin": "RU000A10FM37", "rating": "AA", "cap_pct": None, "floor_pct": None}, "cbonds")
    r = reg.get("RU000A10FM37")
    assert r["cap_pct"] == 25.0 and r["floor_pct"] == 13.0 and r["rating"] == "AA"


def test_non_fixed_isins_excludes_null_and_exotic(reg):
    # KEYRATE/RUONIA/NULL/EXOTIC — все флоатеры, нельзя пускать во вкладку ФИКСЫ
    reg.upsert({"isin": "RU_KS", "base": "KEYRATE"}, "moex")
    reg.upsert({"isin": "RU_NULL"}, "moex")                    # флоатер без параметров
    reg.upsert({"isin": "RU_EXO", "base": "EXOTIC"}, "moex")
    reg.upsert({"isin": "RU_FIX", "base": "FIXED"}, "moex")    # реклассифицирован — фикс
    # подтверждён bondization'ом, но в instruments ещё не заведён
    reg.mark_discovery_seen("RU_SEEN", True)
    reg.mark_discovery_seen("RU_SEEN_FIX", False)
    s = reg.non_fixed_isins()
    assert {"RU_KS", "RU_NULL", "RU_EXO", "RU_SEEN"} <= s
    assert "RU_FIX" not in s and "RU_SEEN_FIX" not in s


def test_non_fixed_isins_skips_inactive(reg):
    reg.upsert({"isin": "RU_DEAD", "base": "KEYRATE", "maturity_date": "2020-01-01"}, "moex")
    reg.retire_matured("2026-01-01")
    assert "RU_DEAD" not in reg.non_fixed_isins()


def test_enrich_pending_rotation_and_ttl(reg):
    # никогда не пробованные — первыми, в исходном порядке
    assert reg.enrich_pending(["A", "B", "C"], 2) == ["A", "B"]
    # свежая попытка (внутри TTL) — пропускается, хвост очереди достигается
    reg.mark_enrich_attempt("A", "not_found")
    assert reg.enrich_pending(["A", "B", "C"], 2) == ["B", "C"]
    # протухшая попытка — возвращается в очередь ПОСЛЕ никогда не пробованных
    import sqlite3
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    with sqlite3.connect(str(reg.DB_PATH)) as c:
        c.execute("UPDATE enrich_seen SET attempted_at=? WHERE isin='A'", (old,))
    assert reg.enrich_pending(["A", "B", "C"], 3) == ["B", "C", "A"]


def test_enrich_pending_ttl_per_result(reg):
    import sqlite3
    from datetime import datetime, timedelta, timezone
    reg.mark_enrich_attempt("EXO", "exotic")       # TTL 30 дн
    reg.mark_enrich_attempt("NF", "not_found")     # TTL 14 дн
    d20 = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    with sqlite3.connect(str(reg.DB_PATH)) as c:
        c.execute("UPDATE enrich_seen SET attempted_at=?", (d20,))
    # 20 дней: not_found уже перечекивается, exotic ещё нет
    assert reg.enrich_pending(["EXO", "NF"], 5) == ["NF"]


def test_discovery_fixed_verdict_expires(reg):
    import sqlite3
    from datetime import datetime, timedelta, timezone
    reg.mark_discovery_seen("RU_FIX0", False)
    assert reg.discovery_pending(["RU_FIX0"], 5) == []          # свежий вердикт держит
    d100 = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    with sqlite3.connect(str(reg.DB_PATH)) as c:
        c.execute("UPDATE discovery_seen SET checked_at=?", (d100,))
    assert reg.discovery_pending(["RU_FIX0"], 5) == ["RU_FIX0"]  # 90д TTL истёк


def test_upsert_keep_source(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 100}, "cbonds")
    reg.upsert({"isin": "RU1", "maturity_date": "2030-01-01"}, "moex", keep_source=True)
    assert reg.get("RU1")["source"] == "cbonds"                 # рефреш не съел провенанс
    reg.upsert({"isin": "RU1", "base": "RUONIA"}, "moex")
    assert reg.get("RU1")["source"] == "moex"                   # параметры — переписывает


def test_enrich_pending_parser_version_invalidates_exotic(reg):
    reg.mark_enrich_attempt("EXO", "exotic", parser_ver=1)
    reg.mark_enrich_attempt("NF", "not_found", parser_ver=1)
    # та же версия: оба свежие → пусто
    assert reg.enrich_pending(["EXO", "NF"], 5, parser_ver=1) == []
    # версия выросла: exotic перечекивается сразу, not_found ждёт своего TTL
    assert reg.enrich_pending(["EXO", "NF"], 5, parser_ver=2) == ["EXO"]


def test_set_exotic_saves_formula(reg):
    reg.upsert({"isin": "RU_X", "base": "KEYRATE"}, "moex")
    reg.set_exotic("RU_X", note="MAX(25.9% - КС; 0.01%)")
    r = reg.get("RU_X")
    assert r["base"] == "EXOTIC" and r["coupon_text"] == "MAX(25.9% - КС; 0.01%)"
    # пустой note не затирает сохранённую формулу
    reg.set_exotic("RU_X", note="")
    assert reg.get("RU_X")["coupon_text"] == "MAX(25.9% - КС; 0.01%)"
    assert any(e["coupon_text"] for e in reg.list_exotic())


def test_list_no_spec(reg):
    # прайсуемый без текста/спеки → в очереди
    reg.upsert({"isin": "RU_NOSPEC", "base": "KEYRATE", "margin_bps": 200,
                "maturity_date": "2030-01-01"}, "cbonds")
    # прайсуемый с текстом → нет
    reg.upsert({"isin": "RU_TXT", "base": "KEYRATE", "margin_bps": 200,
                "maturity_date": "2030-01-01", "coupon_text": "КС + 2%"}, "cbonds")
    # непрайсуемый (нет маржи) → нет (он в incomplete)
    reg.upsert({"isin": "RU_INC", "base": "KEYRATE", "maturity_date": "2030-01-01"}, "cbonds")
    ids = {r["isin"] for r in reg.list_no_spec()}
    assert ids == {"RU_NOSPEC"}


# ── сверка типа купона со smart-lab (services/smartlab_audit) ───────────────

def test_smartlab_type_agrees(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150}, "cbonds")
    assert reg.set_smartlab_type("RU1", "floater") is None      # сходится
    r = reg.get("RU1")
    assert r["sl_type"] == "floater" and r["sl_mismatch"] == 0 and r["sl_checked_at"]


def test_smartlab_catches_wrong_fixed(reg):
    """Наш вердикт FIXED против «плавающего купона» на сайте — расхождение."""
    reg.upsert({"isin": "RU1", "base": "FIXED"}, "moex")
    assert reg.set_smartlab_type("RU1", "floater") == "mismatch_fixed"
    assert reg.get("RU1")["sl_mismatch"] == 1
    assert [x["isin"] for x in reg.list_sl_mismatch()] == ["RU1"]


def test_smartlab_catches_wrong_floater(reg):
    reg.upsert({"isin": "RU1", "base": "RUONIA", "margin_bps": 100}, "cbonds")
    assert reg.set_smartlab_type("RU1", "fixed") == "mismatch_floater"
    assert reg.get("RU1")["sl_mismatch"] == 1


def test_smartlab_silence_is_not_a_verdict(reg):
    """Сайт про тип молчит — не расхождение, и прошлый ответ не затирается."""
    reg.upsert({"isin": "RU1", "base": "FIXED"}, "moex")
    reg.set_smartlab_type("RU1", "fixed")
    assert reg.set_smartlab_type("RU1", None) is None
    r = reg.get("RU1")
    assert r["sl_type"] == "fixed" and r["sl_mismatch"] == 0


def test_clear_base_returns_bond_to_queue(reg):
    reg.upsert({"isin": "RU1", "base": "FIXED", "margin_bps": 300}, "moex")
    assert reg.clear_base("RU1") is True
    r = reg.get("RU1")
    assert r["base"] is None and r["reviewed"] == 0
    assert r["margin_bps"] == 300      # маржа от базы не зависит — не трогаем


def test_clear_base_respects_manual_lock(reg):
    reg.upsert({"isin": "RU1", "base": "FIXED"}, "moex")
    reg.set_manual("RU1", {"base": "FIXED"}, lock=True)
    assert reg.clear_base("RU1") is False
    assert reg.get("RU1")["base"] == "FIXED"


def test_sl_stale_rotation(reg):
    """Порция: сначала ни разу не проверенные, потом самые давние."""
    for i in ("RU1", "RU2", "RU3"):
        reg.upsert({"isin": i, "base": "KEYRATE", "margin_bps": 100}, "cbonds")
    reg.set_smartlab_type("RU1", "floater")
    assert reg.list_sl_stale(2) == ["RU2", "RU3"]
    reg.set_smartlab_type("RU2", "floater")
    reg.set_smartlab_type("RU3", "floater")
    assert reg.list_sl_stale(1) == ["RU1"]      # проверенный раньше всех
