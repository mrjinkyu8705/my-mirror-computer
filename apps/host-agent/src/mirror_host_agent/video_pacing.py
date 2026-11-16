"""Per-peer video RTP diagnostics and bounded inline TURN burst smoothing.

No primary-media queue/padding/drop/reordering, no data-channel or RTCP delay.
Application-owned instance adapter for reviewed aiortc1.15; never edit packages.
"""
from __future__ import annotations

import asyncio
from collections import deque
from importlib.metadata import version
import logging
import math
import sys
import time

from aiortc.rtp import is_rtcp
from aiortc.rtp import RtpPacket
from aiortc import clock as rtc_clock
from .video_retransmission import VideoRetransmission

LOGGER = logging.getLogger('mirror_host_agent')
BURST_BYTES = 4_800
MAX_FRAME_SPREAD_SECONDS = .080
MIN_SLEEP_SECONDS = .002


async def _precision_sleep(seconds):
    # CPython3.12 Windows asyncio timers coalesce at ~16ms on this host.
    # Native time.sleep uses a high-resolution waitable timer. Await ONE shared
    # executor call, never block the event loop or change global timer settings.
    if sys.platform == 'win32':
        await asyncio.to_thread(time.sleep,seconds)
    else:
        await asyncio.sleep(seconds)


def _idr(data: bytes) -> bool:
    offset = 12+4*(data[0]&15)
    if data[0]&16:
        if offset+4 > len(data):
            return False
        offset += 4+4*int.from_bytes(data[offset+2:offset+4], 'big')
    if offset >= len(data):
        return False
    kind = data[offset]&31
    if kind == 5:
        return True
    if kind == 28:
        return offset+1 < len(data) and data[offset+1]&31 == 5
    if kind == 24:
        offset += 1
        while offset+2 < len(data):
            size = int.from_bytes(data[offset:offset+2], 'big'); offset += 2
            if size < 1 or offset+size > len(data):
                return False
            if data[offset]&31 == 5:
                return True
            offset += size
    return False


def selected_route(sender) -> str:
    """ICE pair, not an arbitrary successful candidate or UDP UI label."""
    try:
        pairs = tuple(sender.transport.transport._connection._nominated.values())
        if pairs:
            return 'turn' if any(p.local_candidate.type == 'relay' or p.remote_candidate.type == 'relay'
                                 for p in pairs) else 'direct'
    except (AttributeError, TypeError):
        pass
    return 'unknown'


