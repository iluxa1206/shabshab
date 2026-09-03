"""Сверка типа купона реестра с smart-lab — постоянный шаг аудита.

Зачем отдельный источник. Базу купона мы получаем от corpbonds (формула из
проспекта), а когда он выпуск не знает — выводим сами из истории выплат
(coupon_calib). Второй путь опирается на данные MOEX, и однажды это уже дало
сбой: обрыв пагинации bondization отдал ОБРЕЗАННЫЙ график, на нём «ставка не
менялась» выглядело правдой, и 20 живых флоатеров — включая ОФЗ-ПК — уехали в
FIXED. Пагинацию починили, но класс ошибки остался: наш вывод проверялся только
нашими же данными.

smart-lab пишет тип купона словами в заголовке страницы выпуска и о нашей
математике ничего не знает. Расхождение с ним — сигнал разбираться, причём в
обе стороны:
  • у нас FIXED, там флоатер → почти наверняка ошиблись мы (FIXED у бумаг из
    discovery — наш собственный вывод, а не проспект). Такой вердикт СНИМАЕМ
    автоматически: бумага возвращается в base=NULL, и конвейер определяет её
    заново. Руками зафиксированные (manual_locked) не трогаем.
  • у нас флоатер, там фикс → база могла прийти из проспекта, а ошибаться может
    и сайт. Только помечаем (sl_mismatch=1) — разбирает админ в Справочнике.
  • у нас база НЕ ОПРЕДЕЛЕНА, там фикс → ставим FIXED (старым выпускам, см.
    _STALE_BLIND_DAYS). Мы ничего не утверждали, спорить не с чем, а бумага с
    base=NULL не видна НИ во флоатерах, ни в ФИКСАХ: discovery заводит
    флоатером всё, у чего есть будущий купон без суммы, и фикс с офертой
    попадает туда же. На проде 03.09.2026 в этом лимбе висели 310 бумаг из
    1176 активных — МТС 1P-28, Роснефть 5Р2, РЖД-41, СУЭК, ИАДОМ, РКС Олимп.

Молчание сайта вердиктом не считается: «не знаем» ничего не опровергает.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Порция за прогон и темп: внешний сайт, ходим вежливо. Весь универс
# (~1200 бумаг) прокручивается примерно за месяц ежедневных синков.
SL_AUDIT_LIMIT = 40
_CONCURRENCY = 3
_DELAY = 0.3


async def run(limit: int = SL_AUDIT_LIMIT, isins: Optional[list[str]] = None,
              apply: bool = True) -> dict:
    """Проверить порцию бумаг. → {checked, typed, mismatch, reverted}."""
    from services import instruments_registry as reg
    from services.enrich_smartlab import fetch_smartlab_coupon_type

    targets = isins if isins is not None else await asyncio.to_thread(reg.list_sl_stale, limit)
    if not targets:
        return {"checked": 0, "typed": 0, "mismatch": 0, "reverted": 0}

    stats = {"checked": 0, "typed": 0, "mismatch": 0, "reverted": 0, "fixed": 0}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async with httpx.AsyncClient() as client:
        async def one(isin: str) -> None:
            async with sem:
                sl = await fetch_smartlab_coupon_type(isin, client)
                await asyncio.sleep(_DELAY)
            stats["checked"] += 1
            if sl:
                stats["typed"] += 1
            if not apply:
                return
            verdict = await asyncio.to_thread(reg.set_smartlab_type, isin, sl)
            if sl == "fixed" and not verdict:
                # «у нас база НЕ ОПРЕДЕЛЕНА, сайт говорит фикс» — расхождением не
                # считается (мы ничего не утверждали), но именно этот случай и
                # держал очередь: discovery заводит флоатером всё, у чего есть
                # будущий купон без суммы, а фикс с офертой выглядит так же.
                # Бумага с base=NULL не видна ни во флоатерах, ни в ФИКСАХ.
                # Только СТАРЫЕ выпуски: у свежего параметры доливает конвейер
                # (corpbonds/карточка биржи), и его вердикту доверия больше, чем
                # чужой странице в день размещения.
                await asyncio.to_thread(_fix_stale_blind, isin, stats)
            if not verdict:
                return
            stats["mismatch"] += 1
            row = await asyncio.to_thread(reg.get, isin) or {}
            if verdict == "mismatch_fixed" and not row.get("manual_locked"):
                # наш FIXED против «плавающего купона» на сайте — снимаем свой
                # вердикт, пусть конвейер определяет базу заново
                await asyncio.to_thread(reg.clear_base, isin)
                stats["reverted"] += 1
                logger.warning("smart-lab: %s (%s) помечен FIXED, а сайт видит флоатер "
                               "— база снята, бумага вернулась в очередь",
                               isin, row.get("short_name"))
            else:
                logger.warning("smart-lab: %s (%s) у нас base=%s, сайт говорит %s",
                               isin, row.get("short_name"), row.get("base"), sl)

        await asyncio.gather(*[one(i) for i in targets])
    return stats


# Свежий выпуск не отдаём внешней странице: параметры ему доливает наш конвейер,
# и в первые недели smart-lab обычно ещё зовёт флоатер фиксом.
_STALE_BLIND_DAYS = 45


def apply_known_fixed() -> dict:
    """Прогнать правило «без базы + smart-lab говорит фикс» по УЖЕ СОБРАННЫМ
    ответам сайта, без сети. → {checked, fixed}.

    Ротация сверки — 40 бумаг за прогон на ~1200 активных, то есть полный круг
    занимает месяц. Ответы прошлых кругов лежат в sl_type, и ждать нового захода,
    чтобы применить к ним правило, незачем: на проде 03.09.2026 таких «без базы,
    но фикс по сайту» было 210 из 310 слепых."""
    from services import instruments_registry as reg
    stats = {"checked": 0, "fixed": 0}
    for row in reg.list_blind_sl_fixed():
        stats["checked"] += 1
        _fix_stale_blind(row["isin"], stats)
    if stats["fixed"]:
        logger.info("smart-lab backlog: %d бумаг без базы переклассифицированы в FIXED "
                    "(проверено %d)", stats["fixed"], stats["checked"])
    return stats


def _fix_stale_blind(isin: str, stats: dict) -> None:
    """base=NULL + «фикс» со smart-lab → base='FIXED' (синхронная часть)."""
    from datetime import date
    from services import instruments_registry as reg
    row = reg.get(isin) or {}
    if row.get("base") is not None or row.get("manual_locked"):
        return
    try:
        age = (date.today() - date.fromisoformat((row.get("issue_date") or "")[:10])).days
    except ValueError:
        age = _STALE_BLIND_DAYS + 1      # дата размещения неизвестна = не свежий
    if age <= _STALE_BLIND_DAYS:
        return
    if reg.reclassify_fixed(isin):
        stats["fixed"] = stats.get("fixed", 0) + 1
        logger.info("smart-lab: %s (%s) без базы, сайт говорит фикс — base=FIXED, "
                    "бумага уходит во вкладку ФИКСОВ", isin, row.get("short_name"))
