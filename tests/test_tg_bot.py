"""Телеграм-бот: привязка чата к веб-аккаунту, вебхук, форматирование доставки.
Своей настройки у бота нет — сигналы заводятся на сайте."""
import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.tg import _fmt_event
from services import tg_users
from services.tg_notify import _signal_text

UID = 424242
SECRET = "test-secret"


@pytest.fixture()
def db():
    from services import portfolio_db
    from services.portfolio_db import _connect, _lock
    portfolio_db.init_db()
    with _lock, _connect() as c:
        c.execute("DELETE FROM tg_targets WHERE user_email='u@x.ru'")
    yield
    with _lock, _connect() as c:
        c.execute("DELETE FROM tg_users WHERE tg_user_id IN (?, 555)", (UID,))
        c.execute("DELETE FROM tg_targets WHERE user_email='u@x.ru'")


@pytest.fixture()
def client(db, monkeypatch):
    from api.routes import tg as tg_route
    monkeypatch.setenv("TG_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)   # отправка — no-op
    app = FastAPI()
    app.include_router(tg_route.router, prefix="/api/tg")
    return TestClient(app)


def _update(text, uid=UID, username="tester"):
    return {"message": {"text": text, "from": {"id": uid, "username": username},
                        "chat": {"id": uid, "type": "private"}}}


def _post(client, upd, secret=SECRET):
    return client.post("/api/tg/webhook", json=upd,
                       headers={"X-Telegram-Bot-Api-Secret-Token": secret})


# --- привязка ---

def test_start_creates_pending_request(client):
    assert _post(client, _update("/start")).status_code == 200
    row = tg_users.get(UID)
    assert row["status"] == "pending" and row["email"] is None
    assert row["username"] == "tester"
    assert tg_users.is_allowed(UID) is False
    assert tg_users.email_for(UID) is None


def test_any_message_creates_request(client):
    _post(client, _update("привет", uid=555, username=None))
    assert (tg_users.get(555) or {})["status"] == "pending"


def test_wrong_secret_ignored(client):
    _post(client, _update("/start"), secret="nope")
    assert tg_users.get(UID) is None


def test_approve_binds_account(client):
    _post(client, _update("/start"))
    row = tg_users.approve(UID, "U@X.ru", by="admin@x.ru")
    assert row["status"] == "approved" and row["email"] == "u@x.ru"
    assert tg_users.is_allowed(UID) and tg_users.email_for(UID) == "u@x.ru"


def test_chats_for_email_respects_mute_and_status(client):
    _post(client, _update("/start"))
    assert tg_users.chats_for_email("u@x.ru") == []
    tg_users.approve(UID, "u@x.ru", by="admin@x.ru")
    assert [c["tg_user_id"] for c in tg_users.chats_for_email("u@x.ru")] == [UID]
    assert tg_users.has_chats("u@x.ru") is True

    tg_users.set_muted(UID, True)
    assert tg_users.chats_for_email("u@x.ru") == []
    tg_users.set_muted(UID, False)

    tg_users.revoke(UID)
    assert tg_users.chats_for_email("u@x.ru") == []
    assert tg_users.get(UID)["status"] == "rejected"
    assert tg_users.is_allowed(UID) is False


def test_approve_unknown_chat(db):
    assert tg_users.approve(999999, "u@x.ru", by="admin@x.ru") is None
    with pytest.raises(ValueError):
        tg_users.approve(UID, "", by="admin@x.ru")


def test_admin_links_require_admin(client):
    # роутер подключён без cookie-сессии → зависимость require_admin режет
    assert client.get("/api/tg/links").status_code in (401, 403)


# --- форматирование ---

def test_fmt_event():
    s = _fmt_event({"isin": "RU000A10AU99", "name": "Тест-бонд", "val_bps": 284.4,
                    "price": 100.15, "money_rub": 3.2e6,
                    "fired_at": "2026-08-14T10:42:11+00:00"})
    assert "Тест-бонд" in s and "284 бп" in s and "3.2 млн ₽" in s and "10:42" in s


def test_signal_text_caps_matches():
    """Сделки идут пачкой и режутся по MAX_MATCHES (у заявок своё правило —
    отдельное сообщение на бумагу, см. test_book_events_split_by_issue)."""
    ms = [{"isin": f"RU000A00000{i}", "name": f"Б{i}", "val_bps": 300 - i,
           "price": 100.0, "money_rub": 2e6, "side": "buy"} for i in range(12)]
    txt = _signal_text({"name": "следим", "side": None, "kind": "block",
                        "matches": ms})
    assert "следим" in txt and "…ещё 4" in txt
    assert txt.count("👍") == 8          # маркер стороны на каждой показанной сделке


def test_book_line_layout():
    """Порядок записи: шапка (спред · выпуск со сроком), под ней цена с
    рейтингом и объёмом, а обстоятельства срабатывания — в подписи."""
    txt = _signal_text({"name": "Тест 2", "side": "ask", "kind": "book",
                        "matches": [{"isin": "RU000A109B33", "name": "Газпн3P13R",
                                     "val_bps": 171.0, "price": 99.9, "money_rub": 1e6,
                                     "levels": 1, "years": 1.5, "reason": "money",
                                     "money_ok_rub": 1.06e6, "prev_money_ok_rub": 1e6}]})
    lines = txt.split("\n")
    first, price = lines[0], lines[2]
    assert first.startswith("🔴")                      # оффер красный
    # число без подписи: в строке это единственное значение в бп
    assert "171 бп" in first and "R-spread" not in first
    assert "(1,5 г)" in first, "срок — в скобках при имени"
    # порядок шапки: спред → выпуск со сроком; объём уехал к цене
    assert first.index("171 бп") < first.index("Газпн3P13R")
    assert "₽" not in first
    assert "<b>99,90%</b>" in price and "1м ₽" in price
    assert "RU000A109B33" in lines[3], "ISIN — в деталях, за формулой"
    # подпись фильтра — сноской в конце, а не заголовком; без значка: он ничего
    # не добавляет к имени фильтра, а строку начинает мусором. Обстоятельства
    # срабатывания — там же, у имени фильтра.
    last = txt.strip().split("\n")[-1]
    assert last.startswith("<b>Тест 2</b> · оффер")
    assert "1 ур" in last and "объём +6 %" in last
    assert "📡" not in txt


def test_trade_icons_by_side_and_ndm():
    """У сделки маркер — направление агрессора, у адресной агрессора нет вовсе."""
    def one(**kw):
        m = {"isin": "RU000A10AU99", "name": "Т", "money_rub": 320e6,
             "price": 100.1, "val_bps": 173.0}
        m.update(kw)
        return _signal_text({"name": "следим", "side": None, "kind": "block",
                             "matches": [m]})

    assert one(side="buy", negotiated=False).startswith("👍")
    assert one(side="sell", negotiated=False).startswith("👎")
    assert one(side="buy", negotiated=True).startswith("🤝")
    assert "320м ₽" in one(side="buy", negotiated=False)   # деньги коротко


def test_signal_text_shows_issue_and_isin_monospace():
    """Выпуск: имя моноширинным, следом ISIN. Ссылки нет — в Telegram <code>
    это tap-to-copy, а из чата чаще нужен код бумаги, чем переход на сайт."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [{"isin": "RU000A10AU99", "name": "Тест",
                                     "val_bps": 250.0, "reason": "new"}]})
    assert "<code>Тест</code>" in txt and "<code>RU000A10AU99</code>" in txt
    assert 'href="' not in txt
    lines = txt.split("\n")
    assert "Тест" in lines[0], "имя — в шапке записи"
    isin_line = next(i for i, ln in enumerate(lines) if "RU000A10AU99" in ln)
    assert lines[isin_line] == "<code>RU000A10AU99</code>", "ISIN своей строкой"
    assert isin_line > 0


def test_reason_delta_shows_direction():
    """Причина повтора — величиной со знаком: «спред −18 бп», а не словом."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [{"isin": "RU000A10AU99", "name": "Т",
                                     "val_bps": 240.0, "prev_val_bps": 300.0,
                                     "reason": "spread"}]})
    assert "RS −60 бп" in txt
    # прежнее значение зачёркнутым: видно, откуда пришли
    assert "<s>300</s>" in txt


