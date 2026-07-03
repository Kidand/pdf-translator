# PDF 中英翻译器 — 架构与模块契约

一个 Web 应用：上传 PDF（可能含图片、可能是长篇小说、可能含代码），调用 OpenAI 兼容接口
（DeepSeek `deepseek-v4-flash`）翻译，**保留原始版式**（字号、位置、颜色、粗斜体、对齐），
支持「纯译文」与「原文/译文交错」两种输出，支持在线预览与下载。
代码块不翻译，但代码中的注释要翻译。

## 技术栈

- 后端：Python 3.13（`.venv/bin/python`）+ FastAPI + PyMuPDF 1.28（`import pymupdf`）+ `openai` SDK
- 前端：原生 HTML/CSS/JS + pdf.js（由主线负责，其他人**不要**改 `frontend/`）
- 启动：`.venv/bin/uvicorn backend.main:app --port 8000`

## 目录结构

```
pdf-translator/
├── backend/
│   ├── __init__.py
│   ├── models.py       # TextBlock 共享数据结构（已写好，勿改）
│   ├── config.py       # 配置（env 读取）
│   ├── pdf_engine.py   # PDF 解析、译文回填、输出组装
│   ├── translator.py   # LLM 翻译（批处理、并发、代码规则）
│   ├── jobs.py         # 任务管理（内存 store + asyncio 后台任务）
│   └── main.py         # FastAPI 路由 + 静态托管
├── frontend/           # index.html / app.css / app.js / vendor(pdf.js)
├── data/jobs/<job_id>/ # source.pdf、result.pdf（运行时生成）
├── requirements.txt
└── .env                # DEEPSEEK_API_KEY 等（已 gitignore）
```

## 共享数据结构（严格遵守）

```python
# backend/models.py 中已定义好（勿改），所有模块统一
# `from backend.models import TextBlock`
from dataclasses import dataclass

@dataclass
class TextBlock:
    page_index: int      # 0-based 页码
    block_id: int        # 页内 0-based 序号
    bbox: tuple          # (x0, y0, x1, y1) PDF 坐标
    text: str            # 原文，块内多行以 "\n" 连接
    font_size: float     # 主字号（按字符数取众数）
    font_name: str       # 主字体名
    color: str           # "#rrggbb"
    bold: bool
    italic: bool
    is_code: bool        # 等宽字体启发式（字体名含 mono/courier/consol/menlo/code 等，不区分大小写）
    align: str           # "left" | "center" | "right"（启发式：行相对 bbox 的位置）
    line_count: int

    @property
    def key(self) -> str:
        return f"{self.page_index}:{self.block_id}"
```

**translations 字典**：`dict[str, str]`，key 为 `TextBlock.key`（`"页码:块号"`），value 为译文。
约定：translator 认为无需翻译的块（纯数字页码、空白、URL、纯符号等）**不放入** dict；
pdf_engine 对不在 dict 中的块**保持原样**（不做 redact 也不回填）。

## config.py 契约

```python
from dataclasses import dataclass

@dataclass
class Settings:
    api_key: str            # env DEEPSEEK_API_KEY
    base_url: str           # env DEEPSEEK_BASE_URL, 默认 "https://api.deepseek.com"
    model: str              # env DEEPSEEK_MODEL, 默认 "deepseek-v4-flash"
    thinking_enabled: bool  # env THINKING_ENABLED, 默认 False（可被每次请求覆盖）
    concurrency: int        # env TRANSLATE_CONCURRENCY, 默认 6（并发 LLM 请求数）
    batch_char_budget: int  # env BATCH_CHAR_BUDGET, 默认 2200（每批原文字符预算）
    data_dir: str           # env DATA_DIR, 默认 "<项目根>/data"
    request_timeout: float  # env LLM_TIMEOUT, 默认 300.0

def get_settings() -> Settings: ...   # 读取 .env（手动解析或 os.environ，勿引入新依赖）
```

`.env` 手动解析：若存在项目根 `.env` 文件，逐行解析 `KEY=VALUE`（跳过 `#` 注释），
仅当 os.environ 中不存在该 key 时注入。

## pdf_engine.py 契约

```python
class PdfEngine:
    def __init__(self, src_path): ...          # 打开 PDF；页数存 self.page_count
    def extract_blocks(self) -> list[TextBlock]: ...
    def build_output(self, translations: dict[str, str],
                     mode: str, out_path: str) -> None: ...
        # mode: "translated"（纯译文） | "interleaved"（原文页、译文页交替）
    def close(self): ...
```

