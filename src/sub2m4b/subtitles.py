"""Turn subtitle cues into a sorted union of padded audio intervals."""

from dataclasses import dataclass
from pathlib import Path

import pysubs2


@dataclass(frozen=True, order=True)
class Interval:
    start_ms: int
    end_ms: int

    def __post_init__(self):
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("Audio intervals must have 0 <= start < end")


def load_intervals(
    path: Path,
    duration_ms: int,
    *,
    pre_ms: int = 75,
    post_ms: int = 175,
    offset_ms: int = 0,
    include_styles: tuple[str, ...] = (),
    exclude_styles: tuple[str, ...] = (),
    encoding: str = "utf-8-sig",
) -> list[Interval]:
    if pre_ms < 0 or post_ms < 0:
        raise ValueError("Padding cannot be negative")
    subs = pysubs2.load(path, encoding=encoding)
    intervals = []
    for cue in subs:
        if cue.is_comment or cue.is_drawing or not cue.plaintext.strip():
            continue
        if include_styles and cue.style not in include_styles:
            continue
        if cue.style in exclude_styles or cue.end <= cue.start:
            continue
        start = max(0, cue.start + offset_ms - pre_ms)
        end = min(duration_ms, cue.end + offset_ms + post_ms)
        if end > start:
            intervals.append(Interval(start, end))
    merged: list[Interval] = []
    for interval in sorted(intervals):
        if merged and interval.start_ms <= merged[-1].end_ms:
            previous = merged.pop()
            merged.append(Interval(previous.start_ms, max(previous.end_ms, interval.end_ms)))
        else:
            merged.append(interval)
    if not merged:
        raise ValueError(
            f"No usable subtitle cues in {path.name}; check styles, offset and encoding"
        )
    return merged
