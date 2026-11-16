import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from mirror_host_agent.route_diagnostics import UNKNOWN, RouteDiagnostics, parse_path, selected_host_path
from mirror_host_agent.__main__ import M0Agent, AgentConfig
from mirror_host_agent.h264_encoder import TunedH264Encoder


def sender(local='relay', remote='relay', transport='udp', secure=False):
    pair=NS(local_candidate=NS(type=local,transport='udp'),remote_candidate=NS(type=remote))
    return NS(transport=NS(transport=NS(_connection=NS(_nominated={1:pair},turn_transport=transport,turn_ssl=secure))))


class RouteDiagnosticsTests(unittest.TestCase):
    def test_selected_pair_not_candidate_protocol_determines_turn_leg(self):
        for transport,secure,want in [('udp',False,'udp'),('tcp',False,'tcp'),('tcp',True,'tls')]:
            path=selected_host_path(sender(transport=transport,secure=secure))
            self.assertEqual(path['candidateProtocol'],'udp')
            self.assertEqual(path['relayProtocol'],want)
        self.assertEqual(selected_host_path(sender(local='host'))['relayProtocol'],'not-used')
        self.assertEqual(selected_host_path(None),UNKNOWN)
        self.assertEqual(selected_host_path(NS(transport=None)),UNKNOWN)

    def test_untrusted_labels_and_fields_rejected(self):
        path=selected_host_path(sender())
        self.assertEqual(parse_path(path),path)
        for value in [None,[],dict(path,url='secret'),dict(path,relayProtocol='secret'),
                      dict(path,localType=[]),dict(path,localType='host')]:
            with self.assertRaises(ValueError): parse_path(value)

    def test_periodic_and_change_logs_are_bounded_and_private(self):
        diag=RouteDiagnostics()
        with self.assertLogs('mirror_host_agent',level='INFO') as logs:
            for at in range(100):
                diag.observe(sender(),dict(UNKNOWN),now=at/10)
            diag.observe(sender(),dict(UNKNOWN),now=10)
            diag.observe(sender(transport='tcp',secure=True),dict(UNKNOWN),now=11)
        self.assertEqual(len(logs.output),3)
        self.assertIn('host_turn=tls',logs.output[-1])
        self.assertIn('viewer_turn=unknown',logs.output[-1])
        self.assertNotIn('credential',''.join(logs.output))

    def test_optional_diagnostic_cannot_change_feedback_arguments(self):
        agent=M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
            ticket='unused',ws_url='ws://127.0.0.1:8787/ws',heartbeat_stop_after_seconds=None))
        data=dict(jitterMs=6,packetLossPercent=0,receivedBitrateMbps=2.5,route='turn')
        with patch.object(TunedH264Encoder,'update_receiver_network_health') as update:
            agent._handle_receiver_report(data)
            expected=update.call_args
            agent._handle_path_report(selected_host_path(sender()))
            agent._handle_receiver_report(data)
            self.assertEqual(update.call_args,expected)
            agent._route_diagnostics.observe=lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('fixture'))
            agent._handle_receiver_report(data)
            self.assertEqual(update.call_count,3)
            valid=agent._viewer_path
            agent._handle_path_report(dict(UNKNOWN,url='secret'))
            self.assertEqual(agent._viewer_path,valid)
            agent._handle_receiver_report(dict(data,path=valid))
            self.assertEqual(update.call_count,3)

    def test_stale_viewer_path_becomes_unknown_without_changing_feedback(self):
        agent=M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
            ticket='unused',ws_url='ws://127.0.0.1:8787/ws',heartbeat_stop_after_seconds=None))
        with patch('mirror_host_agent.__main__.time.monotonic',return_value=100):
            agent._handle_path_report(selected_host_path(sender()))
        data=dict(jitterMs=6,packetLossPercent=0,receivedBitrateMbps=2.5,route='turn')
        with patch.object(agent._route_diagnostics,'observe') as observe, patch.object(TunedH264Encoder,'update_receiver_network_health'):
            with patch('mirror_host_agent.__main__.time.monotonic',return_value=110):
                agent._handle_receiver_report(data)
                self.assertEqual(observe.call_args.args[1],agent._viewer_path)
            with patch('mirror_host_agent.__main__.time.monotonic',return_value=116):
                agent._handle_receiver_report(data)
                self.assertIsNone(observe.call_args.args[1])
