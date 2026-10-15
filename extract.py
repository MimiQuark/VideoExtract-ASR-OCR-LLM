#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
video-extract —— 自动化提取视频内容

流程:
  1. 输入解析   本地文件 / 在线链接(yt-dlp, 优先抓现成字幕轨)
  2. 音频抽取   ffmpeg -> 16k 单声道 wav
  3. 语音转写   faster-whisper -> 带时间戳的 srt / txt / json
  4. 画面文字   抽帧(可只取底部字幕带) -> RapidOCR -> 去重 -> 时间轴
  5. 汇总报告   语音+画面合并成时间轴 report.md (可选 LLM 总结)

用法示例:
  python extract.py "D:\\videos\\talk.mp4"
  python extract.py "https://www.bilibili.com/video/BVxxxx" --model medium --lang zh
  python extract.py "D:\\videos" --ocr --ocr-region bottom --ocr-interval 2
  python extract.py "talk.mp4" --summarize --out "D:\\out"
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm",
    ".ts", ".m4v", ".mpg", ".mpeg", ".rmvb", ".3gp",
}
SUBTITLE_EXTS = [".srt", ".vtt", ".ass", ".json3", ".ttml"]
DEFAULT_SUB_LANGS = "zh-Hans,zh-CN,zh,zh-Hant,en"

# ----------------------------------------------------------------------------- 基础设施

class CancelledError(Exception):
    """用户主动取消任务。在检查点抛出，由调用方负责收尾。"""


