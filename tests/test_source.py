from pathlib import Path

import extract


def test_source_identity_is_stable_for_same_url(tmp_path: Path):
    assert extract.source_identity("https://example.com/v") == extract.source_identity("https://example.com/v")
    assert extract.source_identity("https://example.com/a") != extract.source_identity("https://example.com/b")


def test_source_identity_uses_file_metadata(tmp_path: Path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"a")
    first = extract.source_identity(str(path))
    path.write_bytes(b"changed")
    second = extract.source_identity(str(path))
    assert first != second


def test_safe_name_removes_path_separators():
    assert "/" not in extract.safe_name("a/b:c")
    assert extract.safe_name("  ") == "video"
