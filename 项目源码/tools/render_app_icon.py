"""Render the geometric SVG directly at each Windows icon size.

Run this only when maintaining the icon design. Normal builds use the committed
ICO/PNG files and do not run a browser or need image-processing dependencies.
The ICO contains independently rendered frames, rather than repeatedly shrinking
a large illustration. Small frames use 32-bit DIB for Tk 8.6 compatibility; the
256-pixel Explorer frame uses PNG compression. Pillow only converts formats and
does not resize the independently rendered browser screenshots.
"""
from __future__ import annotations

import argparse
import asyncio
from io import BytesIO
from pathlib import Path
import struct


ASSETS = Path(__file__).resolve().parents[1] / "assets"
ICON_SIZES = (16, 18, 20, 24, 28, 30, 32, 36, 40, 48, 60, 64, 72, 96, 128, 256)
TITLE_SIZES = (24, 30, 36, 48)


def dib_bytes(png: bytes) -> bytes:
    """Convert a PNG to a Windows icon DIB, preserving its native pixels."""
    from PIL import Image

    with Image.open(BytesIO(png)) as image:
        image = image.convert("RGBA")
        width, height = image.size
        rgba = image.tobytes()
        color = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes("raw", "BGRA")
    mask_stride = ((width + 31) // 32) * 4
    mask = bytearray(mask_stride * height)
    for y in range(height):
        for x in range(width):
            if rgba[(y * width + x) * 4 + 3] == 0:
                mask[(height - y - 1) * mask_stride + x // 8] |= 0x80 >> (x % 8)
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0,
                         len(color) + len(mask), 0, 0, 0, 0)
    return header + color + mask


def ico_bytes(frames: dict[int, bytes]) -> bytes:
    """Package native-size frames without resampling any pixels."""
    offset = 6 + 16 * len(frames)
    directory, payload = [], []
    for size, png in frames.items():
        frame = png if size == 256 else dib_bytes(png)
        dimension = 0 if size == 256 else size
        directory.append(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32,
                                     len(frame), offset))
        payload.append(frame)
        offset += len(frame)
    return struct.pack("<HHH", 0, 1, len(frames)) + b"".join(directory + payload)


async def render(browser_channel: str) -> None:
    from playwright.async_api import async_playwright

    svg = (ASSETS / "app_icon.svg").read_text(encoding="utf-8")
    ASSETS.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel=browser_channel, headless=True)
        try:
            page = await browser.new_page(device_scale_factor=1)
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(
                "<!doctype html><style>html,body{margin:0;padding:0;background:transparent;}"
                "svg{display:block;width:100vw;height:100vh;}</style>" + svg)
            frames = {}
            for size in (*ICON_SIZES, 512):
                await page.set_viewport_size({"width": size, "height": size})
                png = await page.screenshot(omit_background=True)
                if size in ICON_SIZES:
                    frames[size] = png
                if size in TITLE_SIZES:
                    (ASSETS / f"title_icon_{size}.png").write_bytes(png)
                if size == 512:
                    (ASSETS / "app_icon.png").write_bytes(png)
            temporary = ASSETS / "app_icon.ico.tmp"
            temporary.write_bytes(ico_bytes(frames))
            temporary.replace(ASSETS / "app_icon.ico")
        finally:
            await browser.close()
    print("Rendered ICO frames:", ", ".join(map(str, ICON_SIZES)))
    print("Rendered title icons:", ", ".join(map(str, TITLE_SIZES)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", choices=("chrome", "msedge"), default="chrome")
    asyncio.run(render(parser.parse_args().browser))


if __name__ == "__main__":
    main()
