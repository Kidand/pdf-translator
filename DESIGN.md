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

## v3 增补契约（跨页上下文 / 哈希缓存与重译 / 模型覆盖 / 预览增强）

> v3 在 v2 增量按需翻译之上增补四组能力，接口以本章为准。

### translator.py：跨页・跨批上下文

```python
async def translate_blocks(
    self, blocks, direction, progress_cb=None,
    context_before: str = "", context_after: str = "",
) -> dict[str, str]
```

- `context_before`/`context_after` 是紧邻本页之前/之后的**原文片段**（调用方截好，≤400 字符），
  仅供模型理解跨页断句与指代；不翻译、不出现在返回 dict。
- translator 分批后，每批的实际上下文 = 页级上下文与同页相邻批原文的拼接
  （批 i 的 before = context_before + 批 i 之前各块原文的**尾部**；after = 批 i 之后
  各块原文的**头部** + context_after；各自截到 ≤400 字符）。
- user payload 增加 `"context_before"`/`"context_after"` 字段（空串时可省略）；
  system prompt 增加规则：context 字段仅供理解语境，严禁翻译或输出（"json" 字样不许删）。
- 跳过规则提为模块级函数 `should_skip_text(text) -> bool`（`_should_skip` 委托它），
  供 jobs 判断「页内送翻块」复用。

### jobs.py：页级上下文来源

- `_translate_and_render_page` 从 `runtime.blocks_by_page` 取**上一页最后一段正文**的尾部
  与**下一页第一段正文**的头部（各 ≤400 字符）传入 translate_blocks。送翻与否用
  `should_skip_text` 判断。

### v3.1 跨页上下文选块修正（页眉/页脚鲁棒）

> 缺陷（实测确证）：原实现取「上一页最后一个 / 下一页第一个」送翻块，块按 block_id
> （提取顺序）排，页眉/页码/页脚常排在正文块之前或之后 → 取到页眉当上下文。例：
> 某书第 18 页块序为 `acknowledgments`(页眉)、`xvii`(页码)、正文「Vitanis for their…」；
> 上一页致谢以「…and Viton」结尾，「Viton Vitanis」是被跨页拆开的同一人名。原实现给
> 第 17 页翻译传的 context_after 是页眉「acknowledgments」而非续句「Vitanis…」，跨页
> 线索被打断，译文把 Vitanis 当独立主语（"感谢 Vitanis…"），与不带上下文几乎无差别。

- 边界块按**阅读顺序（bbox 坐标）而非 block_id**选，并跳过页眉/页脚：
  - context_after（下一页开头）= 下一页送翻块中按 `y0` 升序、跳过「又短又贴顶」的
    页眉/页码块后的**第一段正文**头部；
  - context_before（上一页结尾）= 上一页送翻块中按 `y1` 降序、跳过「又短又贴底」的
    页脚/页码块后的**最后一段正文**尾部。
  - 页眉/页脚判定为启发式（如：文本很短 且 y0 在页面顶部带 / y1 在底部带；页高由该页
    全部块的 y 包络估计，避免依赖页尺寸 API）。判不准时宁可**保留**该块当上下文
    （多带无害），不可漏掉真正的边界正文块。
- translator system prompt 增强：明确 context_before 的结尾/ context_after 的开头**可能与
  待翻译文本是同一句被跨页或跨栏截断的两半**，翻译时保持人名、专有名词、句子结构的连贯，
  不得把续接的半句当作独立主语另起一句（"json" 字样与既有规则不许删）。
- 验收：对上述书第 17/18 页，第 17 页翻译的 context_after 应为正文「Vitanis…」而非页眉；
  第 18 页 Vitanis 块译文应体现「Viton Vitanis」为同一人名的连贯（不再"感谢 Vitanis…"独立成句）。

### v3.1 重译按钮可用态（前端）

> 缺陷（实测确证）：finalizing/rendering 期间点「重译本页/全部重译」，后端按契约返回
> 409 busy（数据竞态保护，正确），但前端 `alert("重译失败…")` 弹错，体验为"报错"。

