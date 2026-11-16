from __future__ import annotations

import unittest
from unittest.mock import Mock

from mirror_host_agent.windows_input import (
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    WindowsInputSink,
)


class WindowsNumericKeypadTests(unittest.TestCase):
    @staticmethod
    def _sink() -> tuple[WindowsInputSink, Mock]:
        sink = object.__new__(WindowsInputSink)
        send = Mock()
        sink._send = send
        return sink, send

    def test_numeric_key_uses_vk_numpad_without_extended_flag(self) -> None:
        sink, send = self._sink()

        sink.key("Numpad7", "down")

        keyboard = send.call_args.args[0].union.ki
        self.assertEqual(keyboard.wVk, 0x67)
        self.assertEqual(keyboard.dwFlags, 0)

    def test_numpad_enter_uses_return_with_extended_flag(self) -> None:
        sink, send = self._sink()

        sink.key("NumpadEnter", "down")
        down = send.call_args.args[0].union.ki
        sink.key("NumpadEnter", "up")
        up = send.call_args.args[0].union.ki

        self.assertEqual(down.wVk, 0x0D)
        self.assertEqual(down.dwFlags, KEYEVENTF_EXTENDEDKEY)
        self.assertEqual(up.dwFlags, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)

    def test_divide_and_num_lock_are_extended_keys(self) -> None:
        for code, virtual_key in (("NumpadDivide", 0x6F), ("NumLock", 0x90)):
            with self.subTest(code=code):
                sink, send = self._sink()
                sink.key(code, "down")
                keyboard = send.call_args.args[0].union.ki
                self.assertEqual(keyboard.wVk, virtual_key)
                self.assertEqual(keyboard.dwFlags, KEYEVENTF_EXTENDEDKEY)

    def test_caps_lock_uses_vk_capital_without_extended_flag(self) -> None:
        sink, send = self._sink()

        sink.key("CapsLock", "down")
        down = send.call_args.args[0].union.ki
        sink.key("CapsLock", "up")
        up = send.call_args.args[0].union.ki

        self.assertEqual(down.wVk, 0x14)
        self.assertEqual(down.dwFlags, 0)
        self.assertEqual(up.wVk, 0x14)
        self.assertEqual(up.dwFlags, KEYEVENTF_KEYUP)
