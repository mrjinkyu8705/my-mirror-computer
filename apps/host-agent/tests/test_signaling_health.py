import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from websockets.asyncio.server import serve, ServerConnection
from websockets.asyncio.client import connect, ClientConnection
from websockets.exceptions import ConnectionClosed
from websockets.frames import OP_PONG
from mirror_host_agent.signaling_health import SignalingHealth, HealthClientConnection, SignalingAckTimeout
from mirror_host_agent.__main__ import AgentConfig, M0Agent


def heartbeat(seq=1, session='session_0123456789abcdef'):
    return dict(type='agent.heartbeat',payload={},sequence=seq,sessionId=session,version=1)


class HealthTests(unittest.TestCase):
    def test_only_correlated_fresh_nonreplayed_acks_refresh_health(self):
        health=SignalingHealth(enabled=True,now=100)
        sent=heartbeat();health.sent(sent,now=101)
        for wrong in [dict(sent,type='agent.heartbeat-ack',sessionId='wrong'),
                      dict(sent,type='agent.heartbeat-ack',sequence=True),
                      dict(sent,type='agent.heartbeat-ack',payload={'x':1})]:
            self.assertFalse(health.accept(wrong,now=102))
        ack=dict(sent,type='agent.heartbeat-ack')
        self.assertTrue(health.accept(ack,now=102))
        self.assertEqual(health.last_rtt_ms,1000)
        self.assertFalse(health.accept(ack,now=103))
        self.assertFalse(health.expired(now=131))
        self.assertTrue(health.expired(now=132))
        for seq in range(2,100):health.sent(heartbeat(seq),now=103)
        self.assertEqual(len(health.pending),16)
        self.assertFalse(health.accept(dict(heartbeat(99),type='agent.heartbeat-ack'),now=134))

    def test_capability_required_to_disable_protocol_ping_task(self):
        for value,want in [('ack-v1',True),('other',False),(None,False)]:
            connection=object.__new__(HealthClientConnection)
            connection.response=SimpleNamespace(headers={} if value is None else {'x-mirror-app-heartbeat':value})
            with patch('websockets.asyncio.client.ClientConnection.start_keepalive') as legacy:
                connection.start_keepalive()
                self.assertEqual(connection.app_ack_supported,want)
                self.assertEqual(legacy.call_count,0 if want else 1)


class NoPongServer(ServerConnection):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        send=self.protocol.send_frame
        def without_pong(frame):
            if frame.opcode!=OP_PONG:send(frame)
        self.protocol.send_frame=without_pong


class LocalHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_ack_does_not_wait_for_dead_tcp_close_handshake(self):
        entered=asyncio.Queue()
        async def server(socket):
            socket.transport.pause_reading()
            entered.put_nowait(True)
            # A genuinely half-open peer cannot even read the Close request.
            # Resume later so this local fixture also shuts down cleanly.
            await asyncio.sleep(.5)
            socket.transport.resume_reading()
            await socket.wait_closed()
        async with serve(server,'127.0.0.1',0) as service:
            url=f'ws://127.0.0.1:{service.sockets[0].getsockname()[1]}/'
            elapsed=[]
            for connection in (ClientConnection,HealthClientConnection):
                loop=asyncio.get_running_loop()
                started=loop.time()
                with self.assertRaises(SignalingAckTimeout):
                    async with connect(url,create_connection=connection,close_timeout=.25) as socket:
                        await entered.get()
                        if isinstance(socket,HealthClientConnection):socket.abort_expired_ack()
                        raise SignalingAckTimeout('fixture expired app path')
                elapsed.append(loop.time()-started)
            self.assertGreater(elapsed[0],.20)
            self.assertLess(elapsed[1],.15)

    async def test_watchdog_aborts_only_after_the_unchanged_ack_deadline(self):
        from unittest.mock import Mock
        agent=M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
            ticket='fixture',ws_url='ws://127.0.0.1:9/ws',heartbeat_stop_after_seconds=None))
        agent._websocket=SimpleNamespace(abort_expired_ack=Mock())
        agent._signaling_health=SimpleNamespace(expired=Mock(side_effect=[False,True]))
        with patch('mirror_host_agent.__main__.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(SignalingAckTimeout):await agent._watch_signaling_health()
        self.assertEqual(agent._signaling_health.expired.call_count,2)
        agent._websocket.abort_expired_ack.assert_called_once()

    async def test_normal_close_retains_original_graceful_handshake(self):
        async def server(socket):
            await socket.wait_closed()
        async with serve(server,'127.0.0.1',0) as service:
            url=f'ws://127.0.0.1:{service.sockets[0].getsockname()[1]}/'
            async with connect(url,create_connection=HealthClientConnection) as socket:
                pass
            self.assertEqual(socket.close_code,1000)

    async def test_legacy_pong_timeout_can_close_while_application_echo_is_healthy(self):
        async def server(socket):
            try:
                async for raw in socket:await socket.send(raw)
            except ConnectionClosed:pass
        async with serve(server,'127.0.0.1',0,create_connection=NoPongServer) as service:
            port=service.sockets[0].getsockname()[1];echoes=0
            with self.assertRaises(ConnectionClosed):
                async with connect(f'ws://127.0.0.1:{port}/',ping_interval=.05,ping_timeout=.3,
                                   create_connection=ClientConnection) as socket:
                    for index in range(30):
                        await socket.send('synthetic-heartbeat')
                        await socket.recv();echoes+=1
                        await asyncio.sleep(.02)
            self.assertGreater(echoes,2)

    async def test_real_socket_healthy_app_survives_missing_pongs_and_dead_app_still_times_out(self):
        alive=True
        async def server(socket):
            try:
                async for raw in socket:
                    message=json.loads(raw)
                    if alive and message['type']=='agent.heartbeat':
                        await socket.send(json.dumps(dict(message,type='agent.heartbeat-ack')))
            except ConnectionClosed:pass
        async def negotiate(connection,request,response):
            response.headers['x-mirror-app-heartbeat']='ack-v1'
        async with serve(server,'127.0.0.1',0,create_connection=NoPongServer,process_response=negotiate) as service:
            port=service.sockets[0].getsockname()[1]
            agent=M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
                ticket='fixture',ws_url=f'ws://127.0.0.1:{port}/ws',heartbeat_stop_after_seconds=None))
            async def heartbeats(socket):
                while True:
                    message=agent._message('agent.heartbeat',{});agent._signaling_health.sent(message)
                    await socket.send(json.dumps(message));await asyncio.sleep(.02)
            async def watch():
                while True:
                    await asyncio.sleep(.01)
                    if agent._signaling_health.expired(timeout=.12):
                        from mirror_host_agent.signaling_health import SignalingAckTimeout
                        raise SignalingAckTimeout('fixture')
            with patch('mirror_host_agent.__main__.SIGNALING_PING_INTERVAL_SECONDS',.03), \
                 patch('mirror_host_agent.__main__.SIGNALING_PING_TIMEOUT_SECONDS',.09), \
                 patch.object(agent,'_send_heartbeats',heartbeats),patch.object(agent,'_watch_signaling_health',watch):
                running=asyncio.create_task(agent.run())
                try:
                    await asyncio.sleep(.3)
                    self.assertFalse(running.done())
                    self.assertTrue(agent._signaling_health.enabled)
                    self.assertIsNone(agent._websocket.keepalive_task)
                    self.assertLessEqual(len(agent._signaling_health.pending),16)
                    alive=False
                    from mirror_host_agent.signaling_health import SignalingAckTimeout
                    with self.assertRaises(SignalingAckTimeout):await asyncio.wait_for(running,.5)
                finally:
                    running.cancel();await asyncio.gather(running,return_exceptions=True)

    async def test_ack_does_not_adopt_a_stale_viewer_session(self):
        agent=M0Agent(AgentConfig(device_id='device_0123456789abcdef',session_id='session_0123456789abcdef',
            ticket='fixture',ws_url='ws://127.0.0.1:9/ws',heartbeat_stop_after_seconds=None))
        agent._signaling_health=SignalingHealth(enabled=True)
        message=heartbeat();agent._signaling_health.sent(message)
        agent._session_id='session_new_viewer'
        await agent._handle_message(None,json.dumps(dict(message,type='agent.heartbeat-ack')))
        self.assertEqual(agent._session_id,'session_new_viewer')
        self.assertEqual(agent._signaling_health.last_ack_sequence,1)