实现要求（PyMuPDF 1.28，`import pymupdf`）：

1. **提取**：`page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT)`，按 block 聚合
   （type==0 的文本块）；每块的 text 由 lines/spans 拼接，行间 `"\n"`；
   主字号/字体/颜色按 span 字符数加权取众数；`color` int 转 `"#rrggbb"`。
   bold/italic 从 span flags 位判断（bit 4 = bold(16), bit 1 = italic(2)）。
2. **译文回填**（在源文档副本上操作，用 `pymupdf.open(src_path)` 重新打开）：
   - 对每个有译文的块：`page.add_redact_annot(rect)`，其中 rect 取 bbox 各边向内收缩 0.5pt，
     避免误删相邻内容；每页统一
     `page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)`
     —— 必须保住图片与矢量线条。
   - 回填用 `page.insert_htmlbox(rect, html, scale_low=0.1)`：rect 用**原始 bbox**；
     html 用 `<div style="...">`，style 含 `font-size:{pt}pt; color:{color};
     text-align:{align}; line-height:<数据驱动>`（按块 bbox 高/行数/字号推算原始行距，
     clamp 到 [1.0, 1.3]，避免写死行高导致 insert_htmlbox 无谓缩小），
     bold → `font-weight:bold`，italic → `font-style:italic`；
     `is_code` 的块外层用 `<pre style="font-family:monospace; white-space:pre-wrap; ...">`。
   - 译文文本必须做 HTML 转义（`html.escape`），换行 `\n` → `<br>`（pre 块内保留 `\n` 原样）。
   - insert_htmlbox 自动处理 CJK 字体 fallback 与缩小适配（scale_low=0.1 允许缩到 10%）。
   - 若 insert_htmlbox 返回负值（放不下），忽略即可（已尽力缩小）。
3. **输出组装**：
   - `"translated"`：直接保存回填后的文档。
   - `"interleaved"`：新建 `pymupdf.open()`，对每页 i：先 `insert_pdf(原始doc, from_page=i, to_page=i)`
     再 `insert_pdf(译文doc, from_page=i, to_page=i)`。
   - 保存用 `doc.save(out_path, garbage=3, deflate=True)`。
4. 模块底部写 `if __name__ == "__main__":` 冒烟测试：用 pymupdf 现场生成一个
   含普通段落 + 代码块（Courier 字体）+ 矩形图形的 3 页测试 PDF 到
   `/tmp/pdfeng_test.pdf`，走完 extract → 伪造 translations → 两种 mode 输出，
   断言输出页数正确、译文文本可被重新提取到。

## translator.py 契约

```python
class Translator:
    def __init__(self, settings: Settings, thinking: bool | None = None): ...
        # thinking=None 时用 settings.thinking_enabled
    async def translate_blocks(
        self, blocks: list[TextBlock], direction: str,
        progress_cb=None,          # Callable[[int, int], None]：(已完成块数, 总块数)，可为 None
    ) -> dict[str, str]: ...
```

实现要求：

1. 用 `openai.AsyncOpenAI(api_key=..., base_url=settings.base_url, timeout=...)`。
   `extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}}`。
   读结果只取 `choices[0].message.content`（思考内容在 reasoning_content，忽略）。
2. **direction**：`"auto" | "en2zh" | "zh2en"`。auto 时按块内容判断：CJK 字符占比 >30% → zh2en，
   否则 en2zh（逐批判断，以批内多数为准）。
3. **跳过规则**：文本去空白后为空、无任何字母/CJK（纯数字符号页码等）、或是纯 URL → 不送翻译、
   不进结果 dict。
4. **批处理**：按 `batch_char_budget` 字符预算把块分批（单块超预算则独占一批），
   同批用编号 JSON 发送：
   - system prompt 固定为专业文档翻译指令，要点：只输出 JSON；保留数字/专有名词/格式；
     **`is_code` 为 true 的条目是代码：代码本身（标识符、关键字、字符串值、语法）原样保留，
     仅翻译其中的注释（`//`、`#`、`/* */`、`"""docstring"""` 等）**；普通条目若内部含少量
     行内代码/命令也保持原样。
   - user 消息为 JSON：`{"target_lang": "Chinese"|"English", "items": [{"id": "0:3", "code": false, "text": "..."}]}`
   - 要求模型返回：`{"translations": {"0:3": "译文", ...}}`；请求加
     `response_format={"type": "json_object"}`。
