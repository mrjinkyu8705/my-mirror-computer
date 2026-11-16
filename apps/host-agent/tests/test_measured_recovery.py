"""Delivery-confirmed recovery, bounded and independent of live OS/network."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from mirror_host_agent.delivery_feedback import DeliverySample
from mirror_host_agent.h264_encoder import TunedH264Encoder
from mirror_host_agent.h264_encoder import CapacityProbe


class MeasuredRecoveryTests(unittest.TestCase):
    def setUp(self):
        TunedH264Encoder.clear_receiver_network_health()
        self.enc=TunedH264Encoder()
        self.enc._initial_bitrate_applied=True
        self.enc._active_backend='libx264'
        self.enc.codec=SimpleNamespace()
        self.enc._target_bitrate=500_000
        self.enc._receiver_target_bitrate=500_000
        self.enc._recovery_ceiling=2_500_000
        for at in range(100,108): self.report(at,rx=450_000,tx=450_000)
        self.enc._receiver_estimate_at=107
        self.tick(107)
        self.assertIsNotNone(self.enc._capacity_probe)

    def tearDown(self):
        TunedH264Encoder.clear_receiver_network_health()

    def report(self,at,rx=600_000,tx=600_000,rtt=20,buffer=30,drops=0,decoded=15,loss=0):
        TunedH264Encoder.update_receiver_network_health(jitter_ms=6,packet_loss_percent=loss,
            route='turn',now=at,delivery=DeliverySample(1000,1000-loss,loss,buffer,1,decoded,drops,
                                                       0,round(rx/8) if rx is not None else None,rtt))
        if tx is not None: TunedH264Encoder.update_sender_delivery(sent_bps=tx,now=at+.01)

    def tick(self,at):
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=at):
            self.enc._update_capacity_probe(content_stable=False)

    def finish(self,**fields):
        # Whole one-second intervals must start after the one-second warmup.
        for at in (108.1,109.1,110.1,111.1,112.1): self.report(at,**fields);self.tick(at+.1)
        self.tick(113)

    def test_measured_delivery_can_confirm_despite_unchanged_remb(self):
        self.finish()
        self.assertIsNone(self.enc._capacity_probe)
        self.assertEqual(self.enc.target_bitrate,600_000)
        self.assertEqual(self.enc.receiver_target_bitrate,500_000)
        self.assertEqual(self.enc._probe_backoff,10)

    def test_partial_delivery_never_retains_more_than_observed(self):
        self.finish(rx=560_000,tx=580_000)
        self.assertEqual(self.enc.target_bitrate,560_000)

    def test_missing_static_or_capped_traffic_cannot_ratchet(self):
        for fields in ({'rx':None},{'tx':None},{'rx':50_000,'tx':50_000},
                       {'rx':500_000,'tx':600_000},{'rx':600_000,'tx':450_000}):
            with self.subTest(fields=fields):
                self.setUp();self.finish(**fields)
                self.assertEqual(self.enc.target_bitrate,500_000)

    def test_buffer_control_delay_frame_drops_or_loss_block_confirmation(self):
        for fields in ({'buffer':80},{'rtt':80},{'rtt':None},{'drops':1},{'decoded':0},{'loss':30}):
            with self.subTest(fields=fields):
                self.setUp();self.finish(**fields)
                self.assertEqual(self.enc.target_bitrate,500_000)

    def test_stale_sender_or_one_sample_cannot_confirm(self):
        self.report(112.1);self.tick(113)
        self.assertEqual(self.enc.target_bitrate,500_000)

    def test_success_does_not_allow_immediate_second_trial(self):
        self.finish();self.report(114);self.tick(114)
        self.assertIsNone(self.enc._capacity_probe)
        self.assertEqual(self.enc.target_bitrate,600_000)

    def test_partial_first_interval_cannot_confirm_from_rebuild_or_idr_bytes(self):
        self.report(108.1);self.report(109.1);self.tick(110)
        self.assertIsNotNone(self.enc._capacity_probe)
        self.assertEqual(self.enc.target_bitrate,600_000)
        self.report(110.1);self.tick(110.2)
        self.assertIsNone(self.enc._capacity_probe)

    def test_sparse_content_is_inconclusive_not_exponential_network_failure(self):
        self.finish(rx=100_000,tx=100_000)
        self.assertEqual(self.enc.target_bitrate,500_000)
        self.assertEqual(self.enc._probe_backoff,10)

    def test_payload_guard_preserves_trial_but_invalidates_pre_reset_intervals(self):
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=108):
            self.enc._reset_codec('payload-budget')
        self.assertIsNotNone(self.enc._capacity_probe)
        self.assertEqual(self.enc.target_bitrate,600_000)
        self.assertEqual(self.enc._capacity_probe.settled_after,109)
        self.report(109.1);self.report(110.1);self.tick(110.2)
        self.assertIsNotNone(self.enc._capacity_probe)
        self.report(111.1);self.tick(111.2)
        self.assertIsNone(self.enc._capacity_probe)
        self.assertEqual(self.enc.target_bitrate,600_000)

    def test_repeated_local_resets_cannot_extend_absolute_deadline(self):
        for at in (108,109,110,111,112):
            self.report(at)
            with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=at):
                self.enc._reset_codec('payload-budget')
        self.report(113);self.tick(113)
        self.assertIsNone(self.enc._capacity_probe)
        self.assertEqual(self.enc.target_bitrate,500_000)

    def test_measured_traffic_can_qualify_next_trial_beyond_old_clamped_remb(self):
        self.finish()
        self.enc._target_bitrate=720_000
        for at in range(115,123): self.report(at,rx=720_000,tx=720_000)
        self.enc._receiver_estimate_at=122
        self.tick(122)
        self.assertIsNotNone(self.enc._capacity_probe)
        self.assertEqual(self.enc.target_bitrate,864_000)
        self.assertEqual(self.enc.receiver_target_bitrate,500_000)

    def test_underutilized_encoder_only_keeps_new_payload_increment(self):
        self.enc._capacity_probe=CapacityProbe(107,500_000,600_000,'turn',400_000,30,20,baseline_sent_bps=400_000)
        self.finish(rx=480_000,tx=480_000)
        self.assertEqual(self.enc.target_bitrate,580_000)
        self.assertEqual(self.enc._validated_media_budget[1],480_000)

    def test_short_peak_and_long_quiet_interval_cannot_fake_delivered_capacity(self):
        self.enc._capacity_probe=CapacityProbe(107,500_000,600_000,'turn',400_000,30,20,baseline_sent_bps=400_000)
        for at,interval,rate in [(109,250,1_000_000),(111.8,2750,200_000)]:
            TunedH264Encoder.update_receiver_network_health(jitter_ms=6,packet_loss_percent=0,route='turn',now=at,
                delivery=DeliverySample(interval,1000,0,30,1,15,0,0,round(rate*interval/8000),20))
            TunedH264Encoder.update_sender_delivery(sent_bps=rate,now=at+.01)
        self.assertIsNone(self.enc._measured_probe_capacity(self.enc._capacity_probe,111.9))

    def test_repeated_low_high_content_cycle_cannot_ratchet_budget(self):
        self.enc._validated_media_budget=(600_000,600_000,'turn',106)
        self.enc._target_bitrate=720_000
        self.enc._capacity_probe=CapacityProbe(107,600_000,720_000,'turn',200_000,30,20,baseline_sent_bps=200_000)
        self.finish(rx=240_000,tx=240_000)
        self.assertEqual(self.enc.target_bitrate,600_000)
        self.assertEqual(self.enc._validated_media_budget[1],600_000)

    def test_missing_sender_baseline_or_no_real_growth_does_not_authorize_partial_budget(self):
        for baseline in (None,400_000):
            self.setUp()
            self.enc._capacity_probe=CapacityProbe(107,500_000,600_000,'turn',400_000,30,20,baseline_sent_bps=baseline)
            self.finish(rx=400_000,tx=400_000)
            self.assertEqual(self.enc.target_bitrate,500_000)
