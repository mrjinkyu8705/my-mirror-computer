"""Bounded file-only scheduling; no encoder/profile/ICE changes.

Credit bounds SCTP hand-off, not just bufferedAmount. Disk operations are
serialized in a worker, including cleanup; cancelling never closes a handle
under an in-flight thread operation. No filenames/content are logged here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from .file_transfer import FileTransferError

LOGGER = logging.getLogger('mirror_host_agent')
FILE_WINDOW_BYTES = 8 * 1024
FILE_SEND_BYTES = 4 * 1024
FILE_PACE_BYTES_PER_SECOND = 500_000
MAX_PENDING_FILE_BYTES = 256 * 1024


async def disk_call(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Keep ownership until the actual OS call returns. Never race close /
        # abort against a still-running open/read/write/rename operation.
        try:
            await task
        except Exception:
            pass
        raise


class FileFlow:
    def __init__(self, credit=False):
        self.credit = credit
        self.sent = 0
        self.received = 0
        self.peak = 0
        self.next_at = 0.0

    def acknowledge(self, received):
        if type(received) is int and self.received <= received <= self.sent:
            self.received = received

    async def wait(self, size, active):
        deadline = time.monotonic() + 30
        while active():
            now = time.monotonic()
            blocked = self.credit and self.sent + size - self.received > FILE_WINDOW_BYTES
            delay = max(0, self.next_at - now)
            if not blocked and delay <= 0:
                return True
            if now >= deadline:
                return False
            await asyncio.sleep(.010 if blocked else min(.010, max(.001, delay)))
        return False

    def mark_sent(self, size):
        self.sent += size
        self.peak = max(self.peak, self.sent - self.received)
        self.next_at = time.monotonic() + size / FILE_PACE_BYTES_PER_SECOND


class FileReceivePump:
    def __init__(self, receiver, name, size, digest, accepted, progress, done, failed):
        self.receiver = receiver
        self.queue = asyncio.Queue()
        self.pending = 0
        self.received = 0
        self.closed = False
        self.finishing = False
        self.accepted = False
        self.max_io_ms = 0.0
        self._callbacks = accepted, progress, done, failed
        self.task = asyncio.create_task(self._run(name, size, digest))

    def push(self, chunk):
        if (self.closed or self.finishing or not self.accepted or self.queue.qsize() >= 16 or
                self.pending + len(chunk) > MAX_PENDING_FILE_BYTES):
            return False
        self.pending += len(chunk)
        self.queue.put_nowait(chunk)
        return True

    def complete(self):
        if self.closed or self.finishing or not self.accepted:
            return False
        self.finishing = True
        self.queue.put_nowait(None)
        return True

    def close(self):
        if self.closed:
            return
        self.closed = True
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if item is not None:
                self.pending -= len(item)
        self.queue.put_nowait(None)

    async def _io(self, function, *args):
        before = time.perf_counter()
        try:
            return await disk_call(function, *args)
        finally:
            self.max_io_ms = max(self.max_io_ms, (time.perf_counter() - before) * 1000)

    async def _run(self, name, size, digest):
        accepted, progress, done, failed = self._callbacks
        try:
            await self._io(self.receiver.begin, name, size, digest)
            if self.closed:
                return
            self.accepted = True
            accepted()
            while not self.closed:
                item = await self.queue.get()
                if self.closed:
                    return
                if item is None:
                    result = await self._io(self.receiver.finish)
                    if not self.closed:
                        LOGGER.info('File receive diagnostics: credit-file-v1 bytes=%s max_disk_ms=%.1f', self.received, self.max_io_ms)
                        done(result)
                    return
                await self._io(self.receiver.write_chunk, item)
                self.pending -= len(item)
                self.received += len(item)
                if not self.closed:
                    progress(self.received)
        except FileTransferError as error:
            if not self.closed:
                failed(error.code)
        except OSError:
            if not self.closed:
                failed('WRITE_FAILED')
        except Exception:
            if not self.closed:
                failed('TRANSFER_FAILED')
        finally:
            self.close()
            try:
                await disk_call(self.receiver.abort)
            except OSError:
                LOGGER.warning('File receive cleanup deferred: type=OSError')
