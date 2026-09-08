from __future__ import annotations

import fractions
import unittest
from unittest.mock import patch

import av
import aiortc.codecs
import numpy as np

from mirror_host_agent.h264_encoder import (
    BALANCED_INITIAL_BITRATE,
    BALANCED_MAX_BITRATE,
    BITRATE_DOWNSHIFT_HOLD_SECONDS,
    DESKTOP_MAX_BITRATE,
    HIGH_INITIAL_BITRATE,
    HIGH_MAX_BITRATE,
    LOW_INITIAL_BITRATE,
    LOW_MAX_BITRATE,
    NETWORK_DEGRADATION_HOLD_SECONDS,
    QSV_H264_BACKEND,
    SOFTWARE_H264_BACKEND,
    SCENE_KEYFRAME_COOLDOWN_SECONDS,
    TunedH264Encoder,
    _qsv_is_usable,
    install_tuned_h264_encoder,
)


class TunedH264EncoderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_encoder = aiortc.codecs.H264Encoder
        TunedH264Encoder.clear_receiver_network_health()

    def tearDown(self) -> None:
        aiortc.codecs.H264Encoder = self._original_encoder
        TunedH264Encoder.preset = "veryfast"
        TunedH264Encoder.backend = SOFTWARE_H264_BACKEND
        TunedH264Encoder.clear_receiver_network_health()

    def test_installs_normalized_preset(self) -> None:
        selected = install_tuned_h264_encoder("  VERYFAST ", SOFTWARE_H264_BACKEND)

        self.assertEqual(selected, "veryfast")
        self.assertIs(aiortc.codecs.H264Encoder, TunedH264Encoder)
        self.assertEqual(TunedH264Encoder.preset, "veryfast")
        self.assertEqual(TunedH264Encoder.backend, SOFTWARE_H264_BACKEND)

    def test_rejects_unknown_preset(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown MIRROR_H264_PRESET"):
            install_tuned_h264_encoder("turbo")

    def test_selects_qsv_when_probe_succeeds(self) -> None:
        with patch(
            "mirror_host_agent.h264_encoder._qsv_is_usable",
            return_value=True,
        ):
            install_tuned_h264_encoder("fast", "auto")

        self.assertEqual(TunedH264Encoder.backend, QSV_H264_BACKEND)

    def test_falls_back_to_software_when_qsv_probe_fails(self) -> None:
        with patch(
            "mirror_host_agent.h264_encoder._qsv_is_usable",
            return_value=False,
        ):
            install_tuned_h264_encoder("fast", QSV_H264_BACKEND)

        self.assertEqual(TunedH264Encoder.backend, SOFTWARE_H264_BACKEND)

    def test_rejects_unknown_backend(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown MIRROR_H264_BACKEND"):
            install_tuned_h264_encoder("fast", "gpu-magic")

    def test_encodes_native_1080p_frame_at_level_4(self) -> None:
        encoder = TunedH264Encoder()
        frame = av.VideoFrame(width=1920, height=1080, format="yuv420p")
        frame.pts = 0
        frame.time_base = fractions.Fraction(1, 90_000)

        nal_units = list(encoder._encode_frame(frame, force_keyframe=True))
        sequence_parameter_set = next(
            payload for payload in nal_units if payload[0] & 0x1F == 7
        )

        self.assertGreater(len(nal_units), 0)
        # SPS bytes are NAL header, profile_idc, constraints, level_idc.
        self.assertEqual(sequence_parameter_set[3], 40)
        self.assertIsNotNone(encoder.codec)
        assert encoder.codec is not None
        self.assertEqual(encoder.codec.width, 1920)
        self.assertEqual(encoder.codec.height, 1080)
        self.assertEqual(encoder.codec.bit_rate, HIGH_INITIAL_BITRATE)
        self.assertEqual(encoder.codec.max_bit_rate, HIGH_INITIAL_BITRATE)

    def test_vbv_bounds_a_high_detail_1080p_keyframe_burst(self) -> None:
        encoder = TunedH264Encoder()
        random = np.random.default_rng(20260731)
        image = random.integers(
            0,
            256,
            size=(1080, 1920, 3),
            dtype=np.uint8,
        )
        frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(
            format="yuv420p"
        )
        frame.pts = 0
        frame.time_base = fractions.Fraction(1, 90_000)

        payloads = list(encoder._encode_frame(frame, force_keyframe=True))

        # Without VBV this deterministic frame is roughly 450 KB at 4 Mbps.
        # The capped burst remains well below one second of target bandwidth,
        # leaving room for control-channel heartbeats on the same TURN path.
        self.assertLess(sum(len(payload) for payload in payloads), 250_000)
        self.assertTrue(any(payload[0] & 0x1F == 5 for payload in payloads))

    def test_initial_bitrate_matches_desktop_resolution(self) -> None:
        cases = (
            (1280, 720, LOW_INITIAL_BITRATE),
            (1600, 900, BALANCED_INITIAL_BITRATE),
            (1920, 1080, HIGH_INITIAL_BITRATE),
        )
        for width, height, expected in cases:
            with self.subTest(size=(width, height)):
                frame = av.VideoFrame(width=width, height=height, format="yuv420p")
                self.assertEqual(
                    TunedH264Encoder._preferred_initial_bitrate(frame),
                    expected,
                )

    def test_receiver_bitrate_is_clamped_to_each_profile_ceiling(self) -> None:
        cases = (
            (1280, 720, LOW_MAX_BITRATE),
            (1600, 900, BALANCED_MAX_BITRATE),
            (1920, 1080, HIGH_MAX_BITRATE),
        )
        for width, height, expected_max in cases:
            with self.subTest(size=(width, height)):
                encoder = TunedH264Encoder()
                frame = av.VideoFrame(
                    width=width,
                    height=height,
                    format="yuv420p",
                )
                frame.pts = 0
                frame.time_base = fractions.Fraction(1, 90_000)
                list(encoder._encode_frame(frame, force_keyframe=False))

                encoder.target_bitrate = DESKTOP_MAX_BITRATE * 2
                self.assertEqual(encoder.target_bitrate, expected_max)

                encoder.target_bitrate = 1
                self.assertEqual(encoder.receiver_target_bitrate, 500_000)
                # A downward estimate is filtered after the first frame so a
                # static desktop is not immediately replaced by a blurry IDR.
                self.assertEqual(encoder.target_bitrate, expected_max)

    def test_pre_frame_receiver_bitrate_is_reclamped_to_profile(self) -> None:
        encoder = TunedH264Encoder()
        encoder.target_bitrate = DESKTOP_MAX_BITRATE
        frame = av.VideoFrame(width=1280, height=720, format="yuv420p")
        frame.pts = 0
        frame.time_base = fractions.Fraction(1, 90_000)

        list(encoder._encode_frame(frame, force_keyframe=False))

        self.assertEqual(encoder.target_bitrate, LOW_MAX_BITRATE)
        assert encoder.codec is not None
        self.assertEqual(encoder.codec.bit_rate, LOW_MAX_BITRATE)

    def test_low_remb_keeps_selected_profile_resolution(self) -> None:
        encoder = TunedH264Encoder()
        encoder.target_bitrate = 500_000

        frame = av.VideoFrame(width=1920, height=1080, format="yuv420p")
        frame.pts = 0
        frame.time_base = fractions.Fraction(1, 90_000)
        list(encoder._encode_frame(frame, force_keyframe=False))

        assert encoder.codec is not None
        self.assertEqual((encoder.codec.width, encoder.codec.height), (1920, 1080))

    def test_static_desktop_defers_lower_remb_without_periodic_idr(self) -> None:
        encoder = TunedH264Encoder()

        def encode_at(frame_number: int) -> list[bytes]:
            frame = av.VideoFrame(width=64, height=64, format="yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts = frame_number * 4_500
            frame.time_base = fractions.Fraction(1, 90_000)
            return list(encoder._encode_frame(frame, force_keyframe=False))

        first = encode_at(0)
        encoder.target_bitrate = 500_000
        later = [encode_at(frame_number) for frame_number in range(1, 81)]

        self.assertTrue(any(payload[0] & 0x1F == 5 for payload in first))
        self.assertFalse(
            any(
                payload[0] & 0x1F == 5
                for encoded_frame in later
                for payload in encoded_frame
            )
        )
        self.assertEqual(encoder.receiver_target_bitrate, 500_000)
        self.assertEqual(encoder.target_bitrate, LOW_INITIAL_BITRATE)

    def test_sustained_lower_remb_applies_when_content_is_moving(self) -> None:
        encoder = TunedH264Encoder()
        random = np.random.default_rng(20260806)

        def encode_at(frame_number: int) -> list[bytes]:
            image = random.integers(0, 256, size=(64, 64, 3), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(
                format="yuv420p"
            )
            frame.pts = frame_number * 4_500
            frame.time_base = fractions.Fraction(1, 90_000)
            return list(encoder._encode_frame(frame, force_keyframe=False))

        encode_at(0)
        encoder.target_bitrate = 500_000
        frame_count = round(BITRATE_DOWNSHIFT_HOLD_SECONDS / 0.05) + 3
        encoded = [encode_at(frame_number) for frame_number in range(1, frame_count)]

        self.assertEqual(encoder.target_bitrate, 500_000)
        self.assertTrue(
            any(
                payload[0] & 0x1F == 5
                for encoded_frame in encoded
                for payload in encoded_frame
            )
        )

    def test_fresh_healthy_receiver_report_blocks_a_lower_remb(self) -> None:
        encoder = TunedH264Encoder()
        encoder._initial_bitrate_applied = True
        encoder._target_bitrate = LOW_INITIAL_BITRATE
        encoder._pending_lower_bitrate = 500_000
        encoder._pending_lower_since = 0.0
        TunedH264Encoder.update_receiver_network_health(
            jitter_ms=8.0,
            packet_loss_percent=0.0,
            route="direct",
            now=100.0,
        )

        with patch(
            "mirror_host_agent.h264_encoder.time.monotonic", return_value=103.0
        ):
            encoder._apply_pending_lower_bitrate(3.0, content_stable=False)

        self.assertEqual(encoder.target_bitrate, LOW_INITIAL_BITRATE)

    def test_sustained_unhealthy_receiver_report_allows_a_lower_remb(self) -> None:
        encoder = TunedH264Encoder()
        encoder._initial_bitrate_applied = True
        encoder._target_bitrate = LOW_INITIAL_BITRATE
        encoder._pending_lower_bitrate = 500_000
        encoder._pending_lower_since = 0.0
        TunedH264Encoder.update_receiver_network_health(
            jitter_ms=60.0,
            packet_loss_percent=2.5,
            route="turn",
            now=100.0,
        )

        checked_at = 100.0 + NETWORK_DEGRADATION_HOLD_SECONDS + 0.1
        with patch(
            "mirror_host_agent.h264_encoder.time.monotonic",
            return_value=checked_at,
        ):
            encoder._apply_pending_lower_bitrate(3.0, content_stable=False)

        self.assertEqual(encoder.target_bitrate, 500_000)

    def test_receiver_recovery_request_still_forces_keyframe(self) -> None:
        encoder = TunedH264Encoder()

        def encode_at(frame_number: int, force_keyframe: bool) -> list[bytes]:
            frame = av.VideoFrame(width=64, height=64, format="yuv420p")
            frame.pts = frame_number * 4_500
            frame.time_base = fractions.Fraction(1, 90_000)
            return list(
                encoder._encode_frame(
                    frame,
                    force_keyframe=force_keyframe,
                )
            )

        encode_at(0, False)
        recovery = encode_at(1, True)

        self.assertTrue(any(payload[0] & 0x1F == 5 for payload in recovery))

    def test_qsv_recovery_request_emits_an_idr_when_available(self) -> None:
        if not _qsv_is_usable():
            self.skipTest("Intel QSV is unavailable on this host")
        TunedH264Encoder.backend = QSV_H264_BACKEND
        encoder = TunedH264Encoder()

        def encode_at(frame_number: int, force_keyframe: bool) -> list[bytes]:
            frame = av.VideoFrame(width=64, height=64, format="yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts = frame_number * 4_500
            frame.time_base = fractions.Fraction(1, 90_000)
            return list(encoder._encode_frame(frame, force_keyframe=force_keyframe))

        encode_at(0, False)
        recovery = encode_at(1, True)

        self.assertTrue(any(payload[0] & 0x1F == 5 for payload in recovery))

    def test_large_scene_change_forces_immediate_keyframe(self) -> None:
        encoder = TunedH264Encoder()

        def encode_image(value: int, seconds: float) -> list[bytes]:
            image = np.full((64, 64, 3), value, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(
                format="yuv420p"
            )
            frame.pts = round(seconds * 90_000)
            frame.time_base = fractions.Fraction(1, 90_000)
            return list(encoder._encode_frame(frame, force_keyframe=False))

        first = encode_image(0, 0)
        # Arm desktop scene detection with a short quiet run. Continuous motion
        # must not be treated like a window switch.
        for frame_number in range(1, 5):
            encode_image(0, frame_number * 0.05)
        scene_change = encode_image(
            255,
            SCENE_KEYFRAME_COOLDOWN_SECONDS + 0.1,
        )

        self.assertTrue(any(payload[0] & 0x1F == 5 for payload in first))
        self.assertTrue(any(payload[0] & 0x1F == 5 for payload in scene_change))

    def test_continuous_full_frame_motion_does_not_repeat_keyframes(self) -> None:
        encoder = TunedH264Encoder()
        random = np.random.default_rng(20260806)
        idr_frames: list[int] = []

        for frame_number in range(100):
            image = random.integers(0, 256, size=(64, 64, 3), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(
                format="yuv420p"
            )
            frame.pts = frame_number * 4_500
            frame.time_base = fractions.Fraction(1, 90_000)
            payloads = list(encoder._encode_frame(frame, force_keyframe=False))
            if any(payload[0] & 0x1F == 5 for payload in payloads):
                idr_frames.append(frame_number)

        self.assertEqual(idr_frames, [0])

    def test_small_local_change_does_not_force_keyframe(self) -> None:
        encoder = TunedH264Encoder()

        def encode_image(image: np.ndarray, seconds: float) -> list[bytes]:
            frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(
                format="yuv420p"
            )
            frame.pts = round(seconds * 90_000)
            frame.time_base = fractions.Fraction(1, 90_000)
            return list(encoder._encode_frame(frame, force_keyframe=False))

        base = np.zeros((64, 64, 3), dtype=np.uint8)
        local_change = base.copy()
        local_change[:8, :8, :] = 255

        encode_image(base, 0)
        encoded = encode_image(local_change, 1)

        self.assertFalse(any(payload[0] & 0x1F == 5 for payload in encoded))

    def test_scene_detection_failure_does_not_block_encoding(self) -> None:
        encoder = TunedH264Encoder()
        frame = av.VideoFrame(width=64, height=64, format="rgb24")
        frame.pts = 0
        frame.time_base = fractions.Fraction(1, 90_000)

        encoded = list(encoder._encode_frame(frame, force_keyframe=False))

        self.assertGreater(len(encoded), 0)


if __name__ == "__main__":
    unittest.main()
