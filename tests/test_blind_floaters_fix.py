"""Тесты на разбор «слепой» очереди реестра — бумаг с base=NULL.

Discovery заводит флоатером всё, у чего есть будущий купон без суммы. Так ведёт
себя и ФИКС с офертой («ставку определит эмитент»), и фикс с неопубликованным
хвостом графика: на проде 03.09.2026 таких было 310 из 1176 активных, и бумага
с base=NULL не показывается НИ во флоатерах (нужны KEYRATE/RUONIA), НИ в ФИКСАХ
(non_fixed_isins исключает NULL). Здесь проверяется, что положительное знание
источников («Тип купона: Фикс» у corpbonds, «фикс» у smart-lab) доезжает до
реестра, а обратная ошибка — ложный FIXED у смешанной бумаги — блокируется.
"""
import os
import tempfile
from datetime import date, timedelta

import pytest


@pytest.fixture
def reg(monkeypatch):
    """Свежий реестр в temp-БД на каждый тест.

    В teardown модуль перезагружается с ВОССТАНОВЛЕННЫМ окружением: путь к БД и
    флаг «схема создана» живут в модуле, и оставленная temp-БД (уже удалённая с
    диска) ломала бы следующие тесты, которые берут реестр модулем-синглтоном."""
    db = tempfile.mktemp(suffix=".db")
    prev = os.environ.get("INSTRUMENTS_DB")
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
    if prev is None:
        os.environ.pop("INSTRUMENTS_DB", None)
    else:
        os.environ["INSTRUMENTS_DB"] = prev
    importlib.reload(m)


# ── вето на автоматический FIXED ───────────────────────────────────────────────

def test_reclassify_fixed_applies_and_reports(reg):
    reg.upsert({"isin": "RU1", "base": None, "short_name": "Тест"}, "moex")
    assert reg.reclassify_fixed("RU1") is True
    assert reg.get("RU1")["base"] == "FIXED"


def test_reclassify_fixed_vetoed_by_smartlab_floater(reg):
    """smart-lab видит флоатер → наш вердикт «ставка не менялась» проигрывает.
    Раньше это давало качели: smartlab_audit снимал FIXED, инфер ставил назад."""
    reg.upsert({"isin": "RU1", "base": None, "short_name": "Тест"}, "moex")
    reg.set_smartlab_type("RU1", "floater")
    assert reg.reclassify_fixed("RU1") is False
    assert reg.get("RU1")["base"] is None


def test_reclassify_fixed_vetoed_by_floating_tranche(reg):
    """Башнефть БО-10: купоны 1-16 по 9.5%, 17-20 — КС+2.5%. Все реализованные
    купоны одинаковы, но бумага уже плавает."""
    reg.upsert({"isin": "RU1", "base": None, "coupon_text":
                "1-16 купоны - 9.5% годовых, 17-20 купоны: Кi = R + 2,5%, где "
                "R - Ключевая ставка Центрального Банка"}, "cbonds")
    assert reg.reclassify_fixed("RU1") is False
    assert reg.get("RU1")["base"] is None


def test_reclassify_fixed_allows_plain_fixed_text(reg):
    """У чистого фикса в формуле индекса нет вовсе — вето не срабатывает."""
    reg.upsert({"isin": "RU1", "base": None,
                "coupon_text": "1-20 купоны — 12.5% годовых"}, "cbonds")
    assert reg.reclassify_fixed("RU1") is True


def test_reclassify_fixed_respects_manual_lock(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 150}, "cbonds")
    reg.set_manual("RU1", {"base": "KEYRATE"}, lock=True)
    assert reg.reclassify_fixed("RU1") is False
    assert reg.get("RU1")["base"] == "KEYRATE"


def test_clear_false_fixed(reg):
    """Уборка вердиктов, поставленных до появления вето: маржа остаётся (она из
    проспекта и от базы не зависит), база снимается — конвейер определит заново."""
    reg.upsert({"isin": "RU1", "base": "FIXED", "margin_bps": 250}, "cbonds")
    reg.set_smartlab_type("RU1", "floater")
    reg.upsert({"isin": "RU2", "base": "FIXED"}, "cbonds")        # сайт согласен
    reg.set_smartlab_type("RU2", "fixed")
    assert reg.clear_false_fixed() == ["RU1"]
    assert reg.get("RU1")["base"] is None
    assert reg.get("RU1")["margin_bps"] == 250
    assert reg.get("RU2")["base"] == "FIXED"


# ── очередь «без базы, но фикс по smart-lab» ───────────────────────────────────

