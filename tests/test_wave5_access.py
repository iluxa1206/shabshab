"""Регресс-щит волны 5 аудита 2026-08-26: доступ, уведомления, стакан.

Главное: удаление аккаунта не гасило доставку, а канал обходил revoke."""
import importlib
import time

import pytest


@pytest.fixture
def pdb(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as m
    importlib.reload(m)
    m.init_db()
    yield m
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(m)


# ─── каскад удаления аккаунта ───────────────────────────────────────────────

def test_purge_kills_all_delivery(pdb, monkeypatch):
    """remove_user трогал только users.json: фильтры, привязки и каналы
    оставались, и run_cycle продолжал звонить уволенному в телеграм."""
    import services.user_purge as up
    importlib.reload(up)
    email = "уволен@x.ru"
    T = "2026-08-26T00:00:00"
    with pdb._connect() as c:
        c.execute("INSERT INTO signal_filters(id,user_email,name,enabled,params_json,"
                  "created_at) VALUES(1,?,'f',1,'{}',?)", (email, T))
        c.execute("INSERT INTO signal_state(filter_id,isin,updated_at) "
                  "VALUES(1,'RU1',?)", (T,))
        c.execute("INSERT INTO signal_events(id,filter_id,user_email,isin,fired_at) "
                  "VALUES(1,1,?,'RU1',?)", (email, T))
        c.execute("INSERT INTO tg_targets(id,user_email,chat_id,title,kind,created_at) "
                  "VALUES(1,?,-100,'канал','channel',?)", (email, T))
        c.execute("INSERT INTO trade_flag(user_email,trade_id,isin,ts,created_at) "
                  "VALUES(?,'t1','RU1',?,?)", (email, T, T))
        c.execute("INSERT INTO tg_users(tg_user_id,chat_id,email,status,muted,created_at) "
                  "VALUES(7,77,?,'approved',0,?)", (email, T))

    got = up.purge_delivery(email)
    assert got["filters"] == 1 and got["targets"] == 1 and got["tg_links"] == 1
    assert got["state"] == 1 and got["events"] == 1 and got["flags"] == 1

    with pdb._connect() as c:
        for t in ("signal_filters", "signal_events", "tg_targets", "trade_flag"):
            n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE user_email=?",
                          (email,)).fetchone()[0]
            assert n == 0, f"{t}: осталось {n}"
        assert c.execute("SELECT COUNT(*) FROM signal_state").fetchone()[0] == 0
        # строку tg_users сохраняем (чат сможет подать заявку заново), но отвязываем
        r = c.execute("SELECT email,status FROM tg_users WHERE tg_user_id=7").fetchone()
        assert r["email"] is None and r["status"] == "rejected"


def test_purge_is_scoped_to_email(pdb):
    """Чужие фильтры каскад трогать не должен."""
    import services.user_purge as up
    importlib.reload(up)
    T = "2026-08-26T00:00:00"
    with pdb._connect() as c:
        c.execute("INSERT INTO signal_filters(id,user_email,name,enabled,params_json,"
                  "created_at) VALUES(1,'a@x.ru','f',1,'{}',?)", (T,))
        c.execute("INSERT INTO signal_filters(id,user_email,name,enabled,params_json,"
                  "created_at) VALUES(2,'b@x.ru','f',1,'{}',?)", (T,))
    up.purge_delivery("a@x.ru")
    with pdb._connect() as c:
        left = [r["user_email"] for r in c.execute("SELECT user_email FROM signal_filters")]
    assert left == ["b@x.ru"]


def test_purge_empty_email_is_noop(pdb):
    import services.user_purge as up
    importlib.reload(up)
    assert up.purge_delivery("") == {}
    assert up.purge_delivery(None) == {}


# ─── канал не обходит revoke, но mute глушит только личку ───────────────────

def test_email_exists_approved_ignores_mute(pdb, monkeypatch):
    """Право на доставку в КАНАЛ живёт в привязке владельца и НЕ зависит от
    mute: /mute — «не пиши мне в личку», каналы он глушить не должен.
    А revoke (status='rejected') канал обязан погасить."""
    import services.tg_users as tu
    importlib.reload(tu)
    T = "2026-08-26T00:00:00"
    with pdb._connect() as c:
        c.execute("INSERT INTO tg_users(tg_user_id,chat_id,email,status,muted,created_at) "
                  "VALUES(1,11,'a@x.ru','approved',1,?)", (T,))     # замьючен
        c.execute("INSERT INTO tg_users(tg_user_id,chat_id,email,status,muted,created_at) "
                  "VALUES(2,22,'b@x.ru','rejected',0,?)", (T,))     # отозван
    assert tu.email_exists_approved("a@x.ru") is True    # mute канал не гасит
    assert tu.has_chats("a@x.ru") is False               # ...но личку гасит
    assert tu.email_exists_approved("b@x.ru") is False   # revoke гасит всё
    assert tu.email_exists_approved("нет@x.ru") is False


# ─── свежесть стакана по шардам ─────────────────────────────────────────────

