"""Diagnostic-only native ICE / application / browser scheduling observations.

No subtraction: STUN RTT and SCTP app RTT are different probes. Loop delay is
timer lateness, not the exact time an individual input spent in a browser queue.
No payloads, endpoints or absolute client timestamps are retained/logged.
"""
from __future__ import annotations

import logging
import math
import time

LOGGER = logging.getLogger('mirror_host_agent')
NUMBERS = {'iceRttMs', 'iceAgeMs', 'appRttMs', 'appAgeMs', 'loopDelayMaxMs',
           'statsAwaitMs', 'longTaskMaxMs'}
FIELDS = NUMBERS | {'iceState', 'visibility', 'longTasksSupported', 'longTaskCount'}


def parse_timing(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('invalid timing fields')
    for key in NUMBERS:
        number = value[key]
        if number is not None and (type(number) not in (int, float) or
                                  not 0 <= number <= 60_000 or not math.isfinite(number)):
            raise ValueError('invalid timing number')
    count = value['longTaskCount']
    if count is not None and (type(count) is not int or not 0 <= count <= 128):
        raise ValueError('invalid task count')
    if type(value['longTasksSupported']) is not bool:
        raise ValueError('invalid feature flag')
    if not isinstance(value['iceState'], str) or value['iceState'] not in {'unavailable', 'unverified', 'recent', 'stale'}:
        raise ValueError('invalid ICE state')
    if not isinstance(value['visibility'], str) or value['visibility'] not in {'visible', 'hidden'}:
        raise ValueError('invalid visibility')
    if value['iceState'] == 'unavailable' and value['iceRttMs'] is not None:
        raise ValueError('unavailable RTT')
    if value['iceState'] != 'unavailable' and value['iceRttMs'] is None:
        raise ValueError('missing RTT')
    if value['iceState'] == 'unverified' and value['iceAgeMs'] is not None:
        raise ValueError('unverified age')
    if value['iceState'] == 'recent' and (value['iceAgeMs'] is None or value['iceAgeMs'] > 15_000):
        raise ValueError('invalid recent age')
    if value['iceState'] == 'stale' and value['iceAgeMs'] is not None and value['iceAgeMs'] <= 15_000:
        raise ValueError('invalid stale age')
    if (value['appRttMs'] is None) != (value['appAgeMs'] is None):
        raise ValueError('missing app observation age')
    if not value['longTasksSupported'] and (count is not None or value['longTaskMaxMs'] is not None):
        raise ValueError('unsupported task observation')
    if value['visibility'] == 'hidden' and any(value[k] is not None for k in ('loopDelayMaxMs', 'longTaskCount', 'longTaskMaxMs')):
        raise ValueError('background scheduling excluded')
    return {key: value[key] for key in sorted(FIELDS)}


class TimingDiagnostics:
    def __init__(self):
        self._last = -math.inf

    def observe(self, data, *, now=None):
        value = parse_timing(data)
        now = time.monotonic() if now is None else now
        pressure = value['visibility'] == 'visible' and any(
            (value[key] or 0) >= limit for key, limit in
            (('loopDelayMaxMs', 50), ('appRttMs', 200), ('iceRttMs', 100), ('longTaskMaxMs', 50)))
        if now - self._last < (1 if pressure else 10):
            return
        self._last = now
        LOGGER.info('Connection timing diagnostics: timing-split-v1 ice_rtt_ms=%s ice_state=%s '
                    'ice_observed_age_ms=%s app_rtt_ms=%s app_age_ms=%s loop_max_5s_ms=%s '
                    'stats_await_ms=%s longtasks_supported=%s longtasks_5s_count=%s '
                    'longtask_max_5s_ms=%s visibility=%s',
                    value['iceRttMs'], value['iceState'], value['iceAgeMs'], value['appRttMs'],
                    value['appAgeMs'], value['loopDelayMaxMs'], value['statsAwaitMs'],
                    value['longTasksSupported'], value['longTaskCount'], value['longTaskMaxMs'], value['visibility'])
