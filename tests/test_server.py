import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import server  # noqa: E402


def test_summary_model_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "glm-4-flash")
    req = server.JobRequest(source="x.mp4", summarize=True)
    assert server._options_dict(req)["summary_model"] == "glm-4-flash"


def test_load_env_file_reads_allowlisted_values(tmp_path, monkeypatch):
    env_file = tmp_path / "llm.env.bat"
    env_file.write_text(
        "@echo off\nset OPENAI_MODEL=glm-4-flash\nset EVIL=bad\n",
        encoding="gbk",
    )
    monkeypatch.setattr(server, "ENV_FILE", env_file)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("EVIL", raising=False)
    assert server.load_env_file() == 1
    import os
    assert os.environ["OPENAI_MODEL"] == "glm-4-flash"
    assert "EVIL" not in os.environ


def test_recover_only_completed_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_ROOT", tmp_path)
    complete = tmp_path / "done__abc"
    complete.mkdir()
    (complete / "report.md").write_text("ok", encoding="utf-8")
    (complete / "source.json").write_text(
        json.dumps({"source": "x.mp4", "identity": "abc"}), encoding="utf-8"
    )
    partial = tmp_path / "partial__def"
    partial.mkdir()
    (partial / "transcript.json").write_text("[]", encoding="utf-8")

    server.JOBS.clear()
    restored = server.recover_jobs_from_disk()
    assert restored == 1
    assert any(job.source == "x.mp4" for job in server.JOBS.values())
