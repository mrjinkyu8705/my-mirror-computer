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
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

import av
import aiortc.codecs
import numpy as np
from aiortc.codecs import h264 as aiortc_h264

LOGGER = logging.getLogger("mirror_host_agent.h264_encoder")

DEFAULT_H264_PRESET = "fast"
SOFTWARE_H264_BACKEND = "libx264"
QSV_H264_BACKEND = "h264_qsv"
AUTO_H264_BACKEND = "auto"
DEFAULT_H264_BACKEND = SOFTWARE_H264_BACKEND
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
# A receiver estimate commonly falls while a desktop is static because there is
# very little residual video to measure. Reopening the codec immediately at that
# lower estimate replaces a crisp reference with a low-bitrate IDR even though
# the network itself may be healthy. Hold a sustained decrease and apply it only
# once content is moving again; increases and explicit PLI/FIR recovery remain
# immediate.
BITRATE_DOWNSHIFT_HOLD_SECONDS = 2.0
BITRATE_RECONFIGURE_FRACTION = 0.25
# A fresh viewer report distinguishes actual network congestion from a low REMB
# estimate caused by a static desktop. Require sustained loss or jitter before
# applying a lower estimate. If an older viewer sends no reports, preserve the
# previous motion-gated behavior for protocol compatibility.
RECEIVER_REPORT_MAX_AGE_SECONDS = 5.0
NETWORK_DEGRADATION_HOLD_SECONDS = 2.0
NETWORK_PACKET_LOSS_THRESHOLD_PERCENT = 2.0
NETWORK_JITTER_THRESHOLD_MS = 50.0
# ``bit_rate`` controls the long-term x264 average but does not bound an
# individual IDR frame. A complex 1080p desktop transition can otherwise emit
# close to a megabyte at once, filling a TURN queue and delaying both RTP video
# and SCTP control heartbeats. A half-second VBV absorbs normal variation while
# bounding that burst without lowering the negotiated steady-state bitrate.
RATE_CONTROL_BUFFER_SECONDS = 0.5
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


