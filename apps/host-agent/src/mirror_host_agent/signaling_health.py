"""Negotiated application-level signaling health, bounded per connection.

New servers acknowledge validated application heartbeats. Only then replace
protocol-pong-only liveness; legacy servers keep the existing 10s/30s policy.
No installed library edits, global patch, auth/routing/timing relaxation or
unbounded pending-ping collection. Reviewed websockets15 ClientConnection hook.
"""
from collections import deque
import time
from websockets.asyncio.client import ClientConnection

ACK_HEADER = 'x-mirror-app-heartbeat'
ACK_VERSION = 'ack-v1'
ACK_TYPE = 'agent.heartbeat-ack'


class SignalingAckTimeout(TimeoutError):
    pass


class HealthClientConnection(ClientConnection):
    def abort_expired_ack(self):
        # Called ONLY after the full application ACK watchdog expires. The
        # connect() context manager otherwise adds a 10s graceful-close wait
        # on this already half-open TCP stream. WebRTC is a separate transport.
        self.transport.abort()

    def start_keepalive(self):
        self.app_ack_supported = bool(self.response is not None and
            self.response.headers.get(ACK_HEADER) == ACK_VERSION)
        if not self.app_ack_supported:
            super().start_keepalive()
        # With negotiation, application heartbeats already supply traffic and
        # a separate bounded 30s ACK watchdog supplies liveness. Do NOT leave
        # protocol ping futures accumulating when pongs are unavailable.


class SignalingHealth:
    def __init__(self, *, enabled=False, now=None):
        self.enabled = enabled
        self.last_ack_at = time.monotonic() if now is None else now
        self.last_ack_sequence = -1
        self.last_rtt_ms = None
        self.pending = deque(maxlen=16)

    def sent(self, message, *, now=None):
        if self.enabled:
            self.pending.append((message['sequence'], message['sessionId'],
                                 time.monotonic() if now is None else now))

    def accept(self, message, *, now=None):
        now = time.monotonic() if now is None else now
        if (not self.enabled or message.get('type') != ACK_TYPE or message.get('payload') != {}
                or type(message.get('sequence')) is not int):
            return False
        match = next((row for row in self.pending if row[0] == message['sequence'] and
                      row[1] == message.get('sessionId')), None)
        if match is None or match[0] <= self.last_ack_sequence or not 0 <= now-match[2] <= 30:
            return False
        self.last_ack_at, self.last_ack_sequence = now, match[0]
        self.last_rtt_ms = (now-match[2])*1000
        while self.pending and self.pending[0][0] <= match[0]:
            self.pending.popleft()
        return True

    def expired(self, *, now=None, timeout=30):
        return self.enabled and (time.monotonic() if now is None else now)-self.last_ack_at >= timeout