5. **健壮性**：解析失败或缺 id → 该批重试（最多 3 次，指数退避 2s/4s/8s，429/5xx 同样重试）；
   最终失败的批：其块以**原文**作为译文放入结果（保证流水线不断），并 log warning。
   返回的译文若为空字符串则回退原文。
6. **并发**：`asyncio.Semaphore(settings.concurrency)` 包住每批请求，`asyncio.gather` 全部批次；
   每完成一批调用 `progress_cb(done_blocks, total_blocks)`（total 为送翻译的块数）。
7. 模块底部 `if __name__ == "__main__":` 冒烟测试：构造 3 个假 TextBlock（普通英文、
   含 `# comment` 的 Python 代码块、纯数字页码），真实调用 API（settings 从 config 读），
   打印结果，断言：页码块被跳过、代码块中 `def`/标识符保留、注释变中文。

## 增量按需翻译（v2 架构，当前实现目标）

> v2 取代了「上传后一次性全量翻译」的 v1 流程。核心思想：
> **预览按需翻译（浏览到哪翻到哪 + 预取窗口），只有下载才全量翻译。**

生命周期：`extracting →（提取完成，秒级）→ serving →（用户点下载 finalize）→ finalizing → rendering → done`

- **extracting**：提取全部页的 TextBlock，按页分组缓存；无可译块的页直接标 done。
- **serving**：调度循环反复选出「下一个要翻译的页」：
  1. 焦点窗口内最小的 pending 页：`[focus_page, focus_page + prefetch_pages)`；
  2. 若 finalize_requested：全文档最小的 pending 页；
  3. 都没有：await 唤醒事件（asyncio.Event，focus/finalize 时 set）。
  翻译该页块（复用 Translator.translate_blocks，逐页调用）→ merge 进 job.translations →
  用 PdfEngine.render_page_pdf 渲染单页译文 PDF 写到 `<job.dir>/pages/page_<n>.pdf` →
  `page_status[n]="done"`、`pages_done += 1`。
- **finalizing**：finalize_requested=True 后所有页按序补翻，progress = pages_done/page_count。
- 全部页 done 且 finalize_requested → **rendering**（build_output 按 mode 产出 result.pdf）→ **done**。

### pdf_engine.py 增补契约

```python
class PdfEngine:
    def render_page_pdf(self, page_index: int, translations: dict[str, str]) -> bytes:
        """单页译文 PDF 字节流：新建文档 insert_pdf 源文档该页 → 按缓存块中该页的
        translations 做 redact+回填（与 build_output 完全复用同一套私有回填方法）→
        tobytes(garbage=3, deflate=True)。该页无可译块或无译文时返回原样单页。"""
```

### jobs.py v2 契约

```python
class JobPhase:  # 字符串常量
    EXTRACTING="extracting"; SERVING="serving"; FINALIZING="finalizing"
    RENDERING="rendering"; DONE="done"; ERROR="error"

@dataclass
class Job:
    # v1 字段全部保留（id/filename/mode/direction/thinking/progress/page_count/
    # total_blocks/done_blocks/error/created_at/dir）；status 字段语义改为 phase 值
    status: str                 # JobPhase.*
    page_status: list[str]      # 每页 "pending" | "translating" | "done"
    pages_done: int
    focus_page: int             # 前端最近上报的浏览页（0-based），默认 0
    finalize_requested: bool

class JobManager:
    def create(...) -> Job                       # 签名不变；创建后进入 extracting→serving
    def get / list / to_dict                     # to_dict 增加 page_status/pages_done/
                                                 # focus_page/finalize_requested
    def focus(self, job_id: str, page_index: int) -> bool
        # 更新 focus_page 并唤醒调度器；job 不存在返回 False
    def finalize(self, job_id: str) -> bool
        # 置 finalize_requested 并唤醒（幂等）；done/finalizing 状态下也返回 True
    def page_pdf_path(self, job_id: str, page_index: int) -> str | None
        # 该页已译好 → 返回单页 PDF 路径；未译好 → 返回 None，并把 focus 提示到该页
```

- 调度循环内翻译与渲染依旧 asyncio.to_thread 包装同步 PDF 操作；
  一个 job 同一时刻只翻译一页（页内批次由 Translator 内部并发）。
- progress 语义：extracting=0.02；serving = pages_done/page_count（仅展示）；
  finalizing = pages_done/page_count；rendering=0.97；done=1.0。
- Translator 实例每 job 创建一次复用；direction=auto 由 translate_blocks 每批自判（天然支持逐页）。

