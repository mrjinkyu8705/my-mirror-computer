import asyncio
from collections import deque
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aiortc.rtcsctptransport import DataChunk
from mirror_host_agent.control_trace import ControlTrace, INPUT_TRACE
from mirror_host_agent.input_control import FakeInputSink, InputController
from mirror_host_agent.interactive_runtime import InputDispatcher, QueuedInputSink


class Transport:
    def __init__(self):
        self._data_channel_queue=deque();self._outbound_queue=deque();self._sent_queue=deque()
        self._flight_size=0;self._cwnd=3600;self._local_tsn=0xffff_ffff
        self.sends=[];self.chunks=[];self.acks=[];self.timeouts=0;self._inbound_streams={}
    async def _send(self,*args,**kwargs):self.sends.append((args,kwargs));return 21
    async def _send_chunk(self,chunk):self.chunks.append(chunk);return 22
    async def _receive_sack_chunk(self,chunk):self.acks.append(chunk);return 23
    def _t3_expired(self):self.timeouts+=1;return 24
    async def _receive_data_chunk(self,chunk):return 25
    async def _receive(self,*args):return 26


class ControlTraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.at=10.
        self.sctp=Transport()
        self.channel=SimpleNamespace(id=2,transport=self.sctp,ordered=True)
        self.trace=ControlTrace(self.channel,clock=lambda:self.at)
        self.trace.install()
    async def asyncTearDown(self):
        self.trace.close()
        await asyncio.sleep(0)
    def payload(self,seq=1):
        return json.dumps(dict(event='session.pong',sequence=seq,data={},sessionId='PRIVATE',timestamp=123)).encode()
    def chunk(self,tsn=0xffff_ffff,count=1):
        chunk=DataChunk();chunk.tsn=tsn;chunk._sent_count=count;return chunk
    def sack(self,tsn=0xffff_ffff,gaps=()):return SimpleNamespace(cumulative_tsn=tsn,gaps=gaps)
    async def admit(self,seq=1):
        self.trace.queue_pong(seq,self.at)
        self.at+=.001;self.trace.handled(seq,self.at-.001)
        self.at+=.1
        await self.sctp._send(2,51,self.payload(seq),ordered=True)

    async def test_callbacks_delegate_once_and_preserve_results(self):
        self.assertEqual(await self.sctp._send(2,51,b'not json',ordered=False),21)
        self.assertEqual(await self.sctp._send_chunk(self.chunk()),22)
        self.assertEqual(await self.sctp._receive_sack_chunk(self.sack()),23)
        self.assertEqual(self.sctp._t3_expired(),24)
        self.assertEqual(tuple(map(len,(self.sctp.sends,self.sctp.chunks,self.sctp.acks))),(1,1,1))
        self.assertEqual(self.sctp.timeouts,1)

    async def test_probe_queue_transmit_ack_are_separate_and_numeric_only(self):
        await self.admit()
        self.at+=.2;await self.sctp._send_chunk(self.chunk())
        self.at+=.3
        with self.assertLogs('mirror_host_agent',level='INFO') as output:
            await self.sctp._receive_sack_chunk(self.sack())
        text=' '.join(output.output)
        self.assertIn('app_queue_ms=101.0',text);self.assertIn('sctp_queue_ms=200.0',text)
        self.assertIn('tx_to_ack_ms=300.0',text);self.assertIn('probe_retx=0',text)
        self.assertNotIn('PRIVATE',text);self.assertNotIn('sessionId',text)
        self.assertFalse(self.trace.pending);self.assertFalse(self.trace.tsns)

    async def test_actual_retransmission_and_timeout_counts_not_guessed_from_rtt(self):
        await self.admit();await self.sctp._send_chunk(self.chunk())
        self.sctp._t3_expired()
        self.at+=1;await self.sctp._send_chunk(self.chunk(count=2))
        self.at+=.2
        with self.assertLogs('mirror_host_agent',level='INFO') as output:
            await self.sctp._receive_sack_chunk(self.sack())
        text=' '.join(output.output)
        self.assertIn('probe_retx=1',text);self.assertIn('assoc_retx_delta=1',text);self.assertIn('t3_delta=1',text)

    async def test_32bit_wrap_and_gap_ack(self):
        self.trace.queue_pong(1,self.at)
        self.trace.admit(2,51,self.payload()+b' '*1200)
        self.assertEqual(self.trace.pending[1]['chunks'],{0xffff_ffff,0})
        self.at+=.1;await self.sctp._send_chunk(self.chunk())
        await self.sctp._send_chunk(self.chunk(0))
        await self.sctp._receive_sack_chunk(self.sack(0xffff_fffe,[(1,2)]))
        self.assertFalse(self.trace.pending)

    async def test_stale_ack_does_not_complete_new_probe(self):
        await self.admit()
        await self.sctp._receive_sack_chunk(self.sack(0xffff_fffd))
        self.assertIn(1,self.trace.pending)

    async def test_duplicate_sequences_and_pending_bound_do_not_leak_tsns(self):
        for _ in range(80):
            await self.admit(1)
        self.assertEqual(len(self.trace.pending),1);self.assertEqual(len(self.trace.tsns),1)
        for seq in range(2,100):self.trace.queue_pong(seq,self.at)
        self.assertLessEqual(len(self.trace.pending),64)

    async def test_timeout_is_unknown_not_fake_ack(self):
        await self.admit();self.at+=6
        with self.assertLogs('mirror_host_agent',level='INFO') as output:self.trace.expire()
        self.assertIn('unconfirmed-5s',' '.join(output.output));self.assertIn('tx_to_ack_ms=None',' '.join(output.output))
        self.assertFalse(self.trace.pending);self.assertFalse(self.trace.tsns)

    async def test_diagnostic_exception_cannot_drop_or_change_send(self):
        with patch.object(self.trace,'admit',side_effect=RuntimeError('sensitive')):
            self.assertEqual(await self.sctp._send(2,51,self.payload()),21)
        self.assertEqual(len(self.sctp.sends),1)

    async def test_transport_exception_is_not_swallowed(self):
        self.trace.close()
        async def failure(*args,**kwargs):raise OSError('transport failure')
        self.sctp._send=failure
        self.trace=ControlTrace(self.channel,clock=lambda:self.at);self.trace.install()
        with self.assertRaisesRegex(OSError,'transport failure'):
            await self.sctp._send(2,51,self.payload())

    async def test_close_restores_instance_state_and_cancels_probe_task(self):
        self.trace.close();await asyncio.sleep(0)
        self.assertTrue(self.trace.task.cancelled())
        for name in ('_send','_send_chunk','_receive_sack_chunk','_t3_expired','_receive_data_chunk','_receive'):
            self.assertNotIn(name,self.sctp.__dict__)
        self.trace.close()

    async def test_preserves_preexisting_instance_override(self):
        self.trace.close()
        previous=self.sctp._send;self.sctp._send=previous
        self.trace=ControlTrace(self.channel);self.trace.install();self.trace.close()
        self.assertIs(self.sctp._send,previous)

    async def test_unsupported_version_skips_hooks_but_not_control(self):
        self.trace.close()
        self.trace=ControlTrace(self.channel)
        with patch('mirror_host_agent.control_trace.version',return_value='1.16.0'):
            self.trace.install();first=self.trace.task;self.trace.install()
        self.assertIs(self.trace.task,first);self.assertFalse(self.trace.supported)
        self.assertEqual(await self.sctp._send(2,51,self.payload()),21)

    async def test_input_context_carries_sequence_not_payload_and_resets(self):
        dispatcher=InputDispatcher();raw=FakeInputSink();sink=QueuedInputSink(raw,dispatcher);sink.start()
        controller=InputController(sink,frame_width=1600,frame_height=900)
        message=dict(event='key.down',data={'code':'KeyA'},sequence=7)
        try:
            with patch.object(self.trace,'input_done') as measured:
                self.trace.process_input(controller,message,self.at)
                self.assertTrue(await asyncio.to_thread(dispatcher.idle.wait,2))
                measured.assert_called_once()
                self.assertEqual(measured.call_args.args[0],7)
                self.assertNotIn('KeyA',str(measured.call_args))
            self.assertIsNone(INPUT_TRACE.get());self.assertEqual(raw.calls,[('key','KeyA','down')])
        finally:
            dispatcher.shutdown();await asyncio.to_thread(dispatcher.thread.join,2)

    async def test_controller_exception_propagates_and_context_resets(self):
        controller=Mock();controller.handle.side_effect=ValueError('control')
        with self.assertRaisesRegex(ValueError,'control'):
            self.trace.process_input(controller,{'sequence':1},self.at)
        self.assertIsNone(INPUT_TRACE.get());controller.handle.assert_called_once()

    async def test_incoming_reassembly_and_duplicates_correlate_without_payload(self):
        chunk=self.chunk();chunk.stream_id=2;chunk.stream_seq=65535;chunk.protocol=51;chunk.flags=3
        self.assertEqual(await self.sctp._receive_data_chunk(chunk),25)
        await self.sctp._receive_data_chunk(chunk)
        self.at+=.4
        self.sctp._inbound_streams[2]=SimpleNamespace(sequence_number=0)
        self.assertEqual(await self.sctp._receive(2,51,self.payload(8)),26)
        self.trace.queue_pong(8,self.at)
        self.assertAlmostEqual(self.trace.pending[8]['rx_wait_ms'],400)
        self.assertEqual(self.trace.pending[8]['rx_duplicates'],1)
        self.assertNotIn('PRIVATE',repr(self.trace.incoming)+repr(self.trace.rx_recent)+repr(self.trace.pending))

    async def test_incoming_maps_are_bounded_expire_and_do_not_guess_missing_fragments(self):
        for i in range(100):
            chunk=self.chunk(i);chunk.stream_id=2;chunk.stream_seq=i;chunk.protocol=51;chunk.flags=3
            await self.sctp._receive_data_chunk(chunk)
        self.assertEqual(len(self.trace.incoming),64)
        self.sctp._inbound_streams[2]=SimpleNamespace(sequence_number=100)
        await self.sctp._receive(2,51,self.payload(8))
        self.assertEqual(self.trace.rx_recent[8][2],0)
        self.at+=6;self.trace.expire()
        self.assertFalse(self.trace.incoming);self.assertFalse(self.trace.rx_recent)

    async def test_unordered_control_does_not_invent_ordered_receive_timing(self):
        self.channel.ordered=False
        self.assertEqual(await self.sctp._receive(2,51,self.payload()),26)
        self.assertFalse(self.trace.rx_recent)
