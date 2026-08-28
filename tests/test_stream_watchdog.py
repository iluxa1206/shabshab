"""Сторож тихого отказа стримов: находки доходят до людей, а не только в лог.

Тихий отказ тем и коварен, что снаружи неотличим от спокойного рынка: сигналов
нет — и логи смотреть незачем, ведь «сегодня тихо». Поэтому проверяется именно
доставка: первая поломка звонит сразу, известная молчит до истечения окна,
починка даёт отбой ровно один раз.
"""
import asyncio
import time

import pytest

import api.main as m


@pytest.fixture()
def sent(monkeypatch):
    log = []

    async def fake(text):
        log.append(text)
        return 1

    monkeypatch.setattr("services.tg_notify.notify_admins", fake)
    m._stream_alerted.clear()
    return log


def _alert(problems):
    asyncio.run(m._stream_alert(problems))


def test_new_problem_alerts_immediately(sent):
    _alert({"books": "стаканы — 0 бумаг на сокетах"})
    assert len(sent) == 1
    assert "Стрим молчит" in sent[0] and "0 бумаг" in sent[0]
    # почему это важно, а не просто «что-то не так»
    assert "сигналы не придут" in sent[0]


def test_same_problem_does_not_repeat(sent):
    _alert({"books": "стаканы — 0 бумаг на сокетах"})
    _alert({"books": "стаканы — 0 бумаг на сокетах"})
    assert len(sent) == 1, "чинят с первого сообщения, поток превращает тревогу в фон"


def test_repeat_after_window(sent, monkeypatch):
    monkeypatch.setattr(m, "STREAM_ALERT_REPEAT_MIN", 0.0)
    _alert({"books": "стаканы — 0 бумаг на сокетах"})
    _alert({"books": "стаканы — 0 бумаг на сокетах"})
    assert len(sent) == 2, "окно истекло — напоминаем, отказ ведь не прошёл"


def test_second_problem_alerts_even_while_first_stands(sent):
    _alert({"books": "стаканы — 0 бумаг на сокетах"})
    _alert({"books": "стаканы — 0 бумаг на сокетах",
            "trades": "сделки — 0 бумаг на сокетах"})
    assert len(sent) == 2 and "сделки" in sent[1]
    assert "стаканы" not in sent[1], "о старой поломке второй раз не пишем"


def test_recovery_reports_once(sent):
    _alert({"books": "стаканы — 0 бумаг на сокетах"})
    _alert({})
    _alert({})
    assert len(sent) == 2 and "ожили" in sent[1]


def test_quiet_start_says_nothing(sent):
    """Отбой без предшествующей тревоги никому не нужен."""
    _alert({})
    assert sent == []


def test_failed_delivery_is_retried_next_tick(sent, monkeypatch):
    """Не доставили — не считаем сообщённым: иначе единственная сетевая ошибка
    похоронила бы предупреждение до конца дня."""
    async def dead(text):
        return 0

    monkeypatch.setattr("services.tg_notify.notify_admins", dead)
    _alert({"books": "стаканы — 0 бумаг на сокетах"})

    async def alive(text):
        sent.append(text)
        return 1

    monkeypatch.setattr("services.tg_notify.notify_admins", alive)
    _alert({"books": "стаканы — 0 бумаг на сокетах"})
    assert len(sent) == 1


# --- сторож диска и резервных копий ---

@pytest.fixture()
def disk(tmp_path, monkeypatch):
    """Подсовывает сторожу свою базу, свою папку копий и своё свободное место."""
    import shutil
    db = tmp_path / "portfolio.db"
    db.write_bytes(b"x" * 1000)
    (tmp_path / "backups").mkdir()
    monkeypatch.setattr("services.portfolio_db.DB_PATH", db)
    m._disk_alerted.clear()

    state = {"free": 10_000}
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: type("U", (), {"free": state["free"]})())
    return tmp_path, state


def _backup(tmp_path, age_hours: float, name="portfolio-20260825-0130.db.gz"):
    import os
    f = tmp_path / "backups" / name
    f.write_bytes(b"gz")
    old = time.time() - age_hours * 3600
    os.utime(f, (old, old))
    return f


def test_disk_ok_when_space_and_backup_fresh(disk):
    tmp_path, _ = disk
    _backup(tmp_path, age_hours=2)
    assert m._disk_problems() == {}


