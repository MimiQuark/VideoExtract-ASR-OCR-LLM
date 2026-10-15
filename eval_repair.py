#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""字幕校对质量评估

把「修正 N 处错字」这句自夸，变成可复核的指标：

    python eval_repair.py --out output/<视频目录>
    python eval_repair.py --out output/<视频目录> --review     # 导出待人工标注清单

指标口径
--------
* **无副作用修正率** = (ok + neutral) / 总修正数
  改动后没有引入新错误的占比。这是主指标。
* **错误引入率** = (wrong + bleed) / 总修正数
  细分为两类：
    - wrong  把本来正确的 ASR 改错（多为 OCR 误识反向传播）
    - bleed  把本段没说的内容灌进来（整行/整屏字幕被复用）
* **受影响段落占比** = 引入错误的段数 / 全篇段数
  回答"这个问题到底有多严重"。

判定标准来自人工标注（``eval/repair_gold.jsonl``）。**标注是主观判断**，
换人复核结论可能略有出入，因此脚本同时输出逐条明细供抽查：

    python eval_repair.py --out ... --dump     # 打印全部判定与理由
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import extract  # noqa: E402

DEFAULT_GOLD = BASE_DIR / "eval" / "repair_gold.jsonl"

# 与 server.py 保持一致的"疑似整行灌入"启发式
BLEED_RATIO = 1.6
BLEED_DELTA = 15

VERDICT_LABEL = {"ok": "改对", "neutral": "等价替换", "wrong": "改错", "bleed": "整行灌入"}
REASON_LABEL = {"score": "同音/形近纠错", "norm": "术语或数字规范化", "fmt": "排版规范化",
                "ocr": "OCR 误识反向改错", "noise": "混入噪声字符", "loss": "内容丢失或语义被改",
                "dup": "同句被相邻段复用"}


def collect_corrections(out_dir: Path) -> tuple[list[dict], int]:
    """对比原始转写与校对结果，取出所有发生变化的段落。"""
    raw = json.loads((out_dir / "transcript.json").read_text(encoding="utf-8"))
    repaired_path = out_dir / "transcript.repaired.json"
    if not repaired_path.exists():
        return [], len(raw)
    rep = json.loads(repaired_path.read_text(encoding="utf-8"))
    out = []
    for idx, (a, b) in enumerate(zip(raw, rep)):
        if a.get("text") == b.get("text"):
            continue
        before, after = a.get("text", ""), b.get("text", "")
        ratio = len(after) / max(1, len(before))
        out.append({
            "index": idx, "start": a.get("start", 0.0),
            "before": before, "after": after,
            # 自动启发式：只依赖长度比例，用于对比人工标注的表现
            "auto_bleed": ratio > BLEED_RATIO or (len(after) - len(before)) > BLEED_DELTA,
        })
    return out, len(raw)


