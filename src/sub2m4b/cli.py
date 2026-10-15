"""Command-line interface and conversion orchestration."""

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

import pysubs2

from . import __version__
from .discovery import discover
from .media import (
    MediaError,
    audio_stream,
    encode_book,
    encode_clip,
    probe,
    render_episode,
    require_tools,
    write_metadata,
)
from .metadata import API, AnimeMetadata, MetadataError, fetch_metadata
from .subtitles import load_intervals


def nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def positive(value: str) -> int:
    number = nonnegative(value)
    if not number:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def bitrate(value: str) -> str:
    if not re.fullmatch(r"\d+k", value) or not 32 <= int(value[:-1]) <= 512:
        raise argparse.ArgumentTypeError("use 32k through 512k, for example 128k")
    return value


def episode_selection(value: str) -> set[int]:
    numbers = set()
    try:
        for part in value.split(","):
            bounds = [int(n) for n in part.split("-")]
            if len(bounds) == 1:
                numbers.add(bounds[0])
            elif len(bounds) == 2 and bounds[0] <= bounds[1]:
                numbers.update(range(bounds[0], bounds[1] + 1))
            else:
                raise ValueError
        if not numbers or min(numbers) < 0:
            raise ValueError
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use episode numbers or ranges, such as 1-12,14") from exc
    return numbers


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Cut anime audio to subtitle timestamps and build a chaptered M4B."
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("inspect", "Preview pairs, audio tracks and subtitle cuts"),
        ("build", "Create a chaptered AAC audiobook"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("input", type=Path, help="Directory containing one anime season")
        command.add_argument("--subtitle-dir", type=Path)
        command.add_argument("--subtitle-glob", default="*", help="For example '*[[]JPN[]].ass'")
        command.add_argument(
            "--episode-pattern", help="Regex with episode number in capture group 1"
        )
        command.add_argument("--episodes", type=episode_selection, help="For example 1-12 or 1,3,5")
        command.add_argument("--audio-track", type=nonnegative, help="Zero-based audio track index")
        command.add_argument("--pre-padding", type=nonnegative, default=75, metavar="MS")
        command.add_argument("--post-padding", type=nonnegative, default=175, metavar="MS")
        command.add_argument(
            "--subtitle-offset",
            type=int,
            default=0,
            metavar="MS",
            help="Positive values move cues later",
        )
        command.add_argument("--include-style", action="append", default=[], metavar="NAME")
        command.add_argument("--exclude-style", action="append", default=[], metavar="NAME")
        command.add_argument("--encoding", default="utf-8-sig")
        if name == "build":
            command.add_argument("-o", "--output", type=Path, required=True)
            command.add_argument(
                "--anime", help="Anime search title; defaults to input folder name"
            )
            ids = command.add_mutually_exclusive_group()
            ids.add_argument("--mal-id", type=positive)
            ids.add_argument("--anilist-id", type=positive)
            command.add_argument(
                "--bangumi-id", type=positive, help="Pin the Bangumi subject ID for metadata"
            )
            command.add_argument("--title", help="Override audiobook title")
            command.add_argument("--title-language", choices=["ja", "en", "romaji"], default="ja")
            command.add_argument("--offline", action="store_true", help="Skip all API calls")
            command.add_argument("--cover", type=Path, help="Override cover with a local image")
            command.add_argument("--titles", type=Path, help='JSON episode titles: {"1": "Title"}')
            command.add_argument(
                "--quality",
                choices=["speech", "standard", "high"],
                default="standard",
                help="AAC 64k, 128k, or 192k",
            )
            command.add_argument(
                "--bitrate", type=bitrate, help="Override quality preset, e.g. 96k"
            )
            command.add_argument("--sample-rate", type=int, choices=[44100, 48000], default=48000)
            command.add_argument("--channels", type=int, choices=[1, 2], default=2)
            command.add_argument("--clips-dir", type=Path, help="Also save one M4A per episode")
            command.add_argument(
                "--work-dir", type=Path, help="Parent folder for temporary PCM files"
            )
            command.add_argument(
                "--cache-dir", type=Path, default=Path.home() / ".cache" / "sub2m4b"
            )
            command.add_argument("--overwrite", action="store_true")
    return root


def warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr, flush=True)


