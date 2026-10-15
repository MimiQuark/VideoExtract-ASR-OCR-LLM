@echo off
rem 复制本文件为 llm.env.bat，按需填写（llm.env.bat 已被 .gitignore 忽略）

rem ---- LLM 总结（可选，配了才会生成智能总结）----
set OPENAI_API_KEY=sk-替换成你自己的Key
set OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
set OPENAI_MODEL=glm-4-flash

rem ---- HuggingFace 模型下载（国内必备，否则首次转写会超时）----
rem 镜像不代理新版 Xet 存储，必须同时禁用 Xet，两个都要设，缺一不可。
set HF_ENDPOINT=https://hf-mirror.com
set HF_HUB_DISABLE_XET=1
