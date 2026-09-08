"""Windows clipboard bridge for text and PNG images.

Text uses CF_UNICODETEXT. Images are normalized to PNG for transport and use
CF_DIB when written back to Windows, which lets browsers, Office applications,
Paint, and the Snipping Tool paste them normally. Reads return None off Windows
or when the clipboard is unavailable/held by another app so callers can simply
skip that poll.

read_clipboard_text mirrors the host clipboard to the viewer (agent -> viewer,
ADR-017). write_clipboard_text is the reverse: the viewer asks the agent to set
the host clipboard so the user can Ctrl+V text they typed/pasted in the browser.
Writes only happen when clipboard sharing is enabled and control is active
(enforced by the caller). Neither path is ever logged with its content.
"""

from __future__ import annotations

import ctypes
import io
import sys
from ctypes import wintypes

CF_DIB = 8
CF_UNICODETEXT = 13
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002
# Keep in sync with CLIPBOARD_TEXT_MAX_LENGTH in packages/protocol control schema.
CLIPBOARD_TEXT_MAX_LENGTH = 16_384
# Images use their own DataChannel; cap the normalized PNG so a clipboard update
# cannot consume unbounded memory or starve the connection.
CLIPBOARD_IMAGE_MAX_BYTES = 20 * 1024 * 1024
CLIPBOARD_IMAGE_MAX_PIXELS = 40_000_000
_CLIPBOARD_IMAGE_SOURCE_MAX_BYTES = 160 * 1024 * 1024


def _configure(user32: ctypes.WinDLL, kernel32: ctypes.WinDLL) -> None:
    # Pointer-returning calls MUST declare restype, or ctypes truncates the
    # handle to 32 bits on 64-bit Python and dereferences garbage.
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.RegisterClipboardFormatW.argtypes = (wintypes.LPCWSTR,)
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.GetClipboardData.argtypes = (wintypes.UINT,)
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardSequenceNumber.argtypes = ()
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    user32.EmptyClipboard.argtypes = ()
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = ()
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalSize.restype = ctypes.c_size_t


def _read_global_bytes(
    kernel32: ctypes.WinDLL, handle: wintypes.HANDLE, maximum: int
) -> bytes | None:
    size = int(kernel32.GlobalSize(handle))
    if size <= 0 or size > maximum:
        return None
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return None
    try:
        return ctypes.string_at(pointer, size)
    finally:
        kernel32.GlobalUnlock(handle)


def _normalize_image_to_png(raw: bytes, *, dib: bool) -> bytes | None:
    """Decode a PNG or Windows DIB and return a bounded, normalized PNG."""
    try:
        from PIL import BmpImagePlugin, Image

        source = io.BytesIO(raw)
        image = BmpImagePlugin.DibImageFile(source) if dib else Image.open(source)
        image.load()
        width, height = image.size
        if (
            width <= 0
            or height <= 0
            or width * height > CLIPBOARD_IMAGE_MAX_PIXELS
        ):
            return None
        # Preserve transparency when present; otherwise RGB is more compact.
        normalized = (
            image.convert("RGBA")
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info
            else image.convert("RGB")
        )
        output = io.BytesIO()
        normalized.save(output, format="PNG", compress_level=6)
        payload = output.getvalue()
        if len(payload) > CLIPBOARD_IMAGE_MAX_BYTES:
            return None
        return payload
    except Exception:  # noqa: BLE001 - malformed clipboard images are ignored
        return None


def _png_to_dib(png: bytes) -> bytes | None:
    """Convert a bounded PNG payload to the CF_DIB byte layout."""
    if not png or len(png) > CLIPBOARD_IMAGE_MAX_BYTES:
        return None
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(png))
        image.load()
        width, height = image.size
        if (
            width <= 0
            or height <= 0
            or width * height > CLIPBOARD_IMAGE_MAX_PIXELS
        ):
            return None
        output = io.BytesIO()
        # CF_DIB begins at BITMAPINFOHEADER, so strip the 14-byte BMP file header.
        image.convert("RGB").save(output, format="BMP")
        bitmap = output.getvalue()
        return bitmap[14:] if len(bitmap) > 14 else None
    except Exception:  # noqa: BLE001 - invalid image data is rejected
        return None


