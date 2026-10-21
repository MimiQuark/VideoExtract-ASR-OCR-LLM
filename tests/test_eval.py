import json
from pathlib import Path

import eval_repair


def _make_run(tmp_path: Path):
    out = tmp_path / "run"
    out.mkdir()
    raw = [
        {"start": 0.0, "end": 1.0, "text": "原文一"},
        {"start": 1.0, "end": 2.0, "text": "原文二"},
    ]
    rep = [
        {"start": 0.0, "end": 1.0, "text": "修正一"},
        {"start": 1.0, "end": 2.0, "text": "原文二"},
    ]
    (out / "transcript.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    (out / "transcript.repaired.json").write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    gold = {
        "video": "demo",
        "source": "local",
        "cases": [
            {"index": 0, "before": "原文一", "after": "修正一", "verdict": "ok", "reason": "score"},
        ],
    }
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
    return out, gold_path


def test_gold_matching_requires_before_after(tmp_path: Path):
    out, gold_path = _make_run(tmp_path)
    gold = eval_repair.load_gold(gold_path)
    report = eval_repair.evaluate(out, gold)
    assert report["labeled"] == 1
    assert report["unlabeled"] == 0

    gold[0]["before"] = "不同的原文"
    report2 = eval_repair.evaluate(out, gold)
    assert report2["labeled"] == 0
    assert report2["unlabeled"] == 1


def test_mismatched_segment_counts_raise(tmp_path: Path):
    out, _ = _make_run(tmp_path)
    (out / "transcript.repaired.json").write_text(
        json.dumps([{"start": 0, "end": 1, "text": "only one"}], ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        eval_repair.collect_corrections(out)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
