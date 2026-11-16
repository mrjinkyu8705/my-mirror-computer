"""Host text download tests use fake channels only, never the OS clipboard."""
import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mirror_host_agent.text_clipboard import TextClipboardDownloadSender, RECEIVE_MAX_CHARS
from mirror_host_agent.windows_clipboard import truncate_clipboard_text, CLIPBOARD_TEXT_MAX_LENGTH
from mirror_host_agent.__main__ import M0Agent, AgentConfig

SESSION='session_0123456789abcdef'

class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, accept=True, apply=True):
        sent=[]
        sequences=[0,0]
        active=[True]
        def envelope(event,data):
            sequences[0]+=1
            return json.dumps(dict(event=event,data=data,sessionId=SESSION,version=1,
                sequence=sequences[0],timestamp=int(time.time()*1000)))
        def ack(event,id,**overrides):
            sequences[1]+=1
            payload=dict(event=event,data={'transferId':id},sessionId=SESSION,
                sequence=sequences[1],version=1,timestamp=int(time.time()*1000))
            payload.update(overrides)
            sender.receive(json.dumps(payload))
        def send(raw):
            sent.append(raw)
            if isinstance(raw,str):
                msg=json.loads(raw)
                if msg['event']=='clipboard.text-offer' and accept: ack('clipboard.text-accept',msg['data']['transferId'])
                if msg['event']=='clipboard.text-complete' and apply: ack('clipboard.text-applied',msg['data']['transferId'])
        channel=SimpleNamespace(readyState='open',send=send)
        capacity=AsyncMock(return_value=True)
        sender=TextClipboardDownloadSender(SESSION,channel,envelope,lambda:active[0],capacity)
        return sender,sent,ack,active,capacity

    async def test_exact_korean_limit_is_sent_without_truncation_in_safe_chunks(self):
        sender,sent,_,_,capacity=self.fixture()
        text='한'*65_536
        await sender.send(text)
        chunks=[raw for raw in sent if isinstance(raw,bytes)]
        offer=json.loads(sent[0])['data']
        self.assertEqual(RECEIVE_MAX_CHARS,CLIPBOARD_TEXT_MAX_LENGTH)
        self.assertEqual(b''.join(chunks),text.encode('utf-8'))
        self.assertTrue(all(len(chunk)<=16384 for chunk in chunks))
        self.assertEqual(offer['chars'],65_536)
        self.assertEqual(offer['sha256'],hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(capacity.await_count,len(chunks))
        self.assertIsNone(sender.pending)

    async def test_astral_characters_use_utf16_limits(self):
        sender,sent,_,_,_=self.fixture()
        await sender.send('😀'*32_768)
        self.assertEqual(json.loads(sent[0])['data']['chars'],65_536)
        for text in ['a'*65_537,'😀'*32_769,'a\0b','\ud800','']:
            before=len(sent)
            with self.assertRaises((ValueError,UnicodeError)):
                await sender.send(text)
            self.assertEqual(len(sent),before)

    async def test_waits_for_acceptance_and_application_not_just_queueing(self):
        sender,sent,ack,_,_=self.fixture(False,False)
        task=asyncio.create_task(sender.send('한'*20_000))
        await asyncio.sleep(0)
        id=json.loads(sent[0])['data']['transferId']
        self.assertEqual(len(sent),1)
        ack('clipboard.text-accept',id)
        for _ in range(4): await asyncio.sleep(0)
        self.assertFalse(task.done())
        ack('clipboard.text-applied',id)
        await asyncio.wait_for(task,1)

    async def test_wrong_session_and_premature_ack_do_not_authorize_transfer(self):
        sender,sent,ack,_,_=self.fixture(False,False)
        task=asyncio.create_task(sender.send('hello'))
        await asyncio.sleep(0)
        id=json.loads(sent[0])['data']['transferId']
        ack('clipboard.text-applied',id)
        ack('clipboard.text-accept',id,sessionId='session_wrong_0123456789')
        await asyncio.sleep(0)
        self.assertEqual(len(sent),1)
        task.cancel(); await asyncio.gather(task,return_exceptions=True)
        self.assertIsNone(sender.pending)

    async def test_close_and_permission_loss_release_pending_tasks(self):
        for kind in ('close','permission'):
            sender,_,_,active,capacity=self.fixture(accept=(kind=='permission'))
            if kind=='permission':
                async def deny(*_): active[0]=False; return False
                capacity.side_effect=deny
            task=asyncio.create_task(sender.send('hello'))
            await asyncio.sleep(0)
            if kind=='close': sender.close()
            with self.assertRaises(RuntimeError): await asyncio.wait_for(task,1)
            self.assertIsNone(sender.pending)

    async def test_timeout_and_busy_are_bounded(self):
        sender,_,_,_,_=self.fixture(False,False)
        with patch('mirror_host_agent.text_clipboard.TIMEOUT_SECONDS',0.02):
            task=asyncio.create_task(sender.send('hello'))
            await asyncio.sleep(0)
            with self.assertRaisesRegex(RuntimeError,'BUSY'): await sender.send('second')
            with self.assertRaises(TimeoutError): await task
        self.assertIsNone(sender.pending)

    async def test_agent_bulk_routing_echo_suppression_and_legacy_fallback(self):
        agent=M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id=SESSION,ticket='unused',
            ws_url='ws://127.0.0.1:9/ws',heartbeat_stop_after_seconds=None,clipboard_enabled=True))
        control=SimpleNamespace(readyState='open',send=Mock())
        agent._control_channel=control
        agent._clipboard_text_download_channel=SimpleNamespace(readyState='open')
        sender=SimpleNamespace(send=AsyncMock())
        agent._clipboard_text_download_sender=sender
        text='한'*65_536
        self.assertTrue(await agent._forward_clipboard_text_snapshot(control,1,text))
        sender.send.assert_awaited_once_with(text)
        control.send.assert_not_called()
        agent._clipboard_text_echo=text;agent._clipboard_text_echo_sequence=2
        self.assertTrue(await agent._forward_clipboard_text_snapshot(control,2,text))
        self.assertEqual(sender.send.await_count,1)
        agent._clipboard_text_download_channel=None
        self.assertTrue(await agent._forward_clipboard_text_snapshot(control,3,text))
        self.assertEqual(len(json.loads(control.send.call_args.args[0])['data']['text']),16_384)

    def test_utf16_read_truncation_never_splits_an_astral_character(self):
        self.assertEqual(truncate_clipboard_text('a'*65_535+'😀'),'a'*65_535)
        self.assertEqual(truncate_clipboard_text('😀'*40_000),'😀'*32_768)