def execute(args: argparse.Namespace) -> None:
    require_tools()
    if args.episode_pattern:
        try:
            expression = re.compile(args.episode_pattern)
        except re.error as exc:
            raise ValueError(f"Invalid --episode-pattern: {exc}") from exc
        if expression.groups < 1:
            raise ValueError("--episode-pattern needs a capture group for the episode number")
    episodes = discover(
        args.input,
        args.subtitle_dir,
        pattern=args.episode_pattern,
        subtitle_glob=args.subtitle_glob,
        selected=args.episodes,
    )
    plans = []
    for episode in episodes:
        info = probe(episode.video)
        stream = audio_stream(info, args.audio_track)
        duration_ms = round(float(info["format"]["duration"]) * 1000)
        intervals = load_intervals(
            episode.subtitle,
            duration_ms,
            pre_ms=args.pre_padding,
            post_ms=args.post_padding,
            offset_ms=args.subtitle_offset,
            include_styles=tuple(args.include_style),
            exclude_styles=tuple(args.exclude_style),
            encoding=args.encoding,
        )
        kept = sum(i.end_ms - i.start_ms for i in intervals)
        print(
            f"Episode {episode.number:02d}: {len(intervals)} cuts, "
            f"{kept / 60000:.1f}/{duration_ms / 60000:.1f} min, audio stream {stream}",
            flush=True,
        )
        if args.command == "inspect":
            print(f"  Video: {episode.video.name}\n  Subs:  {episode.subtitle.name}")
            subs = pysubs2.load(episode.subtitle, encoding=args.encoding)
            print(f"  Styles: {', '.join(sorted({cue.style for cue in subs}))}")
            for index, audio in enumerate(s for s in info["streams"] if s["codec_type"] == "audio"):
                tags = audio.get("tags", {})
                print(
                    f"  Track {index}: {audio['codec_name']} "
                    f"{tags.get('language', 'untagged')} {tags.get('title', '')}"
                )
        plans.append((episode, stream, intervals))
    if args.command == "inspect":
        return
    output = args.output.resolve()
    if output.suffix.lower() != ".m4b":
        raise ValueError("Output filename must end in .m4b")
    if output.exists() and not args.overwrite:
        raise ValueError(f"Output exists: {output}; use --overwrite to replace it")
    cover = args.cover.resolve() if args.cover else None
    if cover and not cover.is_file():
        raise ValueError(f"Cover not found: {cover}")
    local_titles = {}
    if args.titles:
        data = json.loads(args.titles.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            str(k).isdigit() and isinstance(v, str) and v.strip() for k, v in data.items()
        ):
            raise ValueError('--titles must be a JSON object such as {"1": "Episode title"}')
        local_titles = {int(k): v for k, v in data.items()}
    clip_paths = []
    if args.clips_dir:
        args.clips_dir.mkdir(parents=True, exist_ok=True)
        clip_paths = [args.clips_dir.resolve() / f"episode-{e.number:03d}.m4a" for e in episodes]
        for path in clip_paths:
            if path.exists() and not args.overwrite:
                raise ValueError(f"Clip exists: {path}; use --overwrite to replace it")
    anime = AnimeMetadata(args.anime or args.input.resolve().name)
    if not args.offline:
        print("Fetching series metadata and episode titles...", flush=True)
        api = API(args.cache_dir)
        try:
            anime = fetch_metadata(
                api,
                anime.title,
                mal_id=args.mal_id,
                anilist_id=args.anilist_id,
                bangumi_id=args.bangumi_id,
                language=args.title_language,
                warn=warn,
            )
            if not cover and anime.cover_url:
                try:
                    cover = api.download_cover(anime.cover_url)
                except MetadataError as exc:
                    warn(str(exc))
        finally:
            api.close()
        print(
            f"Metadata match: {anime.title} (MAL {anime.mal_id}, "
            f"AniList {anime.anilist_id}, Bangumi {anime.bangumi_id})",
            flush=True,
        )
    anime.titles.update(local_titles)
    title = args.title or anime.title
    missing = [e.number for e in episodes if e.number not in anime.titles]
    if missing:
        warn(f"Using numbered chapter titles for episodes {missing}")
    if not cover:
        warn("No cover available; use --cover to supply one")
    chosen_bitrate = (
        args.bitrate or {"speech": "64k", "standard": "128k", "high": "192k"}[args.quality]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
    # Output staging lives beside the destination so the final rename is atomic.
    with (
        tempfile.TemporaryDirectory(prefix="sub2m4b-", dir=args.work_dir) as work,
        tempfile.TemporaryDirectory(prefix=".sub2m4b-", dir=output.parent) as staging,
    ):
        work_path = Path(work)
        clips = []
        chapters = []
        for index, (episode, stream, intervals) in enumerate(plans):
            print(
                f"Rendering episode {episode.number:02d} ({index + 1}/{len(plans)})...", flush=True
            )
            clip = work_path / f"episode-{episode.number:04d}.wav"
            frames = render_episode(
                episode.video, intervals, stream, clip, args.sample_rate, args.channels
            )
            chapter_title = (
                f"{episode.number:02d} - {anime.titles[episode.number]}"
                if episode.number in anime.titles
                else f"Episode {episode.number:02d}"
            )
            clips.append(clip)
            chapters.append((chapter_title, frames))
            if clip_paths:
                # Stage each optional clip on its destination filesystem too.
                with tempfile.TemporaryDirectory(dir=args.clips_dir, prefix=".sub2m4b-") as temp:
                    staged_clip = Path(temp) / "clip.m4a"
                    encode_clip(clip, staged_clip, chosen_bitrate, chapter_title)
                    staged_clip.replace(clip_paths[index])
        metadata = work_path / "chapters.ffmeta"
        write_metadata(metadata, title, anime.artist or title, chapters, args.sample_rate)
        staged_book = Path(staging) / "book.m4b"
        print(f"Encoding audiobook at {chosen_bitrate} AAC...", flush=True)
        encode_book(clips, metadata, cover, staged_book, chosen_bitrate)
        verified = probe(staged_book)
        if len(verified.get("chapters", [])) != len(episodes):
            raise MediaError("Output chapter count does not match episode count")
        expected_seconds = sum(frames for _, frames in chapters) / args.sample_rate
        if abs(float(verified["format"]["duration"]) - expected_seconds) > 0.25:
            raise MediaError("Output duration does not match rendered episode audio")
        staged_book.replace(output)
    print(f"Saved {output} ({len(episodes)} chapters, {expected_seconds / 60:.1f} min)", flush=True)


def main() -> None:
    arg_parser = parser()
    args = arg_parser.parse_args()
    try:
        execute(args)
    except (OSError, ValueError, MediaError, MetadataError, pysubs2.exceptions.Pysubs2Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130) from None