# --- снимок стакана в уведомлении о заявке ---

_BOOK = {"asks": [{"price": 100.20, "qty": 900, "money": 913050.0, "y_idx": 153.0},
                  {"price": 100.05, "qty": 1200, "money": 1215600.0, "y_idx": 168.0}],
         "bids": [{"price": 99.80, "qty": 3000, "money": 3031500.0, "y_idx": 174.0},
                  {"price": 99.75, "qty": 500, "money": 505000.0, "y_idx": 179.0}]}


def _book_match(**kw):
    m = {"isin": "RU000A109B33", "name": "Газпн3P13R", "val_bps": 168.0,
         "price": 100.05, "money_rub": 1.2e6, "levels": 1, "years": 1.5,
         "reason": "new", "book": _BOOK}
    m.update(kw)
    return m


def test_book_snapshot_rendered_under_text():
    """Стакан того же такта — моноширинным блоком под текстом, свой уровень
    помечен: в лестнице из восьми строк цена сигнала иначе теряется."""
    txt = _signal_text({"name": "Тест 2", "side": "ask", "kind": "book",
                        "matches": [_book_match()]})
    assert "<blockquote expandable>" in txt and "</blockquote>" in txt
    body = txt[txt.index("<blockquote"):txt.index("</blockquote>")]
    assert "100,20" in body and "99,75" in body        # обе стороны
    assert "168" in body and "174" in body             # спред уровня
    assert "100,05" in body and "←" in body            # уровень сигнала помечен
    # эмодзи внутри pre недопустимы — они двойной ширины и рвут колонки
    assert "🔴" not in body and "🟢" not in body


