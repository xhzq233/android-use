"""Shared rectangle and timing types used by UI parsing and command output."""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def intersects(self, other: "Rect") -> bool:
        return not (
            self.right <= other.x
            or self.bottom <= other.y
            or self.x >= other.right
            or self.y >= other.bottom
        )

    def to_xywh(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h


@dataclass
class Timing:
    started_at: float
    last_at: float
    splits: list[tuple[str, float]]

    @classmethod
    def start(cls) -> "Timing":
        now = time.perf_counter()
        return cls(started_at=now, last_at=now, splits=[])

    def mark(self, label: str) -> None:
        now = time.perf_counter()
        self.splits.append((label, (now - self.last_at) * 1000))
        self.last_at = now

    @property
    def total_ms(self) -> float:
        return (self.last_at - self.started_at) * 1000


def parse_bounds(bounds: str | None) -> Rect:
    nums = [int(x) for x in re.findall(r"-?\d+", bounds or "")]
    if len(nums) != 4:
        return Rect(0, 0, 0, 0)
    x1, y1, x2, y2 = nums
    return Rect(x1, y1, max(0, x2 - x1), max(0, y2 - y1))


def format_rect(rect: Rect) -> str:
    return f"({rect.x},{rect.y},{rect.w},{rect.h})"


def parse_rect_arg(value: str) -> Rect:
    nums = [int(x) for x in re.findall(r"-?\d+", value)]
    if len(nums) != 4:
        raise argparse.ArgumentTypeError("bounds must contain four numbers: x,y,w,h")
    return Rect(*nums)