def test_disk_warns_before_maintenance_starves(disk):
    """Тревога поднимается ЗАРАНЕЕ: места должно хватать не «вообще», а на
    VACUUM и бэкап, каждый из которых требует места в размер базы."""
    tmp_path, state = disk
    _backup(tmp_path, age_hours=2)
    state["free"] = 1400            # база 1000 Б, нужно 1500 при ratio 1.5
    p = m._disk_problems()
    assert "space" in p and "перестанет ужиматься" in p["space"]


def test_stale_backup_is_a_problem(disk):
    """Проверяем результат, а не механику: так ловится и упавший крон, и отказ
    по месту, и битый файл, не доживший до ротации."""
    tmp_path, _ = disk
    _backup(tmp_path, age_hours=50)
    p = m._disk_problems()
    assert "backup" in p and "крон бэкапа не отработал" in p["backup"]


def test_missing_backups_are_a_problem(disk):
    assert m._disk_problems()["backup"] == "резервных копий базы нет вовсе"


def test_disk_alert_shares_dedup_with_streams(disk, sent):
    """У дискового сторожа своя память тревог: беда с местом не должна глушить
    сообщение о молчащем стриме и наоборот."""
    tmp_path, _ = disk
    asyncio.run(m._disk_alert({"space": "мало места"}))
    asyncio.run(m._stream_alert({"books": "стаканы — 0 бумаг на сокетах"}))
    assert len(sent) == 2
    assert "Диск и копии" in sent[0] and "Стрим молчит" in sent[1]


# ── свежесть ленты: сокет живой ≠ сделки доезжают вовремя ──────────────────

def test_feed_lag_alert_says_what_it_costs(monkeypatch):
    """Отставание ленты — отдельная тревога: поток есть, но сигналы опаздывают."""
    log = []

    async def fake(text):
        log.append(text)
        return 1

    monkeypatch.setattr("services.tg_notify.notify_admins", fake)
    m._feed_alerted.clear()
    asyncio.run(m._feed_alert({"capture": "живьём поймано 3 из 40 крупных сделок"}))
    assert len(log) == 1
    assert "Лента отстаёт" in log[0] and "3 из 40" in log[0]
    assert "+15 мин" in log[0], "человек должен видеть цену отставания"


def test_live_capture_counts_only_resolved_window(tmp_path, monkeypatch):
    """Доля живых считается по разрешившемуся окну: сделку минутной давности
    ISS ещё не привозил, и она бы «живой» стала просто по отсутствию копии."""
    import importlib
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.block_trades as bt
    importlib.reload(bt)

    from datetime import datetime, timedelta
    now = time.time()

    def ts(sec_ago):
        return (datetime.now(bt._MSK) - timedelta(seconds=sec_ago)).strftime(
            "%Y-%m-%d %H:%M:%S")

    rows = [
        # (сделка, запись) — живая: записана через 2 секунды
        (1, ts(1800), now - 1800 + 2),
        # доехала дрейном ISS: 15 минут спустя
        (2, ts(2400), now - 2400 + 900),
        # свежая, окно ещё не разрешилось — в счёт не идёт
        (3, ts(60), now - 58),
    ]
    with pdb._connect() as c:
        c.executemany(
            "INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,qty,"
            "value,side,ins_at) VALUES(?,?,?,?,'bonds','TQCB',100.0,1000,?,'buy',?)",
            [(tid, "RU000FLOAT01", "RU000FLOAT01", t, 5_000_000, int(ins))
             for tid, t, ins in rows])

    cap = bt.live_capture(minutes=60)
    assert cap["total"] == 2 and cap["live"] == 1
    assert cap["ratio"] == 0.5


# ── подтверждение и причина ──────────────────────────────────────────────────

def test_single_bad_tick_is_not_an_alert():
    """Моргнувшая подписка — не отказ: сокеты рвутся и встают за секунды, а
    холодный старт штатно проходит через «0 бумаг»."""
    seen: dict = {}
    p = {"books": "стаканы — 0 бумаг на сокетах"}
    assert m._confirmed(p, seen, 2) == {}
    assert m._confirmed(p, seen, 2) == p, "второй такт подряд — уже отказ"


