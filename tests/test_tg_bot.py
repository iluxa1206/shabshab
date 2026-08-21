"""Телеграм-бот: привязка чата к веб-аккаунту, вебхук, форматирование доставки.
Своей настройки у бота нет — сигналы заводятся на сайте."""
import asyncio

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
    yield
    with _lock, _connect() as c:
        c.execute("DELETE FROM tg_users WHERE tg_user_id IN (?, 555)", (UID,))


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
    """Первая строка: сторона · спред · деньги · срок · ИМЯ В КОНЦЕ (имена разной
    длины, впереди они ломали бы колонку чисел). Вторая — цена, глубина, причина."""
    txt = _signal_text({"name": "Тест 2", "side": "ask", "kind": "book",
                        "matches": [{"isin": "RU000A109B33", "name": "Газпн3P13R",
                                     "val_bps": 171.0, "price": 99.9, "money_rub": 1e6,
                                     "levels": 1, "years": 1.5, "reason": "money",
                                     "money_ok_rub": 1.06e6, "prev_money_ok_rub": 1e6}]})
    first, second = txt.split("\n")[0], txt.split("\n")[1]
    assert first.startswith("🔴")                      # оффер красный
    # число без подписи: в строке это единственное значение в бп
    assert "171 бп" in first and "R-spread" not in first
    assert "1м ₽" in first and "1,5 г" in first
    assert first.index("171 бп") < first.index("Газпн3P13R")
    assert "99,90%" in second and "1 ур" in second and "объём +6 %" in second
    # подпись фильтра — сноской в конце, а не заголовком; без значка: он ничего
    # не добавляет к имени фильтра, а строку начинает мусором
    assert txt.strip().endswith("<b>Тест 2</b> · оффер")
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

_BOOK = {"asks": [{"price": 100.20, "money": 913050.0, "y_idx": 153.0},
                  {"price": 100.05, "money": 1215600.0, "y_idx": 168.0}],
         "bids": [{"price": 99.80, "money": 3031500.0, "y_idx": 174.0},
                  {"price": 99.75, "money": 505000.0, "y_idx": 179.0}]}


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
    """Крупная сделка: время с секундами и ПОСЛЕ рейтинга — внутри минуты по
    крупным принтам важна очерёдность."""
    txt = _signal_text({"name": "блоки", "side": None, "kind": "block",
                        "matches": [{"isin": "RU000A109B33", "name": "Газпн",
                                     "price": 100.05, "money_rub": 26_100_000,
                                     "val_bps": 168.0, "side": "buy",
                                     "rating": "AAA",
                                     "ts": "2026-08-21 10:42:07"}]})
    sub = [ln for ln in txt.split("\n") if "AAA" in ln][0]
    assert "10:42:07" in sub
    assert sub.index("AAA") < sub.index("10:42:07")


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