def test_book_snapshot_absent_is_ok():
    """Стакана в событии нет (снимок не доехал) — сообщение всё равно уходит."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [_book_match(book=None)]})
    assert "<blockquote" not in txt and "Газпн3P13R" in txt


def test_book_events_split_by_issue():
    """Заявки бьются по бумагам: одно сообщение = один выпуск (к каждому свой
    стакан). Сделки остаются пачкой."""
    from services.tg_notify import _group
    a, b = _book_match(), _book_match(isin="RU000A1083W0", name="МТС 2P-05")
    groups = _group([a, b, a], "book")
    assert len(groups) == 2
    assert {g[0] for g in groups} == {"RU000A109B33", "RU000A1083W0"}
    assert len(_group([a, b], "block")) == 1


def test_book_message_keeps_last_state_and_counts():
    """Внутри такта по одной бумаге показываем последнее состояние, но говорим,
    сколько раз сработало."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [_book_match(val_bps=150.0),
                                    _book_match(val_bps=168.0)]})
    assert "168 бп" in txt and "150 бп" not in txt
    assert "срабатываний за такт: 2" in txt


def test_issue_name_is_escaped():
    """Имена приходят из справочников MOEX: «&» в названии не должен рушить
    разбор HTML — иначе Telegram отбивает всё сообщение."""
    from services.tg_notify import _issue_link
    out = _issue_link({"isin": "RU000A1", "name": "Рога & Копыта"})
    assert "Рога &amp; Копыта" in out and "&amp;amp;" not in out


# --- окно коалесценции ---

def _fresh_buffers(monkeypatch, window=10.0):
    from services import tg_notify
    monkeypatch.setattr(tg_notify, "SIGNAL_FLUSH_SEC", window)
    tg_notify._pending.clear()
    tg_notify._last_sent.clear()
    return tg_notify


def test_quiet_chat_fires_immediately(monkeypatch):
    """Первое событие по молчавшему чату не ждёт окна: редкий сигнал должен
    уходить сразу, окно нужно только против серии."""
    tg = _fresh_buffers(monkeypatch)
    now = 1000.0
    tg._pending[(1, 7, None)] = {"matches": [], "first_ts": now}
    assert tg._due(now) == [(1, 7, None)]


def test_series_is_coalesced_within_window(monkeypatch):
    """После отправки следующие события по чату копятся до конца окна."""
    tg = _fresh_buffers(monkeypatch, window=10.0)
    now = 1000.0
    tg._last_sent[1] = now
    tg._pending[(1, 7, None)] = {"matches": [], "first_ts": now}
    assert tg._due(now + 3) == []            # внутри окна — молчим
    assert tg._due(now + 11) == [(1, 7, None)]   # окно вышло — уходит пачкой


def test_burst_is_capped_per_chat(monkeypatch):
    """Всплеск по одному чату режется MAX_BURST; остаток ждёт следующего такта."""
    tg = _fresh_buffers(monkeypatch)
    monkeypatch.setattr(tg, "MAX_BURST", 2)
    now = 1000.0
    for i in range(5):
        tg._pending[(1, i, None)] = {"matches": [], "first_ts": now}
    tg._pending[(2, 0, None)] = {"matches": [], "first_ts": now}
    due = tg._due(now)
    assert len([k for k in due if k[0] == 1]) == 2
    assert (2, 0, None) in due               # лимит на чат, не на такт


