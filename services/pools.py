"""Пулы потоков приложения: чей блокирующий вызов кого может задержать.

Три раздельных пула вместо одного общего:

* ДЕФОЛТНЫЙ (asyncio.to_thread) — только запросы API: чтения SQLite под ручки.
  Его размер задаётся в api.main при старте (см. API_POOL_WORKERS); держать его
  свободным — прямая цена отзывчивости сайта.
* run_bg (здесь) — ФОНОВЫЙ долгий I/O демонов: прун и VACUUM тикового архива,
  снапшоты, служебные пересчёты. Такие вызовы идут минутами, и в общем пуле они
  съедали воркеры, которыми обслуживаются запросы.
* run_heavy (services/heavy) — CPU-краш метрик, один поток: на двухъядерном
  хосте второе ядро всегда остаётся event loop'у.

Разделение появилось после прод-эпизода: шесть воркеров общего пула висели в
блокирующем запросе токена Alor (oauth не отвечал 15с), и весь сайт замирал —
не потому, что был занят процессор, а потому что кончились воркеры.
"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from functools import partial

# Три воркера: фоновые задачи в основном ждут диск и сеть, а не считают. Больше
# смысла нет — они и так конкурируют за два ядра с event loop и heavy-пулом.
BG_WORKERS = int(os.getenv("BG_POOL_WORKERS", "3"))

_BG = ThreadPoolExecutor(max_workers=BG_WORKERS, thread_name_prefix="bgio")


async def run_bg(fn, *args, **kwargs):
    """Как asyncio.to_thread, но в пуле ФОНОВОГО I/O — не занимает воркеры,
    которыми обслуживаются запросы API."""
    loop = asyncio.get_running_loop()
    if args or kwargs:
        fn = partial(fn, *args, **kwargs)
    return await loop.run_in_executor(_BG, fn)
