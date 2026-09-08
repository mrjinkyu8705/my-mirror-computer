from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from aiortc import (
    RTCConfiguration,
    RTCDataChannel,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from websockets.asyncio.client import connect

from .file_transfer import FileReceiver, FileTransferError
from .h264_encoder import (
    DEFAULT_H264_BACKEND,
    DEFAULT_H264_PRESET,
    TunedH264Encoder,
    install_tuned_h264_encoder,
)
from .input_control import InputController, InputSink
from .outgoing import list_outgoing, resolve_outgoing_file
from .video import (
    DESKTOP_SOURCE,
    PROFILES,
    SYNTHETIC_SOURCE,
    SyntheticVideoTrack,  # re-exported for tests and existing import paths
    create_video_track,
    extract_primary_video_codec,
    get_profile,
)

LOGGER = logging.getLogger("mirror_host_agent")
PROTOCOL_VERSION = 1
MAX_SIGNALING_BYTES = 256 * 1024
# Raw per-message cap on the control DataChannel. Sized for the largest control
# message: a clipboard.set carrying up to CLIPBOARD_TEXT_MAX_LENGTH (16384)
# characters — worst-case ~4 UTF-8 bytes each — plus the JSON envelope. Pointer
# and key events are two orders of magnitude smaller and their handlers cap
# individual fields, so this headroom does not loosen their validation.
MAX_CONTROL_BYTES = 72 * 1024
DEFAULT_VIDEO_PROFILE = "balanced"
CONTROL_WATCHDOG_INTERVAL_SECONDS = 1.0
CONTROL_GRANT_TTL_MS = 60 * 60 * 1000
# The viewer sends an authenticated control heartbeat every second. aiortc can
# leave a dead peer looking "connected/open" after ICE consent expires, so do
# not preserve it across signaling reconnects without a recent heartbeat.
CONTROL_PEER_HEALTH_TIMEOUT_SECONDS = 12.0
PEER_CLOSE_TIMEOUT_SECONDS = 3.0
CLIPBOARD_POLL_INTERVAL_SECONDS = 0.7
# Clipboard owners can briefly lock the Win32 clipboard immediately after its
# sequence number changes. Retry the same change for several polls before
# treating it as an unsupported/empty clipboard format.
CLIPBOARD_READ_RETRY_LIMIT = 10
# Minimum spacing between accepted viewer->host clipboard writes. One paste sends
# a single clipboard.set, so this never affects normal use; it just bounds a
# granted viewer clobbering the host user's clipboard in a tight loop.
CLIPBOARD_SET_MIN_INTERVAL_SECONDS = 0.2
CLIPBOARD_IMAGE_MAX_BYTES = 20 * 1024 * 1024
CLIPBOARD_IMAGE_CHUNK_BYTES = 64 * 1024
CLIPBOARD_IMAGE_HIGH_WATER_BYTES = 4 * 1024 * 1024
CONTROL_TIMESTAMP_SKEW_MS = 30_000
# Per-chunk cap on the file DataChannel — bounds memory and matches the viewer's
# send size. The whole-file cap is enforced by FileReceiver (ADR-014).
MAX_FILE_CHUNK_BYTES = 256 * 1024
DEFAULT_FILES_DIRNAME = "MirrorShare"
# Download (agent -> viewer) streaming: chunk size and the send-buffer high-water
# mark that pauses reading so a slow link cannot blow up memory.
FILE_DOWNLOAD_CHUNK_BYTES = 64 * 1024
FILE_DOWNLOAD_HIGH_WATER_BYTES = 8 * 1024 * 1024
# Auto-reconnect (always-on agent): capped exponential backoff. A connection
# that stayed up at least the stable window resets the backoff.
RECONNECT_INITIAL_DELAY_SECONDS = 2.0
RECONNECT_MAX_DELAY_SECONDS = 60.0
RECONNECT_STABLE_RESET_SECONDS = 60.0
SIGNALING_PING_INTERVAL_SECONDS = 10.0
SIGNALING_PING_TIMEOUT_SECONDS = 30.0
ICE_GATHERING_TIMEOUT_SECONDS = 10.0

__all__ = ["AgentConfig", "M0Agent", "SyntheticVideoTrack", "main", "run_agent"]


def configure_dpi_awareness() -> None:
    """Set Per-Monitor DPI Aware V2 so capture and coordinate mapping use
    physical pixels. No-op off Windows; best-effort on older Windows without the
    API."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # noqa: BLE001 - never fatal
        LOGGER.warning("Could not set Per-Monitor DPI Aware V2")


@dataclass(frozen=True)
class AgentConfig:
    device_id: str
    heartbeat_stop_after_seconds: float | None
    session_id: str
    ticket: str
    ws_url: str
    video_source: str = SYNTHETIC_SOURCE
    video_profile: str = DEFAULT_VIDEO_PROFILE
    control_enabled: bool = False
    clipboard_enabled: bool = False
    files_enabled: bool = False
    files_dir: str = ""


@dataclass
class IncomingClipboardImage:
    transfer_id: str
    size: int
    sha256: str
    chunks: bytearray


class M0Agent:
    def __init__(self, config: AgentConfig):
        self._device_id = config.device_id
        self._heartbeat_stop_after_seconds = config.heartbeat_stop_after_seconds
        self._session_id = config.session_id
        # Original configured session id. The agent adopts the viewer's session
        # during a connection (option A); each reconnect starts back from this
        # bootstrap id until the next viewer joins.
        self._bootstrap_session_id = config.session_id
        self._ticket = config.ticket
        self._ws_url = config.ws_url
        self._video_source = config.video_source
        self._video_profile = get_profile(config.video_profile)
        self._control_enabled = config.control_enabled
        self._emergency_locked = False
        self._connected = False
        self._status_listener: Callable[[str], None] | None = None
        self._sequence = 0
        self._peer: RTCPeerConnection | None = None
        self._websocket: Any = None
        self._video_sender: RTCRtpSender | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        # Per-session control state; set on grant, cleared on teardown.
        self._granted_control = False
        self._control_expires_at_ms: int | None = None
        self._pending_input_sink: InputSink | None = None
        self._video_track: Any = None
        self._input_controller: InputController | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        # Bidirectional clipboard share. Text stays on control-v1; image bytes
        # use clipboard-v1 so screenshots never delay mouse/keyboard input.
        self._clipboard_enabled = config.clipboard_enabled
        self._control_channel: RTCDataChannel | None = None
        self._last_control_ping_monotonic: float | None = None
        self._clipboard_image_channel: RTCDataChannel | None = None
        self._clipboard_task: asyncio.Task[None] | None = None
        self._last_clipboard_text: str | None = None
        self._last_clipboard_sequence: int | None = None
        self._clipboard_text_echo: str | None = None
        self._clipboard_text_echo_sequence: int | None = None
        self._clipboard_image_echo_digest: str | None = None
        self._incoming_clipboard_image: IncomingClipboardImage | None = None
        self._last_clipboard_set_monotonic: float | None = None
        # Sandboxed file receive (ADR-014), opt-in and default off.
        self._files_enabled = config.files_enabled
        self._files_dir = Path(config.files_dir) if config.files_dir else None
        self._file_channel: RTCDataChannel | None = None
        self._file_receiver: FileReceiver | None = None
        self._file_transfer_id: str | None = None
        # Sandboxed download (agent -> viewer) from the Outgoing folder. At most
        # one download runs at a time; its id gates the streaming loop.
        self._download_transfer_id: str | None = None
        self._download_task: asyncio.Task[None] | None = None

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    @property
    def control_enabled(self) -> bool:
        return self._control_enabled

    def set_status_listener(self, listener: Callable[[str], None]) -> None:
        self._status_listener = listener
        self._notify_status("offline")

    def _notify_status(self, status: str) -> None:
        if self._status_listener is not None:
            self._status_listener("locked" if self._emergency_locked else status)

    def set_control_enabled(self, enabled: bool) -> None:
        if enabled and self._emergency_locked:
            return
        self._control_enabled = enabled
        if not enabled:
            self._revoke_control()
        if self._peer is not None:
            self._notify_status("controlling" if self._granted_control else "viewing")
        else:
            self._notify_status("online" if self._connected else "offline")
        self._queue_policy_update()

    def _queue_policy_update(self) -> None:
        if self._websocket is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            LOGGER.debug("Could not queue policy update without a running event loop")
            return
        loop.create_task(self._send_policy_update())

    async def _send_policy_update(self) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        try:
            await websocket.send(
                json.dumps(
                    self._message(
                        "session.policy",
                        {
                            "controlEnabled": self._control_enabled,
                            "controlGranted": self._granted_control,
                            "locked": self._emergency_locked,
                        },
                    )
                )
            )
        except Exception as error:  # noqa: BLE001 - connection teardown races are benign
            LOGGER.debug("Could not send local policy update: %s", type(error).__name__)

    def emergency_stop(self) -> None:
        """Fail closed for the rest of this process lifetime.

        The local hotkey is intentionally one-way: control can only be enabled
        again by restarting the agent with its explicit environment opt-in.
        """
        self._control_enabled = False
        self._emergency_locked = True
        self._revoke_control()
        self._notify_status("locked")
        self._queue_policy_update()
        LOGGER.warning("Local emergency stop activated; remote control locked")

    def _message(self, message_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "payload": payload,
            "sequence": self._next_sequence(),
            "sessionId": self._session_id,
            "type": message_type,
            "version": PROTOCOL_VERSION,
        }

    async def run_forever(self) -> None:
        """Keep the agent online: run() and reconnect with capped backoff.

        Every kind of drop is retried — network blips, signaling Worker
        redeploys, and auth rejections too (a 401 can be transient while a
        rotated secret propagates; a genuinely dead device token just keeps
        retrying at the max interval until the operator re-mints it). The
        emergency lock persists across reconnects because it lives on this
        instance. Cancellation (Ctrl+C / task cancel) exits the loop.
        """
        delay = RECONNECT_INITIAL_DELAY_SECONDS
        try:
            while True:
                # A healthy WebRTC/DataChannel session keeps its adopted viewer
                # session id while signaling reconnects in the background. Only
                # a connection without a usable peer returns to the bootstrap id
                # and negotiates from scratch.
                if not self._peer_is_healthy():
                    self._session_id = self._bootstrap_session_id
                started_at = time.monotonic()
                try:
                    await self.run()
                    LOGGER.warning(
                        "Signaling connection closed; retrying in %.0fs", delay
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - reconnect on any drop
                    LOGGER.warning(
                        "Signaling connection lost (%s); retrying in %.0fs",
                        type(error).__name__,
                        delay,
                    )
                if time.monotonic() - started_at >= RECONNECT_STABLE_RESET_SECONDS:
                    delay = RECONNECT_INITIAL_DELAY_SECONDS
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)
        finally:
            # Process shutdown is different from a signaling blink: release all
            # capture/input resources and held keys exactly once.
            await self._close_peer()

    def _peer_is_healthy(self) -> bool:
        peer = self._peer
        channel = self._control_channel
        last_ping = self._last_control_ping_monotonic
        return (
            peer is not None
            and getattr(peer, "connectionState", None) == "connected"
            and channel is not None
            and getattr(channel, "readyState", None) == "open"
            and last_ping is not None
            and time.monotonic() - last_ping
            <= CONTROL_PEER_HEALTH_TIMEOUT_SECONDS
        )

    async def run(self) -> None:
        query = urlencode({"ticket": self._ticket})
        websocket_url = f"{self._ws_url}?{query}"
        async with connect(
            websocket_url,
            max_size=MAX_SIGNALING_BYTES,
            origin=None,
            ping_interval=SIGNALING_PING_INTERVAL_SECONDS,
            ping_timeout=SIGNALING_PING_TIMEOUT_SECONDS,
        ) as websocket:
            self._websocket = websocket
            self._connected = True
            status = "online"
            if self._peer_is_healthy():
                status = "controlling" if self._granted_control else "viewing"
            self._notify_status(status)
            LOGGER.info("M0 agent connected to local signaling")
            await websocket.send(
                json.dumps(
                    self._message(
                        "agent.online",
                        {
                            "agentId": "agent_0123456789abcdef0",
                            "deviceId": self._device_id,
                            "protocolVersion": PROTOCOL_VERSION,
                        },
                    )
                )
            )
            self._heartbeat_task = asyncio.create_task(self._send_heartbeats(websocket))

            try:
                async for raw_message in websocket:
                    await self._handle_message(websocket, raw_message)
            finally:
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    self._heartbeat_task = None
                preserve_peer = self._peer_is_healthy()
                if not preserve_peer:
                    await self._close_peer()
                self._connected = False
                self._websocket = None
                if preserve_peer:
                    LOGGER.info(
                        "Signaling offline; active WebRTC/control session preserved"
                    )
                    self._notify_status(
                        "controlling" if self._granted_control else "viewing"
                    )
                else:
                    self._notify_status("offline")

    async def _send_heartbeats(self, websocket: Any) -> None:
        started_at = time.monotonic()
        while True:
            await asyncio.sleep(3)
            if (
                self._heartbeat_stop_after_seconds is not None
                and time.monotonic() - started_at
                >= self._heartbeat_stop_after_seconds
            ):
                LOGGER.info("M0 test mode stopped application heartbeats")
                return
            await websocket.send(
                json.dumps(self._message("agent.heartbeat", {}))
            )

    async def _handle_message(self, websocket: Any, raw_message: Any) -> None:
        if not isinstance(raw_message, str) or len(raw_message.encode()) > MAX_SIGNALING_BYTES:
            raise ValueError("Invalid signaling message transport")

        message = json.loads(raw_message)
        if not self._is_valid_envelope(message):
            raise ValueError("Invalid signaling envelope")

        message_type = message["type"]
        incoming_session = message["sessionId"]
        if message_type == "session.request":
            # Option A (ADR-018): the always-on agent connects with a bootstrap
            # sessionId (its device token) and adopts the viewer's session when a
            # viewer joins. The room is single-session, so there is exactly one
            # viewer to bind to.
            self._session_id = incoming_session
        elif message_type != "error" and incoming_session != self._session_id:
            # Server-originated 'error' frames carry the room's own sessionId and
            # are only logged, so they are exempt from the session match.
            raise ValueError("Signaling sessionId mismatch")

        if message_type == "session.request":
            requested = message["payload"].get("permission")
            requested_profile = message["payload"].get("videoProfile")
            if requested_profile in PROFILES:
                self._video_profile = get_profile(requested_profile)
            # A duplicate signaling request must not reset an active control
            # grant. Renewal is allowed only by valid heartbeats on the already
            # authenticated control DataChannel; echo the current lease here and
            # leave the running controller untouched.
            if (
                requested == "control"
                and self._granted_control
                and self._control_grant_is_active()
            ):
                await websocket.send(
                    json.dumps(
                        self._message(
                            "session.accept",
                            {
                                "expiresAt": self._control_expires_at_ms,
                                "permission": "control",
                                "videoProfile": self._video_profile.name,
                            },
                        )
                    )
                )
                return
            # Control is granted only when the viewer asks for it AND the local
            # policy allows it AND injection is available (Windows). Otherwise the
            # session gracefully degrades to view-only.
            self._revoke_control()
            can_control = (
                requested == "control"
                and self._control_enabled
                and sys.platform == "win32"
            )
            if can_control:
                try:
                    from .windows_input import create_input_sink

                    self._pending_input_sink = create_input_sink()
                except Exception as error:  # noqa: BLE001 - fail closed
                    LOGGER.warning(
                        "Control backend preflight failed (%s); granting view only",
                        type(error).__name__,
                    )
                    can_control = False
            self._granted_control = can_control
            now_ms = int(time.time() * 1000)
            self._control_expires_at_ms = (
                now_ms + CONTROL_GRANT_TTL_MS if can_control else None
            )
            granted = "control" if can_control else "view"
            LOGGER.info(
                "session.request permission=%s -> granted=%s (local control %s)",
                requested,
                granted,
                "enabled" if self._control_enabled else "disabled",
            )
            await websocket.send(
                json.dumps(
                    self._message(
                        "session.accept",
                        {
                            "expiresAt": self._control_expires_at_ms
                            or now_ms + CONTROL_GRANT_TTL_MS,
                            "permission": granted,
                            "videoProfile": self._video_profile.name,
                        },
                    )
                )
            )
            return

        if message_type == "webrtc.offer":
            await self._answer_offer(websocket, message["payload"]["sdp"])
            return

        if message_type == "session.configure":
            profile_name = message["payload"].get("videoProfile")
            if profile_name not in PROFILES:
                await self._send_error(websocket, "INVALID_VIDEO_PROFILE", False)
                return
            if self._video_sender is None:
                await self._send_error(websocket, "NO_ACTIVE_SESSION", True)
                return
            self._apply_video_profile(profile_name)
            await websocket.send(
                json.dumps(
                    self._message(
                        "session.configured", {"videoProfile": self._video_profile.name}
                    )
                )
            )
            return

        if message_type == "session.close":
            self._revoke_control()
            await self._close_peer()
            return

        if message_type == "error":
            LOGGER.warning("Signaling rejected a message: %s", message["payload"]["code"])

    def _is_valid_envelope(self, message: Any) -> bool:
        # Structural validation only. The sessionId is checked in _handle_message,
        # which adopts the viewer's session on session.request (see option A).
        return (
            isinstance(message, dict)
            and set(message) == {"payload", "sequence", "sessionId", "type", "version"}
            and message.get("version") == PROTOCOL_VERSION
            and isinstance(message.get("sessionId"), str)
            and isinstance(message.get("payload"), dict)
            and isinstance(message.get("sequence"), int)
            and isinstance(message.get("type"), str)
        )

    def _turn_url(self) -> str | None:
        """Derive the signaling Worker's /turn URL from the ws URL. None for a
        local ws:// dev endpoint (host/STUN candidates suffice there)."""
        if not self._ws_url.startswith("wss://"):
            return None
        base = "https://" + self._ws_url[len("wss://") :]
        base = base.rsplit("/ws", 1)[0]
        return f"{base}/turn?{urlencode({'ticket': self._ticket})}"

    @staticmethod
    def _http_get_json(url: str) -> Any:
        import urllib.request

        # An explicit User-Agent is required: Cloudflare's edge answers the
        # default "Python-urllib/x.y" signature with 403 (error 1010) before the
        # Worker ever runs, so /turn always failed and TURN silently degraded to
        # STUN-only on the agent side.
        request = urllib.request.Request(
            url, headers={"user-agent": "mirror-host-agent"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - https only
            return json.loads(response.read().decode("utf-8"))

    async def _fetch_ice_servers(self) -> list[RTCIceServer]:
        servers = [RTCIceServer(urls="stun:stun.cloudflare.com:3478")]
        turn_url = self._turn_url()
        if turn_url is None:
            return servers
        try:
            payload = await asyncio.to_thread(self._http_get_json, turn_url)
        except Exception as error:  # noqa: BLE001 - TURN is best-effort; fall back to STUN
            LOGGER.warning(
                "TURN credential fetch failed (%s); using STUN only",
                type(error).__name__,
            )
            return servers
        entries = payload.get("iceServers", []) if isinstance(payload, dict) else []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            urls = entry.get("urls")
            if urls:
                servers.append(
                    RTCIceServer(
                        urls=urls,
                        username=entry.get("username"),
                        credential=entry.get("credential"),
                    )
                )
        return servers

    async def _answer_offer(self, websocket: Any, sdp: Any) -> None:
        if not isinstance(sdp, str) or not 1 <= len(sdp) <= 131_072:
            raise ValueError("Invalid SDP offer")

        await self._close_peer()
        # STUN + TURN (from /turn, matching the viewer) so ICE can cross NAT and,
        # on UDP-blocked/firewalled networks, relay over TCP/TLS 443 (M3-05).
        peer = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=await self._fetch_ice_servers())
        )
        self._peer = peer

        # Diagnostic only: surface WebRTC/ICE state transitions so intermittent
        # drops on flaky networks (corporate Wi-Fi, TURN relay) are visible in
        # the agent log. No frame/coordinate/keystroke content is logged.
        @peer.on("connectionstatechange")
        def on_connection_state_change() -> None:
            LOGGER.info("WebRTC connection=%s", peer.connectionState)

        @peer.on("iceconnectionstatechange")
        def on_ice_connection_state_change() -> None:
            LOGGER.info("WebRTC ice=%s", peer.iceConnectionState)

        track = create_video_track(self._video_source, self._video_profile)
        self._video_track = track
        self._video_sender = peer.addTrack(track)

        if self._granted_control:
            self._start_control()

        @peer.on("datachannel")
        def on_datachannel(channel: RTCDataChannel) -> None:
            if channel.label == "file-v1":
                self._attach_file_channel(channel)
                return
            if channel.label == "clipboard-v1":
                self._attach_clipboard_image_channel(channel)
                return
            if channel.label != "control-v1":
                channel.close()
                return

            self._control_channel = channel
            self._last_control_ping_monotonic = None
            self._start_clipboard_monitor()

            @channel.on("message")
            def on_message(raw_control: Any) -> None:
                self._on_control_message(channel, raw_control)

            @channel.on("close")
            def on_close() -> None:
                self._stop_clipboard_monitor()
                if self._control_channel is channel:
                    self._control_channel = None
                    self._last_control_ping_monotonic = None

        await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        answer = await peer.createAnswer()
        await peer.setLocalDescription(answer)
        await self._wait_for_ice_gathering(peer)
        local_description = peer.localDescription
        if local_description is None:
            raise RuntimeError("Failed to create WebRTC answer")

        await websocket.send(
            json.dumps(
                self._message("webrtc.answer", {"sdp": local_description.sdp})
            )
        )
        codec = extract_primary_video_codec(local_description.sdp)
        LOGGER.info(
            "Negotiated video: source=%s profile=%s %dx%d@%dfps codec=%s",
            self._video_source,
            self._video_profile.name,
            self._video_profile.width,
            self._video_profile.height,
            self._video_profile.fps,
            codec or "unknown",
        )
        self._notify_status("controlling" if self._granted_control else "viewing")

    async def _send_error(
        self, websocket: Any, code: str, retryable: bool
    ) -> None:
        await websocket.send(
            json.dumps(self._message("error", {"code": code, "retryable": retryable}))
        )

    def _apply_video_profile(self, profile_name: str) -> None:
        sender = self._video_sender
        if sender is None:
            raise RuntimeError("No active video sender")
        new_profile = get_profile(profile_name)
        if new_profile.name == self._video_profile.name:
            return
        old_track = self._video_track
        new_track = create_video_track(self._video_source, new_profile)
        sender.replaceTrack(new_track)
        # replaceTrack keeps the RTCRtpSender and its encoder instance alive.
        # Explicitly request a fresh reference picture so a profile change also
        # clears a low-quality or damaged static frame instead of inheriting it.
        request_keyframe = getattr(sender, "_send_keyframe", None)
        if callable(request_keyframe):
            request_keyframe()
        self._video_profile = new_profile
        self._video_track = new_track
        if self._input_controller is not None:
            self._input_controller.set_frame_size(new_profile.width, new_profile.height)
        if old_track is not None:
            old_track.stop()
        LOGGER.info(
            "Video profile changed: profile=%s %dx%d@%dfps",
            new_profile.name,
            new_profile.width,
            new_profile.height,
            new_profile.fps,
        )

    def _start_control(self) -> None:
        """Create the injection sink + controller for a granted control session.
        Falls back to view-only if injection is unavailable."""
        sink = self._pending_input_sink
        self._pending_input_sink = None
        if (
            sink is None
            and self._granted_control
            and self._control_enabled
            and not self._emergency_locked
            and sys.platform == "win32"
        ):
            # A reconnect can replace the peer while the authenticated control
            # lease is still active. _close_peer() correctly releases the old
            # controller, so prepare a fresh SendInput sink for the replacement
            # peer instead of silently downgrading the new video session to view.
            try:
                from .windows_input import create_input_sink

                sink = create_input_sink()
                LOGGER.info("Control backend re-prepared for peer replacement")
            except Exception as error:  # noqa: BLE001 - fail closed
                LOGGER.warning(
                    "Control backend re-prepare failed (%s); staying view-only",
                    type(error).__name__,
                )
        if sink is None:
            LOGGER.warning("Control backend was not prepared; staying view-only")
            self._revoke_control()
            return
        self._input_controller = InputController(
            sink,
            frame_width=self._video_profile.width,
            frame_height=self._video_profile.height,
        )
        self._watchdog_task = asyncio.create_task(self._run_input_watchdog())
        LOGGER.info("Control granted: input injection active")
        self._notify_status("controlling")

    async def _run_input_watchdog(self) -> None:
        while True:
            await asyncio.sleep(CONTROL_WATCHDOG_INTERVAL_SECONDS)
            if not self._control_grant_is_active():
                self._revoke_control()
                self._queue_policy_update()
                LOGGER.info("Control grant expired; viewer downgraded to view-only")
                return
            controller = self._input_controller
            if controller is not None:
                controller.on_watchdog_tick(time.monotonic())

    def _on_control_message(self, channel: RTCDataChannel, raw_control: Any) -> None:
        if self._granted_control and not self._control_grant_is_active():
            self._revoke_control()
            self._queue_policy_update()
            LOGGER.info("Expired control input rejected; viewer notified")
        pong = self._create_pong(raw_control)
        if pong is not None:
            self._last_control_ping_monotonic = time.monotonic()
            # Keep an authenticated, responsive control session alive. The grant
            # remains a dead-man lease: if the viewer or DataChannel disappears,
            # pings stop and the watchdog still revokes input after the TTL.
            if self._control_grant_is_active():
                self._control_expires_at_ms = (
                    int(time.time() * 1000) + CONTROL_GRANT_TTL_MS
                )
            channel.send(json.dumps(pong))
            return
        message = self._parse_control(raw_control)
        if message is not None and message["event"] == "video.receiver-report":
            self._handle_receiver_report(message["data"])
            return
        controller = self._input_controller
        if not self._granted_control or controller is None:
            return
        if message is None:
            return
        if message["event"] == "clipboard.set":
            self._handle_clipboard_set(message["data"])
            return
        source = getattr(self._video_track, "source_size", None)
        if source is not None:
            controller.set_source_size(source[0], source[1])
        controller.handle(message, now=time.monotonic())

    def _handle_receiver_report(self, data: dict[str, Any]) -> None:
        if set(data) != {
            "jitterMs",
            "packetLossPercent",
            "receivedBitrateMbps",
            "route",
        }:
            return

        def valid_optional_number(
            value: Any, *, minimum: float, maximum: float
        ) -> float | None:
            if value is None:
                return None
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < minimum
                or value > maximum
            ):
                raise ValueError
            return float(value)

        try:
            jitter_ms = valid_optional_number(
                data.get("jitterMs"), minimum=0, maximum=60_000
            )
            packet_loss_percent = valid_optional_number(
                data.get("packetLossPercent"), minimum=0, maximum=100
            )
            received_bitrate = valid_optional_number(
                data.get("receivedBitrateMbps"), minimum=0, maximum=1_000
            )
        except ValueError:
            return
        route = data.get("route")
        if route not in {"direct", "turn", "unknown"} or received_bitrate is None:
            return
        if jitter_ms is None and packet_loss_percent is None:
            return
        TunedH264Encoder.update_receiver_network_health(
            jitter_ms=jitter_ms,
            packet_loss_percent=packet_loss_percent,
            route=route,
        )

    def _handle_clipboard_set(self, data: dict[str, Any]) -> None:
        """Write viewer-supplied text to the host clipboard (viewer -> agent).

        Gated behind the same opt-in flag as the host->viewer mirror; reaching
        here already implies control is granted and active. Never logs content.

        The Win32 write runs synchronously on the event-loop thread on purpose:
        a paste sends clipboard.set immediately followed by a Ctrl+V key chord on
        the same ordered channel, so the clipboard MUST be written before those
        key events are processed. Offloading to an executor would race the paste.
        """
        if not self._clipboard_enabled:
            return
        text = data.get("text")
        if not isinstance(text, str) or not text:
            return
        now = time.monotonic()
        last = self._last_clipboard_set_monotonic
        if last is not None and now - last < CLIPBOARD_SET_MIN_INTERVAL_SECONDS:
            return
        self._last_clipboard_set_monotonic = now

        from .windows_clipboard import (
            CLIPBOARD_TEXT_MAX_LENGTH,
            get_clipboard_sequence_number,
            write_clipboard_text,
        )

        payload = text[:CLIPBOARD_TEXT_MAX_LENGTH]
        ok = write_clipboard_text(payload)
        if ok:
            # Suppress only the exact Win32 sequence created by this viewer
            # write. Content-only de-duplication incorrectly swallowed a later
            # user Ctrl+C when the copied text happened to be identical.
            self._clipboard_text_echo = payload
            self._clipboard_text_echo_sequence = get_clipboard_sequence_number()
        LOGGER.info(
            "Clipboard set from viewer: %s (%d chars)",
            "ok" if ok else "failed",
            len(payload),
        )

    def _parse_control(self, raw_control: Any) -> dict[str, Any] | None:
        if not isinstance(raw_control, str) or len(raw_control.encode()) > MAX_CONTROL_BYTES:
            return None
        try:
            message = json.loads(raw_control)
        except json.JSONDecodeError:
            return None
        timestamp = message.get("timestamp") if isinstance(message, dict) else None
        now_ms = int(time.time() * 1000)
        if (
            not isinstance(message, dict)
            or set(message) != {"data", "event", "sequence", "sessionId", "timestamp", "version"}
            or type(message.get("version")) is not int
            or message.get("version") != PROTOCOL_VERSION
            or message.get("sessionId") != self._session_id
            or not isinstance(message.get("event"), str)
            or type(message.get("sequence")) is not int
            or type(timestamp) is not int
            or abs(timestamp - now_ms) > CONTROL_TIMESTAMP_SKEW_MS
            or not isinstance(message.get("data"), dict)
        ):
            return None
        return message

    def _attach_clipboard_image_channel(self, channel: RTCDataChannel) -> None:
        self._clipboard_image_channel = channel
        self._incoming_clipboard_image = None

        @channel.on("message")
        def on_message(raw: Any) -> None:
            self._on_clipboard_image_message(raw)

        @channel.on("close")
        def on_close() -> None:
            if self._clipboard_image_channel is channel:
                self._clipboard_image_channel = None
            self._incoming_clipboard_image = None

    def _clipboard_image_message(
        self, event: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "data": data,
            "event": event,
            "sequence": self._next_sequence(),
            "sessionId": self._session_id,
            "timestamp": int(time.time() * 1000),
            "version": PROTOCOL_VERSION,
        }

    def _send_clipboard_image_message(
        self, event: str, data: dict[str, Any]
    ) -> None:
        channel = self._clipboard_image_channel
        if channel is None or channel.readyState != "open":
            return
        try:
            channel.send(json.dumps(self._clipboard_image_message(event, data)))
        except Exception as error:  # noqa: BLE001 - teardown races are benign
            LOGGER.debug(
                "Could not send clipboard image message: %s",
                type(error).__name__,
            )

    @staticmethod
    def _valid_transfer_id(value: Any) -> bool:
        return (
            isinstance(value, str)
            and 16 <= len(value) <= 128
            and all(character.isalnum() or character in "_-" for character in value)
        )

    def _parse_clipboard_image_message(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, str) or len(raw.encode()) > 4096:
            return None
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if (
            not isinstance(message, dict)
            or set(message)
            != {"data", "event", "sequence", "sessionId", "timestamp", "version"}
            or message.get("sessionId") != self._session_id
            or message.get("version") != PROTOCOL_VERSION
            or type(message.get("sequence")) is not int
            or type(message.get("timestamp")) is not int
            or abs(message["timestamp"] - int(time.time() * 1000))
            > CONTROL_TIMESTAMP_SKEW_MS
            or not isinstance(message.get("data"), dict)
        ):
            return None
        event = message.get("event")
        data = message["data"]
        transfer_id = data.get("transferId")
        if not self._valid_transfer_id(transfer_id):
            return None
        if event == "clipboard.image-offer":
            size = data.get("size")
            sha256 = data.get("sha256")
            if (
                set(data) != {"mimeType", "sha256", "size", "transferId"}
                or data.get("mimeType") != "image/png"
                or type(size) is not int
                or not 1 <= size <= CLIPBOARD_IMAGE_MAX_BYTES
                or not isinstance(sha256, str)
                or len(sha256) != 64
                or any(character not in "0123456789abcdef" for character in sha256)
            ):
                return None
        elif event == "clipboard.image-complete":
            if set(data) != {"transferId"}:
                return None
        else:
            return None
        return message

    def _fail_clipboard_image(self, transfer_id: Any, code: str) -> None:
        self._incoming_clipboard_image = None
        if self._valid_transfer_id(transfer_id):
            self._send_clipboard_image_message(
                "clipboard.image-error",
                {"code": code, "transferId": transfer_id},
            )

    def _on_clipboard_image_message(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            incoming = self._incoming_clipboard_image
            if incoming is None:
                return
            chunk = bytes(raw)
            if (
                len(chunk) > MAX_FILE_CHUNK_BYTES
                or len(incoming.chunks) + len(chunk) > incoming.size
            ):
                self._fail_clipboard_image(incoming.transfer_id, "SIZE_MISMATCH")
                return
            incoming.chunks.extend(chunk)
            return

        message = self._parse_clipboard_image_message(raw)
        if message is None:
            return
        event = message["event"]
        data = message["data"]
        transfer_id = data["transferId"]
        if event == "clipboard.image-offer":
            if (
                not self._clipboard_enabled
                or not self._granted_control
                or not self._control_grant_is_active()
            ):
                self._fail_clipboard_image(transfer_id, "CONTROL_INACTIVE")
                return
            if self._incoming_clipboard_image is not None:
                self._send_clipboard_image_message(
                    "clipboard.image-error",
                    {"code": "BUSY", "transferId": transfer_id},
                )
                return
            self._incoming_clipboard_image = IncomingClipboardImage(
                transfer_id=transfer_id,
                size=data["size"],
                sha256=data["sha256"],
                chunks=bytearray(),
            )
            return

        incoming = self._incoming_clipboard_image
        if incoming is None or incoming.transfer_id != transfer_id:
            return
        payload = bytes(incoming.chunks)
        self._incoming_clipboard_image = None
        if not self._granted_control or not self._control_grant_is_active():
            self._fail_clipboard_image(transfer_id, "CONTROL_INACTIVE")
            return
        if len(payload) != incoming.size:
            self._fail_clipboard_image(transfer_id, "SIZE_MISMATCH")
            return
        digest = hashlib.sha256(payload).hexdigest()
        if digest != incoming.sha256:
            self._fail_clipboard_image(transfer_id, "HASH_MISMATCH")
            return
        from .windows_clipboard import write_clipboard_image_png

        if not write_clipboard_image_png(payload):
            self._fail_clipboard_image(transfer_id, "CLIPBOARD_WRITE_FAILED")
            return
        # The monitor will observe this clipboard sequence change. Suppress that
        # one matching image so a viewer upload is not immediately echoed back.
        self._clipboard_image_echo_digest = digest
        self._send_clipboard_image_message(
            "clipboard.image-applied", {"transferId": transfer_id}
        )
        LOGGER.info("Clipboard image set from viewer (%d bytes)", len(payload))

    async def _send_clipboard_image_to_viewer(
        self, png: bytes, sha256: str
    ) -> None:
        channel = self._clipboard_image_channel
        if channel is None or channel.readyState != "open":
            return
        transfer_id = f"clipboard_{os.urandom(12).hex()}"
        try:
            channel.send(
                json.dumps(
                    self._clipboard_image_message(
                        "clipboard.image-offer",
                        {
                            "mimeType": "image/png",
                            "sha256": sha256,
                            "size": len(png),
                            "transferId": transfer_id,
                        },
                    )
                )
            )
            for offset in range(0, len(png), CLIPBOARD_IMAGE_CHUNK_BYTES):
                while channel.bufferedAmount > CLIPBOARD_IMAGE_HIGH_WATER_BYTES:
                    if (
                        self._clipboard_image_channel is not channel
                        or channel.readyState != "open"
                    ):
                        return
                    await asyncio.sleep(0.025)
                channel.send(png[offset : offset + CLIPBOARD_IMAGE_CHUNK_BYTES])
            channel.send(
                json.dumps(
                    self._clipboard_image_message(
                        "clipboard.image-complete",
                        {"transferId": transfer_id},
                    )
                )
            )
            LOGGER.info("Clipboard image sent to viewer (%d bytes)", len(png))
        except Exception as error:  # noqa: BLE001 - channel may close mid-copy
            LOGGER.debug(
                "Could not send clipboard image: %s", type(error).__name__
            )

    def _attach_file_channel(self, channel: RTCDataChannel) -> None:
        self._file_channel = channel

        @channel.on("message")
        def on_message(raw: Any) -> None:
            self._on_file_message(raw)

        @channel.on("close")
        def on_close() -> None:
            if self._file_channel is channel:
                self._file_channel = None
            self._reset_file_transfer()
            self._cancel_download()

    def _file_message(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "data": data,
            "event": event,
            "sequence": self._next_sequence(),
            "sessionId": self._session_id,
            "timestamp": int(time.time() * 1000),
            "version": PROTOCOL_VERSION,
        }

    def _send_file(self, event: str, data: dict[str, Any]) -> None:
        channel = self._file_channel
        if channel is None:
            return
        try:
            channel.send(json.dumps(self._file_message(event, data)))
        except Exception as error:  # noqa: BLE001 - teardown races are benign
            LOGGER.debug("Could not send file message: %s", type(error).__name__)

    def _reset_file_transfer(self) -> None:
        if self._file_receiver is not None:
            self._file_receiver.abort()
        self._file_receiver = None
        self._file_transfer_id = None

    def _on_file_message(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            self._on_file_chunk(bytes(raw))
            return
        message = self._parse_file_message(raw)
        if message is None:
            return
        event = message["event"]
        data = message["data"]
        if event == "file.offer":
            self._on_file_offer(data)
        elif event == "file.complete":
            self._on_file_complete(data)
        elif event == "file.cancel":
            self._reset_file_transfer()
            self._cancel_download()
        elif event == "file.list-request":
            self._on_file_list_request()
        elif event == "file.download":
            self._on_file_download(data)

    def _on_file_offer(self, data: dict[str, Any]) -> None:
        transfer_id = data.get("transferId")
        if not self._files_enabled or self._files_dir is None:
            self._send_file("file.error", {"code": "FILES_DISABLED", "transferId": transfer_id})
            return
        if self._file_receiver is not None:
            self._send_file("file.error", {"code": "BUSY", "transferId": transfer_id})
            return
        receiver = FileReceiver(
            self._files_dir / "Incoming",
            mark_of_the_web=_apply_mark_of_the_web,
        )
        try:
            receiver.begin(data.get("name"), data.get("size"), data.get("sha256"))
        except FileTransferError as error:
            self._send_file("file.error", {"code": error.code, "transferId": transfer_id})
            return
        self._file_receiver = receiver
        self._file_transfer_id = transfer_id
        self._send_file("file.accept", {"transferId": transfer_id})

    def _on_file_chunk(self, chunk: bytes) -> None:
        receiver = self._file_receiver
        if receiver is None:
            return
        if len(chunk) > MAX_FILE_CHUNK_BYTES:
            self._fail_file_transfer("CHUNK_TOO_LARGE")
            return
        try:
            receiver.write_chunk(chunk)
        except FileTransferError as error:
            self._fail_file_transfer(error.code)

    def _on_file_complete(self, data: dict[str, Any]) -> None:
        receiver = self._file_receiver
        transfer_id = self._file_transfer_id
        if receiver is None or data.get("transferId") != transfer_id:
            return
        try:
            result = receiver.finish()
        except FileTransferError as error:
            self._fail_file_transfer(error.code)
            return
        LOGGER.info("Received file into Incoming (%d bytes)", result.size)
        self._send_file(
            "file.done", {"savedAs": result.path.name, "transferId": transfer_id}
        )
        self._file_receiver = None
        self._file_transfer_id = None

    def _fail_file_transfer(self, code: str) -> None:
        transfer_id = self._file_transfer_id
        self._reset_file_transfer()
        self._send_file("file.error", {"code": code, "transferId": transfer_id})

    def _outgoing_dir(self) -> Path | None:
        if not self._files_enabled or self._files_dir is None:
            return None
        return self._files_dir / "Outgoing"

    def _cancel_download(self) -> None:
        self._download_transfer_id = None  # cooperatively stops the stream loop
        task = self._download_task
        self._download_task = None
        if task is not None and not task.done():
            task.cancel()

    def _on_file_list_request(self) -> None:
        outgoing = self._outgoing_dir()
        if outgoing is None:
            self._send_file("file.list", {"files": []})
            return
        try:
            outgoing.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._send_file("file.list", {"files": list_outgoing(outgoing)})

    def _on_file_download(self, data: dict[str, Any]) -> None:
        transfer_id = data.get("transferId")
        outgoing = self._outgoing_dir()
        if outgoing is None:
            self._send_file(
                "file.error", {"code": "FILES_DISABLED", "transferId": transfer_id}
            )
            return
        if self._download_transfer_id is not None:
            self._send_file("file.error", {"code": "BUSY", "transferId": transfer_id})
            return
        path = resolve_outgoing_file(outgoing, data.get("name"))
        if path is None:
            self._send_file(
                "file.error", {"code": "NOT_FOUND", "transferId": transfer_id}
            )
            return
        self._download_transfer_id = transfer_id
        self._download_task = asyncio.ensure_future(
            self._run_download(path, transfer_id)
        )

    async def _run_download(self, path: Path, transfer_id: Any) -> None:
        """Stream one Outgoing file to the viewer as raw binary chunks, framed by
        a download-offer (size up front) and a download-complete (sha256 after).
        The hash is computed in the same single pass that streams the bytes."""
        channel = self._file_channel
        try:
            size = path.stat().st_size
        except OSError:
            self._send_file(
                "file.error", {"code": "NOT_FOUND", "transferId": transfer_id}
            )
            self._download_transfer_id = None
            return
        self._send_file(
            "file.download-offer",
            {"name": path.name, "size": size, "transferId": transfer_id},
        )
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while True:
                    if (
                        channel is None
                        or channel.readyState != "open"
                        or self._download_transfer_id != transfer_id
                    ):
                        return  # cancelled / channel gone
                    chunk = handle.read(FILE_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    while channel.bufferedAmount > FILE_DOWNLOAD_HIGH_WATER_BYTES:
                        await asyncio.sleep(0.02)
                        if channel.readyState != "open":
                            return
                    channel.send(chunk)
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except OSError:
            self._send_file(
                "file.error", {"code": "READ_FAILED", "transferId": transfer_id}
            )
            self._download_transfer_id = None
            return
        self._send_file(
            "file.download-complete",
            {"sha256": digest.hexdigest(), "transferId": transfer_id},
        )
        LOGGER.info("Sent file from Outgoing (%d bytes)", size)
        self._download_transfer_id = None

    def _parse_file_message(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, str) or len(raw.encode()) > MAX_SIGNALING_BYTES:
            return None
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return None
        timestamp = message.get("timestamp") if isinstance(message, dict) else None
        now_ms = int(time.time() * 1000)
        if (
            not isinstance(message, dict)
            or set(message) != {"data", "event", "sequence", "sessionId", "timestamp", "version"}
            or type(message.get("version")) is not int
            or message.get("version") != PROTOCOL_VERSION
            or message.get("sessionId") != self._session_id
            or message.get("event")
            not in {
                "file.offer",
                "file.complete",
                "file.cancel",
                "file.list-request",
                "file.download",
            }
            or type(message.get("sequence")) is not int
            or type(timestamp) is not int
            or abs(timestamp - now_ms) > CONTROL_TIMESTAMP_SKEW_MS
            or not isinstance(message.get("data"), dict)
        ):
            return None
        return message

    def _control_grant_is_active(self) -> bool:
        return (
            self._granted_control
            and self._control_expires_at_ms is not None
            and int(time.time() * 1000) < self._control_expires_at_ms
        )

    def _stop_control_runtime(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._input_controller is not None:
            self._input_controller.release_all()
            self._input_controller = None

    def _revoke_control(self) -> None:
        self._stop_control_runtime()
        self._pending_input_sink = None
        self._granted_control = False
        self._control_expires_at_ms = None

    def _start_clipboard_monitor(self) -> None:
        if (
            not self._clipboard_enabled
            or sys.platform != "win32"
            or self._clipboard_task is not None
        ):
            return
        self._clipboard_task = asyncio.create_task(self._run_clipboard_monitor())

    def _stop_clipboard_monitor(self) -> None:
        if self._clipboard_task is not None:
            self._clipboard_task.cancel()
            self._clipboard_task = None
        self._last_clipboard_text = None
        self._last_clipboard_sequence = None
        self._clipboard_text_echo = None
        self._clipboard_text_echo_sequence = None
        self._clipboard_image_echo_digest = None

    def _forward_clipboard_text(
        self,
        channel: RTCDataChannel,
        sequence: int | None,
        text: str | None,
    ) -> bool:
        """Forward one clipboard text snapshot.

        Returns True only when this text has been intentionally consumed
        (delivered, suppressed as the viewer's own write, or unchanged on a
        platform without a sequence counter). False keeps the change pending so
        the poller retries after a clipboard lock or DataChannel race.
        """
        if text is None or getattr(channel, "readyState", None) != "open":
            return False

        echo_matches = (
            text == self._clipboard_text_echo
            and (
                self._clipboard_text_echo_sequence is None
                or sequence == self._clipboard_text_echo_sequence
            )
        )
        if echo_matches:
            self._clipboard_text_echo = None
            self._clipboard_text_echo_sequence = None
            self._last_clipboard_text = text
            return True

        if (
            self._clipboard_text_echo_sequence is not None
            and sequence != self._clipboard_text_echo_sequence
        ):
            # A different copy happened before the monitor observed the viewer
            # write. The old echo marker must never suppress that later copy.
            self._clipboard_text_echo = None
            self._clipboard_text_echo_sequence = None

        # GetClipboardSequenceNumber is available on supported Windows. Retain
        # content de-duplication only as a defensive fallback when it is not;
        # with a sequence, copying identical text is a new user event.
        if sequence is None and text == self._last_clipboard_text:
            return True

        try:
            channel.send(json.dumps(self._clipboard_message(text)))
        except Exception as error:  # noqa: BLE001 - retry transient channel races
            LOGGER.debug(
                "Could not send clipboard update: %s",
                type(error).__name__,
            )
            return False

        self._last_clipboard_text = text
        LOGGER.info("Clipboard text sent to viewer (%d chars)", len(text))
        return True

    async def _run_clipboard_monitor(self) -> None:
        """Forward new host clipboard text and images to the viewer.

        The Windows clipboard sequence counter is checked before reading or
        encoding an image, keeping the idle CPU cost negligible.
        """
        from .windows_clipboard import (
            get_clipboard_sequence_number,
            read_clipboard_image_png,
            read_clipboard_text,
        )

        # Seed with the current contents so only copies made after the viewer
        # connected are forwarded.
        self._last_clipboard_sequence = get_clipboard_sequence_number()
        self._last_clipboard_text = read_clipboard_text()
        pending_sequence: int | None = None
        unreadable_attempts = 0
        while True:
            await asyncio.sleep(CLIPBOARD_POLL_INTERVAL_SECONDS)
            channel = self._control_channel
            if channel is None:
                return
            sequence = get_clipboard_sequence_number()
            if (
                sequence is not None
                and sequence == self._last_clipboard_sequence
            ):
                continue
            if sequence != pending_sequence:
                pending_sequence = sequence
                unreadable_attempts = 0

            text = read_clipboard_text()
            text_handled = self._forward_clipboard_text(channel, sequence, text)
            if text is not None and not text_handled:
                # Do not consume the sequence until the channel accepts it.
                continue

            clipboard_handled = text_handled

            image_channel = self._clipboard_image_channel
            if image_channel is not None and image_channel.readyState == "open":
                image = read_clipboard_image_png()
                if image is not None:
                    clipboard_handled = True
                    digest = hashlib.sha256(image).hexdigest()
                    if digest == self._clipboard_image_echo_digest:
                        self._clipboard_image_echo_digest = None
                    else:
                        await self._send_clipboard_image_to_viewer(image, digest)

            if clipboard_handled:
                self._last_clipboard_sequence = sequence
                pending_sequence = None
                unreadable_attempts = 0
                continue

            # A lock and an unsupported clipboard format both read as None.
            # Retry long enough for normal owners to release the clipboard, then
            # consume the sequence so file-copy/custom formats do not poll
            # forever.
            unreadable_attempts += 1
            if unreadable_attempts >= CLIPBOARD_READ_RETRY_LIMIT:
                self._last_clipboard_sequence = sequence
                pending_sequence = None
                unreadable_attempts = 0

    def _clipboard_message(self, text: str) -> dict[str, Any]:
        return {
            "data": {"text": text},
            "event": "clipboard.text",
            "sequence": self._next_sequence(),
            "sessionId": self._session_id,
            "timestamp": int(time.time() * 1000),
            "version": PROTOCOL_VERSION,
        }

    def _create_pong(self, raw_control: Any) -> dict[str, Any] | None:
        if not isinstance(raw_control, str) or len(raw_control.encode()) > 4096:
            return None
        try:
            message = json.loads(raw_control)
        except json.JSONDecodeError:
            return None

        if (
            not isinstance(message, dict)
            or set(message) != {"data", "event", "sequence", "sessionId", "timestamp", "version"}
            or message.get("event") != "session.ping"
            or message.get("sessionId") != self._session_id
            or message.get("version") != PROTOCOL_VERSION
            or message.get("data") != {}
            or not isinstance(message.get("sequence"), int)
            or not isinstance(message.get("timestamp"), int)
        ):
            return None

        return {
            "data": {},
            "event": "session.pong",
            "sequence": message["sequence"],
            "sessionId": self._session_id,
            "timestamp": message["timestamp"],
            "version": PROTOCOL_VERSION,
        }

    async def _wait_for_ice_gathering(self, peer: RTCPeerConnection) -> None:
        if peer.iceGatheringState == "complete":
            return

        completed = asyncio.Event()

        @peer.on("icegatheringstatechange")
        def on_ice_gathering_state_change() -> None:
            if peer.iceGatheringState == "complete":
                completed.set()

        try:
            await asyncio.wait_for(
                completed.wait(), timeout=ICE_GATHERING_TIMEOUT_SECONDS
            )
        except TimeoutError:
            # Some adapters never emit the terminal gathering event. The local
            # description still contains candidates gathered so far, including
            # TURN when available, so continue instead of dropping signaling and
            # leaving the viewer waiting for a new negotiation.
            LOGGER.warning(
                "ICE gathering did not complete within %.0fs; using partial candidates",
                ICE_GATHERING_TIMEOUT_SECONDS,
            )

    async def _close_peer(self) -> None:
        # Release any held input first so a teardown never leaves stuck keys.
        self._stop_control_runtime()
        self._stop_clipboard_monitor()
        # Discard any half-written incoming file (temp .part is cleaned up).
        self._reset_file_transfer()
        self._file_channel = None
        self._incoming_clipboard_image = None
        self._clipboard_image_channel = None
        self._control_channel = None
        self._last_control_ping_monotonic = None
        TunedH264Encoder.clear_receiver_network_health()
        video_track = self._video_track
        self._video_track = None
        self._video_sender = None
        if video_track is not None:
            try:
                video_track.stop()
            except Exception as error:  # noqa: BLE001 - continue teardown
                LOGGER.warning(
                    "Video track stop failed during teardown (%s)",
                    type(error).__name__,
                )
        peer = self._peer
        # Detach first: even if aiortc's close coroutine stalls, a new offer
        # must be able to build a replacement peer instead of reusing this one.
        self._peer = None
        if peer is not None:
            try:
                await asyncio.wait_for(
                    peer.close(), timeout=PEER_CLOSE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                LOGGER.warning(
                    "WebRTC peer close timed out after %.0fs; continuing",
                    PEER_CLOSE_TIMEOUT_SECONDS,
                )
            except Exception as error:  # noqa: BLE001 - continue replacement
                LOGGER.warning(
                    "WebRTC peer close failed (%s); continuing",
                    type(error).__name__,
                )
        if self._connected:
            self._notify_status("online")


def _apply_mark_of_the_web(path: Path) -> None:
    """Tag a received file as internet-zone (ZoneId=3) via the NTFS
    Zone.Identifier stream so SmartScreen/Office treat it as downloaded. Windows
    + NTFS only; best-effort (the caller ignores OSError)."""
    if sys.platform != "win32":
        return
    marker = path.with_name(path.name + ":Zone.Identifier")
    with open(marker, "w", encoding="ascii") as stream:
        stream.write("[ZoneTransfer]\nZoneId=3\n")


def load_required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


async def run_agent() -> None:
    heartbeat_stop_value = os.environ.get("MIRROR_HEARTBEAT_STOP_AFTER_SECONDS")
    h264_preset = install_tuned_h264_encoder(
        os.environ.get("MIRROR_H264_PRESET", DEFAULT_H264_PRESET),
        os.environ.get("MIRROR_H264_BACKEND", DEFAULT_H264_BACKEND),
    )
    LOGGER.info(
        "H.264 encoder configured: backend=%s preset=%s",
        TunedH264Encoder.backend,
        h264_preset,
    )
    video_source = os.environ.get("MIRROR_VIDEO_SOURCE", SYNTHETIC_SOURCE)
    if video_source.strip().lower() == DESKTOP_SOURCE:
        # DPI awareness must be set once, before any capture starts.
        configure_dpi_awareness()
    agent = M0Agent(
        AgentConfig(
            device_id=load_required_environment("MIRROR_DEVICE_ID"),
            heartbeat_stop_after_seconds=(
                float(heartbeat_stop_value) if heartbeat_stop_value else None
            ),
            session_id=load_required_environment("MIRROR_SESSION_ID"),
            # Production uses the HMAC device token (MIRROR_DEVICE_TOKEN); local
            # dev uses the dev ticket. Both are presented as ?ticket= on /ws and
            # the Worker verifies by host (dev-ticket locally, device token in
            # production).
            ticket=(
                os.environ.get("MIRROR_DEVICE_TOKEN")
                or load_required_environment("MIRROR_DEV_TICKET")
            ),
            ws_url=os.environ.get("MIRROR_WS_URL", "ws://127.0.0.1:8787/ws"),
            video_source=video_source,
            video_profile=os.environ.get("MIRROR_VIDEO_PROFILE", DEFAULT_VIDEO_PROFILE),
            control_enabled=os.environ.get("MIRROR_CONTROL_ENABLED", "0") == "1",
            clipboard_enabled=os.environ.get("MIRROR_CLIPBOARD_ENABLED", "0") == "1",
            files_enabled=os.environ.get("MIRROR_FILES_ENABLED", "0") == "1",
            files_dir=os.environ.get(
                "MIRROR_FILES_DIR",
                str(Path.home() / DEFAULT_FILES_DIRNAME),
            ),
        )
    )
    emergency_stop_monitor: Any = None
    tray_controller: Any = None
    if sys.platform == "win32":
        from . import keep_awake
        from .windows_emergency_stop import WindowsEmergencyStopMonitor
        from .tray import TrayController

        # Keep the SYSTEM awake for the lifetime of the agent so the home PC
        # never sleeps, while still letting the DISPLAY turn off on its own to
        # save power (woken on demand below when a viewer connects).
        keep_awake.prevent_system_sleep()

        loop = asyncio.get_running_loop()
        agent_task = asyncio.current_task()
        emergency_stop_monitor = WindowsEmergencyStopMonitor(
            lambda: loop.call_soon_threadsafe(agent.emergency_stop)
        )

        last_status = "offline"

        def on_status_change(status: str) -> None:
            nonlocal last_status
            if status in ("viewing", "controlling") and last_status not in (
                "viewing",
                "controlling",
            ):
                keep_awake.wake_display()
            last_status = status
            if tray_controller is not None:
                tray_controller.set_status(status)

        try:
            emergency_stop_monitor.start()
            tray_controller = TrayController(
                control_enabled=agent.control_enabled,
                on_control_change=lambda enabled: loop.call_soon_threadsafe(
                    agent.set_control_enabled, enabled
                ),
                on_emergency_lock=lambda: loop.call_soon_threadsafe(
                    agent.emergency_stop
                ),
                on_restart=lambda: loop.call_soon_threadsafe(
                    _restart_agent, agent_task
                ),
                on_open_folder=lambda: _open_files_folder(agent),
                on_wake_display=keep_awake.wake_display,
                on_quit=lambda: loop.call_soon_threadsafe(
                    _quit_agent, agent_task
                ),
            )
            agent.set_status_listener(on_status_change)
            tray_controller.start()
            LOGGER.warning("Tray ready; Ctrl+Alt+F12 locks remote control")
        except Exception as error:  # noqa: BLE001 - fail closed if local safety UI fails
            LOGGER.error("Local safety UI unavailable; control disabled: %s", error)
            if emergency_stop_monitor is not None:
                emergency_stop_monitor.stop()
            agent.emergency_stop()
            emergency_stop_monitor = None
            tray_controller = None
    try:
        await agent.run_forever()
    except asyncio.CancelledError:
        LOGGER.info("M0 agent shutdown requested")
    finally:
        if emergency_stop_monitor is not None:
            emergency_stop_monitor.stop()
        if tray_controller is not None:
            tray_controller.stop()
        if sys.platform == "win32":
            from . import keep_awake

            keep_awake.allow_sleep()


def _quit_agent(agent_task: "asyncio.Task[None] | None") -> None:
    """Best-effort clean shutdown triggered from the tray thread. Cancelling
    the task running run_forever() unwinds the same way Ctrl+C does."""
    if agent_task is not None and not agent_task.done():
        agent_task.cancel()


def _restart_agent(agent_task: "asyncio.Task[None] | None") -> None:
    """Best-effort restart: the agent normally runs under the Windows
    Scheduled Task `MirrorHostAgent`. Spawn a detached helper that ends and
    re-runs that task, then cleanly shut down this process so the task
    scheduler starts a fresh instance."""
    try:
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
        # End the task, then WAIT for THIS process to actually exit before /Run,
        # so a slow graceful teardown (peer close, tray join) can never overlap a
        # fresh instance. The only interpolated value is our own PID (an int from
        # os.getpid()), never external input — no shell-injection surface.
        pid = os.getpid()
        command = (
            "schtasks /End /TN MirrorHostAgent; "
            f"Wait-Process -Id {pid} -Timeout 15 -ErrorAction SilentlyContinue; "
            "schtasks /Run /TN MirrorHostAgent"
        )
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no external input
            ["powershell", "-NoProfile", "-Command", command],
            creationflags=creationflags,
            close_fds=True,
        )
        LOGGER.info("Restart requested; relaunch helper spawned")
    except Exception as error:  # noqa: BLE001 - best-effort
        LOGGER.warning("Could not spawn restart helper: %s", type(error).__name__)
    _quit_agent(agent_task)


def _open_files_folder(agent: "M0Agent") -> None:
    """Best-effort: open the agent's configured MirrorShare folder (or the
    default location if files are disabled/unset) in Explorer."""
    files_dir = agent._files_dir or (Path.home() / DEFAULT_FILES_DIRNAME)
    try:
        files_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(files_dir)  # noqa: S606 - Windows-only, fixed local path
    except Exception as error:  # noqa: BLE001 - best-effort
        LOGGER.warning("Could not open files folder: %s", type(error).__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        LOGGER.info("M0 agent stopped")


if __name__ == "__main__":
    main()
