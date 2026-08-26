#!/usr/bin/env python3
"""Управление доступом к дашборду (только для админа).

Аккаунты создаются вручную — самостоятельной регистрации на сайте нет.
Файл юзеров: data/users.json (в проде — Docker-том, переживает редеплой).

Примеры:
    python scripts/useradd.py add user@example.com          # спросит пароль
    python scripts/useradd.py add user@example.com --role admin
    python scripts/useradd.py add user@example.com --force   # перезаписать пароль
    python scripts/useradd.py list
    python scripts/useradd.py remove user@example.com

В проде на VPS запускать внутри контейнера, чтобы писать в тот же том:
    docker compose -f docker-compose.prod.yml exec floaters \\
        python scripts/useradd.py add user@example.com
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import auth_users  # noqa: E402


def _prompt_password() -> str:
    p1 = getpass.getpass("Пароль (мин. 8 символов): ")
    p2 = getpass.getpass("Повтор пароля: ")
    if p1 != p2:
        sys.exit("Пароли не совпадают")
    return p1


def cmd_add(args: argparse.Namespace) -> None:
    password = args.password or _prompt_password()
    try:
        auth_users.add_user(args.email, password, role=args.role, overwrite=args.force)
    except ValueError as e:
        sys.exit(f"Ошибка: {e}")
    print(f"OK: {args.email} ({args.role}) {'перезаписан' if args.force else 'добавлен'}")


def cmd_remove(args: argparse.Namespace) -> None:
    # тот же каскад, что у admin-ручки: иначе уволенному продолжают идти
    # сигналы и блок-алерты в телеграм (см. services/user_purge)
    from services.user_purge import purge_delivery
    purged = purge_delivery(args.email)
    if auth_users.remove_user(args.email):
        print(f"OK: {args.email} удалён; погашено: {purged}")
    else:
        sys.exit(f"Нет такого пользователя: {args.email}")


def cmd_list(_args: argparse.Namespace) -> None:
    users = auth_users.list_users()
    if not users:
        print("(пусто — ни одного пользователя, сайт закрыт для всех)")
        return
    for u in users:
        print(f"{u['email']:<40} {u['role']:<6} {u.get('created', '')}")


def main() -> None:
    p = argparse.ArgumentParser(description="Управление доступом к дашборду")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add", help="создать/перезаписать пользователя")
    pa.add_argument("email")
    pa.add_argument("--password", help="пароль (иначе спросит интерактивно)")
    pa.add_argument("--role", default="user", choices=["user", "admin"])
    pa.add_argument("--force", action="store_true", help="перезаписать существующего")
    pa.set_defaults(func=cmd_add)

    pr = sub.add_parser("remove", help="удалить пользователя")
    pr.add_argument("email")
    pr.set_defaults(func=cmd_remove)

    pl = sub.add_parser("list", help="список пользователей")
    pl.set_defaults(func=cmd_list)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
