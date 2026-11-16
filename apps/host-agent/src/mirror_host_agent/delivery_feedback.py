"""Count-weighted delivery evidence. No network/capture/config side effects."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math

WINDOW_SECONDS = 6.0
MIN_PACKETS = 120
MIN_WINDOW_SECONDS = 2.0


@dataclass(frozen=True)
class DeliverySample:
    interval_ms: float
    received_packets: int | None
    lost_packets: int | None
    buffer_ms: float | None
    decode_ms: float | None
    decoded_frames: int | None
    dropped_frames: int | None
    recovered_packets: int | None = None
    received_bytes: int | None = None
    control_rtt_ms: float | None = None

    @classmethod
    def parse(cls, raw):
        keys = {'intervalMs', 'receivedPackets', 'lostPackets', 'bufferMs', 'decodeMs', 'decodedFrames', 'droppedFrames'}
        optional = {'recoveredPackets', 'receivedBytes', 'controlRttMs'}
        if not isinstance(raw, dict) or not keys <= set(raw) or set(raw)-keys-optional:
            raise ValueError('INVALID_DELIVERY')
        def number(name, maximum, *, integer=False, nullable=True):
            value = raw.get(name)
            if nullable and value is None:
                return None
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= maximum
                    or (integer and type(value) is not int)):
                raise ValueError('INVALID_DELIVERY')
            return value
        interval = number('intervalMs', 3_000, nullable=False)
        if interval < 250:
            raise ValueError('INVALID_DELIVERY')
        result = cls(interval, number('receivedPackets', 1_000_000, integer=True),
            number('lostPackets', 1_000_000, integer=True), number('bufferMs',60_000),
            number('decodeMs',60_000), number('decodedFrames',10_000,integer=True),
            number('droppedFrames',10_000,integer=True),
            number('recoveredPackets',1_000_000,integer=True),
            number('receivedBytes',67_108_864,integer=True), number('controlRttMs',60_000))
        if result.recovered_packets:
            if (result.lost_packets != 0 or result.received_packets is None
                    or result.recovered_packets > result.received_packets
                    or result.received_bytes is None):
                raise ValueError('INVALID_DELIVERY')
        return result


@dataclass(frozen=True)
class DeliveryEvidence:
    state: str
    packets: int
    lost: int
    loss_percent: float | None
    sample_count: int
    buffer_ms: float | None
    decode_ms: float | None
    counts_valid: bool = False
    recovered: int = 0
    delay_pressure: bool = False


class DeliveryWindow:
    def __init__(self):
        self.samples = deque(maxlen=24)
        self.route = None

    def clear(self):
        self.samples.clear()
        self.route = None

    def add(self, at: float, route: str, sample: DeliverySample):
        if self.samples:
            gap = at - self.samples[-1][0]
            if gap < 0 or gap > 3.0 or route != self.route:
                self.clear()
            elif gap < sample.interval_ms / 2_000:
                # A burst/duplicate cannot manufacture seconds of evidence.
                return False
        self.route = route
        self.samples.append((at, sample))
        return True

    def evidence(self, now: float) -> DeliveryEvidence:
        rows = [(at,s) for at,s in tuple(self.samples) if 0 <= now-at < WINDOW_SECONDS]
        if not rows or now-rows[-1][0] > 2.5:
            return DeliveryEvidence('stale',0,0,None,0,None,None)
        last = rows[-1][1]
        valid = [(at,s) for at,s in rows if s.received_packets is not None and s.lost_packets is not None]
        # Corrections cancel only earlier loss observed IN this window. Excess
        # credit is discarded, never used to hide future loss or another route.
        debts = []
        recovered = 0
        for _, item in valid:
            credit = item.recovered_packets or 0
            for i, debt in enumerate(debts):
                paid = min(credit, debt)
                debts[i] -= paid; credit -= paid; recovered += paid
                if credit == 0:
                    break
            debts.append(item.lost_packets)
        lost = sum(debts)
        packets = sum(s.received_packets for _,s in valid)+lost
        loss = 100*lost/packets if packets else None
        # Missing/corrected counters are never treated as a healthy zero.
        current_valid = last.received_packets is not None and last.lost_packets is not None
        covered = sum(s.interval_ms for _,s in valid)/1_000
        spanned = valid[-1][0]-valid[0][0] if valid else 0
        sufficient = packets >= MIN_PACKETS and covered >= MIN_WINDOW_SECONDS and spanned >= 1
        decoder_ok = last.decode_ms is not None and last.decode_ms <= 40
        severe_loss = bool(current_valid and last.received_packets+last.lost_packets >= MIN_PACKETS
                           and last.lost_packets*100 >= 15*(last.received_packets+last.lost_packets))
        catastrophic_loss = bool(current_valid and last.received_packets+last.lost_packets >= MIN_PACKETS
                                 and last.lost_packets*2 >= last.received_packets+last.lost_packets)
        # One bad interval is a burst, not permission to collapse to the REMB
        # floor. Emergency loss requires consecutive timed observations. Use
        # debt after retransmission corrections; never bridge unknown rows.
        remaining_loss = {at: debt for (at, _), debt in zip(valid, debts)}
        loss_run = []
        for at, item in reversed(rows):
            debt = remaining_loss.get(at)
            if debt is None or item.received_packets is None:
                break
            total = item.received_packets + debt
            if not total or debt*100 < 10*total:
                break
            loss_run.append((at,item,debt))
        sustained_loss = (len(loss_run) >= 3 and loss_run[0][0]-loss_run[-1][0] >= 2
                          and sum(s.interval_ms for _,s,_ in loss_run) >= 2_000
                          and sum(s.received_packets+debt for _,s,debt in loss_run) >= MIN_PACKETS)
        # Jitter-buffer residence is NOT a measurement of the network queue.
        # Require consecutive, timed high-delay observations, rather than
        # collapsing the rate on a single scroll/assembly/playout spike.
        high_delay = []
        for at, item in reversed(rows):
            if (item.received_packets is None or item.lost_packets is None
                    or item.buffer_ms is None or item.buffer_ms < 120
                    or item.decode_ms is None or item.decode_ms > 40):
                break
            high_delay.append((at, item))
        high_delay.reverse()
        # Use the most recent >=2s trend. An old rise followed by a plateau
        # must not authorize repeated decreases for the entire six-second window.
        while len(high_delay) > 3 and high_delay[-1][0]-high_delay[1][0] >= 2:
            high_delay.pop(0)
        sustained = (sufficient and current_valid and len(high_delay) >= 3
                     and high_delay[-1][0]-high_delay[0][0] >= 2)
        # Delay-only pressure normally uses the existing stepped reduction.
        # Keep an emergency path for a sustained >=1s playout backlog, as well
        # as well as catastrophic/sustained packet loss and transport jitter.
        backlog = sustained and all(s.buffer_ms >= 1_000 for _, s in high_delay)
        rising = (sustained and decoder_ok
                  and last.buffer_ms-high_delay[0][1].buffer_ms >= 60
                  and last.buffer_ms >= high_delay[-2][1].buffer_ms)
        recent_delays = [s.buffer_ms for _,s in rows if s.buffer_ms is not None]
        stable_delay = (len(recent_delays) >= 3
                        and max(recent_delays)-min(recent_delays) <= 60)
        if catastrophic_loss or sustained_loss or backlog:
            state = 'severe'
        elif severe_loss:
            state = 'burst-loss'
        elif last.decode_ms is not None and last.decode_ms > 40 and (loss is None or loss < 2):
            state = 'receiver-limited'
        elif sufficient and (loss >= 2 or rising):
            state = 'moderate'
        elif (sufficient and current_valid and loss <= .5
              and (last.buffer_ms is None or last.buffer_ms < 120
                   or (last.buffer_ms < 250 and stable_delay))):
            state = 'healthy'
        else:
            state = 'insufficient'
        return DeliveryEvidence(state,packets,lost,loss,len(rows),last.buffer_ms,last.decode_ms,current_valid,recovered,bool(rising))
