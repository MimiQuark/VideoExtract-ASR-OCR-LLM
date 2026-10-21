# ============================================================
#  video-extract 一键环境安装 (Windows / PowerShell)
#  用法:  powershell -ExecutionPolicy Bypass -File setup.ps1
# ============================================================
$ErrorActionPreference = 'Continue'
$envName = 'videoextract'
$pyVer   = '3.11'

function Info($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Good($m) { Write-Host "    [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    [!]  $m" -ForegroundColor Yellow }

Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

# ---------- 1. ffmpeg ----------
Info '检查 ffmpeg'
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Good "已安装: $((Get-Command ffmpeg).Source)"
} else {
    Warn '未安装，尝试用 winget 安装 (Gyan.FFmpeg)...'
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        Good 'ffmpeg 安装成功'
    } else {
        Warn 'ffmpeg 未进入当前 PATH。请关闭并重开终端，或手动把 ffmpeg\bin 加入 PATH。'
        Warn '手动下载: https://www.gyan.dev/ffmpeg/builds/  (选 release-essentials.zip)'
    }
}

# ---------- 2. conda 环境 ----------
Info "准备 Python $pyVer 环境: $envName"
# 注意: 系统 base 是 Python 3.14，faster-whisper/ctranslate2 暂无轮子，必须用 3.11
$envs = (conda env list 2>$null) -join "`n"
if ($envs -match ('(?m)^\s*' + $envName + '\s')) {
    Good "环境已存在: $envName"
} else {
    conda create -n $envName "python=$pyVer" -y
    if ($LASTEXITCODE -ne 0) {
        Warn 'conda 创建失败。可改用官方 Python 3.11 安装包 + venv，再手动执行下面的 pip 命令。'
        exit 1
    }
    Good "环境已创建: $envName"
}

# ---------- 3. Python 依赖 ----------
Info '安装 Python 依赖 (faster-whisper 等，约 300MB)'
$pkgs = @(
    'faster-whisper',
    'yt-dlp',
    'rapidocr-onnxruntime',
    'opencv-python-headless',
    'openai',
    'opencc-python-reimplemented',   # 繁体转简体
    'imageio-ffmpeg',                # 无系统 ffmpeg 时的兜底
    'fastapi',
    'uvicorn[standard]',
    'python-multipart'
)
conda run -n $envName python -m pip install --upgrade pip -q
conda run -n $envName python -m pip install @pkgs
if ($LASTEXITCODE -eq 0) { Good '依赖安装完成' } else { Warn '部分依赖安装失败，请查看上面输出' }

# ---------- 4. 自检 ----------
Info '自检'
conda run -n $envName python -c "import faster_whisper, yt_dlp, rapidocr_onnxruntime, imageio_ffmpeg; print('python deps OK')"
conda run -n $envName python -c "import extract; exe=extract.ffmpeg_path(); print('ffmpeg:', exe); raise SystemExit(0 if exe else 1)"
if ($LASTEXITCODE -ne 0) {
    Warn '没有找到可用的 ffmpeg。请安装 imageio-ffmpeg，或把 ffmpeg 加入 PATH / 放到 tools\ffmpeg.exe。'
    exit 1
}
Good 'ffmpeg OK'

# ---------- 5. 用法提示 ----------
$usage = @(
    '============================================================',
    '安装结束。以后这样用 (PowerShell):',
    '',
    '  conda activate videoextract          # 每次新开终端先激活',
    '',
    '  # 本地视频',
    '  python extract.py "D:\videos\lecture.mp4"',
    '',
    '  # 中文视频 + 高精度 + 画面硬字幕',
    '  python extract.py "D:\videos" --model large-v3 --lang zh --ocr --ocr-region bottom',
    '',
    '  # 在线视频 (B站 / YouTube)',
    '  python extract.py "https://www.bilibili.com/video/BVxxxxxxxx" --lang zh',
    '',
    '  # 加 LLM 结构化总结',
    '  $env:OPENAI_API_KEY = "sk-..."',
    '  python extract.py "lecture.mp4" --summarize',
    '',
    '输出: .\output\<视频名>__<来源id>\report.md',
    '============================================================'
)
$usage | ForEach-Object { Write-Host $_ -ForegroundColor Gray }
