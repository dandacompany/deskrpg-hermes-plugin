"""A blocking check run off the event loop, waited for only briefly, and remembered for a while.

The plugin's routes run on the gateway's own event loop. A check that shells out or waits on I/O there stalls every
other request and, past the loop watchdog's tolerance, gets the whole gateway killed (exit 75). `SlowProbe` runs the
check in a worker thread, waits at most `timeout` for it, and caches the answer for `ttl` seconds. When the wait runs
out the caller gets the last answer (or None, "not known yet"); the check keeps running and fills the cache for the
next call. At most one check is in flight per probe, so a check that never returns costs one thread, not one per
request.
"""

import asyncio
import time
from typing import Any, Callable, Optional


class SlowProbe:
    def __init__(self, fn: Callable[[], Any], *, ttl: float, clock: Callable[[], float] = time.monotonic):
        self._fn = fn
        self._ttl = ttl
        self._clock = clock
        self._value: Any = None
        self._at: Optional[float] = None
        self._pending: Optional[asyncio.Future] = None

    async def get(self, timeout: float) -> Any:
        if self._at is not None and self._clock() - self._at < self._ttl:
            return self._value
        if self._pending is None:
            self._pending = asyncio.ensure_future(asyncio.to_thread(self._fn))
            self._pending.add_done_callback(self._settle)
        try:
            return await asyncio.wait_for(asyncio.shield(self._pending), timeout)
        except asyncio.TimeoutError:
            return self._value
        except Exception:  # noqa: BLE001 — a failed check is "not known", never a failed request
            return None

    def _settle(self, future: asyncio.Future) -> None:
        self._pending = None
        if future.cancelled():
            return
        self._value = None if future.exception() is not None else future.result()
        self._at = self._clock()
