from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mirror_host_agent.__main__ import (
    CONTROL_GRANT_TTL_MS,
    AgentConfig,
    M0Agent,
    SyntheticVideoTrack,
)
from mirror_host_agent.h264_encoder import TunedH264Encoder
from mirror_host_agent.input_control import FakeInputSink, InputController


class SyntheticVideoTrackTests(unittest.TestCase):
    def test_creates_a_balanced_900p_frame(self) -> None:
        track = SyntheticVideoTrack()

        frame = asyncio.run(track.recv())

        self.assertEqual(frame.width, 1600)
        self.assertEqual(frame.height, 900)
        self.assertEqual(frame.time_base.numerator, 1)
        self.assertEqual(frame.time_base.denominator, 90_000)


class ControlProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used-in-unit-test",
                ws_url="ws://127.0.0.1:8787/ws",
            )
        )

    def test_agent_defaults_to_the_balanced_video_profile(self) -> None:
        self.assertEqual(self.agent._video_profile.name, "balanced")

    def test_echoes_a_strict_ping_as_pong(self) -> None:
        pong = self.agent._create_pong(
            '{"data":{},"event":"session.ping","sequence":7,'
            '"sessionId":"session_0123456789abcdef",'
            '"timestamp":1234,"version":1}'
        )

        self.assertEqual(pong["event"] if pong else None, "session.pong")
        self.assertEqual(pong["timestamp"] if pong else None, 1234)

    def test_rejects_command_shaped_or_wrong_session_messages(self) -> None:
        command = self.agent._create_pong(
            '{"command":"powershell","data":{},"event":"session.ping",'
            '"sequence":7,"sessionId":"session_0123456789abcdef",'
            '"timestamp":1234,"version":1}'
        )
        wrong_session = self.agent._create_pong(
            '{"data":{},"event":"session.ping","sequence":7,'
            '"sessionId":"session_wrong_0123456789",'
            '"timestamp":1234,"version":1}'
        )

        self.assertIsNone(command)
        self.assertIsNone(wrong_session)

    def test_rejects_stale_future_and_boolean_control_metadata(self) -> None:
        def control(timestamp: object, sequence: object = 8) -> str:
            return json.dumps(
                {
                    "data": {"code": "KeyA"},
                    "event": "key.down",
                    "sequence": sequence,
                    "sessionId": "session_0123456789abcdef",
                    "timestamp": timestamp,
                    "version": 1,
                }
            )

        with patch("mirror_host_agent.__main__.time.time", return_value=1000.0):
            self.assertIsNotNone(self.agent._parse_control(control(1_000_000)))
            self.assertIsNone(self.agent._parse_control(control(900_000)))
            self.assertIsNone(self.agent._parse_control(control(1_100_000)))
            self.assertIsNone(self.agent._parse_control(control(1_000_000, True)))
            self.assertIsNone(self.agent._parse_control(control(True)))

    def test_accepts_viewer_network_health_without_control_permission(self) -> None:
        message = json.dumps(
            {
                "data": {
                    "jitterMs": 55.0,
                    "packetLossPercent": 2.5,
                    "receivedBitrateMbps": 1.2,
                    "route": "turn",
                },
                "event": "video.receiver-report",
                "sequence": 8,
                "sessionId": "session_0123456789abcdef",
                "timestamp": 1_000_000,
                "version": 1,
            }
        )

        with (
            patch("mirror_host_agent.__main__.time.time", return_value=1000.0),
            patch.object(
                TunedH264Encoder,
                "update_receiver_network_health",
            ) as update_health,
        ):
            self.agent._on_control_message(_FakeControlChannel(), message)

        update_health.assert_called_once_with(
            jitter_ms=55.0,
            packet_loss_percent=2.5,
            route="turn",
        )

    def test_rejects_invalid_viewer_network_health(self) -> None:
        message = json.dumps(
            {
                "data": {
                    "jitterMs": 10,
                    "packetLossPercent": True,
                    "receivedBitrateMbps": 1.2,
                    "route": "direct",
                },
                "event": "video.receiver-report",
                "sequence": 8,
                "sessionId": "session_0123456789abcdef",
                "timestamp": 1_000_000,
                "version": 1,
            }
        )

        with (
            patch("mirror_host_agent.__main__.time.time", return_value=1000.0),
            patch.object(
                TunedH264Encoder,
                "update_receiver_network_health",
            ) as update_health,
        ):
            self.agent._on_control_message(_FakeControlChannel(), message)

        update_health.assert_not_called()

    def test_control_input_still_runs_without_optional_source_size(self) -> None:
        sink = FakeInputSink()
        self.agent._granted_control = True
        self.agent._control_expires_at_ms = 2_000_000
        self.agent._input_controller = InputController(
            sink, frame_width=1280, frame_height=720
        )
        self.agent._video_track = None
        message = json.dumps(
            {
                "data": {"code": "KeyA"},
                "event": "key.down",
                "sequence": 9,
                "sessionId": "session_0123456789abcdef",
                "timestamp": 1_000_000,
                "version": 1,
            }
        )

        with (
            patch("mirror_host_agent.__main__.time.time", return_value=1000.0),
            patch("mirror_host_agent.__main__.time.monotonic", return_value=10.0),
        ):
            self.agent._on_control_message(_FakeControlChannel(), message)

        self.assertEqual(sink.calls, [("key", "KeyA", "down")])


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


