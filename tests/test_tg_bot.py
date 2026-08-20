"""Телеграм-бот: привязка чата к веб-аккаунту, вебхук, форматирование доставки.
Своей настройки у бота нет — сигналы заводятся на сайте."""
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
    # подпись фильтра — сноской в конце, а не заголовком
    assert txt.strip().endswith("📡 <b>Тест 2</b> · оффер")


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


def test_signal_text_links_to_card():
    """Имя выпуска — ссылка на его карточку: из чата один тап до стакана."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [{"isin": "RU000A10AU99", "name": "Тест",
                                     "val_bps": 250.0, "reason": "new"}]})
    assert 'href="' in txt and "isin=RU000A10AU99" in txt and "ob=1" in txt


def test_reason_delta_shows_direction():
    """Причина повтора — величиной со знаком: «спред −18 бп», а не словом."""
    txt = _signal_text({"name": "ф", "side": "ask", "kind": "book",
                        "matches": [{"isin": "RU000A10AU99", "name": "Т",
                                     "val_bps": 240.0, "prev_val_bps": 300.0,
                                     "reason": "spread"}]})
    assert "R-spread −60 бп" in txt


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
    assert "&amp;" in _issue_link({"isin": "RU000A1", "name": "Рога & Копыта"})