def test_list_blind_sl_fixed_only_blind(reg):
    reg.upsert({"isin": "RU1", "base": None}, "moex")
    reg.set_smartlab_type("RU1", "fixed")
    reg.upsert({"isin": "RU2", "base": "KEYRATE", "margin_bps": 100}, "cbonds")
    reg.set_smartlab_type("RU2", "fixed")     # это уже расхождение, не «слепая»
    reg.upsert({"isin": "RU3", "base": None}, "moex")   # сайт молчит
    assert [r["isin"] for r in reg.list_blind_sl_fixed()] == ["RU1"]


def test_apply_known_fixed_skips_fresh_issue(reg, monkeypatch):
    """Свежему выпуску параметры доливает наш конвейер — чужой странице в первые
    недели не верим (она обычно ещё зовёт флоатер фиксом)."""
    from services import smartlab_audit
    old = (date.today() - timedelta(days=400)).isoformat()
    fresh = (date.today() - timedelta(days=3)).isoformat()
    reg.upsert({"isin": "RUOLD", "base": None, "issue_date": old}, "moex")
    reg.upsert({"isin": "RUNEW", "base": None, "issue_date": fresh}, "moex")
    for i in ("RUOLD", "RUNEW"):
        reg.set_smartlab_type(i, "fixed")
    monkeypatch.setattr("services.instruments_registry", reg, raising=False)
    stats = smartlab_audit.apply_known_fixed()
    assert stats["fixed"] == 1
    assert reg.get("RUOLD")["base"] == "FIXED"
    assert reg.get("RUNEW")["base"] is None


def test_apply_known_fixed_treats_unknown_issue_date_as_old(reg, monkeypatch):
    """У сидового импорта issue_date пуст — такие считаем старыми, иначе весь
    бэклог был бы «свежим» и правило не сработало бы никогда."""
    from services import smartlab_audit
    reg.upsert({"isin": "RU1", "base": None}, "moex")
    reg.set_smartlab_type("RU1", "fixed")
    monkeypatch.setattr("services.instruments_registry", reg, raising=False)
    assert smartlab_audit.apply_known_fixed()["fixed"] == 1
    assert reg.get("RU1")["base"] == "FIXED"


# ── corpbonds: «Тип купона» трёхзначно ────────────────────────────────────────

def test_corpbonds_coupon_type_is_tristate():
    """Отсутствие строки на странице ≠ «фикс»: карточка без формулы бывает и у
    флоатера, которого сайт ещё не разобрал."""
    from services.enrich_corpbonds import parse_corpbonds_html
    tpl = ('<table><tr><td>Дата погашения</td><td><p class="val">{mat}</p></td></tr>'
           '{ct}</table>')
    ct_row = ('<tr><td>Тип купона</td><td><p class="val">{v}</p></td></tr>')
    fixed = parse_corpbonds_html(tpl.format(mat="08.02.2030", ct=ct_row.format(v="Фикс")))
    assert fixed["coupon_type"] == "Фикс" and fixed["is_floater"] is False
    floater = parse_corpbonds_html(tpl.format(mat="08.02.2030",
                                              ct=ct_row.format(v="Флоатер")))
    assert floater["coupon_type"] == "Флоатер" and floater["is_floater"] is True
    silent = parse_corpbonds_html(tpl.format(mat="08.02.2030", ct=""))
    assert silent["coupon_type"] is None and silent["is_floater"] is False


def test_enrich_registry_fixes_blind_on_fix_coupon_type(reg, monkeypatch):
    """Страница есть, «Тип купона: Фикс», формулы нет, своей базы нет → FIXED
    (а не вердикт nodata, с которым бумага висела вне обеих витрин)."""
    import asyncio
    from services import enrich_corpbonds as ec
    reg.upsert({"isin": "RU1", "base": None, "short_name": "МТС 1P-28"}, "moex")
    monkeypatch.setattr("services.instruments_registry", reg, raising=False)

    async def fake_fetch(isin, client=None):
        return {"source": "corpbonds", "is_floater": False, "coupon_type": "Фикс",
                "has_call": None, "call_dates": []}

    monkeypatch.setattr(ec, "fetch_corpbonds", fake_fetch)
    r = asyncio.run(ec.enrich_registry(["RU1"], apply=True, delay=0))
    assert r["stats"]["fixed"] == 1
    assert reg.get("RU1")["base"] == "FIXED"
    assert reg.enrich_info("RU1")["result"] == "fixed"


