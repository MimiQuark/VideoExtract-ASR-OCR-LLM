#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""video-extract Web 服务

把命令行工具包成可演示的界面：提交任务 -> 实时进度 -> 查看结果 / 下载文件。

设计要点:
  * 单工作线程 + 队列 —— ASR/OCR 是 CPU 密集型，放进事件循环会堵死服务；
    且 extract 的进度钩子是模块级全局，串行执行才能保证进度不串台。
  * 进度来自 extract.PROGRESS_HOOK 的上报，是**真实阶段进度**，
    不是去解析子进程输出猜出来的。
  * 任务结果以落盘文件为准（transcript*.json / screen_text.json / report.md），
    服务重启后历史任务仍可读取结果。

启动:
    pip install -r requirements.txt
    python server.py                 # 默认 http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import extract

BASE_DIR = Path(__file__).resolve().parent
OUT_ROOT = BASE_DIR / "output"
STATIC_DIR = BASE_DIR / "static"

MAX_JOBS = 30            # 内存里保留的最近任务数
MAX_LOG_LINES = 500      # 每个任务保留的日志行数
MAX_TIMELINE = 1200      # 回传给前端的时间轴条数上限

# 判定"疑似整行灌入"：校对后的文本比原文长太多，多半是把整屏/整行字幕塞了进来
BLEED_RATIO = 1.6
BLEED_DELTA = 15


# --------------------------------------------------------------------------- 任务模型

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Job:
    id: str
    source: str
    options: dict
    status: str = "queued"          # queued / running / paused / done / failed / cancelled
    stage: str = ""
    pct: int = 0
    message: str = "排队中"
    error: str = ""
    created_at: str = field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""
    out_dir: str = ""
    log: list[str] = field(default_factory=list)
    # 暂停/取消控制器。在创建任务时就建好，这样排队中的任务也能被取消。
    control: Any = None

    @property
    def title(self) -> str:
        return Path(self.out_dir).name if self.out_dir else self.source

    def brief(self) -> dict:
        return {
            "id": self.id, "source": self.source, "status": self.status,
            "stage": self.stage, "pct": self.pct, "message": self.message,
            "error": self.error, "created_at": self.created_at,
            "finished_at": self.finished_at, "title": self.title,
        }

    def detail(self) -> dict:
        data = self.brief()
        data["options"] = self.options
        data["log"] = self.log[-MAX_LOG_LINES:]
        data["started_at"] = self.started_at
        return data


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
JOB_QUEUE: "queue.Queue[str]" = queue.Queue()


# --------------------------------------------------------------------------- 工作线程

def _options_dict(req: "JobRequest") -> dict:
    return {
        "model": req.model, "lang": req.lang or "", "ocr": req.ocr,
        "ocr_region": req.ocr_region, "ocr_interval": req.ocr_interval,
        "repair": req.repair, "summarize": req.summarize,
        "summary_model": req.summary_model, "reuse": req.reuse,
    }


def argv_from_options(source: str, opt: dict) -> list[str]:
    """把界面参数翻译成 CLI 参数。

    复用 extract.build_parser() 而不是手工拼 Namespace —— 参数由同一定义来源产生，
    新增 CLI 参数时这里不会漏掉。
    """
    argv = [source, "--out", str(OUT_ROOT), "--model", opt["model"]]
    if opt.get("lang"):
        argv += ["--lang", opt["lang"]]
    if opt.get("ocr"):
        argv += ["--ocr", "--ocr-region", opt["ocr_region"],
                 "--ocr-interval", str(opt["ocr_interval"])]
    if not opt.get("repair", True):
        argv.append("--no-repair")
    if opt.get("summarize"):
        argv += ["--summarize", "--summary-model", opt["summary_model"]]
    if opt.get("reuse", True):
        argv.append("--reuse")
    return argv


def _on_progress(job: Job, stage: str, pct: int, message: str) -> None:
    job.stage = stage
    job.pct = pct
    job.message = message


def _on_control(job: Job, state: str, message: str) -> None:
    """Control 的状态变化回调（在 worker 线程里被调用）。"""
    if state == "paused":
        job.status = "paused"
    elif state == "running" and job.status == "paused":
        job.status = "running"
    job.message = message


def _on_log(job: Job, msg: str) -> None:
    if msg.strip():
        job.log.append(msg)
        if len(job.log) > MAX_LOG_LINES * 2:
            del job.log[:MAX_LOG_LINES]


