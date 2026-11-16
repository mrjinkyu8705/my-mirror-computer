from __future__ import annotations

import asyncio
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from mirror_host_agent.video import (
    PROFILE_BALANCED,
    PROFILE_LOW,
    DesktopDuplicationTrack,
    SyntheticVideoTrack,
    compute_letterbox_fit,
    create_video_track,
    desktop_video_frame,
    extract_primary_video_codec,
    get_profile,
    letterbox_rgb,
)


class LetterboxFitTests(unittest.TestCase):
    def test_same_aspect_fills_destination(self) -> None:
        fit = compute_letterbox_fit(1280, 720, 1280, 720)
        self.assertEqual((fit.width, fit.height), (1280, 720))
        self.assertEqual((fit.offset_x, fit.offset_y), (0, 0))

    def test_larger_16_9_source_scales_to_full_frame(self) -> None:
        fit = compute_letterbox_fit(2560, 1440, 1280, 720)
        self.assertEqual((fit.width, fit.height), (1280, 720))
        self.assertEqual((fit.offset_x, fit.offset_y), (0, 0))

    def test_16_10_source_pillarboxes(self) -> None:
        fit = compute_letterbox_fit(1920, 1200, 1280, 720)
        self.assertEqual((fit.width, fit.height), (1152, 720))
        self.assertEqual((fit.offset_x, fit.offset_y), (64, 0))

    def test_4_3_source_pillarboxes(self) -> None:
        fit = compute_letterbox_fit(1024, 768, 1280, 720)
        self.assertEqual((fit.width, fit.height), (960, 720))
        self.assertEqual((fit.offset_x, fit.offset_y), (160, 0))

    def test_portrait_source_is_centered(self) -> None:
        fit = compute_letterbox_fit(720, 1280, 1280, 720)
        self.assertEqual((fit.width, fit.height), (405, 720))
        self.assertEqual(fit.offset_y, 0)
        # Symmetric horizontal centering.
        self.assertEqual(fit.offset_x, (1280 - 405) // 2)

    def test_box_always_within_destination(self) -> None:
        fit = compute_letterbox_fit(3000, 1000, 1280, 720)
        self.assertLessEqual(fit.offset_x + fit.width, 1280)
        self.assertLessEqual(fit.offset_y + fit.height, 720)

    def test_16_9_source_fills_16_9_high_profile_with_no_dead_zones(self) -> None:
        # The native 1920x1080 desktop must fill the High profile exactly --
        # no scaling, pillarbox bars, or inactive pointer regions.
        fit = compute_letterbox_fit(1920, 1080, 1920, 1080)
        self.assertEqual((fit.width, fit.height), (1920, 1080))
        self.assertEqual((fit.offset_x, fit.offset_y), (0, 0))

    def test_rejects_non_positive_dimensions(self) -> None:
        with self.assertRaises(ValueError):
            compute_letterbox_fit(0, 100, 1280, 720)
        with self.assertRaises(ValueError):
            compute_letterbox_fit(100, 100, 0, 720)


class LetterboxRgbTests(unittest.TestCase):
    def test_output_shape_and_letterbox_padding(self) -> None:
        # 100x50 source (2:1) into 1280x720 -> full width, 640 tall, 40px top/bottom bars.
        source = np.zeros((50, 100, 3), dtype=np.uint8)
        source[:, :, 0] = 200  # red

        out = letterbox_rgb(source, 1280, 720)

        self.assertEqual(out.shape, (720, 1280, 3))
        # Top bar is black letterbox.
        self.assertTrue(np.array_equal(out[0, 0], np.zeros(3, dtype=np.uint8)))
        self.assertTrue(np.array_equal(out[719, 0], np.zeros(3, dtype=np.uint8)))
        # Center is the (red) source content.
        center = out[360, 640]
        self.assertGreater(int(center[0]), 100)
        self.assertLess(int(center[1]), 60)
        self.assertLess(int(center[2]), 60)

    def test_rejects_non_rgb_input(self) -> None:
        with self.assertRaises(ValueError):
            letterbox_rgb(np.zeros((10, 10), dtype=np.uint8))


class DesktopVideoFrameTests(unittest.TestCase):
    def test_all_deployed_profiles_use_encoder_ready_yuv_for_16_9_bgra(self) -> None:
        source = np.zeros((108, 192, 4), dtype=np.uint8)

        for name in ("low", "balanced", "high"):
            with self.subTest(profile=name):
                profile = get_profile(name)
                frame = desktop_video_frame(
                    source,
                    profile.width,
                    profile.height,
                    frame_index=0,
                    fps=profile.fps,
                )
                self.assertEqual(
                    (frame.width, frame.height), (profile.width, profile.height)
                )
                self.assertEqual(frame.format.name, "yuv420p")

    def test_matching_aspect_uses_encoder_ready_yuv_frame(self) -> None:
        source = np.zeros((108, 192, 3), dtype=np.uint8)

        frame = desktop_video_frame(source, 160, 90, frame_index=2, fps=20)

        self.assertEqual((frame.width, frame.height), (160, 90))
        self.assertEqual(frame.format.name, "yuv420p")
        self.assertEqual(frame.pts, 9000)

    def test_native_bgra_uses_encoder_ready_yuv_frame(self) -> None:
        source = np.zeros((108, 192, 4), dtype=np.uint8)

        frame = desktop_video_frame(source, 160, 90, frame_index=0, fps=20)

        self.assertEqual((frame.width, frame.height), (160, 90))
        self.assertEqual(frame.format.name, "yuv420p")

    def test_mismatched_aspect_retains_rgb_letterbox_path(self) -> None:
        source = np.zeros((100, 100, 3), dtype=np.uint8)

        frame = desktop_video_frame(source, 160, 90, frame_index=1, fps=10)

        self.assertEqual((frame.width, frame.height), (160, 90))
        self.assertEqual(frame.format.name, "rgb24")


class VideoProfileTests(unittest.TestCase):
    def test_known_profiles_resolve(self) -> None:
        low = get_profile("low")
        self.assertEqual((low.width, low.height, low.fps), (1280, 720, 10))
        balanced = get_profile(" BALANCED ")
        self.assertEqual(
            (balanced.width, balanced.height, balanced.fps),
            (1600, 900, 15),
        )

    def test_unknown_profile_raises(self) -> None:
        with self.assertRaises(ValueError):
            get_profile("ultra")

    def test_high_profile_is_native_1080p_16_9(self) -> None:
        # Native 1080p keeps desktop text intact and has no pointer dead zones.
        high = get_profile("high")
        self.assertEqual((high.width, high.height, high.fps), (1920, 1080, 20))
        self.assertEqual(high.name, "high")
        # The factory must accept the new profile like any other.
        track = create_video_track("desktop", high)
        self.assertIsInstance(track, DesktopDuplicationTrack)

    def test_synthetic_track_honors_profile_size(self) -> None:
        track = SyntheticVideoTrack(PROFILE_LOW)
        frame = asyncio.run(track.recv())
        self.assertEqual((frame.width, frame.height), (1280, 720))

    def test_desktop_black_fallback_matches_profile(self) -> None:
        # Force the "no frame yet" path (no capture hardware touched): recv must
        # emit a profile-sized black frame rather than raise or leak a camera.
        track = create_video_track("desktop", PROFILE_LOW)
        track._grab_rgb = lambda: None  # type: ignore[method-assign]
        frame = asyncio.run(track.recv())
        self.assertEqual((frame.width, frame.height), (1280, 720))

    def test_desktop_capture_and_conversion_do_not_block_event_loop(self) -> None:
        track = create_video_track("desktop", PROFILE_LOW)

        def slow_frame() -> np.ndarray:
            time.sleep(0.05)
            return np.zeros((72, 128, 4), dtype=np.uint8)

        track._grab_rgb = slow_frame  # type: ignore[method-assign]

        async def exercise() -> None:
            pending = asyncio.create_task(track.recv())
            await asyncio.sleep(0.01)
            self.assertFalse(pending.done())
            frame = await pending
            self.assertEqual(frame.format.name, "yuv420p")

        asyncio.run(exercise())

    def test_desktop_timestamps_follow_actual_frame_production_time(self) -> None:
        track = create_video_track("desktop", PROFILE_LOW)
        track._grab_rgb = lambda: np.zeros(  # type: ignore[method-assign]
            (72, 128, 4), dtype=np.uint8
        )
        timestamps = iter((100.0, 100.08))
        track._clock = lambda: next(timestamps)  # type: ignore[attr-defined]

        async def exercise() -> tuple[int | None, int | None]:
            first = await track.recv()
            second = await track.recv()
            return first.pts, second.pts

        first_pts, second_pts = asyncio.run(exercise())

        self.assertEqual(first_pts, 0)
        self.assertEqual(second_pts, 7_200)


class ExtractCodecTests(unittest.TestCase):
    _SDP = (
        "v=0\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        "a=rtpmap:111 opus/48000/2\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 102 96\r\n"
        "a=rtpmap:96 VP8/90000\r\n"
        "a=rtpmap:102 H264/90000\r\n"
    )

    def test_returns_first_video_payload_codec(self) -> None:
        # First video payload is 102 -> H264, even though VP8 rtpmap appears first.
        self.assertEqual(extract_primary_video_codec(self._SDP), "H264")

    def test_returns_none_without_video(self) -> None:
        self.assertIsNone(
            extract_primary_video_codec("v=0\r\nm=audio 9 RTP 111\r\n")
        )


class CreateVideoTrackTests(unittest.TestCase):
    def test_synthetic_is_default(self) -> None:
        self.assertIsInstance(create_video_track(), SyntheticVideoTrack)
        self.assertIsInstance(create_video_track("synthetic"), SyntheticVideoTrack)
        self.assertIsInstance(create_video_track(" SYNTHETIC "), SyntheticVideoTrack)

    def test_desktop_source_builds_capture_track_without_starting(self) -> None:
        # __init__ must not touch dxcam/hardware; capture starts lazily on recv.
        track = create_video_track("desktop")
        self.assertIsInstance(track, DesktopDuplicationTrack)
        self.assertEqual(track.restart_count, 0)

    def test_capture_fault_recovers_once_then_backs_off(self) -> None:
        # A capture fault must recover exactly once, then hold off (not rebuild
        # every frame) until the retry window elapses — the black-screen loop was
        # an endless per-frame rebuild.
        track = create_video_track("desktop")

        def boom() -> None:
            raise RuntimeError("simulated capture fault")

        track._ensure_camera = boom  # type: ignore[method-assign]

        track._grab_gdi_fallback = lambda: None  # type: ignore[method-assign]

        self.assertIsNone(track._grab_rgb())  # first attempt faults + recovers
        self.assertEqual(track.restart_count, 1)
        self.assertIsNone(track._grab_rgb())  # within backoff: no rebuild attempt
        self.assertEqual(track.restart_count, 1)

        track._recover_not_before = 0.0  # simulate the retry window elapsing
        self.assertIsNone(track._grab_rgb())
        self.assertEqual(track.restart_count, 2)

    def test_capture_fault_uses_gdi_frame_instead_of_black(self) -> None:
        track = create_video_track("desktop")
        fallback = np.full((20, 30, 3), 127, dtype=np.uint8)

        def boom() -> None:
            raise RuntimeError("simulated capture fault")

        track._ensure_camera = boom  # type: ignore[method-assign]
        track._grab_gdi_fallback = lambda: fallback  # type: ignore[method-assign]

        self.assertIs(track._grab_rgb(), fallback)
        self.assertEqual(track.restart_count, 1)

    def test_dxgi_recovery_leaves_gdi_fallback(self) -> None:
        track = create_video_track("desktop")
        dxgi_frame = np.full((20, 30, 4), 200, dtype=np.uint8)

        class Camera:
            def get_latest_frame(self, *, copy: bool = True) -> np.ndarray:
                self.copy = copy
                return dxgi_frame

        camera = Camera()
        track._camera = camera
        track._gdi_fallback_active = True

        self.assertIs(track._grab_rgb(), dxgi_frame)
        self.assertFalse(camera.copy)
        self.assertFalse(track._gdi_fallback_active)

    def test_recovery_releases_camera_so_next_track_gets_fresh_dxgi(self) -> None:
        track = create_video_track("desktop")

        class Camera:
            def __init__(self) -> None:
                self.stop_calls = 0
                self.release_calls = 0

            def stop(self) -> None:
                self.stop_calls += 1

            def release(self) -> None:
                self.release_calls += 1

        camera = Camera()
        track._camera = camera

        track._recover(RuntimeError("simulated capture fault"))

        self.assertIsNone(track._camera)
        self.assertEqual(camera.stop_calls, 1)
        self.assertEqual(camera.release_calls, 1)

    def test_start_failure_releases_cached_camera_instead_of_retrying_it(self) -> None:
        track = create_video_track("desktop")

        class CachedCamera:
            def __init__(self) -> None:
                self.release_calls = 0
                self.stop_calls = 0

            def start(self, **_kwargs: object) -> None:
                raise RuntimeError("camera is already running")

            def stop(self) -> None:
                self.stop_calls += 1

            def release(self) -> None:
                self.release_calls += 1

        camera = CachedCamera()
        fake_dxcam = SimpleNamespace(create=Mock(return_value=camera))
        track._grab_gdi_fallback = lambda: None  # type: ignore[method-assign]

        with patch.dict(sys.modules, {"dxcam": fake_dxcam}):
            self.assertIsNone(track._grab_rgb())

        self.assertIsNone(track._camera)
        self.assertEqual(camera.stop_calls, 1)
        self.assertEqual(camera.release_calls, 1)

    def test_stopped_track_cannot_recreate_camera_from_queued_capture(self) -> None:
        track = create_video_track("desktop")
        ensure_camera = Mock(side_effect=AssertionError("must not recreate camera"))
        track._ensure_camera = ensure_camera  # type: ignore[method-assign]

        track.stop()

        self.assertIsNone(track._grab_rgb())
        ensure_camera.assert_not_called()

    def test_unknown_source_raises(self) -> None:
        with self.assertRaises(ValueError):
            create_video_track("webcam")


if __name__ == "__main__":
    unittest.main()