class VideoPacing:
    def __init__(self, transport, ssrc: int, rate_provider, route_provider,
                 *, pace=False, clock=time.perf_counter, sleep=_precision_sleep):
        self.transport = transport
        self.ssrc = ssrc
        self.rate_provider = rate_provider
        self.route_provider = route_provider
        self.enabled = pace
        self.clock = clock
        self.sleep = sleep
        self.original = transport._send_rtp
        self._closed = False
        self._timestamp = None
        self._last_primary_sequence = None
        self._prepared_bytes = 0
        self._restore_encoded = None
        self.restamp = None
        self.rtx_ssrc = None
        self.retransmission = None
        self._last_logged = -math.inf
        self.last_summary = None
        self.history = deque(maxlen=64)
        self.stats = dict(frames=0,keyframes=0,bytes=0,max_span_ms=0.,peak_1ms_bytes=0,
                          max_gap_ms=0.,scheduled_wait_ms=0.,deadline_bytes=0,restamped_packets=0)
        async def wrapped(data):
            await self.send(data)
        self.wrapper = wrapped
        transport._send_rtp = wrapped

    def _begin(self, stamp, now):
        self._timestamp = stamp
        self._start = now
        self._bytes = 0
        self._packets = 0
        self._keyframe = False
        self._wait = 0.
        self._max_gap = 0.
        self._last_send = None
        self._burst = deque(maxlen=512)
        self._burst_bytes = 0
        self._peak = 0
        self._flush = 0
        self._finished = False
        self._route = self.route_provider()
        target, initial = self.rate_provider()
        self._rate = max(target*2, initial)
        if not math.isfinite(self._rate) or self._rate <= 0:
            self._rate = 2_500_000
        # The encoded frame is already available inside the RTP sender. Know
        # its size before the first packet, so a big frame is spread over the
        # whole bounded interval, not a large tail flush at the deadline.
        if self._prepared_bytes:
            self._rate = max(self._rate,max(0,self._prepared_bytes-BURST_BYTES)*8/MAX_FRAME_SPREAD_SECONDS)
        self._prepared_bytes = 0

    def prepare_frame(self, payload_sizes):
        self._prepared_bytes = sum(size+64 for size in payload_sizes)

    async def send(self, data: bytes):
        # RTX is paced by the independent NACK pump, never by current-frame
        # accounting. Control/clipboard use _send_data, untouched here.
        if (self._closed or len(data) < 12 or data[0]>>6 != 2 or is_rtcp(data)
                or int.from_bytes(data[8:12], 'big') != self.ssrc):
            if (not self._closed and len(data) >= 12 and data[0]>>6 == 2 and not is_rtcp(data)
                    and int.from_bytes(data[8:12], 'big') == self.rtx_ssrc):
                data = self._restamp(data)
            await self.original(data)
            return
        stamp = int.from_bytes(data[4:8], 'big')
        sequence = int.from_bytes(data[2:4], 'big')
        if self._last_primary_sequence is not None:
            distance = (sequence-self._last_primary_sequence)&65535
            if distance == 0 or distance >= 32768:
                # Without negotiated RTX, retransmits reuse the primary SSRC.
                # They must bypass both pacing and current-frame accounting.
                await self.original(self._restamp(data))
                return
        self._last_primary_sequence = sequence
        now = self.clock()
        if stamp != self._timestamp:
            if self._timestamp is not None and not self._finished:
                self._finish(now)
            self._begin(stamp, now)
        self._bytes += len(data)
        self._keyframe |= _idr(data)
        if self.enabled and self._route == 'turn':
            due = self._start+min(MAX_FRAME_SPREAD_SECONDS,
                                 max(0,self._bytes-BURST_BYTES)*8/self._rate)
            wait = due-now
            if wait >= MIN_SLEEP_SECONDS:
                self._wait += wait
                await self.sleep(wait)
            now = self.clock()
            # Completed frames take precedence over dropping an already-sent
            # reference. Scheduling is capped; OS timer overshoot is measured.
            if max(0,self._bytes-BURST_BYTES)*8/self._rate > MAX_FRAME_SPREAD_SECONDS:
                self._flush += len(data)
        if self._last_send is not None:
            self._max_gap = max(self._max_gap, now-self._last_send)
        self._last_send = now
        while self._burst and now-self._burst[0][0] >= .001:
            self._burst_bytes -= self._burst.popleft()[1]
        if len(self._burst) == 512:
            self._burst_bytes -= self._burst.popleft()[1]
        self._burst.append((now,len(data)))
        self._burst_bytes += len(data)
        self._peak = max(self._peak,self._burst_bytes)
        self._packets += 1
        data = self._restamp(data)
        await self.original(data)
        if data[1]&128:
            self._finish(self.clock())

    def _restamp(self, data):
        if self.enabled and self.route_provider() == 'turn' and self.restamp is not None:
            stamped = self.restamp(data)
            if stamped is not data:
                self.stats['restamped_packets'] += 1
            data = stamped
        return data

    def _finish(self, now):
        self._finished = True
        summary = dict(paced=self.enabled and self._route == 'turn',route=self._route,
            keyframe=self._keyframe,bytes=self._bytes,packets=self._packets,
            span_ms=round((now-self._start)*1000,2),max_gap_ms=round(self._max_gap*1000,2),
            peak_1ms_bytes=self._peak,scheduled_wait_ms=round(self._wait*1000,2),
            deadline_bytes=self._flush,pacing_bps=round(self._rate))
        self.last_summary = summary
        self.history.append(summary)
        self.stats['frames'] += 1;self.stats['keyframes'] += int(self._keyframe);self.stats['bytes'] += self._bytes
        self.stats['max_span_ms'] = max(self.stats['max_span_ms'],summary['span_ms'])
        self.stats['peak_1ms_bytes'] = max(self.stats['peak_1ms_bytes'],summary['peak_1ms_bytes'])
        self.stats['max_gap_ms'] = max(self.stats['max_gap_ms'],summary['max_gap_ms'])
        self.stats['scheduled_wait_ms'] += summary['scheduled_wait_ms']
        self.stats['deadline_bytes'] += summary['deadline_bytes']
        if now-self._last_logged >= (.5 if self._keyframe else 5):
            self._last_logged = now
            LOGGER.info('Video RTP burst: paced=%s route=%s keyframe=%s bytes=%s packets=%s '
                        'span_ms=%s max_gap_ms=%s peak_1ms_bytes=%s scheduled_wait_ms=%s '
                        'deadline_bytes=%s pacing_bps=%s',*summary.values())

    def close(self):
        self._closed = True
        if self.retransmission is not None:
            self.retransmission.close()
        if self.transport._send_rtp is self.wrapper:
            self.transport._send_rtp = self.original
        if self._restore_encoded is not None:
            self._restore_encoded()
            self._restore_encoded = None
        self.history.clear()

    async def wait_closed(self):
        if self.retransmission is not None:
            await self.retransmission.wait_closed()