class _FakeSender:
    def __init__(self) -> None:
        self.tracks: list[object] = []
        self.keyframe_requests = 0

    def replaceTrack(self, track: object) -> None:
        self.tracks.append(track)

    def _send_keyframe(self) -> None:
        self.keyframe_requests += 1


class ControlGrantTests(unittest.IsolatedAsyncioTestCase):
    async def _request_permission(
        self, *, control_enabled: bool, permission: str
    ) -> tuple[M0Agent, dict]:
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                control_enabled=control_enabled,
            )
        )
        websocket = _FakeWebSocket()
        request = json.dumps(
            {
                "payload": {
                    "deviceId": "device_0123456789abcdef",
                    "permission": permission,
                },
                "sequence": 1,
                "sessionId": "session_0123456789abcdef",
                "type": "session.request",
                "version": 1,
            }
        )
        await agent._handle_message(websocket, request)
        accept = next(m for m in websocket.sent if m["type"] == "session.accept")
        return agent, accept

    async def test_view_request_grants_view(self) -> None:
        agent, accept = await self._request_permission(
            control_enabled=True, permission="view"
        )
        self.assertEqual(accept["payload"]["permission"], "view")
        self.assertFalse(agent._granted_control)

    async def test_control_denied_when_local_policy_disabled(self) -> None:
        agent, accept = await self._request_permission(
            control_enabled=False, permission="control"
        )
        self.assertEqual(accept["payload"]["permission"], "view")
        self.assertFalse(agent._granted_control)

    async def test_control_denied_when_input_backend_preflight_fails(self) -> None:
        with patch(
            "mirror_host_agent.windows_input.create_input_sink",
            side_effect=OSError("backend unavailable"),
        ):
            agent, accept = await self._request_permission(
                control_enabled=True, permission="control"
            )
        self.assertEqual(accept["payload"]["permission"], "view")
        self.assertFalse(agent._granted_control)

    @unittest.skipUnless(sys.platform == "win32", "control injection is Windows-only")
    async def test_control_granted_when_enabled_on_windows(self) -> None:
        with patch("mirror_host_agent.__main__.time.time", return_value=1000.0):
            agent, accept = await self._request_permission(
                control_enabled=True, permission="control"
            )
        self.assertEqual(accept["payload"]["permission"], "control")
        self.assertEqual(
            accept["payload"]["expiresAt"], 1_000_000 + CONTROL_GRANT_TTL_MS
        )
        self.assertTrue(agent._granted_control)

    async def test_expired_control_grant_releases_input_and_notifies_viewer(self) -> None:
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                control_enabled=True,
            )
        )
        sink = FakeInputSink()
        controller = InputController(sink, frame_width=1280, frame_height=720)
        controller.set_source_size(1280, 720)
        controller.handle(
            {
                "data": {"code": "KeyA"},
                "event": "key.down",
                "sequence": 1,
                "sessionId": "session_0123456789abcdef",
                "timestamp": 999_000,
                "version": 1,
            },
            now=1.0,
        )
        sink.calls.clear()
        agent._granted_control = True
        agent._control_expires_at_ms = 999_999
        agent._input_controller = controller
        websocket = _FakeWebSocket()
        agent._websocket = websocket

        with patch("mirror_host_agent.__main__.time.time", return_value=1000.0):
            agent._on_control_message(
                _FakeControlChannel(),
                json.dumps(
                    {
                        "data": {"code": "KeyA"},
                        "event": "key.up",
                        "sequence": 2,
                        "sessionId": "session_0123456789abcdef",
                        "timestamp": 1_000_000,
                        "version": 1,
                    }
                ),
            )
        await asyncio.sleep(0)

        self.assertFalse(agent._granted_control)
        self.assertIsNone(agent._input_controller)
        self.assertEqual(sink.calls, [("key", "KeyA", "up")])
        policy = next(m for m in websocket.sent if m["type"] == "session.policy")
        self.assertEqual(
            policy["payload"],
            {
                "controlEnabled": True,
                "controlGranted": False,
                "locked": False,
            },
        )

    async def test_valid_control_ping_renews_an_active_grant(self) -> None:
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                control_enabled=True,
            )
        )
        channel = _FakeControlChannel()
        agent._granted_control = True
        agent._control_expires_at_ms = 1_500_000

        with (
            patch("mirror_host_agent.__main__.time.time", return_value=1000.0),
            patch("mirror_host_agent.__main__.time.monotonic", return_value=123.0),
        ):
            agent._on_control_message(
                channel,
                json.dumps(
                    {
                        "data": {},
                        "event": "session.ping",
                        "sequence": 7,
                        "sessionId": "session_0123456789abcdef",
                        "timestamp": 1_000_000,
                        "version": 1,
                    }
                ),
            )

        self.assertEqual(
            agent._control_expires_at_ms, 1_000_000 + CONTROL_GRANT_TTL_MS
        )
        self.assertEqual(agent._last_control_ping_monotonic, 123.0)
        self.assertEqual(channel.sent[0]["event"], "session.pong")

    @unittest.skipUnless(sys.platform == "win32", "control injection is Windows-only")
    async def test_peer_replacement_reprepares_the_control_backend(self) -> None:
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                control_enabled=True,
            )
        )
        sink = FakeInputSink()
        agent._granted_control = True
        agent._control_expires_at_ms = int(time.time() * 1000) + CONTROL_GRANT_TTL_MS
        agent._pending_input_sink = None

        with patch(
            "mirror_host_agent.windows_input.create_input_sink", return_value=sink
        ) as create_sink:
            agent._start_control()

        self.assertTrue(agent._granted_control)
        self.assertIsNotNone(agent._input_controller)
        create_sink.assert_called_once_with()
        agent._stop_control_runtime()

    @unittest.skipUnless(sys.platform == "win32", "control injection is Windows-only")
    async def test_peer_replacement_fails_closed_when_backend_is_unavailable(self) -> None:
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                control_enabled=True,
            )
        )
        agent._granted_control = True
        agent._control_expires_at_ms = int(time.time() * 1000) + CONTROL_GRANT_TTL_MS

        with patch(
            "mirror_host_agent.windows_input.create_input_sink",
            side_effect=OSError("backend unavailable"),
        ):
            agent._start_control()

        self.assertFalse(agent._granted_control)
        self.assertIsNone(agent._input_controller)
        self.assertIsNone(agent._control_expires_at_ms)

    async def test_active_control_grant_cannot_be_extended_by_re_request(self) -> None:
        # Signaling re-requests do not renew a grant. Only authenticated
        # DataChannel heartbeats may extend the lease; the running controller is
        # left untouched here.
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                control_enabled=True,
            )
        )
        controller = InputController(FakeInputSink(), frame_width=1280, frame_height=720)
        original_expiry = 5_000_000
        agent._granted_control = True
        agent._control_expires_at_ms = original_expiry
        agent._input_controller = controller
        websocket = _FakeWebSocket()
        request = json.dumps(
            {
                "payload": {
                    "deviceId": "device_0123456789abcdef",
                    "permission": "control",
                },
                "sequence": 2,
                "sessionId": "session_0123456789abcdef",
                "type": "session.request",
                "version": 1,
            }
        )

        # 1_000_000 ms is well before the 5_000_000 ms expiry: grant is active.
        with patch("mirror_host_agent.__main__.time.time", return_value=1000.0):
            await agent._handle_message(websocket, request)

        self.assertEqual(agent._control_expires_at_ms, original_expiry)
        self.assertIs(agent._input_controller, controller)
        self.assertTrue(agent._granted_control)
        accept = next(m for m in websocket.sent if m["type"] == "session.accept")
        self.assertEqual(accept["payload"]["permission"], "control")
        self.assertEqual(accept["payload"]["expiresAt"], original_expiry)

    async def test_view_re_request_downgrades_active_control(self) -> None:
        # A view re-request is a legitimate downgrade and must still revoke.
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                control_enabled=True,
            )
        )
        agent._granted_control = True
        agent._control_expires_at_ms = 5_000_000
        agent._input_controller = InputController(
            FakeInputSink(), frame_width=1280, frame_height=720
        )
        websocket = _FakeWebSocket()
        request = json.dumps(
            {
                "payload": {
                    "deviceId": "device_0123456789abcdef",
                    "permission": "view",
                },
                "sequence": 2,
                "sessionId": "session_0123456789abcdef",
                "type": "session.request",
                "version": 1,
            }
        )

        with patch("mirror_host_agent.__main__.time.time", return_value=1000.0):
            await agent._handle_message(websocket, request)

        self.assertFalse(agent._granted_control)
        self.assertIsNone(agent._input_controller)
        self.assertIsNone(agent._control_expires_at_ms)
        accept = next(m for m in websocket.sent if m["type"] == "session.accept")
        self.assertEqual(accept["payload"]["permission"], "view")

    async def test_emergency_stop_releases_input_and_locks_control_until_restart(self) -> None:
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                control_enabled=True,
            )
        )
        sink = FakeInputSink()
        controller = InputController(sink, frame_width=1280, frame_height=720)
        controller.set_source_size(1280, 720)
        controller.handle(
            {
                "data": {"button": "left", "action": "down"},
                "event": "pointer.button",
                "sequence": 1,
                "sessionId": "session_0123456789abcdef",
                "timestamp": 1_000_000,
                "version": 1,
            },
            now=1.0,
        )
        sink.calls.clear()
        agent._granted_control = True
        agent._control_expires_at_ms = 2_000_000
        agent._input_controller = controller

        agent.emergency_stop()

        self.assertFalse(agent.control_enabled)
        self.assertFalse(agent._granted_control)
        self.assertEqual(sink.calls, [("button", "left", "up")])

        websocket = _FakeWebSocket()
        await agent._handle_message(
            websocket,
            json.dumps(
                {
                    "payload": {
                        "deviceId": "device_0123456789abcdef",
                        "permission": "control",
                    },
                    "sequence": 2,
                    "sessionId": "session_0123456789abcdef",
                    "type": "session.request",
                    "version": 1,
                }
            ),
        )
        accept = next(m for m in websocket.sent if m["type"] == "session.accept")
        self.assertEqual(accept["payload"]["permission"], "view")

    async def test_tray_disable_notifies_viewer_and_revokes_control(self) -> None:
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                control_enabled=True,
            )
        )
        websocket = _FakeWebSocket()
        agent._websocket = websocket
        agent._granted_control = True

        agent.set_control_enabled(False)
        await asyncio.sleep(0)

        policy = next(m for m in websocket.sent if m["type"] == "session.policy")
        self.assertEqual(
            policy["payload"],
            {
                "controlEnabled": False,
                "controlGranted": False,
                "locked": False,
            },
        )
        self.assertFalse(agent._granted_control)