def _find_out_dir(source: str) -> Path | None:
    """定位本次任务的输出目录（OUT_ROOT 下最新的、非 _download 的目录）。"""
    if not OUT_ROOT.exists():
        return None
    cands = [p for p in OUT_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_")]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def _run_job(job: Job) -> None:
    job.status = "running"
    job.started_at = _now()
    job.message = "启动中"
    control = job.control or extract.Control()
    job.control = control
    control.on_state = lambda st, msg: _on_control(job, st, msg)
    extract.CONTROL = control
    extract.PROGRESS_HOOK = lambda s, p, m: _on_progress(job, s, p, m)
    extract.LOG_HOOK = lambda m: _on_log(job, m)
    try:
        # 排队期间就被取消的任务，直接跳过
        control.check()
        extract.require_ffmpeg()
        args = extract.build_parser().parse_args(argv_from_options(job.source, job.options))
        extract.process(job.source, OUT_ROOT, args)
        out_dir = _find_out_dir(job.source)
        job.out_dir = str(out_dir) if out_dir else ""
        job.status = "done"
        job.pct = 100
        job.message = "完成"
    except extract.CancelledError:
        job.status = "cancelled"
        job.message = "已取消（在检查点停下）"
    except SystemExit as exc:          # extract.die() 走的是 sys.exit
        job.status = "failed"
        job.error = f"处理失败（退出码 {exc.code}），详见日志"
    except Exception as exc:           # noqa: BLE001 - 后台线程必须兜住所有异常
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        extract.CONTROL = None
        extract.PROGRESS_HOOK = None
        extract.LOG_HOOK = None
        job.control = None
        job.finished_at = _now()


def _worker() -> None:
    while True:
        job_id = JOB_QUEUE.get()
        try:
            job = JOBS.get(job_id)
            if job is not None:
                _run_job(job)
        finally:
            JOB_QUEUE.task_done()


threading.Thread(target=_worker, name="extract-worker", daemon=True).start()


# --------------------------------------------------------------------------- 结果读取

def _section(text: str, start: str, end: str | None) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = text.find(end, i) if end else -1
    return text[i:j].strip() if j > 0 else text[i:].strip()


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def build_result(job: Job) -> dict:
    """从落盘文件组装结果。文件是唯一事实来源，服务重启后仍可读取。"""
    out_dir = Path(job.out_dir) if job.out_dir else None
    if out_dir is None or not out_dir.exists():
        raise HTTPException(404, "结果目录不存在")

    raw = _load_json(out_dir / "transcript.json", [])
    repaired = _load_json(out_dir / "transcript.repaired.json", [])
    screen = _load_json(out_dir / "screen_text.json", [])
    report = ""
    if (out_dir / "report.md").exists():
        report = (out_dir / "report.md").read_text(encoding="utf-8")

    # 校对差异：逐段对比原始转写与校对结果
    diffs = []
    for a, b in zip(raw, repaired):
        if a.get("text") != b.get("text"):
            before, after = a.get("text", ""), b.get("text", "")
            bleed = len(after) > len(before) * BLEED_RATIO or len(after) - len(before) > BLEED_DELTA
            diffs.append({"start": a.get("start", 0.0), "before": before,
                          "after": after, "bleed": bleed})

    # 时间轴：复用 extract 的合并逻辑，保证与 report.md 一致
    timeline = []
    if raw or screen:
        speech_objs = [extract.Speech(**x) for x in (repaired or raw)]
        screen_objs = [extract.Screen(**x) for x in screen]
        for ts, kind, text in extract.merge_timeline(speech_objs, screen_objs)[:MAX_TIMELINE]:
            timeline.append({"t": round(ts, 3), "ts": extract.fmt_ts(ts), "kind": kind, "text": text})

    files = []
    for p in sorted(out_dir.iterdir()):
        if p.is_file():
            files.append({"name": p.name, "size": p.stat().st_size})

    duration = max([s.get("end", 0.0) for s in raw] + [s.get("end", 0.0) for s in screen] + [0.0])
    return {
        "job": job.brief(),
        "summary": _section(report, "## 🤖 智能总结", "## 📝 正文时间轴"),
        "stats": {
            "speech_segments": len(raw),
            "screen_items": len(screen),
            "repaired": len(diffs),
            "bleed": sum(1 for d in diffs if d["bleed"]),
            "duration": extract.fmt_ts(duration) if duration else "00:00:00",
        },
        "repair_diff": diffs[:300],
        "timeline": timeline,
        "timeline_truncated": len(timeline) >= MAX_TIMELINE,
        "files": files,
    }


# --------------------------------------------------------------------------- HTTP

app = FastAPI(title="video-extract", version="1.0.0",
              description="视频内容自动化提取的 Web 界面")


class JobRequest(BaseModel):
    source: str = Field(..., min_length=1, description="视频链接，或服务器上的文件/目录路径")
    model: str = "medium"
    lang: str | None = None
    ocr: bool = True
    ocr_region: str = Field("full", pattern="^(full|bottom|top)$")
    ocr_interval: float = Field(2.0, gt=0, le=30)
    repair: bool = True
    summarize: bool = False
    summary_model: str = "gpt-4o-mini"
    reuse: bool = True


@app.get("/api/health")
def health() -> dict:
    try:
        ffmpeg = extract.ffmpeg_path()
    except Exception:
        ffmpeg = None
    return {
        "status": "ok",
        "ffmpeg": ffmpeg or "",
        "llm_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "queue_size": JOB_QUEUE.qsize(),
        "jobs": len(JOBS),
    }


@app.post("/api/jobs")
def create_job(req: JobRequest) -> dict:
    src = req.source.strip()
    if not extract.is_url(src) and not Path(src).expanduser().exists():
        raise HTTPException(400, f"路径不存在且不是链接: {src}")
    job = Job(id=uuid.uuid4().hex[:12], source=src, options=_options_dict(req))
    job.control = extract.Control()
    with JOBS_LOCK:
        JOBS[job.id] = job
        if len(JOBS) > MAX_JOBS:
            for old in sorted(JOBS.values(), key=lambda j: j.created_at)[:len(JOBS) - MAX_JOBS]:
                JOBS.pop(old.id, None)
    JOB_QUEUE.put(job.id)
    return job.detail()


@app.get("/api/jobs")
def list_jobs() -> dict:
    with JOBS_LOCK:
        items = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)
    return {"jobs": [j.brief() for j in items]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.detail()


def _live_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.status in ("done", "failed", "cancelled"):
        raise HTTPException(409, f"任务已结束（{job.status}），无法继续操作")
    return job


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str) -> dict:
    """请求暂停。在下一个检查点停下（转写的分段 / OCR 的帧 / 阶段之间）。"""
    job = _live_job(job_id)
    if job.control is not None:
        job.control.pause()
    return job.detail()


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str) -> dict:
    job = _live_job(job_id)
    if job.control is not None:
        job.control.resume()
    return job.detail()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """请求取消。排队中/暂停中的任务同样可以取消。"""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.status in ("done", "failed", "cancelled"):
        raise HTTPException(409, f"任务已结束（{job.status}）")
    if job.control is not None:
        job.control.cancel()
    else:
        job.status = "cancelled"
        job.message = "已取消（未开始）"
    return job.detail()