def test_block_message_shows_seconds_after_rating():
    """Крупная сделка: время с секундами — в подписи, за именем фильтра: внутри
    минуты по крупным принтам важна очерёдность."""
    txt = _signal_text({"name": "блоки", "side": None, "kind": "block",
                        "matches": [{"isin": "RU000A109B33", "name": "Газпн",
                                     "price": 100.05, "money_rub": 26_100_000,
                                     "val_bps": 168.0, "side": "buy",
                                     "rating": "AAA",
                                     "ts": "2026-08-21 10:42:07"}]})
    last = txt.strip().split("\n")[-1]
    assert last == "<b>блоки</b> · 10:42:07"


def test_block_time_falls_back_to_fire_moment():
    """Строка из ленты: ts сделки не хранится — берём момент срабатывания."""
    txt = _signal_text({"name": "блоки", "side": None, "kind": "block",
                        "matches": [{"isin": "RU000A109B33", "name": "Газпн",
                                     "price": 100.05, "money_rub": 26_100_000,
                                     "fired_at": "2026-08-21T07:42:07+00:00"}]})
    assert "10:42:07" in txt          # UTC → МСК


def _order_match(**kw):
    m = {"isin": "RU000A109B33", "name": "Газпн3P13R", "val_bps": 168.0,
         "price": 100.05, "money_rub": 1_000_000, "money_ok_rub": 8_200_000,
         "want_money_rub": 1_000_000, "levels": 4, "reason": "spread",
         "prev_val_bps": 153.0, "fired_at": "2026-08-21T09:47:02+00:00"}
    m.update(kw)
    return m


def test_order_shows_market_volume_not_threshold():
    """У заявки в шапке — деньги уровней в диапазоне спреда, а не набранный
    объём: тот в режиме порога равен самому порогу и повторял бы настройку."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [_order_match()]})
    assert "8,2м ₽" in txt and "1м ₽" not in txt


def test_order_volume_falls_back_when_no_spread_bounds():
    """Фильтр без границ спреда: money_ok не считается — показываем money_rub."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [_order_match(money_ok_rub=None,
                                                 money_rub=4_000_000)]})
    assert "4м ₽" in txt


def test_order_shows_time_then_threshold():
    """После причины — время срабатывания с секундами, за ним порог фильтра."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [_order_match()]})
    sub = [ln for ln in txt.split("\n") if "12:47:02" in ln][0]
    assert sub.index("RS") < sub.index("12:47:02") < sub.index(">1м")


def test_order_without_threshold_says_nothing():
    """Фильтр без порога объёма: лишнего «>0» в строке не появляется."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [_order_match(want_money_rub=None)]})
    assert ">0" not in txt and ">1м" not in txt


def test_isin_not_duplicated_when_name_missing():
    """Бумага вне справочника имён (сделки идут по всему рынку): в шапке уже
    стоит ISIN — второй раз строкой его не печатаем."""
    txt = _signal_text({"name": "блоки", "side": None, "kind": "block",
                        "matches": [{"isin": "RU000A10D1M3", "name": "RU000A10D1M3",
                                     "money_rub": 1_925_200_000, "price": 100.0}]})
    assert txt.count("RU000A10D1M3") == 1


# ── /custom: свои маркеры строк ────────────────────────────────────────────

def _cmd(text, uid=UID):
    from api.routes.tg import _handle_command
    return asyncio.run(_handle_command(text, uid, uid, "tester"))


@pytest.fixture()
def linked(db):
    """Одобренный чат: команды работают только для привязанных."""
    tg_users.request_access(UID, UID, "tester")
    tg_users.approve(UID, "u@x.ru", "admin@x.ru")
    return UID


def test_custom_shows_defaults(linked):
    out = _cmd("/custom")
    assert "🔴" in out and "🟢" in out and "🤝" in out
    assert "/custom ask" in out


def test_custom_sets_and_applies_icon(linked):
    out = _cmd("/custom ask 🟠")
    assert "🟠" in out
    icons = tg_users.icons(tg_users.get(UID))
    assert icons["ask"] == "🟠" and icons["bid"] == "🟢", "меняется только свой слот"

    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "icons": icons,
                        "matches": [{"isin": "RU000A1", "name": "Т",
                                     "val_bps": 100.0, "reason": "new"}]})
    assert txt.startswith("🟠"), "маркер чата доезжает до сообщения"


def test_custom_accepts_russian_slot(linked):
    _cmd("/custom бид 💚")
    assert tg_users.icons(tg_users.get(UID))["bid"] == "💚"


