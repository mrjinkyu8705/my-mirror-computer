"""Bulk clipboard tests never touch the real system clipboard or remote input."""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from aiortc import RTCConfiguration, RTCPeerConnection
from mirror_host_agent.text_clipboard import CHUNK_BYTES, MAX_BYTES, MAX_CHARS, TextClipboardReceiver
from mirror_host_agent import windows_clipboard
from mirror_host_agent.__main__ import AgentConfig, M0Agent

SESSION = 'session_0123456789abcdef'
TRANSFER = 'transfer_0123456789abcdef'


def message(event: str, data: dict, sequence: int = 1, **overrides) -> str:
    return json.dumps(dict(event=event, data=data, sequence=sequence, sessionId=SESSION,
                           timestamp=int(time.time() * 1000), version=1, **overrides))


def offer(text: str, **overrides) -> str:
    payload = text.encode('utf-8')
    data = dict(transferId=TRANSFER, encoding='utf-8', chars=len(text.encode('utf-16-le')) // 2,
                size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    data.update(overrides)
    return message('clipboard.text-offer', data)


def stream(receiver: TextClipboardReceiver, payload: bytes) -> None:
    for offset in range(0, len(payload), CHUNK_BYTES):
        receiver.receive(payload[offset:offset + CHUNK_BYTES])


class TextReceiverTests(unittest.TestCase):
    def setUp(self):
        self.writes = []
        self.replies = []
        self.allowed = True
        self.receiver = TextClipboardReceiver(SESSION, lambda: self.allowed,
            lambda text: self.writes.append(text) or True,
            lambda event, data: self.replies.append((event, data)))

    def complete(self, sequence=2):
        self.receiver.receive(message('clipboard.text-complete', {'transferId': TRANSFER}, sequence))

    def test_85000_korean_characters_written_exactly_once_only_after_complete(self):
        text = '한' * 85_000
        self.receiver.receive(offer(text))
        self.assertEqual(self.replies[0][0], 'clipboard.text-accept')
        stream(self.receiver, text.encode('utf-8'))
        self.assertEqual(self.writes, [])
        self.complete()
        self.complete(3)
        self.assertEqual(self.writes, [text])
        self.assertEqual(self.replies[-1][0], 'clipboard.text-applied')
        self.assertIsNone(self.receiver.incoming)

    def test_exact_limit_with_astral_characters_matches_browser_utf16_units(self):
        text = '\U0001f600' * (MAX_CHARS // 2)
        self.receiver.receive(offer(text))
        stream(self.receiver, text.encode('utf-8'))
        self.complete()
        self.assertEqual(self.writes, [text])
        self.assertEqual(MAX_CHARS, windows_clipboard.CLIPBOARD_TEXT_WRITE_MAX_LENGTH)

    def test_full_1048576_korean_character_payload_is_not_truncated(self):
        self.assertEqual(MAX_CHARS, 1_048_576)
        self.assertEqual(MAX_BYTES, 4_194_304)
        text = '한' * MAX_CHARS
        self.receiver.receive(offer(text))
        stream(self.receiver, text.encode('utf-8'))
        self.complete()
        self.assertEqual(self.writes, [text])
        self.assertEqual(self.replies[-1][0], 'clipboard.text-applied')

    def test_invalid_metadata_never_allocates_or_writes(self):
        valid = json.loads(offer('hello'))
        invalid = [
            {**valid, 'sessionId': 'wrong_session_0123456789'},
            {**valid, 'timestamp': 1}, {**valid, 'sequence': True},
            {**valid, 'version': True}, {**valid, 'command': 'not-allowed'},
        ]
        for override in [dict(chars=MAX_CHARS+1), dict(size=MAX_BYTES+1), dict(size=True),
                         dict(chars=0), dict(encoding='utf-16'), dict(extra=1), dict(sha256='g'*64)]:
            invalid.append({**valid, 'data': {**valid['data'], **override}})
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                self.receiver.receive(json.dumps(metadata))
                self.assertIsNone(self.receiver.incoming)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.replies, [])

    def test_size_hash_encoding_and_character_mismatches_never_write(self):
        cases = [('abc', b'ab', {}, 'SIZE_MISMATCH'),
                 ('abc', b'abd', {}, 'HASH_MISMATCH'),
                 ('abc', b'abc', {'chars': 2}, 'INVALID_TEXT'),
                 ('a\x00b', b'a\x00b', {}, 'INVALID_TEXT'),
                 ('abc', b'\xffab', {'sha256': hashlib.sha256(b'\xffab').hexdigest()}, 'INVALID_TEXT')]
        for text, payload, overrides, expected in cases:
            with self.subTest(expected=expected):
                self.setUp()
                self.receiver.receive(offer(text, **overrides))
                stream(self.receiver, payload)
                self.complete()
                self.assertEqual(self.writes, [])
                self.assertEqual(self.replies[-1][1]['code'], expected)

    def test_oversized_or_overflow_chunk_discards_partial_buffer(self):
        for payload in [b'a' * (CHUNK_BYTES+1), b'abcdef']:
            self.setUp()
            self.receiver.receive(offer('abc'))
            self.receiver.receive(payload)
            self.assertIsNone(self.receiver.incoming)
            self.assertEqual(self.replies[-1][1]['code'], 'SIZE_MISMATCH')
            self.assertEqual(self.writes, [])

    def test_cancel_revocation_and_expiry_do_not_write(self):
        for condition in ['cancel', 'revoked', 'timeout', 'close']:
            with self.subTest(condition=condition):
                self.setUp()
                self.receiver.receive(offer('hello'))
                self.receiver.receive(b'hel')
                if condition == 'cancel':
                    self.receiver.receive(message('clipboard.text-cancel', {'transferId': TRANSFER}, 2))
                elif condition == 'revoked':
                    self.allowed = False
                elif condition == 'timeout':
                    self.receiver.incoming['deadline'] = 0
                else:
                    self.receiver.close()
                self.receiver.receive(b'lo')
                self.complete(3)
                self.assertEqual(self.writes, [])
                self.assertIsNone(self.receiver.incoming)

    def test_no_permission_or_failed_native_write_cannot_ack_applied(self):
        self.allowed = False
        self.receiver.receive(offer('hello'))
        self.assertEqual(self.replies[-1][1]['code'], 'CONTROL_INACTIVE')
        self.allowed = True
        self.receiver.write = lambda text: False
        self.receiver.receive(message('clipboard.text-offer', json.loads(offer('hello'))['data'], 2))
        stream(self.receiver, b'hello')
        self.complete(3)
        self.assertEqual(self.replies[-1][1]['code'], 'CLIPBOARD_WRITE_FAILED')
        self.assertNotIn('clipboard.text-applied', [event for event, _ in self.replies])

    def test_second_offer_and_replayed_complete_cannot_replace_pending_transfer(self):
        self.receiver.receive(offer('hello'))
        self.receiver.receive(message('clipboard.text-offer', json.loads(offer('bad', transferId='other_0123456789abcdef'))['data'], 2))
        self.assertEqual(self.replies[-1][1]['code'], 'BUSY')
        self.receiver.receive(message('clipboard.text-complete', {'transferId': TRANSFER}, 1))
        stream(self.receiver, b'hello')
        self.complete(3)
        self.assertEqual(self.writes, ['hello'])


class NativeWriterTests(unittest.TestCase):
    def test_native_buffer_receives_85000_and_maximum_characters_without_real_clipboard_access(self):
        for count in [85_000, MAX_CHARS]:
            with self.subTest(count=count):
                text = '한' * count
                destination = ctypes.create_unicode_buffer(len(text)+1)
                user, kernel = MagicMock(), MagicMock()
                user.OpenClipboard.return_value = True
                user.EmptyClipboard.return_value = True
                user.SetClipboardData.return_value = 1
                kernel.GlobalAlloc.return_value = 1
                kernel.GlobalLock.return_value = ctypes.addressof(destination)
                with patch.object(windows_clipboard.sys, 'platform', 'win32'), \
                     patch.object(windows_clipboard.ctypes, 'WinDLL', side_effect=[user, kernel], create=True), \
                     patch.object(windows_clipboard, '_configure'):
                    self.assertTrue(windows_clipboard.write_clipboard_text(text))
                self.assertEqual(destination.value, text)
                user.SetClipboardData.assert_called_once()
                kernel.GlobalFree.assert_not_called()

    def test_rejects_bad_text_before_any_native_access(self):
        with patch.object(windows_clipboard.sys, 'platform', 'win32'), \
             patch.object(windows_clipboard.ctypes, 'WinDLL', create=True) as dll:
            for text in ['a' * (MAX_CHARS+1), 'a\x00b', '\ud800']:
                self.assertFalse(windows_clipboard.write_clipboard_text(text))
            dll.assert_not_called()


class AgentTextWiringTests(unittest.TestCase):
    def test_agent_channel_reassembles_full_text_and_tracks_echo_without_logging_contents(self):
        agent = M0Agent(AgentConfig(device_id='device_0123456789abcdef',
            session_id=SESSION, ticket='test-only', ws_url='ws://127.0.0.1:8787/ws',
            heartbeat_stop_after_seconds=None))
        agent._clipboard_enabled = True
        agent._granted_control = True
        class Channel:
            readyState = 'open'
            handlers = {}
            replies = []
            def on(self, event):
                def attach(handler):
                    self.handlers[event] = handler
                    return handler
                return attach
            def send(self, raw): self.replies.append(json.loads(raw))
        channel = Channel()
        text = '한' * 85_000
        with patch.object(agent, '_control_grant_is_active', return_value=True), \
             patch.object(windows_clipboard, 'write_clipboard_text', return_value=True) as write, \
             patch.object(windows_clipboard, 'get_clipboard_sequence_number', return_value=42):
            agent._attach_clipboard_text_channel(channel)
            channel.handlers['message'](offer(text))
            stream(agent._clipboard_text_receiver, text.encode('utf-8'))
            channel.handlers['message'](message('clipboard.text-complete', {'transferId': TRANSFER}, 2))
            write.assert_called_once_with(text)
            self.assertEqual(channel.replies[-1]['event'], 'clipboard.text-applied')
            self.assertEqual(agent._clipboard_text_echo, text[:65_536])
            self.assertEqual(agent._clipboard_text_echo_sequence, 42)
            channel.handlers['close']()
            self.assertIsNone(agent._clipboard_text_receiver)


class RealDataChannelTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        # Windows Proactor UDP teardown may wait indefinitely on connection_lost
        # in a two-peer test. Use a private selector loop, not a global policy.
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)

    async def test_real_ordered_webrtc_channel_transfers_85000_korean_characters(self):
        # Two local aiortc peers, no STUN/TURN, synthetic in-memory writer only.
        sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        host = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        opened, accepted, applied = asyncio.Event(), asyncio.Event(), asyncio.Event()
        writes = []
        receiver = None
        channel = sender.createDataChannel('clipboard-text-v1', ordered=True)
        channel.on('open', opened.set)
        @channel.on('message')
        def on_reply(raw):
            event = json.loads(raw)['event']
            if event == 'clipboard.text-accept': accepted.set()
            if event == 'clipboard.text-applied': applied.set()
        @host.on('datachannel')
        def on_channel(remote):
            nonlocal receiver
            receiver = TextClipboardReceiver(SESSION, lambda: True,
                lambda text: writes.append(text) or True,
                lambda event, data: remote.send(message(event, data)))
            remote.on('message', receiver.receive)
        try:
            # Restrict test candidate gathering to loopback, avoiding unrelated
            # VPN/virtual adapters and Windows interface enumeration delays.
            with patch('aioice.ice.get_host_addresses', return_value=['127.0.0.1']):
                await asyncio.wait_for(sender.setLocalDescription(await sender.createOffer()), 10)
            await host.setRemoteDescription(sender.localDescription)
            with patch('aioice.ice.get_host_addresses', return_value=['127.0.0.1']):
                await asyncio.wait_for(host.setLocalDescription(await host.createAnswer()), 10)
            await sender.setRemoteDescription(host.localDescription)
            await asyncio.wait_for(opened.wait(), 10)
            text = '한' * 85_000
            channel.send(offer(text))
            await asyncio.wait_for(accepted.wait(), 10)
            stream_channel = text.encode('utf-8')
            for offset in range(0, len(stream_channel), CHUNK_BYTES):
                channel.send(stream_channel[offset:offset+CHUNK_BYTES])
            channel.send(message('clipboard.text-complete', {'transferId': TRANSFER}, 2))
            await asyncio.wait_for(applied.wait(), 10)
            self.assertEqual(writes, [text])
        finally:
            if receiver: receiver.close()
            await asyncio.wait_for(sender.close(), 3)
            await asyncio.wait_for(host.close(), 3)
