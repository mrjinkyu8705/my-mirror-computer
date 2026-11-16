import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiortc.rtp import RtpPacket, RtcpRrPacket, HeaderExtensionsMap
from aiortc.rtcrtpparameters import RTCRtpParameters, RTCRtpHeaderExtensionParameters
from unittest.mock import patch
from mirror_host_agent.video_pacing import VideoPacing, selected_route, MAX_FRAME_SPREAD_SECONDS, install_video_pacing


class Clock:
    def __init__(self): self.now=100.; self.waits=[]
    def __call__(self): return self.now
    async def sleep(self,seconds):
        self.waits.append(seconds);self.now+=seconds
        await asyncio.sleep(0)


def packets(count=48,stamp=9000,sequence=100,ssrc=7):
    result=[]
    for i in range(count):
        packet=RtpPacket(payload_type=100,sequence_number=(sequence+i)&65535,timestamp=stamp)
        packet.ssrc=ssrc;packet.marker=int(i==count-1)
        packet.payload=bytes([0x7c,0x85 if i==0 else 0x45])+b'x'*1186
        result.append(packet.serialize())
    return result


class VideoPacingTests(unittest.IsolatedAsyncioTestCase):
    def make(self,*,pace=False,route='turn',rate=(2_500_000,2_500_000),send=None):
        clock=Clock();transport=SimpleNamespace(_send_rtp=send or AsyncMock(),_send_data=AsyncMock())
        adapter=VideoPacing(transport,7,lambda:rate,lambda:route,pace=pace,clock=clock,sleep=clock.sleep)
        return adapter,transport,clock

    async def test_diagnostics_measures_burst_without_changing_timing_or_payload(self):
        adapter,t,clock=self.make()
        data=packets()
        for packet in data: await t._send_rtp(packet)
        self.assertEqual([x.args[0] for x in adapter.original.await_args_list],data)
        self.assertEqual(clock.waits,[])
        self.assertEqual(adapter.last_summary['packets'],48)
        self.assertEqual(adapter.last_summary['peak_1ms_bytes'],sum(map(len,data)))
        self.assertTrue(adapter.last_summary['keyframe'])

    async def test_bounded_pacing_eliminates_loss_in_explicit_small_queue_model(self):
        results=[]
        for paced in (False,True):
            accepted=[];debt=0;last=100.;dropped=0
            async def wire(data):
                nonlocal debt,last,dropped
                debt=max(0,debt-(clock()-last)*10_000_000/8);last=clock()
                if debt+len(data)>8192: dropped+=1
                else: debt+=len(data)
                accepted.append(data)
            adapter,t,clock=self.make(pace=paced,send=wire)
            for packet in packets(): await t._send_rtp(packet)
            results.append(dropped)
            self.assertEqual(accepted,packets())
            self.assertLessEqual(adapter.last_summary['span_ms'],MAX_FRAME_SPREAD_SECONDS*1000+.01)
        self.assertGreater(results[0],0)
        self.assertEqual(results[1],0)

    async def test_direct_route_has_no_added_wait(self):
        adapter,t,clock=self.make(pace=True,route='direct')
        for packet in packets(): await t._send_rtp(packet)
        self.assertEqual(clock.waits,[])
        self.assertFalse(adapter.last_summary['paced'])

    async def test_rtcp_rtx_duplicate_and_primary_retransmit_bypass(self):
        adapter,t,clock=self.make(pace=True)
        for packet in packets(): await t._send_rtp(packet)
        count=len(clock.waits);history=len(adapter.history)
        for packet in [bytes(RtcpRrPacket(ssrc=7,reports=[])),packets(ssrc=9)[0],packets()[0]]:
            await t._send_rtp(packet)
        self.assertEqual(len(clock.waits),count)
        self.assertEqual(len(adapter.history),history)

    async def test_sequence_wrap_and_profile_rate_reevaluated_per_frame(self):
        adapter,t,clock=self.make(pace=True)
        for packet in packets(sequence=65520): await t._send_rtp(packet)
        self.assertEqual(adapter.last_summary['packets'],48)
        adapter.rate_provider=lambda:(500_000,2_500_000)
        for packet in packets(16,stamp=15000,sequence=32): await t._send_rtp(packet)
        self.assertEqual(adapter.last_summary['packets'],16)
        self.assertEqual(adapter.last_summary['pacing_bps'],2_500_000)

    async def test_oversized_frame_is_not_dropped_or_queued_unboundedly(self):
        adapter,t,clock=self.make(pace=True,rate=(500_000,1_500_000))
        for packet in packets(800): await t._send_rtp(packet)
        self.assertEqual(adapter.original.await_count,800)
        self.assertLessEqual(adapter.last_summary['span_ms'],80.01)
        self.assertGreater(adapter.last_summary['deadline_bytes'],0)
        self.assertLessEqual(len(adapter._burst),512)

    async def test_control_data_bypasses_an_inline_video_wait_and_close_restores(self):
        adapter,t,clock=self.make(pace=True)
        entered=asyncio.Event();release=asyncio.Event()
        async def wait(seconds):
            entered.set();await release.wait();clock.now+=seconds
        adapter.sleep=wait
        async def video():
            for packet in packets(): await t._send_rtp(packet)
        task=asyncio.create_task(video());await entered.wait()
        await t._send_data(b'control')
        t._send_data.assert_awaited_once_with(b'control')
        release.set();await task
        original=adapter.original;adapter.close();adapter.close()
        self.assertIs(t._send_rtp,original)
        self.assertEqual(len(adapter.history),0)

    async def test_original_send_failure_propagates_and_cancel_does_not_leave_tasks(self):
        adapter,t,clock=self.make(pace=True,send=AsyncMock(side_effect=ConnectionError()))
        with self.assertRaises(ConnectionError): await t._send_rtp(packets()[0])
        adapter.close()

    async def test_frame_aware_rate_avoids_large_deadline_tail_and_restores_hook(self):
        data=packets(120)
        next_frame=AsyncMock(return_value=SimpleNamespace(payloads=[p[12:] for p in data],timestamp=9000))
        transport=SimpleNamespace(_send_rtp=AsyncMock())
        sender=SimpleNamespace(transport=transport,_ssrc=7,_next_encoded_frame=next_frame)
        adapter=install_video_pacing(sender,lambda:'balanced',pace=True)
        clock=Clock();adapter.clock=clock;adapter.sleep=clock.sleep;adapter.route_provider=lambda:'turn'
        frame=await sender._next_encoded_frame(None)
        self.assertEqual(frame.payloads,[p[12:] for p in data])
        for packet in data: await transport._send_rtp(packet)
        self.assertEqual(adapter.last_summary['deadline_bytes'],0)
        self.assertLessEqual(adapter.last_summary['span_ms'],80.01)
        self.assertGreater(adapter.last_summary['pacing_bps'],5_000_000)
        adapter.close();self.assertIs(sender._next_encoded_frame,next_frame)

    async def test_os_timer_overshoot_is_measured_not_misreported_as_hard_bound(self):
        adapter,t,clock=self.make(pace=True)
        async def oversleep(seconds):clock.now+=seconds+.020
        adapter.sleep=oversleep
        for packet in packets():await t._send_rtp(packet)
        self.assertGreater(adapter.last_summary['max_gap_ms'],20)
        self.assertEqual(adapter.original.await_count,48)

    async def test_abs_send_time_is_refreshed_after_wait_with_payload_and_sequence_preserved(self):
        mapping=HeaderExtensionsMap()
        mapping.configure(RTCRtpParameters(headerExtensions=[RTCRtpHeaderExtensionParameters(id=3,
            uri='http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time')]))
        transport=SimpleNamespace(_send_rtp=AsyncMock())
        sender=SimpleNamespace(transport=transport,_ssrc=7,_next_encoded_frame=AsyncMock(),
                               _RTCRtpSender__rtp_header_extensions_map=mapping)
        adapter=install_video_pacing(sender,lambda:'balanced',pace=True)
        clock=Clock();adapter.clock=clock;adapter.sleep=clock.sleep;adapter.route_provider=lambda:'turn'
        original=RtpPacket.parse(packets(1)[0]);original.extensions.abs_send_time=1
        with patch('mirror_host_agent.video_pacing.rtc_clock.current_ntp_time',return_value=123456789000):
            await transport._send_rtp(original.serialize(mapping))
        sent=RtpPacket.parse(adapter.original.await_args.args[0],mapping)
        self.assertNotEqual(sent.extensions.abs_send_time,1)
        self.assertEqual(sent.payload,original.payload)
        self.assertEqual((sent.ssrc,sent.timestamp,sent.sequence_number),
                         (original.ssrc,original.timestamp,original.sequence_number))
        self.assertEqual(adapter.stats['restamped_packets'],1)
        adapter.close()

    def test_selected_pair_not_any_successful_candidate(self):
        pair=SimpleNamespace(local_candidate=SimpleNamespace(type='relay'),remote_candidate=SimpleNamespace(type='srflx'))
        sender=SimpleNamespace(transport=SimpleNamespace(transport=SimpleNamespace(_connection=SimpleNamespace(_nominated={1:pair}))))
        self.assertEqual(selected_route(sender),'turn')
        pair.local_candidate.type='host';self.assertEqual(selected_route(sender),'direct')
        self.assertEqual(selected_route(SimpleNamespace()),'unknown')
