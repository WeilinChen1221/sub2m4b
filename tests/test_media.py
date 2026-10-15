import json
import shutil
import subprocess
import sys

import pytest

from sub2m4b.media import MediaError, audio_stream, probe, run


@pytest.mark.parametrize(
    "streams,track,expected",
    [
        (
            [
                {"index": 1, "codec_type": "audio", "tags": {"language": "eng"}},
                {"index": 3, "codec_type": "audio", "tags": {"language": "jpn"}},
            ],
            None,
            3,
        ),
        ([{"index": 2, "codec_type": "audio"}], None, 2),
        ([{"index": 2, "codec_type": "audio"}, {"index": 4, "codec_type": "audio"}], 1, 4),
    ],
)
def test_audio_selection(streams, track, expected):
    assert audio_stream({"streams": streams}, track) == expected


def test_ambiguous_audio():
    with pytest.raises(MediaError, match="Multiple"):
        audio_stream(
            {"streams": [{"index": 1, "codec_type": "audio"}, {"index": 2, "codec_type": "audio"}]}
        )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required"
)
def test_full_cli_cuts_chapters_cover_quality_and_overwrite(tmp_path):
    source = tmp_path / "input with 'quote"
    source.mkdir()
    for number in (1, 2):
        run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={400 * number}:duration=4:sample_rate=48000",
                "-c:a",
                "flac",
                "-metadata:s:a:0",
                "language=jpn",
                str(source / f"show [{number:02d}].mkv"),
            ]
        )
        (source / f"show [{number:02d}].srt").write_text(
            "1\n00:00:00,500 --> 00:00:01,000\nHello\n\n2\n00:00:02,000 --> 00:00:03,000\nWorld\n"
        )
    cover = tmp_path / "cover.jpg"
    run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64",
            "-frames:v",
            "1",
            str(cover),
        ]
    )
    titles = tmp_path / "titles.json"
    titles.write_text(json.dumps({"1": "出会い #1;= test", "2": "二話"}))
    output = tmp_path / "book.m4b"
    command = [
        sys.executable,
        "-m",
        "sub2m4b",
        "build",
        str(source),
        "--offline",
        "--cover",
        str(cover),
        "--titles",
        str(titles),
        "--title",
        "日本語 #;= title",
        "--channels",
        "1",
        "--bitrate",
        "96k",
        "--clips-dir",
        str(tmp_path / "clips"),
        "-o",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    info = probe(output)
    assert info["format"]["tags"]["title"] == "日本語 #;= title"
    assert info["format"]["tags"]["artist"] == "日本語 #;= title"
    assert info["format"]["tags"]["media_type"] == "2"
    assert len(info["chapters"]) == 2
    assert info["chapters"][0]["tags"]["title"] == "01 - 出会い #1;= test"
    assert float(info["chapters"][1]["start_time"]) == pytest.approx(2, abs=0.001)
    assert float(info["chapters"][1]["end_time"]) == pytest.approx(4, abs=0.001)
    assert float(info["format"]["duration"]) == pytest.approx(4, abs=0.03)
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000"
    assert audio["channels"] == 1
    assert any(s.get("disposition", {}).get("attached_pic") for s in info["streams"])
    assert len(list((tmp_path / "clips").glob("*.m4a"))) == 2
    original = output.read_bytes()
    rerun = subprocess.run(command, capture_output=True, text=True)
    assert rerun.returncode == 1
    assert "Output exists" in rerun.stderr
    assert output.read_bytes() == original
    assert not list(tmp_path.glob(".sub2m4b-*"))

    # Decode the AAC and confirm cuts preserve the first and second episode signals.
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(output), "-map", "0:a:0", "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    import array

    samples = array.array("h", raw)
    for start, frequency in ((0.2, 400), (2.2, 800)):
        window = samples[int(start * 48000) : int((start + 0.5) * 48000)]
        crossings = sum(a < 0 <= b for a, b in zip(window, window[1:], strict=False))
        assert crossings * 2 == pytest.approx(frequency, abs=4)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_single_cut_at_44100_has_sample_accurate_length(tmp_path):
    from sub2m4b.media import render_episode
    from sub2m4b.subtitles import Interval

    source = tmp_path / "source.wav"
    run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", str(source)])
    frames = render_episode(source, [Interval(123, 789)], 0, tmp_path / "cut.wav", 44100, 2)
    assert frames == 789 * 44100 // 1000 - 123 * 44100 // 1000
