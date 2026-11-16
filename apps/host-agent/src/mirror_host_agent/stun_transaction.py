"""Reviewed aioice 0.10.x adapter for a completed-transaction retry race.

The event loop may already have queued a retry when cancellation or a response
completes the Future, before run() gets its turn to cancel the TimerHandle.
Only completed transactions are skipped; pending retransmissions, timeouts and
authentication failures retain aioice's implementation. No site-packages edits.
"""
from __future__ import annotations

from importlib.metadata import version
import logging

from aioice import stun

LOGGER = logging.getLogger('mirror_host_agent')
_BaseTransaction = stun.Transaction


class SafeStunTransaction(_BaseTransaction):
    # Explicit base-private name is intentional: aioice run() and call_later()
    # resolve this callback on self. Gate installation to the reviewed version.
    def _Transaction__retry(self) -> None:
        if self._Transaction__future.done():
            return
        super()._Transaction__retry()


def install_safe_stun_transactions() -> None:
    if stun.Transaction is SafeStunTransaction:
        return
    if not version('aioice').startswith('0.10.'):
        raise RuntimeError('STUN adapter requires reviewed aioice 0.10.x')
    if stun.Transaction is not _BaseTransaction:
        raise RuntimeError('Unexpected STUN transaction adapter; review before replacing')
    stun.Transaction = SafeStunTransaction
    LOGGER.info('STUN transaction policy: completed-retry-v1; pending retries and authentication unchanged')