@app.get("/api/jobs/{job_id}/result")
def get_result(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.status != "done":
        raise HTTPException(409, f"任务尚未完成（当前 {job.status}）")
    return build_result(job)


@app.get("/api/jobs/{job_id}/files/{name}")
def download(job_id: str, name: str) -> FileResponse:
    job = JOBS.get(job_id)
    if job is None or not job.out_dir:
        raise HTTPException(404, "任务不存在或暂无输出")
    base = Path(job.out_dir).resolve()
    target = (base / name).resolve()
    # 防目录穿越：解析后的路径必须仍在任务输出目录内
    if base not in target.parents or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, filename=target.name)


if STATIC_DIR.exists():
    # 挂在最后：静态资源不拦截上面已注册的 /api 路由
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def _open_browser_when_ready(url: str, tries: int = 60) -> None:
    """轮询健康检查，服务真的起来之后再打开浏览器。

    直接 sleep 固定秒数是不稳的：模型/依赖首次加载可能超过 3 秒，
    那样会打开一个"无法连接"的页面。
    """
    import time
    import urllib.request
    import webbrowser

    for _ in range(tries):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(url + "/api/health", timeout=1)
            webbrowser.open(url)
            return
        except Exception:
            continue
    print(f"[提示] 服务启动较慢，请手动打开 {url}")


if __name__ == "__main__":
    import argparse
    import threading

    import uvicorn

    ap = argparse.ArgumentParser(description="video-extract Web 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="启动完成后自动打开浏览器")
    ap.add_argument("--reload", action="store_true", help="改代码自动重载（开发用）")
    a = ap.parse_args()

    url = f"http://{a.host}:{a.port}"
    print(f"\n  界面地址: {url}\n  按 Ctrl+C 停止服务\n")
    if a.open:
        threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

    uvicorn.run("server:app", host=a.host, port=a.port, reload=a.reload)