def test_gap_resets_confirmation():
    seen: dict = {}
    p = {"books": "стаканы — 0 бумаг на сокетах"}
    m._confirmed(p, seen, 2)
    m._confirmed({}, seen, 2)          # починилось
    assert m._confirmed(p, seen, 2) == {}, "счётчик начинается заново"


def test_pool_hint_separates_causes():
    """«0 бумаг» от неподнявшегося пула и от мёртвых сокетов лечится по-разному."""
    assert "не собрал шарды" in m._pool_hint({"pool": {"err": "ISS 502"}})
    assert "ISS 502" in m._pool_hint({"pool": {"err": "ISS 502"}})
    assert "ни разу" in m._pool_hint({"pool": {"built": 0.0}})
    assert "токен/сеть/брокер" in m._pool_hint({"pool": {"built": 1.0, "shards": 13}})


# ── восстановление без потери данных ─────────────────────────────────────────

def test_failed_flush_returns_ticks_to_buffer(monkeypatch):
    """Пачка уходит из буфера ДО записи: ошибка записи не должна её съедать —
    по бумагам вне юниверса такую сделку не вернёт никто, кроме ISS с планкой
    1 млн и 15 минутами."""
    from services import trades_stream as ts

    ts._buf.clear()
    ts._buf["RU000A1"] = [{"id": 1, "price": 100.0, "qty": 1, "time": "", "val": 5.0}]

    async def boom(*a, **kw):
        raise RuntimeError("диск кончился")

    monkeypatch.setattr(ts, "_faces_map", boom)
    assert asyncio.run(ts._flush_once()) == 0
    assert [t["id"] for t in ts._buf["RU000A1"]] == [1]
    ts._buf.clear()


def test_requeue_keeps_order_and_caps(monkeypatch):
    """Вернувшиеся тики старше тех, что прилетели во время записи, — значит
    вперёд. Потолок роняет самое старое и об этом говорит, а не растёт до OOM."""
    from services import trades_stream as ts

    ts._buf.clear()
    ts._buf["X"] = [{"id": 3}]
    ts._requeue([("X", [{"id": 1}, {"id": 2}])])
    assert [t["id"] for t in ts._buf["X"]] == [1, 2, 3]

    monkeypatch.setattr(ts, "_REQUEUE_MAX", 4)
    ts._requeue([("X", [{"id": 0}, {"id": -1}])])
    assert len(ts._buf["X"]) == 4, "потолок держит буфер конечным"
    ts._buf.clear()


def test_dead_shard_alerts_even_when_neighbours_live():
    """Мёртвый шард уносит 150–250 бумаг, а общий счётчик бумаг на сокетах при
    этом не ноль — раньше сторож видел живых соседей и молчал."""
    us = {"streamed": 450, "shards": {"total": 4, "up": 2},
          "depth_shards": {"total": 4, "up": 4}}
    ts = {"streamed": 3000, "shards": {"total": 13, "up": 13}}
    problems = {}
    for k, sh, what in (("books_shards", us.get("shards") or {}, "котировок"),
                        ("depth_shards", us.get("depth_shards") or {}, "стаканов"),
                        ("trades_shards", ts.get("shards") or {}, "сделок")):
        tot, up = sh.get("total") or 0, sh.get("up") or 0
        if tot and up < m.SHARD_UP_MIN * tot:
            problems[k] = f"сокетов {what}: живо {up} из {tot}"
    assert set(problems) == {"books_shards"}


def test_daemon_restarts_after_crash():
    """Воркер, упавший вне своего внутреннего try, обязан вернуться сам: голый
    create_task убивал его насмерть и молча."""
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("упал")

    real_sleep = asyncio.sleep

    async def no_wait(*_a, **_k):       # бэкофф демона не должен держать тест
        await real_sleep(0)

    async def run():
        m._daemon_restarts.clear()
        m.asyncio.sleep = no_wait
        try:
            task = asyncio.create_task(m._supervise("test", flaky))
            for _ in range(200):        # ждём перезапуск, не завися от таймера
                await real_sleep(0)
                if task.done():
                    break
            task.cancel()
        finally:
            m.asyncio.sleep = real_sleep

    asyncio.run(run())
    assert len(calls) == 2, "после падения демон поднялся заново"
    assert m._daemon_restarts["test"] == 1
