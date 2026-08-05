"""Разовая заливка has_call (наличие call-опциона эмитента) из corpbonds в реестр.

ЗАЧЕМ. MOEX bondization даёт ДАТЫ оферт, но не их вид: на всём универсе offertype
принимает лишь значения 'Оферта', 'Оферта (состоялось)', 'Оферта/Погашение' — чей
это опцион (держателя или эмитента), оттуда не узнать. Единственный источник
call-флага — corpbonds («Наличие call-опциона»). Без этого маркер `c` у даты
погашения в таблице никогда бы не зажёгся.

ПОЧЕМУ ОТДЕЛЬНЫЙ СКРИПТ. Дневной синк скрейпит corpbonds только для проблемных
бумаг (incomplete/suspect/exotic/no_spec) плюс новая квота _CORPBONDS_QUOTA_CALL
(10/прогон) — здоровый флоатер с офертой добирался бы неделями. Скрипт заливает
их разом, синк потом лишь поддерживает.

КОГО БЕРЁМ. Активные флоатеры с has_call IS NULL и БУДУЩЕЙ офертой в кэше
bondization (по ним маркер и рисуется). Расписание берём из кэша, в MOEX за
списком кандидатов не ходим.

Кэш читаем ФАЙЛОМ, а не через MarketDataService: тот выбрасывает всё, что не
сегодняшнего торгового дня, и разовый запуск до утреннего прогрева видел бы ноль
кандидатов. Даты оферт внутри дня не меняются, так что вчерашнего снапшота для
ОТБОРА достаточно; бумаги, чьё расписание появилось сегодня, доберёт следующий
запуск или дневная квота синка.

ЗАПУСК (dry-run по умолчанию, APPLY=1 — запись):
    .venv/bin/python scripts/backfill_has_call.py
    APPLY=1 .venv/bin/python scripts/backfill_has_call.py

В проде — внутри контейнера, чтобы писать в тот же том:
    docker compose -f docker-compose.prod.yml exec -e APPLY=1 floaters \
        python scripts/backfill_has_call.py

Переменные: LIMIT (сколько бумаг за прогон, по умолчанию 300), DELAY (пауза между
запросами к corpbonds, сек, по умолчанию 0.7 — внешний сайт, не долбим),
LIST_ONLY=1 (показать кандидатов и выйти, в сеть не ходить).

BIND (en0 либо IP) — обход VPN-туннеля, ровно как DEPLOY_SSH_BIND в scripts/
deploy.sh. При VPN в режиме TUN дефолтный маршрут уходит в туннель, DNS отдаёт
fake-ip (198.18.x.x), и corpbonds.ru молча таймаутит. С BIND скрипт резолвит
хост настоящим DNS мимо туннеля и привязывает исходящий сокет к физическому
интерфейсу — системные настройки не трогаются:
    BIND=en0 .venv/bin/python scripts/backfill_has_call.py
В контейнере на проде BIND не нужен (туннеля нет) — вся ветка не активируется.
"""
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.valuation import next_offer_info  # noqa: E402
from services import instruments_registry as reg  # noqa: E402
from services.enrich_corpbonds import fetch_corpbonds  # noqa: E402
from services.market_data import SCHEDULE_FULL_CACHE_FILE  # noqa: E402

APPLY = os.environ.get("APPLY") == "1"
LIST_ONLY = os.environ.get("LIST_ONLY") == "1"   # только показать кандидатов, без сети
ALL = os.environ.get("ALL") == "1"               # весь универс, не только бумаги с офертой
LIMIT = int(os.environ.get("LIMIT", "300"))
DELAY = float(os.environ.get("DELAY", "0.7"))
BIND = os.environ.get("BIND", "")   # en0 | IP — обход VPN-туннеля (см. шапку)
_DEAD_AFTER = 5   # столько промахов ПОДРЯД с начала = сайт лежит, обрываем прогон
_CB_HOST = "corpbonds.ru"


