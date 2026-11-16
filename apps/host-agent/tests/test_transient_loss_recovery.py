"""Transient vs sustained loss, and bounded source-limited recovery. No OS I/O."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from mirror_host_agent.delivery_feedback import DeliverySample
from mirror_host_agent.h264_encoder import TunedH264Encoder, CapacityProbe


class TransientLossRecoveryTests(unittest.TestCase):
    def setUp(self):
        TunedH264Encoder.clear_receiver_network_health()
        self.encoder=TunedH264Encoder()
        self.encoder._initial_bitrate_applied=True
        self.encoder._active_backend='libx264'
        self.encoder._target_bitrate=2_500_000
        self.encoder._receiver_target_bitrate=500_000
        self.encoder._recovery_ceiling=2_500_000
        self.encoder._profile_max_bitrate=5_000_000
        self.encoder.codec=SimpleNamespace(max_bit_rate=2_500_000)

    def tearDown(self):
        TunedH264Encoder.clear_receiver_network_health()

    def report(self,at,*,received=1000,lost=0,rx=300_000,tx=300_000,rtt=15,buffer=30,
               jitter=7,interval=1000,decoded=15,dropped=0,route='turn'):
        total=(received or 0)+(lost or 0)
        TunedH264Encoder.update_receiver_network_health(jitter_ms=jitter,
            packet_loss_percent=(lost*100/total if total and lost is not None else None),route=route,now=at,
            delivery=DeliverySample(interval,received,lost,buffer,1,decoded,dropped,0,
                                    round(rx*interval/8000) if rx is not None else None,rtt))
        if tx is not None:TunedH264Encoder.update_sender_delivery(sent_bps=tx,now=at+.01)

    def lower(self,at):
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=at):
            self.encoder.target_bitrate=500_000
            self.encoder._apply_pending_lower_bitrate(at,content_stable=False)

    def tick(self,at,stable=False,changed=False):
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=at):
            self.encoder._update_capacity_probe(content_stable=stable,content_changed=changed)

    def ready_for_recovery(self,**fields):
        self.encoder._target_bitrate=840_000
        self.encoder._last_downshift_at=99
        for at in range(100,108):self.report(at,**fields)
        self.encoder._receiver_estimate_at=107

    def test_single_large_interval_reduces_at_most_twenty_percent(self):
        self.report(100,received=800,lost=200)
        self.lower(100)
        self.assertEqual(self.encoder.target_bitrate,2_000_000)

    def test_production_weighted_six_percent_with_one_twenty_percent_interval_is_not_emergency(self):
        for at in range(100,105):self.report(at,received=186)
        self.report(105,received=343,lost=88)
        self.assertAlmostEqual(TunedH264Encoder._receiver_network_report.delivery.loss_percent,88/1361*100)
        self.lower(105)
        self.assertEqual(self.encoder.target_bitrate,2_000_000)

    def test_same_burst_cannot_repeat_on_many_encode_frames_or_old_window_debt(self):
        self.report(100,received=800,lost=200);self.lower(100)
        for at in (100.1,100.5,101,102,103):
            if at>=101:self.report(at,received=1000)
            self.lower(at)
        self.assertEqual(self.encoder.target_bitrate,2_000_000)

    def test_three_consecutive_loss_reports_over_two_seconds_retain_emergency(self):
        for at in (100,101,102):self.report(at,received=800,lost=200);self.lower(at)
        self.assertEqual(TunedH264Encoder._receiver_network_report.delivery.state,'severe')
        self.assertEqual(self.encoder.target_bitrate,500_000)

    def test_separate_loss_spikes_do_not_become_continuous_emergency(self):
        for at,lost in ((100,200),(101,0),(102,200),(103,0),(104,200)):
            self.report(at,received=1000-lost,lost=lost);self.lower(at)
            self.assertNotEqual(TunedH264Encoder._receiver_network_report.delivery.state,'severe')
        self.assertGreaterEqual(self.encoder.target_bitrate,1_280_000)

    def test_three_subsecond_reports_do_not_fake_two_seconds_of_persistent_loss(self):
        for at in (100,100.25,100.5):self.report(at,received=800,lost=200,interval=250)
        self.assertNotEqual(TunedH264Encoder._receiver_network_report.delivery.state,'severe')

    def test_jitter_alone_is_not_emergency_but_sustained_backlog_remains_emergency(self):
        self.report(100,jitter=150);self.lower(100)
        self.assertEqual(self.encoder.target_bitrate,2_500_000)
        self.setUp()
        for at in (100,101,102):self.report(at,buffer=1100)
        self.lower(102)
        self.assertEqual(self.encoder.target_bitrate,500_000)

    def test_recorded_typing_jitter_367_without_loss_does_not_collapse_to_floor(self):
        self.encoder._target_bitrate=1_280_000
        self.encoder.codec.max_bit_rate=1_280_000
        for at in (100,101,102):self.report(at,received=30,buffer=48)
        self.report(103,received=134,buffer=53.3,rtt=334,jitter=367)
        self.lower(103)
        self.assertNotEqual(TunedH264Encoder._receiver_network_report.delivery.state,'severe')
        self.assertEqual(self.encoder.target_bitrate,1_280_000)
        for at in (104,105,106):self.report(at,received=30,buffer=55,jitter=10);self.lower(at)
        self.assertEqual(self.encoder.target_bitrate,1_280_000)

    def test_repeated_jitter_without_loss_or_backlog_does_not_invent_bandwidth_loss(self):
        for at in range(100,107):
            self.report(at,jitter=367,buffer=50,rtt=20);self.lower(at)
        self.assertEqual(self.encoder.target_bitrate,2_500_000)

    def test_catastrophic_half_packet_loss_retains_immediate_emergency(self):
        self.report(100,received=500,lost=500)
        self.lower(100)
        self.assertEqual(self.encoder.target_bitrate,500_000)

    def test_healthy_paired_underutilized_media_can_start_a_bounded_motion_trial(self):
        self.ready_for_recovery();self.tick(107)
        self.assertIsNotNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,1_008_000)
        self.assertEqual(self.encoder.receiver_target_bitrate,500_000)

    def test_static_source_or_no_previous_reduction_does_not_activate_new_gate(self):
        self.ready_for_recovery();self.tick(107,stable=True)
        self.assertIsNone(self.encoder._capacity_probe)
        self.encoder._last_downshift_at=None;self.tick(107)
        self.assertIsNone(self.encoder._capacity_probe)

    def test_new_gate_rejects_missing_mismatched_sparse_or_pressured_delivery(self):
        for values in ({'tx':None},{'rx':None},{'rx':40_000,'tx':40_000},
                       {'rx':150_000,'tx':300_000},{'rtt':180},{'buffer':180},
                       {'decoded':0},{'dropped':1},{'received':None}):
            with self.subTest(values=values):
                self.setUp();self.ready_for_recovery(**values);self.tick(107)
                self.assertIsNone(self.encoder._capacity_probe)

    def test_source_limited_trial_cannot_retain_budget_without_new_delivery(self):
        self.ready_for_recovery();self.tick(107)
        for at in (108.1,109.1,110.1,111.1,112.1,113.1):self.report(at);self.tick(at)
        self.assertIsNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,840_000)
        self.assertEqual(self.encoder._probe_backoff,10)

    def test_confirmed_real_growth_can_retry_sooner_but_not_immediately(self):
        self.ready_for_recovery();self.tick(107)
        for at in (108.1,109.1,110.1):self.report(at,rx=600_000,tx=600_000);self.tick(at+.1)
        self.assertIsNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,1_008_000)
        self.assertAlmostEqual(self.encoder._probe_next_at,112.2)
        self.report(111.1,rx=600_000,tx=600_000);self.encoder._receiver_estimate_at=111.1;self.tick(111.2)
        self.assertIsNone(self.encoder._capacity_probe)
        self.report(112.2,rx=600_000,tx=600_000);self.encoder._receiver_estimate_at=112.2;self.tick(112.3)
        self.assertIsNotNone(self.encoder._capacity_probe)

    def test_new_loss_or_route_change_aborts_the_same_trial(self):
        for fields in ({'received':970,'lost':30},{'route':'direct'}):
            self.setUp();self.ready_for_recovery();self.tick(107)
            self.report(108,**fields);self.tick(108)
            self.assertIsNone(self.encoder._capacity_probe)
            self.assertEqual(self.encoder.target_bitrate,840_000)

    def test_recovery_never_exceeds_selected_initial_budget(self):
        self.ready_for_recovery();self.encoder._target_bitrate=2_450_000;self.tick(107)
        self.assertEqual(self.encoder.target_bitrate,2_500_000)

    def test_stale_estimate_cannot_start_a_source_limited_trial(self):
        self.ready_for_recovery();self.encoder._receiver_estimate_at=100;self.tick(107)
        self.assertIsNone(self.encoder._capacity_probe)

    def test_unknown_current_counters_break_a_persistent_loss_run(self):
        self.report(100,received=800,lost=200)
        self.report(101,received=None,lost=None)
        self.report(102,received=800,lost=200)
        self.report(103,received=800,lost=200)
        self.assertNotEqual(TunedH264Encoder._receiver_network_report.delivery.state,'severe')

    def test_source_limited_trial_rejects_a_recent_buffer_rise(self):
        self.ready_for_recovery(buffer=30)
        self.report(108,buffer=100)
        self.encoder._receiver_estimate_at=108;self.tick(108)
        self.assertIsNone(self.encoder._capacity_probe)

    def test_actual_delivery_confirmation_after_long_idle_keeps_fast_retry(self):
        self.ready_for_recovery();self.encoder._last_downshift_at=1;self.tick(107)
        for at in (108.1,109.1,110.1):self.report(at,rx=600_000,tx=600_000);self.tick(at+.1)
        self.assertAlmostEqual(self.encoder._probe_next_at,112.2)

    def end_source_limited_trial(self):
        self.ready_for_recovery();self.tick(107)
        for at in (108.1,109.1,110.1,111.1,112.1,113.1):self.report(at);self.tick(at)
        self.assertEqual(self.encoder.target_bitrate,840_000)
        self.assertAlmostEqual(self.encoder._probe_next_at,123.1)

    def test_resumed_motion_can_retry_early_only_after_matching_delivery_growth(self):
        self.end_source_limited_trial()
        for at in (114.1,115.1,116.1,117.1):
            self.report(at,rx=450_000,tx=450_000)
            self.encoder._receiver_estimate_at=at
            self.tick(at,stable=at >= 116.1)
            self.assertIsNone(self.encoder._capacity_probe)
        self.tick(117.2)
        self.assertIsNotNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,1_008_000)
        self.assertIsNone(self.encoder._motion_retry)
        self.report(118.1,received=950,lost=50,rx=450_000,tx=450_000)
        self.tick(118.1)
        self.assertIsNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,840_000)

    def test_same_traffic_or_mismatched_delivery_cannot_shorten_cooldown(self):
        for rx,tx in ((310_000,310_000),(450_000,300_000)):
            with self.subTest(rx=rx,tx=tx):
                self.setUp();self.end_source_limited_trial()
                for at in (114.1,115.1,116.1,117.1):
                    self.report(at,rx=rx,tx=tx);self.encoder._receiver_estimate_at=at;self.tick(at)
                self.assertIsNone(self.encoder._capacity_probe)
                self.assertEqual(self.encoder.target_bitrate,840_000)

    def test_short_scene_change_survives_feedback_and_cooldown_but_cannot_retain_rate(self):
        self.ready_for_recovery()
        self.encoder._probe_next_at=108.0
        old=np.zeros((24,32),dtype=np.uint8)
        new=np.full((24,32),200,dtype=np.uint8)
        with patch.object(self.encoder,'_sample_luma',side_effect=[old,new]+[new]*4):
            self.encoder._is_large_scene_change(None,None)
            self.assertFalse(self.encoder._sampled_content_motion)
            self.encoder._is_large_scene_change(None,None)
            self.assertTrue(self.encoder._sampled_content_motion)
            self.tick(107.1,changed=self.encoder._sampled_content_motion)
            self.assertIsNone(self.encoder._capacity_probe)
            for _ in range(4):self.encoder._is_large_scene_change(None,None)
            self.assertFalse(self.encoder._sampled_content_motion)
        self.tick(108.0,stable=True)
        self.assertIsNone(self.encoder._capacity_probe, 'Need feedback newer than the motion')
        self.report(108.1);self.encoder._receiver_estimate_at=108.1;self.tick(108.1,stable=True)
        self.assertIsNotNone(self.encoder._capacity_probe)
        self.assertIsNone(self.encoder._recent_probe_motion)
        for at in (109.2,110.2,111.2,112.2,113.2,114.2):self.report(at);self.tick(at,stable=True)
        self.assertIsNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,840_000)

    def test_short_motion_cannot_override_expiry_stale_feedback_loss_or_route_change(self):
        for gap in ('expiry','stale','loss','route'):
            with self.subTest(gap=gap):
                self.setUp();self.ready_for_recovery();self.encoder._probe_next_at=110.5
                self.tick(107.1,changed=True)
                self.assertIsNotNone(self.encoder._recent_probe_motion)
                if gap=='loss':self.report(108,received=900,lost=100)
                if gap=='route':self.report(108,route='direct')
                if gap!='expiry':self.tick(109.7 if gap=='stale' else 108,stable=True)
                self.assertIsNone(self.encoder._capacity_probe)
                if gap!='expiry':self.assertIsNone(self.encoder._recent_probe_motion)
                for at in (108.1,109.1,110.1,111.1):self.report(at)
                self.encoder._receiver_estimate_at=111.1;self.tick(111.1,stable=True)
                self.assertIsNone(self.encoder._recent_probe_motion)
                self.assertIsNone(self.encoder._capacity_probe)
                self.assertEqual(self.encoder.target_bitrate,840_000)

    def test_short_motion_retry_keeps_existing_three_second_and_growth_checks(self):
        self.end_source_limited_trial()
        self.report(114.1,rx=450_000,tx=450_000);self.encoder._receiver_estimate_at=114.1
        self.tick(114.2,changed=True)
        self.report(115.1,rx=450_000,tx=450_000);self.encoder._receiver_estimate_at=115.1
        self.tick(116.0,stable=True)
        self.assertIsNone(self.encoder._capacity_probe)
        self.report(116.2,rx=450_000,tx=450_000);self.encoder._receiver_estimate_at=116.2
        self.tick(116.2,stable=True)
        self.assertIsNotNone(self.encoder._capacity_probe)
        self.report(117.2,received=900,lost=100);self.tick(117.2,stable=True)
        self.assertEqual(self.encoder.target_bitrate,840_000)
        self.assertIsNone(self.encoder._capacity_probe)

    def test_corrected_old_debt_cannot_create_a_persistent_emergency(self):
        self.report(100,received=800,lost=200)
        self.report(101,received=800,lost=200)
        TunedH264Encoder.update_receiver_network_health(jitter_ms=7,packet_loss_percent=0,route='turn',now=102,
            delivery=DeliverySample(1000,1000,0,30,1,15,0,400,300_000//8,15))
        self.report(103,received=800,lost=200)
        self.assertNotEqual(TunedH264Encoder._receiver_network_report.delivery.state,'severe')