class _FakeControlChannel:
    def __init__(self) -> None:
        self.readyState = "open"
        self.sent: list[dict] = []

    def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


class VideoProfileConfigureTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self) -> M0Agent:
        return M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
            )
        )

    def _message(self, profile: str) -> str:
        return json.dumps(
            {
                "payload": {"videoProfile": profile},
                "sequence": 20,
                "sessionId": "session_0123456789abcdef",
                "type": "session.configure",
                "version": 1,
            }
        )

    async def test_replaces_track_and_acknowledges_applied_profile(self) -> None:
        agent = self._agent()
        sender = _FakeSender()
        agent._video_sender = sender
        agent._video_track = SyntheticVideoTrack()
        websocket = _FakeWebSocket()

        await agent._handle_message(websocket, self._message("low"))

        self.assertEqual(agent._video_profile.name, "low")
        self.assertEqual(len(sender.tracks), 1)
        self.assertEqual(sender.tracks[0].source_size, (1280, 720))
        self.assertEqual(sender.keyframe_requests, 1)
        configured = next(m for m in websocket.sent if m["type"] == "session.configured")
        self.assertEqual(configured["payload"], {"videoProfile": "low"})

    async def test_accepts_high_profile(self) -> None:
        # Regression: the allowlist must track video.PROFILES, not a stale
        # {"low","balanced"} literal, or the viewer's High option is rejected.
        agent = self._agent()
        sender = _FakeSender()
        agent._video_sender = sender
        agent._video_track = SyntheticVideoTrack()
        websocket = _FakeWebSocket()

        await agent._handle_message(websocket, self._message("high"))

        self.assertEqual(agent._video_profile.name, "high")
        self.assertEqual(
            (agent._video_profile.width, agent._video_profile.height), (1920, 1080)
        )
        configured = next(m for m in websocket.sent if m["type"] == "session.configured")
        self.assertEqual(configured["payload"], {"videoProfile": "high"})
        self.assertFalse(any(m["type"] == "error" for m in websocket.sent))

    async def test_rejects_unknown_profile(self) -> None:
        agent = self._agent()
        agent._video_sender = _FakeSender()
        agent._video_track = SyntheticVideoTrack()
        websocket = _FakeWebSocket()

        await agent._handle_message(websocket, self._message("ultra"))

        error = next(m for m in websocket.sent if m["type"] == "error")
        self.assertEqual(error["payload"]["code"], "INVALID_VIDEO_PROFILE")

    async def test_rejects_profile_change_without_active_sender(self) -> None:
        agent = self._agent()
        websocket = _FakeWebSocket()

        await agent._handle_message(websocket, self._message("low"))

        error = next(m for m in websocket.sent if m["type"] == "error")
        self.assertEqual(
            error["payload"], {"code": "NO_ACTIVE_SESSION", "retryable": True}
        )


