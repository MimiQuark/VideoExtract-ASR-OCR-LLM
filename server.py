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
            # 列表接口不带整个 options（太重），只暴露 UI 需要的这一个标记
            "recovered": bool((self.options or {}).get("recovered")),
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
        # process() 直接返回产物目录 —— 原先靠"output/ 下最新的目录"来猜，
        # 多任务并行时会张冠李戴（实测：21 分钟的视频挂上了另一个视频的统计）
        out_dir = extract.process(job.source, OUT_ROOT, args)
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


# --------------------------------------------------------------------------- 历史恢复

def recover_jobs_from_disk() -> int:
    """启动时从 output/ 恢复历史任务。

    任务表原先只存在内存里，服务一重启就空了——而真正的产物（逐字稿、报告、
    画面文字）其实都还在磁盘上，只是列表里看不到，页面因此像"什么都没有"。
    这里按目录扫描把条目补回来：状态一律标为 done，source 无法还原就留空，
    id 由目录名哈希得到（跨重启保持稳定，下载链接才不会失效）。
    """
    if not OUT_ROOT.exists():
        return 0
    restored = 0
    for d in sorted(OUT_ROOT.iterdir(), key=lambda p: p.stat().st_mtime):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        if not ((d / "transcript.json").exists() or (d / "report.md").exists()):
            continue          # 空目录/下载缓存，不算任务
        job_id = "disk-" + uuid.uuid5(uuid.NAMESPACE_URL, d.name).hex[:10]
        mtime = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        job = Job(id=job_id, source="", options={"recovered": True})
        job.status = "done"
        job.pct = 100
        job.message = "已完成（从磁盘恢复的历史任务）"
        job.out_dir = str(d)
        job.created_at = mtime
        job.finished_at = mtime
        JOBS[job_id] = job
        restored += 1
    return restored


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


# --------------------------------------------------------------------------- 模型配置

ENV_FILE = BASE_DIR / "llm.env.bat"
_FORBIDDEN = set("\r\n%&|<>^\"")


def _mask(key: str) -> str:
    """只回显掩码，绝不把完整 Key 发给前端。"""
    if not key:
        return ""
    return (key[:6] + "…" + key[-4:]) if len(key) > 14 else "已配置"


def _clean(value: str, field: str) -> str:
    """拒绝会导致 .bat 注入的字符（换行/百分号/重定向等）。

    这些值会被写进 llm.env.bat，一旦允许换行就能往里塞任意命令。
    """
    value = (value or "").strip()
    bad = sorted(set(value) & _FORBIDDEN)
    if bad:
        raise HTTPException(400, f"{field} 含不允许的字符: {' '.join(repr(c) for c in bad)}")
    return value


class SettingsRequest(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    persist: bool = False      # 是否同时写入 llm.env.bat（重启后仍生效）


@app.get("/api/settings")
def get_settings() -> dict:
    key = os.environ.get("OPENAI_API_KEY", "")
    return {
        "configured": bool(key),
        "api_key_masked": _mask(key),
        "base_url": os.environ.get("OPENAI_BASE_URL", ""),
        "model": os.environ.get("OPENAI_MODEL", ""),
        # 值来自启动环境还是界面上填的（界面上填的只在内存里，重启就没了）
        "from_env": bool(os.environ.get("OPENAI_API_KEY_FROM_ENV")),
        "env_file": str(ENV_FILE),
        "env_file_exists": ENV_FILE.exists(),
    }


@app.post("/api/settings")
def save_settings(req: SettingsRequest) -> dict:
    """配置 LLM。改动只作用于当前进程内存，立即生效、无需重启。

    persist=True 时额外写入 llm.env.bat（已在 .gitignore 中忽略）。
    """
    key = _clean(req.api_key, "API Key")
    base = _clean(req.base_url, "Base URL")
    model = _clean(req.model, "模型名")
    if key:
        os.environ["OPENAI_API_KEY"] = key
    if base:
        os.environ["OPENAI_BASE_URL"] = base
    if model:
        os.environ["OPENAI_MODEL"] = model

    if req.persist and key:
        if ENV_FILE.exists():
            # 备份一份，避免手写内容被直接覆盖
            ENV_FILE.with_suffix(".bat.bak").write_text(
                ENV_FILE.read_text(encoding="gbk", errors="replace"), encoding="gbk")
        lines = [
            "@echo off",
            "rem 由界面保存。本文件含密钥，已在 .gitignore 中忽略，不要提交。",
            f"set OPENAI_API_KEY={key}",
        ]
        if base:
            lines.append(f"set OPENAI_BASE_URL={base}")
        if model:
            lines.append(f"set OPENAI_MODEL={model}")
        for extra in ("HF_ENDPOINT", "HF_HUB_DISABLE_XET"):
            if os.environ.get(extra):
                lines.append(f"set {extra}={_clean(os.environ[extra], extra)}")
        # newline="" 关闭换行转换：否则文本模式会把 \n 再转成 \r\n，
        # 写成 \r\r\n，文件里每行之间平白多一个空行
        with open(ENV_FILE, "w", encoding="gbk", newline="") as fh:
            fh.write("\r\n".join(lines) + "\r\n")

    return get_settings()


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


# 标记：Key 是否来自启动环境（用于界面区分"环境给的"与"界面填的"）
if os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY_FROM_ENV"] = "1"

# 启动即恢复历史任务：否则重启后任务列表为空，页面看起来"什么都没有"
_restored = recover_jobs_from_disk()
if _restored:
    print(f"  已从磁盘恢复 {_restored} 个历史任务")

if STATIC_DIR.exists():
    # 挂在最后：静态资源不拦截上面已注册的 /api 路由
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def _port_open(host: str, port: int) -> bool:
    """端口上是否已有程序在监听。"""
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) == 0


def _is_our_service(url: str) -> bool:
    """确认占用端口的是本服务，而不是别的程序。

    只看"端口被占"是不够的——用户很可能同时开着别的项目（本项目默认 8000，
    同类工具也常用 8000），必须能区分开再决定是"直接打开"还是"报错换端口"。
    """
    import urllib.request
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=1.5) as resp:
            data = json.load(resp)
        return data.get("status") == "ok" and "queue_size" in data
    except Exception:
        return False


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

    # 端口已被占用时的处理：分成"本服务已在跑"和"别的程序占着"两种情况
    if _port_open(a.host, a.port):
        if _is_our_service(url):
            print(f"\n  端口 {a.port} 上已有本服务在运行，直接打开界面。\n  {url}")
            print("  提示: 若标签页显示的是旧页面，按 Ctrl+F5 强制刷新。\n")
            import webbrowser
            webbrowser.open(url)
            raise SystemExit(0)
        print(f"\n  [错误] 端口 {a.port} 已被其他程序占用。\n"
              f"  解决办法（任选其一）:\n"
              f"    1) 换一个端口:  python server.py --port 8010      （双击启动则 start.bat --port 8010）\n"
              f"    2) 先结束占用 {a.port} 的程序，再重新启动\n")
        raise SystemExit(1)

    print(f"\n  界面地址: {url}\n  按 Ctrl+C 停止服务")
    print("  提示: 若浏览器标签页上显示的是别的页面（同一端口被其它项目用过），"
          "按 Ctrl+F5 强制刷新即可。\n")
    if a.open:
        threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

    uvicorn.run("server:app", host=a.host, port=a.port, reload=a.reload)
