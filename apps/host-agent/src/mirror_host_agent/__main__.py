from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import secrets
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
from websockets.exceptions import ConnectionClosedError

from .file_transfer import FileReceiver, FileTransferError
from .file_runtime import FILE_SEND_BYTES, FileFlow, FileReceivePump, disk_call
from .bulk_transfer import BULK_BUFFER_BYTES, BULK_CHUNK_BYTES, wait_for_bulk_capacity
from .delivery_feedback import DeliverySample
from .h264_encoder import (
    DEFAULT_H264_BACKEND,
    DEFAULT_H264_PRESET,
    TunedH264Encoder,
    install_tuned_h264_encoder,
)
from .input_control import InputController, InputSink
from .interactive_runtime import read_clipboard_off_loop
from .control_trace import ControlTrace
from .outgoing import list_outgoing, resolve_outgoing_file
from .text_clipboard import TextClipboardReceiver, TextClipboardDownloadSender
from .stun_transaction import install_safe_stun_transactions
from .turn_transport import install_safe_turn_transport
from .route_diagnostics import RouteDiagnostics, parse_path
from .timing_diagnostics import TimingDiagnostics
from .signaling_health import (ACK_HEADER, ACK_VERSION, ACK_TYPE, HealthClientConnection,
                               SignalingHealth, SignalingAckTimeout)
from .video_pacing import install_video_pacing
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
VIDEO_PACING_ENABLED = True  # Verified bounded TURN-video smoothing; no setting/profile changes.
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
CLIPBOARD_IMAGE_CHUNK_BYTES = BULK_CHUNK_BYTES
CONTROL_TIMESTAMP_SKEW_MS = 30_000
# Per-chunk cap on the file DataChannel — bounds memory and matches the viewer's
# send size. The whole-file cap is enforced by FileReceiver (ADR-014).
MAX_FILE_CHUNK_BYTES = 256 * 1024
DEFAULT_FILES_DIRNAME = "MirrorShare"
# Download (agent -> viewer) streaming: chunk size and the send-buffer high-water
# mark that pauses reading so a slow link cannot blow up memory.
FILE_DOWNLOAD_CHUNK_BYTES = BULK_CHUNK_BYTES
# Auto-reconnect (always-on agent): capped exponential backoff. A connection
# that stayed up at least the stable window resets the backoff.
RECONNECT_INITIAL_DELAY_SECONDS = 2.0
RECONNECT_MAX_DELAY_SECONDS = 60.0
RECONNECT_STABLE_RESET_SECONDS = 60.0
SIGNALING_PING_INTERVAL_SECONDS = 10.0
SIGNALING_PING_TIMEOUT_SECONDS = 30.0
ICE_GATHERING_TIMEOUT_SECONDS = 10.0

__all__ = ["AgentConfig", "M0Agent", "SyntheticVideoTrack", "main", "run_agent"]


