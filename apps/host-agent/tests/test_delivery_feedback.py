"""Count-weighted feedback, safe stepped adaptation, and v1 compatibility."""
import unittest
from unittest.mock import patch

from mirror_host_agent.delivery_feedback import DeliverySample, DeliveryWindow
from mirror_host_agent.h264_encoder import TunedH264Encoder, CapacityProbe


def raw(**values):
    return dict(intervalMs=1000,receivedPackets=998,lostPackets=2,bufferMs=15,
                decodeMs=6,decodedFrames=15,droppedFrames=0,**values)


def sample(**values):
    data=raw();data.update(values)
    return DeliverySample.parse(data)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        TunedH264Encoder.clear_receiver_network_health()

    def tearDown(self):
        TunedH264Encoder.clear_receiver_network_health()

    def report(self,now,**values):
        item=sample(**values)
        total=(item.received_packets or 0)+(item.lost_packets or 0)
        TunedH264Encoder.update_receiver_network_health(jitter_ms=6,
            packet_loss_percent=(100*(item.lost_packets or 0)/total if total else None),
            route='turn',delivery=item,now=now)

    def encoder(self):
        enc=TunedH264Encoder();enc._initial_bitrate_applied=True
        enc._target_bitrate=2_500_000;enc._pending_lower_bitrate=500_000
        enc._pending_lower_since=0
        enc._receiver_estimate_at=100
        return enc

    def lower(self,enc,now,frame=4):
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=now):
            enc._apply_pending_lower_bitrate(frame,content_stable=False)

    def test_rejects_malformed_counts_intervals_and_unknown_fields(self):
        cases=[{'lostPackets':True},{'lostPackets':1.5},{'lostPackets':-1},
               {'intervalMs':0},{'intervalMs':3001},{'bufferMs':float('nan')},
               {'decodeMs':float('inf')},{'receivedPackets':1_000_001},
               {'decodedFrames':10_001},{'unknown':4}]
        for fields in cases:
            data=raw();data.update(fields)
            with self.subTest(fields=fields),self.assertRaises(ValueError):
                DeliverySample.parse(data)
        with self.assertRaises(ValueError): DeliverySample.parse(None)

    def test_missing_values_stay_unknown(self):
        self.report(100,receivedPackets=None,lostPackets=None,bufferMs=None,decodeMs=None)
        r=TunedH264Encoder._receiver_network_report
        self.assertEqual(r.delivery.state,'insufficient')
        self.assertIsNone(r.delivery.loss_percent)
        self.assertFalse(r.unhealthy)

    def test_packet_weighted_counterexample_no_longer_collapses(self):
        for i,(lost,total) in enumerate(((1,16),(0,1000),(1,16),(0,1000),(1,16))):
            self.report(100+i,receivedPackets=total-lost,lostPackets=lost)
        r=TunedH264Encoder._receiver_network_report
        self.assertAlmostEqual(r.delivery.loss_percent,3/2048*100)
        self.assertEqual(r.delivery.state,'healthy')
        enc=self.encoder();self.lower(enc,104)
        self.assertEqual(enc.target_bitrate,2_500_000)

    def test_sparse_large_percent_does_not_manufacture_capacity_congestion(self):
        for i in range(5): self.report(100+i,receivedPackets=14,lostPackets=2)
        self.assertEqual(TunedH264Encoder._receiver_network_report.delivery.state,'insufficient')
        enc=self.encoder();self.lower(enc,104)
        self.assertEqual(enc.target_bitrate,2_500_000)

    def test_moderate_loss_is_stepped_and_cannot_repeat_in_same_interval(self):
        for i in range(3): self.report(100+i,receivedPackets=970,lostPackets=30)
        self.assertEqual(TunedH264Encoder._receiver_network_report.delivery.state,'moderate')
        enc=self.encoder();self.lower(enc,102)
        self.assertEqual(enc.target_bitrate,2_000_000)
        enc._pending_lower_bitrate=500_000;enc._pending_lower_since=0
        self.lower(enc,102.1)
        self.assertEqual(enc.target_bitrate,2_000_000)
        self.report(104,receivedPackets=970,lostPackets=30)
        enc._receiver_estimate_at=104
        self.lower(enc,104)
        self.assertEqual(enc.target_bitrate,1_600_000)

    def test_single_real_loss_burst_bypasses_hold_with_a_bounded_step(self):
        self.report(100,receivedPackets=800,lostPackets=200)
        enc=self.encoder();enc._pending_lower_since=None
        self.lower(enc,100,0)
        self.assertEqual(enc.target_bitrate,2_000_000)

    def test_one_large_playout_spike_does_not_prove_severe_congestion(self):
        self.report(100,lostPackets=0,bufferMs=300,decodeMs=7)
        self.assertEqual(TunedH264Encoder._receiver_network_report.delivery.state,'insufficient')

    def test_slow_decoder_is_not_mislabeled_as_network_backlog(self):
        for i in range(3): self.report(100+i,lostPackets=0,bufferMs=300,decodeMs=100)
        self.assertEqual(TunedH264Encoder._receiver_network_report.delivery.state,'receiver-limited')

    def test_growing_buffer_with_fast_decode_is_moderate(self):
        for i,delay in enumerate((130,170,210)):
            self.report(100+i,lostPackets=0,bufferMs=delay)
        self.assertEqual(TunedH264Encoder._receiver_network_report.delivery.state,'moderate')

    def test_production_like_lossless_356ms_spike_does_not_collapse_rate(self):
        for i,delay in enumerate((80,78,82,356.1)):
            self.report(100+i,lostPackets=0,bufferMs=delay,decodeMs=1.3)
        enc=self.encoder();self.lower(enc,103)
        self.assertEqual(enc.target_bitrate,2_500_000)
        self.assertFalse(TunedH264Encoder._receiver_network_report.unhealthy)

    def test_repeated_transient_spikes_do_not_accumulate_as_a_backlog(self):
        for i,delay in enumerate((80,350,80,340,90,360)):
            self.report(100+i,lostPackets=0,bufferMs=delay)
            self.assertNotEqual(TunedH264Encoder._receiver_network_report.delivery.state,'severe')

    def test_stable_150ms_playout_is_eligible_for_guarded_recovery(self):
        for i in range(4): self.report(100+i,lostPackets=0,bufferMs=150)
        self.assertEqual(TunedH264Encoder._receiver_network_report.delivery.state,'healthy')

    def test_falling_high_delay_does_not_trigger_another_downshift(self):
        for i,delay in enumerate((350,320,280)):
            self.report(100+i,lostPackets=0,bufferMs=delay)
        self.assertFalse(TunedH264Encoder._receiver_network_report.unhealthy)

    def test_sustained_rising_delay_uses_step_not_emergency_collapse(self):
        for i,delay in enumerate((280,400,520)):
            self.report(100+i,lostPackets=0,bufferMs=delay)
        enc=self.encoder();self.lower(enc,102)
        self.assertEqual(enc.target_bitrate,2_000_000)

    def test_sustained_one_second_backlog_retains_emergency_safety(self):
        for i in range(3): self.report(100+i,lostPackets=0,bufferMs=1100)
        enc=self.encoder();enc._pending_lower_since=None;self.lower(enc,102,0)
        self.assertEqual(enc.target_bitrate,500_000)

    def test_old_rise_followed_by_plateau_does_not_repeat_delay_downshift(self):
        for i,delay in enumerate((130,190,250,250,250)):
            self.report(100+i,lostPackets=0,bufferMs=delay)
        self.assertFalse(TunedH264Encoder._receiver_network_report.unhealthy)

    def test_high_backlog_qualifies_after_two_seconds_despite_older_mild_delay(self):
        for i,delay in enumerate((150,150,1100,1100,1100)):
            self.report(100+i,lostPackets=0,bufferMs=delay)
        self.assertEqual(TunedH264Encoder._receiver_network_report.delivery.state,'severe')

    def test_missing_counts_or_decode_cannot_prove_backlog(self):
        for fields in ({'receivedPackets':None},{'decodeMs':None}):
            TunedH264Encoder.clear_receiver_network_health()
            for i in range(3): self.report(100+i,lostPackets=0,bufferMs=1100,**fields)
            self.assertFalse(TunedH264Encoder._receiver_network_report.unhealthy)

    def test_burst_samples_cannot_manufacture_two_seconds_of_backlog(self):
        for i in range(8): self.report(100+i*.25,lostPackets=0,bufferMs=1100,intervalMs=250)
        self.assertNotEqual(TunedH264Encoder._receiver_network_report.delivery.state,'severe')

    def test_null_current_counts_cannot_reuse_previous_healthy_state(self):
        for i in range(3): self.report(100+i,lostPackets=0)
        self.report(103,receivedPackets=None,lostPackets=None)
        r=TunedH264Encoder._receiver_network_report
        self.assertEqual(r.delivery.state,'insufficient')
        enc=self.encoder();enc._pending_higher_bitrate=3_000_000;enc._last_upshift_at=0
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=103): enc._apply_pending_higher_bitrate()
        self.assertEqual(enc.target_bitrate,2_500_000)

    def test_window_resets_on_route_gap_or_backwards_clock(self):
        window=DeliveryWindow()
        for i in range(3): window.add(100+i,'turn',sample())
        self.assertEqual(window.evidence(102).state,'healthy')
        for at,route in ((103,'direct'),(108,'direct'),(90,'direct')):
            window.add(at,route,sample())
            self.assertEqual(window.evidence(at).state,'insufficient')
        self.assertEqual(window.evidence(100).state,'stale')

    def test_duplicate_report_cannot_manufacture_seconds_of_evidence(self):
        window=DeliveryWindow();window.add(100,'turn',sample(lostPackets=30))
        for i in range(20): self.assertFalse(window.add(100.01+i*.01,'turn',sample(lostPackets=30)))
        self.assertEqual(window.evidence(100.2).sample_count,1)
        self.assertEqual(window.evidence(100.2).state,'insufficient')

    def test_new_viewer_gap_does_not_downgrade_to_legacy_percentage_voting(self):
        self.report(100,lostPackets=0)
        for i in range(3):
            TunedH264Encoder.update_receiver_network_health(jitter_ms=6,
                packet_loss_percent=20,route='turn',now=101+i)
        enc=self.encoder();self.lower(enc,103)
        self.assertEqual(enc.target_bitrate,2_500_000)
        self.assertEqual(TunedH264Encoder._receiver_network_report.delivery.state,'insufficient')

    def test_legacy_viewer_retains_existing_compatibility(self):
        for i in range(3):
            TunedH264Encoder.update_receiver_network_health(jitter_ms=6,packet_loss_percent=5,route='turn',now=100+i)
        enc=self.encoder();self.lower(enc,102)
        self.assertEqual(enc.target_bitrate,500_000)
        self.assertIsNone(TunedH264Encoder._receiver_network_report.delivery)

    def test_session_reset_clears_delivery_capability_and_history(self):
        self.report(100)
        TunedH264Encoder.clear_receiver_network_health()
        self.assertFalse(TunedH264Encoder._delivery_seen)
        self.assertEqual(len(TunedH264Encoder._delivery_window.samples),0)

    def test_prolonged_missing_counts_uses_moving_only_stepped_fallback(self):
        enc=self.encoder()
        for i in range(10): self.report(100+i,receivedPackets=None,lostPackets=None)
        enc._receiver_estimate_at=109
        self.lower(enc,109,9)
        self.assertEqual(enc.target_bitrate,2_000_000)

    def test_missing_counts_with_old_remb_does_not_authorize_a_reduction(self):
        enc=self.encoder()
        for i in range(10): self.report(100+i,receivedPackets=None,lostPackets=None)
        self.lower(enc,109,9)
        self.assertEqual(enc.target_bitrate,2_500_000)

    def test_window_storage_is_bounded(self):
        window=DeliveryWindow()
        for i in range(200): window.add(100+i*.25,'turn',sample(intervalMs=250))
        self.assertLessEqual(len(window.samples),24)

    def test_partial_fresh_confirmation_keeps_only_verified_recovery(self):
        enc=self.encoder();enc._target_bitrate=600_000
        enc._recovery_ceiling=2_500_000
        enc._capacity_probe=CapacityProbe(106,500_000,600_000)
        enc._receiver_target_bitrate=560_000;enc._receiver_estimate_at=108
        for i in range(100,110): self.report(i,lostPackets=0)
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=109):
            enc._update_capacity_probe(content_stable=False)
        self.assertIsNone(enc._capacity_probe)
        self.assertEqual(enc.target_bitrate,560_000)
        self.assertEqual(enc._probe_backoff,10)

    def test_host_accepts_new_counts_and_rejects_malformed_payloads(self):
        from mirror_host_agent.__main__ import M0Agent, AgentConfig
        agent=M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
            ticket='unused',ws_url='ws://127.0.0.1:8787/ws',heartbeat_stop_after_seconds=None))
        data={'jitterMs':6,'packetLossPercent':.2,'receivedBitrateMbps':1,'route':'turn','delivery':raw()}
        with patch.object(TunedH264Encoder,'update_receiver_network_health') as update:
            agent._handle_receiver_report(data)
            self.assertEqual(update.call_count,1)
            self.assertIsInstance(update.call_args.kwargs['delivery'],DeliverySample)
        for extra in ({'lostPackets':True},{'intervalMs':99999},{'bufferMs':float('nan')},{'command':'run'}):
            bad=raw();bad.update(extra)
            with self.subTest(extra=extra),patch.object(TunedH264Encoder,'update_receiver_network_health') as update:
                agent._handle_receiver_report({**data,'delivery':bad})
                update.assert_not_called()

    def test_late_recovery_cancels_previous_window_loss(self):
        self.report(100,receivedPackets=90,lostPackets=10,recoveredPackets=0,receivedBytes=10000)
        self.report(101,receivedPackets=110,lostPackets=0,recoveredPackets=10,receivedBytes=10000)
        self.report(102,receivedPackets=100,lostPackets=0,recoveredPackets=0,receivedBytes=10000)
        result=TunedH264Encoder._receiver_network_report.delivery
        self.assertEqual(result.lost,0)
        self.assertEqual(result.recovered,10)
        self.assertEqual(result.packets,300)
        self.assertEqual(result.state,'healthy')

    def test_excess_recovery_never_hides_future_loss(self):
        self.report(100,receivedPackets=100,lostPackets=0,recoveredPackets=80,receivedBytes=10000)
        self.report(101,receivedPackets=970,lostPackets=30,recoveredPackets=0,receivedBytes=10000)
        self.report(102,receivedPackets=100,lostPackets=0,recoveredPackets=0,receivedBytes=10000)
        result=TunedH264Encoder._receiver_network_report.delivery
        self.assertEqual(result.lost,30)
        self.assertEqual(result.recovered,0)
        self.assertEqual(result.state,'moderate')

    def test_partial_recovery_preserves_unrecovered_loss(self):
        self.report(100,receivedPackets=90,lostPackets=10,recoveredPackets=0,receivedBytes=10000)
        self.report(101,receivedPackets=100,lostPackets=0,recoveredPackets=4,receivedBytes=10000)
        self.report(102,receivedPackets=100,lostPackets=0,recoveredPackets=0,receivedBytes=10000)
        self.assertEqual(TunedH264Encoder._receiver_network_report.delivery.lost,6)

    def test_correction_bounds_and_simultaneous_loss_are_rejected(self):
        for fields in ({'recoveredPackets':-1},{'recoveredPackets':5,'lostPackets':1},
                       {'recoveredPackets':101,'receivedPackets':100,'lostPackets':0,'receivedBytes':100},
                       {'recoveredPackets':5,'lostPackets':0}, {'receivedBytes':True}, {'controlRttMs':float('nan')}):
            with self.subTest(fields=fields),self.assertRaises(ValueError): sample(**fields)

    def test_raw_receiver_estimate_is_not_hidden_by_minimum_clamp(self):
        enc=self.encoder();enc.target_bitrate=120_000
        self.assertEqual(enc._raw_receiver_bitrate,120_000)
        self.assertEqual(enc.receiver_target_bitrate,500_000)

    def test_same_moderate_loss_cannot_be_spent_on_three_downshifts(self):
        for i in range(3):self.report(100+i,receivedPackets=40,lostPackets=4 if i==0 else 0)
        enc=self.encoder();self.lower(enc,102)
        self.assertEqual(enc.target_bitrate,2_000_000)
        self.report(104,receivedPackets=40,lostPackets=0)
        enc._pending_lower_bitrate=500_000;enc._pending_lower_since=0;enc._receiver_estimate_at=104
        self.lower(enc,104)
        self.assertEqual(enc.target_bitrate,2_000_000)

    def test_new_loss_after_first_cut_can_authorize_another_stepped_cut(self):
        for i in range(3):self.report(100+i,receivedPackets=970,lostPackets=30)
        enc=self.encoder();self.lower(enc,102)
        self.report(104,receivedPackets=970,lostPackets=30)
        enc._pending_lower_bitrate=500_000;enc._pending_lower_since=0;enc._receiver_estimate_at=104
        self.lower(enc,104)
        self.assertEqual(enc.target_bitrate,1_600_000)

    def test_fresh_delay_growth_and_sustained_loss_are_not_blocked_by_spent_loss(self):
        for i in range(3):self.report(100+i,receivedPackets=970,lostPackets=30)
        enc=self.encoder();self.lower(enc,102)
        for i,delay in enumerate((130,200,280)):
            self.report(103+i,receivedPackets=1000,lostPackets=0,bufferMs=delay)
        enc._pending_lower_bitrate=500_000;enc._pending_lower_since=0;enc._receiver_estimate_at=105
        self.lower(enc,105)
        self.assertEqual(enc.target_bitrate,1_600_000)
        self.report(106,receivedPackets=800,lostPackets=200)
        enc._pending_lower_bitrate=500_000;enc._pending_lower_since=None;enc._receiver_estimate_at=106
        self.lower(enc,106)
        self.assertEqual(enc.target_bitrate,1_600_000)  # same two-second cut budget
        for at in (107,108):
            self.report(at,receivedPackets=800,lostPackets=200)
            enc._pending_lower_bitrate=500_000;enc._pending_lower_since=None;enc._receiver_estimate_at=at
            self.lower(enc,at)
        self.assertEqual(enc.target_bitrate,500_000)


if __name__=='__main__': unittest.main()
