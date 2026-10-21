import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_failed_targets_return_nonzero():
    result = subprocess.run(
        [sys.executable, "extract.py", "definitely-not-a-video.mp4"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 1
