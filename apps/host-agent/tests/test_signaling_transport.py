from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, patch

from mirror_host_agent.__main__ import AgentConfig, M0Agent


class IdleSocket:
    latency = 0.012

    def __init__(self):
        self.send = AsyncMock()
        self.receiver_stopped = asyncio.Event()

    async def __aiter__(self):
        try:
            await asyncio.Future()
            yield ''
        finally:
            self.receiver_stopped.set()


class SignalingTransportTests(unittest.IsolatedAsyncioTestCase):
    def agent(self):
        return M0Agent(AgentConfig(device_id='device_0123456789abcdef', session_id='session_0123456789abcdef',
            ticket='secret-ticket', ws_url='ws://127.0.0.1:9/ws', heartbeat_stop_after_seconds=None))

    def context(self, socket):
        context = AsyncMock()
        context.__aenter__.return_value = socket
        return patch('mirror_host_agent.__main__.connect', return_value=context)

    async def test_failed_heartbeat_promptly_unblocks_receiver_and_cleans_up(self):
        agent, socket = self.agent(), IdleSocket()
        with self.context(socket), patch.object(agent, '_send_heartbeats', AsyncMock(side_effect=OSError('secret transport URL'))):
            with self.assertLogs('mirror_host_agent', level='INFO') as logs:
                with self.assertRaises(OSError):
                    await asyncio.wait_for(agent.run(), timeout=0.5)
        self.assertTrue(socket.receiver_stopped.is_set())
        self.assertFalse(agent._connected)
        self.assertIsNone(agent._websocket)
        self.assertIsNone(agent._heartbeat_task)
        self.assertNotIn('secret', ' '.join(logs.output))
        self.assertIn('Signaling transport ended link=', ' '.join(logs.output))

    async def test_failed_heartbeat_preserves_a_healthy_media_peer(self):
        agent, socket = self.agent(), IdleSocket()
        peer = SimpleNamespace(connectionState='connected')
        agent._peer = peer
        agent._control_channel = SimpleNamespace(readyState='open')
        agent._last_control_ping_monotonic = time.monotonic()
        agent._granted_control = True
        close = AsyncMock()
        with self.context(socket), patch.object(agent, '_send_heartbeats', AsyncMock(side_effect=OSError('failure'))), patch.object(agent, '_close_peer', close):
            with self.assertRaises(OSError):
                await asyncio.wait_for(agent.run(), 0.5)
        close.assert_not_awaited()
        self.assertIs(agent._peer, peer)

    async def test_announcement_send_failure_also_clears_connection_state(self):
        agent, socket = self.agent(), IdleSocket()
        socket.send.side_effect = OSError('failure')
        with self.context(socket):
            with self.assertRaises(OSError):
                await agent.run()
        self.assertFalse(agent._connected)
        self.assertIsNone(agent._websocket)
        self.assertIsNone(agent._heartbeat_task)

    async def test_parent_cancellation_cleans_up_both_tasks(self):
        agent, socket = self.agent(), IdleSocket()
        sender_stopped = asyncio.Event()
        async def sender(_socket):
            try:
                await asyncio.Future()
            finally:
                sender_stopped.set()
        with self.context(socket), patch.object(agent, '_send_heartbeats', sender):
            running = asyncio.create_task(agent.run())
            for _ in range(4):
                await asyncio.sleep(0)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
        self.assertTrue(socket.receiver_stopped.is_set())
        self.assertTrue(sender_stopped.is_set())
        self.assertFalse(agent._connected)

    async def test_normal_test_mode_heartbeat_stop_keeps_receiving(self):
        agent, socket = self.agent(), IdleSocket()
        with self.context(socket), patch.object(agent, '_send_heartbeats', AsyncMock()):
            running = asyncio.create_task(agent.run())
            await asyncio.sleep(0.02)
            self.assertFalse(running.done())
            self.assertTrue(agent._connected)
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_receiver_failure_cancels_pending_heartbeat(self):
        agent, socket = self.agent(), IdleSocket()
        async def sender(_socket):
            await asyncio.Future()
        with self.context(socket), patch.object(agent, '_send_heartbeats', sender), \
             patch.object(agent, '_receive_signaling', AsyncMock(side_effect=ValueError('synthetic protocol rejection'))):
            with self.assertRaises(ValueError):
                await agent.run()
        self.assertIsNone(agent._heartbeat_task)
        self.assertFalse(agent._connected)

    async def test_correlation_changes_per_socket_but_instance_auth_is_stable(self):
        agent = self.agent()
        instance = agent._instance_id
        ids = []
        for _ in range(2):
            socket = IdleSocket()
            with self.context(socket) as connect, patch.object(agent, '_send_heartbeats', AsyncMock(side_effect=OSError('failure'))):
                with self.assertRaises(OSError):
                    await agent.run()
                headers = connect.call_args.kwargs['additional_headers']
                ids.append(headers['x-mirror-signaling-link'])
                self.assertEqual(headers['x-mirror-agent-instance'], instance)
                self.assertEqual(connect.call_args.kwargs['ping_interval'], 10.0)
                self.assertEqual(connect.call_args.kwargs['ping_timeout'], 30.0)
                self.assertEqual(json.loads(socket.send.call_args.args[0])['type'], 'agent.online')
        self.assertRegex(ids[0], r'^[a-f0-9]{16}$')
        self.assertNotEqual(*ids)
        self.assertNotIn(instance, ids)
