"""Каскад «уволили человека»: снос всего, что продолжает ему звонить.

ЗАЧЕМ. Аккаунт живёт в data/users.json (auth_users), а доставка — в
portfolio.db. remove_user() трогал только JSON, поэтому после удаления
аккаунта оставались:

  * signal_filters — signals.list_enabled() берёт ВСЕ строки с enabled=1 и не
    сверяется с существованием аккаунта, значит run_cycle гоняет их вечно;
  * tg_users с этим email — chats_for_email() матчит по строке, а не по живому
    пользователю, и личные чаты продолжают получать сигналы;
  * tg_targets — привязанные каналы (см. tg_notify: канал победит пустой
    список личных чатов);
  * signal_events / signal_state / trade_flag — мусор, растущий вечно.

Итог: веб-доступ закрыт, а уволенному по-прежнему идут живые сигналы рынка и
блок-алерты в телеграм.

ПОРЯДОК ВНУТРИ ТРАНЗАКЦИИ. signal_state сносится ПЕРВЫМ: он ключуется по
filter_id, и после удаления signal_filters эти id уже не собрать. Внешних
ключей между таблицами нет, поэтому порядок держим руками.

tg_users — UPDATE, а не DELETE: строку сохраняем, чтобы чат мог подать заявку
заново, а админ видел историю привязок (та же семантика, что у tg_users.revoke).
"""
from __future__ import annotations

import logging

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)


def purge_delivery(user_email: str) -> dict:
    """Гасит доставку для email. Возвращает {таблица: сколько строк}.

    Одна транзакция: полумера тут хуже, чем ничего — осиротевший фильтр без
    аккаунта уже некому выключить через интерфейс."""
    email = (user_email or "").strip().lower()
    if not email:
        return {}
    with _lock, _connect() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM signal_filters WHERE user_email=?", (email,))]
        n_state = 0
        if ids:
            marks = ",".join("?" * len(ids))
            n_state = c.execute(
                f"DELETE FROM signal_state WHERE filter_id IN ({marks})", ids).rowcount or 0
        n_ev = c.execute("DELETE FROM signal_events WHERE user_email=?",
                         (email,)).rowcount or 0
        n_f = c.execute("DELETE FROM signal_filters WHERE user_email=?",
                        (email,)).rowcount or 0
        n_t = c.execute("DELETE FROM tg_targets WHERE user_email=?",
                        (email,)).rowcount or 0
        n_fl = c.execute("DELETE FROM trade_flag WHERE user_email=?",
                         (email,)).rowcount or 0
        n_tg = c.execute(
            "UPDATE tg_users SET email=NULL, status='rejected', approved_at=NULL, "
            "approved_by=NULL WHERE email=?", (email,)).rowcount or 0
    out = {"filters": n_f, "events": n_ev, "state": n_state,
           "targets": n_t, "flags": n_fl, "tg_links": n_tg}
    if any(out.values()):
        logger.info("purge %s: %s", email, out)
    return out
