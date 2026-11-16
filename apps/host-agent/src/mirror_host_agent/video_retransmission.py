"""Bounded per-sender TURN NACK pump for reviewed aiortc 1.15.

Do not pace inside the DTLS receive/RTCP callback: doing so also stalls input
and subsequent feedback. Keep only sequence numbers from aiortc's own 128-entry
history, and let its native retransmit preserve RTX sequence/SSRC/encryption.
"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import math

from aiortc.rtp import RtcpRtpfbPacket, RTCP_RTPFB_NACK
from aiortc.rtcrtpsender import RTP_HISTORY_SIZE

LOGGER = logging.getLogger('mirror_host_agent')
BURST_BYTES = 4_800


class VideoRetransmission:
    def __init__(self, sender, rate_provider, route_provider, clock, sleep):
        self.sender = sender
        self.rate_provider = rate_provider
        self.route_provider = route_provider
        self.clock = clock
        self.sleep = sleep
        self.original = sender._handle_rtcp_packet
        self._pending = {}  # insertion-ordered, at most the native history size
        self._active = None
        self._task = None
        self._closed = False
        self._credit = BURST_BYTES
        self._at = clock()
        self._burst = deque(maxlen=128)
        self._last_logged = -math.inf
        self.stats = dict(requests=0, sent=0, coalesced=0, stale=0,
                          queue_peak=0, peak_1ms_bytes=0, wait_ms=0.)

        async def handle(packet):
            if (not self._closed and self.route_provider() == 'turn' and
                    isinstance(packet, RtcpRtpfbPacket) and
                    packet.fmt == RTCP_RTPFB_NACK and packet.media_ssrc == sender._ssrc):
                self.enqueue(packet.lost)
                return
            await self.original(packet)
        self.wrapper = handle
        sender._handle_rtcp_packet = handle

    def _packet(self, sequence):
        packet = getattr(self.sender, '_RTCRtpSender__rtp_history', {}).get(sequence % RTP_HISTORY_SIZE)
        return packet if packet is not None and packet.sequence_number == sequence else None

    def enqueue(self, sequences):
        if self._closed:
            return
        for sequence in sequences:
            self.stats['requests'] += 1
            if sequence in self._pending or sequence == self._active:
                self.stats['coalesced'] += 1
            elif self._packet(sequence) is None:
                self.stats['stale'] += 1
            else:
                # Evict only requests whose native history has already gone.
                # This also prevents adversarial NACKs accumulating stale IDs.
                if len(self._pending) >= RTP_HISTORY_SIZE:
                    self._pending = {s: None for s in self._pending if self._packet(s) is not None}
                if len(self._pending) < RTP_HISTORY_SIZE:
                    self._pending[sequence] = None
        self.stats['queue_peak'] = max(self.stats['queue_peak'], len(self._pending))
        if self._pending and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._drain(), name='mirror-video-retransmission')
            self._task.add_done_callback(self._completed)

    async def _drain(self):
        try:
            while self._pending and not self._closed:
                sequence = next(iter(self._pending))
                del self._pending[sequence]
                self._active = sequence
                packet = self._packet(sequence)
                if packet is None:
                    self.stats['stale'] += 1
                    continue
                size = len(packet.payload) + 80  # RTP/extensions/RTX overhead
                if self.route_provider() == 'turn':
                    target, initial = self.rate_provider()
                    rate = max(target * 2, initial)
                    if not math.isfinite(rate) or rate <= 0:
                        rate = 2_500_000
                    now = self.clock()
                    self._credit = min(BURST_BYTES, self._credit + max(0, now-self._at)*rate/8)
                    self._at = now
                    wait = max(0, size-self._credit)*8/rate
                    if wait >= .002:
                        self.stats['wait_ms'] += wait*1000
                        await self.sleep(wait)
                        now = self.clock()
                        self._credit = min(BURST_BYTES, self._credit + max(0, now-self._at)*rate/8)
                        self._at = now
                    self._credit -= size
                if self._closed:
                    return
                if self._packet(sequence) is None:
                    self.stats['stale'] += 1
                    continue
                await self.sender._retransmit(sequence)
                self.stats['sent'] += 1
                now = self.clock()
                while self._burst and now-self._burst[0][0] >= .001:
                    self._burst.popleft()
                self._burst.append((now, size))
                self.stats['peak_1ms_bytes'] = max(self.stats['peak_1ms_bytes'],sum(s for _, s in self._burst))
                if now-self._last_logged >= 5:
                    self._last_logged = now
                    LOGGER.info('Video retransmission: policy=bounded-nack-v1 requests=%s sent=%s '
                                'coalesced=%s stale=%s queue_peak=%s peak_1ms_bytes=%s wait_ms=%.2f',
                                *self.stats.values())
        finally:
            self._active = None

    def _completed(self, task):
        if not task.cancelled() and task.exception() is not None:
            LOGGER.warning('Video retransmission stopped: type=%s', type(task.exception()).__name__)

    def close(self):
        self._closed = True
        if self.sender._handle_rtcp_packet is self.wrapper:
            self.sender._handle_rtcp_packet = self.original
        self._pending.clear()
        self._burst.clear()
        if self._task is not None:
            self._task.cancel()

    async def wait_closed(self):
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
