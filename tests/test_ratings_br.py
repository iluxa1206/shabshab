"""Слой рейтингов по агентствам (bondresearch): лента событий → текущая оценка.

Сеть не трогается: все проверки на синтетических выгрузках той же формы.
"""
from services.ratings import rating_min, rating_norm, rating_rank, rating_to_bucket
from services.ratings_br import build, parse_current, parse_isin_map

# [эмитент, агентство, дата, текущий, предыдущий, тип, issuer_code, изменение]
def ch(issuer, agency, day, cur, action="Присвоен"):
    return [issuer, agency, day, cur, None, action, "X01", 1]


def test_latest_event_wins():
    """Текущий рейтинг = ПОСЛЕДНЕЕ по дате событие пары (эмитент, агентство)."""
    got = parse_current([
        ch("Эмитент", "АКРА", "2024-01-10", "A"),
        ch("Эмитент", "АКРА", "2026-06-30", "A+", "Повышен"),
        ch("Эмитент", "АКРА", "2025-03-01", "A-", "Понижен"),
    ])
    assert got == {"Эмитент": {"acra": {"rating": "A+", "date": "2026-06-30"}}}


def test_withdrawn_kills_pair():
    """«Отозван» — рейтинга НЕТ, а не «последний известный»."""
    got = parse_current([
        ch("Э", "АКРА", "2024-01-10", "A"),
        ch("Э", "АКРА", "2026-01-10", "A", "Отозван"),
        ch("Э", "Эксперт РА", "2025-01-10", "BBB"),
    ])
    assert got == {"Э": {"expert": {"rating": "BBB", "date": "2025-01-10"}}}


def test_withdrawn_then_returned():
    # отозвали и вернули — рейтинг снова действует
    got = parse_current([
        ch("Э", "АКРА", "2024-01-10", "A", "Отозван"),
        ch("Э", "АКРА", "2025-01-10", "A-", "Возвращен"),
    ])
    assert got["Э"]["acra"]["rating"] == "A-"


def test_other_agencies_ignored():
    """НКР/НРА/Композит в ленте есть, но в слой пока не берутся."""
    got = parse_current([ch("Э", "НКР", "2026-01-10", "AA"),
                         ch("Э", "Композит", "2026-01-10", "AA"),
                         ch("Э", "НРА", "2026-01-10", "AA")])
    assert got == {}


def test_cyrillic_rating_survives():
    # в выгрузках рейтинг пишут и русскими буквами
    got = parse_current([ch("Э", "АКРА", "2026-01-10", "АА-")])
    assert got["Э"]["acra"]["rating"] == "AA-"


def test_isin_map_from_both_boards():
    fixed = [[None] * 40]
    fixed[0][26], fixed[0][39] = "Фикс-эмитент", "RU000FIXED01"
    floaters = [[None] * 39]
    floaters[0][1], floaters[0][24] = "RU000FLOAT01", "Флоатер-эмитент"
    assert parse_isin_map(fixed, floaters) == {
        "RU000FIXED01": "Фикс-эмитент", "RU000FLOAT01": "Флоатер-эмитент"}


def test_build_takes_worst_agency():
    """Рейтинг бумаги = ХУДШАЯ оценка: инвестор ограничен низшей."""
    fixed = [[None] * 40]
    fixed[0][26], fixed[0][39] = "Э", "RU000TEST001"
    m = build([ch("Э", "АКРА", "2026-01-10", "AA+"),
               ch("Э", "Эксперт РА", "2026-01-10", "AA-")], fixed, [])
    assert m["RU000TEST001"]["rating"] == "AA-"
    assert m["RU000TEST001"]["bucket"] == "AA"
    assert set(m["RU000TEST001"]["agencies"]) == {"acra", "expert"}


def test_build_skips_unrated_issuer():
    fixed = [[None] * 40]
    fixed[0][26], fixed[0][39] = "Безрейтинговый", "RU000TEST002"
    assert build([ch("Другой", "АКРА", "2026-01-10", "A")], fixed, []) == {}


# --- шкала (services/ratings) ---

def test_rating_min_picks_worst():
    assert rating_min("AA+", "AA-") == "AA-"
    assert rating_min("A", None, "") == "A"
    assert rating_min(None, "") == ""