class TunedH264Encoder(aiortc_h264.H264Encoder):
    """aiortc H.264 encoder with an explicit x264 speed preset."""

    preset = DEFAULT_H264_PRESET
    backend = SOFTWARE_H264_BACKEND
    _receiver_network_report: ClassVar[ReceiverNetworkReport | None] = None
    _receiver_network_unhealthy_since: ClassVar[float | None] = None

    @classmethod
    def clear_receiver_network_health(cls) -> None:
        cls._receiver_network_report = None
        cls._receiver_network_unhealthy_since = None

    @classmethod
    def update_receiver_network_health(
        cls,
        *,
        jitter_ms: float | None,
        packet_loss_percent: float | None,
        route: str,
        now: float | None = None,
    ) -> None:
        received_at = time.monotonic() if now is None else now
        unhealthy = (
            packet_loss_percent is not None
            and packet_loss_percent >= NETWORK_PACKET_LOSS_THRESHOLD_PERCENT
        ) or (
            jitter_ms is not None
            and jitter_ms >= NETWORK_JITTER_THRESHOLD_MS
        )
        previous = cls._receiver_network_report
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
        )

    @classmethod
    def _network_allows_bitrate_downshift(cls, now: float | None = None) -> bool:
        checked_at = time.monotonic() if now is None else now
        report = cls._receiver_network_report
        if (
            report is None
            or checked_at - report.received_at > RECEIVER_REPORT_MAX_AGE_SECONDS
        ):
            # Backward compatibility for viewers that predate receiver reports.
            return True
        unhealthy_since = cls._receiver_network_unhealthy_since
        return (
            report.unhealthy
            and unhealthy_since is not None
            and checked_at - unhealthy_since >= NETWORK_DEGRADATION_HOLD_SECONDS
        )

    def __init__(self) -> None:
        self._profile_max_bitrate = DESKTOP_MAX_BITRATE
        super().__init__()
        # H264Encoder stores its bitrate in a private attribute. Keep a local
        # property so we can raise only the upper bound without patching aiortc
        # module constants or disabling receiver-driven congestion control.
        self._target_bitrate = aiortc_h264.DEFAULT_BITRATE
        self._receiver_target_bitrate = aiortc_h264.DEFAULT_BITRATE
        self._pending_lower_bitrate: int | None = None
        self._pending_lower_since: float | None = None
        self._initial_bitrate_applied = False
        self._last_scene_keyframe_time: float | None = None
        self._previous_luma_sample: np.ndarray | None = None
        self._stable_frame_count = 0
        self._active_backend = self.backend

    @property
    def target_bitrate(self) -> int:
        """Receiver-controlled target bitrate with a desktop-video ceiling."""

        return self._target_bitrate

    @target_bitrate.setter
    def target_bitrate(self, bitrate: int) -> None:
        clamped = max(
            aiortc_h264.MIN_BITRATE,
            min(int(bitrate), self._profile_max_bitrate),
        )
        previous_receiver = self._receiver_target_bitrate
        self._receiver_target_bitrate = clamped

        if not self._initial_bitrate_applied or clamped >= self._target_bitrate:
            self._target_bitrate = clamped
            self._pending_lower_bitrate = None
            self._pending_lower_since = None
        else:
            # Keep the first observation time while later REMB reports refine
            # the pending value. A noisy estimate must not restart the hold on
            # every report and therefore postpone a real congestion response
            # forever.
            self._pending_lower_bitrate = clamped

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
        """Latest raw REMB estimate before stability filtering."""

        return self._receiver_target_bitrate

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

        if (
            frame_time is not None
            and self._last_scene_keyframe_time is not None
            and frame_time - self._last_scene_keyframe_time
            < SCENE_KEYFRAME_COOLDOWN_SECONDS
        ):
            return False

        return (
            had_stable_context
            and changed_fraction >= SCENE_CHANGED_FRACTION
            and mean_delta >= SCENE_MEAN_DELTA
        )

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
        if self._pending_lower_since is None or frame_time < self._pending_lower_since:
            self._pending_lower_since = frame_time
            return
        if (
            content_stable
            or frame_time - self._pending_lower_since
            < BITRATE_DOWNSHIFT_HOLD_SECONDS
            or not self._network_allows_bitrate_downshift()
        ):
            return

        previous = self._target_bitrate
        self._target_bitrate = pending
        self._pending_lower_bitrate = None
        self._pending_lower_since = None
        LOGGER.info(
            "H.264 applied sustained bitrate decrease: %.2f -> %.2f Mbps",
            previous / 1_000_000,
            self._target_bitrate / 1_000_000,
        )

    def _encode_frame(
        self, frame: av.VideoFrame, force_keyframe: bool
    ) -> Iterator[bytes]:
        # RTCP feedback cannot exist before the encoder's first frame. Give each
        # desktop resolution a readable starting point, then leave all later
        # target changes to the receiver's REMB congestion estimate.
        if not self._initial_bitrate_applied:
            self._profile_max_bitrate = self._preferred_max_bitrate(frame)
            if self.target_bitrate == aiortc_h264.DEFAULT_BITRATE:
                self.target_bitrate = self._preferred_initial_bitrate(frame)
            else:
                # REMB can arrive before the first encoded frame. Re-apply the
                # selected mode's ceiling before creating the codec.
                self.target_bitrate = self.target_bitrate
            self._initial_bitrate_applied = True

        frame_time = self._frame_time(frame)
        scene_keyframe = self._is_large_scene_change(frame, frame_time)
        self._apply_pending_lower_bitrate(
            frame_time,
            content_stable=(
                self._stable_frame_count >= SCENE_STABLE_FRAMES_REQUIRED
            ),
        )
        # This follows aiortc 1.15's H264Encoder implementation. Packetisation
        # and receiver-controlled target bitrate remain compatible with aiortc.
        if self.codec and (
            frame.width != self.codec.width
            or frame.height != self.codec.height
            or abs(self.target_bitrate - self.codec.bit_rate) / self.codec.bit_rate
            > BITRATE_RECONFIGURE_FRACTION
        ):
            self.buffer_data = b""
            self.buffer_pts = None
            self.codec = None

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
            self.codec = av.CodecContext.create(self._active_backend, "w")
            self.codec.width = frame.width
            self.codec.height = frame.height
            self.codec.bit_rate = self.target_bitrate
            self.codec.pix_fmt = (
                "nv12" if self._active_backend == QSV_H264_BACKEND else "yuv420p"
            )
            self.codec.framerate = fractions.Fraction(aiortc_h264.MAX_FRAME_RATE, 1)
            self.codec.time_base = fractions.Fraction(1, aiortc_h264.MAX_FRAME_RATE)
            rate_options = {
                "bufsize": str(
                    max(
                        aiortc_h264.MIN_BITRATE // 2,
                        round(self.target_bitrate * RATE_CONTROL_BUFFER_SECONDS),
                    )
                ),
                "maxrate": str(self.target_bitrate),
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
            if emit_keyframe:
                self._last_scene_keyframe_time = frame_time
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
    # aiortc.codecs.get_encoder resolves this module global at call time.  The
    # RTCRtpSender retains aiortc's original factory function, so replacing the
    # class here is sufficient and avoids patching sender internals.
    aiortc.codecs.H264Encoder = TunedH264Encoder
    return normalized