def install_video_pacing(sender, profile_provider, *, pace=False):
    if version('aiortc') != '1.15.0':
        raise RuntimeError('Video pacing requires reviewed aiortc1.15.0')
    def rate():
        encoder = getattr(sender,'_RTCRtpSender__encoder',None)
        initial = {'low':1_500_000,'balanced':2_500_000,'high':4_000_000}[profile_provider()]
        return getattr(encoder,'target_bitrate',initial),initial
    result = VideoPacing(sender.transport,sender._ssrc,rate,lambda:selected_route(sender),pace=pace)
    def restamp(data):
        mapping = getattr(sender,'_RTCRtpSender__rtp_header_extensions_map',None)
        if mapping is None:
            return data
        packet = RtpPacket.parse(data,mapping)
        if packet.extensions.abs_send_time is None:
            return data
        # aiortc serializes AST before _send_rtp. Refresh ONLY this negotiated
        # header after the pacing wait, before SRTP authentication/encryption.
        # Otherwise our own scheduled delay looks like network congestion.
        packet.extensions.abs_send_time = (rtc_clock.current_ntp_time()>>14)&0xffffff
        return packet.serialize(mapping)
    result.restamp = restamp
    result.rtx_ssrc = getattr(sender, '_rtx_ssrc', None)
    if pace and callable(getattr(sender, '_handle_rtcp_packet', None)):
        result.retransmission = VideoRetransmission(sender, rate, lambda:selected_route(sender),
                                                   lambda:result.clock(), lambda s:result.sleep(s))
    original_next = sender._next_encoded_frame
    async def next_frame(codec):
        frame = await original_next(codec)
        if frame is not None:
            result.prepare_frame(len(p) for p in frame.payloads)
        return frame
    sender._next_encoded_frame = next_frame
    def restore():
        if sender._next_encoded_frame is next_frame:
            sender._next_encoded_frame = original_next
    result._restore_encoded = restore
    LOGGER.info('Video RTP policy: %s; video-only TURN; bounded-nack-v1; data/RTCP unchanged; spread_ms=80 burst_bytes=4800',
                'bounded-pacing-v9' if pace else 'burst-diagnostics-v9')
    return result
