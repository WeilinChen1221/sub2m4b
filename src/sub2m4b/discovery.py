"""Conservative episode pairing across release naming conventions."""

import re
from dataclasses import dataclass
from pathlib import Path

VIDEOS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v"}
SUBTITLES = {".ass", ".srt"}
PATTERNS = (
    r"(?i)s\d{1,2}e(\d{1,4})(?!\d)",
    r"(?i)(?:\bep(?:isode)?[ ._-]*|\be)(\d{1,4})(?!\d)",
    r"\[(\d{1,3})\]",
    r"\s-\s*(\d{1,3})(?![\d.])",
    r"(?:^|[ _.-])(\d{1,3})(?=(?:[ ._-]*(?:ja|jp|jpn|en|eng))?$)",
)


@dataclass(frozen=True)
class Episode:
    number: int
    video: Path
    subtitle: Path


def episode_number(path: Path, pattern: str | None = None) -> int | None:
    for expression in (pattern,) if pattern else PATTERNS:
        match = re.search(expression, path.stem)
        if match:
            return int(match.group(1))
    return None


def discover(
    directory: Path,
    subtitle_dir: Path | None = None,
    *,
    pattern: str | None = None,
    subtitle_glob: str = "*",
    selected: set[int] | None = None,
) -> list[Episode]:
    videos = sorted(p for p in directory.iterdir() if p.suffix.lower() in VIDEOS and p.is_file())
    subtitles = sorted(
        p
        for p in (subtitle_dir or directory).glob(subtitle_glob)
        if p.suffix.lower() in SUBTITLES and p.is_file()
    )
    episodes = []
    seen = set()
    used = set()
    for video in videos:
        number = episode_number(video, pattern)
        if number is None:
            raise ValueError(f"Cannot identify episode: {video.name}. Use --episode-pattern.")
        if selected is not None and number not in selected:
            continue
        if number in seen:
            raise ValueError(
                f"Multiple videos for episode {number}; use one season per input folder"
            )
        seen.add(number)
        exact = [s for s in subtitles if s.stem == video.stem]
        candidates = exact or [s for s in subtitles if episode_number(s, pattern) == number]
        if len(candidates) != 1:
            names = ", ".join(s.name for s in candidates) or "none"
            raise ValueError(
                f"Episode {number} needs exactly one subtitle, found {names}. "
                "Use --subtitle-glob or --subtitle-dir."
            )
        if candidates[0] in used:
            raise ValueError(f"Subtitle paired more than once: {candidates[0]}")
        used.add(candidates[0])
        episodes.append(Episode(number, video.resolve(), candidates[0].resolve()))
    if selected is not None and selected - seen:
        raise ValueError(f"Missing requested episodes: {sorted(selected - seen)}")
    if not episodes:
        raise ValueError(f"No video/subtitle pairs found in {directory}")
    return sorted(episodes, key=lambda episode: episode.number)
