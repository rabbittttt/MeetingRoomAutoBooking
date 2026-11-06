"""Validate the shipped Windows icon frames without requiring a desktop."""
from pathlib import Path
import struct
import unittest


ASSETS = Path(__file__).resolve().parents[1] / "assets"
PNG = b"\x89PNG\r\n\x1a\n"


class AppIconAssetsTests(unittest.TestCase):
    def test_windows_scaling_sizes_are_native_dib_frames_with_no_outer_padding(self):
        blob = (ASSETS / "app_icon.ico").read_bytes()
        reserved, kind, count = struct.unpack_from("<HHH", blob)
        self.assertEqual((reserved, kind), (0, 1))
        frames = {}
        for index in range(count):
            width, height, _, _, planes, bits, length, offset = struct.unpack_from(
                "<BBBBHHII", blob, 6 + index * 16)
            width, height = width or 256, height or 256
            self.assertEqual(width, height)
            self.assertEqual((planes, bits), (1, 32))
            self.assertGreaterEqual(offset, 6 + count * 16)
            self.assertLessEqual(offset + length, len(blob))
            frames[width] = blob[offset:offset + length]
        self.assertTrue({16, 20, 24, 30, 32, 36, 40, 48, 64, 128, 256} <= frames.keys())
        for size, frame in frames.items():
            with self.subTest(size=size):
                if size == 256:
                    self.assertTrue(frame.startswith(PNG))
                    self.assertEqual(struct.unpack_from(">II", frame, 16), (size, size))
                    continue
                header, width, doubled_height, planes, bits = struct.unpack_from("<IiiHH", frame)
                self.assertEqual((header, width, doubled_height, planes, bits), (40, size, 2 * size, 1, 32))
                color = frame[40:40 + size * size * 4]
                self.assertEqual(len(color), size * size * 4)
                # Transparent corners with an edge-to-edge solid tile, avoiding the
                # previous illustration's large mostly-transparent outer margin.
                self.assertEqual(color[3], 0)
                for x, y in ((size // 2, 0), (size // 2, size - 1),
                             (0, size // 2), (size - 1, size // 2)):
                    self.assertEqual(color[(y * size + x) * 4 + 3], 255)

    def test_title_images_have_their_own_exact_pixel_dimensions(self):
        for size in (24, 30, 36, 48):
            with self.subTest(size=size):
                png = (ASSETS / f"title_icon_{size}.png").read_bytes()
                self.assertTrue(png.startswith(PNG))
                self.assertEqual(struct.unpack_from(">II", png, 16), (size, size))
                self.assertEqual(png[24:26], bytes((8, 6)))  # eight-bit RGBA


if __name__ == "__main__":
    unittest.main()
