"""Однопоточный executor для тяжёлого CPU-счёта (краншы метрик, reprice-циклы).

Зачем отдельный: asyncio.to_thread кидает всё в общий пул (6+ потоков), и на
двухъядерном хосте несколько тяжёлых краншей разом (поллер юниверса + фиксы +
движок + демон баров) насыщали обе ядра — event loop просыпался с лагом до 2с,
сайт «подвисал», хотя каждая операция по отдельности уже была в потоке.

Один worker = тяжёлые задачи выстраиваются в очередь друг за другом и занимают
максимум одно ядро; второе всегда остаётся event loop'у и лёгким to_thread
(чтения SQLite под запросы). Цена — фоновые пересчёты идут последовательно и
чуть дольше; им спешить некуда.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

_HEAVY = ThreadPoolExecutor(max_workers=1, thread_name_prefix="heavy")


async def run_heavy(fn, *args, **kwargs):
    """Как asyncio.to_thread, но в выделенном однопоточном пуле тяжёлого счёта."""
    loop = asyncio.get_running_loop()
    if args or kwargs:
        fn = partial(fn, *args, **kwargs)
    return await loop.run_in_executor(_HEAVY, fn)