### main.py 增补路由

```python
GET  /api/jobs/{job_id}/page/{page_index}
  # 该页已译好 → FileResponse 单页 PDF（inline, application/pdf）
  # 未译好   → 202 JSON {"status": "<page_status>"}（同时等效 focus 提示）
  # job 不存在或页码越界 → 404
POST /api/jobs/{job_id}/focus      # body {"page": int} → {"ok": true}；越界夹到合法范围
POST /api/jobs/{job_id}/finalize   # → {"ok": true}（幂等触发全量翻译）
# /file/result 与 /download 仍要求 status == "done"，否则 409（不变）
```

### config.py 增补

```python
prefetch_pages: int   # env PREFETCH_PAGES, 默认 3（焦点页向后预取的页数窗口）
```

## jobs.py 契约（v1，历史参考；与上方 v2 章节冲突处以 v2 为准）

```python
class JobStatus:   # 字符串常量
    QUEUED="queued"; EXTRACTING="extracting"; TRANSLATING="translating"
    RENDERING="rendering"; DONE="done"; ERROR="error"

@dataclass
class Job:
    id: str                # uuid4 hex
    filename: str          # 原始文件名
    mode: str              # "translated" | "interleaved"
    direction: str
    thinking: bool
    status: str            # JobStatus.*
    progress: float        # 0.0~1.0（translating 阶段 = done/total，其他阶段给固定值）
    page_count: int
    total_blocks: int
    done_blocks: int
    error: str | None
    created_at: float
    dir: str               # data/jobs/<id>

class JobManager:
    def __init__(self, settings): ...
    def create(self, file_bytes: bytes, filename, mode, direction, thinking) -> Job:
        # 写 source.pdf 到 job dir，状态 QUEUED，然后 asyncio.create_task 跑 _run(job)
    def get(self, job_id) -> Job | None: ...
    def list(self) -> list[Job]: ...          # 按 created_at 倒序
    async def _run(self, job): ...
        # EXTRACTING → PdfEngine.extract_blocks
        # TRANSLATING → Translator.translate_blocks(progress_cb 更新 job)
        # RENDERING → build_output(mode) 写 result.pdf
        # DONE；任何异常 → ERROR + error 消息，日志打印 traceback
    @staticmethod
    def to_dict(job) -> dict: ...             # 全部字段，供 API 返回

# PdfEngine.extract_blocks / build_output 是同步 CPU 操作，
# 在 _run 中用 asyncio.to_thread 包装，避免阻塞事件循环。
# 注意：PdfEngine 与 Translator 在 _run 内部延迟 import（函数级 import），
# 保证 jobs.py 模块本身可独立 import（便于并行开发与测试）。
```

## main.py 路由契约

```python
app = FastAPI()
# 启动时创建全局 JobManager（lifespan 或模块级）

POST /api/translate
  # multipart form: file (UploadFile, 必须 .pdf), mode, direction, thinking ("true"/"false")
  # → 201 {"job_id": "..."}；非 PDF → 400 {"detail": ...}；文件 >80MB → 413

GET /api/jobs/{job_id}          # → JobManager.to_dict(job)；不存在 → 404
GET /api/jobs                   # → {"jobs": [...]}
GET /api/jobs/{job_id}/file/original   # FileResponse source.pdf, inline, media_type application/pdf
GET /api/jobs/{job_id}/file/result     # FileResponse result.pdf, inline；job 未 DONE → 409
GET /api/jobs/{job_id}/download        # result.pdf, Content-Disposition attachment,
                                       # 文件名 "<原名去.pdf>_translated.pdf"（RFC5987 处理非 ASCII）
GET /api/health                 # {"status":"ok","model":settings.model}

# 最后挂载静态：app.mount("/", StaticFiles(directory="frontend", html=True))
# CORS：allow_origins ["*"]（本地工具）
```

## 前端（主线负责，本契约仅供后端理解交互）

上传 → `POST /api/translate` → 轮询 `GET /api/jobs/{id}`（800ms）→ DONE 后
pdf.js 渲染 `/api/jobs/{id}/file/result` 预览（对照视图同时取 `file/original`），下载按钮走 `/download`。

## 通用约定

- 全部文件 UTF-8，Python 代码带类型标注，`logging` 模块打日志（logger 名 = 模块名）。
- 不引入契约之外的第三方依赖（已装：pymupdf, fastapi, uvicorn, openai, python-multipart, httpx）。
- 各模块只写自己负责的文件，不改别人的文件。