class ClipboardTests(unittest.TestCase):
    def _agent(self, clipboard_enabled: bool) -> M0Agent:
        return M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                clipboard_enabled=clipboard_enabled,
            )
        )

    def test_clipboard_message_shape(self) -> None:
        agent = self._agent(clipboard_enabled=True)
        with patch("mirror_host_agent.__main__.time.time", return_value=1000.0):
            message = agent._clipboard_message("hello")
        self.assertEqual(message["event"], "clipboard.text")
        self.assertEqual(message["data"], {"text": "hello"})
        self.assertEqual(message["sessionId"], "session_0123456789abcdef")
        self.assertEqual(message["timestamp"], 1_000_000)
        self.assertEqual(message["version"], 1)
        self.assertIsInstance(message["sequence"], int)

    def test_clipboard_monitor_is_noop_when_disabled(self) -> None:
        agent = self._agent(clipboard_enabled=False)
        agent._start_clipboard_monitor()
        self.assertIsNone(agent._clipboard_task)

    def test_clipboard_set_writes_host_clipboard_when_enabled(self) -> None:
        agent = self._agent(clipboard_enabled=True)
        with patch(
            "mirror_host_agent.windows_clipboard.write_clipboard_text",
            return_value=True,
        ) as writer, patch(
            "mirror_host_agent.windows_clipboard.get_clipboard_sequence_number",
            return_value=42,
        ):
            agent._handle_clipboard_set({"text": "회사에서 복사한 텍스트"})
        writer.assert_called_once_with("회사에서 복사한 텍스트")
        self.assertEqual(agent._clipboard_text_echo_sequence, 42)

    def test_same_text_with_new_sequence_is_forwarded(self) -> None:
        agent = self._agent(clipboard_enabled=True)
        channel = _FakeControlChannel()
        agent._last_clipboard_text = "same text"

        handled = agent._forward_clipboard_text(channel, 11, "same text")

        self.assertTrue(handled)
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(channel.sent[0]["data"], {"text": "same text"})

    def test_viewer_clipboard_echo_is_suppressed_by_exact_sequence(self) -> None:
        agent = self._agent(clipboard_enabled=True)
        channel = _FakeControlChannel()
        agent._clipboard_text_echo = "viewer text"
        agent._clipboard_text_echo_sequence = 20

        handled = agent._forward_clipboard_text(channel, 20, "viewer text")

        self.assertTrue(handled)
        self.assertEqual(channel.sent, [])
        self.assertIsNone(agent._clipboard_text_echo)
        self.assertIsNone(agent._clipboard_text_echo_sequence)

    def test_stale_viewer_echo_does_not_hide_a_later_copy(self) -> None:
        agent = self._agent(clipboard_enabled=True)
        channel = _FakeControlChannel()
        agent._clipboard_text_echo = "same text"
        agent._clipboard_text_echo_sequence = 20

        handled = agent._forward_clipboard_text(channel, 21, "same text")

        self.assertTrue(handled)
        self.assertEqual(len(channel.sent), 1)
        self.assertIsNone(agent._clipboard_text_echo)

    def test_connecting_channel_keeps_clipboard_text_pending(self) -> None:
        agent = self._agent(clipboard_enabled=True)
        channel = _FakeControlChannel()
        channel.readyState = "connecting"

        handled = agent._forward_clipboard_text(channel, 30, "retry me")

        self.assertFalse(handled)
        self.assertEqual(channel.sent, [])
        self.assertIsNone(agent._last_clipboard_text)

    def test_failed_send_keeps_clipboard_text_pending_for_retry(self) -> None:
        agent = self._agent(clipboard_enabled=True)

        class FailOnceChannel(_FakeControlChannel):
            def __init__(self) -> None:
                super().__init__()
                self.failures = 1

            def send(self, data: str) -> None:
                if self.failures:
                    self.failures -= 1
                    raise RuntimeError("channel temporarily unavailable")
                super().send(data)

        channel = FailOnceChannel()
        self.assertFalse(agent._forward_clipboard_text(channel, 31, "retry me"))
        self.assertIsNone(agent._last_clipboard_text)

        self.assertTrue(agent._forward_clipboard_text(channel, 31, "retry me"))
        self.assertEqual(len(channel.sent), 1)

    def test_clipboard_set_is_noop_when_disabled(self) -> None:
        agent = self._agent(clipboard_enabled=False)
        with patch(
            "mirror_host_agent.windows_clipboard.write_clipboard_text"
        ) as writer:
            agent._handle_clipboard_set({"text": "must-not-write"})
        writer.assert_not_called()

    def test_clipboard_set_ignores_empty_or_non_string(self) -> None:
        agent = self._agent(clipboard_enabled=True)
        with patch(
            "mirror_host_agent.windows_clipboard.write_clipboard_text"
        ) as writer:
            agent._handle_clipboard_set({"text": ""})
            agent._handle_clipboard_set({"text": 123})
            agent._handle_clipboard_set({})
        writer.assert_not_called()

    def test_clipboard_set_is_rate_limited(self) -> None:
        agent = self._agent(clipboard_enabled=True)
        with patch(
            "mirror_host_agent.windows_clipboard.write_clipboard_text",
            return_value=True,
        ) as writer, patch(
            "mirror_host_agent.__main__.time.monotonic",
            side_effect=[100.0, 100.05],
        ):
            agent._handle_clipboard_set({"text": "first"})
            agent._handle_clipboard_set({"text": "second-too-soon"})
        writer.assert_called_once_with("first")

    def test_clipboard_set_allowed_again_after_interval(self) -> None:
        agent = self._agent(clipboard_enabled=True)
        with patch(
            "mirror_host_agent.windows_clipboard.write_clipboard_text",
            return_value=True,
        ) as writer, patch(
            "mirror_host_agent.__main__.time.monotonic",
            side_effect=[100.0, 100.5],
        ):
            agent._handle_clipboard_set({"text": "first"})
            agent._handle_clipboard_set({"text": "second-ok"})
        self.assertEqual(writer.call_count, 2)


class ClipboardMonitorTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self) -> M0Agent:
        return M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                clipboard_enabled=True,
            )
        )

    async def test_locked_clipboard_is_retried_without_losing_change(self) -> None:
        agent = self._agent()
        channel = _FakeControlChannel()
        agent._control_channel = channel  # type: ignore[assignment]

        with patch(
            "mirror_host_agent.windows_clipboard.get_clipboard_sequence_number",
            side_effect=[100, 101, 101],
        ), patch(
            "mirror_host_agent.windows_clipboard.read_clipboard_text",
            side_effect=["initial", None, "copied after lock"],
        ), patch(
            "mirror_host_agent.windows_clipboard.read_clipboard_image_png",
            return_value=None,
        ), patch(
            "mirror_host_agent.__main__.asyncio.sleep",
            new=AsyncMock(
                side_effect=[None, None, asyncio.CancelledError()],
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await agent._run_clipboard_monitor()

        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(
            channel.sent[0]["data"],
            {"text": "copied after lock"},
        )
        self.assertEqual(agent._last_clipboard_sequence, 101)


class _FakeClipboardImageChannel:
    def __init__(self) -> None:
        self.bufferedAmount = 0
        self.readyState = "open"
        self.sent: list[dict | bytes] = []

    def send(self, data: str | bytes) -> None:
        self.sent.append(json.loads(data) if isinstance(data, str) else data)


class ClipboardImageTests(unittest.TestCase):
    def _agent(self) -> tuple[M0Agent, _FakeClipboardImageChannel]:
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                clipboard_enabled=True,
            )
        )
        channel = _FakeClipboardImageChannel()
        agent._clipboard_image_channel = channel  # type: ignore[assignment]
        agent._granted_control = True
        agent._control_expires_at_ms = int(time.time() * 1000) + 60_000
        return agent, channel

    @staticmethod
    def _message(event: str, data: dict) -> str:
        return json.dumps(
            {
                "data": data,
                "event": event,
                "sequence": 1,
                "sessionId": "session_0123456789abcdef",
                "timestamp": int(time.time() * 1000),
                "version": 1,
            }
        )

    def test_viewer_image_is_verified_written_and_acknowledged(self) -> None:
        agent, channel = self._agent()
        payload = b"bounded-png-payload"
        transfer_id = "clipboard_0123456789abcdef"
        offer = self._message(
            "clipboard.image-offer",
            {
                "mimeType": "image/png",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "transferId": transfer_id,
            },
        )
        complete = self._message(
            "clipboard.image-complete", {"transferId": transfer_id}
        )

        with patch(
            "mirror_host_agent.windows_clipboard.write_clipboard_image_png",
            return_value=True,
        ) as writer:
            agent._on_clipboard_image_message(offer)
            agent._on_clipboard_image_message(payload)
            agent._on_clipboard_image_message(complete)

        writer.assert_called_once_with(payload)
        self.assertEqual(channel.sent[-1]["event"], "clipboard.image-applied")
        self.assertEqual(
            agent._clipboard_image_echo_digest, hashlib.sha256(payload).hexdigest()
        )

    def test_viewer_image_digest_mismatch_is_rejected(self) -> None:
        agent, channel = self._agent()
        transfer_id = "clipboard_0123456789abcdef"
        agent._on_clipboard_image_message(
            self._message(
                "clipboard.image-offer",
                {
                    "mimeType": "image/png",
                    "sha256": "a" * 64,
                    "size": 4,
                    "transferId": transfer_id,
                },
            )
        )
        agent._on_clipboard_image_message(b"data")
        agent._on_clipboard_image_message(
            self._message(
                "clipboard.image-complete", {"transferId": transfer_id}
            )
        )

        self.assertEqual(channel.sent[-1]["event"], "clipboard.image-error")
        self.assertEqual(channel.sent[-1]["data"]["code"], "HASH_MISMATCH")

    def test_host_image_uses_offer_chunks_and_completion(self) -> None:
        agent, channel = self._agent()
        payload = b"x" * (70 * 1024)
        digest = hashlib.sha256(payload).hexdigest()

        asyncio.run(agent._send_clipboard_image_to_viewer(payload, digest))

        self.assertEqual(channel.sent[0]["event"], "clipboard.image-offer")
        self.assertEqual(channel.sent[0]["data"]["sha256"], digest)
        self.assertEqual(channel.sent[-1]["event"], "clipboard.image-complete")
        chunks = [item for item in channel.sent[1:-1] if isinstance(item, bytes)]
        self.assertEqual(b"".join(chunks), payload)
        self.assertGreater(len(chunks), 1)