def test_rank_orders_steps():
    assert rating_rank("AAA") < rating_rank("AA+") < rating_rank("AA") < rating_rank("AA-")
    assert rating_rank("Withdrawn") is None


def test_cyrillic_rating_normalized():
    """«АА-» русскими буквами раньше становилось NR — бумага с AA- уезжала
    к безрейтинговым."""
    assert rating_norm("АА-") == "AA-"
    assert rating_to_bucket("А+") == "A"


def test_agency_name_is_not_mistaken_for_rating():
    """«АКРА» после транслитерации похожа на «AA» — имя агентства не должно
    просачиваться в значение."""
    assert rating_norm("АКРА AA-(RU)") == "AA-"
    assert rating_norm("НКР A") == "A"
    assert rating_norm("АКРА") == ""


def test_two_ratings_in_one_string_take_worst():
    assert rating_norm("AA (AA-)") == "AA-"
    assert rating_norm("A/BBB") == "BBB"


# --- приоритет слоёв: bondresearch выше corpbonds ---

import pytest

from services import ratings as rt


@pytest.fixture
def two_layers(monkeypatch):
    """corpbonds знает обе бумаги, bondresearch — только первую."""
    monkeypatch.setattr(rt, "_cache", {
        "RU000BR00001": {"bucket": "AA", "ts": 0},
        "RU000CB00002": {"bucket": "BBB", "ts": 0},
    })
    monkeypatch.setattr(rt, "_save", lambda: None)

    class FakeBR:
        MAP = {"RU000BR00001": {"rating": "AA-", "bucket": "AA"}}

        @staticmethod
        def ratings_of(isin):
            return FakeBR.MAP.get(isin)

        @staticmethod
        def bucket_map(isins):
            return {i: FakeBR.MAP[i]["bucket"] for i in isins if i in FakeBR.MAP}

    monkeypatch.setattr(rt, "_br", lambda: FakeBR)
    return FakeBR


def test_read_prefers_bondresearch(two_layers):
    assert rt.bucket_of("RU000BR00001") == "AA"     # слой знает
    assert rt.bucket_of("RU000CB00002") == "BBB"    # фолбэк на corpbonds
    assert rt.bucket_of("RU000NOBODY9") is None


def test_bucket_map_fixed_layers_and_ofz(two_layers):
    got = rt.bucket_map_fixed([("RU000BR00001", "corp"), ("RU000CB00002", "corp"),
                               ("SU26238RMFS4", "ofz")])
    assert got == {"RU000BR00001": "AA", "RU000CB00002": "BBB", "SU26238RMFS4": "AAA"}


@pytest.mark.asyncio
async def test_drain_does_not_overwrite_bondresearch(two_layers, monkeypatch):
    """Дрейн corpbonds НЕ трогает колонку rating у бумаг, чей рейтинг ведёт
    приоритетный слой: иначе два писателя гоняются за колонкой, по которой
    работают фильтры, и рейтинг мигает между прогонами."""
    written = {}

    # Патчим ФУНКЦИИ реального модуля, а не подсовываем фейк через sys.modules
    # или атрибут пакета: и то и другое зависит от того, был ли модуль уже
    # импортирован, и тест проходил ровно в одном из двух режимов прогона.
    from services import instruments_registry as reg_mod
    monkeypatch.setattr(reg_mod, "rating_checked_map", lambda isins: {})
    monkeypatch.setattr(reg_mod, "ratings_map", lambda isins: {})
    monkeypatch.setattr(reg_mod, "get", lambda isin: {"isin": isin})
    monkeypatch.setattr(reg_mod, "set_rating",
                        lambda isin, rating: written.__setitem__(isin, rating))

    async def fake_corpbonds(isin, client=None):
        return {"rating_raw": "AA"}          # corpbonds отдаёт грубый грейд

    import services.enrich_corpbonds as ec
    monkeypatch.setattr(ec, "fetch_corpbonds", fake_corpbonds)

    await rt.refresh(["RU000BR00001", "RU000CB00002"], cap=10, delay=0)

    assert "RU000BR00001" not in written    # ведёт bondresearch — не трогаем
    assert written.get("RU000CB00002") == "AA"   # своя бумага — пишем как раньше