def _bind_ip() -> str:
    """BIND → IPv4. Принимает имя интерфейса (en0) или сразу адрес."""
    import subprocess
    try:
        out = subprocess.run(["ipconfig", "getifaddr", BIND], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return out or BIND
    except (OSError, subprocess.SubprocessError):
        return BIND


def _real_ip(host: str, src: str) -> str | None:
    """Настоящий A-адрес хоста: публичный DNS запрашивается С ФИЗИЧЕСКОГО адреса,
    иначе ответ приходит от резолвера туннеля (fake-ip 198.18.x.x)."""
    import subprocess
    try:
        out = subprocess.run(
            ["dig", "@8.8.8.8", "+short", "+time=3", "+tries=1", "-b", src, host],
            capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    ips = [x for x in out if x.count(".") == 3 and not x.startswith("198.18.")]
    return ips[0] if ips else None


def setup_tunnel_bypass() -> dict:
    """kwargs для httpx.AsyncClient: сокет с физического интерфейса + подмена
    резолва corpbonds.ru на настоящий IP. Пусто, если BIND не задан или обойти
    не вышло (тогда идём обычным путём и, если туннель глушит, упрёмся в
    _DEAD_AFTER с внятным сообщением)."""
    import socket
    if not BIND:
        return {}
    src = _bind_ip()
    ip = _real_ip(_CB_HOST, src)
    if not ip:
        print(f"BIND={BIND}: не удалось узнать настоящий IP {_CB_HOST}, иду обычным путём")
        return {}
    print(f"обход туннеля: сокет с {src}, {_CB_HOST} → {ip}")
    _orig = socket.getaddrinfo

    def _patched(host, port, *a, **kw):
        return _orig(ip if host == _CB_HOST else host, port, *a, **kw)

    socket.getaddrinfo = _patched
    import httpx
    return {"transport": httpx.AsyncHTTPTransport(local_address=src, retries=1)}


def _schedules() -> dict:
    """Дисковый кэш bondization {isin: {coupons, amorts, offers}} БЕЗ проверки
    свежести (см. шапку: даты оферт внутри дня стабильны)."""
    import json
    try:
        with open(SCHEDULE_FULL_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"кэш расписаний недоступен ({e}) — сначала прогрей синком")
        return {}
    print(f"кэш расписаний: {SCHEDULE_FULL_CACHE_FILE} от {raw.get('date')}, "
          f"{len(raw.get('items') or {})} бумаг")
    return raw.get("items") or {}


def candidates() -> list[dict]:
    """has_call IS NULL среди активных флоатеров. По умолчанию сужаем до бумаг с
    будущей офертой (только у них маркер и рисуется); ALL=1 — весь универс, чтобы
    закрыть флаг разом и не ждать квоту синка по 10 за прогон."""
    today = date.today()
    rows = reg.list_call_unknown()
    if ALL:
        return rows
    sched_by = _schedules()
    out = []
    for r in rows:
        sched = sched_by.get(r["isin"])
        if not sched:
            continue
        off = next_offer_info(sched.get("offers"), today)
        if off:
            out.append({**r, "offer_date": off[0].isoformat(), "offer_type": off[1]})
    return out


async def main() -> None:
    cands = candidates()
    scope = "весь универс" if ALL else "будущая оферта"
    print(f"кандидатов (has_call IS NULL, {scope}): {len(cands)}; "
          f"берём {min(LIMIT, len(cands))}, apply={APPLY}", flush=True)
    if not cands or LIST_ONLY:
        for r in cands:
            off = f"оферта {r['offer_date']}  {r['offer_type']}" if r.get("offer_date") else ""
            print(f"  {r['isin']}  {(r.get('short_name') or ''):<14} {off}")
        return
    import httpx
    from services.enrich_corpbonds import _UA
    stats = {"yes": 0, "no": 0, "no_field": 0, "not_found": 0}
    async with httpx.AsyncClient(headers=_UA, timeout=15,
                                 **setup_tunnel_bypass()) as client:
        for n, r in enumerate(cands[:LIMIT], 1):
            isin = r["isin"]
            parsed = await fetch_corpbonds(isin, client=client)
            await asyncio.sleep(DELAY)
            if parsed is None:
                stats["not_found"] += 1
                # Подряд идущие промахи в начале = сайт недоступен (нет VPN/лежит),
                # а не «бумаг нет на corpbonds». Без обрыва прогон молча выжигает
                # 15-секундный таймаут на каждой из сотни бумаг (~26 минут в пустоту).
                if n == _DEAD_AFTER and stats["not_found"] == _DEAD_AFTER:
                    print(f"первые {_DEAD_AFTER} запросов подряд без ответа — "
                          f"corpbonds.ru недоступен, обрываю (проверь сеть/VPN)")
                    return
                continue
            hc = parsed.get("has_call")
            if hc is None:
                stats["no_field"] += 1
                continue
            stats["yes" if hc else "no"] += 1
            if hc:
                off = f" (оферта {r['offer_date']})" if r.get("offer_date") else ""
                print(f"  CALL  {isin} {r.get('short_name') or ''}{off}", flush=True)
            if APPLY:
                reg.set_has_call(isin, hc)
    print("итог:", stats)
    if not APPLY:
        print("dry-run: ничего не записано, повтори с APPLY=1")


if __name__ == "__main__":
    asyncio.run(main())