def test_enrich_registry_keeps_own_base_on_fix_label(reg, monkeypatch):
    """Разобранную формулу из проспекта чужая метка не перебивает."""
    import asyncio
    from services import enrich_corpbonds as ec
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 250}, "cbonds")
    monkeypatch.setattr("services.instruments_registry", reg, raising=False)

    async def fake_fetch(isin, client=None):
        return {"source": "corpbonds", "is_floater": False, "coupon_type": "Фикс",
                "has_call": None, "call_dates": []}

    monkeypatch.setattr(ec, "fetch_corpbonds", fake_fetch)
    asyncio.run(ec.enrich_registry(["RU1"], apply=True, delay=0))
    assert reg.get("RU1")["base"] == "KEYRATE"


# ── сверка маржи с карточкой биржи ────────────────────────────────────────────

def test_bench_mismatch_flags_margin_and_base(reg):
    reg.upsert({"isin": "RUOK", "base": "KEYRATE", "margin_bps": 130,
                "maturity_date": "2030-01-01"}, "cbonds")
    reg.set_bench_margin("RUOK", "KEYRATE", 130)
    reg.upsert({"isin": "RUDIFF", "base": "KEYRATE", "margin_bps": 130,
                "maturity_date": "2030-02-08", "short_name": "ГазКап3P29"}, "corpbonds")
    reg.set_bench_margin("RUDIFF", "KEYRATE", 150)          # биржа спорит на 20 bps
    reg.upsert({"isin": "RUBASE", "base": "KEYRATE", "margin_bps": 100,
                "maturity_date": "2030-01-01"}, "cbonds")
    reg.set_bench_margin("RUBASE", "RUONIA", 100)           # спорит сам индекс
    got = {r["isin"]: r["diff_bps"] for r in reg.list_bench_mismatch()}
    assert got == {"RUDIFF": -20, "RUBASE": None}


def test_bench_mismatch_ignores_rounding(reg):
    reg.upsert({"isin": "RU1", "base": "KEYRATE", "margin_bps": 130,
                "maturity_date": "2030-01-01"}, "cbonds")
    reg.set_bench_margin("RU1", "KEYRATE", 135)             # 5 bps < порога
    assert reg.list_bench_mismatch() == []


# ── долговечная отметка полного синка ─────────────────────────────────────────

def test_meta_roundtrip_and_last_full_sync(reg, monkeypatch):
    from services import instruments_sync as sync
    monkeypatch.setattr("services.instruments_registry", reg, raising=False)
    assert reg.get_meta("last_full_sync") is None
    assert sync.last_full_sync() is None
    reg.set_meta("last_full_sync", "2026-09-03")
    assert sync.last_full_sync() == "2026-09-03"
    reg.set_meta("last_full_sync", "2026-09-04")            # перезапись, не дубль
    assert sync.last_full_sync() == "2026-09-04"


# ── витрина ФИКСОВ видит подтверждённый фикс ───────────────────────────────────

def test_non_fixed_isins_respects_confirmed_fixed(reg):
    """Отметка discovery «флоатер» — эвристика («есть купон без суммы»), а
    base='FIXED' — вердикт по типу купона от источника. Пока отметка была
    сильнее, переклассифицированная бумага исключалась и из ФИКСОВ тоже, то есть
    не показывалась нигде."""
    reg.upsert({"isin": "RUFIX", "base": "FIXED"}, "corpbonds")
    reg.mark_discovery_seen("RUFIX", True)          # discovery считала флоатером
    reg.upsert({"isin": "RUBLIND", "base": None}, "moex")
    reg.mark_discovery_seen("RUBLIND", True)
    reg.upsert({"isin": "RUFL", "base": "KEYRATE", "margin_bps": 100}, "cbonds")
    out = reg.non_fixed_isins()
    assert "RUFIX" not in out                        # уехала в ФИКСЫ
    assert {"RUBLIND", "RUFL"} <= out                # база не подтверждена / флоатер


def test_clear_false_fixed_drops_enrich_cache(reg):
    """Снятая база должна переспрашиваться источниками сразу, а не по TTL
    прошлой попытки (у «filled» это 7 дней)."""
    reg.upsert({"isin": "RU1", "base": "FIXED", "margin_bps": 250}, "cbonds")
    reg.set_smartlab_type("RU1", "floater")
    reg.mark_enrich_attempt("RU1", "filled", parser_ver=6)
    assert reg.clear_false_fixed() == ["RU1"]
    assert reg.enrich_info("RU1") is None
    assert reg.enrich_pending(["RU1"], 5, parser_ver=6) == ["RU1"]