def load_gold(path: Path) -> dict[str, dict]:
    """读取人工标注，返回 {index: case}。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[警告] 金标文件解析失败: {exc}")
        return {}
    return {int(c["index"]): c for c in data.get("cases", [])}


def evaluate(out_dir: Path, gold: dict[str, dict]) -> dict[str, Any]:
    corrections, total_segments = collect_corrections(out_dir)
    if not corrections:
        return {"corrections": 0, "total_segments": total_segments,
                "message": "本次没有发生任何校对改动"}

    matched, missing = [], []
    for c in corrections:
        g = gold.get(c["index"])
        if g is None:
            missing.append(c)
        else:
            matched.append({**c, "verdict": g.get("verdict", "?"), "reason": g.get("reason", "?")})

    verdicts = Counter(m["verdict"] for m in matched)
    reasons = Counter(m["reason"] for m in matched)
    n = len(matched)
    harmless = verdicts.get("ok", 0) + verdicts.get("neutral", 0)
    errors = verdicts.get("wrong", 0) + verdicts.get("bleed", 0)

    # 自动启发式 vs 人工标注：看这个便宜规则值不值得用
    gold_bleed = {m["index"] for m in matched if m["verdict"] == "bleed"}
    auto_bleed = {m["index"] for m in matched if m["auto_bleed"]}
    tp = len(gold_bleed & auto_bleed)
    precision = tp / len(auto_bleed) if auto_bleed else 0.0
    recall = tp / len(gold_bleed) if gold_bleed else 0.0

    return {
        "corrections": len(corrections),
        "total_segments": total_segments,
        "labeled": n,
        "unlabeled": len(missing),
        "coverage": round(n / len(corrections), 4) if corrections else 0.0,
        "verdicts": dict(verdicts),
        "reasons": dict(reasons),
        "harmless_rate": round(harmless / n, 4) if n else 0.0,
        "error_rate": round(errors / n, 4) if n else 0.0,
        "affected_segment_ratio": round(errors / total_segments, 4) if total_segments else 0.0,
        "auto_bleed": {
            "detected": len(auto_bleed),
            "gold": len(gold_bleed),
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
        },
        "cases": matched,
        "unlabeled_cases": missing,
    }


def print_report(rep: dict[str, Any], verbose: bool = False) -> None:
    if not rep.get("corrections"):
        print(rep.get("message", "无数据"))
        return
    print("=" * 76)
    print("字幕校对质量评估")
    print("=" * 76)
    print(f"  全篇语音段        : {rep['total_segments']}")
    print(f"  发生改动的段      : {rep['corrections']}")
    print(f"  已有金标覆盖      : {rep['labeled']}/{rep['corrections']}"
          f"  (覆盖率 {rep['coverage']*100:.0f}%)")
    if rep["unlabeled"]:
        print(f"  ⚠ 未标注          : {rep['unlabeled']} 处（指标仅基于已标注部分）")
    print()
    print(f"  ★ 无副作用修正率  : {rep['harmless_rate']*100:.1f}%   (ok + 等价替换)")
    print(f"    错误引入率      : {rep['error_rate']*100:.1f}%")
    print(f"    受影响段落占比  : {rep['affected_segment_ratio']*100:.1f}%"
          f"  ({rep['verdicts'].get('wrong',0)+rep['verdicts'].get('bleed',0)}/{rep['total_segments']} 段)")
    print()
    print("  判定分布:")
    for k in ("ok", "neutral", "wrong", "bleed"):
        v = rep["verdicts"].get(k, 0)
        if v:
            print(f"    {VERDICT_LABEL.get(k,k):10s} {v:4d}  ({v/rep['labeled']*100:5.1f}%)")
    print("  成因分布:")
    for k, v in sorted(rep["reasons"].items(), key=lambda x: -x[1]):
        print(f"    {REASON_LABEL.get(k,k):18s} {v:4d}")
    print()
    a = rep["auto_bleed"]
    print(f"  自动灌入检测器    : 检出 {a['detected']} / 实际 {a['gold']}"
          f"   精确率 {a['precision']:.2f}  召回率 {a['recall']:.2f}  F1 {a['f1']:.2f}")
    print("    （只靠长度比例的廉价规则；召回不足说明该换更强的判据）")
    print()
    if verbose:
        print("  逐条明细:")
        for c in rep["cases"]:
            tag = f"{VERDICT_LABEL.get(c['verdict'],'?'):6s}/{REASON_LABEL.get(c['reason'],'?'):16s}"
            print(f"    [{extract.fmt_ts(c['start'])}] {tag} {c['before']}")
            print(f"                             -> {c['after']}")


def export_review(out_dir: Path, path: Path) -> int:
    """导出待人工标注清单（新视频用）。"""
    corrections, _ = collect_corrections(out_dir)
    payload = {"video": out_dir.name, "note": "为每条填写 verdict(ok/wrong/bleed/neutral) 与 reason",
               "cases": [{**c, "verdict": "", "reason": ""} for c in corrections]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已导出 {len(corrections)} 条待标注 -> {path}")
    return len(corrections)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    p = argparse.ArgumentParser(description="字幕校对质量评估")
    p.add_argument("--out", required=True, help="视频的输出目录（含 transcript*.json）")
    p.add_argument("--gold", default=str(DEFAULT_GOLD), help="人工标注金标文件")
    p.add_argument("--review", action="store_true", help="导出待人工标注清单后退出")
    p.add_argument("--dump", action="store_true", help="打印逐条判定明细")
    p.add_argument("--json", action="store_true", help="只输出 JSON")
    args = p.parse_args()

    out_dir = Path(args.out).expanduser()
    if not out_dir.is_dir():
        print(f"[错误] 目录不存在: {out_dir}")
        return 1

    if args.review:
        export_review(out_dir, BASE_DIR / "eval" / f"review_{out_dir.name}.jsonl")
        return 0

    gold = load_gold(Path(args.gold).expanduser())
    if not gold:
        print(f"[提示] 未找到金标（{args.gold}），只能给出改动清单，无法计算准确率。")
        print("       用 --review 导出清单，逐条填写 verdict 后再跑。")

    report = evaluate(out_dir, gold)
    if args.json:
        print(json.dumps({k: v for k, v in report.items() if k not in ("cases", "unlabeled_cases")},
                         ensure_ascii=False, indent=2))
    else:
        print_report(report, verbose=args.dump)

    out_path = BASE_DIR / "reports" / "repair_eval.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.json:
        print(f"明细已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