class SignalingValidationError(ValueError):
    """Fixed internal category only; never include received message contents."""
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _signaling_error_details(error: Exception) -> tuple[int | None, int | None, int | None, str]:
    """Log only numeric protocol metadata, never a ticket/URL/header/body/reason."""
    def numeric(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    status = numeric(getattr(getattr(error, "response", None), "status_code", None))
    received = numeric(getattr(getattr(error, "rcvd", None), "code", None))
    sent = numeric(getattr(getattr(error, "sent", None), "code", None))
    categories = {401: "authentication_rejected", 403: "forbidden", 409: "agent_already_online", 429: "rate_limited"}
    category = categories.get(status, "network_or_protocol")
    if status is not None and status >= 500:
        category = "signaling_server_error"
    elif getattr(getattr(error, "sent", None), "reason", None) == "keepalive ping timeout":
        category = "keepalive_timeout"
    elif isinstance(error, SignalingValidationError):
        category = error.category
    elif isinstance(error, SignalingAckTimeout):
        category = 'application_ack_timeout'
    elif isinstance(error, ConnectionClosedError) and received is None and sent is None:
        category = 'abnormal_eof'
    return status, received, sent, category


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
        # Stable across retries, fresh for every process. Never persisted/logged.
        self._instance_id = secrets.token_urlsafe(32)
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
        self._video_pacing = None
        self._sender_stats_task: asyncio.Task[None] | None = None
        self._sender_byte_sample: tuple[float, int] | None = None
        self._delivery_v2_logged = False
        self._route_diagnostics = RouteDiagnostics()
        self._timing_diagnostics = TimingDiagnostics()
        self._viewer_path = None
        self._viewer_path_at = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        # Ephemeral per-connection correlation only, distinct from all auth,
        # device and session identities. Never log URLs/headers or payloads.
        self._signaling_link_id: str | None = None
        self._signaling_heartbeat_sent_at: float | None = None
        self._signaling_max_loop_lag_ms = 0.0
        self._signaling_health = SignalingHealth()
        # Per-session control state; set on grant, cleared on teardown.
        self._granted_control = False
        self._control_expires_at_ms: int | None = None
        self._pending_input_sink: InputSink | None = None
        self._video_track: Any = None
        self._input_controller: InputController | None = None
        self._input_runtime_sink = None
        self._watchdog_task: asyncio.Task[None] | None = None
        # Legacy text polling stays on control-v1; bulk text and image bytes
        # use clipboard-v1 so screenshots never delay mouse/keyboard input.
        self._clipboard_enabled = config.clipboard_enabled
        self._control_channel: RTCDataChannel | None = None
        self._control_trace = None
        self._last_control_ping_monotonic: float | None = None
        self._clipboard_image_channel: RTCDataChannel | None = None
        self._clipboard_text_channel: RTCDataChannel | None = None
        self._clipboard_text_receiver: TextClipboardReceiver | None = None
        self._clipboard_text_download_channel: RTCDataChannel | None = None
        self._clipboard_text_download_sender: TextClipboardDownloadSender | None = None
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
        self._file_receive_pump: FileReceivePump | None = None
        self._file_io_tasks: set[asyncio.Task] = set()
        self._download_flow: FileFlow | None = None
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
                failure: Exception | None = None
                try:
                    await self.run()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - reconnect on any drop
                    failure = error
                if time.monotonic() - started_at >= RECONNECT_STABLE_RESET_SECONDS:
                    delay = RECONNECT_INITIAL_DELAY_SECONDS
                if failure is not None:
                    status, received, sent, category = _signaling_error_details(failure)
                    cause = failure.__cause__
                    LOGGER.warning(
                        "Signaling connection lost (%s); retrying in %.0fs "
                        "status=%s close_received=%s close_sent=%s category=%s peer_preserved=%s "
                        "cause_type=%s errno=%s winerror=%s",
                        type(failure).__name__,
                        delay,
                        status, received, sent, category, self._peer_is_healthy(),
                        type(cause).__name__ if cause is not None else None,
                        getattr(cause, "errno", None) if isinstance(cause, OSError) else None,
                        getattr(cause, "winerror", None) if isinstance(cause, OSError) else None,
                    )
                else:
                    LOGGER.warning("Signaling connection closed; retrying in %.0fs", delay)
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
        self._signaling_link_id = secrets.token_hex(8)
        self._signaling_heartbeat_sent_at = None
        self._signaling_max_loop_lag_ms = 0.0
        query = urlencode({"ticket": self._ticket})
        websocket_url = f"{self._ws_url}?{query}"
        async with connect(
            websocket_url,
            max_size=MAX_SIGNALING_BYTES,
            origin=None,
            additional_headers={"x-mirror-agent-instance": self._instance_id,
                                "x-mirror-signaling-link": self._signaling_link_id,
                                ACK_HEADER: ACK_VERSION},
            ping_interval=SIGNALING_PING_INTERVAL_SECONDS,
            ping_timeout=SIGNALING_PING_TIMEOUT_SECONDS,
            create_connection=HealthClientConnection,
        ) as websocket:
            self._signaling_health = SignalingHealth(enabled=getattr(websocket,'app_ack_supported',False) is True)
            LOGGER.info('Signaling liveness: %s; heartbeat=3s timeout=30s; legacy ping=10s/30s',
                        'app-ack-v1' if self._signaling_health.enabled else 'legacy-pong-v1')
            if self._signaling_health.enabled:
                LOGGER.info('Signaling cleanup policy: abort-expired-ack-v1; timeout=30s; healthy media unchanged')
            self._websocket = websocket
            self._connected = True
            status = "online"
            if self._peer_is_healthy():
                status = "controlling" if self._granted_control else "viewing"
            self._notify_status(status)
            connected_at = time.monotonic()
            LOGGER.info("M0 agent connected to local signaling link=%s", self._signaling_link_id)
            receiver: asyncio.Task[None] | None = None
            health_task: asyncio.Task[None] | None = None
            try:
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
                receiver = asyncio.create_task(self._receive_signaling(websocket))
                watched = {receiver, self._heartbeat_task}
                if self._signaling_health.enabled:
                    health_task=asyncio.create_task(self._watch_signaling_health())
                    watched.add(health_task)
                # A failed heartbeat sender must not leave recv() silently
                # waiting on a half-open socket until a later ping timeout.
                done, _ = await asyncio.wait(
                    watched, return_when=asyncio.FIRST_COMPLETED,
                )
                # A watchdog abort may also wake recv/send in the same loop
                # tick. Preserve the diagnosed timeout rather than reporting
                # its intentional TCP abort as an unexplained external EOF.
                if health_task is not None and health_task in done:
                    await health_task
                if self._heartbeat_task in done:
                    # Normal return is deliberate heartbeat-stop test mode;
                    # retain that behavior, but immediately propagate errors.
                    await self._heartbeat_task
                    if receiver not in done and health_task is not None:
                        done,_=await asyncio.wait({receiver,health_task},return_when=asyncio.FIRST_COMPLETED)
                if health_task is not None and health_task in done:
                    await health_task
                await receiver
            finally:
                if health_task is not None:
                    health_task.cancel()
                    await asyncio.gather(health_task,return_exceptions=True)
                if receiver is not None:
                    if not receiver.done():
                        receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
                await self._stop_heartbeats()
                preserve_peer = self._peer_is_healthy()
                if not preserve_peer:
                    await self._close_peer()
                self._connected = False
                self._websocket = None
                now = time.monotonic()
                heartbeat_age = (now-self._signaling_heartbeat_sent_at
                                 if self._signaling_heartbeat_sent_at is not None else -1.0)
                latency = getattr(websocket, "latency", None)
                ping_ms = (float(latency)*1000 if not self._signaling_health.enabled and isinstance(latency, (int, float))
                           and math.isfinite(latency) else -1.0)
                LOGGER.info(
                    "Signaling transport ended link=%s age_s=%.1f heartbeat_age_s=%.1f "
                    "max_loop_lag_ms=%.1f ping_rtt_ms=%.1f peer_preserved=%s",
                    self._signaling_link_id, now-connected_at, heartbeat_age,
                    self._signaling_max_loop_lag_ms, ping_ms, preserve_peer,
                )
                if self._signaling_health.enabled:
                    LOGGER.info('Signaling ACK health: age_s=%.1f rtt_ms=%s pending=%s',
                        max(0,now-self._signaling_health.last_ack_at),
                        None if self._signaling_health.last_rtt_ms is None else round(self._signaling_health.last_rtt_ms,1),
                        len(self._signaling_health.pending))
                if preserve_peer:
                    LOGGER.info(
                        "Signaling offline; active WebRTC/control session preserved"
                    )
                    self._notify_status(
                        "controlling" if self._granted_control else "viewing"
                    )
                else:
                    self._notify_status("offline")

    async def _receive_signaling(self, websocket: Any) -> None:
        async for raw_message in websocket:
            await self._handle_message(websocket, raw_message)

    async def _stop_heartbeats(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        # Retrieve an already-failed send task too; cancellation alone leaves
        # its exception unobserved during reconnect. Do not swallow cancellation
        # of this parent task, or expose exception text containing credentials.
        results = await asyncio.gather(task, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                status, received, sent, category = _signaling_error_details(result)
                LOGGER.warning("Signaling heartbeat stopped (%s) status=%s close_received=%s close_sent=%s category=%s",
                               type(result).__name__, status, received, sent, category)

    async def _send_heartbeats(self, websocket: Any) -> None:
        started_at = time.monotonic()
        while True:
            due_at = time.monotonic() + 3.0
            await asyncio.sleep(3)
            loop_lag_ms = max(0.0, time.monotonic()-due_at)*1000
            self._signaling_max_loop_lag_ms = max(self._signaling_max_loop_lag_ms, loop_lag_ms)
            if loop_lag_ms >= 50:
                LOGGER.info('Host loop scheduling: heartbeat_lag_ms=%.1f; diagnostic-only', loop_lag_ms)
            if (
                self._heartbeat_stop_after_seconds is not None
                and time.monotonic() - started_at
                >= self._heartbeat_stop_after_seconds
            ):
                LOGGER.info("M0 test mode stopped application heartbeats")
                return
            message=self._message('agent.heartbeat',{})
            self._signaling_health.sent(message)
            await asyncio.wait_for(websocket.send(json.dumps(message)),timeout=SIGNALING_PING_TIMEOUT_SECONDS)
            self._signaling_heartbeat_sent_at = time.monotonic()

    async def _watch_signaling_health(self) -> None:
        while True:
            await asyncio.sleep(3)
            if self._signaling_health.expired(timeout=SIGNALING_PING_TIMEOUT_SECONDS):
                abort = getattr(self._websocket, 'abort_expired_ack', None)
                if callable(abort):
                    abort()
                raise SignalingAckTimeout('Application heartbeat acknowledgement expired')

    async def _handle_message(self, websocket: Any, raw_message: Any) -> None:
        if not isinstance(raw_message, str) or len(raw_message.encode()) > MAX_SIGNALING_BYTES:
            raise SignalingValidationError("invalid_signaling_transport")

        message = json.loads(raw_message)
        if not self._is_valid_envelope(message):
            raise SignalingValidationError("invalid_signaling_envelope")

        message_type = message["type"]
        if message_type == ACK_TYPE:
            # Correlate even if a viewer session was adopted since this heartbeat
            # was sent. A delayed ACK must never adopt/change the active session.
            first_ack=self._signaling_health.last_ack_sequence<0
            if self._signaling_health.accept(message) and first_ack:
                LOGGER.info('Signaling application ACK active: rtt_ms=%.1f; bounded watchdog=30s',
                            self._signaling_health.last_rtt_ms)
            return
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
            # Late close/ICE/configure frames from a previous viewer are not a
            # transport failure. Fail closed for this message, not the healthy
            # agent socket/peer. Never adopt a session except on session.request.
            LOGGER.info("Signaling ignored stale-session message")
            return

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
            raise SignalingValidationError("invalid_sdp_offer")

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
        self._video_pacing = install_video_pacing(self._video_sender, lambda:self._video_profile.name,
                                                  pace=VIDEO_PACING_ENABLED)

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
            if channel.label == "clipboard-text-v1":
                self._attach_clipboard_text_channel(channel)
                return
            if channel.label == "clipboard-text-receive-v1":
                self._attach_clipboard_text_download_channel(channel)
                return
            if channel.label != "control-v1":
                channel.close()
                return

            self._control_channel = channel
            try:
                if self._control_trace is not None:
                    self._control_trace.close()
                self._control_trace = ControlTrace(channel)
                self._control_trace.install()
            except Exception:
                if self._control_trace is not None:
                    self._control_trace.close()
                self._control_trace = None
                LOGGER.info('Control message diagnostics unavailable; control unchanged')
            self._last_control_ping_monotonic = None
            self._start_clipboard_monitor()

            @channel.on("message")
            def on_message(raw_control: Any) -> None:
                self._on_control_message(channel, raw_control)

            @channel.on("close")
            def on_close() -> None:
                self._stop_clipboard_monitor()
                if self._control_channel is channel:
                    if self._control_trace is not None:
                        self._control_trace.close()
                        self._control_trace = None
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
        self._input_runtime_sink = sink
        def failed():
            if self._input_runtime_sink is sink:
                LOGGER.warning('Input worker unavailable; viewer downgraded to view-only')
                self._revoke_control()
                self._queue_policy_update()
        handler = getattr(sink, 'set_failure_handler', None)
        if callable(handler): handler(failed)
        start = getattr(sink, 'start', None)
        try:
            if callable(start): start()
        except Exception as error:
            LOGGER.warning('Input worker start failed: type=%s; staying view-only', type(error).__name__)
            close = getattr(sink, 'close', None)
            if callable(close): close()
            self._revoke_control()
            self._queue_policy_update()
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
        received_at = time.perf_counter()
        if self._granted_control and not self._control_grant_is_active():
            self._revoke_control()
            self._queue_policy_update()
            LOGGER.info("Expired control input rejected; viewer notified")
        pong = self._create_pong(raw_control)
        if pong is not None:
            trace = self._control_trace
            if trace is not None and trace.channel is channel:
                trace.safe(trace.queue_pong,pong['sequence'],received_at)
            self._last_control_ping_monotonic = time.monotonic()
            # Keep an authenticated, responsive control session alive. The grant
            # remains a dead-man lease: if the viewer or DataChannel disappears,
            # pings stop and the watchdog still revokes input after the TTL.
            if self._control_grant_is_active():
                self._control_expires_at_ms = (
                    int(time.time() * 1000) + CONTROL_GRANT_TTL_MS
                )
            channel.send(json.dumps(pong))
            if trace is not None and trace.channel is channel:
                trace.safe(trace.handled,pong['sequence'],received_at)
            return
        message = self._parse_control(raw_control)
        if message is not None and message['event'] == 'video.timing-report':
            # Does not renew control lease or call quality/input/routing code.
            try:
                self._timing_diagnostics.observe(message['data'])
            except Exception:
                pass  # Optional diagnostics cannot interrupt control/media.
            return
        if message is not None and message['event'] == 'video.path-report':
            self._handle_path_report(message['data'])
            return
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
        trace = self._control_trace
        if trace is None:
            controller.handle(message, now=time.monotonic())
        else:
            trace.process_input(controller,message,received_at)

    def _handle_receiver_report(self, data: dict[str, Any]) -> None:
        required = {
            "jitterMs",
            "packetLossPercent",
            "receivedBitrateMbps",
            "route",
        }
        if set(data) not in (required, required | {"delivery"}):
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
            delivery = DeliverySample.parse(data['delivery']) if 'delivery' in data else None
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
        # Diagnostic side effects cannot prevent existing quality feedback.
        try:
            fresh_path = (self._viewer_path if self._viewer_path_at is not None and
                0 <= time.monotonic()-self._viewer_path_at <= 15 else None)
            self._route_diagnostics.observe(self._video_sender, fresh_path, delivery=delivery,
                jitter=jitter_ms, loss=packet_loss_percent, mbps=received_bitrate)
        except Exception:
            pass
        TunedH264Encoder.update_receiver_network_health(
            jitter_ms=jitter_ms,
            packet_loss_percent=packet_loss_percent,
            route=route,
            **({'delivery': delivery} if delivery is not None else {}),
        )
        if delivery is not None and delivery.received_bytes is not None:
            if not self._delivery_v2_logged:
                LOGGER.info('Receiver delivery telemetry: counts-v2 active; corrected-loss/bytes/control-RTT; settings unchanged')
                self._delivery_v2_logged = True
            if (self._video_sender is not None and
                    (self._sender_stats_task is None or self._sender_stats_task.done()) and
                    (self._sender_byte_sample is None or time.monotonic()-self._sender_byte_sample[0] >= .5)):
                self._sender_stats_task = asyncio.create_task(self._sample_sender_delivery(self._video_sender))

    def _handle_path_report(self, data: dict[str, Any]) -> None:
        try:
            self._viewer_path = parse_path(data)
            self._viewer_path_at = time.monotonic()
        except ValueError:
            return

    async def _sample_sender_delivery(self, sender: RTCRtpSender) -> None:
        try:
            stats = await asyncio.wait_for(sender.getStats(), timeout=.5)
            if sender is not self._video_sender:
                return
            total = next((r.bytesSent for r in stats.values()
                          if r.type == 'outbound-rtp' and r.kind == 'video'), None)
            if not isinstance(total,int) or isinstance(total,bool) or total < 0:
                return
            now = time.monotonic()
            before = self._sender_byte_sample
            self._sender_byte_sample = (now,total)
            if before is not None and .25 <= now-before[0] <= 3 and total >= before[1]:
                TunedH264Encoder.update_sender_delivery(sent_bps=(total-before[1])*8/(now-before[0]),now=now)
        except Exception:
            # Optional diagnostics must never disturb media or input.
            return

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
            truncate_clipboard_text,
            get_clipboard_sequence_number,
            write_clipboard_text,
        )

        payload = truncate_clipboard_text(text, 16_384)
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

    def _attach_clipboard_text_channel(self, channel: RTCDataChannel) -> None:
        if self._clipboard_text_channel is not None:
            channel.close()
            return
        self._clipboard_text_channel = channel

        def reply(event: str, data: dict[str, Any]) -> None:
            if self._clipboard_text_channel is channel and channel.readyState == "open":
                try:
                    channel.send(json.dumps(self._clipboard_image_message(event, data)))
                except Exception:  # teardown race; no text content is logged
                    pass

        receiver = TextClipboardReceiver(
            self._session_id,
            allowed=lambda: self._clipboard_enabled and self._granted_control
            and self._control_grant_is_active(),
            write=self._write_bulk_clipboard_text,
            reply=reply,
        )
        self._clipboard_text_receiver = receiver

        @channel.on("message")
        def on_text_message(raw: Any) -> None:
            if self._clipboard_text_channel is channel:
                receiver.receive(raw)

        @channel.on("close")
        def on_text_close() -> None:
            receiver.close()
            if self._clipboard_text_channel is channel:
                self._clipboard_text_channel = None
                self._clipboard_text_receiver = None

    def _write_bulk_clipboard_text(self, text: str) -> bool:
        from .windows_clipboard import (
            truncate_clipboard_text,
            get_clipboard_sequence_number,
            write_clipboard_text,
        )
        now = time.monotonic()
        last = self._last_clipboard_set_monotonic
        if last is not None and now - last < CLIPBOARD_SET_MIN_INTERVAL_SECONDS:
            return False
        if not write_clipboard_text(text):
            return False
        self._last_clipboard_set_monotonic = now
        self._clipboard_text_echo = truncate_clipboard_text(text)
        self._clipboard_text_echo_sequence = get_clipboard_sequence_number()
        LOGGER.info("Bulk clipboard text applied (%d chars)", len(text))
        return True

    def _attach_clipboard_text_download_channel(self, channel: RTCDataChannel) -> None:
        if self._clipboard_text_download_channel is not None:
            channel.close()
            return
        self._clipboard_text_download_channel = channel
        sender = TextClipboardDownloadSender(self._session_id, channel,
            lambda event, data: json.dumps(self._clipboard_image_message(event, data)),
            lambda: self._clipboard_enabled and self._clipboard_text_download_channel is channel
                and self._control_channel is not None and self._control_channel.readyState == 'open',
            self._wait_for_bulk_capacity)
        self._clipboard_text_download_sender = sender
        @channel.on('message')
        def on_message(raw: Any) -> None:
            if self._clipboard_text_download_channel is channel:
                sender.receive(raw)
        @channel.on('close')
        def on_close() -> None:
            sender.close()
            if self._clipboard_text_download_channel is channel:
                self._clipboard_text_download_channel = None
                self._clipboard_text_download_sender = None

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
                chunk = png[offset : offset + CLIPBOARD_IMAGE_CHUNK_BYTES]
                if not await self._wait_for_bulk_capacity(
                    channel, len(chunk), lambda: self._clipboard_image_channel is channel,
                ):
                    if self._clipboard_image_channel is channel and channel.readyState == "open":
                        self._send_clipboard_image_message(
                            "clipboard.image-error", {"code": "TIMEOUT", "transferId": transfer_id},
                        )
                    return
                channel.send(chunk)
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

    async def _wait_for_bulk_capacity(
        self, channel: RTCDataChannel, size: int, is_active: Callable[[], bool],
    ) -> bool:
        return await wait_for_bulk_capacity(
            channel, size, is_active,
            lambda: (self._file_channel, self._clipboard_image_channel, self._clipboard_text_channel,
                     self._clipboard_text_download_channel),
            lambda: self._control_channel,
        )

    def _attach_file_channel(self, channel: RTCDataChannel) -> None:
        self._file_channel = channel

        @channel.on("message")
        def on_message(raw: Any) -> None:
            if self._file_channel is channel:
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
        if self._file_receive_pump is not None:
            self._file_receive_pump.close()
        self._file_receive_pump = None
        if self._file_receiver is not None:
            self._file_receiver.abort()
        self._file_receiver = None
        self._file_transfer_id = None

    def _track_file_task(self, task: asyncio.Task) -> None:
        self._file_io_tasks.add(task)
        def completed(finished):
            self._file_io_tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                LOGGER.warning('File worker stopped: type=%s', type(finished.exception()).__name__)
        task.add_done_callback(completed)

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
            if data.get('transferId') == self._file_transfer_id:
                self._reset_file_transfer()
            if data.get('transferId') == self._download_transfer_id:
                self._cancel_download()
        elif event == "file.list-request":
            self._on_file_list_request()
        elif event == "file.download":
            self._on_file_download(data)
        elif event == 'file.progress':
            if (set(data) == {'received', 'transferId'} and
                    data.get('transferId') == self._download_transfer_id and self._download_flow is not None):
                self._download_flow.acknowledge(data.get('received'))

    def _on_file_offer(self, data: dict[str, Any]) -> None:
        transfer_id = data.get("transferId")
        if not self._files_enabled or self._files_dir is None:
            self._send_file("file.error", {"code": "FILES_DISABLED", "transferId": transfer_id})
            return
        if self._file_receiver is not None or self._file_receive_pump is not None or len(self._file_io_tasks) >= 4:
            self._send_file("file.error", {"code": "BUSY", "transferId": transfer_id})
            return
        receiver = FileReceiver(
            self._files_dir / "Incoming",
            mark_of_the_web=_apply_mark_of_the_web,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # Synchronous embedding/tests; production always has a loop.
        else:
            channel = self._file_channel
            credit = data.get('flowControl') == 'credit-v1'
            def active():
                return self._file_receive_pump is pump and self._file_channel is channel
            def accepted():
                if active():
                    LOGGER.info('File receipt window: credit-file-v1 negotiated=%s', credit)
                    self._send_file('file.accept', {'transferId': transfer_id,
                        **({'flowControl': 'credit-v1'} if credit else {})})
            def progress(received):
                if active() and credit:
                    self._send_file('file.progress', {'transferId': transfer_id, 'received': received})
            def done(result):
                if active():
                    LOGGER.info('Received file into Incoming (%d bytes)', result.size)
                    self._send_file('file.done', {'savedAs': result.path.name, 'transferId': transfer_id})
                    self._file_receive_pump = None
                    self._file_transfer_id = None
            def failed(code):
                if active():
                    self._fail_file_transfer(code)
            pump = FileReceivePump(receiver, data.get('name'), data.get('size'), data.get('sha256'),
                                   accepted, progress, done, failed)
            self._file_receive_pump = pump
            self._file_transfer_id = transfer_id
            self._track_file_task(pump.task)
            return
        try:
            receiver.begin(data.get("name"), data.get("size"), data.get("sha256"))
        except FileTransferError as error:
            self._send_file("file.error", {"code": error.code, "transferId": transfer_id})
            return
        self._file_receiver = receiver
        self._file_transfer_id = transfer_id
        self._send_file("file.accept", {"transferId": transfer_id})

    def _on_file_chunk(self, chunk: bytes) -> None:
        pump = self._file_receive_pump
        if pump is not None:
            if len(chunk) > MAX_FILE_CHUNK_BYTES:
                self._fail_file_transfer('CHUNK_TOO_LARGE')
            elif not pump.push(chunk):
                self._fail_file_transfer('RECEIVE_BUSY')
            return
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
        pump = self._file_receive_pump
        if pump is not None:
            if data.get('transferId') == self._file_transfer_id:
                pump.complete()
            return
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
        self._download_flow = None
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
        if self._download_transfer_id is not None or len(self._file_io_tasks) >= 4:
            self._send_file("file.error", {"code": "BUSY", "transferId": transfer_id})
            return
        path = resolve_outgoing_file(outgoing, data.get("name"))
        if path is None:
            self._send_file(
                "file.error", {"code": "NOT_FOUND", "transferId": transfer_id}
            )
            return
        self._download_transfer_id = transfer_id
        self._download_flow = FileFlow(data.get('flowControl') == 'credit-v1')
        self._download_task = asyncio.ensure_future(
            self._run_download(path, transfer_id)
        )
        self._track_file_task(self._download_task)

    async def _run_download(self, path: Path, transfer_id: Any) -> None:
        """Stream one Outgoing file to the viewer as raw binary chunks, framed by
        a download-offer (size up front) and a download-complete (sha256 after).
        The hash is computed in the same single pass that streams the bytes."""
        channel = self._file_channel
        flow = self._download_flow or FileFlow()
        try:
            size = await disk_call(lambda: path.stat().st_size)
        except OSError:
            if self._download_transfer_id == transfer_id:
                self._send_file('file.error', {'code': 'NOT_FOUND', 'transferId': transfer_id})
                self._download_transfer_id = None
            return
        if self._download_transfer_id != transfer_id or self._file_channel is not channel:
            return
        self._send_file(
            "file.download-offer",
            {"name": path.name, "size": size, "transferId": transfer_id,
             **({'flowControl': 'credit-v1'} if flow.credit else {})},
        )
        digest = hashlib.sha256()
        holder = {}
        def open_file():
            holder['handle'] = path.open('rb')
        try:
            await disk_call(open_file)
            handle = holder['handle']
            def active():
                return (channel is not None and channel.readyState == 'open' and
                        self._file_channel is channel and self._download_transfer_id == transfer_id)
            while active():
                chunk = await disk_call(handle.read, min(FILE_DOWNLOAD_CHUNK_BYTES, FILE_SEND_BYTES))
                if not chunk:
                    break
                digest.update(chunk)
                if (not await flow.wait(len(chunk), active) or
                        not await self._wait_for_bulk_capacity(channel, len(chunk), active)):
                    if active():
                        self._send_file('file.error', {'code': 'TIMEOUT', 'transferId': transfer_id})
                        self._download_transfer_id = None
                    return
                if not active():
                    return
                channel.send(chunk)
                flow.mark_sent(len(chunk))
        except asyncio.CancelledError:
            raise
        except OSError:
            if self._download_transfer_id == transfer_id:
                self._send_file('file.error', {'code': 'READ_FAILED', 'transferId': transfer_id})
                self._download_transfer_id = None
            return
        finally:
            if holder.get('handle') is not None:
                await disk_call(holder['handle'].close)
        if self._download_transfer_id != transfer_id or self._file_channel is not channel:
            return
        self._send_file(
            "file.download-complete",
            {"sha256": digest.hexdigest(), "transferId": transfer_id},
        )
        LOGGER.info("Sent file from Outgoing (%d bytes)", size)
        LOGGER.info('File send diagnostics: credit-file-v1 credit=%s bytes=%s acknowledged=%s peak_inflight=%s',
                    flow.credit, flow.sent, flow.received, flow.peak)
        self._download_transfer_id = None
        self._download_flow = None

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
                "file.progress",
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
        self._input_runtime_sink = None
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._input_controller is not None:
            self._input_controller.close()
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

    def _clipboard_text_was_consumed(
        self,
        channel: RTCDataChannel,
        sequence: int | None,
        text: str | None,
    ) -> bool:
        """Suppress only our own write or an unchanged sequence-less snapshot.

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
        return False

    def _forward_clipboard_text(self, channel: RTCDataChannel, sequence: int | None, text: str | None) -> bool:
        if text is None or getattr(channel, 'readyState', None) != 'open':
            return False
        if self._clipboard_text_was_consumed(channel, sequence, text):
            return True
        from .windows_clipboard import truncate_clipboard_text
        # Compatibility for old viewers; never enlarge control-v1/SCTP frames.
        legacy = truncate_clipboard_text(text, 16_384)

        try:
            channel.send(json.dumps(self._clipboard_message(legacy)))
        except Exception as error:  # noqa: BLE001 - retry transient channel races
            LOGGER.debug(
                "Could not send clipboard update: %s",
                type(error).__name__,
            )
            return False

        self._last_clipboard_text = text
        LOGGER.info("Clipboard text sent to viewer (%d chars)", len(legacy))
        return True

    async def _forward_clipboard_text_snapshot(self, channel, sequence, text) -> bool:
        sender = self._clipboard_text_download_sender
        download = self._clipboard_text_download_channel
        if text is None or len(text.encode('utf-16-le'))//2 <= 16_384 or sender is None or download is None or download.readyState != 'open':
            return self._forward_clipboard_text(channel, sequence, text)
        if self._clipboard_text_was_consumed(channel, sequence, text):
            return True
        try:
            await sender.send(text)
        except Exception as error:
            LOGGER.debug('Could not send bulk clipboard text: %s', type(error).__name__)
            return False
        self._last_clipboard_text = text
        LOGGER.info('Bulk clipboard text sent to viewer (%d UTF-16 units)', len(text.encode('utf-16-le'))//2)
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
            has_clipboard_image_format,
        )

        # Seed with the current contents so only copies made after the viewer
        # connected are forwarded.
        self._last_clipboard_sequence = get_clipboard_sequence_number()
        self._last_clipboard_text = await read_clipboard_off_loop(read_clipboard_text)
        pending_sequence: int | None = None
        unreadable_attempts = 0
        pending_text_handled = False
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
                pending_text_handled = False

            text = await read_clipboard_off_loop(read_clipboard_text)
            if self._control_channel is not channel:
                return
            if sequence is not None and get_clipboard_sequence_number() != sequence:
                continue
            image_channel = self._clipboard_image_channel
            image_ready = image_channel is not None and image_channel.readyState == "open"
            image = None
            image_expected = False
            if image_channel is not None:
                image_expected = has_clipboard_image_format() is not False
                if image_ready:
                    # Office delayed rendering/GDI/PNG encoding must not block
                    # the media encoder, input heartbeat or signaling loop.
                    image = await asyncio.to_thread(read_clipboard_image_png)
                    if sequence is not None and get_clipboard_sequence_number() != sequence:
                        continue  # A newer copy replaced the snapshot while decoding.
            text_handled = pending_text_handled or await self._forward_clipboard_text_snapshot(channel, sequence, text)
            pending_text_handled = text_handled
            if text is not None and not text_handled:
                # Do not consume the sequence until the channel accepts it.
                continue

            clipboard_handled = text_handled

            if image_ready:
                if image is not None:
                    clipboard_handled = True
                    digest = hashlib.sha256(image).hexdigest()
                    if digest == self._clipboard_image_echo_digest:
                        self._clipboard_image_echo_digest = None
                    else:
                        await self._send_clipboard_image_to_viewer(image, digest)

            # A text representation must not consume an Office copy before its
            # advertised image becomes available. Text is sent only once while
            # retrying; the same bounded retry budget still applies.
            if clipboard_handled and not (image_expected and image is None):
                self._last_clipboard_sequence = sequence
                pending_sequence = None
                unreadable_attempts = 0
                pending_text_handled = False
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
                pending_text_handled = False

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
        if self._control_trace is not None:
            self._control_trace.close()
            self._control_trace = None
        # Release any held input first so a teardown never leaves stuck keys.
        self._stop_control_runtime()
        self._stop_clipboard_monitor()
        # Discard any half-written incoming file (temp .part is cleaned up).
        self._reset_file_transfer()
        self._file_channel = None
        if self._file_io_tasks:
            await asyncio.wait(tuple(self._file_io_tasks), timeout=.5)
        self._incoming_clipboard_image = None
        self._clipboard_image_channel = None
        if self._clipboard_text_receiver is not None:
            self._clipboard_text_receiver.close()
        self._clipboard_text_receiver = None
        self._clipboard_text_channel = None
        if self._clipboard_text_download_sender is not None:
            self._clipboard_text_download_sender.close()
        self._clipboard_text_download_sender = None
        self._clipboard_text_download_channel = None
        self._control_channel = None
        self._last_control_ping_monotonic = None
        TunedH264Encoder.clear_receiver_network_health()
        if self._sender_stats_task is not None:
            self._sender_stats_task.cancel()
            await asyncio.gather(self._sender_stats_task, return_exceptions=True)
        self._sender_stats_task = None
        self._sender_byte_sample = None
        self._delivery_v2_logged = False
        self._route_diagnostics = RouteDiagnostics()
        self._timing_diagnostics = TimingDiagnostics()
        self._viewer_path = None
        self._viewer_path_at = None
        video_track = self._video_track
        self._video_track = None
        self._video_sender = None
        if self._video_pacing is not None:
            self._video_pacing.close()
            await self._video_pacing.wait_closed()
            self._video_pacing = None
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
    install_safe_stun_transactions()
    install_safe_turn_transport()
    LOGGER.info("Signaling transport policy: supervised-v1; heartbeat=3s ping=10s/30s; media settings unchanged")
    LOGGER.info("Clipboard image policy: office-gdi-v1 PNG/DIB/EMF/bitmap; bounded background conversion")
    LOGGER.info('Clipboard text policy: fourfold-v1 send=1048576 receive=65536 UTF-16 units; bulk chunks unchanged')
    heartbeat_stop_value = os.environ.get("MIRROR_HEARTBEAT_STOP_AFTER_SECONDS")
    LOGGER.info(
        "Bulk transfer policy: interactive-v1 chunk=%d aggregate_queue=%d",
        BULK_CHUNK_BYTES, BULK_BUFFER_BYTES,
    )
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
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    diagnostic_log = os.environ.get("MIRROR_DIAGNOSTIC_LOG")
    if diagnostic_log:
        try:
            handlers.append(RotatingFileHandler(
                diagnostic_log, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            ))
        except OSError:
            # Diagnostics must not prevent an otherwise healthy agent starting.
            logging.getLogger(__name__).warning("Diagnostic file unavailable; using stderr")
    logging.basicConfig(
        handlers=handlers,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    LOGGER.info('Video RTP scheduling policy: %s; profiles/settings unchanged',
                'bounded-pacing-v9' if VIDEO_PACING_ENABLED else 'burst-diagnostics-v9')
    LOGGER.info('ICE diagnostics policy: per-leg-v1; production routing unchanged')
    LOGGER.info('Connection timing policy: timing-split-v1; diagnostic-only; settings unchanged')
    LOGGER.info('File scheduling policy: credit-file-v1; bounded receipt window; worker disk I/O; media/settings unchanged')
    LOGGER.info('Interactive runtime policy: ordered-worker-v1; input/clipboard-read off-loop; settings unchanged')
    LOGGER.info('Control trace policy: control-trace-v1; numeric-only; SCTP observer/100ms loop; transport unchanged')
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        LOGGER.info("M0 agent stopped")
    except Exception:
        LOGGER.exception("Agent terminated unexpectedly")
        raise


if __name__ == "__main__":
    main()
