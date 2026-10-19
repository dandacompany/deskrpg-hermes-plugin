"""Retry deferred original/copy cleanup without blocking the gateway event loop."""
import asyncio
import contextlib
import logging
from .conversation_files_store import FileStore, store_root

logger = logging.getLogger('deskrpg_plugin')


def cleanup_context(api):
    async def lifecycle(_app):
        async def sweep():
            while True:
                await asyncio.sleep(60)
                try:
                    await asyncio.to_thread(lambda: FileStore(store_root(api)).sweep_files())
                except Exception as exc:
                    logger.warning('[deskrpg] conversation file cleanup failed: %s', type(exc).__name__)
        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return lifecycle
