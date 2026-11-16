from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aioice import turn
from mirror_host_agent.turn_transport import SafeTurnTransport, _public_target, install_safe_turn_transport


class TurnPolicyTests(unittest.TestCase):
    def test_only_public_unicast_targets(self):
        for host in ("192.168.0.5", "10.0.0.1", "127.0.0.1", "169.254.0.1", "224.0.0.1", "::1", "fe80::1", "fc00::1", "ff02::1", "100.64.0.1", "unknown.local"):
            with self.subTest(host=host):
                self.assertFalse(_public_target(host))
        for host in ("8.8.8.8", "104.30.1.1", "2001:4860:4860::8888"):
            self.assertTrue(_public_target(host))

    def test_install_is_idempotent_and_does_not_modify_send_data(self):
        original = turn.TurnClientMixin.send_data
        with patch.object(turn, "TurnTransport", turn.TurnTransport):
            install_safe_turn_transport()
            install_safe_turn_transport()
            self.assertIs(turn.TurnTransport, SafeTurnTransport)
            self.assertIs(turn.TurnClientMixin.send_data, original)


class TurnTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostics_classify_headers_without_logging_payload(self):
        transport,inner=self.make_transport()
        for data in (b'\x80\x64'+b'private media', b'\x80\xc9'+b'private report',
                     b'\x17\xfe'+b'private control', b'\x00\x01'):
            await transport._send(data,('8.8.8.8',5000))
        self.assertEqual(transport._diag_kinds,dict(rtp=1,rtcp=1,dtls=1,other=1))
        self.assertEqual(inner.send_data.await_count,4)

    def make_transport(self, server="turn.cloudflare.com"):
        inner = SimpleNamespace(server=(server, 3478), send_data=AsyncMock(),
                                peer_connect_waiters={}, refresh_task=None,
                                delete=AsyncMock(), transport=Mock())
        return SafeTurnTransport(inner), inner

    async def drain(self, transport):
        await asyncio.gather(*list(transport._pending), return_exceptions=True)
        await asyncio.sleep(0)

    async def test_private_cf_target_skipped_public_still_sent(self):
        transport, inner = self.make_transport()
        transport.sendto(b"private", ("192.168.0.5", 5000))
        transport.sendto(b"public", ("8.8.8.8", 5000))
        await self.drain(transport)
        inner.send_data.assert_awaited_once_with(b"public", ("8.8.8.8", 5000))

    async def test_custom_turn_allows_private_targets(self):
        transport, inner = self.make_transport("internal.example.com")
        transport.sendto(b"data", ("192.168.0.5", 5000))
        await self.drain(transport)
        inner.send_data.assert_awaited_once()

    async def test_failed_bind_releases_waiter_and_next_send_can_retry(self):
        transport, inner = self.make_transport()
        addr = ("8.8.8.8", 5000)
        count = 0
        async def send(data, peer):
            nonlocal count
            self.assertNotIn(peer, inner.peer_connect_waiters)
            count += 1
            inner.peer_connect_waiters[peer] = []
            if count == 1:
                raise RuntimeError("secret must not appear in log")
        inner.send_data.side_effect = send
        with self.assertLogs("mirror_host_agent", level="WARNING") as logs:
            transport.sendto(b"one", addr)
            transport.sendto(b"two", addr)
            await self.drain(transport)
        self.assertEqual(count, 2)
        self.assertEqual(inner.peer_connect_waiters, {})
        self.assertFalse(transport._pending)
        self.assertNotIn("secret", "".join(logs.output))

    async def test_cancelled_bind_releases_waiter_and_close_is_idempotent(self):
        transport, inner = self.make_transport()
        started = asyncio.Event()
        async def send(data, peer):
            inner.peer_connect_waiters[peer] = []
            started.set()
            await asyncio.Future()
        inner.send_data.side_effect = send
        transport.sendto(b"data", ("8.8.8.8", 5000))
        await started.wait()
        transport.close()
        transport.close()
        await transport._close_task
        self.assertFalse(inner.peer_connect_waiters)
        self.assertFalse(transport._pending)
        inner.delete.assert_awaited_once()
        inner.transport.close.assert_called_once()
        transport.sendto(b"ignored", ("8.8.8.8", 5000))
        self.assertEqual(inner.send_data.await_count, 1)

    async def test_other_peers_not_blocked_by_slow_bind(self):
        transport, inner = self.make_transport()
        release = asyncio.Event()
        fast = asyncio.Event()
        async def send(data, peer):
            if peer[0] == "8.8.8.8":
                await release.wait()
            else:
                fast.set()
        inner.send_data.side_effect = send
        transport.sendto(b"slow", ("8.8.8.8", 5000))
        transport.sendto(b"fast", ("1.1.1.1", 5000))
        await asyncio.wait_for(fast.wait(), 1)
        release.set()
        await self.drain(transport)

    async def test_close_awaits_refresh_cancellation(self):
        transport, inner = self.make_transport()
        refresh = asyncio.create_task(asyncio.sleep(100))
        inner.refresh_task = refresh
        transport.close()
        await transport._close_task
        self.assertTrue(refresh.cancelled())

    async def test_actual_aioice_failed_channel_bind_can_retry_without_hanging(self):
        inner = turn.TurnClientUdpProtocol(("turn.cloudflare.com", 3478), "user", "password", 600, 500)
        inner.transport = Mock()
        inner.channel_bind = AsyncMock(side_effect=[RuntimeError("denied"), None])
        transport = SafeTurnTransport(inner)
        addr = ("8.8.8.8", 5000)
        with self.assertLogs("mirror_host_agent", level="WARNING"):
            transport.sendto(b"one", addr)
            transport.sendto(b"two", addr)
            await asyncio.wait_for(self.drain(transport), 1)
        self.assertEqual(inner.channel_bind.await_count, 2)
        self.assertEqual(inner.peer_connect_waiters, {})
        self.assertIn(addr, inner.peer_to_channel)
        inner.transport.sendto.assert_called_once()

    async def test_delete_failure_still_closes_underlying_transport(self):
        transport, inner = self.make_transport()
        inner.delete.side_effect = RuntimeError("sensitive delete error")
        with self.assertLogs("mirror_host_agent", level="WARNING") as logs:
            transport.close()
            await asyncio.gather(transport._close_task, return_exceptions=True)
            await asyncio.sleep(0)
        inner.transport.close.assert_called_once()
        self.assertNotIn("sensitive", " ".join(logs.output))
