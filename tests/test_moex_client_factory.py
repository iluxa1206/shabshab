"""Все запросы к MOEX идут через одну фабрику.

10.09.2026 путь с прод-VPS до iss.moex.com оказался перерезан по домену (TLS на
443 не встаёт, на 80 промежуточное устройство отдаёт фейковый 302 с
«Server: Apache/2.2.6 (Fedora)»). Чинится это только сменой сетевого пути —
переменной MOEX_PROXY. Переключатель бесполезен, если хоть один запрос идёт
мимо фабрики: именно он и останется сломанным, причём молча.
"""
import os
import re
from pathlib import Path

import httpx
import pytest

import services.market_data as md

ROOT = Path(__file__).resolve().parents[1] / "services"

# Клиенты к ЧУЖИМ хостам — им фабрика MOEX не нужна и вредна.
NOT_MOEX = {
    "smartlab_audit.py",   # smart-lab.ru
    "bondresearch.py",     # bondresearch.ru
    "trades_archive.py",   # там один клиент к Alor (headers с токеном)
}


def test_no_bare_client_in_moex_modules():
    offenders = []
    for path in ROOT.glob("*.py"):
        if path.name in NOT_MOEX or path.name == "telegram.py":
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if "httpx.AsyncClient()" in line:
                offenders.append(f"{path.name}:{i}")
    assert not offenders, (
        "запрос к MOEX мимо moex_client() — MOEX_PROXY его не накроет: "
        + ", ".join(offenders))


def test_factory_passes_proxy(monkeypatch):
    monkeypatch.setenv("MOEX_PROXY", "socks5://user:pass@node:1080")
    captured = {}

    class _Fake(httpx.AsyncClient):
        def __init__(self, **kw):
            captured.update(kw)
            super().__init__()

    monkeypatch.setattr(httpx, "AsyncClient", _Fake)
    md.moex_client()
    assert captured.get("proxy") == "socks5://user:pass@node:1080"


def test_factory_is_direct_without_env(monkeypatch):
    """Пустой MOEX_PROXY — прямое соединение, как было. Переключатель не должен
    менять поведение, пока его не включили."""
    monkeypatch.delenv("MOEX_PROXY", raising=False)
    captured = {}

    class _Fake(httpx.AsyncClient):
        def __init__(self, **kw):
            captured.update(kw)
            super().__init__()

    monkeypatch.setattr(httpx, "AsyncClient", _Fake)
    md.moex_client()
    assert "proxy" not in captured


def test_env_read_per_call_not_at_import(monkeypatch):
    """Узел меняют в .env и перезапускают контейнер, а не пересобирают образ."""
    monkeypatch.setenv("MOEX_PROXY", "socks5://a:1080")
    assert md._moex_proxy() == "socks5://a:1080"
    monkeypatch.setenv("MOEX_PROXY", "socks5://b:1080")
    assert md._moex_proxy() == "socks5://b:1080"
    monkeypatch.setenv("MOEX_PROXY", "   ")
    assert md._moex_proxy() is None