def test_custom_rejects_text_marker(linked):
    """Маркер стоит первым символом строки: буквы ломают вёрстку, «<» — HTML."""
    before = tg_users.icons(tg_users.get(UID))
    for bad in ("/custom ask ask", "/custom ask <b>", "/custom ask 1"):
        assert "эмодзи" in _cmd(bad)
    assert tg_users.icons(tg_users.get(UID)) == before


def test_custom_unknown_slot(linked):
    assert "нет" in _cmd("/custom спред 🟠").lower()


def test_custom_reset_and_single_revert(linked):
    _cmd("/custom ask 🟠")
    _cmd("/custom sell 🔻")
    assert tg_users.icons(tg_users.get(UID))["ask"] == "🟠"

    _cmd("/custom ask -")
    icons = tg_users.icons(tg_users.get(UID))
    assert icons["ask"] == "🔴" and icons["sell"] == "🔻", "снят только один слот"

    _cmd("/custom reset")
    assert tg_users.icons(tg_users.get(UID)) == {
        k: v[1] for k, v in tg_users.ICON_SLOTS.items()}


def test_poller_recovers_from_webhook_conflict(monkeypatch):
    """Поставленный вебхук глушит поллинг — поллер снимает его сам.

    Регресс 21.08.2026: вебхук зарегистрировали руками, Bot API начал отдавать на
    getUpdates «409 Conflict», и бот молчал час — вебхук на этом VPS тоже не
    доходит («Connection timed out»), так что команды не шли ни одним путём;
    в очереди зависли 4 апдейта. Поллер только логировал предупреждение.
    """
    import asyncio
    from services import telegram, tg_poll

    calls = []

    async def fake_call(method, payload=None, files=None, timeout=30.0):
        calls.append(method)
        if method == "getUpdates":
            telegram.last_error["description"] = (
                "Conflict: can't use getUpdates method while webhook is active")
            await asyncio.sleep(0)
            return None
        return True

    monkeypatch.setattr(telegram, "call", fake_call)
    monkeypatch.setattr(telegram, "enabled", lambda: True)
    monkeypatch.setattr(tg_poll, "enabled", lambda: True)

    async def run():
        task = asyncio.create_task(tg_poll.tg_poll_worker())
        for _ in range(200):                    # ждём реакции, не весь цикл
            if calls.count("deleteWebhook") >= 2:   # старт + лечение конфликта
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert calls.count("deleteWebhook") >= 2, (
        f"поллер не снял вебхук при конфликте: {calls[:6]}")


# ── каналы доставки: привязка пересылкой ───────────────────────────────────

def _forward_update(chat_id=-1001234, title="Р5", uid=UID):
    """Пользователь переслал боту пост из своего канала."""
    return {"message": {"text": "", "from": {"id": uid, "username": "tester"},
                        "chat": {"id": uid, "type": "private"},
                        "forward_from_chat": {"id": chat_id, "title": title,
                                              "type": "channel"}}}


def test_forwarded_post_binds_channel(linked, monkeypatch):
    from api.routes import tg as tg_route
    from services import tg_targets

    sent = []

    async def fake_send(chat_id, text, **kw):
        sent.append((chat_id, text))
        return {"message_id": 1}
    monkeypatch.setattr(tg_route.telegram, "send_message", fake_send)

    asyncio.run(tg_route.process_update(_forward_update()))
    rows = tg_targets.list_for_user("u@x.ru")
    assert [t["chat_id"] for t in rows] == [-1001234]
    assert rows[0]["title"] == "Р5"
    # в сам канал ушла проверка права писать — узнать об этом надо здесь,
    # а не на первом пропавшем сигнале
    assert sent[0][0] == -1001234
    tg_targets.remove("u@x.ru", rows[0]["id"])


def test_forward_without_rights_is_not_bound(linked, monkeypatch):
    """Бот не админ канала — Bot API отказал: адресата не заводим."""
    from api.routes import tg as tg_route
    from services import tg_targets

    async def fail_send(chat_id, text, **kw):
        if chat_id < 0:
            tg_route.telegram.last_error["description"] = "Forbidden: bot is not a member"
            return None
        return {"message_id": 1}
    monkeypatch.setattr(tg_route.telegram, "send_message", fail_send)

    asyncio.run(tg_route.process_update(_forward_update(chat_id=-1009)))
    assert tg_targets.list_for_user("u@x.ru") == []


