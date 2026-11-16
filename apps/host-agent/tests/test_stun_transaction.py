from __future__ import annotations

import asyncio
import unittest
from unittest.mock import Mock, patch

from aioice import ice, stun, turn
from mirror_host_agent.stun_transaction import SafeStunTransaction, install_safe_stun_transactions


class StunTransactionTests(unittest.IsolatedAsyncioTestCase):
    def transaction(self, retransmissions=0):
        sender = Mock()
        request = stun.Message(stun.Method.BINDING, stun.Class.REQUEST)
        return SafeStunTransaction(request, ('127.0.0.1', 9), sender, retransmissions=retransmissions), sender

    async def test_queued_retry_after_cancellation_does_not_set_cancelled_future(self):
        transaction, sender = self.transaction()
        running = asyncio.create_task(transaction.run())
        await asyncio.sleep(0)
        handle = transaction._Transaction__timeout_handle
        running.cancel()
        handle._callback()  # already queued before run() executes its finally
        await asyncio.gather(running, return_exceptions=True)
        self.assertTrue(running.cancelled())
        self.assertTrue(handle.cancelled())
        self.assertEqual(sender.send_stun.call_count, 1)

    async def test_queued_retry_after_success_preserves_response_and_sends_no_extra_packet(self):
        transaction, sender = self.transaction(retransmissions=2)
        running = asyncio.create_task(transaction.run())
        await asyncio.sleep(0)
        callback = transaction._Transaction__timeout_handle._callback
        response = stun.Message(stun.Method.BINDING, stun.Class.RESPONSE)
        transaction.response_received(response, ('127.0.0.1', 9))
        callback()
        self.assertEqual(await running, (response, ('127.0.0.1', 9)))
        self.assertEqual(sender.send_stun.call_count, 1)

    async def test_queued_retry_after_timeout_does_not_complete_twice(self):
        transaction, _ = self.transaction()
        running = asyncio.create_task(transaction.run())
        await asyncio.sleep(0)
        callback = transaction._Transaction__timeout_handle._callback
        callback()
        callback()
        with self.assertRaises(stun.TransactionTimeout):
            await running

    async def test_authentication_rejection_is_not_hidden_by_late_retry(self):
        transaction, sender = self.transaction()
        running = asyncio.create_task(transaction.run())
        await asyncio.sleep(0)
        response = stun.Message(stun.Method.BINDING, stun.Class.ERROR)
        response.attributes['ERROR-CODE'] = (401, 'Unauthorized')
        transaction.response_received(response, ('127.0.0.1', 9))
        transaction._Transaction__timeout_handle._callback()
        with self.assertRaises(stun.TransactionFailed) as raised:
            await running
        self.assertIs(raised.exception.response, response)
        self.assertEqual(sender.send_stun.call_count, 1)

    async def test_pending_retry_count_and_exponential_schedule_remain_unchanged(self):
        transaction, sender = self.transaction(retransmissions=2)
        running = asyncio.create_task(transaction.run())
        await asyncio.sleep(0)
        delays = []
        for _ in range(3):
            handle = transaction._Transaction__timeout_handle
            delays.append(transaction._Transaction__timeout_delay)
            callback = handle._callback
            handle.cancel()
            callback()
        with self.assertRaises(stun.TransactionTimeout):
            await running
        self.assertEqual(sender.send_stun.call_count, 3)
        self.assertEqual(delays, [stun.RETRY_RTO*2, stun.RETRY_RTO*4, stun.RETRY_RTO*8])

    async def test_event_loop_receives_no_callback_errors_during_cancel_races(self):
        loop = asyncio.get_running_loop()
        errors = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        try:
            for _ in range(25):
                transaction, _ = self.transaction()
                running = asyncio.create_task(transaction.run())
                await asyncio.sleep(0)
                handle = transaction._Transaction__timeout_handle
                loop.call_soon(handle._callback)
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            self.assertEqual(errors, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_unexpected_sender_error_is_not_swallowed(self):
        transaction, sender = self.transaction()
        sender.send_stun.side_effect = OSError('synthetic transport failure')
        with self.assertRaises(OSError):
            await transaction.run()


class StunInstallationTests(unittest.TestCase):
    def test_install_is_idempotent_and_used_by_ice_and_turn(self):
        with patch.object(stun, 'Transaction', stun.Transaction):
            install_safe_stun_transactions()
            install_safe_stun_transactions()
            self.assertIs(ice.stun.Transaction, SafeStunTransaction)
            self.assertIs(turn.stun.Transaction, SafeStunTransaction)

    def test_unreviewed_dependency_is_rejected(self):
        with patch('mirror_host_agent.stun_transaction.version', return_value='0.11.0'):
            with self.assertRaises(RuntimeError):
                install_safe_stun_transactions()
