"""Bounded, all-or-nothing UTF-8 clipboard transfer; no filesystem or input access."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import secrets
from collections.abc import Callable
from typing import Any

MAX_CHARS = 1_048_576  # UTF-16 code units, matching browser maxLength
RECEIVE_MAX_CHARS = 65_536
MAX_BYTES = MAX_CHARS * 4
CHUNK_BYTES = 32 * 1024
TIMEOUT_SECONDS = 60.0


class TextClipboardReceiver:
    def __init__(self, session_id: str, allowed: Callable[[], bool],
                 write: Callable[[str], bool], reply: Callable[[str, dict[str, Any]], None]):
        self.session_id = session_id
        self.allowed = allowed
        self.write = write
        self.reply = reply
        self.incoming: dict[str, Any] | None = None
        self.last_sequence = 0
        self.timer: asyncio.TimerHandle | None = None

    def close(self) -> None:
        self.incoming = None
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def _fail(self, code: str) -> None:
        incoming = self.incoming
        self.close()
        if incoming:
            self.reply('clipboard.text-error', {'transferId': incoming['transferId'], 'code': code})

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return (isinstance(value, str) and 16 <= len(value) <= 128
                and all(ch.isascii() and (ch.isalnum() or ch in '_-') for ch in value))

    def _parse(self, raw: str) -> dict[str, Any] | None:
        try:
            if len(raw.encode('utf-8')) > 4096:
                return None
            message = json.loads(raw)
        except (ValueError, UnicodeError):
            return None
        if (not isinstance(message, dict)
                or set(message) != {'data', 'event', 'sequence', 'sessionId', 'timestamp', 'version'}
                or message['sessionId'] != self.session_id
                or type(message['version']) is not int or message['version'] != 1
                or type(message['sequence']) is not int or message['sequence'] <= self.last_sequence
                or type(message['timestamp']) is not int
                or abs(message['timestamp'] - int(time.time() * 1000)) > 30_000
                or not isinstance(message['data'], dict)):
            return None
        data = message['data']
        if not self._valid_id(data.get('transferId')):
            return None
        if message['event'] == 'clipboard.text-offer':
            if (set(data) != {'transferId', 'encoding', 'chars', 'size', 'sha256'}
                    or data['encoding'] != 'utf-8'
                    or type(data['size']) is not int or not 1 <= data['size'] <= MAX_BYTES
                    or type(data['chars']) is not int or not 1 <= data['chars'] <= MAX_CHARS
                    or not isinstance(data['sha256'], str) or len(data['sha256']) != 64
                    or any(ch not in '0123456789abcdef' for ch in data['sha256'])):
                return None
        elif message['event'] in ('clipboard.text-complete', 'clipboard.text-cancel'):
            if set(data) != {'transferId'}:
                return None
        else:
            return None
        self.last_sequence = message['sequence']
        return message

    def receive(self, raw: Any) -> None:
        if self.incoming and time.monotonic() >= self.incoming['deadline']:
            self._fail('TIMEOUT')
        if isinstance(raw, (bytes, bytearray)):
            incoming = self.incoming
            if not incoming:
                return
            if not self.allowed():
                self._fail('CONTROL_INACTIVE')
                return
            if not 1 <= len(raw) <= CHUNK_BYTES or len(incoming['bytes']) + len(raw) > incoming['size']:
                self._fail('SIZE_MISMATCH')
                return
            incoming['bytes'].extend(raw)
            return
        if not isinstance(raw, str):
            return
        message = self._parse(raw)
        if message is None:
            return
        data, event = message['data'], message['event']
        transfer_id = data['transferId']
        if event == 'clipboard.text-offer':
            if not self.allowed():
                self.reply('clipboard.text-error', {'transferId': transfer_id, 'code': 'CONTROL_INACTIVE'})
                return
            if self.incoming:
                self.reply('clipboard.text-error', {'transferId': transfer_id, 'code': 'BUSY'})
                return
            self.incoming = {**data, 'bytes': bytearray(), 'deadline': time.monotonic() + TIMEOUT_SECONDS}
            try:
                self.timer = asyncio.get_running_loop().call_later(TIMEOUT_SECONDS, self._fail, 'TIMEOUT')
            except RuntimeError:
                pass  # Synchronous unit tests exercise the deadline check explicitly.
            self.reply('clipboard.text-accept', {'transferId': transfer_id})
            return
        incoming = self.incoming
        if not incoming or incoming['transferId'] != transfer_id:
            return
        if event == 'clipboard.text-cancel':
            self.close()
            return
        if not self.allowed():
            self._fail('CONTROL_INACTIVE')
            return
        payload = bytes(incoming['bytes'])
        if len(payload) != incoming['size']:
            self._fail('SIZE_MISMATCH')
            return
        if hashlib.sha256(payload).hexdigest() != incoming['sha256']:
            self._fail('HASH_MISMATCH')
            return
        try:
            text = payload.decode('utf-8', errors='strict')
            units = len(text.encode('utf-16-le')) // 2
        except UnicodeError:
            self._fail('INVALID_TEXT')
            return
        if units != incoming['chars'] or units > MAX_CHARS or '\x00' in text:
            self._fail('INVALID_TEXT')
            return
        # No native clipboard access until all bytes and characters are validated.
        try:
            written = self.write(text)
        except Exception:
            written = False
        if not written:
            self._fail('CLIPBOARD_WRITE_FAILED')
            return
        self.close()
        self.reply('clipboard.text-applied', {'transferId': transfer_id})


class TextClipboardDownloadSender:
    """Host -> viewer text over a distinct ordered channel, with bounded chunks.

    The remote receiver stages validated text; it never overwrites its system
    clipboard automatically. No text content or acknowledgement reason is logged.
    """
    def __init__(self, session_id, channel, message, allowed, wait_capacity):
        self.session_id, self.channel = session_id, channel
        self.message, self.allowed, self.wait_capacity = message, allowed, wait_capacity
        self.pending = None
        self.closed = False
        self.last_sequence = 0

    def _fail(self, code):
        pending = self.pending
        if pending is None:
            return
        pending['failed'] = True
        for key in ('accepted', 'applied'):
            if not pending[key].done():
                pending[key].set_exception(RuntimeError(code))

    def close(self):
        self.closed = True
        self._fail('DISCONNECTED')

    def receive(self, raw):
        if not isinstance(raw, str):
            return
        try:
            if len(raw.encode('utf-8')) > 4096:
                return
            msg = json.loads(raw)
        except (ValueError, UnicodeError):
            return
        pending = self.pending
        if (not pending or not isinstance(msg, dict) or
            set(msg) != {'data','event','sequence','sessionId','timestamp','version'} or
            msg['sessionId'] != self.session_id or type(msg['version']) is not int or msg['version'] != 1 or
            type(msg['sequence']) is not int or msg['sequence'] <= self.last_sequence or
            type(msg['timestamp']) is not int or abs(msg['timestamp']-int(time.time()*1000)) > 30_000 or
            not isinstance(msg['data'], dict) or msg['data'].get('transferId') != pending['id']):
            return
        event, data = msg['event'], msg['data']
        if event not in ('clipboard.text-accept','clipboard.text-applied','clipboard.text-error'):
            return
        if set(data) != ({'transferId','code'} if event == 'clipboard.text-error' else {'transferId'}):
            return
        if event == 'clipboard.text-error' and (not isinstance(data['code'], str) or
            not 1 <= len(data['code']) <= 48 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ_' for c in data['code'])):
            return
        self.last_sequence = msg['sequence']
        if event == 'clipboard.text-error':
            self._fail('RECEIVER_REJECTED')
        elif event == 'clipboard.text-accept' and pending['phase'] == 'offered' and not pending['accepted'].done():
            pending['accepted'].set_result(None)
        elif event == 'clipboard.text-applied' and pending['phase'] == 'complete' and not pending['applied'].done():
            pending['applied'].set_result(None)

    def _send(self, event, data):
        if self.closed or self.channel.readyState != 'open':
            raise RuntimeError('DISCONNECTED')
        self.channel.send(self.message(event, data))

    async def send(self, text):
        units = len(text.encode('utf-16-le')) // 2
        if not 1 <= units <= RECEIVE_MAX_CHARS or '\0' in text:
            raise ValueError('INVALID_TEXT')
        if self.pending is not None:
            raise RuntimeError('BUSY')
        loop = asyncio.get_running_loop()
        pending = dict(id=secrets.token_hex(16), accepted=loop.create_future(),
                       applied=loop.create_future(), phase='offered', failed=False)
        for key in ('accepted','applied'):
            pending[key].add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        self.pending = pending
        active = lambda: not self.closed and not pending['failed'] and self.allowed()
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                if not active():
                    raise RuntimeError('DISCONNECTED')
                payload = text.encode('utf-8')
                self._send('clipboard.text-offer', dict(transferId=pending['id'], encoding='utf-8',
                    chars=units, size=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
                await pending['accepted']
                pending['phase'] = 'streaming'
                # Match the shared bulk admission cap, never the full text size.
                from .bulk_transfer import BULK_CHUNK_BYTES
                for offset in range(0, len(payload), BULK_CHUNK_BYTES):
                    chunk = payload[offset:offset+BULK_CHUNK_BYTES]
                    if not active() or not await self.wait_capacity(self.channel, len(chunk), active):
                        raise RuntimeError('DISCONNECTED')
                    self.channel.send(chunk)
                pending['phase'] = 'complete'
                self._send('clipboard.text-complete', dict(transferId=pending['id']))
                await pending['applied']
                if not active():
                    raise RuntimeError('DISCONNECTED')
        except BaseException:
            try:
                self._send('clipboard.text-cancel', dict(transferId=pending['id']))
            except Exception:
                pass
            raise
        finally:
            for key in ('accepted','applied'):
                if not pending[key].done():
                    pending[key].cancel()
            self.pending = None