def test_chats_command_lists_and_removes(linked, monkeypatch):
    from services import tg_targets
    t = tg_targets.add("u@x.ru", -1005, "Ф5", "channel")
    assert "Ф5" in _cmd("/chats")
    assert "снят" in _cmd(f"/chats del {t['id']}")
    assert tg_targets.list_for_user("u@x.ru") == []


def test_order_money_is_level_not_side_total():
    """В шапке заявки — накопленный объём по цене сигнала, а не сумма стороны.

    Регресс Газпн3P13R 24.08: сообщение показывало 20,7м ₽ (весь оффер книги)
    при 3,8м, доступных по 99,86, — цене, по которой фильтр сработал."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [_order_match(level_money_rub=3_800_000,
                                                 money_ok_rub=20_700_000,
                                                 money_rub=1_000_000)]})
    assert "3,8м ₽" in txt and "20,7м ₽" not in txt


def test_order_money_falls_back_for_old_events():
    """Событие из ленты, записанное до появления поля — показываем что есть."""
    m = _order_match(money_ok_rub=8_200_000, money_rub=1_000_000)
    m.pop("level_money_rub", None)
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book", "matches": [m]})
    assert "8,2м ₽" in txt


def test_head_order_is_spread_issue_money():
    """Шапка заявки: спред · выпуск (срок); объём стоит у цены, потому что это
    накопленная глубина именно до неё."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [_order_match(years=1.1,
                                                 level_money_rub=12_100_000)]})
    lines = txt.split("\n")
    head, price = lines[0], lines[2]
    assert "<code>Газпн3P13R</code> (1,1 г)" in head
    assert head.index("168 бп") < head.index("Газпн3P13R")
    assert "12,1м ₽" in price and "₽" not in head


def test_head_without_maturity_has_no_empty_brackets():
    """Срок неизвестен — скобок нет вовсе."""
    m = _order_match()
    m.pop("years", None)
    head = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                         "matches": [m]}).split("\n")[0]
    assert "()" not in head and "Газпн3P13R" in head


def test_book_layout_puts_details_after_orderbook():
    """Стакан стоит ВНУТРИ записи: шапка → цена → книга → формула с ISIN."""
    m = _order_match(years=1.1, level_money_rub=32_600_000, reason="new")
    m["book"] = {"asks": [{"price": 99.83, "qty": 38, "money": 37_900, "y_idx": 164}],
                 "bids": [{"price": 99.71, "qty": 14, "money": 14_300, "y_idx": 178}]}
    lines = _signal_text({"name": "Тест 2", "side": "ask", "kind": "book",
                          "matches": [m]}).split("\n")
    assert "бп" in lines[0] and "Газпн3P13R" in lines[0]
    assert lines[1] == "", "пустая строка между шапкой и ценой"
    assert lines[2].startswith("<b>100,05%</b>")
    assert lines[3].startswith("<blockquote"), "стакан сразу под ценой"
    book_end = next(i for i, ln in enumerate(lines) if "</blockquote>" in ln)
    assert "RU000A109B33" in lines[book_end + 1], "формула с ISIN — за стаканом"
    assert lines[-2] == "", "подпись отбита от записи"
    foot = lines[-1]
    assert foot.startswith("<b>Тест 2</b> · оффер")
    assert "заявка" in foot and "12:47:02" in foot and ">1м" in foot


def test_repeat_count_joins_details_line():
    """Счётчик повторов такта — в той же строке подписи, не отдельной."""
    m = _order_match(reason="new")
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [m, m]})
    line = [ln for ln in txt.split("\n") if "срабатываний" in ln][0]
    assert "заявка" in line and "срабатываний за такт: 2" in line


def test_order_rating_goes_after_price():
    """У заявки рейтинг стоит между ценой и объёмом: «почём, чьё, на сколько»."""
    line = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                         "matches": [_order_match(rating="AAA")]}).split("\n")[2]
    assert line.index("100,05") < line.index("AAA") < line.index("₽")


def test_trade_rating_not_duplicated():
    """У сделки рейтинг стоит в деталях рядом с формулой и ISIN, и только
    там — дублировать его в строке цены незачем."""
    txt = _signal_text({"name": "блоки", "side": None, "kind": "block",
                        "matches": [{"isin": "RU000A10AU99", "name": "Т",
                                     "price": 100.1, "money_rub": 2.6e8,
                                     "rating": "AAA", "ts": "2026-08-24 12:47:02"}]})
    assert txt.count("AAA") == 1
    rating_line = [ln for ln in txt.split("\n") if "AAA" in ln][0]
    assert "RU000A10AU99" in rating_line
    assert "12:47:02" in txt.strip().split("\n")[-1], "время — в подписи"


