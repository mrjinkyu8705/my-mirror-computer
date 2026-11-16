from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mirror_host_agent.bulk_transfer import (
    BULK_BUFFER_BYTES, BULK_CHUNK_BYTES, wait_for_bulk_capacity,
)


class BulkTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_open_chunk_yields_before_admission(self):
        channel = SimpleNamespace(readyState="open", bufferedAmount=0)
        with patch("mirror_host_agent.bulk_transfer.asyncio.sleep") as sleep:
            self.assertTrue(await wait_for_bulk_capacity(channel,100,lambda:True,lambda:(channel,),lambda:None))
            sleep.assert_awaited_once_with(0)

    async def test_combined_queue_and_control_block_new_bulk(self):
        channel = SimpleNamespace(readyState="open", bufferedAmount=0)
        peer = SimpleNamespace(readyState="open", bufferedAmount=BULK_BUFFER_BYTES)
        control = SimpleNamespace(bufferedAmount=2048)
        waits = []
        async def sleep(delay):
            waits.append(delay)
            if len(waits)==2: peer.bufferedAmount=0
            if len(waits)==3: control.bufferedAmount=0
        with patch("mirror_host_agent.bulk_transfer.asyncio.sleep", sleep):
            self.assertTrue(await wait_for_bulk_capacity(channel,BULK_CHUNK_BYTES,lambda:True,lambda:(channel,peer),lambda:control))
        self.assertEqual(waits,[0,0.01,0.01])

    async def test_close_or_cancel_during_yield_prevents_send(self):
        for cancelled in (False,True):
            channel=SimpleNamespace(readyState="open",bufferedAmount=0)
            active=True
            async def sleep(delay):
                nonlocal active
                if cancelled: active=False
                else: channel.readyState="closed"
            with patch("mirror_host_agent.bulk_transfer.asyncio.sleep",sleep):
                self.assertFalse(await wait_for_bulk_capacity(channel,100,lambda:active,lambda:(channel,),lambda:None))

    async def test_invalid_chunks_and_stall_are_bounded(self):
        channel=SimpleNamespace(readyState="open",bufferedAmount=BULK_BUFFER_BYTES)
        with self.assertRaises(ValueError):
            await wait_for_bulk_capacity(channel,BULK_CHUNK_BYTES+1,lambda:True,lambda:(channel,),lambda:None)
        with patch("mirror_host_agent.bulk_transfer.asyncio.sleep"), patch("mirror_host_agent.bulk_transfer.time.monotonic",side_effect=[0,31]):
            self.assertFalse(await wait_for_bulk_capacity(channel,100,lambda:True,lambda:(channel,),lambda:None))


if __name__ == "__main__":
    unittest.main()
