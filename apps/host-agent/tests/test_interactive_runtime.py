import asyncio
import threading
import time
import unittest
from unittest.mock import Mock

from mirror_host_agent.input_control import FakeInputSink, InputController
from mirror_host_agent.interactive_runtime import InputDispatcher, QueuedInputSink, read_clipboard_off_loop


class InteractiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dispatcher = InputDispatcher()

    async def asyncTearDown(self):
        self.dispatcher.shutdown()
        if self.dispatcher.thread:
            await asyncio.to_thread(self.dispatcher.thread.join, 2)
            self.assertFalse(self.dispatcher.thread.is_alive())

    async def idle(self):
        self.assertTrue(await asyncio.to_thread(self.dispatcher.idle.wait, 2))

    def sink(self, backend=None):
        raw = backend or FakeInputSink()
        sink = QueuedInputSink(raw, self.dispatcher)
        sink.start()
        return sink, raw

    async def test_preserves_modifier_key_mouse_and_text_order(self):
        sink, raw = self.sink()
        sink.key('ControlLeft','down');sink.key('KeyC','down');sink.key('KeyC','up');sink.key('ControlLeft','up')
        sink.move_absolute(1,2);sink.mouse_button('left','down');sink.wheel(0,120);sink.mouse_button('left','up');sink.type_text('한글')
        await self.idle()
        self.assertEqual(raw.calls,[('key','ControlLeft','down'),('key','KeyC','down'),('key','KeyC','up'),('key','ControlLeft','up'),
            ('move',1,2),('button','left','down'),('wheel',0,120),('button','left','up'),('text','한글')])

    async def test_slow_sendinput_does_not_stall_main_event_loop(self):
        entered, proceed = threading.Event(), threading.Event()
        class Slow(FakeInputSink):
            def key(self,code,action):
                if action=='down': entered.set();proceed.wait(5)
                super().key(code,action)
        sink, raw = self.sink(Slow())
        sink.key('KeyA','down');sink.key('KeyA','up')
        ticks=0
        while not entered.is_set():await asyncio.sleep(.001)
        for _ in range(10):await asyncio.sleep(.01);ticks+=1
        self.assertEqual(ticks,10)
        self.assertFalse(self.dispatcher.idle.is_set())
        proceed.set()
        await self.idle()
        self.assertEqual(raw.calls,[('key','KeyA','down'),('key','KeyA','up')])

    async def test_release_discards_queued_keys_and_releases_running_down(self):
        entered, proceed = threading.Event(), threading.Event()
        class Slow(FakeInputSink):
            def key(self,code,action):
                if action=='down': entered.set();proceed.wait(2)
                super().key(code,action)
        sink, raw = self.sink(Slow())
        sink.key('ControlLeft','down')
        while not entered.is_set():await asyncio.sleep(.001)
        sink.key('KeyC','down');sink.key('KeyC','up')
        sink.release_all();proceed.set()
        await self.idle()
        self.assertEqual(raw.calls,[('key','ControlLeft','down'),('key','ControlLeft','up')])

    async def test_reconnect_cleans_old_modifiers_before_new_inputs_and_ignores_old_close(self):
        first, raw = self.sink()
        first.key('ShiftLeft','down');await self.idle()
        second, new_raw = self.sink()
        first.close();first.key('KeyX','down')
        second.key('KeyA','down');second.key('KeyA','up')
        await self.idle()
        self.assertEqual(raw.calls,[('key','ShiftLeft','down'),('key','ShiftLeft','up')])
        self.assertEqual(new_raw.calls,[('key','KeyA','down'),('key','KeyA','up')])

    async def test_queue_overflow_fails_closed_and_releases_actual_keys(self):
        entered, proceed = threading.Event(), threading.Event()
        class Slow(FakeInputSink):
            def key(self,code,action):
                if action=='down':entered.set();proceed.wait(2)
                super().key(code,action)
        sink, raw = self.sink(Slow());failed=Mock();sink.set_failure_handler(failed)
        sink.key('ShiftLeft','down')
        while not entered.is_set():await asyncio.sleep(.001)
        for _ in range(257):sink.key('KeyA','down')
        self.assertTrue(sink.failed);self.assertLessEqual(len(self.dispatcher.queue),256)
        proceed.set();await self.idle();await asyncio.sleep(0)
        failed.assert_called_once()
        self.assertEqual(raw.calls,[('key','ShiftLeft','down'),('key','ShiftLeft','up')])

    async def test_pointer_move_tail_coalesces_without_reordering_button(self):
        sink, raw = self.sink()
        with self.dispatcher.condition:
            for i in range(1000):sink.move_absolute(i,i)
            sink.mouse_button('left','down')
            for i in range(1000,2000):sink.move_absolute(i,i)
            sink.mouse_button('left','up')
            self.assertEqual(len(self.dispatcher.queue),4)
        await self.idle()
        self.assertEqual(raw.calls,[('move',999,999),('button','left','down'),('move',1999,1999),('button','left','up')])

    async def test_controller_cleanup_invalidates_backend_queue(self):
        sink, raw = self.sink()
        controller=InputController(sink,frame_width=1600,frame_height=900)
        controller.handle(dict(event='key.down',data={'code':'ShiftLeft'},sequence=1),now=1)
        await self.idle();controller.close();await self.idle()
        self.assertEqual(controller.pressed_count,0)
        self.assertEqual(raw.calls,[('key','ShiftLeft','down'),('key','ShiftLeft','up')])
        sink.key('KeyX','down');await self.idle()
        self.assertEqual(len(raw.calls),2)

    async def test_backend_error_does_not_kill_worker_or_leave_a_down_key(self):
        class Fails(FakeInputSink):
            def key(self,code,action):
                super().key(code,action)
                if action=='down':raise OSError('private details')
        sink, raw=self.sink(Fails());failed=Mock();sink.set_failure_handler(failed)
        with self.assertLogs('mirror_host_agent',level='WARNING') as logs:
            sink.key('KeyA','down');await self.idle();await asyncio.sleep(0)
        self.assertNotIn('private details',''.join(logs.output))
        failed.assert_called_once()
        self.assertEqual(raw.calls,[('key','KeyA','down'),('key','KeyA','up')])

    async def test_clipboard_read_is_off_loop_and_pool_is_serial(self):
        entered, proceed=threading.Event(),threading.Event()
        active=0;peak=0
        def slow():
            nonlocal active,peak
            active+=1;peak=max(peak,active);entered.set();proceed.wait(2);active-=1;return 'fixture'
        task=asyncio.create_task(read_clipboard_off_loop(slow))
        while not entered.is_set():await asyncio.sleep(.001)
        other=asyncio.create_task(read_clipboard_off_loop(slow))
        for _ in range(5):await asyncio.sleep(.01)
        self.assertFalse(task.done());proceed.set()
        self.assertEqual(await task,'fixture');self.assertEqual(await other,'fixture');self.assertEqual(peak,1)