def test_book_volume_in_lots_not_rubles():
    """Колонка стакана — БУМАГИ: в стакане торгуют количеством, а рубли уже
    стоят в шапке (накопленный объём)."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [_book_match()]})
    body = txt[txt.index("<blockquote"):txt.index("</blockquote>")]
    assert "ШТ" in body and "ОБЪЁМ" not in body
    assert "1 200" in body and "3 000" in body      # точное число бумаг
    assert "913" not in body and "1215" not in body  # рублёвых сумм нет


def test_book_volume_small_and_large():
    """До сотни тысяч — точное число, крупнее — порядок: «0к» вместо 38 бумаг
    бесполезно, а миллион цифрами не влезает в колонку телефона."""
    from services.tg_notify import _book_qty
    assert _book_qty({"qty": 38}).strip() == "38"
    assert _book_qty({"qty": 24_950}).strip() == "24 950"
    assert _book_qty({"qty": 120_000}).strip() == "120к"
    assert _book_qty({"qty": None}).strip() == ""


def test_coupon_formula_under_orderbook():
    """Формула купона — первой в строке под стаканом: спред без ответа «чем
    бумага платит» висит в воздухе."""
    from services.tg_notify import _formula
    assert _formula({"base": "KEYRATE", "margin_bps": 120, "cpy": 12}) == "КС + 1,2% (12)"
    assert _formula({"base": "RUONIA", "margin_bps": 87.5, "cpy": 4}) == "RU + 0,88% (4)"
    assert _formula({"base": "KEYRATE", "margin_bps": 200}) == "КС + 2%"
    assert _formula({"base": None, "margin_bps": 120}) == "", "без базы не пишем"

    m = _order_match(base="KEYRATE", margin_bps=120, cpy=12)
    m["book"] = {"asks": [{"price": 99.83, "qty": 38, "y_idx": 164}],
                 "bids": [{"price": 99.71, "qty": 14, "y_idx": 178}]}
    lines = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                          "matches": [m]}).split("\n")
    book_end = next(i for i, ln in enumerate(lines) if "</blockquote>" in ln)
    details = lines[book_end + 1]
    assert details.startswith("КС + 1,2% (12) · ")


# --- нить по бумаге: повтор отвечает на прошлое сообщение ---

@pytest.fixture()
def sent(monkeypatch):
    """Перехват отправки: копим (chat_id, reply_to) и раздаём message_id."""
    import services.tg_notify as tg
    tg._pending.clear()
    tg._last_sent.clear()
    tg._threads.clear()
    log = []

    async def fake_send(chat_id, text, **kw):
        mid = 100 + len(log) + 1
        log.append({"chat": chat_id, "reply_to": kw.get("reply_to"),
                    "text": text, "mid": mid})
        return {"message_id": mid}

    monkeypatch.setattr("services.tg_notify.telegram.send_message", fake_send)
    return log


def _flush(*bufs):
    """Кладёт буферы под их ключами и сливает — как это делает воркер."""
    import services.tg_notify as tg
    for key, buf in bufs:
        buf.setdefault("first_ts", 0.0)
        tg._pending[key] = buf
    asyncio.run(tg._flush_signals())


def _book_buf(reason, isin="RU000A109B33"):
    return {"name": "ф", "side": "ask", "kind": "book",
            "matches": [_order_match(isin=isin, reason=reason)]}


def test_repeat_replies_to_previous_message(sent):
    """Повтор по бумаге уходит ОТВЕТОМ на прошлое сообщение о ней: в чате
    видно историю одной заявки, а не набор одинаковых карточек."""
    key = (1, 7, "RU000A109B33")
    _flush((key, _book_buf("new")))
    _flush((key, _book_buf("spread")))
    _flush((key, _book_buf("money")))
    assert [s["reply_to"] for s in sent] == [None, 101, 102], "нить тянется за хвостом"


def test_new_signal_starts_new_thread(sent):
    """«Заявка» начинает нить заново: уровень пропал и появился снова — это
    новая история, а не продолжение прошлой."""
    key = (1, 7, "RU000A109B33")
    _flush((key, _book_buf("new")))
    _flush((key, _book_buf("spread")))
    _flush((key, _book_buf("new")))
    assert [s["reply_to"] for s in sent] == [None, 101, None]


def test_stale_thread_is_dropped(sent, monkeypatch):
    """Протухшая нить начинается заново: ответ на утреннее сообщение к вечеру
    уводит читателя в архив вместо связи."""
    import services.tg_notify as tg
    monkeypatch.setattr(tg, "THREAD_TTL_SEC", 0.0)
    key = (1, 7, "RU000A109B33")
    _flush((key, _book_buf("new")))
    _flush((key, _book_buf("spread")))
    assert [s["reply_to"] for s in sent] == [None, None]
    tg._prune_threads(tg.time.monotonic())
    assert tg._threads == {}, "протухшие нити чистятся, словарь не растёт"


def test_threads_are_per_issue_and_chat(sent):
    """Нить своя у каждой пары (чат, выпуск): ответ в чужую бумагу или в чужой
    чат был бы хуже отсутствия связи."""
    a, b = (1, 7, "RU000A109B33"), (1, 7, "RU000A1083W0")
    other = (2, 7, "RU000A109B33")
    _flush((a, _book_buf("new")), (b, _book_buf("new", "RU000A1083W0")))
    assert [s["reply_to"] for s in sent] == [None, None], "две новые нити"
    first_a = next(s for s in sent if "RU000A109B33" in s["text"])

    _flush((a, _book_buf("spread")), (other, _book_buf("spread")))
    repeat_a = next(s for s in sent[2:] if s["chat"] == 1)
    repeat_other = next(s for s in sent[2:] if s["chat"] == 2)
    assert repeat_a["reply_to"] == first_a["mid"], "ответ в нить своей бумаги"
    assert repeat_other["reply_to"] is None, "чужой чат нити не наследует"


def test_trades_have_no_thread(sent):
    """Сделки идут пачкой: выпуска в ключе нет, отвечать не на что."""
    m = {"isin": "RU000A10AU99", "name": "Т", "price": 100.1, "money_rub": 2e6}
    buf = {"name": "блоки", "side": None, "kind": "block", "matches": [m]}
    _flush(((1, 7, None), buf))
    _flush(((1, 7, None), dict(buf)))
    assert [s["reply_to"] for s in sent] == [None, None]


# ── отказ канала доставки ──────────────────────────────────────────────────

def test_failed_send_returns_signal_to_queue(monkeypatch):
    """Bot API молчит — сигнал остаётся в очереди, а не исчезает.

    Раньше буфер чистился ДО результата отправки: упавший прокси-сайдкар
    стирал сигналы бесследно (в вебе есть, в телеграм не придут никогда)."""
    from services import tg_notify as tn
    tn._pending.clear(); tn._last_sent.clear()
    m = {"isin": "RU000A1", "name": "Т", "val_bps": 150.0, "price": 100.0,
         "reason": "new", "level_money_rub": 2e6}
    tn._pending[(1, 7, None)] = {"name": "ф", "side": "ask", "kind": "book",
                                 "matches": [m], "first_ts": time.monotonic()}

    async def dead(*a, **k):
        return None
    monkeypatch.setattr(tn.telegram, "send_message", dead)
    asyncio.run(tn._flush_signals())
    assert (1, 7, None) in tn._pending, "сигнал должен вернуться в очередь"
    assert 1 not in tn._last_sent, "чат снова считается молчавшим"

    sent = []

    async def alive(chat_id, text, **k):
        sent.append(chat_id)
        return {"message_id": 1}
    monkeypatch.setattr(tn.telegram, "send_message", alive)
    asyncio.run(tn._flush_signals())
    assert sent == [1] and not tn._pending, "канал вернулся — доставили"


def test_stale_signal_is_dropped_not_kept_forever(monkeypatch):
    """Канал молчит дольше окна — бросаем: сигнал уже не новость."""
    from services import tg_notify as tn
    tn._pending.clear(); tn._last_sent.clear()
    old = time.monotonic() - tn.REQUEUE_MAX_SEC - 1
    tn._pending[(1, 7, None)] = {"name": "ф", "side": "ask", "kind": "book",
                                 "matches": [{"isin": "RU000A1", "name": "Т",
                                              "reason": "new"}], "first_ts": old}

    async def dead(*a, **k):
        return None
    monkeypatch.setattr(tn.telegram, "send_message", dead)
    asyncio.run(tn._flush_signals())
    assert not tn._pending, "старое не держим вечно"
