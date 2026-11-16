"""Application-owned safety adapter for aioice 0.10.x TURN transports.

Cloudflare rejects non-public channel targets. Filtering here, not in ICE,
preserves private candidates for direct LAN connectivity. aioice's channel-bind
failure path leaves peer_connect_waiters behind; serialize binds per peer and
always release that state. Never modify site-packages or authentication retries.
"""
from __future__ import annotations

import asyncio
from collections import deque
from functools import lru_cache
from importlib.metadata import version
from ipaddress import ip_address
import logging
import time

from aioice import turn

LOGGER = logging.getLogger("mirror_host_agent")
_BaseTransport = turn.TurnTransport


@lru_cache(maxsize=256)
def _public_target(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        # ICE resolves mDNS before constructing a remote candidate pair. Do not
        # send an unclassified hostname to a public TURN server.
        return False
    return address.is_global and not address.is_multicast


class SafeTurnTransport(_BaseTransport):
    def __init__(self, inner_protocol):
        super().__init__(inner_protocol)
        self._inner = inner_protocol
        self._cloudflare = inner_protocol.server[0].lower().rstrip(".") == "turn.cloudflare.com"
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._pending: set[asyncio.Task] = set()
        self._closing = False
        self._close_task: asyncio.Task | None = None
        self._reported: set[str] = set()
        self._diag_started = time.perf_counter()
        self._diag_task_peak = 0
        self._diag_wait = 0.
        self._diag_send = 0.
        self._diag_packets = 0
        self._diag_bytes = 0
        self._diag_window = deque()
        self._diag_window_bytes = 0
        self._diag_peak_bytes = 0
        self._diag_kinds = dict(rtp=0, rtcp=0, dtls=0, other=0)
        self._diag_delay_last = 0.0

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._closing:
            return
        if self._cloudflare and not _public_target(addr[0]):
            if "private" not in self._reported:
                self._reported.add("private")
                LOGGER.info("TURN policy: skipped non-public relay target; direct ICE unchanged")
            return
        task = asyncio.create_task(self._send(data, addr, time.perf_counter()))
        self._pending.add(task)
        self._diag_task_peak = max(self._diag_task_peak,len(self._pending))
        task.add_done_callback(self._completed)

    async def _send(self, data: bytes, addr: tuple[str, int], queued_at: float | None = None) -> None:
        dispatched = time.perf_counter()
        lock = self._locks.setdefault(addr, asyncio.Lock())
        async with lock:
            try:
                acquired = time.perf_counter()
                channels = getattr(self._inner, 'peer_to_channel', None)
                refreshes = getattr(self._inner, 'channel_refresh_at', None)
                phase = 'unknown'
                if isinstance(channels, dict) and isinstance(refreshes, dict):
                    channel = channels.get(addr)
                    refresh = refreshes.get(channel)
                    phase = 'bind' if channel is None else 'refresh' if isinstance(refresh, (float,int)) and time.time()>refresh else 'data'
                await self._inner.send_data(data, addr)
                done = time.perf_counter()
                # Only public packet-header categories, never content/addresses.
                kind = ('rtcp' if 192 <= data[1] <= 208 else 'rtp') if len(data) >= 2 and 128 <= data[0] <= 191 else (
                    'dtls' if data and 20 <= data[0] <= 63 else 'other')
                self._observe_send(len(data),queued_at if queued_at is not None else acquired,acquired,done,kind)
                queued = dispatched if queued_at is None else queued_at
                if done-queued >= .1 and done-self._diag_delay_last >= 1:
                    self._diag_delay_last = done
                    LOGGER.info('TURN delay diagnostics: dispatch_ms=%.1f lock_ms=%.1f send_ms=%.1f phase=%s kind=%s',
                                (dispatched-queued)*1000,(acquired-dispatched)*1000,(done-acquired)*1000,phase,kind)
            finally:
                # All sends on this transport use the same per-peer lock, so
                # there are no concurrent aioice waiters to abandon or awaken.
                self._inner.peer_connect_waiters.pop(addr, None)

    def _observe_send(self, size, queued, acquired, done, kind='other'):
        self._diag_wait = max(self._diag_wait,acquired-queued)
        self._diag_send = max(self._diag_send,done-acquired)
        self._diag_packets += 1;self._diag_bytes += size
        self._diag_kinds[kind] += 1
        while self._diag_window and (done-self._diag_window[0][0]>=.001 or len(self._diag_window)>=512):
            self._diag_window_bytes -= self._diag_window.popleft()[1]
        self._diag_window.append((done,size));self._diag_window_bytes += size
        self._diag_peak_bytes = max(self._diag_peak_bytes,self._diag_window_bytes)
        if done-self._diag_started >= 2:
            LOGGER.info('TURN send queue: packets=%s bytes=%s task_peak=%s max_wait_ms=%.2f '
                        'max_send_ms=%.2f peak_1ms_bytes=%s window_ms=%.0f '
                        'rtp_packets=%s rtcp_packets=%s dtls_packets=%s other_packets=%s',
                self._diag_packets,self._diag_bytes,self._diag_task_peak,self._diag_wait*1000,
                self._diag_send*1000,self._diag_peak_bytes,(done-self._diag_started)*1000,*self._diag_kinds.values())
            self._diag_started=done;self._diag_task_peak=0;self._diag_wait=0.;self._diag_send=0.
            self._diag_packets=0;self._diag_bytes=0;self._diag_peak_bytes=0
            self._diag_kinds = dict(rtp=0, rtcp=0, dtls=0, other=0)

    def _completed(self, task: asyncio.Task) -> None:
        self._pending.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            code = getattr(getattr(error, "response", None), "attributes", {}).get("ERROR-CODE", (None,))[0]
            category = f"{type(error).__name__}:{code}"
            if category not in self._reported:
                self._reported.add(category)
                # No exception text, peer addresses, credentials or payloads.
                LOGGER.warning("TURN operation failed: type=%s code=%s", type(error).__name__, code)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._close_task = asyncio.create_task(self._shutdown())
        self._close_task.add_done_callback(self._completed)

    async def _shutdown(self) -> None:
        pending = list(self._pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        refresh = self._inner.refresh_task
        try:
            await asyncio.wait_for(self._inner.delete(), timeout=2.0)
        finally:
            if refresh is not None:
                refresh.cancel()
                await asyncio.gather(refresh, return_exceptions=True)
            self._inner.transport.close()
            self._locks.clear()


def install_safe_turn_transport() -> None:
    if turn.TurnTransport is SafeTurnTransport:
        return
    # This adapter depends on the documented 0.10.x transport factory and the
    # inspected bind-waiter implementation. Re-review on a dependency upgrade.
    if not version("aioice").startswith("0.10."):
        raise RuntimeError("TURN adapter requires reviewed aioice 0.10.x")
    turn.TurnTransport = SafeTurnTransport
    LOGGER.info("TURN transport policy: safe-turn-v1; direct candidates and credentials unchanged")