- 前端在 job.status ∈ {finalizing, rendering} 时**禁用**重译按钮（置灰 + tooltip 说明
  "正在生成完整译本，暂不可重译"），不让用户点了才弹错；
- 即便如此仍收到 409（时序竞态）时，用**温和 inline 提示**（如按钮旁短暂文案）替代
  `alert`，不打断浏览。

### v3.1 预览缩放平移与双栏同步（前端）

- 缩放 > 适宽（画布溢出容器）时，`.canvas-wrap` 内支持**鼠标拖拽平移**（按下拖动移动
  scrollLeft/scrollTop，`cursor: grab`/`grabbing`；不吞掉点击目录/按钮的正常交互）；
  触控与滚轮滚动保持可用。
- 对照预览（compare）新增**双栏滚动同步开关**（工具栏 checkbox，默认开）：拖动/滚动一栏
  时另一栏同步到相同归一化位置（按 scrollLeft/scrollWidth、scrollTop/scrollHeight 比例
  映射，避免两栏尺寸不同导致错位）；用「正在同步方」标志防止双向滚动事件递归抖动。
  单栏（仅译文）视图隐藏该开关。

### 内容哈希缓存（data/cache）

- `POST /api/translate` 对上传字节算 sha256；`Job` 增加公开字段 `file_sha256: str`
  与 `cache_hit: bool`（均进 to_dict；to_dict 断言测试同步更新）。
- 缓存文件：`data/cache/<sha256>/<direction>__<model规范名>.json`，
  模型名中非 `[A-Za-z0-9._-]` 的字符替换为 `_`；内容
  `{"version": 1, "model": ..., "direction": ..., "translations": {key: 译文}}`。
- form 新增 `use_cache`（默认 "true"）：命中时把缓存 translations 预载进
  `runtime.translations`（cache_hit=True）。调度循环选中某页时，若该页**所有送翻块**
  的 key 均已有译文 → 跳过 LLM 直接渲染标 done（不预渲染全部命中页，浏览到哪渲到哪）。
- 每页翻完（确有新增译文时）将全量 translations 中该 (direction, model) 的条目 merge
  写回缓存：`asyncio.to_thread` + 临时文件 + `os.replace` 原子替换；JobManager 持有
  按缓存路径分锁的 `asyncio.Lock` 串行化读-改-写（同一文件的并发 job 不互相丢写）。
- direction=auto 的缓存按 `"auto"` 记（同一文档同一方向选项才互相命中）。

### 重译

```
POST /api/jobs/{job_id}/retranslate
  body {"scope": "all"} | {"scope": "page", "page": <0-based int>}
  → {"ok": true}；job 不存在 404；extracting 阶段（页表未就绪）409；page 越界 400
```

- 语义：删除对应页（或全部页）在 `runtime.translations` 中的 key、`page_status` 置回
  pending、`pages_done` 重算、这些页加入 `runtime.force_pages`（选页时跳过「缓存已覆盖」
  判断，强制走 LLM；译完移除）、focus 移到重译页（scope=page）或 0、唤醒调度器。
- job 已 done/error：状态拨回 serving、`finalize_requested=False`、error=None、progress
  重算，并重启调度任务——`_run` 支持恢复模式：engine 已关则重开；`blocks_by_page` 为空
  则重提取（沿用既有 page_status，不重置已 done 页）；`result.pdf` 保持旧文件直到下次
  finalize 覆盖（/download 依旧只在 done 时可用）。
- 重译完成的页照常 merge 写回缓存（新译文覆盖旧条目）。

### 模型覆盖（OpenAI 兼容接口均可用）

- `POST /api/translate` 新增可选 form 字段：`model`、`base_url`、`api_key`
  （空串/缺省 = 用 `.env` 默认值）。
- `Job` 增加公开字段 `model: str`（生效模型名，进 to_dict）；`base_url`/`api_key` 的
  override 存 `_JobRuntime`，**绝不进 to_dict、绝不写日志**。
