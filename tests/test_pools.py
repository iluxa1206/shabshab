"""Разделение пулов: фоновый I/O не занимает воркеров, которыми обслуживаются
запросы API.

Регресс: пул был один на всё приложение (дефолтный to_thread, 6 воркеров на
двухъядерном хосте). Долгий фоновый вызов — VACUUM тикового архива, прун,
зависший запрос токена — выедал воркеры, и запросы к сайту вставали в очередь,
хотя процессор простаивал.
"""
import asyncio
import threading

from services.pools import BG_WORKERS, run_bg


def test_run_bg_uses_separate_threads():
    """Задачи run_bg исполняются в своём пуле (имя потока bgio), а не в том,
    что обслуживает to_thread."""
    async def main():
        bg = await run_bg(lambda: threading.current_thread().name)
        api = await asyncio.to_thread(lambda: threading.current_thread().name)
        return bg, api

    bg, api = asyncio.run(main())
    assert bg.startswith("bgio")
    assert not api.startswith("bgio")


def test_bg_congestion_does_not_block_to_thread():
    """Пул фонового I/O забит наглухо — to_thread всё равно отвечает."""
    started = threading.Event()
    release = threading.Event()

    def blocker():
        started.set()
        release.wait(timeout=5)

    async def main():
        tasks = [asyncio.create_task(run_bg(blocker)) for _ in range(BG_WORKERS)]
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        try:
            # очередь bg занята целиком, а этот путь свободен
            return await asyncio.wait_for(asyncio.to_thread(lambda: "api ok"), timeout=2)
        finally:
            release.set()
            await asyncio.gather(*tasks)

    assert asyncio.run(main()) == "api ok"
