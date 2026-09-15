"""Dependency-free screenshot, coordinate, and capture artifact helpers."""

from __future__ import annotations

import argparse
import binascii
import math
import re
import shutil
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*$", re.IGNORECASE)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class RawScreenshot:
    width: int
    height: int
    rgba: bytes
    pixel_format: int
    color_space: int | None


def parse_duration(value: str | float | int) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"invalid duration {value!r}; use values such as 500ms, 3s, or 1m")
    amount = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    if unit == "ms":
        return amount / 1000.0
    if unit == "m":
        return amount * 60.0
    return amount


def duration_arg(value: str) -> float:
    try:
        return parse_duration(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def normalized_to_input(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid input size: {width}x{height}")
    if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
        raise ValueError("normalized coordinates must be within [0, 1]")
    return min(width - 1, round(x * width)), min(height - 1, round(y * height))


def image_dimensions(path: str | Path) -> tuple[int, int]:
    image_path = Path(path)
    with image_path.open("rb") as handle:
        header = handle.read(32)
        if header.startswith(_PNG_SIGNATURE):
            return png_dimensions(header)
        if header[:2] != b"\xff\xd8":
            raise ValueError(f"unsupported image format: {image_path}")
        handle.seek(2)
        while True:
            marker_start = handle.read(1)
            if not marker_start:
                break
            if marker_start != b"\xff":
                continue
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if not marker:
                break
            marker_value = marker[0]
            if marker_value in (0xD8, 0xD9) or 0xD0 <= marker_value <= 0xD7:
                continue
            length_data = handle.read(2)
            if len(length_data) != 2:
                break
            length = struct.unpack(">H", length_data)[0]
            if marker_value in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                payload = handle.read(length - 2)
                if len(payload) < 5:
                    break
                height, width = struct.unpack(">HH", payload[1:5])
                return width, height
            handle.seek(max(0, length - 2), 1)
    raise ValueError(f"cannot read image dimensions: {image_path}")


def png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE):
        raise ValueError("invalid PNG screenshot")
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0 or width > 20000 or height > 20000:
        raise ValueError(f"invalid PNG dimensions: {width}x{height}")
    return width, height


def screenshot_to_input(
    x: float,
    y: float,
    screenshot_size: tuple[int, int],
    input_size: tuple[int, int],
    *,
    aspect_tolerance: float = 0.02,
) -> tuple[int, int]:
    source_width, source_height = screenshot_size
    input_width, input_height = input_size
    if source_width <= 0 or source_height <= 0:
        raise ValueError(f"invalid screenshot size: {source_width}x{source_height}")
    if input_width <= 0 or input_height <= 0:
        raise ValueError(f"invalid input size: {input_width}x{input_height}")
    if not 0 <= x < source_width or not 0 <= y < source_height:
        raise ValueError(
            f"screenshot point ({x:g},{y:g}) is outside {source_width}x{source_height}"
        )
    source_aspect = source_width / source_height
    input_aspect = input_width / input_height
    relative_error = abs(source_aspect - input_aspect) / input_aspect
    if relative_error > aspect_tolerance:
        raise ValueError(
            "screenshot and current device have different aspect ratios or orientations: "
            f"screenshot={source_width}x{source_height} input={input_width}x{input_height}"
        )
    mapped_x = min(input_width - 1, round(x * input_width / source_width))
    mapped_y = min(input_height - 1, round(y * input_height / source_height))
    return mapped_x, mapped_y


def _convert_pixels(pixel_format: int, pixels: bytes, count: int) -> bytes:
    if pixel_format == 1:  # RGBA_8888
        expected = count * 4
        if len(pixels) != expected:
            raise ValueError(f"invalid RGBA screenshot payload: {len(pixels)} != {expected}")
        return pixels
    if pixel_format == 2:  # RGBX_8888
        expected = count * 4
        if len(pixels) != expected:
            raise ValueError(f"invalid RGBX screenshot payload: {len(pixels)} != {expected}")
        converted = bytearray(pixels)
        converted[3::4] = b"\xff" * count
        return bytes(converted)
    if pixel_format == 3:  # RGB_888
        expected = count * 3
        if len(pixels) != expected:
            raise ValueError(f"invalid RGB screenshot payload: {len(pixels)} != {expected}")
        converted = bytearray(count * 4)
        for source, target in zip(range(0, expected, 3), range(0, count * 4, 4)):
            converted[target : target + 3] = pixels[source : source + 3]
            converted[target + 3] = 255
        return bytes(converted)
    if pixel_format == 4:  # RGB_565
        expected = count * 2
        if len(pixels) != expected:
            raise ValueError(f"invalid RGB565 screenshot payload: {len(pixels)} != {expected}")
        converted = bytearray(count * 4)
        for index in range(count):
            value = pixels[index * 2] | (pixels[index * 2 + 1] << 8)
            target = index * 4
            converted[target] = ((value >> 11) & 0x1F) * 255 // 31
            converted[target + 1] = ((value >> 5) & 0x3F) * 255 // 63
            converted[target + 2] = (value & 0x1F) * 255 // 31
            converted[target + 3] = 255
        return bytes(converted)
    raise ValueError(f"unsupported Android screencap pixel format: {pixel_format}")


def parse_raw_screenshot(data: bytes) -> RawScreenshot:
    if len(data) < 12:
        raise ValueError(f"raw screencap payload is too short: {len(data)}")
    width, height, pixel_format = struct.unpack("<III", data[:12])
    if width <= 0 or height <= 0 or width > 20000 or height > 20000:
        raise ValueError(f"invalid raw screencap dimensions: {width}x{height}")
    count = width * height
    bytes_per_pixel = {1: 4, 2: 4, 3: 3, 4: 2}.get(pixel_format)
    if bytes_per_pixel is None:
        raise ValueError(f"unsupported Android screencap pixel format: {pixel_format}")
    expected_pixels = count * bytes_per_pixel
    if len(data) == expected_pixels + 16:
        color_space = struct.unpack("<I", data[12:16])[0]
        pixels = data[16:]
    elif len(data) == expected_pixels + 12:
        color_space = None
        pixels = data[12:]
    else:
        raise ValueError(
            "raw screencap payload size does not match its header: "
            f"{len(data)} bytes for {width}x{height} format={pixel_format}"
        )
    return RawScreenshot(
        width=width,
        height=height,
        rgba=_convert_pixels(pixel_format, pixels, count),
        pixel_format=pixel_format,
        color_space=color_space,
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def encode_png(screenshot: RawScreenshot, *, compression: int = 6) -> bytes:
    if not 0 <= compression <= 9:
        raise ValueError("PNG compression must be within [0,9]")
    stride = screenshot.width * 4
    scanlines = b"".join(
        b"\x00" + screenshot.rgba[offset : offset + stride]
        for offset in range(0, len(screenshot.rgba), stride)
    )
    header = struct.pack(
        ">IIBBBBB",
        screenshot.width,
        screenshot.height,
        8,
        6,
        0,
        0,
        0,
    )
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, compression))
        + _png_chunk(b"IEND", b"")
    )


