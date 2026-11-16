import asyncio
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mirror_host_agent.__main__ import AgentConfig, M0Agent
from mirror_host_agent.file_runtime import FileFlow, FileReceivePump, disk_call, FILE_WINDOW_BYTES
from mirror_host_agent.file_transfer import FileReceiver

FIXTURES = Path(__file__).resolve().parents[3] / 'local-artifacts/verification/bulk-responsiveness-20261110/unit-fixtures'


class FileRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=FIXTURES)
        self.folder = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def make_pump(self, payload, receiver=None):
        events = []
        accepted = asyncio.Event()
        def ready():
            events.append(('accept',)); accepted.set()
        pump = FileReceivePump(receiver or FileReceiver(self.folder), 'fixture.bin', len(payload),
            hashlib.sha256(payload).hexdigest(), ready, lambda n: events.append(('progress', n)),
            lambda r: events.append(('done', r.size)), lambda code: events.append(('error', code)))
        await asyncio.wait_for(accepted.wait(), 2)
        return pump, events

    async def test_ordered_disk_bytes_hash_and_receipts_before_done(self):
        payload = b'file-runtime-fixture' * 700
        pump, events = await self.make_pump(payload)
        self.assertTrue(pump.push(payload[:8192])); self.assertTrue(pump.push(payload[8192:]))
        self.assertTrue(pump.complete())
        await pump.task
        self.assertEqual((self.folder/'fixture.bin').read_bytes(), payload)
        self.assertEqual(events, [('accept',), ('progress', 8192), ('progress', len(payload)), ('done', len(payload))])
        self.assertFalse(any(self.folder.glob('*.part')))

    async def test_slow_disk_does_not_stall_event_loop(self):
        class SlowReceiver(FileReceiver):
            def write_chunk(self, chunk):
                time.sleep(.15)
                super().write_chunk(chunk)
        pump, events = await self.make_pump(b'fixture', SlowReceiver(self.folder))
        pump.push(b'fixture'); pump.complete()
        ticks = 0
        while not pump.task.done():
            await asyncio.sleep(.01); ticks += 1
        await pump.task
        self.assertGreaterEqual(ticks, 8)
        self.assertEqual(events[-1], ('done', 7))
        self.assertGreaterEqual(pump.max_io_ms, 140)

    async def test_queue_is_bounded_even_for_empty_chunks(self):
        pump, events = await self.make_pump(b'')
        for _ in range(16): self.assertTrue(pump.push(b''))
        self.assertFalse(pump.push(b''))
        pump.close(); await pump.task
        self.assertFalse((self.folder/'fixture.bin').exists())
        self.assertFalse(any(self.folder.glob('*.part')))

    async def test_cancel_waits_for_disk_write_then_cleans_without_done(self):
        entered, release = threading.Event(), threading.Event()
        class SlowReceiver(FileReceiver):
            def write_chunk(self, chunk):
                entered.set(); release.wait(2); super().write_chunk(chunk)
        pump, events = await self.make_pump(b'ab', SlowReceiver(self.folder))
        pump.push(b'a')
        while not entered.is_set(): await asyncio.sleep(.001)
        pump.close(); release.set(); await pump.task
        self.assertEqual(events, [('accept',)])
        self.assertFalse((self.folder/'fixture.bin').exists())
        self.assertFalse(any(self.folder.glob('*.part')))

    async def test_digest_error_keeps_prior_file_and_removes_part(self):
        (self.folder/'fixture.bin').write_bytes(b'prior')
        pump, events = await self.make_pump(b'ab')
        pump.push(b'xx'); pump.complete(); await pump.task
        self.assertEqual(events[-1], ('error', 'DIGEST_MISMATCH'))
        self.assertEqual((self.folder/'fixture.bin').read_bytes(), b'prior')
        self.assertFalse(any(self.folder.glob('*.part')))

    async def test_unique_parts_allow_cancelled_worker_and_same_name_retry(self):
        first, second = FileReceiver(self.folder), FileReceiver(self.folder)
        digest = hashlib.sha256(b'a').hexdigest()
        first.begin('same.bin', 1, digest); second.begin('same.bin', 1, digest)
        self.assertNotEqual(first._temp_path, second._temp_path)
        first.abort(); second.write_chunk(b'a'); result = second.finish()
        self.assertEqual(result.path.read_bytes(), b'a')

    async def test_disk_cancel_preserves_thread_ownership(self):
        entered, release = threading.Event(), threading.Event()
        def operation(): entered.set(); release.wait(2)
        task = asyncio.create_task(disk_call(operation))
        while not entered.is_set(): await asyncio.sleep(.001)
        task.cancel(); await asyncio.sleep(.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task

    async def test_flow_rejects_invalid_credits_and_bounds_sctp_handoff(self):
        flow = FileFlow(True); flow.mark_sent(FILE_WINDOW_BYTES)
        task = asyncio.create_task(flow.wait(4096, lambda: True))
        await asyncio.sleep(.06); self.assertFalse(task.done())
        for value in (True, -1, 1.5, FILE_WINDOW_BYTES+1): flow.acknowledge(value)
        self.assertEqual(flow.received, 0)
        flow.acknowledge(FILE_WINDOW_BYTES); self.assertTrue(await task)
        flow.acknowledge(0); self.assertEqual(flow.received, FILE_WINDOW_BYTES)
        self.assertEqual(flow.peak, FILE_WINDOW_BYTES)

    async def test_agent_negotiates_credit_only_with_new_viewer_and_writes_off_loop(self):
        for credit in (False, True):
            agent = M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
                ticket='unused',ws_url='ws://127.0.0.1:8787/ws',heartbeat_stop_after_seconds=None,
                files_enabled=True,files_dir=str(self.folder)))
            class Channel:
                readyState = 'open'
                sent = []
                def send(self, raw): self.sent.append(json.loads(raw))
            channel = Channel(); channel.sent = []; agent._file_channel = channel
            data = dict(name=f'agent-{credit}.bin',size=4,sha256=hashlib.sha256(b'data').hexdigest(),transferId='transfer_0123456789abcdef')
            if credit: data['flowControl'] = 'credit-v1'
            agent._on_file_offer(data)
            pump = agent._file_receive_pump
            while not channel.sent: await asyncio.sleep(.001)
            self.assertEqual(channel.sent[0]['data'].get('flowControl'), 'credit-v1' if credit else None)
            agent._on_file_chunk(b'data'); agent._on_file_complete({'transferId': data['transferId']})
            await pump.task
            self.assertEqual([m['event'] for m in channel.sent], ['file.accept','file.progress','file.done'] if credit else ['file.accept','file.done'])
            self.assertIsNone(agent._file_receive_pump)

    async def test_stale_session_worker_cannot_ack_or_finish_new_connection(self):
        agent = M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
            ticket='unused',ws_url='ws://127.0.0.1:8787/ws',heartbeat_stop_after_seconds=None,
            files_enabled=True,files_dir=str(self.folder)))
        with patch.object(agent, '_send_file') as send:
            agent._on_file_offer(dict(name='stale.bin',size=1,sha256=hashlib.sha256(b'a').hexdigest(),transferId='transfer_0123456789abcdef'))
            pump = agent._file_receive_pump
            agent._reset_file_transfer()
            await pump.task
            send.assert_not_called()

    async def test_zero_byte_transfer_and_invalid_offer_do_not_leave_parts(self):
        for name, digest, expected in [('empty.bin', hashlib.sha256(b'').hexdigest(), 'done'),
                                       ('../outside.bin', '0'*64, 'error')]:
            events = []
            ready = asyncio.Event()
            pump = FileReceivePump(FileReceiver(self.folder), name, 0, digest,
                ready.set, lambda n: None, lambda r: events.append('done'), lambda code: events.append('error'))
            while not pump.task.done() and not ready.is_set(): await asyncio.sleep(.001)
            if ready.is_set(): pump.complete()
            await pump.task
            self.assertEqual(events, [expected])
            self.assertFalse(any(self.folder.glob('*.part')))

    async def test_disk_failure_aborts_without_false_receipt_or_completion(self):
        class FailedReceiver(FileReceiver):
            def write_chunk(self, chunk): raise OSError('private path must not be logged')
        pump, events = await self.make_pump(b'a', FailedReceiver(self.folder))
        pump.push(b'a'); pump.complete(); await pump.task
        self.assertEqual(events, [('accept',), ('error', 'WRITE_FAILED')])
        self.assertFalse((self.folder/'fixture.bin').exists())
        self.assertFalse(any(self.folder.glob('*.part')))

    async def test_cancel_is_transfer_scoped_and_forged_credit_is_ignored(self):
        agent = M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
            ticket='unused',ws_url='ws://127.0.0.1:8787/ws',heartbeat_stop_after_seconds=None))
        agent._file_transfer_id = 'transfer_upload_0123456789'
        agent._download_transfer_id = 'transfer_download_0123456789'
        agent._download_flow = FileFlow(True); agent._download_flow.mark_sent(100)
        def message(event, data):
            return json.dumps(dict(event=event,data=data,sequence=1,sessionId=agent._session_id,
                                   timestamp=int(time.time()*1000),version=1))
        agent._on_file_message(message('file.progress', dict(transferId='wrong', received=100)))
        agent._on_file_message(message('file.progress', dict(transferId=agent._download_transfer_id, received=101)))
        self.assertEqual(agent._download_flow.received, 0)
        agent._on_file_message(message('file.cancel', dict(transferId='transfer_unknown_0123456789')))
        self.assertIsNotNone(agent._download_transfer_id)
        agent._on_file_message(message('file.cancel', dict(transferId=agent._file_transfer_id)))
        self.assertIsNone(agent._file_transfer_id)
        self.assertIsNotNone(agent._download_transfer_id)

    async def test_late_old_file_channel_cannot_cancel_or_write_to_new_transfer(self):
        agent = M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
            ticket='unused',ws_url='ws://127.0.0.1:8787/ws',heartbeat_stop_after_seconds=None))
        class Channel:
            def __init__(self): self.handlers = {}
            def on(self, event):
                def attach(callback): self.handlers[event] = callback; return callback
                return attach
        old, new = Channel(), Channel()
        agent._attach_file_channel(old)
        agent._attach_file_channel(new)
        with patch.object(agent, '_on_file_message') as message, patch.object(agent, '_reset_file_transfer') as reset:
            old.handlers['message'](b'stale')
            old.handlers['close']()
            message.assert_not_called(); reset.assert_not_called()
            self.assertIs(agent._file_channel, new)
            new.handlers['message'](b'current')
            message.assert_called_once_with(b'current')

