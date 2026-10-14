from pathlib import Path

import pytest

from sub2m4b.discovery import discover, episode_number


@pytest.mark.parametrize(
    "name,number",
    [
        ("[Pussub&VCB-Studio] Nozaki-kun [01][Hi10p_1080p][x264_flac_aac].mkv", 1),
        ("[Kamigami] Nozaki-kun - 01 [BD 1920x1080 x265 Ma10p FLACx2][JPN].ass", 1),
        ("Show S02E12 1080p.mkv", 12),
        ("Show EP03.ass", 3),
        ("Show 09.ja.srt", 9),
        ("Show 1080p x265.mkv", None),
        ("01.srt", 1),
    ],
)
def test_episode_number(name, number):
    assert episode_number(Path(name)) == number


def test_pair_across_release_groups_sorted_numerically(tmp_path):
    for name in ("[A] show [10].mkv", "[A] show [2].mkv", "[B] show - 10.ass", "[B] show - 2.ass"):
        (tmp_path / name).touch()
    assert [e.number for e in discover(tmp_path)] == [2, 10]
    assert [e.number for e in discover(tmp_path, selected={10})] == [10]
    with pytest.raises(ValueError, match="Missing requested"):
        discover(tmp_path, selected={3})


def test_ambiguity_and_exact_stem_priority(tmp_path):
    for name in ("show [01].mkv", "other [01].ass", "other [01].srt"):
        (tmp_path / name).touch()
    with pytest.raises(ValueError, match="exactly one"):
        discover(tmp_path)
    (tmp_path / "show [01].srt").touch()
    assert discover(tmp_path)[0].subtitle.name == "show [01].srt"


def test_duplicate_episode_videos_rejected(tmp_path):
    for name in ("a [01].mkv", "b [01].mp4", "a [01].srt"):
        (tmp_path / name).touch()
    with pytest.raises(ValueError, match="Multiple videos"):
        discover(tmp_path)
