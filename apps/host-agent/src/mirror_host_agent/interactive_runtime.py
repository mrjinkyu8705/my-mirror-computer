"""Ordered, bounded Win32 work outside the media/signaling event loop.

One process-wide input worker preserves order across reconnects. Release-all
invalidates queued work, and releases only keys/buttons actually submitted by
this worker. Clipboard reads have a separate single-worker pool so a delayed
clipboard owner cannot consume capture/encoder executor threads.
"""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time
from .control_trace import INPUT_TRACE

LOGGER = logging.getLogger('mirror_host_agent')
_CLIPBOARD_READER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mirror-clipboard-read')


async def read_clipboard_off_loop(function):
    before = time.perf_counter()
    result = await asyncio.get_running_loop().run_in_executor(_CLIPBOARD_READER, function)
    elapsed = (time.perf_counter() - before) * 1000
    if elapsed >= 50:
        LOGGER.info('Clipboard read diagnostics: elapsed_ms=%.1f; content omitted', elapsed)
    return result


class InputDispatcher:
    def __init__(self):
        self.condition = threading.Condition()
        self.queue = deque()
        self.current = None
        self.reset = False
        self.stopping = False
        self.thread = None
        self.idle = threading.Event()
        self.idle.set()
        self.last_diagnostic = 0.0

    def activate(self, scope):
        with self.condition:
            self.current = scope
            self.queue.clear()
            self.reset = True
            self.idle.clear()
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='mirror-input', daemon=True)
                self.thread.start()
            self.condition.notify()

    def release(self, scope, close=False):
        with self.condition:
            if self.current is not scope:
                return
            scope.epoch += 1
            if close:
                self.current = None
            self.queue.clear()
            self.reset = True
            self.idle.clear()
            self.condition.notify()

    def submit(self, scope, operation, args):
        failed = False
        with self.condition:
            if self.current is not scope or scope.failed:
                return
            item = (scope, scope.epoch, operation, args, time.perf_counter(), INPUT_TRACE.get())
            if operation == 'move_absolute' and self.queue and self.queue[-1][0] is scope and self.queue[-1][2] == operation:
                self.queue[-1] = item
            elif len(self.queue) < 256:
                self.queue.append(item)
            else:
                scope.failed = True
                self.current = None
                self.queue.clear()
                self.reset = True
                failed = True
            self.idle.clear()
            self.condition.notify()
        if failed:
            scope.notify_failure()

    def _run(self):
        backend = None
        keys, buttons = set(), set()
        def release_actual():
            if backend is not None:
                # A failed release must not prevent cleanup of other held keys.
                for operation, held in (('key', keys), ('mouse_button', buttons)):
                    for value in tuple(held):
                        try:
                            getattr(backend, operation)(value, 'up')
                        except Exception as error:
                            LOGGER.warning('Input release failed: type=%s', type(error).__name__)
            keys.clear(); buttons.clear()
        while True:
            with self.condition:
                if not self.reset and not self.queue:
                    self.idle.set()
                self.condition.wait_for(lambda: self.reset or self.queue or self.stopping)
                resetting = self.reset
                if resetting:
                    self.reset = False
                    item = None
                elif self.queue:
                    item = self.queue.popleft()
                else:
                    break
            if resetting:
                try:
                    release_actual()
                except Exception as error:
                    LOGGER.warning('Input release failed: type=%s', type(error).__name__)
                    keys.clear(); buttons.clear()
                backend = None
                continue
            scope, epoch, operation, args, queued, trace_context = item
            with self.condition:
                if self.current is not scope or scope.epoch != epoch or scope.failed:
                    continue
            backend = scope.backend
            started = time.perf_counter()
            try:
                if operation in {'key', 'mouse_button'} and args[1] == 'down':
                    (keys if operation == 'key' else buttons).add(args[0])
                getattr(backend, operation)(*args)
                if operation in {'key', 'mouse_button'}:
                    held = keys if operation == 'key' else buttons
                    if args[1] == 'down': held.add(args[0])
                    else: held.discard(args[0])
            except Exception as error:
                LOGGER.warning('Input OS call failed: operation=%s type=%s', operation, type(error).__name__)
                scope.failed = True
                self.release(scope, close=True)
                scope.notify_failure()
            elapsed = (time.perf_counter() - started) * 1000
            queued_ms = (started - queued) * 1000
            if trace_context is not None:
                trace, sequence = trace_context
                trace.safe(trace.input_done,sequence,queued_ms,elapsed)
            if (elapsed >= 50 or queued_ms >= 100) and time.perf_counter()-self.last_diagnostic >= 1:
                self.last_diagnostic = time.perf_counter()
                LOGGER.info('Input OS diagnostics: operation=%s elapsed_ms=%.1f queue_ms=%.1f', operation, elapsed, queued_ms)

    def shutdown(self):
        with self.condition:
            self.current = None
            self.queue.clear()
            self.reset = True
            self.stopping = True
            self.condition.notify()


_INPUT_DISPATCHER = InputDispatcher()


class QueuedInputSink:
    def __init__(self, backend, dispatcher=None):
        self.backend = backend
        self.dispatcher = dispatcher or _INPUT_DISPATCHER
        self.epoch = 0
        self.failed = False
        self.loop = None
        self.failure_handler = None

    def start(self):
        self.loop = asyncio.get_running_loop()
        self.dispatcher.activate(self)

    def set_failure_handler(self, handler):
        self.failure_handler = handler

    def notify_failure(self):
        if self.failure_handler and self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.failure_handler)

    def move_absolute(self, x, y): self.dispatcher.submit(self, 'move_absolute', (x, y))
    def mouse_button(self, button, action): self.dispatcher.submit(self, 'mouse_button', (button, action))
    def wheel(self, x, y): self.dispatcher.submit(self, 'wheel', (x, y))
    def key(self, code, action): self.dispatcher.submit(self, 'key', (code, action))
    def type_text(self, text): self.dispatcher.submit(self, 'type_text', (text,))
    def release_all(self): self.dispatcher.release(self)
    def close(self): self.dispatcher.release(self, close=True)