- `Translator.__init__` 增加可选参数 `model/base_url/api_key`（None → settings 值）。
- 缓存文件名含模型规范名（见上），换模型自动分开缓存，互不污染。
- 前端「设置」面板（topbar ⚙，localStorage 持久化）：base_url / api_key / model 三项，
  留空用服务端默认；上传时随表单提交；api_key 输入框 type=password，并提示仅存本机浏览器。

### 前端（v3）

- **目录侧栏**：预览区左侧，`originalDoc.getOutline()`（pdf.js）渲染可折叠树；dest →
  页码用 `getDestination`/`getPageIndex` 惰性解析；点击跳页；无目录显示空态；侧栏可整体
  折叠（工具栏按钮）。
- **缩放**：`state.zoom ∈ {null(适应宽度), 0.5 … 3.0}`；工具栏 −/百分比/+/适宽四控件；
  左右两栏同步缩放；canvas 容器 overflow:auto 可平移。
- **取页错误语义**：仅 HTTP 409 视为任务失败（showError 退出预览）；fetch 拒绝、解析
  失败等瞬态异常一律按「未译好」处理——占位 + 指数退避重试（700ms 起、上限 5s）+
  console.warn。**这是 #连续翻页崩溃 的修复核心，不许回退。**
- **并发纪律**：同页取页请求 in-flight 去重（Map<页, Promise>）；postFocus 150ms 节流
  只报最终位置；pdf.js 渲染 cancel 后必须 await 旧任务结束再启动新渲染（吞
  RenderingCancelledException）；页缓存 LRU 驱逐永不销毁当前页/正在渲染页的文档。
- **重译入口**：工具栏「重译本页」按钮 + 「全部重译」（带 confirm）；命中缓存时预览区
  给出一次性提示（如 chip「已载入历史译文」）。

## v3 增补契约·二（任务持久化 / 运行时全局配置）

> 与上一章节互补：上一章节的 data/cache 内容哈希缓存、重译、per-job 模型覆盖为准；
> 本章节补充任务持久化与**全局默认**模型配置（per-job 覆盖优先于全局默认）。

### 1) 任务持久化（jobs.py）

使重启不丢任务、data/cache 缓存跨重启可用：

- job 目录写 `meta.json`：Job 全部可序列化字段 + `file_sha256`（每次状态/页进度
  变化时重写，to_thread + 原子写：先写 .tmp 再 os.replace）。
- 每页译完把累计 translations 重写 `translations.json`（同样原子写）。
- `JobManager.__init__` 启动时扫描 data/jobs/*/meta.json rehydrate 内存 _jobs
  （在 retention 清理之后）；非终态 job 修正为 SERVING，finalize_requested 保留。
- rehydrate 的 job **惰性恢复调度**：任何 focus/finalize/page_pdf_path 触及且存在
  pending 页时，若该 job 无运行中的调度任务则启动 _run；_run 需支持中途恢复
  （重新 extract 幂等重建块缓存与 key，加载 translations.json，跳过已 done 页；
  已 done 页的单页 PDF 若缺失则用已载译文重渲染，不重新翻译）。

### 2) 运行时全局配置（config.py + main.py，OpenAI 兼容自定义）

```python
# config.py
def apply_updates(updates: dict) -> Settings:
    # 白名单键：api_key / base_url / model / thinking_enabled / concurrency
    # 校验（非空字符串、int>=1、bool），更新进程内全局 settings 单例，
    # 并持久化写回项目根 .env（读现有行、替换同名 KEY 或追加；原子写）
```

```
GET /api/config  → {"base_url", "model", "thinking_enabled", "concurrency",
                    "api_key_masked": "sk-****last4"}   # 绝不回传完整 key
PUT /api/config  body JSON，键同白名单；api_key 缺省或空串=不修改；
                 校验失败 → 422 {"detail": ...}；成功 → 同 GET 的响应
```

- main.py 与 JobManager 共享同一 Settings 实例；PUT 后调用
  `job_manager.reconfigure(settings)`：重建全局 LLM Semaphore（新并发值），
  在途 job 沿用旧 Translator，不受影响；新建 job 使用新配置。

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