class SessionAdoptionTests(unittest.IsolatedAsyncioTestCase):
    """Option A (ADR-018): the always-on agent adopts the viewer's session."""

    def _agent(self) -> M0Agent:
        return M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_boot_0123456789",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
            )
        )

    @staticmethod
    def _envelope(message_type: str, session_id: str, payload: dict) -> str:
        return json.dumps(
            {
                "payload": payload,
                "sequence": 1,
                "sessionId": session_id,
                "type": message_type,
                "version": 1,
            }
        )

    async def test_adopts_viewer_session_on_request(self) -> None:
        agent = self._agent()
        websocket = _FakeWebSocket()
        # Viewer's session differs from the agent's bootstrap session.
        await agent._handle_message(
            websocket,
            self._envelope(
                "session.request",
                "session_viewer_9876543210",
                {"deviceId": "device_0123456789abcdef", "permission": "view"},
            ),
        )

        self.assertEqual(agent._session_id, "session_viewer_9876543210")
        accept = websocket.sent[-1]
        self.assertEqual(accept["type"], "session.accept")
        # The reply now carries the adopted session so the room/viewer agree.
        self.assertEqual(accept["sessionId"], "session_viewer_9876543210")

    async def test_rejects_stale_session_after_adoption(self) -> None:
        agent = self._agent()
        websocket = _FakeWebSocket()
        await agent._handle_message(
            websocket,
            self._envelope(
                "session.request",
                "session_viewer_9876543210",
                {"deviceId": "device_0123456789abcdef", "permission": "view"},
            ),
        )

        # A message tagged with the old bootstrap session is now foreign.
        with self.assertRaises(ValueError):
            await agent._handle_message(
                websocket,
                self._envelope(
                    "session.configure",
                    "session_boot_0123456789",
                    {"videoProfile": "low"},
                ),
            )

    async def test_error_frame_is_exempt_from_session_match(self) -> None:
        agent = self._agent()
        websocket = _FakeWebSocket()
        await agent._handle_message(
            websocket,
            self._envelope(
                "session.request",
                "session_viewer_9876543210",
                {"deviceId": "device_0123456789abcdef", "permission": "view"},
            ),
        )

        # Server 'error' frames carry the room's own sessionId; they must be
        # logged, not treated as a fatal mismatch.
        await agent._handle_message(
            websocket,
            self._envelope(
                "error", "session_boot_0123456789", {"code": "RATE_LIMITED"}
            ),
        )


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


