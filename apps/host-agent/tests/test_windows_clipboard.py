from __future__ import annotations

import io
import sys
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from mirror_host_agent.windows_clipboard import (
    _normalize_image_to_png,
    _png_to_dib,
)
from mirror_host_agent import windows_clipboard as clipboard


class ClipboardImageConversionTests(unittest.TestCase):
    @staticmethod
    def _png() -> bytes:
        output = io.BytesIO()
        Image.new("RGBA", (32, 18), (20, 180, 140, 160)).save(
            output, format="PNG"
        )
        return output.getvalue()

    def test_png_round_trips_through_windows_dib(self) -> None:
        dib = _png_to_dib(self._png())
        self.assertIsNotNone(dib)

        normalized = _normalize_image_to_png(dib or b"", dib=True)
        self.assertIsNotNone(normalized)
        image = Image.open(io.BytesIO(normalized or b""))
        self.assertEqual(image.size, (32, 18))
        self.assertEqual(image.format, "PNG")

    def test_rejects_invalid_image_bytes(self) -> None:
        self.assertIsNone(_png_to_dib(b"not-an-image"))
        self.assertIsNone(_normalize_image_to_png(b"not-an-image", dib=False))

    @unittest.skipUnless(sys.platform == "win32", "Windows clipboard APIs")
    def test_malformed_priority_format_falls_back_to_dib_after_unlock(self):
        user, kernel = Mock(), Mock()
        user.RegisterClipboardFormatW.side_effect = [1000, 1001]
        user.OpenClipboard.return_value = True
        user.IsClipboardFormatAvailable.side_effect = lambda value: value in (1000, clipboard.CF_DIB)
        user.GetClipboardData.side_effect = lambda value: value
        dib = _png_to_dib(self._png())
        def contents(kernel_api, handle, maximum):
            return b"bad-png" if handle == 1000 else dib
        with (patch.object(clipboard, "_configure"),
              patch.object(clipboard.ctypes, "WinDLL", side_effect=[user, kernel]),
              patch.object(clipboard, "_read_global_bytes", side_effect=contents)):
            result = clipboard.read_clipboard_image_png()
        self.assertIsNotNone(result)
        self.assertEqual(user.CloseClipboard.call_count, 4)

    @unittest.skipUnless(sys.platform == "win32", "Windows clipboard APIs")
    def test_image_format_metadata_includes_office_emf(self):
        user = Mock()
        user.RegisterClipboardFormatW.return_value = 1000
        user.IsClipboardFormatAvailable.side_effect = lambda value: value == clipboard.CF_ENHMETAFILE
        with (patch.object(clipboard, "_configure"),
              patch.object(clipboard.ctypes, "WinDLL", return_value=user)):
            self.assertTrue(clipboard.has_clipboard_image_format())
        user.GetClipboardData.assert_not_called()


if __name__ == "__main__":
    unittest.main()
