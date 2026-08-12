"""Разовая заливка ДАТ call-опциона из corpbonds в реестр (колонка call_dates).

ЗАЧЕМ. has_call отвечает лишь «опцион есть» — этого хватает на маркер `c` в
таблице, но не на прайсинг: горизонт call появляется, только когда известна
ДАТА (правилу цены нужно с чем сравнивать). MOEX даты колла не даёт вовсе — у
СибурХ1Р04/05/06 блок offers в bondization пуст, хотя колл ежемесячный с
14.12.2026, и спред считался к погашению 2032 года.

ГДЕ ДАТЫ. В таблице параметров corpbonds их нет — только в КАЛЕНДАРЕ ВЫПЛАТ,
строками «call-опцион» (парсер: services.enrich_corpbonds._parse_call_dates).
Колл обычно бермудский, поэтому в реестр ложится весь список; «ближайшая
будущая» вычисляется на дату расчёта (services.market_data._with_call_offers).

КОГО БЕРЁМ. Активные флоатеры с call_dates IS NULL и has_call ∈ {1, NULL}.
has_call=0 пропускаем: corpbonds уже сказал, что опциона нет.

ЗАПУСК (dry-run по умолчанию, APPLY=1 — запись):
    .venv/bin/python scripts/backfill_call_dates.py
    APPLY=1 .venv/bin/python scripts/backfill_call_dates.py

В проде — внутри контейнера, чтобы писать в тот же том:
    docker compose -f docker-compose.prod.yml exec -e APPLY=1 floaters \
        python scripts/backfill_call_dates.py

Переменные: LIMIT (сколько бумаг за прогон, по умолчанию 400), DELAY (пауза
между запросами к corpbonds, сек, 0.7), LIST_ONLY=1 (показать кандидатов),
ONLY=ISIN,ISIN (точечный прогон), BIND (en0 либо IP — обход VPN-туннеля, см.
scripts/backfill_has_call.py).
"""
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import instruments_registry as reg  # noqa: E402
from services.enrich_corpbonds import fetch_corpbonds  # noqa: E402

APPLY = os.environ.get("APPLY") == "1"
LIST_ONLY = os.environ.get("LIST_ONLY") == "1"
LIMIT = int(os.environ.get("LIMIT", "400"))
DELAY = float(os.environ.get("DELAY", "0.7"))
ONLY = [s.strip().upper() for s in (os.environ.get("ONLY") or "").split(",") if s.strip()]
_DEAD_AFTER = 5   # промахов подряд с начала = сайт лежит, обрываем прогон


def candidates() -> list[dict]:
    if ONLY:
        return [{"isin": i, "short_name": "", "has_call": None} for i in ONLY]
    return reg.list_call_dates_missing()


async def main() -> None:
    cands = candidates()
    print(f"кандидатов (call_dates IS NULL, has_call ∈ 1/NULL): {len(cands)}; "
          f"берём {min(LIMIT, len(cands))}, apply={APPLY}", flush=True)
    if not cands or LIST_ONLY:
        for r in cands:
            print(f"  {r['isin']}  {(r.get('short_name') or ''):<14} has_call={r.get('has_call')}")
        return

    import httpx
    from services.enrich_corpbonds import _UA
    # обход VPN-туннеля переиспользуем из соседнего бэкфилла: та же переменная BIND
    from scripts.backfill_has_call import setup_tunnel_bypass

    today = date.today().isoformat()
    stats = {"with_dates": 0, "empty": 0, "not_found": 0}
    async with httpx.AsyncClient(headers=_UA, timeout=15,
                                 **setup_tunnel_bypass()) as client:
        for n, r in enumerate(cands[:LIMIT], 1):
            isin = r["isin"]
            parsed = await fetch_corpbonds(isin, client=client)
            await asyncio.sleep(DELAY)
            if parsed is None:
                stats["not_found"] += 1
                if n == _DEAD_AFTER and stats["not_found"] == _DEAD_AFTER:
                    print(f"первые {_DEAD_AFTER} запросов подряд без ответа — "
                          f"corpbonds.ru недоступен, обрываю (проверь сеть/VPN)")
                    return
                continue
            dates = parsed.get("call_dates") or []
            if dates:
                stats["with_dates"] += 1
                future = [d for d in dates if d > today]
                print(f"  CALL  {isin} {(r.get('short_name') or ''):<12} "
                      f"дат={len(dates)}  ближайшая будущая={future[0] if future else '—'}",
                      flush=True)
            else:
                stats["empty"] += 1
            if APPLY:
                reg.set_call_dates(isin, dates)
                if parsed.get("has_call") is not None:
                    reg.set_has_call(isin, parsed["has_call"])
    print("итог:", stats)
    if not APPLY:
        print("dry-run: ничего не записано, повтори с APPLY=1")


if __name__ == "__main__":
    asyncio.run(main())
