from pathlib import Path

import extract


def test_parse_vtt_with_short_timestamps(tmp_path: Path):
    path = tmp_path / "sub.vtt"
    path.write_text(
        "WEBVTT\n\n00:01.000 --> 00:02.500\n你好\n\n00:03.000 --> 00:04.000\n再见\n",
        encoding="utf-8",
    )
    speech = extract.parse_subtitle(path)
    assert len(speech) == 2
    assert speech[0].start == 1.0
    assert speech[0].end == 2.5
    assert speech[1].text == "再见"


def test_parse_gbk_srt(tmp_path: Path):
    path = tmp_path / "sub.srt"
    path.write_bytes(
        "1\n00:00:01,000 --> 00:00:02,000\n中文\n".encode("gb18030")
    )
    speech = extract.parse_subtitle(path)
    assert len(speech) == 1
    assert speech[0].text == "中文"
