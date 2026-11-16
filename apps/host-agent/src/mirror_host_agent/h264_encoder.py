"""Low-latency desktop H.264 encoder tuning for aiortc.

aiortc intentionally uses libx264's default (``medium``) preset.  That is a
good generic default, but real-time desktop video spends substantially more CPU
than necessary on compression efficiency.  Keep aiortc's packetisation and
bitrate feedback intact while making the x264 speed preset configurable.

Desktop video also differs from camera video: maximizing a window can replace
most pixels in a single frame.  Detect those large transitions from a tiny luma
sample and request an immediate recovery frame, rather than leaving the browser
on an incomplete reference picture while it requests a recovery keyframe.
"""

from __future__ import annotations

import fractions
import logging
import math
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import ClassVar

import av
import aiortc.codecs
import numpy as np
from aiortc.codecs import h264 as aiortc_h264
from .delivery_feedback import DeliverySample, DeliveryWindow, DeliveryEvidence

LOGGER = logging.getLogger("mirror_host_agent.h264_encoder")

DEFAULT_H264_PRESET = "veryfast"
SOFTWARE_H264_BACKEND = "libx264"
QSV_H264_BACKEND = "h264_qsv"
AUTO_H264_BACKEND = "auto"
DEFAULT_H264_BACKEND = AUTO_H264_BACKEND
VALID_H264_BACKENDS = frozenset(
    {SOFTWARE_H264_BACKEND, QSV_H264_BACKEND, AUTO_H264_BACKEND}
)
# A large desktop scene change (opening a window, maximize/minimize) can span
# many RTP packets. If one of those packets is lost, an H.264 decoder may have
# no complete reference picture until the next key frame. aiortc honors browser
# PLI/FIR requests, and the scene detector below proactively sends one for large
# desktop transitions. Do not add an unconditional periodic keyframe: with a
# bounded VBV buffer, repeatedly rebuilding a detailed static desktop from an
# IDR frame creates a visible blur/recovery pulse.
# aiortc's generic H.264 ceiling is 3 Mbps. That is marginal for a full-screen
# 1080p desktop transition even at 20 fps. Keep REMB congestion feedback in
# control, but allow a healthy connection to use enough bandwidth for readable
# text after a large transition.
DESKTOP_MAX_BITRATE = 8_000_000
HIGH_INITIAL_BITRATE = 4_000_000
HIGH_MAX_BITRATE = DESKTOP_MAX_BITRATE
BALANCED_INITIAL_BITRATE = 2_500_000
BALANCED_MAX_BITRATE = 5_000_000
LOW_INITIAL_BITRATE = 1_500_000
LOW_MAX_BITRATE = 3_000_000
# Scene detection examines at most roughly 64 x 36 luma samples. Requiring both
# a broad changed area and a meaningful average delta avoids keyframes for
# cursor movement, typing and small animations.
SCENE_SAMPLE_COLUMNS = 64
SCENE_SAMPLE_ROWS = 36
SCENE_PIXEL_DELTA = 24
SCENE_CHANGED_FRACTION = 0.30
SCENE_MEAN_DELTA = 18.0
SCENE_KEYFRAME_COOLDOWN_SECONDS = 0.75
# A window switch is a large change after several quiet frames. Continuous game
# motion is not: forcing an IDR every cooldown interval wastes bandwidth and can
# create visible quality pulses. Arm proactive scene keyframes only after a
# short stable run, while browser PLI/FIR remains available for packet loss.
SCENE_STABLE_FRAMES_REQUIRED = 4
SCENE_STABLE_CHANGED_FRACTION = 0.05
SCENE_STABLE_MEAN_DELTA = 4.0
# A vertical translation needs normal inter prediction, not a new IDR. Only
# classify a scroll when a small row shift explains most of the sampled change.
# Window replacements/random motion do not match this test; PLI/FIR is never
# suppressed. Work is limited to the already-small luma sample.
SCROLL_SAMPLE_MAX_SHIFT = 8
SCROLL_MATCH_ERROR_RATIO = 0.45
# A receiver estimate commonly falls while a desktop is static because there is
# very little residual video to measure. Reopening the codec immediately at that
# lower estimate replaces a crisp reference with a low-bitrate IDR even though
# the network itself may be healthy. Hold a sustained decrease and apply it only
# once content is moving again (or fresh reports confirm real congestion).
# Explicit PLI/FIR recovery remains immediate; capacity recovery is ramped.
BITRATE_DOWNSHIFT_HOLD_SECONDS = 2.0
BITRATE_RECONFIGURE_FRACTION = 0.25
# A fresh viewer report distinguishes actual network congestion from a low REMB
# estimate caused by a static desktop. Require sustained loss or jitter before
# applying a lower estimate. Brief reporting gaps get a bounded grace period;
# viewers that never send reports retain motion-gated compatibility behavior.
RECEIVER_REPORT_MAX_AGE_SECONDS = 5.0
# Once a reporting viewer is known, a short stats interruption is not evidence
# of congestion. Bound that grace period: legacy viewers and a prolonged outage
# must still be able to reduce their sender's bitrate.
RECEIVER_REPORT_TIMEOUT_SECONDS = 8.0
BITRATE_UPSHIFT_INTERVAL_SECONDS = 1.0
BITRATE_UPSHIFT_FRACTION = 0.25
RECEIVER_ESTIMATE_MAX_AGE_SECONDS = 5.0
NETWORK_DEGRADATION_HOLD_SECONDS = 2.0
NETWORK_PACKET_LOSS_THRESHOLD_PERCENT = 2.0
NETWORK_JITTER_THRESHOLD_MS = 50.0
# Alternating good/bad reports must not reset congestion evidence forever.
# Require at least three bad reports spanning 2s and >=half of a fresh 6s
# window. A lone spike, stale feedback and low REMB alone remain insufficient.
NETWORK_RECURRING_WINDOW_SECONDS = 6.0
NETWORK_RECURRING_MIN_BAD_REPORTS = 3
# ``bit_rate`` controls the long-term x264 average but does not bound an
# individual IDR frame. A complex 1080p desktop transition can otherwise emit
# close to a megabyte at once, filling a TURN queue and delaying both RTP video
# and SCTP control heartbeats. A half-second VBV absorbs normal variation while
# bounding that burst without lowering the negotiated steady-state bitrate.
RATE_CONTROL_BUFFER_SECONDS = 0.5
RATE_GUARD_WINDOW_SECONDS = 1.0
# PyAV cannot enlarge an opened VBV maxrate. Grow its software envelope in
# geometric bands, not for every tiny REMB increase. This reserve is NOT a
# higher sender target: ABR and the payload guard still use the current target.
VBV_RECOVERY_GROWTH_FACTOR = 2
# A payload-budget rebuild removes the old VBV reserve. Reopening it on the
# very next REMB increase caused pairs of low-rate IDRs during scroll recovery.
VBV_REBUILD_HOLD_SECONDS = 3.0
# An application-limited desktop can keep REMB pinned to its reduced send rate.
# Allow only a short, small media-rate trial after continuous healthy feedback.
# No padding, forced IDR, quality floor, or unconditional upward ratchet.
RECOVERY_PROBE_HEALTH_SECONDS = 6.0
RECOVERY_PROBE_FEEDBACK_AGE_SECONDS = 2.5
RECOVERY_PROBE_DURATION_SECONDS = 3.0
# Three seconds is the earliest decision, not permission to use a partial
# receiver interval containing an IDR. Allow bounded settling after one local
# VBV rebuild; never extend a trial beyond this absolute six-second deadline.
RECOVERY_PROBE_MAX_DURATION_SECONDS = 6.0
RECOVERY_PROBE_INTERVAL_SECONDS = 10.0
RECOVERY_PROBE_MAX_BACKOFF_SECONDS = 120.0
RECOVERY_PROBE_FRACTION = 0.20
RECOVERY_PROBE_MAX_EXTRA_BITRATE = 200_000
# Verified delivery after a cut can recover even after a long idle desktop.
# A source-limited trial may retry on new motion, but cannot retain an increase
# without the same measured-delivery checks as any other trial.
RECOVERY_PROVEN_RETRY_SECONDS = 2.0
RECOVERY_MOTION_RETRY_SECONDS = 3.0
RECOVERY_MOTION_RETRY_MIN_GROWTH_BPS = 64_000
# A short scroll/window transition can end before the next receiver report.
# Remember it only long enough to bridge feedback/cooldown, not as proof of capacity.
RECOVERY_SHORT_MOTION_SECONDS = 3.0
VALID_H264_PRESETS = frozenset(
    {
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    }
)


