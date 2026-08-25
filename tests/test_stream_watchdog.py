"""Сторож тихого отказа стримов: находки доходят до людей, а не только в лог.

Тихий отказ тем и коварен, что снаружи неотличим от спокойного рынка: сигналов
нет — и логи смотреть незачем, ведь «сегодня тихо». Поэтому проверяется именно
доставка: первая поломка звонит сразу, известная молчит до истечения окна,
починка даёт отбой ровно один раз.
"""
import asyncio

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