def get_clipboard_sequence_number() -> int | None:
    """Return Windows' cheap clipboard-change counter, or None off Windows."""
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetClipboardSequenceNumber.argtypes = ()
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        return int(user32.GetClipboardSequenceNumber())
    except Exception:  # noqa: BLE001 - polling is best-effort
        return None


def read_clipboard_text() -> str | None:
    """Return the current clipboard text (capped), or None if unavailable.

    Never raises: any Win32 failure (including the clipboard being locked by
    another process) yields None so the poller just tries again later.
    """
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure(user32, kernel32)
        if not user32.OpenClipboard(None):
            return None
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                text = ctypes.c_wchar_p(pointer).value
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001 - clipboard access is best-effort
        return None

    if not text:
        return None
    return text[:CLIPBOARD_TEXT_MAX_LENGTH]


def write_clipboard_text(text: str) -> bool:
    """Set the host clipboard to ``text`` (capped). Returns True on success.

    Never raises: any Win32 failure (clipboard locked, allocation failure)
    returns False so the caller can log a masked failure and move on. On success
    the system takes ownership of the moveable global block, so we must NOT free
    it; on failure we free it ourselves to avoid a leak.
    """
    if sys.platform != "win32":
        return False
    if not text:
        return False
    text = text[:CLIPBOARD_TEXT_MAX_LENGTH]
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure(user32, kernel32)

        # UTF-16LE, NUL-terminated, as CF_UNICODETEXT expects.
        buffer = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buffer)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        # Single ownership flag: the system takes the block only once
        # SetClipboardData succeeds. Until then any exit — including an exception
        # from memmove/OpenClipboard/SetClipboardData — must free it, so the
        # free lives in one finally rather than scattered per branch.
        transferred = False
        try:
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return False
            try:
                ctypes.memmove(pointer, buffer, size)
            finally:
                kernel32.GlobalUnlock(handle)

            if not user32.OpenClipboard(None):
                return False
            try:
                user32.EmptyClipboard()
                if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                    return False
                transferred = True
            finally:
                user32.CloseClipboard()
        finally:
            if not transferred:
                kernel32.GlobalFree(handle)
    except Exception:  # noqa: BLE001 - clipboard access is best-effort
        return False
    return True


def read_clipboard_image_png() -> bytes | None:
    """Return the current Windows clipboard image as PNG, or None.

    The registered PNG format is preferred when available. CF_DIBV5/CF_DIB are
    accepted as fallbacks because they are emitted by the Windows Snipping Tool
    and most desktop applications.
    """
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure(user32, kernel32)
        png_format = int(user32.RegisterClipboardFormatW("PNG"))
        if not user32.OpenClipboard(None):
            return None
        try:
            for clipboard_format, is_dib in (
                (png_format, False),
                (CF_DIBV5, True),
                (CF_DIB, True),
            ):
                if not clipboard_format or not user32.IsClipboardFormatAvailable(
                    clipboard_format
                ):
                    continue
                handle = user32.GetClipboardData(clipboard_format)
                if not handle:
                    continue
                raw = _read_global_bytes(
                    kernel32, handle, _CLIPBOARD_IMAGE_SOURCE_MAX_BYTES
                )
                if raw is not None:
                    return _normalize_image_to_png(raw, dib=is_dib)
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001 - clipboard access is best-effort
        return None
    return None


def write_clipboard_image_png(png: bytes) -> bool:
    """Write PNG bytes to the Windows clipboard as CF_DIB."""
    if sys.platform != "win32":
        return False
    dib = _png_to_dib(png)
    if dib is None:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure(user32, kernel32)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
        if not handle:
            return False
        transferred = False
        try:
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return False
            try:
                ctypes.memmove(pointer, dib, len(dib))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.OpenClipboard(None):
                return False
            try:
                if not user32.EmptyClipboard():
                    return False
                if not user32.SetClipboardData(CF_DIB, handle):
                    return False
                transferred = True
            finally:
                user32.CloseClipboard()
        finally:
            if not transferred:
                kernel32.GlobalFree(handle)
    except Exception:  # noqa: BLE001 - clipboard access is best-effort
        return False
    return True
