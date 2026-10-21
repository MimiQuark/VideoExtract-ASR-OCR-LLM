# VideoExtract | ASR + OCR 视频内容提取

[![CI](https://github.com/MimiQuark/VideoExtract-ASR-OCR-LLM/actions/workflows/ci.yml/badge.svg)](https://github.com/MimiQuark/VideoExtract-ASR-OCR-LLM/actions/workflows/ci.yml)
[![GitHub](https://img.shields.io/badge/GitHub-Repository-181717?logo=github&logoColor=white)](https://github.com/MimiQuark/VideoExtract-ASR-OCR-LLM)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![faster-whisper](https://img.shields.io/badge/ASR-faster--whisper-4B8BBE)](https://github.com/SYSTRAN/faster-whisper)
[![RapidOCR](https://img.shields.io/badge/OCR-RapidOCR-E97627)](https://github.com/RapidAI/RapidOCR)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

把视频变成**可检索、可复制的文字**：语音转写（带时间戳）+ 画面文字 OCR（硬字幕 / PPT）→ 合并成 `report.md`。可选 LLM 结构化总结；字幕校对会用画面文字修正 ASR 错字，并用长度/相似度护栏尽量减少整屏幻灯片灌入。

<p align="center">
  <img src="docs/screenshot.png" width="880" alt="界面截图">
</p>

## 架构

```text
输入（本地文件 / B站、YouTube 链接）
  -> yt-dlp（优先复用现成字幕轨）
  -> ffmpeg 抽音频 / 抽帧
  -> faster-whisper ASR          RapidOCR 画面文字
  -> 字幕校对（OCR 对齐 ASR，带长度/相似度护栏）
  -> report.md + SRT/TXT/JSON + 可选 LLM 总结
  -> CLI 或 FastAPI Web 界面（任务队列 / 进度 / 暂停取消）
```

- `extract.py`：核心流程、CLI、ASR/OCR/校对/报告。
- `server.py`：单工作线程任务队列、真实进度、任务恢复、设置。
- `static/index.html`：单文件 Web 界面。
- `eval_repair.py`：用人工金标评估字幕校对质量。
- `tests/`：纯函数与服务状态测试，不下载 Whisper 模型。

## 快速开始（Windows）

前提：Windows + PowerShell、conda、Python 3.11、网络（首次下载模型）、足够磁盘空间。

```powershell
# 1. 安装 ffmpeg（优先 winget；失败时用 imageio-ffmpeg 兜底）+ conda 环境 + 依赖
powershell -ExecutionPolicy Bypass -File setup.ps1

# 2. 重开终端并激活环境
conda activate videoextract

# 3. 提取本地视频
python extract.py "D:\videos\我的视频.mp4"

# 4. 或启动网页界面
python server.py
# 也可双击 start.bat（需先激活环境，或设置 VIDEOEXTRACT_PY 指向 python.exe）
```

输出在：

```text
output/<视频名>__<来源id>/
├── source.json              # 来源、标题、稳定身份
├── report.md                # 主产出：时间轴 + 画面文字 + 逐字稿 + 可选总结
├── transcript.srt/.txt/.json
├── transcript.repaired.srt/.txt/.json  # 发生校对时
├── screen_text.json         # --ocr 时
├── audio.wav                # 仅在 --keep-audio 时
└── frames/                  # 仅在 --keep-frames 时
```

目录名里的 `<来源id>` 由 URL 或本地文件路径 + 大小/修改时间生成；同名视频不会互相复用转写或 OCR 结果。`--reuse` 也只复用 `source.json` 身份一致的目录。

## 常用命令

```powershell
# 中文教程视频 + 高精度 + 画面 PPT
python extract.py "lecture.mp4" --model large-v3 --lang zh --ocr --ocr-region bottom

# 只有烧录硬字幕、不需要 ASR
python extract.py "talk.mp4" --ocr --no-asr

# 只想要字幕轨，不做 OCR
python extract.py "video.mp4" --no-ocr

# 整目录批量
python extract.py "D:\videos" --model medium

# B站 / YouTube
python extract.py "https://www.bilibili.com/video/BVxxxxxxxx" --lang zh --ocr

# 加 LLM 总结
$env:OPENAI_API_KEY = "sk-..."
python extract.py "lecture.mp4" --summarize
```

关键参数：

| 参数 | 说明 |
|---|---|
| `--model` | `tiny/base/small/medium/large-v3`，默认 `medium` |
| `--lang` | `zh/en/ja`；中文建议显式 `zh` |
| `--ocr` / `--no-ocr` | 开启 / 关闭画面文字识别（默认关闭） |
| `--ocr-region` | `full/bottom/top`，硬字幕推荐 `bottom` |
| `--ocr-max-frames` | 默认 1800；达到上限会在日志中明确警告，后续画面不再识别 |
| `--no-repair` | 关闭 OCR 对 ASR 的校对 |
| `--reuse` | 复用同一来源目录的转写/OCR，只重算报告 |
| `--summarize` / `--summary-model` | LLM 总结；模型为空时使用 `OPENAI_MODEL`，再回退 `gpt-4o-mini` |

## Web 界面

```powershell
python server.py                 # http://127.0.0.1:8000
python server.py --port 8010
```

- 单工作线程：ASR/OCR 是 CPU 密集型，串行执行保证进度不串任务。
- 支持提交、暂停、恢复、取消、下载；服务重启会恢复有 `report.md` 的完成任务。
- 任务时间轴超过 1200 条时会截断，界面会显示“已截断”，不再把部分当全部。
- 设置页可填 API Key / Base URL / 模型；`llm.env.bat` 已 gitignore，且不会再生成含 Key 的 `.bak`。
- **本地/单用户工具**：没有鉴权，绑定 `0.0.0.0` 会把任务提交、任意本地路径处理和结果下载暴露给网络；生产部署前必须加鉴权与路径白名单。

## 评测：字幕校对质量

`eval/repair_gold.jsonl` 是 21 分钟幻灯片视频的 149 条人工标注（文件是 JSON 对象，不是 JSON Lines）。评估口径：

| 指标 | 定义 |
|---|---|
| 无副作用修正率 | (改对 + 等价替换) / 总修正数 |
| 错误引入率 | (改错 + 整行灌入) / 总修正数 |
| 受影响段落占比 | 引入错误的段数 / 全篇段数 |

在 625 段语音、149 处改动上的实测：

```text
无副作用修正率 : 71.8%  (改对 102 + 等价替换 5)
错误引入率     : 28.2%  (改错 23 + 整行灌入 19)
受影响段落占比 : 6.7%   (42/625)
```

复现：

```powershell
python eval_repair.py --out "output\<视频目录>" --dump
python eval_repair.py --out "output\<视频目录>" --review   # 导出 review_*.json 待人工标注
```

评测边界：单视频、单模型配置、人工标注有主观性；这些数字不能直接外推到所有视频。当前校对逻辑会对单 token 整屏 OCR 和超长插入做拒绝，但 OCR 噪声、相邻段复用仍可能造成错误，应结合 `--no-repair` 或抽样复核使用。

## 配置模型

- ASR 模型由 `--model` 指定；首次运行会下载模型到 Hugging Face 缓存。
- LLM 用 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`；可写 `llm.env.bat`（已忽略）或在设置页保存。
- `HF_ENDPOINT`、`HF_HUB_DISABLE_XET` 可写入 `llm.env.bat`，用于镜像/兼容。
- `.env` 与 `llm.env.bat*` 均被 gitignore；不要把 Key 提交。

## 已知局限

- 不做说话人分离；多人抢话、背景音乐、重口音会降低 ASR 质量。
- OCR 只识别画面文字，不理解画面内容；背景花纹可能误识别，可用 `--ocr-min-score` 和高阈值。
- OCR 有帧数上限；长视频超过 `--ocr-max-frames` 后不再识别后续画面（会警告）。
- LLM 总结在超长转写上会按字符预算截断；报告不会明确标注“摘要只覆盖了前一部分”，长视频需要人工注意摘要覆盖范围。
- 字幕校对不是“绝不跨整屏”的保证，而是带护栏的启发式；请用评测/抽样复核。
- 测试与 CI 只覆盖纯函数、解析和 FastAPI 状态；不会在 CI 下载 Whisper 模型或跑完整 ffmpeg/ASR/OCR 链路。
- `examples/` 和 `eval/repair_gold.jsonl` 包含少量 B 站视频的逐字稿/标注节选，仅用于展示格式与评测；版权归原作者，若需下架请联系替换。截图中的本地路径属于演示环境。

## 测试与 CI

```powershell
python -m pytest -q
python -m compileall -q extract.py server.py eval_repair.py
```

15 项测试覆盖 `fmt_ts` 进位、SRT/VTT/GBK 解析、单 token OCR 防灌入、校对插入长度、来源身份、gold 前后文匹配、任务恢复和模型默认值。CI 在 Python 3.11 上安装 `requirements-dev.txt` 后运行这些测试，不下载模型。

## 文件

| 文件 | 作用 |
|---|---|
| `extract.py` | 核心提取/校对/报告 CLI |
| `server.py` | FastAPI 任务队列与设置 |
| `static/index.html` | Web 界面 |
| `eval_repair.py` | 校对质量评估 |
| `setup.ps1` / `start.bat` | Windows 安装与启动 |
| `examples/` | 输出格式节选 |
| `docs/screenshot.png` | 界面截图 |

## License

MIT。见 [LICENSE](LICENSE)。第三方视频文字节选仅作格式展示与评测，版权归原作者。
