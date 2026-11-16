"""Passive, per-peer control diagnostics for reviewed aiortc 1.15.0.

No wire fields, payload retention, scheduling/priority/reliability changes or
installed-package edits. Probe ACK is an SCTP transport ACK, NOT browser display
or input application completion. Client outbound queue/TCP retransmits remain
unknown. Diagnostic failures must never replace transport results/exceptions.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
from importlib.metadata import version
import json
import logging
import time

from aiortc.rtcsctptransport import DataChunk, USERDATA_MAX_LENGTH

LOGGER = logging.getLogger('mirror_host_agent')
INPUT_TRACE = ContextVar('mirror_input_trace', default=None)


class ControlTrace:
    def __init__(self, channel, *, clock=time.perf_counter):
        self.channel = channel
        self.sctp = channel.transport
        self.clock = clock
        self.pending = {}  # At most 64 small, content-free probe records.
        self.tsns = {}
        self.incoming = {}  # Numeric fragment metadata only, max 64 messages.
        self.rx_recent = {}  # Sequence -> observed reassembly delay, max 64.
        self.hooks = {}
        self.task = None
        self.closed = False
        self.supported = False
        self.retransmits = 0
        self.timeouts = 0
        self.loop_samples = deque(maxlen=64)
        self.last_probe_log = -float('inf')
        self.last_input_log = -float('inf')
        self.last_loop_log = -float('inf')
        self.last_rx_log = -float('inf')

    def safe(self, function, *args):
        if not self.closed:
            try:
                return function(*args)
            except Exception:
                pass  # Optional diagnostics cannot change control/media behavior.

    def install(self):
        if self.hooks or self.task is not None or self.closed:
            return
        try:
            if version('aiortc') != '1.15.0' or USERDATA_MAX_LENGTH != 1200:
                raise ValueError('unsupported SCTP version')
            for name in ('_send', '_send_chunk', '_receive_sack_chunk', '_t3_expired', '_receive_data_chunk', '_receive'):
                if not callable(getattr(self.sctp, name, None)):
                    raise ValueError('unsupported SCTP methods')
            for name in ('_data_channel_queue','_outbound_queue','_sent_queue','_flight_size','_cwnd','_local_tsn','_inbound_streams'):
                getattr(self.sctp, name)
            original_send = self.sctp._send
            original_chunk = self.sctp._send_chunk
            original_ack = self.sctp._receive_sack_chunk
            original_timeout = self.sctp._t3_expired
            original_rx = self.sctp._receive_data_chunk
            original_deliver = self.sctp._receive

            async def send(stream_id, pp_id, user_data, *args, **kwargs):
                self.safe(self.admit, stream_id, pp_id, user_data)
                return await original_send(stream_id, pp_id, user_data, *args, **kwargs)

            async def chunk(value):
                self.safe(self.transmit, value)
                return await original_chunk(value)

            async def ack(value):
                observed = self.clock()
                result = await original_ack(value)
                self.safe(self.acknowledge, value, observed)
                return result

            def timeout():
                if not self.closed:
                    self.timeouts += 1
                return original_timeout()

            async def rx(chunk):
                self.safe(self.receive_fragment,chunk)
                return await original_rx(chunk)

            async def deliver(stream_id, pp_id, data):
                self.safe(self.receive_message,stream_id,pp_id,data)
                return await original_deliver(stream_id,pp_id,data)

            for name, wrapper in (('_send',send),('_send_chunk',chunk),('_receive_sack_chunk',ack),
                                  ('_t3_expired',timeout),('_receive_data_chunk',rx),('_receive',deliver)):
                # Remember whether a previous instance override existed.
                self.hooks[name] = (name in self.sctp.__dict__, getattr(self.sctp,name), wrapper)
                setattr(self.sctp, name, wrapper)
            self.supported = True
        except Exception:
            self._restore()
            LOGGER.info('Control message diagnostics: SCTP observer unavailable; transport unchanged')
        self.task = asyncio.create_task(self.watch_loop())
        LOGGER.info('Control message diagnostics: control-trace-v1 active; sctp=%s; numeric-only; transport unchanged',self.supported)

    def queue_pong(self, sequence, received):
        if not self.supported or type(sequence) is not int or not 0 <= sequence <= 2**53-1:
            return
        self.expire()
        self.remove(sequence)
        while len(self.pending) >= 64:
            self.remove(next(iter(self.pending)))
        rx = self.rx_recent.pop(sequence,None)
        self.pending[sequence] = dict(received=received, queued=self.clock(), handled_ms=None,
            admitted=None, first_tx=None, retries=0, chunks=set(), acked=set(),
            base_retx=self.retransmits, base_timeouts=self.timeouts, queue=None,
            rx_wait_ms=rx[1] if rx else None, rx_duplicates=rx[2] if rx else None)

    def receive_fragment(self, chunk):
        # Only ordered control-v1. No fragment payload is copied or retained.
        if chunk.stream_id != self.channel.id or chunk.protocol != 51 or chunk.flags & 4:
            return
        key = chunk.stream_seq
        if key not in self.incoming:
            while len(self.incoming)>=64:
                self.incoming.pop(next(iter(self.incoming)))
            self.incoming[key] = dict(at=self.clock(),seen=set(),duplicates=0)
        row = self.incoming[key]
        if chunk.tsn in row['seen']:
            row['duplicates'] = min(65535,row['duplicates']+1)
        elif len(row['seen'])<64:
            row['seen'].add(chunk.tsn)

    def receive_message(self, stream_id, pp_id, data):
        if stream_id != self.channel.id or pp_id != 51 or not self.channel.ordered:
            return
        # Reviewed ordered pop_messages advances SSN immediately before yield.
        stream = self.sctp._inbound_streams.get(stream_id)
        key = (stream.sequence_number-1)%65536 if stream is not None else None
        row = self.incoming.pop(key,None)
        if row is None or len(data)>4096:
            return
        sequence = json.loads(data).get('sequence')
        if type(sequence) is not int or not 0<=sequence<=2**53-1:
            return
        now = self.clock();wait=(now-row['at'])*1000
        while len(self.rx_recent)>=64:
            self.rx_recent.pop(next(iter(self.rx_recent)))
        self.rx_recent[sequence] = (now,round(wait,2),row['duplicates'])
        if now-self.last_rx_log >= (1 if wait>=50 or row['duplicates'] else 10):
            self.last_rx_log=now
            LOGGER.info('Control receive: seq=%s reassembly_ms=%.2f observed_chunks=%s duplicate_chunks=%s observed_only=True',
                sequence,wait,len(row['seen']),row['duplicates'])

    def handled(self, sequence, received):
        row = self.pending.get(sequence)
        if row is not None:
            row['handled_ms'] = (self.clock()-received)*1000

    def admit(self, stream_id, pp_id, data):
        if stream_id != self.channel.id or pp_id != 51 or not isinstance(data,bytes) or len(data)>4096:
            return
        message = json.loads(data)
        if message.get('event') != 'session.pong':
            return
        sequence = message.get('sequence')
        if type(sequence) is not int:
            return
        row = self.pending.get(sequence)
        if row is None or row['admitted'] is not None:
            return
        # _send allocates ALL fragments synchronously before its first await in
        # reviewed 1.15.0. Only map TSNs; never modify chunks or retain payloads.
        row['admitted'] = self.clock()
        row['queue'] = (len(self.sctp._data_channel_queue),len(self.sctp._outbound_queue),
                        len(self.sctp._sent_queue),self.sctp._flight_size,self.sctp._cwnd)
        for i in range((len(data)+1199)//1200):
            tsn = (self.sctp._local_tsn+i) % 2**32
            row['chunks'].add(tsn)
            self.tsns[tsn] = sequence

    def transmit(self, chunk):
        if not isinstance(chunk, DataChunk):
            return
        repeated = getattr(chunk,'_sent_count',1)>1
        if repeated:
            self.retransmits += 1
        row = self.pending.get(self.tsns.get(chunk.tsn))
        if row is not None:
            if row['first_tx'] is None:
                row['first_tx'] = self.clock()
            row['retries'] += int(repeated)

    def acknowledge(self, sack, observed):
        for tsn, sequence in tuple(self.tsns.items()):
            delta = (tsn-sack.cumulative_tsn) % 2**32
            acknowledged = delta == 0 or delta >= 2**31 or any(a<=delta<=b for a,b in sack.gaps)
            if acknowledged:
                row = self.pending.get(sequence)
                if row is not None:
                    row['acked'].add(tsn)
                    if row['chunks'] and row['acked'] == row['chunks']:
                        self.emit(sequence,row,observed,'acked')
                        self.remove(sequence)

    def remove(self, sequence):
        row = self.pending.pop(sequence,None)
        if row:
            for tsn in row['chunks']:
                self.tsns.pop(tsn,None)

    def expire(self):
        now = self.clock()
        for key,row in tuple(self.incoming.items()):
            if now-row['at']>=5:self.incoming.pop(key,None)
        for key,row in tuple(self.rx_recent.items()):
            if now-row[0]>=5:self.rx_recent.pop(key,None)
        for sequence,row in tuple(self.pending.items()):
            if now-row['queued'] >= 5:
                self.emit(sequence,row,now,'unconfirmed-5s')
                self.remove(sequence)

    def emit(self, sequence, row, at, status):
        elapsed = (at-row['queued'])*1000
        pressure = elapsed>=100 or (row['rx_wait_ms'] or 0)>=50 or (row['handled_ms'] or 0)>=50 or row['retries']>0
        if not pressure and at-self.last_probe_log < 10:
            return
        if at-self.last_probe_log < 1:
            return
        self.last_probe_log = at
        ms = lambda end,start: None if end is None or start is None else round((end-start)*1000,2)
        queue = row['queue'] or (None,)*5
        loop = max((delay for when,delay in self.loop_samples if 0<=at-when<=5),default=0)
        LOGGER.info('Control probe: seq=%s status=%s handler_ms=%s app_queue_ms=%s '
            'sctp_queue_ms=%s tx_to_ack_ms=%s probe_retx=%s assoc_retx_delta=%s t3_delta=%s '
            'queued_messages=%s outbound_chunks=%s unacked_chunks=%s flight_bytes=%s cwnd_bytes=%s '
            'host_loop_max5s_ms=%.2f rx_reassembly_ms=%s rx_duplicate_chunks=%s',
            sequence,status,row['handled_ms'],ms(row['admitted'],row['queued']),
            ms(row['first_tx'],row['admitted']),ms(at,row['first_tx']) if status=='acked' else None,
            row['retries'],self.retransmits-row['base_retx'],self.timeouts-row['base_timeouts'],*queue,loop,
            row['rx_wait_ms'],row['rx_duplicates'])

    def process_input(self, controller, message, received):
        token = INPUT_TRACE.set((self,message['sequence']))
        try:
            return controller.handle(message,now=time.monotonic())
        finally:
            INPUT_TRACE.reset(token)
            self.safe(self.handler_done,message['sequence'],received)

    def handler_done(self, sequence, received):
        elapsed = (self.clock()-received)*1000
        if elapsed>=50:
            self.input_done(sequence,0,elapsed,'handler')

    def input_done(self, sequence, queued_ms, elapsed_ms, stage='os'):
        # Called by the single input worker. No keys/coordinates/text retained.
        now = self.clock()
        if self.closed or now-self.last_input_log < (1 if max(queued_ms,elapsed_ms)>=50 else 10):
            return
        self.last_input_log = now
        LOGGER.info('Control input timing: seq=%s stage=%s queue_ms=%.2f work_ms=%.2f sampled=True',
                    sequence,stage,queued_ms,elapsed_ms)

    async def watch_loop(self):
        while not self.closed:
            due = self.clock()+.1
            await asyncio.sleep(.1)
            now = self.clock()
            delay = max(0,now-due)*1000
            self.loop_samples.append((now,delay))
            self.safe(self.expire)
            if delay>=50 and now-self.last_loop_log>=1:
                self.last_loop_log = now
                LOGGER.info('Control host scheduling: tick_ms=100 late_ms=%.2f; diagnostic-only',delay)

    def _restore(self):
        for name,(existed,original,wrapper) in self.hooks.items():
            if getattr(self.sctp,name,None) is wrapper:
                if existed: setattr(self.sctp,name,original)
                else: delattr(self.sctp,name)
        self.hooks.clear()

    def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
        self._restore()
        self.pending.clear(); self.tsns.clear(); self.loop_samples.clear()
        self.incoming.clear();self.rx_recent.clear()
