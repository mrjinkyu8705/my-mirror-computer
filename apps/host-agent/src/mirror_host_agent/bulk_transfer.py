"""Cooperative SCTP bulk admission; no transport-priority or latency guarantee."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from typing import Any

BULK_CHUNK_BYTES = 16 * 1024
BULK_BUFFER_BYTES = 64 * 1024
CONTROL_BUSY_BYTES = 2 * 1024
BULK_POLL_SECONDS = 0.010
BULK_STALL_SECONDS = 30.0


async def wait_for_bulk_capacity(
    channel: Any,
    size: int,
    is_active: Callable[[], bool],
    bulk_channels: Callable[[], Iterable[Any]],
    control_channel: Callable[[], Any],
) -> bool:
    """Caller sends synchronously after success; re-check for every <=16KiB chunk.

    Yield before each chunk, keep combined outbound bulk queues bounded, and
    withhold new bulk bytes while control has queued work. Already-submitted
    SCTP bytes cannot be pre-empted. Cancellation/close is checked after waits.
    """
    if not 0 < size <= BULK_CHUNK_BYTES:
        raise ValueError("invalid bulk chunk size")
    if not is_active() or channel.readyState != "open":
        return False
    # A cooperative yield, not a fixed-rate speed cap (Windows timer resolution
    # can turn even a small sleep into a much larger throughput penalty).
    await asyncio.sleep(0)
    deadline = time.monotonic() + BULK_STALL_SECONDS
    while True:
        if not is_active() or channel.readyState != "open":
            return False
        buffered = sum(
            getattr(other, "bufferedAmount", 0)
            for other in bulk_channels()
            if other is not None and other.readyState == "open"
        )
        buffered = max(buffered, getattr(channel, "bufferedAmount", 0))
        control = control_channel()
        if (
            buffered + size <= BULK_BUFFER_BYTES
            and getattr(control, "bufferedAmount", 0) < CONTROL_BUSY_BYTES
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(BULK_POLL_SECONDS)
