"""Bounded native clipboard image conversion. Never writes the clipboard.

Only owned copies of HBITMAP/HENHMETAFILE are converted/deleted. Office's
clipboard handles stay owned by Windows, and the clipboard is closed before
rendering/PNG compression. Call from the clipboard worker, not the media loop.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import io
import math

CF_BITMAP = 2
CF_ENHMETAFILE = 14
MAX_PIXELS = 40_000_000
MAX_BYTES = 20 * 1024 * 1024
MAX_SOURCE_BYTES = 160 * 1024 * 1024


class BitmapInfo(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("width", wt.LONG), ("height", wt.LONG),
                ("planes", wt.WORD), ("bits", wt.WORD), ("compression", wt.DWORD),
                ("image_size", wt.DWORD), ("xppm", wt.LONG), ("yppm", wt.LONG),
                ("colors", wt.DWORD), ("important", wt.DWORD)]


class Bitmap(ctypes.Structure):
    _fields_ = [("kind", wt.LONG), ("width", wt.LONG), ("height", wt.LONG),
                ("stride", wt.LONG), ("planes", wt.WORD), ("bits", wt.WORD),
                ("pixels", wt.LPVOID)]


class EmfHeader(ctypes.Structure):
    _fields_ = [("kind", wt.DWORD), ("size", wt.DWORD), ("bounds", wt.RECT),
                ("frame", wt.RECT), ("signature", wt.DWORD), ("version", wt.DWORD),
                ("bytes", wt.DWORD), ("records", wt.DWORD), ("handles", wt.WORD),
                ("reserved", wt.WORD), ("description", wt.DWORD), ("offset", wt.DWORD),
                ("palette", wt.DWORD), ("device", wt.SIZE), ("millimeters", wt.SIZE),
                ("pixel_format", wt.DWORD), ("pixel_offset", wt.DWORD),
                ("opengl", wt.DWORD), ("micrometers", wt.SIZE)]


def _gdi():
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    signatures = {
        "GetObjectW": ((wt.HANDLE, ctypes.c_int, wt.LPVOID), ctypes.c_int),
        "GetDIBits": ((wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, wt.LPVOID, wt.LPVOID, wt.UINT), ctypes.c_int),
        "CreateCompatibleDC": ((wt.HDC,), wt.HDC),
        "DeleteDC": ((wt.HDC,), wt.BOOL),
        "DeleteObject": ((wt.HANDLE,), wt.BOOL),
        "SelectObject": ((wt.HDC, wt.HANDLE), wt.HANDLE),
        "CreateDIBSection": ((wt.HDC, wt.LPVOID, wt.UINT, ctypes.POINTER(wt.LPVOID), wt.HANDLE, wt.DWORD), wt.HBITMAP),
        "GetEnhMetaFileHeader": ((wt.HANDLE, wt.UINT, wt.LPVOID), wt.UINT),
        "CopyEnhMetaFileW": ((wt.HANDLE, wt.LPCWSTR), wt.HANDLE),
        "DeleteEnhMetaFile": ((wt.HANDLE,), wt.BOOL),
        "PlayEnhMetaFile": ((wt.HDC, wt.HANDLE, ctypes.POINTER(wt.RECT)), wt.BOOL),
        "GdiFlush": ((), wt.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(gdi, name)
        function.argtypes, function.restype = args, result
    return gdi


def _bounded(width: int, height: int) -> bool:
    return 0 < width <= 16_384 and 0 < height <= 16_384 and width * height <= MAX_PIXELS


def _encode(raw: bytes, width: int, height: int, layout: str, stride: int = 0) -> bytes | None:
    from PIL import Image
    image = Image.frombytes("RGB", (width, height), raw, "raw", layout, stride, 1)
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=6)
    png = output.getvalue()
    return png if len(png) <= MAX_BYTES else None


def bitmap_to_png(handle) -> bytes | None:
    gdi = _gdi()
    bitmap = Bitmap()
    if not gdi.GetObjectW(handle, ctypes.sizeof(bitmap), ctypes.byref(bitmap)):
        return None
    width, height = int(bitmap.width), int(bitmap.height)
    if not _bounded(width, height):
        return None
    stride = (width * 3 + 3) & ~3
    info = BitmapInfo(size=ctypes.sizeof(BitmapInfo), width=width, height=-height, planes=1, bits=24)
    pixels = ctypes.create_string_buffer(stride * height)
    dc = gdi.CreateCompatibleDC(None)
    if not dc:
        return None
    try:
        # The source bitmap is not selected into any DC here (GetDIBits rule).
        if gdi.GetDIBits(dc, handle, 0, height, pixels, ctypes.byref(info), 0) != height:
            return None
        return _encode(pixels.raw, width, height, "BGR", stride)
    finally:
        gdi.DeleteDC(dc)


def _emf_size(header: EmfHeader) -> tuple[int, int] | None:
    if header.signature != 0x464D4520 or not 0 < header.bytes <= MAX_SOURCE_BYTES:
        return None
    fw, fh = header.frame.right - header.frame.left, header.frame.bottom - header.frame.top
    if fw > 0 and fh > 0:
        # Rasterize Office vectors at 144dpi. PNG/DIB source pixels take priority.
        width, height = math.ceil(fw * 144 / 2540), math.ceil(fh * 144 / 2540)
    else:
        width = header.bounds.right - header.bounds.left + 1
        height = header.bounds.bottom - header.bounds.top + 1
    return (width, height) if _bounded(width, height) else None


def metafile_to_png(handle) -> bytes | None:
    gdi = _gdi()
    header = EmfHeader()
    if not gdi.GetEnhMetaFileHeader(handle, ctypes.sizeof(header), ctypes.byref(header)):
        return None
    size = _emf_size(header)
    if size is None:
        return None
    width, height = size
    dc = gdi.CreateCompatibleDC(None)
    if not dc:
        return None
    bitmap = None
    old = None
    try:
        info = BitmapInfo(size=ctypes.sizeof(BitmapInfo), width=width, height=-height, planes=1, bits=32)
        pixels = wt.LPVOID()
        bitmap = gdi.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(pixels), None, 0)
        if not bitmap or not pixels:
            return None
        old = gdi.SelectObject(dc, bitmap)
        if not old or old == ctypes.c_void_p(-1).value:
            old = None
            return None
        # GDI does not provide a trustworthy alpha channel. Use opaque white
        # only for this vector fallback; existing PNG transparency is unchanged.
        ctypes.memset(pixels, 255, width * height * 4)
        rectangle = wt.RECT(0, 0, width, height)
        if not gdi.PlayEnhMetaFile(dc, handle, ctypes.byref(rectangle)):
            return None
        gdi.GdiFlush()
        return _encode(ctypes.string_at(pixels, width * height * 4), width, height, "BGRX")
    finally:
        if old:
            gdi.SelectObject(dc, old)
        if bitmap:
            gdi.DeleteObject(bitmap)
        gdi.DeleteDC(dc)


def read_gdi_clipboard_image_png(user32, clipboard_format: int) -> bytes | None:
    gdi = _gdi()
    user32.CopyImage.argtypes = (wt.HANDLE, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT)
    user32.CopyImage.restype = wt.HANDLE
    owned = None
    try:
        if not user32.OpenClipboard(None):
            return None
        try:
            if not user32.IsClipboardFormatAvailable(clipboard_format):
                return None
            source = user32.GetClipboardData(clipboard_format)
            if not source:
                return None
            if clipboard_format == CF_ENHMETAFILE:
                header = EmfHeader()
                if not gdi.GetEnhMetaFileHeader(source, ctypes.sizeof(header), ctypes.byref(header)) or _emf_size(header) is None:
                    return None
                owned = gdi.CopyEnhMetaFileW(source, None)
            elif clipboard_format == CF_BITMAP:
                bitmap = Bitmap()
                if not gdi.GetObjectW(source, ctypes.sizeof(bitmap), ctypes.byref(bitmap)) or not _bounded(bitmap.width, bitmap.height):
                    return None
                owned = user32.CopyImage(source, 0, 0, 0, 0x2000)  # LR_CREATEDIBSECTION, not COPYRETURNORG
            else:
                return None
        finally:
            user32.CloseClipboard()
        if not owned:
            return None
        return metafile_to_png(owned) if clipboard_format == CF_ENHMETAFILE else bitmap_to_png(owned)
    finally:
        if owned:
            (gdi.DeleteEnhMetaFile if clipboard_format == CF_ENHMETAFILE else gdi.DeleteObject)(owned)
