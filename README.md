# sub2m4b

Turn a season of subtitled anime into an AAC `.m4b` audiobook for Japanese listening practice. Each episode becomes a chapter. AniList supplies series information and cover art; Jikan supplies episode titles from MyAnimeList.

The CLI keeps audio covered by subtitle cues, including 75 ms before and 175 ms after each cue by default. It merges overlapping cues so dialogue never repeats. It does not detect voices or separate dialogue from background music. Signs, sound-effect captions and song lyrics may retain unwanted audio. Use ASS style filters when available.

## Install

Install Python tooling with [uv](https://docs.astral.sh/uv/) and install `ffmpeg` and `ffprobe` separately. Both executables must be on `PATH`.

```sh
uv sync
uv run sub2m4b --help
# Optional: install the CLI into your user tools
uv tool install .
```

Python 3.11 or later is supported. No API keys are required.

## Inspect first

Keep one season's videos and external `.ass` or `.srt` subtitles in a folder. Files can come from different release groups. The CLI recognizes `S01E01`, `EP01`, `[01]`, ` - 01`, and trailing episode numbers, then sorts numerically. Identical filename stems take priority when pairing subtitles. Ambiguous or missing pairs stop the build.

```sh
uv run sub2m4b inspect '/path/to/anime'
uv run sub2m4b inspect '/path/to/anime' --episodes 1 --include-style Default
```

Inspection lists the selected audio stream, available tracks, subtitle styles, and retained duration. The first Japanese-tagged audio track wins. An untagged single track is accepted; ambiguous untagged tracks require `--audio-track 0` or another zero-based audio-track index.

Subtitles from another release may have a timing offset. Check them against the video. `--subtitle-offset 500` moves all cues 500 ms later; negative values move them earlier. Variable timing drift needs corrected subtitles.

## Build an audiobook

```sh
uv run sub2m4b build '/path/to/anime' \
  --anime 'Gekkan Shoujo Nozaki-kun' --mal-id 23289 \
  --output output/nozaki-kun.m4b
```

Without an ID, the CLI searches using `--anime` or the input folder name. A search can match the wrong season. Pin `--mal-id` or `--anilist-id` for reproducible metadata. The CLI prints the selected match. Japanese episode titles are preferred; `--title-language en` or `romaji` changes this. Missing translations fall back to the API's default title, then `Episode 01`.

A build encodes the full season once to AAC, writes chapter boundaries from the actual rendered sample counts, embeds cover art, and marks the file as an audiobook. The output uses the MP4 container with an `.m4b` extension and standard chapter metadata. Open the result in Books on macOS and sync it to your iPhone through Finder. Device import and playback still need to be checked on your own iOS version.

```sh
# Dialogue-only styles in the supplied Nozaki-kun ASS files
uv run sub2m4b build '/Volumes/SMB/Gekkan Shoujo Nozaki-kun' \
  --include-style 单jp --include-style 单jp2 --include-style 单jp-up \
  --mal-id 23289 --pre-padding 75 --post-padding 175 \
  --quality high --clips-dir output/episodes \
  -o output/nozaki-kun.m4b
```

`--clips-dir` also saves one condensed `.m4a` per episode. The audiobook retains all episodes in one file. A 3 ms fade at each cut reduces clicks.

## Options

| Option | Behavior |
| --- | --- |
| `--quality speech/standard/high` | AAC at 64/128/192 kbps; default is standard |
| `--bitrate 96k` | Overrides quality; accepts 32k through 512k |
| `--channels 1` | Mono; default is stereo |
| `--sample-rate 44100` | Default is 48000 Hz |
| `--pre-padding 100 --post-padding 200` | Padding in milliseconds; any nonnegative values accepted |
| `--include-style NAME` | Keep only these ASS styles; repeat for multiple styles |
| `--exclude-style NAME` | Drop these styles; takes precedence over include |
| `--episodes 1-3,5` | Process only selected episode numbers |
| `--subtitle-dir PATH` | Read subtitles from a different folder |
| `--subtitle-glob '*.ja.srt'` | Narrow subtitle candidates; quote shell globs |
| `--episode-pattern 'Episode_(\d+)'` | Custom regex; capture group 1 must contain the episode number |
| `--encoding shift_jis` | Override UTF-8 subtitle decoding |
| `--title TEXT` | Override the audiobook title |
| `--cover cover.jpg` | Use a local cover |
| `--titles titles.json` | Override episode titles with `{"1": "Title", "2": "Title"}` |
| `--offline` | Disable API calls; combine with local title and cover options |
| `--cache-dir PATH` | API cache; default `~/.cache/sub2m4b`, JSON expires after seven days |
| `--work-dir PATH` | Temporary PCM storage location |
| `--overwrite` | Allow replacing existing outputs |

API failures produce warnings and retain available metadata. Jikan requests are paced and temporary failures retried. Explicit AniList IDs never fall back to a potentially different search match. Jikan exposes community-maintained episode names; availability and spelling depend on its source data.

The input files are read only. Temporary PCM files need roughly 11.5 MB per retained minute at 48 kHz stereo, and are deleted on completion or failure. Rendering processes one episode at a time. The final audiobook is staged and checked before replacing the destination. Optional episode clips completed before a later failure remain available.

## Development

```sh
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests cover pairing, cue filtering, timing, metadata retries and pagination, and a real FFmpeg conversion with chapter and cover checks. Media tests skip if FFmpeg is absent. Tests do not use the network or need anime files.

API and media references: [AniList](https://docs.anilist.co/guide/graphql/queries/media), [Jikan](https://docs.api.jikan.moe/), [FFmpeg metadata](https://ffmpeg.org/ffmpeg-formats.html#Metadata).
