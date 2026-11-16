import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mirror_host_agent.__main__ import M0Agent, AgentConfig
from mirror_host_agent.h264_encoder import TunedH264Encoder


class SenderDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        TunedH264Encoder.clear_receiver_network_health()
        self.agent=M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
            ticket='fixture-unused',ws_url='ws://127.0.0.1:8787/ws',heartbeat_stop_after_seconds=None))
        self.sender=SimpleNamespace(getStats=AsyncMock())
        self.agent._video_sender=self.sender

    async def asyncTearDown(self):
        await self.agent._close_peer()
        TunedH264Encoder.clear_receiver_network_health()

    async def sample(self,at,total):
        self.sender.getStats.return_value={'video':SimpleNamespace(type='outbound-rtp',kind='video',bytesSent=total)}
        with patch('mirror_host_agent.__main__.time.monotonic',return_value=at):
            await self.agent._sample_sender_delivery(self.sender)

    async def test_real_outbound_counter_delta_is_bounded_and_resets_safely(self):
        await self.sample(100,100_000);await self.sample(101,175_000)
        self.assertEqual(TunedH264Encoder._sender_delivery_history[-1],(101,600_000))
        await self.sample(102,1);await self.sample(106,1_000_000)
        self.assertEqual(len(TunedH264Encoder._sender_delivery_history),1)

    async def test_stats_error_or_old_sender_cannot_affect_current_peer(self):
        self.sender.getStats.side_effect=RuntimeError('fixture')
        await self.agent._sample_sender_delivery(self.sender)
        self.assertEqual(len(TunedH264Encoder._sender_delivery_history),0)
        self.sender.getStats.side_effect=None
        self.agent._video_sender=None
        await self.sample(101,1000)
        self.assertIsNone(self.agent._sender_byte_sample)

    async def test_only_one_pending_stats_task_and_teardown_cancels_it(self):
        gate=asyncio.Event()
        async def delayed():
            await gate.wait()
            return {}
        self.sender.getStats.side_effect=delayed
        data={'jitterMs':6,'packetLossPercent':0,'receivedBitrateMbps':.6,'route':'turn',
              'delivery':{'intervalMs':1000,'receivedPackets':100,'lostPackets':0,'bufferMs':20,'decodeMs':1,
                          'decodedFrames':15,'droppedFrames':0,'recoveredPackets':0,'receivedBytes':75000,'controlRttMs':20}}
        self.agent._handle_receiver_report(data)
        task=self.agent._sender_stats_task
        self.agent._handle_receiver_report(data)
        self.assertIs(self.agent._sender_stats_task,task)
        await asyncio.sleep(0)
        await self.agent._close_peer()
        self.assertTrue(task.done())
        self.assertIsNone(self.agent._sender_byte_sample)
        self.assertEqual(len(TunedH264Encoder._sender_delivery_history),0)
