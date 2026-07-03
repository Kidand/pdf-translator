# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

中英 PDF 翻译器 Web 应用：调用 OpenAI 兼容接口（DeepSeek `deepseek-v4-flash`）翻译 PDF，
在原位置按原样式（字号/字体/颜色/粗斜体/对齐）回填译文，保留图片与矢量图形。
支持「纯译文」与「原文/译文交错」两种输出，前端在线预览与下载。
代码块不翻译、注释翻译。

`DESIGN.md` 是各模块的权威契约（函数签名、字段、路由、错误码）——改接口前先改它。

## 常用命令

```bash
# 环境（Python 3.13 venv，依赖已在 requirements.txt）
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 启动服务（前端由 FastAPI 静态托管，打开 http://localhost:8000）
.venv/bin/uvicorn backend.main:app --port 8000

# 冒烟测试（无 pytest 套件；每个模块的 __main__ 内嵌自测）
.venv/bin/python -m backend.pdf_engine    # 离线：现场生成测试 PDF 走全流程
.venv/bin/python -m backend.translator    # 联网：真实调用 DeepSeek API，需 .env 有 key
.venv/bin/python -m backend.jobs          # 离线：任务管理与后台 Task 生命周期
.venv/bin/python -m backend.config        # 离线：env 解析
```

配置从项目根 `.env` 读取（手动解析，非 python-dotenv），必填 `DEEPSEEK_API_KEY`，
参考 `.env.example`。

## 架构（v2：增量按需翻译）

核心理念：**预览按需翻译（浏览到哪翻到哪 + 预取窗口），只有下载才全量翻译。**
调度器在 `backend/jobs.py` 的 `JobManager._run`：

```
POST /api/translate (main.py)
  → EXTRACTING：PdfEngine.extract_blocks 全部页（秒级）；无块页直接标 done（并落盘原样单页）
  → SERVING：调度循环选页翻译 —— 优先级：焦点窗口 [focus, focus+prefetch_pages) 内最小
    pending 页 → finalize_requested 时全局最小 pending 页 → 无任务则 await asyncio.Event
    每译完一页：merge translations → render_page_pdf 写 <job.dir>/pages/page_<n>.pdf
  → 前端进入预览（不等全量）：右栏按页 GET /page/{n}（202=翻译中→轮询），翻页 POST /focus
  → 用户点下载 → POST /finalize → FINALIZING（逐页补翻，progress=pages_done/page_count）
  → RENDERING：build_output 按 mode 产出 result.pdf → DONE → /download 可用
```

调度器并发注意：唤醒采用「每轮先 event.clear() 再挑页、focus/finalize 时 set」的模式防丢唤醒；
一个 job 同一时刻只翻一页（页内批次由 Translator 内部并发）；私有协调对象
（Event/块缓存/译文累积）放在 `JobManager._runtimes`，绝不能进 `to_dict`。

跨模块约定（务必遵守，破坏会静默出错）：

- `TextBlock` 定义在 `backend/models.py`，所有模块从这里 import；
  `translations` 字典的 key 是 `TextBlock.key`（`"页码:块号"`）。
- **不在 translations 中的块保持原样**：translator 跳过不该翻译的块（页码/URL/纯符号）时
  直接不放入结果 dict，pdf_engine 对缺失 key 不 redact 不回填。空译文同样视为保持原样。
- 翻译批次最终失败时以**原文**作为译文回填，保证长文档流水线不中断。
- `jobs.py` 在 `_run` 内部延迟 import PdfEngine/Translator（保持模块可独立 import/测试）；
  同步 PDF 操作一律用 `asyncio.to_thread` 包装。
- `main.py` 的静态目录挂载（`app.mount("/")`）必须在所有 API 路由之后。
- `page_pdf_path` 只对 done 页返回路径，且该文件必须真实存在（无块页也要落盘原样单页）；
  `/page/{n}` 未译好返回 202、`/file/result` 与 `/download` 未 DONE 返回 409。
- `render_page_pdf` 与 `build_output` 必须共用 `_apply_page_translations`，不许复制回填逻辑。

版式保留的关键实现（`backend/pdf_engine.py`）：

- PyMuPDF 1.28，统一 `import pymupdf`（不要用旧的 `import fitz`）。
- redaction 必须传 `images=PDF_REDACT_IMAGE_NONE, graphics=PDF_REDACT_LINE_ART_NONE`，
  否则图片/矢量线条会被一并删除；redact rect 各边内缩 0.5pt 防误删相邻内容。
- 回填用 `insert_htmlbox(rect, html, scale_low=0.1)`：自动缩小适配 + CJK 字体回退；
  代码块用 `<pre>` 保留换行，普通块 `\n`→`<br>`，译文必须 `html.escape`。
- `_split_side_by_side_lines`：MuPDF 会把同一水平带上相距很远的文字（图示标签、表格
  单元格）合并进一个 block，提取时按行的垂直重叠拆分，否则译文会挤进同一 bbox 错位。
- `_drop_covered_blocks`：真实 PDF 里常有被上层填充/文字遮住的隐藏残留文本（图形编辑
  遗留、叠印页码）。被内容流更靠后的块覆盖超过 55% 的块直接跳过（不翻译、不 redact），
  否则其译文会浮到最上层与可见文字重叠。
- 代码块识别是字体启发式（字体名含 mono/courier/consol/menlo/code 等）。

LLM 调用（`backend/translator.py`）：

- DeepSeek 思考模式经 `extra_body={"thinking": {"type": "enabled"/"disabled"}}` 控制，
  默认关闭（翻译场景更快更省）；只读 `message.content`，忽略 `reasoning_content`。
- `response_format={"type": "json_object"}` 要求 prompt 中出现 "json" 字样（已写在
  system prompt 里，改 prompt 时别删）。
- 重试语义：初次 + 最多 3 次重试（共 4 次调用），退避 2/4/8s。

前端（`frontend/`，原生 JS + 本地 vendor 的 pdf.js，无构建步骤）：

- 预览右栏按页加载：`getTranslatedPageDoc` 请求 `/page/{n-1}`，202 时显示占位并 700ms 重试；
  翻页时 `postFocus` 火后不理地上报浏览位置；「模式」选择只影响下载产物，预览始终逐页对照。
- 下载按钮承担 finalize 流程：点击 → POST /finalize → 按钮内轮询显示页进度 → done 后
  自动跳转 /download。
- CSS 里 `[hidden] { display: none !important; }` 不能删——多个容器用 flex/grid，
  否则 `hidden` 属性会被 display 规则覆盖。
- pdf.js 升级时同步替换 `frontend/vendor/pdf.min.mjs` 与 `pdf.worker.min.mjs` 两个文件。
