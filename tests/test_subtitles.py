import pysubs2
import pytest

from sub2m4b.subtitles import Interval, load_intervals


def test_srt_merges_padded_overlaps_and_clamps(tmp_path):
    path = tmp_path / "test.srt"
    path.write_text(
        "1\n00:00:00,020 --> 00:00:01,000\nはい\n\n"
        "2\n00:00:00,900 --> 00:00:02,000\nそう\n\n"
        "3\n00:00:02,900 --> 00:00:04,000\n終わり\n",
        encoding="utf-8",
    )
    assert load_intervals(path, 3000) == [Interval(0, 2175), Interval(2825, 3000)]


def test_ass_filters_comments_drawings_empty_and_styles(tmp_path):
    subs = pysubs2.SSAFile()
    subs.events = [
        pysubs2.SSAEvent(start=1000, end=2000, text="会話", style="Dialogue"),
        pysubs2.SSAEvent(start=3000, end=4000, text="歌", style="Song"),
        pysubs2.SSAEvent(start=4000, end=4500, text="note", type="Comment"),
        pysubs2.SSAEvent(start=5000, end=6000, text=r"{\p1}m 0 0 l 10 10"),
        pysubs2.SSAEvent(start=6000, end=7000, text=" "),
        pysubs2.SSAEvent(start=8000, end=7000, text="backwards"),
    ]
    path = tmp_path / "test.ass"
    subs.save(path)
    assert load_intervals(
        path, 10000, pre_ms=50, post_ms=150, offset_ms=-1100, include_styles=("Dialogue",)
    ) == [Interval(0, 1050)]
    with pytest.raises(ValueError, match="No usable"):
        load_intervals(path, 10000, include_styles=("Dialogue",), exclude_styles=("Dialogue",))


def test_out_of_range_and_negative_padding(tmp_path):
    path = tmp_path / "test.srt"
    path.write_text("1\n00:00:10,000 --> 00:00:11,000\nHi\n")
    with pytest.raises(ValueError, match="No usable"):
        load_intervals(path, 1000)
    with pytest.raises(ValueError, match="Padding"):
        load_intervals(path, 1000, pre_ms=-1)