class Control:
    """任务控制：暂停 / 恢复 / 取消。

    为什么只能在检查点生效：ASR 与 OCR 都是第三方库里的阻塞调用，
    无法从外部中断单次推理。所以把检查点放在**长循环的迭代处**——
    转写的每个分段、OCR 的每一帧、下载的每次进度回调、以及各阶段之间。
    实际粒度约等于"一帧/一句"，对视频这类长任务已经足够灵敏。
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._paused = False
        self.on_state = None          # 可选回调：(state, message) -> None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def paused(self) -> bool:
        return self._paused

    def cancel(self) -> None:
        self._cancelled = True
        self._paused = False          # 取消要能唤醒暂停中的等待
        self._notify("cancelled", "已请求取消")

    def pause(self) -> None:
        if not self._cancelled:
            self._paused = True
            self._notify("paused", "已暂停（将在当前检查点停下）")

    def resume(self) -> None:
        self._paused = False
        self._notify("running", "已恢复")

    def _notify(self, state: str, message: str) -> None:
        if self.on_state is not None:
            try:
                self.on_state(state, message)
            except Exception:
                pass

    def check(self) -> None:
        """检查点：暂停时阻塞等待，取消时抛 CancelledError。"""
        while self._paused and not self._cancelled:
            time.sleep(0.2)
        if self._cancelled:
            raise CancelledError("任务已取消")


# 由调用方（Web 服务）注入；CLI 下为 None，检查点直接放行
CONTROL: Control | None = None


def checkpoint() -> None:
    if CONTROL is not None:
        CONTROL.check()


# 日志钩子：Web 服务用它把完整日志回传给前端；CLI 下为 None。
LOG_HOOK = None


def log(msg: str = "") -> None:
    if LOG_HOOK is not None:
        try:
            LOG_HOOK(msg)
        except Exception:
            pass
    print(msg, flush=True)


def die(msg: str, code: int = 1):
    log(f"\n[错误] {msg}")
    sys.exit(code)


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


# 进度钩子：由调用方（如 Web 服务）注入。CLI 下保持 None，不产生任何额外输出。
# 约定 (stage_name, 百分比 0-100, 给用户看的一句话)。
PROGRESS_HOOK = None


def stage(name: str, pct: int, msg: str = "") -> None:
    """上报当前处理阶段。钩子内部异常必须吞掉，不能影响主流程。"""
    if PROGRESS_HOOK is None:
        return
    try:
        PROGRESS_HOOK(name, int(pct), msg or name)
    except Exception:
        pass


FFMPEG_EXE: str | None = None


def ffmpeg_path() -> str | None:
    """按优先级定位 ffmpeg: 环境变量 -> 系统 PATH -> 项目自带 tools/ -> imageio-ffmpeg 内置。

    这样无论是否装了系统 ffmpeg、有没有管理员权限, 都能找到可用的二进制。
    """
    global FFMPEG_EXE
    if FFMPEG_EXE:
        return FFMPEG_EXE
    env = os.environ.get("FFMPEG_BINARY")
    if env and Path(env).exists():
        FFMPEG_EXE = env
    elif which("ffmpeg"):
        FFMPEG_EXE = which("ffmpeg")
    else:
        exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        local = Path(__file__).resolve().parent / "tools" / exe
        if local.exists():
            FFMPEG_EXE = str(local)
        else:
            try:
                import imageio_ffmpeg
                cand = imageio_ffmpeg.get_ffmpeg_exe()
                FFMPEG_EXE = cand if cand and Path(cand).exists() else None
            except Exception:
                FFMPEG_EXE = None
    return FFMPEG_EXE


def require_ffmpeg() -> str:
    exe = ffmpeg_path()
    if not exe:
        die(
            "未找到 ffmpeg。三种解决方式(任选其一):\n"
            "  1) 运行自动安装: powershell -ExecutionPolicy Bypass -File setup.ps1\n"
            "  2) 装 Python 内置版: pip install imageio-ffmpeg\n"
            "  3) 手动放置到: <脚本目录>\\tools\\ffmpeg.exe\n"
            "  也可用环境变量 FFMPEG_BINARY 指定完整路径。"
        )
    return exe


def have_module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def run(cmd: list[str], desc: str = "", quiet: bool = True) -> int:
    if desc:
        log(f"  -> {desc}")
    kwargs = {}
    if quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.PIPE
    proc = subprocess.run(cmd, **kwargs)
    if proc.returncode != 0:
        err = ""
        if quiet and proc.stderr:
            err = proc.stderr.decode("utf-8", errors="replace")[-800:]
        die(f"命令失败 (exit {proc.returncode}): {' '.join(cmd[:3])} ...\n{err}")
    return proc.returncode


def fmt_ts(seconds: float, srt: bool = False) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if srt:
        ms = int(round((seconds - int(seconds)) * 1000))
        if ms == 1000:
            ms, s = 0, s + 1
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def safe_name(text: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|]", "_", text).strip(" .")
    return (text or "video")[:80]


_CC = None
_CC_READY = False


def _opencc():
    global _CC, _CC_READY
    if not _CC_READY:
        _CC_READY = True
        try:
            from opencc import OpenCC
            _CC = OpenCC("t2s")
        except Exception:
            _CC = None
    return _CC


def t2s_available() -> bool:
    return _opencc() is not None


def normalize_chinese(items: list) -> tuple[list, int]:
    """繁体转简体。

    Whisper 对中文偶尔会整段输出繁体(实测 21 分钟视频里有 1 段), 对中文用户是明显瑕疵。
    简体文本转换后不变, 所以可以无脑对所有中文文本执行。
    """
    cc = _opencc()
    if cc is None:
        return items, 0
    out, changed = [], 0
    for it in items:
        text = cc.convert(it.text)
        if text != it.text:
            changed += 1
            out.append(type(it)(it.start, it.end, text))
        else:
            out.append(it)
    return out, changed


# ----------------------------------------------------------------------------- 数据模型

@dataclass
class Speech:
    start: float
    end: float
    text: str


@dataclass
class Screen:
    start: float
    end: float
    text: str


# ----------------------------------------------------------------------------- 1. 输入解析

def is_url(s: str) -> bool:
    return s.lower().startswith(("http://", "https://"))


def download(url: str, workdir: Path, want_subs: bool = True) -> tuple[Path, Path | None, str]:
    """用 yt-dlp 下载。返回 (视频路径, 字幕路径或None, 标题)。"""
    if not have_module("yt_dlp"):
        die("需要 yt-dlp 下载在线视频: pip install yt-dlp")
    import yt_dlp  # noqa: F401

    # 用视频 id 命名: 固定叫 source 会让 yt-dlp 把上一个视频的文件当成已下载而跳过
    tmpl = str(workdir / "%(id)s.%(ext)s")
    opts = {
        "outtmpl": tmpl,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_path(),
    }
    if want_subs:
        opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": DEFAULT_SUB_LANGS.split(","),
            "subtitlesformat": "srt/vtt/best",
            "skip_download": False,
        })
    def _dl_hook(d: dict) -> None:
        # 下载是最早的长耗时步骤，把进度与检查点都放在这里：
        # 取消时直接抛错，yt-dlp 会随之退出。
        checkpoint()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            if total:
                pct = got / total * 100
                stage("download", 5 + int(pct * 0.04),
                      f"下载中 {pct:.0f}% ({got / 1048576:.0f}/{total / 1048576:.0f} MB)")

    opts["progress_hooks"] = [_dl_hook]
    log("  -> yt-dlp 下载中 (优先抓现成字幕轨)...")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    vid_id = str(info.get("id") or "")

    def _downloaded(exts) -> list[Path]:
        """找出本次下载产生的文件。

        不能只 glob ``f"{vid_id}.*"``：多 P（分 P）视频会被 yt-dlp 命名成
        ``<id>_p1.mp4`` / ``<id>_p2.mp4``，分隔符是下划线而不是点，
        用点号匹配会一个都找不到，然后误报"下载完成但没找到视频文件"
        （实测一个 3 分 P 的 1.6GB 视频就这样白等了 32 分钟）。
        这里统一按"以 id 开头"匹配，点号前缀和下划线前缀都兼容。
        """
        out: list[Path] = []
        for f in sorted(workdir.iterdir()):
            if not f.is_file() or f.suffix.lower() not in exts:
                continue
            name = f.name
            if name.startswith(f"{vid_id}.") or name.startswith(f"{vid_id}_") or name == vid_id:
                out.append(f)
        return out

    videos = _downloaded(VIDEO_EXTS)
    if not videos:
        die(f"下载完成但没找到视频文件，请检查目录: {workdir}")
    video = videos[0]
    if len(videos) > 1:
        log(f"  [提示] 该链接包含 {len(videos)} 个分 P，本次只处理第一个: {video.name}")
        log("         需要处理其它分 P 请单独传入对应链接。")

    sub = None
    found = _downloaded(SUBTITLE_EXTS)
    if found:
        # 优先中文
        found.sort(key=lambda p: (0 if re.search(r"zh|chi", p.name, re.I) else 1, len(p.name)))
        sub = found[0]
        log(f"  -> 命中现成字幕轨: {sub.name} (省掉ASR，更准)")
    title = (info.get("title") or "").strip()
    log(f"  -> 视频标题: {title}")
    return video, sub, title


def parse_srt(path: Path) -> list[Speech]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    raw = raw.replace("\r\n", "\n")
    out: list[Speech] = []
    pattern = re.compile(
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
    )
    for block in re.split(r"\n\s*\n", raw):
        m = pattern.search(block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = block[m.end():]
        lines = [ln.strip() for ln in body.split("\n")]
        lines = [ln for ln in lines if ln and not ln.isdigit()]
        text = " ".join(lines)
        text = re.sub(r"<[^>]+>", "", text).strip()
        if text:
            out.append(Speech(start, end, text))
    return out


# ----------------------------------------------------------------------------- 2. 音频抽取

def extract_audio(video: Path, out_wav: Path) -> Path:
    run([
        require_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(out_wav),
    ], desc=f"抽取音频 -> {out_wav.name}")
    if not out_wav.exists() or out_wav.stat().st_size < 1024:
        die("音频抽取失败：该视频可能没有音轨。")
    return out_wav


# ----------------------------------------------------------------------------- 3. 语音转写

def transcribe(wav: Path, model_name: str, language: str | None,
               device: str, compute_type: str, initial_prompt: str | None,
               cpu_threads: int = 0) -> list[Speech]:
    if not have_module("faster_whisper"):
        die("缺少 faster-whisper。先运行: powershell -ExecutionPolicy Bypass -File setup.ps1")
    from faster_whisper import WhisperModel

    log(f"  -> 加载 Whisper 模型: {model_name} ({device}/{compute_type})")
    if device == "auto":
        device = "cuda" if _cuda_ok() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
    # CPU 推理默认只用 4 线程, 显式拉满物理核心能显著提速
    threads = cpu_threads or (os.cpu_count() or 4)
    log(f"  -> CPU 线程数: {threads}")
    checkpoint()   # 首次使用某模型会联网下载（几百 MB~1.5GB），下载本身无法中断
    log(f"  -> 加载 Whisper 模型: {model_name} ({device}/{compute_type})")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type,
                             cpu_threads=threads)
    except Exception as exc:  # 显存不足 / 无 CUDA 时自动降级
        log(f"  [警告] {device}/{compute_type} 初始化失败({exc})，回退 CPU/int8")
        model = WhisperModel(model_name, device="cpu", compute_type="int8",
                             cpu_threads=threads)

    checkpoint()
    log("  -> 转写中(首次会自动下载模型)...")
    segments, info = model.transcribe(
        str(wav),
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        initial_prompt=initial_prompt,
        condition_on_previous_text=False,
    )
    log(f"  -> 识别语言: {info.language} (置信度 {info.language_probability:.2f})")

    out: list[Speech] = []
    for seg in segments:
        checkpoint()          # 每识别出一句就检查一次暂停/取消
        text = (seg.text or "").strip()
        if not text:
            continue
        out.append(Speech(round(seg.start, 3), round(seg.end, 3), text))
        log(f"\r     [{fmt_ts(seg.end)}] {text[:56]}")
    log()
    return out


def _cuda_ok() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


# ----------------------------------------------------------------------------- 4. 画面文字 OCR

def sample_frames(video: Path, frames_dir: Path, interval: float,
                  region: str, max_width: int | None, max_frames: int) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    vf = [f"fps=1/{interval}"]
    if region == "bottom":
        vf.append("crop=iw:ih*0.35:0:ih*0.65")   # 字幕带
    elif region == "top":
        vf.append("crop=iw:ih*0.3:0:0")
    if max_width:
        vf.append(f"scale={max_width}:-2")
    checkpoint()
    run([
        require_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-vf", ",".join(vf),
        "-frames:v", str(max_frames), "-q:v", "3",
        str(frames_dir / "f%06d.jpg"),
    ], desc=f"抽帧 (每 {interval}s 一张, region={region})")
    return sorted(frames_dir.glob("f*.jpg"))


def ocr_frames(frames: list[Path], interval: float, min_score: float) -> list[Screen]:
    if not have_module("rapidocr_onnxruntime"):
        log("  [跳过] 未安装 rapidocr-onnxruntime，无法做画面文字识别。")
        log("         安装: pip install rapidocr-onnxruntime")
        return []
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    events: list[Screen] = []
    last_key = None
    for idx, img in enumerate(frames):
        checkpoint()      # 每帧检查：只是一次标志位读取，开销可忽略，
                          # 换来取消/暂停更灵敏（OCR 一帧约 0.5~2s）
        ts = idx * interval
        try:
            result, _ = engine(str(img))
        except Exception as exc:
            log(f"  [警告] OCR 失败 {img.name}: {exc}")
            continue
        if not result:
            last_key = None
            continue
        lines = []
        for box, text, score in result:
            text = (text or "").strip()
            if not text or float(score) < min_score:
                continue
            lines.append(text)
        if not lines:
            continue
        text = " ".join(lines)
        key = re.sub(r"\s+", "", text)
        if key == last_key:                      # 与上一帧相同 -> 只延长时间
            if events:
                events[-1].end = round(ts + interval, 3)
            continue
        last_key = key
        events.append(Screen(round(ts, 3), round(ts + interval, 3), text))
        log(f"\r     [{fmt_ts(ts)}] {text[:50]}")
    log()
    return events


# ----------------------------------------------------------------------------- 5. 汇总输出

_PUNCT_RE = re.compile(
    "[\u3000\\s,.\uFF0C\u3002!\uFF01?\uFF1F:\uFF1A;\uFF1B\"'\u201C\u201D\u2018\u2019"
    "()\uFF08\uFF09\\[\\]\u3010\u3011\u300A\u300B<>\u2014_\u2026~\u00B7-]+"
)


def _norm_key(text: str) -> str:
    """归一化: 去掉空白与标点, 只保留实义字符, 用于相似度比较。"""
    return _PUNCT_RE.sub("", text)


def _sim(a: str, b: str) -> float:
    """文本相似度。

    取两种度量的较大值:
      - ratio     : 标准序列相似度, 适合长度相当的两句话
      - containment: 匹配字符数 / 较短串长度, 适合字幕比 ASR 文本短的情况
        (ASR 常带"首先""那么"等语气词, 字幕不会写, 用 ratio 会低估)
    较短串少于 4 字时不用包含度, 避免"可以"这种短词误命中。
    """
    if not a or not b:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b)
    ratio = sm.ratio()
    short = min(len(a), len(b))
    if short >= 4:
        matched = sum(bl.size for bl in sm.get_matching_blocks())
        return max(ratio, matched / short)
    return ratio


# 抽帧间隔会让硬字幕与语音段时间上错开, 配对时需要容差
SCREEN_TIME_TOLERANCE = 2.5


def _extract_core(screen_text: str, speech_text: str) -> str:
    """从 OCR 行里只取出真正对应字幕的那一段。

    难点: 幻灯片类视频的 OCR 会把整屏文字(幻灯片正文 + 水印 + 浏览器栏 + 字幕)
    用空格拼成一条。若按"第一个匹配片段到最后一个匹配片段"裁剪, 一旦首尾各匹配上
    一个片段, 中间的整页幻灯片文字都会被吞进来(实测导致文稿膨胀 3 倍)。

    所以这里只围绕"最长匹配"所在的片段, 在 1~3 个相邻片段的窗口内选相似度最高的
    组合, 并把长度作为次要标准(同分取更短), 保证既不丢字幕, 也不吞幻灯片。
    """
    if not screen_text:
        return ""
    sm = difflib.SequenceMatcher(None, screen_text, speech_text, autojunk=False)
    longest = sm.find_longest_match(0, len(screen_text), 0, len(speech_text))
    if longest.size == 0:
        return ""

    tokens = screen_text.split()
    if not tokens:
        return ""
    # 记录每个片段在原文里的位置
    spans: list[tuple[int, int]] = []
    cursor = 0
    for tk in tokens:
        i = screen_text.find(tk, cursor)
        spans.append((i, i + len(tk)))
        cursor = i + len(tk)

    # 最长匹配落在哪个片段上
    hit = 0
    for idx, (a, b) in enumerate(spans):
        if a <= longest.a < b:
            hit = idx
            break

    base_key = _norm_key(speech_text)
    best_text, best_score, best_len = "", -1.0, 0
    for win in (1, 2, 3):
        for s0 in range(max(0, hit - win + 1), hit + 1):
            s1 = min(len(tokens), s0 + win)
            if s0 >= s1:
                continue
            cand = " ".join(tokens[s0:s1]).strip()
            if not cand:
                continue
            score = _sim(_norm_key(cand), base_key)
            if score > best_score or (score == best_score and len(cand) < best_len):
                best_text, best_score, best_len = cand, score, len(cand)
    return best_text or screen_text[spans[hit][0]:spans[hit][1]].strip()


def _splice_correction(speech_text: str, core: str) -> str:
    """把语音文本里与字幕不一致的地方换成字幕写法, 其余内容原样保留。

    不做整句替换的原因: 语音段常带有"首先""那么"等字幕里没有的语气词, 整句替换会
    把它们连同信息一起丢掉; 而且一旦匹配到错行的字幕, 整句替换会引入无关内容。
    这里只改"对不上"的片段, 因此既纠错又不丢字。
    """
    if not speech_text or not core:
        return speech_text
    sm = difflib.SequenceMatcher(None, speech_text, core, autojunk=False)
    parts: list[str] = []
    replaced = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            parts.append(speech_text[i1:i2])
        elif tag == "replace":
            parts.append(core[j1:j2])
            replaced += i2 - i1
        elif tag == "delete":
            # 语音比字幕多出来的字(语气词): 保留
            parts.append(speech_text[i1:i2])
        # insert: 字幕比语音多出的字, 丢弃, 避免凭空加词
    # 改动面积过大说明匹配到了不相干的字幕行, 放弃这次校对
    if replaced > 0.5 * max(1, len(speech_text)):
        return speech_text
    return "".join(parts)


def repair_with_screen(speech: list[Speech], screen: list[Screen],
                       threshold: float = 0.5) -> tuple[list[Speech], int]:
    """用视频自带的硬字幕校正语音转写。

    流程: 对每条语音段, 在时间邻近的屏幕文字里提取出"最像本句的那段字幕"
    (见 _extract_core), 再用 _splice_correction 只替换对不上的片段。

    关键设计: 打分必须针对真正要用进去的那段核心文字, 而不是整屏 OCR 原文 ——
    幻灯片类视频的整屏文字会把打分带偏, 选到错行的字幕。
    """
    if not speech or not screen:
        return speech, 0

    best: dict[int, tuple[float, str]] = {}
    for si, s in enumerate(speech):
        speech_key = _norm_key(s.text)
        if len(speech_key) < 2:
            continue
        for c in screen:
            if (c.end < s.start - SCREEN_TIME_TOLERANCE
                    or c.start > s.end + SCREEN_TIME_TOLERANCE):
                continue
            core = _extract_core(c.text, s.text)
            core_key = _norm_key(core)
            if len(core_key) < 2:
                continue
            score = _sim(core_key, speech_key)
            if score < threshold:
                continue
            cur = best.get(si)
            if cur is None or score > cur[0]:
                best[si] = (score, core)

    fixed = 0
    out: list[Speech] = []
    for si, s in enumerate(speech):
        item = best.get(si)
        text = _splice_correction(s.text, item[1]) if item else s.text
        if text != s.text and _norm_key(text) != _norm_key(s.text):
            fixed += 1
            out.append(Speech(s.start, s.end, text))
        else:
            out.append(s)
    return out, fixed


def write_srt(speech: list[Speech], path: Path) -> None:
    blocks = []
    for i, s in enumerate(speech, 1):
        blocks.append(f"{i}\n{fmt_ts(s.start, True)} --> {fmt_ts(s.end, True)}\n{s.text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def coalesce_screen(screen: list[Screen]) -> list[Screen]:
    """把相邻且文字相同的画面文字合并成一条(OCR 偶发失败时会被拆断)。"""
    out: list[Screen] = []
    for s in screen:
        key = re.sub(r"\s+", "", s.text)
        if out and re.sub(r"\s+", "", out[-1].text) == key:
            out[-1].end = max(out[-1].end, s.end)
            continue
        out.append(Screen(s.start, s.end, s.text))
    return out


def merge_timeline(speech: list[Speech], screen: list[Screen]) -> list[tuple[float, str, str]]:
    rows: list[tuple[float, str, str]] = [(s.start, "speech", s.text) for s in speech]
    rows += [(s.start, "screen", s.text) for s in coalesce_screen(screen)]
    rows.sort(key=lambda r: r[0])
    return rows


SPEECH_BUDGET = 60000
SCREEN_BUDGET = 15000


def screen_digest(screen: list[Screen], limit: int = SCREEN_BUDGET) -> str:
    """把画面文字整理成可供 LLM 阅读的紧凑文本。

    两步处理:
      1. 去重 —— 幻灯片长期停留会让同一条文字在几十帧里重复出现,
         不去重会把 prompt 预算全耗在重复内容上;
      2. 去噪 —— 丢弃过短的行(单字/符号),这些多半是 OCR 对画面装饰元素的误识。
    """
    seen: set[str] = set()
    kept: list[str] = []
    for s in screen:
        key = re.sub(r"\s+", "", s.text)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        kept.append(s.text.strip())
    digest = "\n".join(kept)
    if len(digest) > limit:
        digest = digest[:limit] + "\n...(已截断)"
    return digest


def _speech_block(speech: list[Speech], limit: int) -> tuple[str, int]:
    """把语音转写拼成带时间戳的文本块。

    时间戳必须一起给模型: 否则"关键时间点"这一节只能瞎猜或退化成发布日期
    (实测过)。这里按预算截断, 并返回实际用掉的字符数用于日志。
    """
    lines: list[str] = []
    used = 0
    for item in speech:
        line = f"[{fmt_ts(item.start)}] {item.text}"
        if used + len(line) > limit:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines), used


def summarize(speech: list[Speech], title: str, model: str = "gpt-4o-mini",
              screen_text: str = "") -> str | None:
    """用 LLM 生成结构化总结。

    两路内容都进 prompt:
      - 语音转写(带时间戳)给"说了什么";
      - 画面文字给"展示过什么"(幻灯片标题、框架名、代码、表格),
        用来补全语音里指代不明或一笔带过的部分(如"这三个框架"到底是哪三个)。
    """
    if not os.environ.get("OPENAI_API_KEY"):
        log("  [跳过] 未设置 OPENAI_API_KEY，不做 LLM 总结。")
        return None
    if not have_module("openai"):
        log("  [跳过] 未安装 openai 包: pip install openai")
        return None
    from openai import OpenAI

    body, used = _speech_block(speech, SPEECH_BUDGET)
    parts = [
        "你是视频内容分析助手。下面是一个视频的两路提取结果:",
        "- 【语音转写】: 视频里说的话, 每行以 [时间戳] 开头(已用硬字幕校对过 ASR 错字)",
        "- 【画面文字】: 从画面 OCR 得到的文字(幻灯片正文、硬字幕、表格), 已按出现顺序去重",
        "",
        "请输出中文结构化总结:",
        "## 一句话主题",
        "## 核心要点(5-10条)",
        "## 关键结论 / 行动项",
        "## 关键时间点(每条格式为 `mm:ss — 要点`; 时间必须取自【语音转写】的方括号时间戳,",
        "  不要拿画面文字里的日期当时间点, 也不要写视频发布日期)",
        "",
        "要求:",
        "- 忠于原文, 不要编造; 保留重要数字、专有名词",
        "- 画面文字常含幻灯片标题与框架/工具名称, 用它补全语音里口语化、指代不明的部分",
        "- 若两路内容冲突, 以语音转写为准(画面文字可能存在 OCR 误识)",
        "",
        f"视频标题: {title}",
        "",
        f"【语音转写】(共 {used} 字)",
        body,
    ]
    if screen_text:
        parts += ["", "【画面文字】", screen_text]
    prompt = "\n".join(parts)

    try:
        client = OpenAI()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return resp.choices[0].message.content
    except Exception as exc:
        log(f"  [警告] LLM 总结失败: {exc}")
        return None


def build_report(title: str, source: str, speech: list[Speech], screen: list[Screen],
                 summary: str | None, lang_note: str, out: Path,
                 repaired_speech: list[Speech] | None = None) -> None:
    lines: list[str] = [f"# {title}", ""]
    lines.append(f"- 来源: `{source}`")
    lines.append(f"- 语音片段: {len(speech)} 段 / 画面文字: {len(screen)} 条")
    lines.append(f"- 识别方式: {lang_note}")
    if speech:
        lines.append(f"- 时长覆盖: {fmt_ts(speech[-1].end)}")
    lines.append("")

    if summary:
        lines += ["## 🤖 智能总结", "", summary, ""]

    lines += ["## 📝 正文时间轴", "", "> 🗣=语音转写(已用字幕校对)  🖥=画面文字(OCR)", ""]
    if not speech and not screen:
        lines.append("_未提取到内容。_")
    for ts, kind, text in merge_timeline(repaired_speech or speech, screen):
        icon = "🗣" if kind == "speech" else "🖥"
        lines.append(f"- `[{fmt_ts(ts)}]` {icon} {text}")
    lines.append("")

    if screen:
        lines += ["## 🖥 画面文字全文(已去重)", ""]
        seen = set()
        for s in screen:
            k = re.sub(r"\s+", "", s.text)
            if k in seen:
                continue
            seen.add(k)
            lines.append(f"- `[{fmt_ts(s.start)}]` {s.text}")
        lines.append("")

    if repaired_speech:
        lines += ["## ✅ 校对后逐字稿（推荐复制这段）", "",
                  "> 已用视频硬字幕校正 ASR 错字，可直接用于复制/喂给大模型。", "",
                  "```text"]
        lines.append("".join(s.text for s in repaired_speech) or "(无)")
        lines += ["```", ""]

    lines += ["## 📄 语音转写原文（未校对）", "", "```text"]
    lines.append(" ".join(s.text for s in speech) or "(无)")
    lines += ["```", ""]

    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------------- 主流程

def process(source: str, out_root: Path, args) -> None:
    stage("resolve", 3, f"解析输入: {source}")
    work = Path(source).expanduser()
    log(f"\n{'=' * 68}")
    log(f"[处理] {source}")
    log("=" * 68)

    tmp_dl: Path | None = None
    existing_sub: Path | None = None

    if is_url(source):
        tmp_dl = out_root / "_download"
        tmp_dl.mkdir(parents=True, exist_ok=True)
        video, existing_sub, dl_title = download(source, tmp_dl, want_subs=not args.no_subs)
        title = dl_title or video.stem
    else:
        if not work.exists():
            die(f"文件不存在: {work}")
        video = work
        title = video.stem
        # 同目录下若已有字幕，直接复用
        for ext in SUBTITLE_EXTS:
            cand = video.with_suffix(ext)
            if cand.exists():
                existing_sub = cand
                break
        if existing_sub:
            log(f"  -> 检测到同名字幕文件: {existing_sub.name}")

    out = out_root / safe_name(title)
    out.mkdir(parents=True, exist_ok=True)
    stage("download", 8, f"已获取视频: {title}")

    # --- 语音 ---
    speech: list[Speech] = []
    lang_note = ""
    if args.reuse and (out / "transcript.json").exists():
        speech = [Speech(**x) for x in
                  json.loads((out / "transcript.json").read_text(encoding="utf-8"))]
        lang_note = "复用上次转写结果"
        log(f"  -> 复用已有转写: {len(speech)} 段")
    elif existing_sub and not args.force_asr:
        speech = parse_srt(existing_sub)
        lang_note = f"现成字幕轨 ({existing_sub.name})"
        log(f"  -> 解析字幕: {len(speech)} 段")
    if not speech:
        if args.no_asr:
            die("未找到字幕且指定了 --no-asr，没有可提取的语音内容。")
        checkpoint()
        stage("asr", 12, "语音转写中（最耗时的一步）…")
        wav = out / "audio.wav"
        extract_audio(video, wav)
        speech = transcribe(wav, args.model, args.lang, args.device,
                            args.compute_type, args.initial_prompt, args.cpu_threads)
        lang_note = f"faster-whisper {args.model}"
        if not args.keep_audio:
            wav.unlink(missing_ok=True)

    if speech and not args.no_t2s:
        speech, n_cc = normalize_chinese(speech)
        if n_cc:
            log(f"  -> 繁体转简体: 修正 {n_cc} 段")

    if speech:
        write_srt(speech, out / "transcript.srt")
        (out / "transcript.txt").write_text(
            "\n".join(s.text for s in speech), encoding="utf-8")
        (out / "transcript.json").write_text(
            json.dumps([s.__dict__ for s in speech], ensure_ascii=False, indent=2),
            encoding="utf-8")
        log(f"  -> 文稿已写入 transcript.srt / .txt / .json")

    # --- 画面文字 ---
    screen: list[Screen] = []
    if args.reuse and (out / "screen_text.json").exists():
        screen = [Screen(**x) for x in
                  json.loads((out / "screen_text.json").read_text(encoding="utf-8"))]
        log(f"  -> 复用已有画面文字: {len(screen)} 条")
    elif args.ocr:
        checkpoint()
        stage("ocr", 50, "画面文字识别中…")
        frames_dir = out / "frames"
        frames = sample_frames(video, frames_dir, args.ocr_interval,
                              args.ocr_region, args.ocr_max_width, args.ocr_max_frames)
        log(f"  -> 抽到 {len(frames)} 帧，开始 OCR...")
        screen = ocr_frames(frames, args.ocr_interval, args.ocr_min_score)
        (out / "screen_text.json").write_text(
            json.dumps([s.__dict__ for s in screen], ensure_ascii=False, indent=2),
            encoding="utf-8")
        if not args.keep_frames:
            shutil.rmtree(frames_dir, ignore_errors=True)

    if screen:
        stage("ocr", 75, f"画面文字识别完成: {len(screen)} 条")

    if screen and not args.no_t2s:
        screen, n_cc = normalize_chinese(screen)
        if n_cc:
            log(f"  -> 画面文字繁体转简体: {n_cc} 条")

    # --- 字幕校对: 用硬字幕修正 ASR 错字 ---
    repaired = None
    if speech:
        stage("asr", 45, f"语音转写完成: {len(speech)} 段")

    checkpoint()
    if speech and screen and not args.no_repair:
        repaired, fixed_n = repair_with_screen(speech, screen)
        if fixed_n:
            log(f"  -> 字幕校对: 修正 {fixed_n} 处 ASR 错字")
            (out / "transcript.repaired.txt").write_text(
                "".join(s.text for s in repaired), encoding="utf-8")
            (out / "transcript.repaired.json").write_text(
                json.dumps([s.__dict__ for s in repaired], ensure_ascii=False, indent=2),
                encoding="utf-8")
            write_srt(repaired, out / "transcript.repaired.srt")
            stage("repair", 82, f"字幕校对完成: 修正 {fixed_n} 处 ASR 错字")
            log(f"  -> transcript.repaired.txt / .srt / .json")
        else:
            repaired = None
            log("  -> 字幕校对: 无需修正")

    final_speech = repaired or speech
    screen_text = "" if args.no_screen_in_summary else screen_digest(screen)
    summary = None
    if args.summarize and final_speech:
        checkpoint()
        stage("summarize", 88, "LLM 生成总结中…")
        log("  -> 调用 LLM 生成总结"
            + (f" (语音 {len(final_speech)} 段 + 画面文字 {len(screen_text)} 字)" if screen_text
               else " (仅语音)"))
        summary = summarize(final_speech, title, args.summary_model, screen_text)

    stage("report", 95, "生成报告…")
    build_report(title, source, speech, screen, summary, lang_note or "无", out, repaired)
    stage("done", 100, "完成")
    log(f"\n✅ 完成 -> {out}")
    log(f"   report.md        汇总报告(时间轴+总结)")
    log(f"   transcript.srt   字幕文件")
    log(f"   transcript.txt   纯文稿")
    if screen:
        log(f"   screen_text.json 画面文字")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="自动化提取视频内容(语音转写 + 画面OCR + 汇总报告)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("input", nargs="+", help="视频文件 / 目录 / 视频链接(可多个)")
    p.add_argument("--out", default="output", help="输出目录 (默认: ./output)")

    g = p.add_argument_group("语音转写")
    g.add_argument("--model", default="medium",
                   help="whisper 模型: tiny/base/small/medium/large-v3 (默认 medium)")
    g.add_argument("--lang", default=None, help="语言代码: zh/en/ja... 省略则自动检测")
    g.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    g.add_argument("--compute-type", default="int8",
                   choices=["int8", "float16", "float32", "int8_float16"])
    g.add_argument("--cpu-threads", type=int, default=0,
                   help="CPU 推理线程数, 0=自动用满所有核心 (默认 0)")
    g.add_argument("--initial-prompt", default=None,
                   help="提示词，可显著改善专业术语/人名识别")
    g.add_argument("--no-asr", action="store_true", help="只要字幕，不做语音识别")
    g.add_argument("--no-t2s", action="store_true",
                   help="关闭繁体转简体(默认自动把 Whisper 偶发输出的繁体转成简体)")
    g.add_argument("--force-asr", action="store_true", help="忽略现成字幕，强制 ASR")
    g.add_argument("--no-subs", action="store_true", help="下载时不抓字幕轨")
    g.add_argument("--keep-audio", action="store_true", help="保留抽取的 wav")

    o = p.add_argument_group("画面文字 OCR")
    o.add_argument("--ocr", action="store_true", help="开启画面文字识别(硬字幕/PPT)")
    o.add_argument("--ocr-interval", type=float, default=2.0, help="抽帧间隔秒数 (默认 2)")
    o.add_argument("--ocr-region", default="full", choices=["full", "bottom", "top"],
                   help="识别区域: bottom=只认底部字幕带(更快更准)")
    o.add_argument("--ocr-max-width", type=int, default=None, help="缩放到指定宽度再 OCR")
    o.add_argument("--ocr-max-frames", type=int, default=1800, help="最多抽多少帧")
    o.add_argument("--ocr-min-score", type=float, default=0.6, help="OCR 置信度下限")
    o.add_argument("--keep-frames", action="store_true", help="保留抽出的帧图片")
    o.add_argument("--no-repair", action="store_true",
                   help="关闭字幕校对(默认用硬字幕自动修正 ASR 错字)")
    o.add_argument("--reuse", action="store_true",
                   help="复用输出目录里已有的转写/OCR结果, 只重算报告(调参时秒级完成)")

    s = p.add_argument_group("总结")
    s.add_argument("--summarize", action="store_true", help="用 LLM 生成结构化总结")
    s.add_argument("--summary-model", default="gpt-4o-mini")
    s.add_argument("--no-screen-in-summary", action="store_true",
                   help="总结时只用语音转写, 不并入画面文字(默认两者都送入, 便于补全指代不明的表述)")

    return p


def main() -> None:
    args = build_parser().parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    log(f"ffmpeg: {require_ffmpeg()}")

    out_root = Path(args.out).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    targets: list[str] = []
    for item in args.input:
        if is_url(item):
            targets.append(item)
        else:
            path = Path(item).expanduser()
            if path.is_dir():
                found = [p for p in sorted(path.rglob("*")) if p.suffix.lower() in VIDEO_EXTS]
                if not found:
                    log(f"[警告] 目录内未找到视频文件: {path}")
                targets += [str(p) for p in found]
            else:
                targets.append(item)

    if not targets:
        die("没有找到任何待处理的视频。")

    log(f"共 {len(targets)} 个任务，输出目录: {out_root}")
    ok, fail = 0, 0
    for t in targets:
        try:
            process(t, out_root, args)
            ok += 1
        except SystemExit:
            raise
        except Exception as exc:
            fail += 1
            log(f"[失败] {t}: {type(exc).__name__}: {exc}")

    log(f"\n{'=' * 68}")
    log(f"全部结束: 成功 {ok} / 失败 {fail}")
    if ok:
        log(f"报告位置: {out_root.resolve()}")


if __name__ == "__main__":
    main()
