"""Группировка рейтингов: чип грейда забирает ступени, мусор — в NR."""
from api.routes.trades import _rating_isins, rating_bucket, rating_norm
from services import ref_data


def test_norm_strips_agency_suffixes():
    for raw in ("ruAA-", "AA-(RU)", "AA-|ru|", "AA-.ru", " aa- "):
        assert rating_norm(raw) == "AA-"


def test_bucket_folds_steps_and_deep_hy():
    assert rating_bucket("AA-") == "AA"
    assert rating_bucket("AA+") == "AA"
    assert rating_bucket("BBB+") == "BBB"
    assert rating_bucket("CCC") == "B"
    assert rating_bucket("D|RU|") == "B"
    # «рейтинга нет»: пусто, отзыв, нераспознанное
    for raw in (None, "", "WITHDRAWN", "мусор"):
        assert rating_bucket(raw) == "NR"


LABELS = {
    "AAp": {"rating": "AA+"}, "AA": {"rating": "AA"}, "AAm": {"rating": "AA-"},
    "AAA": {"rating": "AAA"}, "BBm": {"rating": "BB-"},
    "none": {"rating": None}, "wd": {"rating": "Withdrawn"},
}


def test_grade_chip_takes_whole_group():
    assert sorted(_rating_isins(LABELS, None, ["AA"])) == ["AA", "AAm", "AAp"]


def test_step_pick_takes_only_that_step():
    assert _rating_isins(LABELS, None, ["AA-"]) == ["AAm"]
    assert sorted(_rating_isins(LABELS, None, ["AA-", "AAA"])) == ["AAA", "AAm"]


def test_below_and_nr():
    assert _rating_isins(LABELS, None, ["BELOW"]) == ["BBm"]
    # отозванный рейтинг ловится чипом NR — иначе бумага пропадала при любом выборе
    assert sorted(_rating_isins(LABELS, None, ["NR"])) == ["none", "wd"]


def test_empty_selection_is_no_filter():
    assert _rating_isins(LABELS, None, []) is None


def test_ref_data_rating_rejects_non_scale(tmp_path):
    """В реестр из выгрузки едет только значение шкалы: «Withdrawn» — не рейтинг."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ISIN", "Рейтинг эмитента АКРА", "Рейтинг эмитента Эксперт РА"])
    ws.append(["RU000TEST0010", "Withdrawn", "ruA+"])
    ws.append(["RU000TEST0011", "-", "Withdrawn"])
    p = tmp_path / "bondsearch_02_02_2030.xlsx"
    wb.save(p)
    cb = ref_data.load_cbonds(str(p))
    assert cb["RU000TEST0010"]["rating"] == "A+"
    assert cb["RU000TEST0011"]["rating"] is None


def test_withdrawn_is_not_deep_hy():
    """Отзыв рейтинга — это «рейтинга нет». Прежняя чистка «всё не-[A-D]»
    превращала Withdrawn в «DA» → бакет B: бумага уезжала в самый рисковый грейд."""
    from services.ratings import rating_to_bucket
    assert rating_to_bucket("Withdrawn") == "NR"
    assert rating_to_bucket("АКРА AA-(RU)") == "AA"
    assert rating_to_bucket("НКР A") == "A"


def test_screener_selector_matches_by_grade():
    """Селектор СИГНАЛОВ/бота работает грейдами: «AA» обязан ловить AA±."""
    from services.screener_core import selected
    p = {"ratings": ["AA"], "emitters": [], "isins": []}
    assert selected({"rating": "AA-"}, p) is True
    assert selected({"rating": "AA+"}, p) is True
    assert selected({"rating": "AAA"}, p) is False
    assert selected({"rating": None}, p) is False
