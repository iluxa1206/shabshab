"""Аукционы ОФЗ Минфина (services/ofz_auctions): парсеры на НАСТОЯЩИХ файлах
Минфина, план/факт на синтетике, резолв кода выпуска в SECID.

Фикстуры — реальные: годовой xlsx итогов 2026 «по состоянию на 10.09.2026»
(66 строк + «Итого») и HTML графика аукционов на III квартал 2026 (14 дат,
корзины «до 10 лет включительно 900» и «от 10 лет 600»). Сети нет.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services import ofz_auctions as oa  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
PLAN_URL = ("https://minfin.gov.ru/ru/perfomance/public_debt/internal/operations/ofz/auction"
            "?id_65=316929-grafik_auktsionov_po_razmeshcheniyu_obligatsii_federalnykh_zaimov"
            "_na_iii_kvartal_2026_goda")


@pytest.fixture(scope="module")
def rows():
    with open(os.path.join(FIX, "minfin_auction_results_2026.xlsx"), "rb") as f:
        return oa.parse_results_xlsx(f.read())


@pytest.fixture(scope="module")
def plan():
    with open(os.path.join(FIX, "minfin_plan_2026q3.html"), encoding="utf-8") as f:
        return oa.parse_plan_html(f.read(), PLAN_URL)


# ────────────────────────── xlsx итогов ──────────────────────────

def test_xlsx_rows_and_statuses(rows):
    assert len(rows) == 66                           # «Итого» не строка
    by = {}
    for r in rows:
        by[(r["fmt"], r["status"])] = by.get((r["fmt"], r["status"]), 0) + 1
    assert by[("drpa", "drpa")] == 15
    assert by[("auction", "failed")] == 2
    assert by[("auction", "ok")] == 49
    assert all(r["date"] >= "2026-01-14" and r["date"] <= "2026-09-09" for r in rows)


def test_xlsx_failed_auction_nulls(rows):
    """Несостоявшийся 29028 15.07.2026: '-****' → NULL, размещено 0, спрос есть."""
    f = next(r for r in rows if r["code"] == "29028RMFS" and r["date"] == "2026-07-15")
    assert f["status"] == "failed" and f["fmt"] == "auction"
    assert f["sec_type"] == "ОФЗ-ПК"
    assert f["cut_price"] is None and f["wap_price"] is None
    assert f["cut_yield"] is None and f["wap_yield"] is None
    assert f["placed_mln"] == 0 and f["demand_mln"] == pytest.approx(144290.257)
    assert oa.proceeds_113(f["placed_mln"], f["wap_price"]) == 0.0


def test_xlsx_drpa_row(rows):
    """ДРПА: спрос и коэффициент '-' → NULL, цена = средневзвес аукциона."""
    d = next(r for r in rows if r["fmt"] == "drpa" and r["date"] == "2026-01-21")
    assert d["code"] == "26230RMFS" and d["status"] == "drpa"
    assert d["demand_mln"] is None and d["fill_ratio"] is None
    assert d["cut_price"] == d["wap_price"] == pytest.approx(61.7155)
    assert d["placed_mln"] == pytest.approx(9145.295)


def test_xlsx_pk_yield_zero_is_null(rows):
    """У ОФЗ-ПК доходность «не рассчитывается»: 0 в файле → NULL, а не 0 %."""
    pk = next(r for r in rows if r["code"] == "29031RMFS")
    assert pk["status"] == "ok" and pk["wap_price"] == pytest.approx(92.5122)
    assert pk["cut_yield"] is None and pk["wap_yield"] is None
    assert pk["placed_mln"] == pytest.approx(999999.989)


def test_xlsx_first_row_fields(rows):
    r = rows[0]
    assert r["date"] == "2026-01-14" and r["code"] == "26253RMFS" and r["sec_type"] == "ОФЗ-ПД"
    assert r["maturity"] == "2038-10-06" and r["days_to_mat"] == 4648
    assert r["offered_mln"] == pytest.approx(674231.07)
    assert r["cut_price"] == pytest.approx(91.4983) and r["wap_price"] == pytest.approx(91.52)
    assert r["wap_yield"] == pytest.approx(14.99)
    assert r["fill_ratio"] == pytest.approx(0.3032, abs=1e-4)


def test_xlsx_old_layout_without_format_column():
    """Файлы 2021–2023: 14 колонок, «Дата аукциона», без «Формат» — все строки
    аукционы; в остальном те же поля со сдвигом на одну колонку."""
    import io
    import openpyxl
    from datetime import datetime
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Результаты проведенных аукционов"])
    ws.append(["Дата аукциона", "Код  выпуска", "Тип бумаги*", "Дата погашения", "Дней до погашения",
               "Объем предложения", "Цена отсечения", "Цена средневзвешенная", "Доходность по цене отсечения**",
               "Доходность по средневзве- шенной цене**", "Совокупный объем спроса по номиналу",
               "Объем размещения по номиналу", "Объем выручки", "Коэффициент удовлетворения спроса на аукционе"])
    ws.append([None] * 14)
    ws.append([datetime(2021, 1, 13), "26236RMFS", "ОФЗ-ПД", datetime(2028, 5, 17), 2681, 478585.398,
               99.22, 99.2736, 5.91, 5.91, 15300.929, 10150.457, 10176.603, 0.6634])
    ws.append([datetime(2021, 1, 13), "52003RMFS", "ОФЗ-ИН", datetime(2030, 7, 17), 3472, 141594.3,
               " -****", " -****", " -****", " -****", 3112.6, 0, 0, 0])
    ws.append(["Итого", "", "", "", "", "", "", "", "", "", 18413.5, 10150.457, 10176.603, 0.55])
    buf = io.BytesIO()
    wb.save(buf)
    rows = oa.parse_results_xlsx(buf.getvalue())
    assert len(rows) == 2
    assert rows[0]["fmt"] == "auction" and rows[0]["status"] == "ok"
    assert rows[0]["code"] == "26236RMFS" and rows[0]["days_to_mat"] == 2681
    assert rows[0]["cut_price"] == pytest.approx(99.22) and rows[0]["placed_mln"] == pytest.approx(10150.457)
    assert rows[0]["fill_ratio"] == pytest.approx(0.6634)
    assert rows[1]["status"] == "failed" and rows[1]["sec_type"] == "ОФЗ-ИН" and rows[1]["wap_yield"] is None


def test_num_normalization():
    assert oa._num(" -****") is None and oa._num("-") is None and oa._num("") is None
    assert oa._num("1 234,5") == 1234.5 and oa._num(7) == 7.0 and oa._num(None) is None


# ────────────────────────── HTML плана ──────────────────────────

def test_plan_html(plan):
    assert plan["quarter"] == "2026Q3" and plan["src_id"] == 316929
    assert len(plan["dates"]) == 14
    assert plan["dates"][0] == "2026-07-01" and plan["dates"][-1] == "2026-09-30"
    assert "2026-07-15" in plan["dates"]
    # чужие даты страницы (публикация 30.06.2026 и т.п.) не попали
    assert all(d.startswith("2026-0") and "07-01" <= d[5:] <= "09-30" for d in plan["dates"])
    assert plan["buckets"] == [
        {"bucket": "до 10 лет включительно", "lo_y": None, "hi_y": 10.0, "amount_bln": 900.0},
        {"bucket": "от 10 лет", "lo_y": 10.0, "hi_y": None, "amount_bln": 600.0},
    ]
    assert sum(b["amount_bln"] for b in plan["buckets"]) == 1500.0


def test_parse_bucket_variants():
    assert oa.parse_bucket("до 5 лет") == (None, 5.0)
    assert oa.parse_bucket("от 5 до 10 лет") == (5.0, 10.0)
    assert oa.parse_bucket("от 5 до 10 лет включительно") == (5.0, 10.0)
    assert oa.parse_bucket("от 10 лет") == (10.0, None)
    assert oa.parse_bucket("свыше 10 лет") == (10.0, None)
    assert oa.parse_bucket("Срок до погашения размещаемых ОФЗ") is None
    assert oa.parse_bucket("Итого") is None


def test_plans_index_prefers_bigger_id():
    html = ('<a href="/x/auction?id_65=313000-grafik_auktsionov_na_iv_kvartal_2025_goda">a</a>'
            '<a href="/x/auction?id_65=313991-grafik_auktsionov_na_iv_kvartal_2025_goda_utochnennyi">b</a>'
            '<a href="/x/auction?id_65=316929-grafik_auktsionov_na_iii_kvartal_2026_goda">c</a>')
    idx = oa.parse_plans_index(html)
    assert idx["2025Q4"][0] == 313991 and idx["2025Q4"][1].endswith("_utochnennyi")
    assert idx["2026Q3"][0] == 316929


def test_results_index_latest_per_year():
    html = ('<a href="/common/upload/library/2026/09/main/INTERNET_Auction_Results_rus_2026_20260910.xlsx">'
            '<a href="/common/upload/library/2026/08/main/INTERNET_Auction_Results_rus_2026_20260827.xlsx">'
            '<a href="/common/upload/library/2026/01/main/INTERNET_Auction_Results_rus_2025_20251231.xlsx">')
    idx = oa.parse_results_index(html)
    assert idx[2026][1] == "INTERNET_Auction_Results_rus_2026_20260910.xlsx"
    assert idx[2026][0].startswith("https://minfin.gov.ru/common/upload/")
    assert idx[2025][1].endswith("2025_20251231.xlsx")


# ────────────────────────── план/факт ──────────────────────────

def _row(date, code, placed, wap, days, fmt="auction", sec_type="ОФЗ-ПД", status="ok",
         demand=None, wap_yield=None):
    return {"date": date, "code": code, "fmt": fmt, "sec_type": sec_type, "status": status,
            "days_to_mat": days, "placed_mln": placed, "wap_price": wap, "demand_mln": demand,
            "wap_yield": wap_yield, "isin": None, "secid": None}


def test_plan_fact_synthetic():
    buckets = [{"bucket": "до 10 лет включительно", "lo_y": None, "hi_y": 10.0, "amount_bln": 900.0},
               {"bucket": "от 10 лет", "lo_y": 10.0, "hi_y": None, "amount_bln": 600.0}]
    dates = ["2026-07-01", "2026-07-08", "2026-07-15", "2026-07-22"]
    rows = [
        # короткий по 90: 100 млрд номинала → 90 по 113-й
        _row("2026-07-01", "26251RMFS", 100_000, 90.0, 365 * 4, demand=200_000, wap_yield=15.0),
        # длинный с премией к номиналу: 113-я = номинал (min(цена,100))
        _row("2026-07-01", "26238RMFS", 50_000, 105.0, 365 * 15),
        # ДРПА того же выпуска — тоже привлечение
        _row("2026-07-01", "26238RMFS", 10_000, 105.0, 365 * 15, fmt="drpa", status="drpa"),
        # ровно 10 лет — включительно, короткая корзина
        _row("2026-07-08", "26230RMFS", 20_000, 50.0, int(10 * 365.25)),
        # несостоявшийся
        _row("2026-07-08", "29028RMFS", 0, None, 4000, sec_type="ОФЗ-ПК", status="failed", demand=1000),
    ]
    pf = oa.plan_fact_calc("2026Q3", buckets, dates, rows, today="2026-07-10")
    assert pf["plan_bln"] == 1500.0
    assert pf["fact_nominal_bln"] == pytest.approx(180.0)
    assert pf["fact_113_bln"] == pytest.approx(90 + 50 + 10 + 10)        # 160
    assert pf["pct_113"] == pytest.approx(160 / 1500 * 100, abs=0.05)
    assert pf["auctions_held"] == 2 and pf["auctions_planned"] == 4
    assert pf["dates_passed"] == 2 and pf["remaining"] == 2 and pf["next_date"] == "2026-07-15"
    assert pf["need_per_auction_bln"] == pytest.approx((1500 - 160) / 2)
    assert pf["avg_per_auction_bln"] == pytest.approx(80.0)
    assert pf["pace"] == pytest.approx(160 / (1500 * 2 / 4), abs=1e-3)
    assert pf["n_failed"] == 1 and pf["n_drpa"] == 1
    short, long_ = pf["buckets"]
    assert short["bucket"] == "до 10 лет включительно"
    assert short["fact_113_bln"] == pytest.approx(90 + 10) and short["plan_bln"] == 900
    assert long_["fact_113_bln"] == pytest.approx(60) and long_["fact_nominal_bln"] == pytest.approx(60)
    assert long_["pct_113"] == pytest.approx(10.0)
    # ряд по датам: будущие даты графика присутствуют без факта, прямая плана равномерна
    bd = {d["date"]: d for d in pf["by_date"]}
    assert bd["2026-07-15"]["held"] is False and bd["2026-07-15"]["cum_113_bln"] is None
    assert bd["2026-07-08"]["cum_113_bln"] == pytest.approx(160) and bd["2026-07-08"]["n_failed"] == 1
    assert bd["2026-07-22"]["plan_line_bln"] == pytest.approx(1500.0)
    assert bd["2026-07-01"]["plan_line_bln"] == pytest.approx(375.0)


def test_plan_fact_today_is_auction_day_without_results():
    """День аукциона, итогов ещё нет: дата считается оставшейся и «следующей»."""
    dates = ["2026-07-01", "2026-07-08"]
    rows = [_row("2026-07-01", "26251RMFS", 100_000, 90.0, 1000)]
    pf = oa.plan_fact_calc("2026Q3", [{"bucket": "до 10 лет", "lo_y": None, "hi_y": 10, "amount_bln": 100}],
                           dates, rows, today="2026-07-08")
    assert pf["next_date"] == "2026-07-08" and pf["remaining"] == 1 and pf["dates_passed"] == 1


def test_plan_fact_without_plan_only_fact():
    rows = [_row("2023-02-01", "26238RMFS", 30_000, 60.0, 6000)]
    pf = oa.plan_fact_calc("2023Q1", [], [], rows, today="2026-09-15")
    assert pf["has_plan"] is False and pf["plan_bln"] is None and pf["pct_113"] is None
    assert pf["fact_113_bln"] == pytest.approx(18.0) and pf["buckets"] == []
    assert pf["by_date"][0]["cum_113_bln"] == pytest.approx(18.0)


def test_bucket_for_inclusive_boundary():
    b = [{"bucket": "a", "lo_y": None, "hi_y": 10.0}, {"bucket": "b", "lo_y": 10.0, "hi_y": None}]
    assert oa.bucket_for(10.0, b) == "a" and oa.bucket_for(10.01, b) == "b"
    assert oa.bucket_for(None, b) is None


def test_quarter_helpers():
    assert oa.quarter_of("2026-07-15") == "2026Q3" and oa.quarter_of("2026-12-31") == "2026Q4"
    assert oa.quarter_bounds("2026Q4") == ("2026-10-01", "2026-12-31")
    assert oa.quarter_bounds("2026Q1") == ("2026-01-01", "2026-03-31")
    assert oa.next_quarter("2026Q4") == "2027Q1" and oa.next_quarter("2026Q2") == "2026Q3"


def test_series_and_stats_on_fixture(rows):
    s = oa.series_calc(rows)
    last = s[-1]
    assert last["date"] == "2026-09-09" and last["n"] == 2 and last["n_drpa"] == 1
    assert last["placed_mln"] == pytest.approx(51496.169 + 35215.268 + 3949)
    # кумулятив 113-й внутри квартала: III кв. 2026 = 6 строк, заметно меньше номинала
    q3 = [x for x in s if x["quarter"] == "2026Q3"]
    nominal = sum(x["placed_mln"] for x in q3)
    assert q3[-1]["cum_113_mln"] < nominal * 0.95
    assert q3[-1]["cum_113_mln"] == pytest.approx(996707.56, abs=1)
    # первый аукцион IV кв. сбросил бы кумулятив — здесь проверяем сброс на границе кварталов
    q2_last = [x for x in s if x["quarter"] == "2026Q2"][-1]
    assert q3[0]["cum_113_mln"] < q2_last["cum_113_mln"]
    st = oa.stats_calc(rows)
    assert st["n_rows"] == 66 and st["n_failed"] == 2 and st["n_drpa"] == 15
    assert st["placed_mln"] == pytest.approx(4353442.286, abs=1)
    assert st["demand_mln"] == pytest.approx(6971845.204, abs=1)
    assert st["bid_cover_median"] > 1 and len(st["top"]) == 5
    assert st["top"][0]["code"] == "29031RMFS"
    assert st["by_type"]["ОФЗ-ПК"]["share"] == pytest.approx(0.23, abs=0.01)
    assert st["premium_avg_bps"] is None            # вторички в синтетике нет


def test_enrich_premium_uses_prior_day_only():
    r = _row("2026-09-09", "26218RMFS", 51_496, 77.92, 1834, demand=91_823, wap_yield=15.45)
    r["isin"] = "RU000A0ZYUB4"
    out = oa.enrich([dict(r)], {("RU000A0ZYUB4", "2026-09-09"): 15.20})[0]
    assert out["premium_bps"] == pytest.approx(25.0)
    assert out["bid_cover"] == pytest.approx(91_823 / 51_496, abs=0.01)
    assert out["proceeds_113_mln"] == pytest.approx(51_496 * 0.7792)
    assert out["term_y"] == pytest.approx(5.02, abs=0.01)
    # без точки вторички — None, не 0
    assert oa.enrich([dict(r)], {})[0]["premium_bps"] is None
    # у ПК премии не бывает: доходности нет
    pk = dict(r, sec_type="ОФЗ-ПК", wap_yield=None)
    assert oa.enrich([pk], {("RU000A0ZYUB4", "2026-09-09"): 15.20})[0]["premium_bps"] is None


# ────────────────────────── резолв SECID через sec_ref ──────────────────────────

def test_resolve_secids_like(tmp_path, monkeypatch, rows):
    import services.portfolio_db as pdb
    monkeypatch.setattr(pdb, "DB_PATH", tmp_path / "portfolio.db")
    pdb.init_db()
    with pdb._connect() as c:
        c.executemany("INSERT INTO sec_ref(secid,isin,shortname,type,is_traded,mat_date) VALUES(?,?,?,?,?,?)", [
            ("SU26253RMFS3", "RU000A10D517", "ОФЗ 26253", "ofz_bond", 1, "2038-10-06"),
            ("SU29028RMFS6", "RU000A10D4Z9", "ОФЗ 29028", "ofz_bond", 1, "2039-10-22"),
            # двусмысленность: два кандидата под один код — побеждает совпавшая дата погашения
            ("SU26230RMFS1", "RU000A100EF5", "ОФЗ 26230", "ofz_bond", 0, "2039-03-16"),
            ("SU26230RMFS9", "RU000FAKE000", "ОФЗ 26230x", "ofz_bond", 1, "2030-01-01"),
        ])
    n = oa.upsert_results(rows, "test.xlsx")
    assert n == 66
    res = oa.resolve_secids()
    assert res["resolved"] >= 3 and res["ambiguous"] == 1
    with pdb._connect() as c:
        got = {r["code"]: (r["secid"], r["isin"]) for r in c.execute(
            "SELECT DISTINCT code, secid, isin FROM ofz_auction WHERE secid IS NOT NULL")}
    assert got["26253RMFS"] == ("SU26253RMFS3", "RU000A10D517")
    assert got["29028RMFS"] == ("SU29028RMFS6", "RU000A10D4Z9")
    assert got["26230RMFS"] == ("SU26230RMFS1", "RU000A100EF5")
    # повторный upsert того же файла secid не стирает
    oa.upsert_results(rows, "test2.xlsx")
    with pdb._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM ofz_auction WHERE secid IS NULL AND code='26253RMFS'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM ofz_auction").fetchone()[0] == 66
    # план/факт из базы: III кв. 2026 по фикстуре
    with open(os.path.join(FIX, "minfin_plan_2026q3.html"), encoding="utf-8") as f:
        doc = oa.parse_plan_html(f.read(), PLAN_URL)
    assert oa.save_plan(doc) == 2
    pf = oa.plan_fact("2026Q3", today="2026-09-15")
    assert pf["plan_bln"] == 1500.0 and pf["auctions_planned"] == 14
    assert pf["fact_nominal_bln"] == pytest.approx(1101.025, abs=0.01)
    assert pf["fact_113_bln"] == pytest.approx(996.708, abs=0.01)
    assert pf["fact_113_bln"] < pf["fact_nominal_bln"]
    assert pf["next_date"] == "2026-09-16" and pf["remaining"] == 3
    qs = oa.quarters()
    assert qs[0]["quarter"] == "2026Q3" and qs[0]["has_plan"] is True
    assert any(q["quarter"] == "2026Q1" and not q["has_plan"] for q in qs)
    iss = oa.by_issue()
    top = next(i for i in iss if i["code"] == "26253RMFS")
    assert top["n"] == 6 and top["n_drpa"] == 2 and top["secid"] == "SU26253RMFS3" and top["shortname"] == "ОФЗ 26253"
    assert top["price_min"] < top["price_max"]
    # results с фильтрами
    failed = oa.results("2026-01-01", None, None, None, None, "failed")
    assert {r["code"] for r in failed} == {"26251RMFS", "29028RMFS"}
    one = oa.results(None, None, None, None, "26230RMFS", None)
    assert len(one) == 6 and all(r["code"] == "26230RMFS" for r in one)
    assert oa.stats("2026-07-01", None)["n_rows"] == 6
