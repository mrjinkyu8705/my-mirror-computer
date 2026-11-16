"""Bounded, enum-only per-leg ICE diagnostics. Never controls bitrate or ICE.

Application-owned introspection of reviewed aiortc1.15/aioice0.10 selected pair.
Unknown/missing state stays unknown. No endpoint, SDP, URL or auth is logged.
"""
from __future__ import annotations

import logging
import math
import time

LOGGER = logging.getLogger('mirror_host_agent')
TYPES = {'host', 'srflx', 'prflx', 'relay', 'unknown'}
PROTOCOLS = {'udp', 'tcp', 'unknown'}
RELAYS = {'udp', 'tcp', 'tls', 'unknown', 'not-used'}
UNKNOWN = dict(localType='unknown', remoteType='unknown', candidateProtocol='unknown', relayProtocol='unknown')


def parse_path(value):
    if not isinstance(value, dict) or set(value) != set(UNKNOWN):
        raise ValueError('invalid diagnostic fields')
    for field, allowed in (('localType', TYPES), ('remoteType', TYPES),
                           ('candidateProtocol', PROTOCOLS), ('relayProtocol', RELAYS)):
        if not isinstance(value[field], str) or value[field] not in allowed:
            raise ValueError('invalid diagnostic label')
    if value['localType'] != 'relay' and value['relayProtocol'] not in {'not-used', 'unknown'}:
        raise ValueError('relay transport requires local relay')
    return {field: value[field] for field in UNKNOWN}


def selected_host_path(sender):
    try:
        connection = sender.transport.transport._connection
        pair = connection._nominated.get(1)
        if pair is None:
            return dict(UNKNOWN)
        local, remote = pair.local_candidate, pair.remote_candidate
        local_type = local.type if local.type in TYPES else 'unknown'
        remote_type = remote.type if remote.type in TYPES else 'unknown'
        protocol = local.transport if local.transport in PROTOCOLS else 'unknown'
        relay = 'not-used' if local_type != 'relay' else 'unknown'
        # A nominated local relay was allocated with this connection's TURN
        # settings; do not use remote candidate protocol to guess its TURN leg.
        if local_type == 'relay':
            transport = getattr(connection, 'turn_transport', None)
            secure = getattr(connection, 'turn_ssl', None)
            if transport == 'tcp' and secure is True:
                relay = 'tls'
            elif secure is False and transport in {'udp', 'tcp'}:
                relay = transport
        return dict(localType=local_type, remoteType=remote_type,
                    candidateProtocol=protocol, relayProtocol=relay)
    except (AttributeError, TypeError, ValueError):
        return dict(UNKNOWN)


class RouteDiagnostics:
    def __init__(self):
        self._last = -math.inf
        self._path = None

    def observe(self, sender, viewer=None, *, delivery=None, jitter=None, loss=None, mbps=None, now=None):
        now = time.monotonic() if now is None else now
        host = selected_host_path(sender)
        viewer = dict(UNKNOWN) if viewer is None else parse_path(viewer)
        signature = (tuple(host.items()), tuple(viewer.items()))
        if now - self._last < (1 if signature != self._path else 10):
            return
        self._last, self._path = now, signature
        LOGGER.info('ICE path diagnostics: host_local=%s host_remote=%s host_candidate=%s host_turn=%s '
                    'viewer_local=%s viewer_remote=%s viewer_candidate=%s viewer_turn=%s '
                    'rx_mbps=%s loss_pct=%s jitter_ms=%s buffer_ms=%s decode_ms=%s '
                    'control_rtt_ms=%s interval_ms=%s received=%s lost=%s recovered=%s dropped=%s',
                    *host.values(), *viewer.values(), mbps, loss, jitter,
                    getattr(delivery, 'buffer_ms', None), getattr(delivery, 'decode_ms', None),
                    getattr(delivery, 'control_rtt_ms', None), getattr(delivery, 'interval_ms', None),
                    getattr(delivery, 'received_packets', None), getattr(delivery, 'lost_packets', None),
                    getattr(delivery, 'recovered_packets', None), getattr(delivery, 'dropped_frames', None))