def test_depth_drops_only_dead_shard():
    """Глобальный depth_ts обновлял ЛЮБОЙ живой шард, и смерть одного сокета
    (150 бумаг) пряталась за соседями: скринер сигналил по заявкам,
    снятым часы назад."""
    from services.market_data import market_cache
    from services import depth
    now = time.time()
    saved = {k: market_cache.get(k) for k in
             ("depth", "depth_ts", "depth_shard_ts", "depth_shard_isins")}
    try:
        market_cache["depth"] = {"RU1": {"b": [], "a": []}, "RU2": {"b": [], "a": []}}
        market_cache["depth_ts"] = now                    # рынок «свежий»
        market_cache["depth_shard_isins"] = {0: ["RU1"], 1: ["RU2"]}
        market_cache["depth_shard_ts"] = {0: now, 1: now - 3600}   # шард 1 мёртв
        got = depth.get_depth()
        assert "RU1" in got and "RU2" not in got, got
        # протухшая бумага ИЗЪЯТА, а не отдана пустой лестницей: иначе
        # потребитель прочитал бы «в стакане ничего нет» и посчитал нулевой объём
        assert got.get("RU2") is None
    finally:
        for k, v in saved.items():
            if v is None:
                market_cache.pop(k, None)
            else:
                market_cache[k] = v


def test_depth_without_shards_returns_all():
    """Батч-поллер работает без стрима — шардовых меток нет, отдаём всё."""
    from services.market_data import market_cache
    from services import depth
    now = time.time()
    saved = {k: market_cache.get(k) for k in
             ("depth", "depth_ts", "depth_shard_ts", "depth_shard_isins")}
    try:
        market_cache["depth"] = {"RU1": {"b": [], "a": []}}
        market_cache["depth_ts"] = now
        market_cache.pop("depth_shard_ts", None)
        market_cache.pop("depth_shard_isins", None)
        assert "RU1" in depth.get_depth()
    finally:
        for k, v in saved.items():
            if v is None:
                market_cache.pop(k, None)
            else:
                market_cache[k] = v


def test_batch_poller_overrides_dead_shard():
    """depth_stream_covers включает HTTP-фолбэк уже при потере 20% шардов, и он
    честно обновляет ВЕСЬ юниверс. Выбрасывать эти бумаги по устаревшей
    шардовой метке — значит терять свежие данные ровно тогда, когда стрим
    деградировал."""
    from services.market_data import market_cache
    from services import depth
    now = time.time()
    keys = ("depth", "depth_ts", "depth_shard_ts", "depth_shard_isins", "depth_batch")
    saved = {k: market_cache.get(k) for k in keys}
    try:
        market_cache["depth"] = {"RU1": {"b": [], "a": []}, "RU2": {"b": [], "a": []}}
        market_cache["depth_ts"] = now
        market_cache["depth_shard_isins"] = {0: ["RU1"], 1: ["RU2"]}
        market_cache["depth_shard_ts"] = {0: now, 1: now - 3600}   # шард 1 мёртв
        # ...но поллер только что обновил обе бумаги
        market_cache["depth_batch"] = {"ts": now, "isins": {"RU1", "RU2"}}
        got = depth.get_depth()
        assert "RU1" in got and "RU2" in got, got

        # протухший батч уже не спасает
        market_cache["depth_batch"] = {"ts": now - 3600, "isins": {"RU1", "RU2"}}
        got2 = depth.get_depth()
        assert "RU2" not in got2
    finally:
        for k, v in saved.items():
            if v is None:
                market_cache.pop(k, None)
            else:
                market_cache[k] = v


def _depth_case(shard_isins, shard_ts, batch=None, depth_map=None):
    """Прогон get_depth на подставленном состоянии кэша."""
    from services.market_data import market_cache
    from services import depth
    now = time.time()
    keys = ("depth", "depth_ts", "depth_shard_ts", "depth_shard_isins", "depth_batch")
    saved = {k: market_cache.get(k) for k in keys}
    try:
        market_cache["depth"] = depth_map or {"RU1": {"b": [], "a": []}}
        market_cache["depth_ts"] = now
        market_cache["depth_shard_isins"] = shard_isins
        market_cache["depth_shard_ts"] = {k: now - v for k, v in shard_ts.items()}
        if batch is None:
            market_cache.pop("depth_batch", None)
        else:
            market_cache["depth_batch"] = batch
        return depth.get_depth()
    finally:
        for k, v in saved.items():
            if v is None:
                market_cache.pop(k, None)
            else:
                market_cache[k] = v


def test_live_shard_beats_retired_list():
    """depth_shard_isins не снимается ни при смерти сокета, ни при пересборке
    пула, а shard_id переиспользуются: без вычитания «жив у соседа» бумага,
    которую ведёт ЖИВОЙ шард, выбрасывалась по списку шарда, которого нет."""
    got = _depth_case({0: ["RU1"], 8: ["RU1"]}, {0: 0})     # шард 8 без метки
    assert "RU1" in got, "живую бумагу выбросил отставной список"


def test_all_shards_dead_is_not_no_stream():
    """Смерть ВСЕХ шардов (реконнект-шторм, протухший токен) — не то же самое,
    что «стрима нет». Проверка по пустому shard_ts отдавала бы весь протухший
    стакан как свежий — исходный баг в максимальном масштабе."""
    got = _depth_case({0: ["RU1"]}, {})       # списки есть, меток нет
    assert "RU1" not in got, "протухший стакан отдан как свежий"


def test_no_stream_at_all_returns_everything():
    """А вот когда стрим не поднимался вовсе — списков нет, работает батч-поллер."""
    got = _depth_case({}, {})
    assert "RU1" in got
