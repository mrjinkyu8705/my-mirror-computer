import json
import time
import unittest
from unittest.mock import Mock, patch
from mirror_host_agent.timing_diagnostics import TimingDiagnostics, parse_timing
from mirror_host_agent.__main__ import AgentConfig, M0Agent
from mirror_host_agent.h264_encoder import TunedH264Encoder


def sample(**changes):
    return {**dict(iceRttMs=12, iceAgeMs=0, iceState='recent', appRttMs=15,
                appAgeMs=200, loopDelayMaxMs=1, statsAwaitMs=1,
                longTasksSupported=True, longTaskCount=0, longTaskMaxMs=0,
                visibility='visible'), **changes}


class TimingDiagnosticsTests(unittest.TestCase):
    def test_valid_and_missing_optional_browser_features(self):
        self.assertEqual(parse_timing(sample()), sample())
        value = sample(iceRttMs=None, iceAgeMs=None, iceState='unavailable',
                       appRttMs=None, appAgeMs=None, longTasksSupported=False,
                       longTaskCount=None, longTaskMaxMs=None)
        self.assertEqual(parse_timing(value), value)
        parse_timing(sample(iceState='stale', iceAgeMs=None))

    def test_strict_numbers_enums_consistency_and_privacy(self):
        invalid = [None, [], {**sample(), 'url': 'secret'},
                   {k: v for k, v in sample().items() if k != 'iceRttMs'}]
        for key in ('iceRttMs', 'iceAgeMs', 'appRttMs', 'appAgeMs', 'loopDelayMaxMs', 'statsAwaitMs', 'longTaskMaxMs'):
            invalid.extend(sample(**{key: x}) for x in (True, -1, 60001, float('nan'), float('inf'), 'url', []))
        invalid.extend([sample(iceState=[]), sample(visibility='url'), sample(longTasksSupported=1),
                        sample(longTaskCount=1.5), sample(longTaskCount=129), sample(longTaskCount=True),
                        sample(iceState='recent', iceAgeMs=None), sample(iceState='unverified', iceAgeMs=0),
                        sample(iceState='unavailable'), sample(iceState='recent', iceRttMs=None),
                        sample(iceState='stale', iceAgeMs=100), sample(appRttMs=None),
                        sample(longTasksSupported=False), sample(visibility='hidden')])
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_timing(value)

    def test_bounded_independent_log_fields(self):
        diag = TimingDiagnostics()
        with self.assertLogs('mirror_host_agent', level='INFO') as logs:
            for at in range(101): diag.observe(sample(), now=at / 10)
            diag.observe(sample(appRttMs=550, loopDelayMaxMs=530, longTaskMaxMs=550), now=11)
            diag.observe(sample(appRttMs=550), now=11.5)
        self.assertEqual(len(logs.output), 3)
        self.assertIn('ice_rtt_ms=12', logs.output[-1])
        self.assertIn('app_rtt_ms=550', logs.output[-1])
        self.assertIn('loop_max_5s_ms=530', logs.output[-1])
        self.assertNotIn('secret', ''.join(logs.output))

    def test_dispatch_does_not_change_quality_control_or_lease(self):
        agent = M0Agent(AgentConfig(device_id='device_0123456789abcdef', session_id='session_0123456789abcdef',
                       ticket='unused', ws_url='ws://127.0.0.1:8787/ws', heartbeat_stop_after_seconds=None))
        agent._input_controller = Mock()
        agent._granted_control = True
        agent._control_expires_at_ms = int(time.time() * 1000) + 60000
        agent._last_control_ping_monotonic = 123
        message = dict(event='video.timing-report', data=sample(), sequence=1,
                       sessionId=agent._session_id, timestamp=int(time.time() * 1000), version=1)
        with patch.object(TunedH264Encoder, 'update_receiver_network_health') as quality, \
                patch.object(agent._timing_diagnostics, 'observe') as observe:
            agent._on_control_message(Mock(), json.dumps(message))
            observe.assert_called_once_with(sample())
            quality.assert_not_called()
            agent._input_controller.handle.assert_not_called()
            self.assertEqual(agent._last_control_ping_monotonic, 123)
            observe.side_effect = RuntimeError('fixture')
            agent._on_control_message(Mock(), json.dumps(message))
            agent._input_controller.handle.assert_not_called()

    def test_legacy_host_input_controller_ignores_new_event(self):
        from mirror_host_agent.input_control import InputController, FakeInputSink
        sink = FakeInputSink()
        controller = InputController(sink, frame_width=1600, frame_height=900)
        self.assertFalse(controller.handle(dict(event='video.timing-report', data=sample(), sequence=1), now=1))
        self.assertEqual(sink.calls, [])
        self.assertTrue(controller.handle(dict(event='key.down', data={'code': 'KeyA'}, sequence=2), now=1.1))
        self.assertEqual(sink.calls, [('key', 'KeyA', 'down')])
