from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import io
import sys
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from mirror_host_agent import clipboard_gdi as native


@unittest.skipUnless(sys.platform == "win32", "Native Windows GDI")
class NativeClipboardImageTests(unittest.TestCase):
    def setUp(self):
        self.gdi = native._gdi()

    def bitmap(self, width=31, height=19):
        info = native.BitmapInfo(size=40, width=width, height=-height, planes=1, bits=32)
        pixels = wt.LPVOID()
        handle = self.gdi.CreateDIBSection(None, ctypes.byref(info), 0, ctypes.byref(pixels), None, 0)
        self.assertTrue(handle)
        self.addCleanup(self.gdi.DeleteObject, handle)
        ctypes.memmove(pixels, bytes((220, 140, 30, 0)) * width * height, width * height * 4)
        return handle

    def metafile(self):
        self.gdi.CreateEnhMetaFileW.argtypes = (wt.HDC, wt.LPCWSTR, ctypes.POINTER(wt.RECT), wt.LPCWSTR)
        self.gdi.CreateEnhMetaFileW.restype = wt.HDC
        self.gdi.CloseEnhMetaFile.argtypes = (wt.HDC,)
        self.gdi.CloseEnhMetaFile.restype = wt.HANDLE
        self.gdi.CreateSolidBrush.argtypes = (wt.DWORD,)
        self.gdi.CreateSolidBrush.restype = wt.HANDLE
        self.gdi.Rectangle.argtypes = (wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
        self.gdi.Rectangle.restype = wt.BOOL
        frame = wt.RECT(0, 0, 5080, 2540)
        dc = self.gdi.CreateEnhMetaFileW(None, None, ctypes.byref(frame), None)
        self.assertTrue(dc)
        brush = self.gdi.CreateSolidBrush(30 | (140 << 8) | (220 << 16))
        previous = self.gdi.SelectObject(dc, brush)
        try:
            self.gdi.Rectangle(dc, -1000, -1000, 10000, 10000)
        finally:
            self.gdi.SelectObject(dc, previous)
            self.gdi.DeleteObject(brush)
        handle = self.gdi.CloseEnhMetaFile(dc)
        self.assertTrue(handle)
        self.addCleanup(self.gdi.DeleteEnhMetaFile, handle)
        return handle

    def assert_image(self, png, size):
        self.assertIsNotNone(png)
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.size, size)
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.getpixel((size[0] // 2, size[1] // 2)), (30, 140, 220))

    def test_native_struct_layouts_are_64bit_safe(self):
        self.assertEqual(ctypes.sizeof(native.EmfHeader), 108)
        self.assertEqual(ctypes.sizeof(native.BitmapInfo), 40)
        self.assertEqual(ctypes.sizeof(native.Bitmap), 32 if ctypes.sizeof(wt.HANDLE) == 8 else 24)

    def test_bitmap_handles_convert_with_color_and_odd_stride(self):
        self.assert_image(native.bitmap_to_png(self.bitmap()), (31, 19))

    def test_office_enhanced_metafile_renders_at_bounded_144dpi(self):
        self.assert_image(native.metafile_to_png(self.metafile()), (288, 144))

    def test_vector_size_and_bytes_limits(self):
        header = native.EmfHeader(signature=0x464D4520, bytes=100)
        header.frame = wt.RECT(0, 0, 5080, 2540)
        self.assertEqual(native._emf_size(header), (288, 144))
        header.frame = wt.RECT(0, 0, 2_000_000_000, 2_000_000_000)
        self.assertIsNone(native._emf_size(header))
        header.frame = wt.RECT(0, 0, 0, 0)
        header.bounds = wt.RECT(0, 0, 30, 18)
        self.assertEqual(native._emf_size(header), (31, 19))
        header.bytes = native.MAX_SOURCE_BYTES + 1
        self.assertIsNone(native._emf_size(header))
        self.assertFalse(native._bounded(40_000, 1))
        self.assertFalse(native._bounded(10_000, 10_000))

    def test_clipboard_ownership_and_close_before_compression(self):
        for format_id, source, encoder_name in (
            (native.CF_BITMAP, self.bitmap(), "bitmap_to_png"),
            (native.CF_ENHMETAFILE, self.metafile(), "metafile_to_png"),
        ):
            user = ctypes.WinDLL("user32", use_last_error=True)
            fake = Mock()
            fake.CopyImage = user.CopyImage
            fake.OpenClipboard.return_value = True
            fake.GetClipboardData.return_value = source
            fake.IsClipboardFormatAvailable.return_value = True
            convert = getattr(native, encoder_name)
            def inspect(owned):
                self.assertNotEqual(owned, source)
                fake.CloseClipboard.assert_called_once()
                return convert(owned)
            with patch.object(native, encoder_name, side_effect=inspect):
                self.assertIsNotNone(native.read_gdi_clipboard_image_png(fake, format_id))
            # The clipboard-owned original was neither mutated nor deleted.
            self.assertIsNotNone(convert(source))

    def test_gdi_objects_do_not_leak_on_repeated_success_or_png_failure(self):
        process = ctypes.WinDLL("kernel32").GetCurrentProcess
        process.restype = wt.HANDLE
        gui = ctypes.WinDLL("user32").GetGuiResources
        gui.argtypes, gui.restype = (wt.HANDLE, wt.DWORD), wt.DWORD
        handle = self.metafile()
        native.metafile_to_png(handle)  # Warm up.
        before = gui(process(), 0)
        for _ in range(15):
            self.assertIsNotNone(native.metafile_to_png(handle))
        with patch.object(native, "_encode", side_effect=ValueError("invalid PNG")):
            for _ in range(10):
                with self.assertRaises(ValueError):
                    native.metafile_to_png(handle)
        self.assertLessEqual(gui(process(), 0), before + 1)

    def test_bad_handles_are_rejected(self):
        self.assertIsNone(native.bitmap_to_png(None))
        self.assertIsNone(native.metafile_to_png(None))
