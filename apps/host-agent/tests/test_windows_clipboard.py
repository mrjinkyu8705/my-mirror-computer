from __future__ import annotations

import io
import unittest

from PIL import Image

from mirror_host_agent.windows_clipboard import (
    _normalize_image_to_png,
    _png_to_dib,
)


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


if __name__ == "__main__":
    unittest.main()