@dataclass(frozen=True)
class ReceiverNetworkReport:
    jitter_ms: float | None
    packet_loss_percent: float | None
    received_at: float
    route: str
    unhealthy: bool
    delivery: DeliveryEvidence | None = None
    received_bps: float | None = None
    control_rtt_ms: float | None = None
    decoded_frames: int | None = None
    dropped_frames: int | None = None
    interval_ms: float | None = None
    interval_packets: int | None = None
    interval_loss_percent: float | None = None


@dataclass(frozen=True)
class CapacityProbe:
    started_at: float
    baseline: int
    target: int
    route: str | None = None
    baseline_received_bps: float | None = None
    baseline_buffer_ms: float | None = None
    baseline_control_rtt_ms: float | None = None
    settled_after: float | None = None
    baseline_sent_bps: float | None = None


class TunedH264Encoder(aiortc_h264.H264Encoder):
    """aiortc H.264 encoder with an explicit x264 speed preset."""

    preset = DEFAULT_H264_PRESET
    backend = SOFTWARE_H264_BACKEND
    _receiver_network_report: ClassVar[ReceiverNetworkReport | None] = None
    _receiver_network_unhealthy_since: ClassVar[float | None] = None
    _receiver_network_history: ClassVar[deque[tuple[float, bool]]] = deque(maxlen=32)
    _receiver_probe_history: ClassVar[deque[tuple[float, bool]]] = deque(maxlen=32)
    _delivery_window: ClassVar[DeliveryWindow] = DeliveryWindow()
    _delivery_seen: ClassVar[bool] = False
    _delivery_missing_since: ClassVar[float | None] = None
    _receiver_delivery_history: ClassVar[deque[ReceiverNetworkReport]] = deque(maxlen=16)
    _sender_delivery_history: ClassVar[deque[tuple[float, float]]] = deque(maxlen=16)
    _receiver_last_loss_at: ClassVar[float | None] = None

    @classmethod
    def clear_receiver_network_health(cls) -> None:
        cls._receiver_network_report = None
        cls._receiver_network_unhealthy_since = None
        cls._receiver_network_history.clear()
        cls._receiver_probe_history.clear()
        cls._delivery_window.clear()
        cls._delivery_seen = False
        cls._delivery_missing_since = None
        cls._receiver_delivery_history.clear()
        cls._sender_delivery_history.clear()
        cls._receiver_last_loss_at = None

    @classmethod
    def update_sender_delivery(cls, *, sent_bps: float, now: float) -> None:
        if not math.isfinite(sent_bps) or not 0 <= sent_bps <= 1_000_000_000:
            return
        if cls._sender_delivery_history and now <= cls._sender_delivery_history[-1][0]:
            return
        cls._sender_delivery_history.append((now, sent_bps))

    @classmethod
    def update_receiver_network_health(
        cls,
        *,
        jitter_ms: float | None,
        packet_loss_percent: float | None,
        route: str,
        delivery: DeliverySample | None = None,
        now: float | None = None,
    ) -> None:
        received_at = time.monotonic() if now is None else now
        evidence = None
        if delivery is not None:
            if not cls._delivery_seen:
                LOGGER.info('Receiver delivery telemetry: counts-v1 active; weighted_window=6s min_packets=120; settings unchanged')
            cls._delivery_seen = True
            accepted = cls._delivery_window.add(received_at, route, delivery)
            evidence = cls._delivery_window.evidence(received_at)
            if not accepted:
                evidence = DeliveryEvidence('insufficient',0,0,None,0,None,None)
        elif cls._delivery_seen:
            # Missing v2 stats cannot switch a live session back to percentage
            # voting. A new peer explicitly clears this capability marker.
            cls._delivery_window.clear()
            evidence = DeliveryEvidence('insufficient',0,0,None,0,None,None)
        if evidence is not None:
            if evidence.counts_valid:
                cls._delivery_missing_since = None
            elif cls._delivery_missing_since is None:
                cls._delivery_missing_since = received_at
        unhealthy = (
            packet_loss_percent is not None
            and packet_loss_percent >= NETWORK_PACKET_LOSS_THRESHOLD_PERCENT
        ) or (
            jitter_ms is not None
            and jitter_ms >= NETWORK_JITTER_THRESHOLD_MS
        )
        if evidence is not None:
            unhealthy = evidence.state in {'burst-loss','moderate','severe'}
            # A single RFC3550 jitter spike is not sustained congestion. Native
            # input / scheduling stalls and packet reordering can create it
            # without loss or a growing playout backlog. Keep the count/delay
            # window's classification, including its genuine emergency paths.
        previous = cls._receiver_network_report
        if previous is not None and (
            received_at < previous.received_at
            or received_at - previous.received_at > RECEIVER_REPORT_MAX_AGE_SECONDS
            or route != previous.route
        ):
            cls._receiver_network_history.clear()
            cls._receiver_probe_history.clear()
            cls._receiver_delivery_history.clear()
            cls._sender_delivery_history.clear()
            cls._receiver_last_loss_at = None
        if delivery is not None and accepted and delivery.lost_packets is not None and delivery.lost_packets > 0:
            cls._receiver_last_loss_at = received_at
        cls._receiver_network_history.append((received_at, unhealthy))
        cls._receiver_probe_history.append((received_at,
            route in {"direct", "turn"}
            and (evidence is None or evidence.state == 'healthy')
            and (packet_loss_percent if evidence is None else evidence.loss_percent) is not None
            and 0 <= (packet_loss_percent if evidence is None else evidence.loss_percent) <= 0.5
            and jitter_ms is not None and 0 <= jitter_ms <= (20 if evidence is None else 50)))
        if unhealthy:
            previous_is_currently_unhealthy = (
                previous is not None
                and previous.unhealthy
                and received_at - previous.received_at
                <= RECEIVER_REPORT_MAX_AGE_SECONDS
            )
            if not previous_is_currently_unhealthy:
                cls._receiver_network_unhealthy_since = received_at
        else:
            cls._receiver_network_unhealthy_since = None
        cls._receiver_network_report = ReceiverNetworkReport(
            jitter_ms=jitter_ms,
            packet_loss_percent=packet_loss_percent,
            received_at=received_at,
            route=route,
            unhealthy=unhealthy,
            delivery=evidence,
            received_bps=(delivery.received_bytes*8_000/delivery.interval_ms
                          if delivery is not None and delivery.received_bytes is not None else None),
            control_rtt_ms=delivery.control_rtt_ms if delivery is not None else None,
            decoded_frames=delivery.decoded_frames if delivery is not None else None,
            dropped_frames=delivery.dropped_frames if delivery is not None else None,
            interval_ms=delivery.interval_ms if delivery is not None else None,
            interval_packets=(delivery.received_packets+delivery.lost_packets
                if delivery is not None and accepted and delivery.received_packets is not None
                and delivery.lost_packets is not None else None),
            interval_loss_percent=(100*delivery.lost_packets/(delivery.received_packets+delivery.lost_packets)
                if delivery is not None and accepted and delivery.received_packets is not None
                and delivery.lost_packets is not None and delivery.received_packets+delivery.lost_packets else None),
        )
        cls._receiver_delivery_history.append(cls._receiver_network_report)

    @classmethod
    def _has_recurring_congestion(cls, now: float) -> bool:
        report = cls._receiver_network_report
        if report is not None and report.delivery is not None:
            return (0 <= now-report.received_at <= RECEIVER_REPORT_MAX_AGE_SECONDS
                    and report.delivery.state in {'burst-loss','moderate','severe'})
        samples = [(at, bad) for at, bad in tuple(cls._receiver_network_history)
                   if 0 <= now - at <= NETWORK_RECURRING_WINDOW_SECONDS]
        bad_times = [at for at, bad in samples if bad]
        return (
            len(bad_times) >= NETWORK_RECURRING_MIN_BAD_REPORTS
            and len(bad_times) * 2 >= len(samples)
            and bad_times[-1] - bad_times[0] >= NETWORK_DEGRADATION_HOLD_SECONDS
        )

    @classmethod
    def _network_downshift_reason(cls, now: float | None = None) -> str:
        checked_at = time.monotonic() if now is None else now
        report = cls._receiver_network_report
        if report is None:
            return "legacy-no-report"
        age = checked_at - report.received_at
        if age > RECEIVER_REPORT_MAX_AGE_SECONDS:
            return (
                "report-timeout"
                if age >= RECEIVER_REPORT_TIMEOUT_SECONDS
                else "report-gap"
            )
        if report.delivery is not None:
            if report.delivery.state == 'severe':
                return 'verified-severe-congestion'
            if (not report.delivery.counts_valid and cls._delivery_missing_since is not None
                    and checked_at-cls._delivery_missing_since >= RECEIVER_REPORT_TIMEOUT_SECONDS):
                return 'delivery-feedback-timeout'
            return {'severe':'verified-severe-congestion',
                    'burst-loss':'verified-burst-loss',
                    'moderate':'verified-moderate-congestion',
                    'healthy':'fresh-healthy'}.get(report.delivery.state, 'insufficient-delivery-evidence')
        unhealthy_since = cls._receiver_network_unhealthy_since
        if (
            report.unhealthy
            and unhealthy_since is not None
            and report.received_at - unhealthy_since
            >= NETWORK_DEGRADATION_HOLD_SECONDS
        ):
            return "sustained-congestion"
        if report.unhealthy and cls._has_recurring_congestion(checked_at):
            return "recurring-congestion"
        return "transient-congestion" if report.unhealthy else "fresh-healthy"

    @classmethod
    def _network_allows_bitrate_downshift(cls, now: float | None = None) -> bool:
        return cls._network_downshift_reason(now) in {
            "legacy-no-report", "report-timeout", "sustained-congestion", "recurring-congestion",
            "verified-burst-loss", "verified-moderate-congestion", "verified-severe-congestion", "delivery-feedback-timeout"
        }

    def __init__(self) -> None:
        self._profile_max_bitrate = DESKTOP_MAX_BITRATE
        super().__init__()
        # H264Encoder stores its bitrate in a private attribute. Keep a local
        # property so we can raise only the upper bound without patching aiortc
        # module constants or disabling receiver-driven congestion control.
        self._target_bitrate = aiortc_h264.DEFAULT_BITRATE
        self._receiver_target_bitrate = aiortc_h264.DEFAULT_BITRATE
        self._raw_receiver_bitrate = aiortc_h264.DEFAULT_BITRATE
        self._pending_lower_bitrate: int | None = None
        self._pending_lower_since: float | None = None
        self._pending_higher_bitrate: int | None = None
        self._last_upshift_at: float | None = None
        self._receiver_estimate_at: float | None = None
        self._last_wait_reason: str | None = None
        self._rate_guard_samples: deque[tuple[float, int, int]] = deque()
        self._rate_guard_target: int | None = None
        self._rate_guard_started: float | None = None
        self._initial_bitrate_applied = False
        self._last_scene_keyframe_time: float | None = None
        self._previous_luma_sample: np.ndarray | None = None
        self._stable_frame_count = 0
        self._active_backend = self.backend
        self._next_vbv_max_bitrate: int | None = None
        self._vbv_growth_blocked_until = 0.0
        self._recovery_ceiling = 0  # Set from the actual selected frame profile.
        self._capacity_probe: CapacityProbe | None = None
        self._probe_next_at = 0.0
        self._probe_backoff = RECOVERY_PROBE_INTERVAL_SECONDS
        self._last_downshift_at: float | None = None
        self._moderate_loss_used_at = -math.inf
        self._validated_media_budget: tuple[int,float,str,float] | None = None
        self._probe_measured_delivery: float | None = None
        self._motion_retry: tuple[float, str, float] | None = None
        self._recent_probe_motion: tuple[float, str] | None = None
        self._sampled_content_motion = False

    @property
    def target_bitrate(self) -> int:
        """Receiver-controlled target bitrate with a desktop-video ceiling."""

        return self._target_bitrate

    @target_bitrate.setter
    def target_bitrate(self, bitrate: int) -> None:
        self._raw_receiver_bitrate = int(bitrate)
        clamped = max(
            aiortc_h264.MIN_BITRATE,
            min(int(bitrate), self._profile_max_bitrate),
        )
        previous_receiver = self._receiver_target_bitrate
        self._receiver_target_bitrate = clamped
        self._receiver_estimate_at = time.monotonic()

        if not self._initial_bitrate_applied or clamped == self._target_bitrate:
            self._target_bitrate = clamped
            self._pending_lower_bitrate = None
            self._pending_lower_since = None
            self._pending_higher_bitrate = None
            self._last_upshift_at = None
        elif clamped > self._target_bitrate:
            self._pending_higher_bitrate = clamped
            self._pending_lower_bitrate = None
            self._pending_lower_since = None
        else:
            # Keep the first observation time while later REMB reports refine
            # the pending value. A noisy estimate must not restart the hold on
            # every report and therefore postpone a real congestion response
            # forever.
            self._pending_lower_bitrate = clamped
            self._pending_higher_bitrate = None
            self._last_upshift_at = None

        if (
            clamped != previous_receiver
            and abs(clamped - previous_receiver)
            >= max(250_000, round(previous_receiver * 0.20))
        ):
            LOGGER.info(
                "H.264 receiver bitrate estimate: %.2f Mbps",
                clamped / 1_000_000,
            )

    @property
    def receiver_target_bitrate(self) -> int:
        """Latest bounded REMB; the original is retained in _raw_receiver_bitrate."""

        return self._receiver_target_bitrate

    def _log_rate_decision(self, reason: str, previous: int) -> None:
        report = self._receiver_network_report
        LOGGER.info(
            "H.264 rate decision: reason=%s target=%.2f->%.2f Mbps "
            "remb=%.2f Mbps remb_age=%s report_age=%s "
            "loss_pct=%s jitter_ms=%s route=%s delivery=%s packets=%s lost=%s weighted_loss_pct=%s buffer_ms=%s decode_ms=%s "
            "raw_remb_bps=%s rx_bps=%s tx_bps=%s control_rtt_ms=%s recovered=%s",
            reason, previous / 1_000_000, self.target_bitrate / 1_000_000,
            self.receiver_target_bitrate / 1_000_000,
            "none" if self._receiver_estimate_at is None else round(
                max(0, time.monotonic() - self._receiver_estimate_at), 2
            ),
            "none" if report is None else round(
                max(0, time.monotonic() - report.received_at), 2
            ),
            None if report is None else report.packet_loss_percent,
            None if report is None else report.jitter_ms,
            "unknown" if report is None else report.route,
            'legacy' if report is None or report.delivery is None else report.delivery.state,
            None if report is None or report.delivery is None else report.delivery.packets,
            None if report is None or report.delivery is None else report.delivery.lost,
            None if report is None or report.delivery is None or report.delivery.loss_percent is None else round(report.delivery.loss_percent,3),
            None if report is None or report.delivery is None else report.delivery.buffer_ms,
            None if report is None or report.delivery is None else report.delivery.decode_ms,
            self._raw_receiver_bitrate,
            None if report is None else report.received_bps,
            self._sender_delivery_history[-1][1] if self._sender_delivery_history and
                0 <= time.monotonic()-self._sender_delivery_history[-1][0] <= 2.5 else None,
            None if report is None else report.control_rtt_ms,
            None if report is None or report.delivery is None else report.delivery.recovered,
        )

    def _apply_pending_higher_bitrate(self) -> None:
        if self._capacity_probe is not None:
            return  # A trial cannot grow again before feedback confirms it.
        pending = self._pending_higher_bitrate
        if pending is None:
            return
        now = time.monotonic()
        report = self._receiver_network_report
        if (
            self._receiver_estimate_at is None
            or now - self._receiver_estimate_at > RECEIVER_ESTIMATE_MAX_AGE_SECONDS
            or self._has_recurring_congestion(now)
            or (
                report is not None
                and (
                    report.unhealthy
                    or (report.delivery is not None and report.delivery.state != 'healthy')
                    or now - report.received_at > RECEIVER_REPORT_MAX_AGE_SECONDS
                )
            )
        ):
            # No blind recovery from low loss alone, a stale REMB, or a report
            # gap. A new higher capacity estimate is the upper bound.
            self._last_upshift_at = None
            return
        if self._last_upshift_at is None:
            self._last_upshift_at = now
            return
        if now - self._last_upshift_at < BITRATE_UPSHIFT_INTERVAL_SECONDS:
            return
        previous = self._target_bitrate
        candidate = min(
            pending,
            previous + max(100_000, round(previous * BITRATE_UPSHIFT_FRACTION)),
        )
        if (self.codec is not None and now < self._vbv_growth_blocked_until
                and previous <= (self.codec.max_bit_rate or 0)):
            candidate = min(candidate, self.codec.max_bit_rate)
        if candidate <= previous:
            return
        self._target_bitrate = candidate
        self._last_upshift_at = now
        self._log_rate_decision("capacity-recovery", previous)
        if self._target_bitrate == pending:
            self._pending_higher_bitrate = None
            self._last_upshift_at = None

    def _probe_feedback_healthy(self, now: float, report: ReceiverNetworkReport | None) -> bool:
        # Stricter than the congestion threshold: incomplete, jittery, lossy or
        # stale reports are not permission to test additional capacity.
        loss = None if report is None else (
            report.packet_loss_percent if report.delivery is None else report.delivery.loss_percent)
        jitter_limit = 20 if report is None or report.delivery is None else 50
        return bool(
            report is not None
            and (report.delivery is None or report.delivery.state == 'healthy')
            and report.route in {"direct", "turn"}
            and 0 <= now - report.received_at <= RECOVERY_PROBE_FEEDBACK_AGE_SECONDS
            and loss is not None and 0 <= loss <= 0.5
            # Old healthy rows must not dilute a new, adequately sampled loss
            # interval enough to confirm/continue a recovery trial.
            and not (report.interval_packets is not None and report.interval_packets>=120
                     and report.interval_loss_percent is not None and report.interval_loss_percent>=2)
            and report.jitter_ms is not None and 0 <= report.jitter_ms <= jitter_limit
            and not self._has_recurring_congestion(now)
        )

    def _probe_has_healthy_history(self, now: float) -> bool:
        report = self._receiver_network_report
        if report is None or not 0 <= now-report.received_at <= RECOVERY_PROBE_FEEDBACK_AGE_SECONDS:
            return False
        # History is measured at its latest REPORT, not every encode frame.
        # Otherwise sample expiry between two healthy reports fabricates a gap.
        end = report.received_at
        samples = [(at, healthy) for at, healthy in tuple(self._receiver_probe_history)
                   if 0 <= end - at <= RECOVERY_PROBE_HEALTH_SECONDS]
        return bool(
            len(samples) >= 4 and all(healthy for _, healthy in samples)
            and samples[-1][0] - samples[0][0] >= RECOVERY_PROBE_HEALTH_SECONDS - 1
            and all(0 < right[0] - left[0] <= RECOVERY_PROBE_FEEDBACK_AGE_SECONDS
                    for left, right in zip(samples, samples[1:]))
        )

    def _finish_capacity_probe(self, now: float, reason: str, *, confirmed: bool = False,
                               inconclusive: bool = False) -> None:
        probe = self._capacity_probe
        assert probe is not None
        self._capacity_probe = None
        if confirmed:
            self._probe_backoff = RECOVERY_PROBE_INTERVAL_SECONDS
            if self._probe_measured_delivery is not None and probe.route is not None:
                previous_proof=self._validated_media_budget
                peak=self._probe_measured_delivery
                if (previous_proof is not None and previous_proof[2]==probe.route
                        and (self._last_downshift_at is None or self._last_downshift_at<previous_proof[3])):
                    peak=max(peak,previous_proof[1])
                self._validated_media_budget=(self.target_bitrate,peak,probe.route,now)
        else:
            # Never undo a real congestion downshift that already went lower.
            self._target_bitrate = min(self._target_bitrate, probe.baseline)
            self._pending_higher_bitrate = None
            self._last_upshift_at = None
        retry = self._probe_backoff
        if (confirmed and self._probe_measured_delivery is not None
                and self._last_downshift_at is not None
                and now >= self._last_downshift_at):
            retry = RECOVERY_PROVEN_RETRY_SECONDS
        self._probe_next_at = now + retry
        if not confirmed and not inconclusive:
            self._probe_backoff = min(RECOVERY_PROBE_MAX_BACKOFF_SECONDS,
                                      self._probe_backoff * 2)
        self._log_rate_decision("probe-" + reason, probe.target)
        self._probe_measured_delivery=None

    def _can_retry_on_motion(self, now: float, report: ReceiverNetworkReport) -> bool:
        if self._motion_retry is None:
            return False
        earliest, route, previous_peak = self._motion_retry
        if now < earliest or report.route != route or not self._source_limited_probe_ready(now):
            return False
        rx, tx = self._baseline_probe_delivery(now)
        return bool(rx is not None and tx is not None and min(rx, tx) >= previous_peak + max(
            RECOVERY_MOTION_RETRY_MIN_GROWTH_BPS, previous_peak * 0.15))

    def _measured_probe_capacity(self, probe: CapacityProbe, now: float) -> int | None:
        """Bounded alternative to REMB: delivery, not absence of loss, proves rate.

        Exclude the first second/IDR. Require two aligned sender+receiver samples,
        actual received growth, stable playout/control delay, and no frame drops.
        No stats/motion/bytes means no permission to increase. Sender counters
        describe emitted RTP payload, not measured physical-link throughput.
        """
        if (probe.baseline_received_bps is None or probe.baseline_buffer_ms is None
                or probe.baseline_control_rtt_ms is None):
            return None
        settled = probe.settled_after if probe.settled_after is not None else probe.started_at+1
        rows = [r for r in tuple(self._receiver_delivery_history)
                if r.interval_ms is not None and r.received_at-r.interval_ms/1000 >= settled
                and r.received_at <= min(now,probe.started_at+RECOVERY_PROBE_MAX_DURATION_SECONDS)]
        if (len(rows) < 2 or rows[-1].received_at-rows[0].received_at < .7
                or not 0 <= now-rows[-1].received_at <= 2.5):
            return None
        received=[];sent=[];weights=[]
        for row in rows:
            send = min(tuple(self._sender_delivery_history),
                       key=lambda s: abs(s[0]-row.received_at), default=None)
            if (row.route != probe.route or row.delivery is None or row.delivery.state != 'healthy'
                    or row.received_bps is None
                    or row.control_rtt_ms is None or row.control_rtt_ms > min(250,probe.baseline_control_rtt_ms+25)
                    or row.delivery.buffer_ms is None or row.delivery.buffer_ms > min(200,probe.baseline_buffer_ms+25)
                    or row.decoded_frames is None or row.decoded_frames < 1 or row.dropped_frames != 0
                    or send is None or abs(send[0]-row.received_at) > .75
                    or send[1] <= 0):
                return None
            received.append(row.received_bps);sent.append(send[1])
            weights.append(row.interval_ms)
        # One-second source intervals fluctuate during scrolling. Compare the
        # aggregate of at least two FULL post-warmup intervals, never an IDR or
        # one lucky burst. Ratios still reject missing delivery/RTX inflation.
        rx=sum(rate*weight for rate,weight in zip(received,weights))/sum(weights)
        tx=sum(rate*weight for rate,weight in zip(sent,weights))/sum(weights)
        if not tx*.85 <= rx <= tx*1.25:
            return None
        matched=min(rx,tx)
        if matched >= max(probe.baseline*1.08,probe.target*.85,probe.baseline_received_bps*1.08):
            self._probe_measured_delivery=matched
            return min(probe.target,round(matched))
        if probe.baseline_sent_bps is not None:
            before=min(probe.baseline_received_bps,probe.baseline_sent_bps)
            proof=self._validated_media_budget
            if (proof is not None and proof[0]==probe.baseline and proof[2]==probe.route
                    and (self._last_downshift_at is None or self._last_downshift_at<proof[3])):
                # Never ratchet on the same low/high source-content cycle.
                # Partial budget growth requires a NEW delivered high-water.
                before=max(before,proof[1])
            growth=matched-before
            if (before>=64_000 and growth>=max(20_000,before*.08)):
                # ABR target is an encoder budget, not emitted throughput. x264
                # can emit e.g.0.42->0.48Mbps when budget0.5->0.6Mbps. Retain
                # ONLY the additional payload actually delivered (here+60kbps),
                # not the unmeasured full100kbps trial. This is NOT a claim of
                # physical-link capacity equal to the nominal encoder target.
                self._probe_measured_delivery=matched
                return min(probe.target,probe.baseline+round(growth))
        return None

    def _baseline_probe_delivery(self, now: float) -> tuple[float | None,float | None]:
        rows=[r for r in self._receiver_delivery_history if 0<=now-r.received_at<=3.5][-3:]
        if len(rows)<2 or rows[-1].received_at-rows[0].received_at<.7:
            return None,None
        rx=[];tx=[];weights=[]
        for row in rows:
            send=min(tuple(self._sender_delivery_history),key=lambda s:abs(s[0]-row.received_at),default=None)
            if (row.received_bps is None or row.interval_ms is None or row.delivery is None or row.delivery.state!='healthy'
                    or send is None or abs(send[0]-row.received_at)>.75 or send[1]<=0
                    or not send[1]*.85<=row.received_bps<=send[1]*1.25):
                return None,None
            rx.append(row.received_bps);tx.append(send[1])
            weights.append(row.interval_ms)
        return (sum(rate*weight for rate,weight in zip(rx,weights))/sum(weights),
                sum(rate*weight for rate,weight in zip(tx,weights))/sum(weights))

    def _delivered_current_target(self, now: float) -> bool:
        """Fresh delivered traffic, not a clamped REMB, can qualify another trial.

        A previous successful step must not strand recovery at 0.72Mbps solely
        because a sparse desktop still reports the old 0.5Mbps floor. This is
        only a start gate: NEW delivered growth must still confirm the trial.
        """
        rows = [r for r in tuple(self._receiver_delivery_history)
                if 0 <= now-r.received_at <= 2.5]
        if len(rows)<2 or rows[-1].received_at-rows[-2].received_at<.7:
            return False
        required=self.target_bitrate*.9
        proof=self._validated_media_budget
        if (proof is not None and proof[0]==self.target_bitrate and proof[2]==self._receiver_network_report.route
                and (self._last_downshift_at is None or self._last_downshift_at<proof[3])):
            required=max(64_000,proof[1]*.9)
        for row in rows[-2:]:
            send=min(tuple(self._sender_delivery_history),key=lambda s:abs(s[0]-row.received_at),default=None)
            if (not self._probe_feedback_healthy(now,row) or row.route != self._receiver_network_report.route
                    or row.received_bps is None or row.received_bps < required
                    or row.control_rtt_ms is None or row.control_rtt_ms>250
                    or row.delivery is None or row.delivery.buffer_ms is None or row.delivery.buffer_ms>200
                    or row.decoded_frames is None or row.decoded_frames<1 or row.dropped_frames!=0
                    or send is None or abs(send[0]-row.received_at)>.75 or send[1]<required
                    or not send[1]*.85 <= row.received_bps <= send[1]*1.25):
                return False
        return True

    def _source_limited_probe_ready(self, now: float) -> bool:
        """Qualify a bounded trial, never capacity confirmation, after a cut.

        Quiet content can send 0.3Mbps at a 0.84Mbps encoder budget. Requiring
        90% saturation or a REMB >=80% of that budget strands recovery.
        Matched real delivery with low delay may qualify one moving-content
        trial. New payload growth must still pass the original verifier.
        """
        if self._last_downshift_at is None:
            return False
        rx, tx = self._baseline_probe_delivery(now)
        if rx is None or tx is None or min(rx,tx) < 64_000 or tx >= self.target_bitrate*.9:
            return False
        rows=[r for r in self._receiver_delivery_history if 0<=now-r.received_at<=2.5]
        if len(rows)<2 or rows[-1].received_at-rows[-2].received_at<.7:
            return False
        for row in rows[-2:]:
            if (not self._probe_feedback_healthy(now,row)
                    or row.route != self._receiver_network_report.route
                    or row.control_rtt_ms is None or not 0<=row.control_rtt_ms<=100
                    or row.delivery is None or row.delivery.buffer_ms is None
                    or row.delivery.buffer_ms>120 or row.decoded_frames is None
                    or row.decoded_frames<1 or row.dropped_frames!=0):
                return False
        return abs(rows[-1].delivery.buffer_ms-rows[-2].delivery.buffer_ms)<=25

    def _update_capacity_probe(self, *, content_stable: bool, content_changed: bool = False) -> None:
        now = time.monotonic()
        probe = self._capacity_probe
        report = self._receiver_network_report
        healthy = self._probe_feedback_healthy(now, report)
        if (not healthy or report is None or report.route not in {"direct", "turn"}
                or self._last_downshift_at is None or self._last_downshift_at > now):
            self._recent_probe_motion = None
        elif probe is None:
            motion = self._recent_probe_motion
            if motion is not None and (motion[1] != report.route
                    or not 0 <= now - motion[0] <= RECOVERY_SHORT_MOTION_SECONDS):
                self._recent_probe_motion = None
            if content_changed and not content_stable:
                self._recent_probe_motion = (now, report.route)
        if probe is not None:
            # A trial has already passed the history gate. During its bounded
            # 3s lifetime, require fresh current feedback, not a continuously
            # re-qualifying six-second start window. Never ignore new bad data.
            abort = ("feedback" if not healthy else
                     "route" if probe.route is not None and report.route != probe.route else
                     "downshift" if self.target_bitrate < probe.target else
                     "profile" if probe.target > self._recovery_ceiling else None)
            if abort is not None:
                self._finish_capacity_probe(now, "aborted-" + abort)
            elif now - probe.started_at >= RECOVERY_PROBE_DURATION_SECONDS:
                measured = self._measured_probe_capacity(probe, now)
                if measured is not None and measured > probe.baseline:
                    self._target_bitrate = min(self.target_bitrate,measured)
                    self._finish_capacity_probe(now, "delivery-confirmed" if measured >= probe.target else
                                                "delivery-partial", confirmed=True)
                elif (self._receiver_estimate_at is not None
                        and (probe.settled_after or probe.started_at+1) <= self._receiver_estimate_at <= min(now,probe.started_at+RECOVERY_PROBE_MAX_DURATION_SECONDS)
                        and now-self._receiver_estimate_at <= RECOVERY_PROBE_FEEDBACK_AGE_SECONDS
                        and report.received_at >= probe.started_at+1
                        and probe.baseline < self.receiver_target_bitrate < probe.target):
                    self._target_bitrate = min(self.target_bitrate,self.receiver_target_bitrate)
                    self._finish_capacity_probe(now, "partial", confirmed=True)
                elif now-probe.started_at >= RECOVERY_PROBE_MAX_DURATION_SECONDS:
                    # Sparse content cannot prove capacity, but is not a failed
                    # network test. Do not penalize it with exponential backoff.
                    recent=[s[1] for s in self._sender_delivery_history if probe.started_at+1 <= s[0] <= now]
                    sparse=bool(recent) and max(recent)<probe.target*.85
                    self._finish_capacity_probe(now, "application-limited" if sparse else "unconfirmed",
                                                inconclusive=sparse)
                    if sparse and probe.route is not None and self._last_downshift_at is not None:
                        self._motion_retry = (now + RECOVERY_MOTION_RETRY_SECONDS, probe.route, max(recent))
            elif (self._receiver_estimate_at is not None
                  and (probe.settled_after or probe.started_at+1) <= self._receiver_estimate_at <= now
                  and now - self._receiver_estimate_at <= RECOVERY_PROBE_FEEDBACK_AGE_SECONDS
                  and report.received_at >= probe.started_at + 1
                  and self.receiver_target_bitrate >= probe.target):
                # Capacity must be acknowledged by a NEW RTCP estimate and a
                # new healthy report, not inferred solely from lack of loss.
                self._finish_capacity_probe(now, "confirmed", confirmed=True)
            return
        motion = self._recent_probe_motion
        use_recent_motion = bool(content_stable and motion is not None and report is not None
                                 and report.received_at >= motion[0])
        if ((content_stable and not use_recent_motion) or not healthy or not self._probe_has_healthy_history(now)
                or now < self._vbv_growth_blocked_until
                or self._active_backend != SOFTWARE_H264_BACKEND
                or self.codec is None or not self._initial_bitrate_applied
                or self._pending_higher_bitrate is not None
                or self._receiver_estimate_at is None
                or not 0 <= now - self._receiver_estimate_at <= RECOVERY_PROBE_FEEDBACK_AGE_SECONDS
                or not (self.target_bitrate * 0.8 <= self.receiver_target_bitrate <= self.target_bitrate
                        or self._delivered_current_target(now)
                        or self._source_limited_probe_ready(now))):
            return
        early_motion_retry = now < self._probe_next_at
        if early_motion_retry and not self._can_retry_on_motion(now, report):
            return
        ceiling = min(self._recovery_ceiling, self._profile_max_bitrate)
        trial = min(ceiling, self.target_bitrate + min(
            RECOVERY_PROBE_MAX_EXTRA_BITRATE, round(self.target_bitrate * RECOVERY_PROBE_FRACTION)))
        if trial <= self.target_bitrate:
            return
        baseline_rx,baseline_tx=self._baseline_probe_delivery(now)
        self._probe_measured_delivery=None
        self._motion_retry = None
        self._recent_probe_motion = None
        self._capacity_probe = CapacityProbe(now, self.target_bitrate, trial, report.route,
            baseline_rx if baseline_rx is not None else report.received_bps,
            report.delivery.buffer_ms if report.delivery is not None else None,
            report.control_rtt_ms,baseline_sent_bps=baseline_tx)
        previous = self.target_bitrate
        self._target_bitrate = trial
        if use_recent_motion:
            reason = "probe-short-motion-retry" if early_motion_retry else "probe-short-motion-start"
        else:
            reason = "probe-motion-retry" if early_motion_retry else "probe-start"
        self._log_rate_decision(reason, previous)

    @staticmethod
    def _frame_time(frame: av.VideoFrame) -> float | None:
        if frame.pts is None or frame.time_base is None:
            return None
        return float(frame.pts * frame.time_base)

    @staticmethod
    def _preferred_initial_bitrate(frame: av.VideoFrame) -> int:
        pixels = frame.width * frame.height
        if pixels >= 1920 * 1080:
            return HIGH_INITIAL_BITRATE
        if pixels >= 1600 * 900:
            return BALANCED_INITIAL_BITRATE
        return LOW_INITIAL_BITRATE

    @staticmethod
    def _preferred_max_bitrate(frame: av.VideoFrame) -> int:
        pixels = frame.width * frame.height
        if pixels >= 1920 * 1080:
            return HIGH_MAX_BITRATE
        if pixels >= 1600 * 900:
            return BALANCED_MAX_BITRATE
        return LOW_MAX_BITRATE

    @staticmethod
    def _preferred_frame_rate(frame: av.VideoFrame) -> int:
        # Match the fixed desktop profiles. Generic aiortc defaults to 30 fps,
        # which underallocates bits/frame for our 10/15/20 fps captures.
        if frame.width * frame.height >= 1920 * 1080:
            return 20
        if frame.width * frame.height >= 1600 * 900:
            return 15
        return 10

    @staticmethod
    def _sample_luma(frame: av.VideoFrame) -> np.ndarray | None:
        """Copy a small Y-plane sample without converting or copying the frame."""

        if frame.format.name != "yuv420p" or not frame.planes:
            return None
        plane = frame.planes[0]
        if frame.width <= 0 or frame.height <= 0 or plane.line_size < frame.width:
            return None
        try:
            luma = np.frombuffer(plane, dtype=np.uint8).reshape(
                frame.height,
                plane.line_size,
            )
        except (BufferError, ValueError):
            # Scene detection is an optional quality hint. A frame with an
            # unusual plane layout must still reach the encoder normally.
            return None
        row_step = max(1, frame.height // SCENE_SAMPLE_ROWS)
        column_step = max(1, frame.width // SCENE_SAMPLE_COLUMNS)
        return luma[
            : frame.height : row_step,
            : frame.width : column_step,
        ].copy()

    def _is_large_scene_change(
        self,
        frame: av.VideoFrame,
        frame_time: float | None,
    ) -> bool:
        current = self._sample_luma(frame)
        self._sampled_content_motion = False
        previous = self._previous_luma_sample
        self._previous_luma_sample = current
        if (
            current is None
            or previous is None
            or current.shape != previous.shape
        ):
            self._stable_frame_count = 0
            return False

        had_stable_context = (
            self._stable_frame_count >= SCENE_STABLE_FRAMES_REQUIRED
        )
        delta = np.abs(current.astype(np.int16) - previous.astype(np.int16))
        changed_fraction = float(np.mean(delta >= SCENE_PIXEL_DELTA))
        mean_delta = float(np.mean(delta))
        stable = (
            changed_fraction <= SCENE_STABLE_CHANGED_FRACTION
            and mean_delta <= SCENE_STABLE_MEAN_DELTA
        )
        self._stable_frame_count = self._stable_frame_count + 1 if stable else 0
        self._sampled_content_motion = not stable

        if (
            frame_time is not None
            and self._last_scene_keyframe_time is not None
            and frame_time - self._last_scene_keyframe_time
            < SCENE_KEYFRAME_COOLDOWN_SECONDS
        ):
            return False

        large_change = (
            had_stable_context
            and changed_fraction >= SCENE_CHANGED_FRACTION
            and mean_delta >= SCENE_MEAN_DELTA
        )
        return large_change and not self._is_vertical_scroll(previous, current)

    @staticmethod
    def _is_vertical_scroll(previous: np.ndarray, current: np.ndarray) -> bool:
        if previous.shape != current.shape or current.shape[0] < 4:
            return False
        before = previous.astype(np.int16)
        after = current.astype(np.int16)
        max_shift = min(SCROLL_SAMPLE_MAX_SHIFT, current.shape[0] // 4)
        for shift in range(1, max_shift + 1):
            for old_rows, new_rows, same_rows in (
                (before[shift:], after[:-shift], before[:-shift]),
                (before[:-shift], after[shift:], before[shift:]),
            ):
                baseline = float(np.mean(np.abs(same_rows - new_rows)))
                aligned = float(np.mean(np.abs(old_rows - new_rows)))
                if (
                    baseline >= SCENE_MEAN_DELTA
                    and aligned <= SCENE_MEAN_DELTA / 2
                    and aligned <= baseline * SCROLL_MATCH_ERROR_RATIO
                ):
                    return True
        return False

    def _apply_pending_lower_bitrate(
        self,
        frame_time: float | None,
        *,
        content_stable: bool,
    ) -> None:
        pending = self._pending_lower_bitrate
        if pending is None or frame_time is None:
            return
        if pending >= self._target_bitrate:
            self._pending_lower_bitrate = None
            self._pending_lower_since = None
            return
        reason = self._network_downshift_reason()
        now = time.monotonic()
        report = self._receiver_network_report
        if (report is not None and report.delivery is not None and
                (self._receiver_estimate_at is None or not 0 <= now-self._receiver_estimate_at <= RECEIVER_ESTIMATE_MAX_AGE_SECONDS)):
            return  # No reduction based on an obsolete capacity estimate.
        severe = reason == 'verified-severe-congestion'
        burst = reason == 'verified-burst-loss'
        # A burst gets an immediate bounded step, not a minimum-rate collapse.
        # Persistent loss/backlog/jitter retains the emergency response.
        if not (severe or burst) and (self._pending_lower_since is None or frame_time < self._pending_lower_since):
            self._pending_lower_since = frame_time
            return
        if not (severe or burst) and frame_time - self._pending_lower_since < BITRATE_DOWNSHIFT_HOLD_SECONDS:
            return
        if reason not in {"legacy-no-report", "report-timeout", "sustained-congestion", "recurring-congestion",
                          "verified-burst-loss", "verified-moderate-congestion", "verified-severe-congestion", "delivery-feedback-timeout"}:
            if self._last_wait_reason != reason:
                self._last_wait_reason = reason
                self._log_rate_decision("defer-" + reason, self.target_bitrate)
            return
        if content_stable and reason not in {"sustained-congestion", "recurring-congestion",
                                            "verified-burst-loss", "verified-moderate-congestion", "verified-severe-congestion"}:
            # A static desktop produces misleadingly small REMB estimates, but
            # confirmed loss/jitter still requires a congestion response. Live
            # ABR can now lower the target without destroying its crisp reference.
            return

        previous = self._target_bitrate
        report = self._receiver_network_report
        if (reason in {'verified-burst-loss','verified-moderate-congestion'} and report is not None and report.delivery is not None
                and not report.delivery.delay_pressure and self._receiver_last_loss_at is not None
                and self._receiver_last_loss_at <= self._moderate_loss_used_at):
            if self._last_wait_reason != 'reused-loss-evidence':
                self._last_wait_reason = 'reused-loss-evidence'
                self._log_rate_decision('defer-reused-loss-evidence',previous)
            return
        stepped = reason in {'verified-burst-loss','verified-moderate-congestion','delivery-feedback-timeout'} or (
            reason == 'report-timeout' and report is not None and report.delivery is not None)
        if stepped:
            if self._last_downshift_at is not None and now-self._last_downshift_at < 2:
                return
            # The raw REMB may be application-limited. Mild measured loss does
            # not justify an80% collapse; severe evidence above still can.
            pending = max(pending, round(previous*.8), aiortc_h264.MIN_BITRATE)
        self._target_bitrate = pending
        self._last_downshift_at = now
        self._validated_media_budget=None
        self._motion_retry = None
        self._recent_probe_motion = None
        if reason in {'verified-burst-loss','verified-moderate-congestion'} and self._receiver_last_loss_at is not None:
            self._moderate_loss_used_at = self._receiver_last_loss_at
        self._pending_lower_bitrate = None
        self._pending_lower_since = None
        self._last_wait_reason = None
        LOGGER.info(
            "H.264 applied sustained bitrate decrease: %.2f -> %.2f Mbps",
            previous / 1_000_000,
            self._target_bitrate / 1_000_000,
        )
        self._log_rate_decision(reason, previous)

    def _reset_codec(self, reason: str, *, vbv_max_bitrate: int | None = None) -> None:
        LOGGER.info(
            "H.264 codec reset: reason=%s backend=%s target=%.2f Mbps",
            reason, self._active_backend, self.target_bitrate / 1_000_000,
        )
        self.buffer_data = b""
        self.buffer_pts = None
        self.codec = None
        self._next_vbv_max_bitrate = vbv_max_bitrate
        self._rate_guard_samples.clear()
        self._rate_guard_started = None
        if reason == "payload-budget":
            self._vbv_growth_blocked_until = time.monotonic() + VBV_REBUILD_HOLD_SECONDS
            if self._capacity_probe is not None:
                # Local overshoot guard is not evidence of transport failure.
                # Keep the same bounded trial target and rebuild tightly at
                # that rate; discard pre-rebuild/IDR measurement intervals.
                # The absolute six-second deadline and congestion abort remain.
                self._capacity_probe=replace(self._capacity_probe,settled_after=time.monotonic()+1)

    def _rate_guard_exceeded(self, data_size: int, frame_time: float | None) -> bool:
        """Keep a preserved x264 VBV envelope from overrunning a lower target.

        PyAV permits a live bit_rate change but exposes max_bit_rate read-only.
        x264 preserves its old bounded VBV envelope during ABR reconfiguration.
        If the current frame or a one-second payload window exceeds the new
        target plus half a second of burst allowance, discard this *unsent*
        encode and re-encode the same input as a tightly bounded IDR. Never drop
        a transmitted reference, queue frames, sleep, or bypass congestion.
        """
        assert self.codec is not None
        if self._rate_guard_target != self.target_bitrate:
            self._rate_guard_target = self.target_bitrate
            # Do not clear the window for each recovery step: frequent target
            # updates must not continually restart the overshoot grace period.
            # Samples carry their own target to normalize a changing budget.
        if (
            self._active_backend != SOFTWARE_H264_BACKEND
            or (self.codec.max_bit_rate or 0) <= self.target_bitrate
        ):
            return False
        allowance = self.target_bitrate * RATE_CONTROL_BUFFER_SECONDS / 8
        if data_size > allowance:
            return True
        if frame_time is None:
            return False
        if (
            self._rate_guard_started is None
            or frame_time < self._rate_guard_started
            or (
                self._rate_guard_samples
                and frame_time <= self._rate_guard_samples[-1][0]
            )
        ):
            self._rate_guard_samples.clear()
            self._rate_guard_started = frame_time
        self._rate_guard_samples.append((frame_time, data_size, self.target_bitrate))
        while (
            self._rate_guard_samples
            and frame_time - self._rate_guard_samples[0][0] >= RATE_GUARD_WINDOW_SECONDS
        ):
            self._rate_guard_samples.popleft()
        return (
            frame_time - self._rate_guard_started >= RATE_GUARD_WINDOW_SECONDS
            and sum(size * 8 / target for _, size, target in self._rate_guard_samples)
            > RATE_GUARD_WINDOW_SECONDS + RATE_CONTROL_BUFFER_SECONDS
        )

    def _encode_frame(
        self, frame: av.VideoFrame, force_keyframe: bool
    ) -> Iterator[bytes]:
        # replaceTrack retains this encoder. Re-evaluate only the ceiling on a
        # profile switch, never invent additional capacity or reset to an
        # initial bitrate that could exceed a congested receiver's estimate.
        profile_max = self._preferred_max_bitrate(frame)
        self._recovery_ceiling = self._preferred_initial_bitrate(frame)
        if self._initial_bitrate_applied and profile_max != self._profile_max_bitrate:
            self._validated_media_budget=None
            self._recent_probe_motion = None
            self._profile_max_bitrate = profile_max
            self._target_bitrate = min(self._target_bitrate, profile_max)
            self._receiver_target_bitrate = min(self._receiver_target_bitrate, profile_max)
            if self._pending_higher_bitrate is not None:
                self._pending_higher_bitrate = min(self._pending_higher_bitrate, profile_max)
            if self._pending_lower_bitrate is not None:
                self._pending_lower_bitrate = min(self._pending_lower_bitrate, profile_max)
        # RTCP feedback cannot exist before the encoder's first frame. Give each
        # desktop resolution a readable starting point, then leave all later
        # target changes to the receiver's REMB congestion estimate.
        if not self._initial_bitrate_applied:
            self._profile_max_bitrate = profile_max
            if self.target_bitrate == aiortc_h264.DEFAULT_BITRATE:
                self.target_bitrate = self._preferred_initial_bitrate(frame)
            else:
                # REMB can arrive before the first encoded frame. Re-apply the
                # selected mode's ceiling before creating the codec.
                self.target_bitrate = self.target_bitrate
            self._initial_bitrate_applied = True

        frame_time = self._frame_time(frame)
        scene_keyframe = self._is_large_scene_change(frame, frame_time)
        # A changed window is not evidence of a broken decoder reference.
        # With fresh healthy feedback, let inter/intra prediction refresh the
        # new image progressively instead of injecting an unsolicited IDR.
        # Unknown/bad/stale feedback retains proactive recovery; explicit
        # receiver PLI/FIR (force_keyframe) is honored below in every state.
        if scene_keyframe and self._network_downshift_reason() == "fresh-healthy":
            scene_keyframe = False
        self._apply_pending_lower_bitrate(
            frame_time,
            content_stable=(
                self._stable_frame_count >= SCENE_STABLE_FRAMES_REQUIRED
            ),
        )
        dimensions_changed = self.codec is not None and (
            frame.width != self.codec.width or frame.height != self.codec.height)
        if dimensions_changed:
            self._recent_probe_motion = None
        self._update_capacity_probe(
            content_stable=self._stable_frame_count >= SCENE_STABLE_FRAMES_REQUIRED,
            content_changed=self._sampled_content_motion and not dimensions_changed,
        )
        self._apply_pending_higher_bitrate()
        # This follows aiortc 1.15's H264Encoder implementation. Packetisation
        # and receiver-controlled target bitrate remain compatible with aiortc.
        if self.codec:
            if dimensions_changed:
                self._reset_codec("dimensions")
            elif self._active_backend == SOFTWARE_H264_BACKEND:
                if self.target_bitrate > (self.codec.max_bit_rate or 0):
                    reserve = min(
                        self._profile_max_bitrate,
                        max(
                            self.target_bitrate,
                            (self.codec.max_bit_rate or self.target_bitrate)
                            * VBV_RECOVERY_GROWTH_FACTOR,
                        ),
                    )
                    self._reset_codec("vbv-capacity-increase", vbv_max_bitrate=reserve)
                elif self.codec.bit_rate != self.target_bitrate:
                    # FFmpeg's libx264 wrapper reconfigures ABR per input frame.
                    # Retain reference pictures; changing options after open
                    # would not update the VBV maxrate/buffer in PyAV.
                    self.codec.bit_rate = self.target_bitrate
                    LOGGER.info(
                        "H.264 live ABR update: target=%.2f Mbps vbv_max=%.2f Mbps",
                        self.target_bitrate / 1_000_000,
                        self.codec.max_bit_rate / 1_000_000,
                    )
            elif (
                abs(self.target_bitrate - self.codec.bit_rate) / self.codec.bit_rate
                > BITRATE_RECONFIGURE_FRACTION
            ):
                self._reset_codec("hardware-bitrate-change")

        initial_keyframe = self.codec is None
        emit_keyframe = (
            force_keyframe
            or initial_keyframe
            or scene_keyframe
        )

        frame.pict_type = (
            av.video.frame.PictureType.I
            if emit_keyframe
            else av.video.frame.PictureType.NONE
        )

        if self.codec is None:
            vbv_max_bitrate = self._next_vbv_max_bitrate or self.target_bitrate
            self._next_vbv_max_bitrate = None
            self.codec = av.CodecContext.create(self._active_backend, "w")
            self.codec.width = frame.width
            self.codec.height = frame.height
            self.codec.bit_rate = self.target_bitrate
            self.codec.pix_fmt = (
                "nv12" if self._active_backend == QSV_H264_BACKEND else "yuv420p"
            )
            codec_fps = (
                self._preferred_frame_rate(frame)
                if self._active_backend == SOFTWARE_H264_BACKEND
                else aiortc_h264.MAX_FRAME_RATE
            )
            self.codec.framerate = fractions.Fraction(codec_fps, 1)
            self.codec.time_base = fractions.Fraction(1, codec_fps)
            rate_options = {
                "bufsize": str(
                    max(
                        aiortc_h264.MIN_BITRATE // 2,
                        round(self.target_bitrate * RATE_CONTROL_BUFFER_SECONDS),
                    )
                ),
                "maxrate": str(vbv_max_bitrate),
            }
            if self._active_backend == QSV_H264_BACKEND:
                # Intel Quick Sync uses the dedicated media engine. Baseline +
                # Annex-B output matches aiortc's existing WebRTC H.264 offer.
                self.codec.options = {
                    **rate_options,
                    "async_depth": "1",
                    # QSV otherwise may encode a requested I picture without an
                    # IDR boundary, leaving damaged references usable by the
                    # decoder. PLI and guarded scene recovery need a true IDR.
                    "forced_idr": "1",
                    "level": "4.0",
                    "look_ahead": "0",
                    "preset": self.preset,
                    "profile": "baseline",
                }
            else:
                self.codec.options = {
                    **rate_options,
                    # 1080p/30 exceeds H.264 Level 3.1's frame-size limit. Level
                    # 4.0 is supported by modern WebRTC browsers and covers High.
                    "level": "40",
                    "preset": self.preset,
                    "tune": "zerolatency",
                    # PLI/FIR and the stable-scene detector own recovery frames.
                    "x264-params": "keyint=2147483647:scenecut=0",
                }
                self.codec.profile = "Baseline"

        data_to_send = b""
        codec_frame = frame
        if self._active_backend == QSV_H264_BACKEND and frame.format.name != "nv12":
            codec_frame = frame.reformat(format="nv12")
            codec_frame.pts = frame.pts
            codec_frame.time_base = frame.time_base
            codec_frame.pict_type = frame.pict_type
        for packet in self.codec.encode(codec_frame):
            data_to_send += bytes(packet)

        if data_to_send:
            if self._rate_guard_exceeded(len(data_to_send), frame_time):
                self._reset_codec("payload-budget")
                # Recursion is bounded to one retry: the rebuilt codec has
                # maxrate == target, so the preserved-envelope guard is off.
                yield from self._encode_frame(frame, force_keyframe=True)
                return
            if emit_keyframe:
                self._last_scene_keyframe_time = frame_time
                keyframe_reason = (
                    "initial" if initial_keyframe
                    else "receiver-request" if force_keyframe else "scene-change"
                )
                LOGGER.info(
                    "H.264 keyframe: reason=%s bytes=%d target=%.2f Mbps",
                    keyframe_reason, len(data_to_send), self.target_bitrate / 1_000_000,
                )
            yield from self._split_bitstream(data_to_send)


def _qsv_is_usable() -> bool:
    """Probe the local Intel media stack without changing persistent state."""

    if QSV_H264_BACKEND not in av.codec.codecs_available:
        return False
    try:
        codec = av.CodecContext.create(QSV_H264_BACKEND, "w")
        codec.width = 64
        codec.height = 64
        codec.bit_rate = aiortc_h264.MIN_BITRATE
        codec.pix_fmt = "nv12"
        codec.framerate = fractions.Fraction(20, 1)
        codec.time_base = fractions.Fraction(1, 20)
        codec.options = {
            "async_depth": "1",
            "forced_idr": "1",
            "level": "4.0",
            "look_ahead": "0",
            "preset": DEFAULT_H264_PRESET,
            "profile": "baseline",
        }
        codec.open()
        frame = av.VideoFrame(width=64, height=64, format="nv12")
        frame.pts = 0
        return bool(codec.encode(frame))
    except (OSError, RuntimeError, ValueError, av.FFmpegError):
        return False


def install_tuned_h264_encoder(
    preset: str = DEFAULT_H264_PRESET,
    backend: str = DEFAULT_H264_BACKEND,
) -> str:
    """Install the tuned encoder in aiortc and return the normalized preset."""

    normalized = preset.strip().lower()
    if normalized not in VALID_H264_PRESETS:
        allowed = ", ".join(sorted(VALID_H264_PRESETS))
        raise ValueError(f"unknown MIRROR_H264_PRESET '{preset}' (expected {allowed})")

    normalized_backend = backend.strip().lower()
    if normalized_backend not in VALID_H264_BACKENDS:
        allowed = ", ".join(sorted(VALID_H264_BACKENDS))
        raise ValueError(
            f"unknown MIRROR_H264_BACKEND '{backend}' (expected {allowed})"
        )
    selected_backend = normalized_backend
    if normalized_backend in {AUTO_H264_BACKEND, QSV_H264_BACKEND}:
        if _qsv_is_usable():
            selected_backend = QSV_H264_BACKEND
        else:
            selected_backend = SOFTWARE_H264_BACKEND
            LOGGER.warning(
                "Intel QSV H.264 unavailable; falling back to %s",
                SOFTWARE_H264_BACKEND,
            )

    TunedH264Encoder.preset = normalized
    TunedH264Encoder.backend = selected_backend
    LOGGER.info('Video recovery policy: evidence-gated-v11; no jitter-only emergency; measured recovery; no quality floor')
    LOGGER.info('Motion recovery policy: measured-motion-v1; bounded retry after real delivery growth')
    LOGGER.info('Short motion recovery policy: short-motion-v1; three-second one-shot probe opportunity')
    LOGGER.info(
        "H.264 desktop rate policy: measured-recovery-v7 "
        "report_grace=%ss recovery_step=%s%%/s vbv_growth=%sx recurring_window=%ss "
        "probe=%s%%/%ss max_extra=%s hold_after_reset=%ss",
        RECEIVER_REPORT_TIMEOUT_SECONDS, round(BITRATE_UPSHIFT_FRACTION * 100),
        VBV_RECOVERY_GROWTH_FACTOR,
        NETWORK_RECURRING_WINDOW_SECONDS,
        round(RECOVERY_PROBE_FRACTION * 100), RECOVERY_PROBE_DURATION_SECONDS,
        RECOVERY_PROBE_MAX_EXTRA_BITRATE, VBV_REBUILD_HOLD_SECONDS,
    )
    # aiortc.codecs.get_encoder resolves this module global at call time.  The
    # RTCRtpSender retains aiortc's original factory function, so replacing the
    # class here is sufficient and avoids patching sender internals.
    aiortc.codecs.H264Encoder = TunedH264Encoder
    return normalized