class FileTransferHandlingTests(unittest.TestCase):
    """Agent-side file-v1 handling (offer -> accept -> chunks -> done)."""

    def _agent(self, tmp: str, *, enabled: bool = True) -> tuple[M0Agent, _FakeChannel]:
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
                files_enabled=enabled,
                files_dir=tmp,
            )
        )
        channel = _FakeChannel()
        agent._file_channel = channel  # type: ignore[assignment]
        return agent, channel

    @staticmethod
    def _offer(name: str, size: int, sha256: str) -> str:
        return json.dumps(
            {
                "data": {
                    "name": name,
                    "sha256": sha256,
                    "size": size,
                    "transferId": "transfer_0123456789",
                },
                "event": "file.offer",
                "sequence": 1,
                "sessionId": "session_0123456789abcdef",
                "timestamp": int(time.time() * 1000),
                "version": 1,
            }
        )

    @staticmethod
    def _complete() -> str:
        return json.dumps(
            {
                "data": {"transferId": "transfer_0123456789"},
                "event": "file.complete",
                "sequence": 2,
                "sessionId": "session_0123456789abcdef",
                "timestamp": int(time.time() * 1000),
                "version": 1,
            }
        )

    def test_happy_path_writes_and_reports_done(self) -> None:
        import hashlib
        import tempfile
        from pathlib import Path

        data = b"remote upload payload" * 500
        with tempfile.TemporaryDirectory() as tmp:
            agent, channel = self._agent(tmp)
            agent._on_file_message(self._offer("doc.txt", len(data), hashlib.sha256(data).hexdigest()))
            agent._on_file_message(data)
            agent._on_file_message(self._complete())

            events = [m["event"] for m in channel.sent]
            self.assertEqual(events, ["file.accept", "file.done"])
            saved = Path(tmp) / "Incoming" / "doc.txt"
            self.assertEqual(saved.read_bytes(), data)

    def test_offer_rejected_when_disabled(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            agent, channel = self._agent(tmp, enabled=False)
            agent._on_file_message(self._offer("doc.txt", 4, "a" * 64))
            self.assertEqual(channel.sent[-1]["event"], "file.error")
            self.assertEqual(channel.sent[-1]["data"]["code"], "FILES_DISABLED")

    def test_blocked_extension_reported(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            agent, channel = self._agent(tmp)
            agent._on_file_message(self._offer("evil.exe", 4, "a" * 64))
            self.assertEqual(channel.sent[-1]["data"]["code"], "BLOCKED_TYPE")

    def test_digest_mismatch_reports_error_and_writes_nothing(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            agent, channel = self._agent(tmp)
            agent._on_file_message(self._offer("doc.txt", 4, "a" * 64))
            agent._on_file_message(b"data")
            agent._on_file_message(self._complete())
            self.assertEqual(channel.sent[-1]["event"], "file.error")
            self.assertEqual(channel.sent[-1]["data"]["code"], "DIGEST_MISMATCH")
            self.assertFalse((Path(tmp) / "Incoming").exists() and any((Path(tmp) / "Incoming").iterdir()))

    def test_wrong_session_offer_ignored(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            agent, channel = self._agent(tmp)
            bad = json.loads(self._offer("doc.txt", 4, "a" * 64))
            bad["sessionId"] = "session_intruder_000000"
            agent._on_file_message(json.dumps(bad))
            self.assertEqual(channel.sent, [])


class IceGatheringTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_uses_partial_candidates_instead_of_dropping_session(
        self,
    ) -> None:
        agent = M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_0123456789abcdef",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
            )
        )

        class GatheringPeer:
            iceGatheringState = "gathering"

            def on(self, _event: str):
                return lambda callback: callback

        with patch(
            "mirror_host_agent.__main__.ICE_GATHERING_TIMEOUT_SECONDS", 0.01
        ):
            await agent._wait_for_ice_gathering(GatheringPeer())  # type: ignore[arg-type]


class RunForeverTests(unittest.IsolatedAsyncioTestCase):
    """Auto-reconnect loop: backoff, session reset, cancellation."""

    def _agent(self) -> M0Agent:
        return M0Agent(
            AgentConfig(
                device_id="device_0123456789abcdef",
                heartbeat_stop_after_seconds=None,
                session_id="session_boot_0123456789",
                ticket="not-used",
                ws_url="ws://127.0.0.1:8787/ws",
            )
        )

    async def test_retries_with_exponential_backoff_and_stops_on_cancel(self) -> None:
        agent = self._agent()
        attempts = 0

        async def failing_run() -> None:
            nonlocal attempts
            attempts += 1
            raise OSError("connection refused")

        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) >= 4:
                raise asyncio.CancelledError

        with (
            patch.object(agent, "run", failing_run),
            patch("mirror_host_agent.__main__.asyncio.sleep", fake_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await agent.run_forever()

        self.assertEqual(attempts, 4)
        # 2 -> 4 -> 8 -> 16, capped later at 60.
        self.assertEqual(delays, [2.0, 4.0, 8.0, 16.0])

    async def test_delay_caps_at_maximum(self) -> None:
        agent = self._agent()

        async def failing_run() -> None:
            raise OSError("boom")

        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) >= 8:
                raise asyncio.CancelledError

        with (
            patch.object(agent, "run", failing_run),
            patch("mirror_host_agent.__main__.asyncio.sleep", fake_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await agent.run_forever()

        self.assertEqual(max(delays), 60.0)
        self.assertEqual(delays[-2:], [60.0, 60.0])

    async def test_resets_adopted_session_before_each_attempt(self) -> None:
        agent = self._agent()
        seen_sessions: list[str] = []

        async def failing_run() -> None:
            seen_sessions.append(agent._session_id)
            # Simulate having adopted a viewer session during the connection.
            agent._session_id = "session_viewer_9876543210"
            raise OSError("dropped")

        async def fake_sleep(_delay: float) -> None:
            if len(seen_sessions) >= 2:
                raise asyncio.CancelledError

        with (
            patch.object(agent, "run", failing_run),
            patch("mirror_host_agent.__main__.asyncio.sleep", fake_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await agent.run_forever()

        # Every attempt starts from the bootstrap session, not the stale one.
        self.assertEqual(
            seen_sessions,
            ["session_boot_0123456789", "session_boot_0123456789"],
        )

    async def test_preserves_adopted_session_while_peer_is_healthy(self) -> None:
        agent = self._agent()
        seen_sessions: list[str] = []

        async def failing_run() -> None:
            seen_sessions.append(agent._session_id)
            if len(seen_sessions) == 1:
                agent._session_id = "session_viewer_9876543210"
                agent._peer = SimpleNamespace(connectionState="connected")
                agent._control_channel = SimpleNamespace(readyState="open")
                agent._last_control_ping_monotonic = time.monotonic()
            raise OSError("signaling dropped")

        async def fake_sleep(_delay: float) -> None:
            if len(seen_sessions) >= 2:
                raise asyncio.CancelledError

        close_peer = AsyncMock()
        with (
            patch.object(agent, "run", failing_run),
            patch.object(agent, "_close_peer", close_peer),
            patch("mirror_host_agent.__main__.asyncio.sleep", fake_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await agent.run_forever()

        self.assertEqual(
            seen_sessions,
            ["session_boot_0123456789", "session_viewer_9876543210"],
        )
        close_peer.assert_awaited_once()

    async def test_peer_health_requires_a_recent_authenticated_ping(self) -> None:
        agent = self._agent()
        agent._peer = SimpleNamespace(connectionState="connected")
        agent._control_channel = SimpleNamespace(readyState="open")

        with patch(
            "mirror_host_agent.__main__.time.monotonic", return_value=100.0
        ):
            agent._last_control_ping_monotonic = 99.0
            self.assertTrue(agent._peer_is_healthy())

            agent._last_control_ping_monotonic = 80.0
            self.assertFalse(agent._peer_is_healthy())

    async def test_peer_close_timeout_does_not_block_replacement(self) -> None:
        agent = self._agent()

        class HangingPeer:
            async def close(self) -> None:
                await asyncio.Future()

        agent._peer = HangingPeer()  # type: ignore[assignment]

        with patch(
            "mirror_host_agent.__main__.PEER_CLOSE_TIMEOUT_SECONDS", 0.01
        ):
            await agent._close_peer()

        self.assertIsNone(agent._peer)


if __name__ == "__main__":
    unittest.main()