def write_png(screenshot: RawScreenshot, path: str | Path) -> Path:
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encode_png(screenshot))
    return out


def marked_screenshot(
    screenshot: RawScreenshot,
    point: tuple[int, int],
) -> RawScreenshot:
    x, y = point
    if not 0 <= x < screenshot.width or not 0 <= y < screenshot.height:
        raise ValueError(f"mark point ({x},{y}) is outside screenshot")
    pixels = bytearray(screenshot.rgba)
    radius = max(12, min(screenshot.width, screenshot.height) // 60)
    thickness = max(3, radius // 5)

    def paint(px: int, py: int) -> None:
        if 0 <= px < screenshot.width and 0 <= py < screenshot.height:
            offset = (py * screenshot.width + px) * 4
            pixels[offset : offset + 4] = b"\xff\x20\x20\xff"

    for delta in range(-radius * 2, radius * 2 + 1):
        for width_delta in range(-(thickness // 2), thickness - thickness // 2):
            paint(x + delta, y + width_delta)
            paint(x + width_delta, y + delta)
    for degree in range(360):
        radians = math.radians(degree)
        for width_delta in range(thickness):
            ring = radius - width_delta
            paint(round(x + math.cos(radians) * ring), round(y + math.sin(radians) * ring))
    return RawScreenshot(
        width=screenshot.width,
        height=screenshot.height,
        rgba=bytes(pixels),
        pixel_format=1,
        color_space=screenshot.color_space,
    )


def copy_selected_frames(
    frames: list[dict[str, Any]],
    out_dir: str | Path,
) -> dict[str, str]:
    if not frames:
        return {}
    output = Path(out_dir)
    selected: dict[str, str] = {}
    for label, index in (("middle", len(frames) // 2), ("end", len(frames) - 1)):
        source = Path(frames[index]["path"])
        destination = output / f"selected-{label}{source.suffix}"
        shutil.copyfile(source, destination)
        selected[label] = str(destination.resolve())
    return selected


def select_contact_sheet_frames(
    frames: Iterable[dict[str, Any]],
    max_frames: int = 49,
) -> list[dict[str, Any]]:
    records = list(frames)
    if len(records) <= max_frames:
        return records
    indexes = [
        round(index * (len(records) - 1) / (max_frames - 1))
        for index in range(max_frames)
    ]
    return [records[index] for index in indexes]
