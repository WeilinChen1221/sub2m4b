"""FFmpeg decoding, sample-accurate cuts, and audiobook muxing."""

import json
import shutil
import subprocess
import wave
from pathlib import Path

from .subtitles import Interval


class MediaError(RuntimeError):
    pass


def require_tools() -> None:
    for name in ("ffmpeg", "ffprobe"):
        if not shutil.which(name):
            raise MediaError(f"{name} is required. Install FFmpeg and put it on PATH.")


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise MediaError(f"{command[0]} failed:\n{result.stderr[-6000:]}")
    return result.stdout


def probe(path: Path) -> dict:
    return json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-show_chapters",
                "-of",
                "json",
                str(path),
            ]
        )
    )


def audio_stream(info: dict, track: int | None = None) -> int:
    streams = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not streams:
        raise MediaError("Input has no audio stream")
    if track is not None:
        if track >= len(streams):
            raise MediaError(f"Audio track {track} does not exist; found {len(streams)} tracks")
        return streams[track]["index"]
    japanese = [s for s in streams if s.get("tags", {}).get("language") in {"ja", "jpn"}]
    if japanese:
        return japanese[0]["index"]
    if len(streams) == 1:
        return streams[0]["index"]
    raise MediaError(
        "Multiple audio tracks, none tagged Japanese. Choose --audio-track, starting at 0"
    )


def render_episode(
    video: Path,
    intervals: list[Interval],
    stream: int,
    destination: Path,
    sample_rate: int,
    channels: int,
) -> int:
    # Split decoded audio, trim on samples, then concatenate without AAC encoder delay.
    count = len(intervals)
    graph = [
        f"[0:{stream}]aresample={sample_rate},"
        f"aformat=sample_fmts=s16:channel_layouts={'mono' if channels == 1 else 'stereo'},"
        f"asetpts=PTS-STARTPTS,asplit={count}" + "".join(f"[s{i}]" for i in range(count))
    ]
    for i, interval in enumerate(intervals):
        start = interval.start_ms * sample_rate // 1000
        end = interval.end_ms * sample_rate // 1000
        fade = min(0.003, (end - start) / sample_rate / 2)
        graph.append(
            f"[s{i}]atrim=start_sample={start}:end_sample={end},asetpts=PTS-STARTPTS,"
            f"afade=t=in:d={fade:.6f},"
            f"afade=t=out:st={(end - start) / sample_rate - fade:.6f}:d={fade:.6f}[c{i}]"
        )
    graph.append("".join(f"[c{i}]" for i in range(count)) + f"concat=n={count}:v=0:a=1[out]")
    script = destination.with_suffix(".filter")
    script.write_text(";\n".join(graph), encoding="utf-8")
    run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-filter_complex_script",
            str(script),
            "-map",
            "[out]",
            "-map_metadata",
            "-1",
            "-c:a",
            "pcm_s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            str(destination),
        ]
    )
    with wave.open(str(destination), "rb") as audio:
        frames = audio.getnframes()
    if not frames:
        raise MediaError(f"No audio rendered from {video.name}")
    expected = sum(
        i.end_ms * sample_rate // 1000 - i.start_ms * sample_rate // 1000 for i in intervals
    )
    if abs(frames - expected) > sample_rate // 10:
        raise MediaError(f"Audio ended before subtitle cuts in {video.name}; check subtitle timing")
    return frames


def escape_metadata(value: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ")
    for char in ("\\", "=", ";", "#"):
        value = value.replace(char, "\\" + char)
    return value


def write_metadata(path: Path, title: str, chapters: list[tuple[str, int]], rate: int) -> None:
    lines = [
        ";FFMETADATA1",
        f"title={escape_metadata(title)}",
        f"album={escape_metadata(title)}",
        f"artist={escape_metadata(title)}",
        "genre=Audiobook",
        "language=jpn",
        "media_type=2",
    ]
    position = 0
    for chapter_title, frames in chapters:
        lines.extend(
            [
                "[CHAPTER]",
                f"TIMEBASE=1/{rate}",
                f"START={position}",
                f"END={position + frames}",
                f"title={escape_metadata(chapter_title)}",
            ]
        )
        position += frames
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def encode_book(
    clips: list[Path],
    metadata: Path,
    cover: Path | None,
    destination: Path,
    bitrate: str,
) -> None:
    # Generated relative filenames avoid quoting user paths in concat manifests.
    manifest = metadata.with_suffix(".concat")
    manifest.write_text("".join(f"file '{p.name}'\n" for p in clips), encoding="utf-8")
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "1",
        "-i",
        str(manifest),
        "-i",
        str(metadata),
    ]
    if cover:
        command += ["-i", str(cover)]
    command += [
        "-map",
        "0:a:0",
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
        "-metadata:s:a:0",
        "language=jpn",
    ]
    if cover:
        command += [
            "-map",
            "2:v:0",
            "-c:v",
            "mjpeg",
            "-disposition:v:0",
            "attached_pic",
        ]
    command += ["-movflags", "+faststart", "-f", "mp4", str(destination)]
    run(command)


def encode_clip(source: Path, destination: Path, bitrate: str, title: str) -> None:
    run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-c:a",
            "aac",
            "-b:a",
            bitrate,
            "-metadata",
            f"title={title}",
            "-metadata:s:a:0",
            "language=jpn",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
