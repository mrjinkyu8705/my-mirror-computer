import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiortc import RTCRtpSender
from aiortc.rtp import RtpPacket, RtcpRtpfbPacket, RtcpPsfbPacket, RTCP_RTPFB_NACK, RTCP_PSFB_PLI
from aiortc.rtcrtpparameters import RTCRtpParameters, RTCRtpHeaderExtensionParameters
from mirror_host_agent.video_retransmission import VideoRetransmission
from mirror_host_agent.video_pacing import install_video_pacing


class Clock:
    now = 100.
    def __call__(self): return self.now
    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


def nack(sender, sequences):
    return RtcpRtpfbPacket(fmt=RTCP_RTPFB_NACK,ssrc=99,media_ssrc=sender._ssrc,lost=sequences)


class RetransmissionTests(unittest.IsolatedAsyncioTestCase):
    def make(self, *, route='turn', wire=None, rtx=True):
        clock=Clock()
        transport=SimpleNamespace(state='new',_send_rtp=wire or AsyncMock(),_send_data=AsyncMock())
        sender=RTCRtpSender('video',transport)
        sender._RTCRtpSender__rtx_payload_type=101 if rtx else None
        for i in range(128):
            packet=RtpPacket(payload_type=100,sequence_number=i,timestamp=9000,ssrc=sender._ssrc,payload=b'x'*1188)
            sender._RTCRtpSender__rtp_history[i]=packet
        pump=VideoRetransmission(sender,lambda:(2_500_000,2_500_000),lambda:route,clock,clock.sleep)
        self.addAsyncCleanup(self.cleanup,pump)
        return sender,pump,clock

    async def cleanup(self,pump):
        pump.close();await pump.wait_closed()

    async def test_native_nack_burst_reproduced_and_smoothed_in_small_queue_model(self):
        results=[]
        for paced in (False,True):
            debt=0.;last=100.;drops=0;sent=[]
            async def wire(data):
                nonlocal debt,last,drops
                debt=max(0,debt-(clock()-last)*10_000_000/8);last=clock()
                if debt+len(data)>8192: drops+=1
                else: debt+=len(data)
                sent.append(RtpPacket.parse(data))
            sender,pump,clock=self.make(wire=wire)
            message=nack(sender,list(range(100)))
            if paced:
                await sender._handle_rtcp_packet(message)
                await pump._task
            else:
                await pump.original(message)
            self.assertEqual(len(sent),100)
            self.assertEqual([int.from_bytes(p.payload[:2],'big') for p in sent],list(range(100)))
            self.assertTrue(all(p.payload[2:]==b'x'*1188 and p.ssrc==sender._rtx_ssrc for p in sent))
            results.append(drops)
        self.assertGreater(results[0],80)
        self.assertEqual(results[1],0)
        self.assertLess(pump.stats['peak_1ms_bytes'],8192)

    async def test_pending_duplicates_and_invalid_history_are_bounded(self):
        sender,pump,clock=self.make()
        await sender._handle_rtcp_packet(nack(sender,list(range(65536))))
        await sender._handle_rtcp_packet(nack(sender,list(range(128))))
        self.assertLessEqual(len(pump._pending),128)
        await pump._task
        self.assertEqual(pump.stats['sent'],128)
        self.assertEqual(pump.stats['coalesced'],128)

    async def test_control_rtcp_and_primary_rtp_do_not_wait_for_retransmission(self):
        sender,pump,clock=self.make()
        entered=asyncio.Event();release=asyncio.Event()
        async def slow(seconds): entered.set();await release.wait();clock.now+=seconds
        pump.sleep=slow
        await sender._handle_rtcp_packet(nack(sender,list(range(100))))
        await asyncio.wait_for(entered.wait(),1)
        await sender._handle_rtcp_packet(RtcpPsfbPacket(fmt=RTCP_PSFB_PLI,ssrc=99,media_ssrc=sender._ssrc))
        self.assertTrue(sender._RTCRtpSender__force_keyframe)
        await sender.transport._send_data(b'input')
        await sender.transport._send_rtp(b'primary-is-independent')
        self.assertFalse(pump._task.done())
        release.set();await pump._task

    async def test_history_replaced_while_waiting_is_not_sent_as_wrong_sequence(self):
        sender,pump,clock=self.make()
        async def wait(seconds):
            clock.now+=seconds
            sender._RTCRtpSender__rtp_history.clear()
        pump.sleep=wait
        await sender._handle_rtcp_packet(nack(sender,list(range(100))))
        await pump._task
        self.assertGreater(pump.stats['stale'],0)
        self.assertLess(pump.stats['sent'],100)

    async def test_direct_route_uses_native_behavior_without_worker(self):
        sender,pump,clock=self.make(route='direct')
        await sender._handle_rtcp_packet(nack(sender,list(range(20))))
        self.assertIsNone(pump._task)
        self.assertEqual(clock.now,100.)
        self.assertEqual(sender.transport._send_rtp.await_count,20)

    async def test_sequence_wrap_and_non_rtx_negotiation_preserved(self):
        sender,pump,clock=self.make(rtx=False)
        sender._RTCRtpSender__rtp_history.clear()
        for seq in [65535,0,1]:
            sender._RTCRtpSender__rtp_history[seq%128]=RtpPacket(sequence_number=seq,ssrc=sender._ssrc,payload=b'x')
        await sender._handle_rtcp_packet(nack(sender,[65535,0,1]))
        await pump._task
        self.assertEqual([RtpPacket.parse(c.args[0]).sequence_number for c in sender.transport._send_rtp.await_args_list],[65535,0,1])

    async def test_close_cancels_worker_and_restores_only_owned_hook(self):
        sender,pump,clock=self.make()
        entered=asyncio.Event()
        async def wait(seconds):entered.set();await asyncio.Future()
        pump.sleep=wait
        await sender._handle_rtcp_packet(nack(sender,list(range(100))))
        await entered.wait()
        pump.close();pump.close();await pump.wait_closed()
        self.assertTrue(pump._task.cancelled())
        self.assertIs(sender._handle_rtcp_packet,pump.original)
        self.assertFalse(pump._pending)

    async def test_native_retransmission_uses_fresh_ast_not_original_send_time(self):
        sender,pump,clock=self.make()
        pump.close();await pump.wait_closed()
        mapping=sender._RTCRtpSender__rtp_header_extensions_map
        mapping.configure(RTCRtpParameters(headerExtensions=[RTCRtpHeaderExtensionParameters(id=3,
            uri='http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time')]))
        source=sender._RTCRtpSender__rtp_history[0]
        source.extensions.abs_send_time=123
        # Reproduce native copying of the old timestamp on an actual sender.
        await sender._retransmit(0)
        self.assertEqual(RtpPacket.parse(sender.transport._send_rtp.await_args.args[0],mapping).extensions.abs_send_time,123)
        adapter=install_video_pacing(sender,lambda:'balanced',pace=True)
        self.addAsyncCleanup(self.cleanup,adapter)
        adapter.clock=clock;adapter.sleep=clock.sleep
        with patch('mirror_host_agent.video_pacing.selected_route',return_value='turn'), patch(
                'mirror_host_agent.video_pacing.rtc_clock.current_ntp_time',return_value=123456789000):
            await sender._handle_rtcp_packet(nack(sender,[0]))
            await adapter.retransmission._task
        result=RtpPacket.parse(adapter.original.await_args.args[0],mapping)
        self.assertNotEqual(result.extensions.abs_send_time,123)
        self.assertEqual(result.timestamp,source.timestamp)
        self.assertEqual(result.payload,b'\x00\x00'+source.payload)
        self.assertEqual(source.extensions.abs_send_time,123)

    async def test_foreign_media_ssrc_passes_through_native_handler(self):
        sender,pump,clock=self.make()
        message=nack(sender,[0]);message.media_ssrc^=1
        await sender._handle_rtcp_packet(message)
        self.assertIsNone(pump._task)
