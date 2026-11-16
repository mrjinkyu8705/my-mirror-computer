"""Recovery feedback and real-codec regressions; no capture/clipboard/OS input."""
from __future__ import annotations

import fractions
import unittest
from unittest.mock import patch

import av
import numpy as np

from mirror_host_agent.h264_encoder import (
    BALANCED_INITIAL_BITRATE, CapacityProbe, RECOVERY_PROBE_MAX_BACKOFF_SECONDS,
    SOFTWARE_H264_BACKEND, TunedH264Encoder, VBV_REBUILD_HOLD_SECONDS,
)


class QualityRecoveryTests(unittest.TestCase):
    def setUp(self):
        TunedH264Encoder.clear_receiver_network_health()
        self.encoder = TunedH264Encoder()
        self.encoder._initial_bitrate_applied = True
        self.encoder._target_bitrate = 500_000
        self.encoder._receiver_target_bitrate = 500_000
        self.encoder._recovery_ceiling = BALANCED_INITIAL_BITRATE
        self.encoder._profile_max_bitrate = 5_000_000
        self.encoder._active_backend = SOFTWARE_H264_BACKEND
        self.frame = av.VideoFrame.from_ndarray(np.zeros((64,64,3), dtype=np.uint8), format='rgb24').reformat(format='yuv420p')
        self.frame.pts = 0
        self.frame.time_base = fractions.Fraction(1,90_000)
        # Open an actual codec; the fixture then sets its selected profile cap.
        list(self.encoder._encode_frame(self.frame, False))
        self.encoder._recovery_ceiling = BALANCED_INITIAL_BITRATE
        self.encoder._profile_max_bitrate = 5_000_000

    def tearDown(self):
        TunedH264Encoder.clear_receiver_network_health()

    def report(self, now, *, loss=0, jitter=7, route='turn'):
        TunedH264Encoder.update_receiver_network_health(
            packet_loss_percent=loss, jitter_ms=jitter, route=route, now=now)

    def healthy(self, end=106):
        for now in range(end-6, end+1):
            self.report(now)
        self.encoder._receiver_estimate_at = end

    def tick(self, now, stable=False):
        with patch('mirror_host_agent.h264_encoder.time.monotonic', return_value=now):
            self.encoder._update_capacity_probe(content_stable=stable)

    def start(self):
        self.healthy()
        self.tick(106)
        self.assertIsNotNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate, 600_000)

    def test_small_probe_requires_stable_feedback_and_moving_content(self):
        self.report(106)
        self.encoder._receiver_estimate_at = 106
        self.tick(106)
        self.assertIsNone(self.encoder._capacity_probe)
        self.healthy()
        self.tick(106, stable=True)
        self.assertIsNone(self.encoder._capacity_probe)
        self.tick(106)
        self.assertEqual(self.encoder.target_bitrate, 600_000)
        self.assertEqual(self.encoder.receiver_target_bitrate, 500_000)

    def test_new_capacity_and_new_healthy_report_confirm_probe(self):
        self.start()
        self.report(107)
        with patch('mirror_host_agent.h264_encoder.time.monotonic', return_value=107):
            self.encoder.target_bitrate = 620_000
        self.tick(107)
        self.assertIsNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate, 600_000)
        self.assertEqual(self.encoder._probe_next_at, 117)

    def test_old_estimate_never_confirms_and_fresh_partial_keeps_only_acknowledged_rate(self):
        for estimate, at, expected in ((700_000,106,500_000), (590_000,111,590_000), (500_000,111,500_000)):
            with self.subTest(estimate=estimate, at=at):
                self.encoder._capacity_probe = CapacityProbe(106,500_000,600_000)
                self.encoder._target_bitrate = 600_000
                self.encoder._receiver_target_bitrate = estimate
                self.encoder._receiver_estimate_at = at
                self.report(112)
                self.tick(112)
                self.assertIsNone(self.encoder._capacity_probe)
                # A fresh partial RTCP acknowledgement is not a full trial
                # confirmation; retain only its measured rate (v5 contract).
                self.assertEqual(self.encoder.target_bitrate,expected)

    def test_new_estimate_with_old_health_report_never_confirms(self):
        self.start()
        self.encoder._receiver_target_bitrate = 700_000
        self.encoder._receiver_estimate_at = 107
        self.tick(107)
        self.assertIsNotNone(self.encoder._capacity_probe)

    def test_path_change_during_probe_aborts_instead_of_confirming(self):
        self.start()
        self.report(107,route='direct')
        self.encoder._receiver_target_bitrate=700_000
        self.encoder._receiver_estimate_at=107
        self.tick(107)
        self.assertIsNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,500_000)

    def test_pre_trial_feedback_arriving_immediately_is_not_confirmation(self):
        self.start()
        self.report(106.1)
        self.encoder._receiver_target_bitrate=700_000
        self.encoder._receiver_estimate_at=106.1
        self.tick(106.1)
        self.assertIsNotNone(self.encoder._capacity_probe)

    def test_late_ack_does_not_extend_six_second_trial(self):
        self.start()
        self.report(112.1)
        self.encoder._receiver_target_bitrate=700_000
        self.encoder._receiver_estimate_at=112.1
        self.tick(112.1)
        self.assertIsNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,500_000)

    def test_expiry_rolls_back_without_upward_ratchet(self):
        self.start()
        self.report(112)
        self.tick(112)
        self.assertEqual(self.encoder.target_bitrate, 500_000)
        self.assertEqual(self.encoder._probe_backoff,20)
        self.healthy(118)
        self.tick(118)
        self.assertIsNone(self.encoder._capacity_probe)

    def test_probe_aborts_on_loss_jitter_missing_or_stale_feedback(self):
        for loss,jitter,report_at in ((1,7,107),(0,30,107),(None,7,107),(0,None,107),(0,7,103)):
            with self.subTest(loss=loss,jitter=jitter,report_at=report_at):
                self.encoder._capacity_probe=CapacityProbe(106,500_000,600_000)
                self.encoder._target_bitrate=600_000
                TunedH264Encoder.clear_receiver_network_health()
                self.report(report_at,loss=loss,jitter=jitter)
                self.tick(107)
                self.assertIsNone(self.encoder._capacity_probe)
                self.assertEqual(self.encoder.target_bitrate,500_000)

    def test_true_downshift_is_never_reversed_by_probe_abort(self):
        self.start()
        self.encoder._target_bitrate=500_000
        self.report(107,loss=12)
        self.tick(107)
        self.assertEqual(self.encoder.target_bitrate,500_000)

    def test_probe_never_starts_with_sparse_stale_or_incomplete_feedback(self):
        for case in ('gap','stale','loss','jitter','null','unknown-route','no-estimate'):
            with self.subTest(case=case):
                TunedH264Encoder.clear_receiver_network_health()
                self.healthy()
                if case=='gap':
                    TunedH264Encoder.clear_receiver_network_health()
                    self.report(100); self.report(106)
                if case=='stale': self.encoder._receiver_estimate_at=100
                if case=='loss': self.report(106.1,loss=1)
                if case=='jitter': self.report(106.1,jitter=30)
                if case=='null': self.report(106.1,loss=None)
                if case=='unknown-route': self.report(106.1,route='unknown')
                if case=='no-estimate': self.encoder._receiver_estimate_at=None
                self.tick(106.2)
                self.assertIsNone(self.encoder._capacity_probe)

    def test_one_good_report_does_not_clear_mildly_bad_history(self):
        self.healthy()
        self.report(105.5,loss=1)  # Below ordinary2% congestion threshold.
        self.report(106)
        self.tick(106)
        self.assertIsNone(self.encoder._capacity_probe)

    def test_path_change_and_new_session_clear_probe_history(self):
        self.healthy()
        self.report(106.1,route='direct')
        self.tick(106.1)
        self.assertIsNone(self.encoder._capacity_probe)
        TunedH264Encoder.clear_receiver_network_health()
        self.assertEqual(len(TunedH264Encoder._receiver_probe_history),0)

    def test_trial_is_capped_and_not_started_above_profile_initial(self):
        self.healthy()
        self.encoder._target_bitrate=2_400_000
        self.encoder._receiver_target_bitrate=2_400_000
        self.tick(106)
        self.assertEqual(self.encoder.target_bitrate,2_500_000)
        self.encoder._capacity_probe=None
        self.encoder._target_bitrate=2_500_000
        self.encoder._receiver_target_bitrate=2_500_000
        self.tick(106)
        self.assertIsNone(self.encoder._capacity_probe)

    def test_trial_extra_is_bounded_to_200kbps(self):
        self.healthy()
        self.encoder._target_bitrate=2_000_000
        self.encoder._receiver_target_bitrate=2_000_000
        self.tick(106)
        self.assertEqual(self.encoder.target_bitrate,2_200_000)

    def test_no_trial_while_normal_recovery_is_pending(self):
        self.healthy()
        self.encoder._pending_higher_bitrate=900_000
        self.tick(106)
        self.assertIsNone(self.encoder._capacity_probe)

    def test_unverified_trial_cannot_stack_normal_upshifts(self):
        self.start()
        self.encoder._pending_higher_bitrate=2_500_000
        self.encoder._last_upshift_at=100
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=107):
            self.encoder._apply_pending_higher_bitrate()
        self.assertEqual(self.encoder.target_bitrate,600_000)

    def test_no_trial_if_raw_estimate_is_much_lower_or_codec_missing(self):
        self.healthy()
        self.encoder._receiver_target_bitrate=300_000
        self.tick(106)
        self.assertIsNone(self.encoder._capacity_probe)
        self.encoder._receiver_target_bitrate=500_000
        self.encoder.codec=None
        self.tick(106)
        self.assertIsNone(self.encoder._capacity_probe)

    def test_failed_probe_backoff_is_bounded(self):
        for now in range(100,120):
            self.encoder._capacity_probe=CapacityProbe(now-6,500_000,600_000)
            self.encoder._target_bitrate=600_000
            self.tick(now)
        self.assertEqual(self.encoder._probe_backoff,RECOVERY_PROBE_MAX_BACKOFF_SECONDS)
        self.assertLessEqual(self.encoder._probe_next_at-119,RECOVERY_PROBE_MAX_BACKOFF_SECONDS)

    def test_payload_reset_blocks_immediate_vbv_regrowth_but_not_later_recovery(self):
        self.healthy()
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=106):
            self.encoder._reset_codec('payload-budget')
            list(self.encoder._encode_frame(self.frame,False))
        codec=self.encoder.codec
        self.encoder._recovery_ceiling=BALANCED_INITIAL_BITRATE
        self.assertEqual(self.encoder._vbv_growth_blocked_until,106+VBV_REBUILD_HOLD_SECONDS)
        self.report(107)
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=107):
            self.encoder.target_bitrate=900_000
            self.encoder._last_upshift_at=100
            self.encoder._apply_pending_higher_bitrate()
        self.assertIs(self.encoder.codec,codec)
        self.assertEqual(self.encoder.target_bitrate,500_000)
        self.report(115)
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=115):
            self.encoder.target_bitrate=900_000
            self.encoder._apply_pending_higher_bitrate()
        self.assertEqual(self.encoder.target_bitrate,625_000)

    def test_payload_reset_preserves_trial_and_restarts_measurement_warmup(self):
        self.start()
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=107):
            self.encoder._reset_codec('payload-budget')
        self.assertIsNotNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,600_000)
        self.assertEqual(self.encoder._capacity_probe.settled_after,108)
        self.assertEqual(self.encoder._vbv_growth_blocked_until,107+VBV_REBUILD_HOLD_SECONDS)

    def test_network_congestion_still_lowers_target_without_a_quality_floor(self):
        self.encoder._target_bitrate=2_500_000
        self.encoder._pending_lower_bitrate=500_000
        self.encoder._pending_lower_since=0
        for now in (100,101,102): self.report(now,loss=15,jitter=70)
        with patch('mirror_host_agent.h264_encoder.time.monotonic',return_value=102):
            self.encoder._apply_pending_lower_bitrate(3,content_stable=True)
        self.assertEqual(self.encoder.target_bitrate,500_000)

    def test_probe_history_is_bounded(self):
        for now in range(100,250): self.report(now)
        self.assertEqual(len(TunedH264Encoder._receiver_probe_history),32)

    def test_report_cadence_does_not_expire_start_history_between_encode_frames(self):
        for i in range(21): self.report(100+i*1.05)
        self.encoder._receiver_estimate_at=121
        self.assertTrue(self.encoder._probe_has_healthy_history(121))
        self.assertTrue(self.encoder._probe_has_healthy_history(121.8))
        self.tick(121.8)
        self.assertIsNotNone(self.encoder._capacity_probe)

    def test_active_probe_survives_history_expiry_without_new_bad_feedback(self):
        for i in range(21): self.report(100+i*1.05)
        self.encoder._receiver_estimate_at=121
        self.tick(121)
        self.tick(121.8)
        self.assertIsNotNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,600_000)
        self.report(122.05)
        self.encoder._receiver_estimate_at=122.1
        self.encoder._receiver_target_bitrate=600_000
        self.tick(122.1)
        self.assertIsNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,600_000)

    def test_active_probe_still_aborts_when_current_report_becomes_stale(self):
        self.start()
        self.tick(108.6)
        self.assertIsNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,500_000)

    def test_new_bad_feedback_still_aborts_a_started_probe_immediately(self):
        self.start()
        self.report(107,loss=5)
        self.tick(107)
        self.assertIsNone(self.encoder._capacity_probe)
        self.assertEqual(self.encoder.target_bitrate,500_000)

    def test_weighted_loss_and_stable_turn_jitter_can_start_guarded_recovery(self):
        from mirror_host_agent.delivery_feedback import DeliverySample
        for i in range(12):
            # A lone lost packet yields a large raw per-sample percentage but
            # stays <0.5% over the measured count-weighted window.
            packets,lost=(15,1) if i%3==0 else (1000,0)
            TunedH264Encoder.update_receiver_network_health(jitter_ms=35,
                packet_loss_percent=100*lost/(packets+lost),route='turn',now=100+i,
                delivery=DeliverySample(1000,packets,lost,150,1,15,0))
        self.encoder._receiver_estimate_at=111
        self.tick(111)
        self.assertIsNotNone(self.encoder._capacity_probe)
        self.assertLessEqual(self.encoder.target_bitrate,600_000)

    def test_unknown_or_large_jitter_does_not_authorize_weighted_probe(self):
        from mirror_host_agent.delivery_feedback import DeliverySample
        for jitter in (None,70):
            TunedH264Encoder.clear_receiver_network_health()
            for i in range(12):
                TunedH264Encoder.update_receiver_network_health(jitter_ms=jitter,
                    packet_loss_percent=0,route='turn',now=100+i,
                    delivery=DeliverySample(1000,100,0,80,1,15,0))
            self.encoder._receiver_estimate_at=111
            self.tick(111)
            self.assertIsNone(self.encoder._capacity_probe)


if __name__=='__main__': unittest.main()
