# PDF 中英翻译器

一个功能强大的在线 PDF 文档翻译器，支持中英双向翻译，完整保留原始版式，支持多种输出模式。

## 项目简介

**PDF 中英翻译器** 是一款 Web 应用，专门设计用于翻译 PDF 文档。它采用 OpenAI 兼容接口（DeepSeek `deepseek-v4-flash`）进行高质量翻译，同时在翻译过程中完全保留原始文档的版式（字号、颜色、位置、粗斜体、对齐方式等）。

## 核心特性

- **增量按需翻译**
  - 上传后秒级完成版式解析，立即进入在线预览，无需等待全文翻译
  - 预览时只翻译「当前浏览页 + 预取窗口」，翻到哪、译到哪，长篇文档不浪费 token
  - 点击下载时才全量翻译剩余页面，并实时显示生成进度

- **双语翻译模式**（影响下载的文件）
  - 纯译文模式（translated）：输出完整的译文版本
  - 原文/译文交错模式（interleaved）：原始页和译文页交替呈现，便于对照学习

- **完整的版式保留**
  - 保留文本的字号、字体、颜色、位置
  - 保留文本风格（粗体、斜体、对齐方式）
  - 保留页面中的图片和矢量线条

- **智能翻译规则**
  - 代码块自动识别（基于字体启发式），**代码本身原样保留**，仅翻译代码内的注释
  - 自动跳过无需翻译的内容（纯数字页码、空白、URL、纯符号等）
  - 支持 Python、JavaScript 等多语言代码注释翻译（`//`、`#`、`/* */`、`"""docstring"""` 等）

- **在线预览与下载**
  - 实时预览翻译结果（使用 PDF.js 渲染）
  - 同时查看原始 PDF 和译文 PDF
  - 下载翻译后的 PDF 文件

- **强大的翻译能力**
  - 支持自动检测文档语言方向（中→英 or 英→中）
  - 可选思考模式（思考模式更慢更贵，但翻译质量更高）
  - 智能批处理和并发请求，提升翻译速度

## 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| 后端 | Python + FastAPI | 3.13 + FastAPI |
| PDF 处理 | PyMuPDF | 1.28 |
| LLM 接口 | OpenAI SDK | 兼容 DeepSeek |
| 前端 | 原生 HTML/CSS/JS + PDF.js | - |
| 服务器 | Uvicorn | - |

## 快速开始

### 1. 环境准备

```bash
# 进入项目根目录
cd pdf-translator

# 创建虚拟环境
python3.13 -m venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
.venv/bin/pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
# 复制配置文件模板
cp .env.example .env

# 编辑 .env，填入你的 DeepSeek API Key
# 使用你的编辑器打开 .env 文件，填入以下内容：
# DEEPSEEK_API_KEY=sk-your-api-key-here
```

### 3. 启动服务

```bash
# 启动服务（开发时可加 --reload）
.venv/bin/uvicorn backend.main:app --port 8000
```

### 4. 访问应用

在浏览器中打开：

```
http://localhost:8000
```

即可看到上传 PDF 并开始翻译。

## 配置项说明

在 `.env` 文件中配置以下项：

| 配置项 | 环境变量 | 默认值 | 说明 |
|-------|---------|--------|------|
| API Key | `DEEPSEEK_API_KEY` | - | 必填。DeepSeek API 密钥，从 [DeepSeek 官网](https://platform.deepseek.com) 获取 |
| API 地址 | `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API 服务地址 |
| 模型名称 | `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 使用的模型名称 |
| 思考模式 | `THINKING_ENABLED` | `false` | 是否启用思考模式（更准但更慢更贵，建议默认关闭） |
| 并发数 | `TRANSLATE_CONCURRENCY` | `6` | 并发 LLM 请求数（建议 6～10） |
| 字符预算 | `BATCH_CHAR_BUDGET` | `2200` | 每批翻译的字符预算（避免单次请求过大） |
| 数据目录 | `DATA_DIR` | `<项目根>/data` | 存储翻译任务和临时文件的目录 |
| 超时时间 | `LLM_TIMEOUT` | `300.0` | LLM 请求超时时间（秒） |

### 配置示例

```ini
# .env 文件示例
DEEPSEEK_API_KEY=sk-your-actual-api-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
THINKING_ENABLED=false
TRANSLATE_CONCURRENCY=6
BATCH_CHAR_BUDGET=2200
DATA_DIR=./data
LLM_TIMEOUT=300.0
```

## API 接口一览

### 翻译 API

**请求翻译任务**

```
POST /api/translate
```

**参数**（multipart/form-data）：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | File | 是 | PDF 文件（必须为 .pdf 后缀） |
| `mode` | string | 是 | 输出模式：`translated`（纯译文） 或 `interleaved`（交错对照） |
| `direction` | string | 是 | 翻译方向：`auto`（自动检测）、`en2zh`（英→中）、`zh2en`（中→英） |
| `thinking` | string | 否 | 是否启用思考模式：`"true"` 或 `"false"`（默认 `"false"`） |

**响应（成功）**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**响应（错误）**

- `400 Bad Request`：非 PDF 文件或参数错误
- `413 Payload Too Large`：文件大于 80MB

### 任务查询 API

**获取单个任务状态**

```
GET /api/jobs/{job_id}
```

**响应示例**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "filename": "document.pdf",
  "mode": "translated",
  "direction": "en2zh",
  "thinking": false,
  "status": "done",
  "progress": 1.0,
  "page_count": 10,
  "total_blocks": 156,
  "done_blocks": 156,
  "error": null,
  "created_at": 1688000000.123,
  "dir": "data/jobs/550e8400-e29b-41d4-a716-446655440000"
}
```

**任务状态说明**

| 状态 | 说明 |
|------|------|
| `extracting` | 正在解析版式、提取文本块（秒级） |
| `serving` | 可预览，按浏览位置增量翻译（page_status 逐页给出 pending/translating/done） |
| `finalizing` | 已请求下载，正在全量补翻剩余页面 |
| `rendering` | 正在按输出模式合成最终 PDF |
| `done` | 完成（progress = 1.0，可下载） |
| `error` | 出错（error 字段有详情） |

### 增量翻译 API

```
GET  /api/jobs/{job_id}/page/{page_index}   # 单页译文 PDF；未译好返回 202 {"status": ...}
POST /api/jobs/{job_id}/focus               # body {"page": 3}，上报浏览位置以优先翻译附近页
POST /api/jobs/{job_id}/finalize            # 触发全量翻译（幂等），进度看任务状态接口
```

**获取所有任务**

```
GET /api/jobs
```

**响应示例**

```json
{
  "jobs": [
    { /* job 对象 */ },
    { /* job 对象 */ }
  ]
}
```

### 文件下载 API

**获取原始 PDF**

```
GET /api/jobs/{job_id}/file/original
```

返回原始 PDF 文件（inline 显示）。

**获取翻译后 PDF（预览）**

```
GET /api/jobs/{job_id}/file/result
```

返回翻译后的 PDF 文件（inline 显示），用于在线预览。
- 若任务未完成（status != `done`），返回 `409 Conflict`

**下载翻译后 PDF（附件）**

```
GET /api/jobs/{job_id}/download
```

下载翻译后的 PDF 文件，文件名格式：`<原名去.pdf>_translated.pdf`（如 `document_translated.pdf`）。

### 健康检查 API

**服务状态检查**

```
GET /api/health
```

**响应示例**

```json
{
  "status": "ok",
  "model": "deepseek-v4-flash"
}
```

## 工作流程

1. **上传 PDF**：选择文件、配置方向与下载模式，`POST /api/translate` 返回 `job_id`
2. **秒级进预览**：版式解析完成（`serving`）即打开对照预览，右栏按页请求译文
3. **浏览驱动翻译**：翻页时前端上报 `focus`，后端优先翻译「当前页 + 预取窗口（默认 3 页）」；
   未浏览到的页保持不翻译，节省时间与 token
4. **下载才全量**：点击下载触发 `finalize`，剩余页面全部翻译（按钮内实时显示页进度），
   随后按所选模式合成最终 PDF 并自动下载

### 后端处理流程

1. **EXTRACTING**：PyMuPDF 提取全部文本块并按页分组（无文本页直接就绪）
2. **SERVING**：调度器按「焦点窗口优先」逐页翻译，每页译完立刻生成单页预览 PDF
3. **FINALIZING**：收到下载请求后补翻全部剩余页
4. **RENDERING → DONE**：按输出模式（纯译文/交错）合成 `result.pdf` 供下载

## 注意事项

### 💡 思考模式

- **启用思考模式**可以提升翻译质量，但会显著增加：
  - API 调用耗时（可能增加 2~3 倍）
  - API 成本（思考过程也计费）
- 建议仅在需要高质量翻译时启用，日常使用保持禁用

### 📄 长文档翻译

- 翻译超过 100 页的文档会耗时较长（可能需要数十分钟），具体取决于：
  - 文档复杂度和文本量
  - 配置的并发数和字符预算
  - DeepSeek API 的响应速度
- 建议在后台运行，不要频繁刷新进度页面

### 🖼️ 扫描版 PDF

- **扫描版 PDF**（纯图像，无文本层）**不支持翻译**
  - 应用只能提取有 OCR 文本层的 PDF
  - 如果 PDF 来自扫描或纯图像，需先用 OCR 工具提取文本层
  - 建议使用在线 OCR 服务（如 Google Docs、Adobe 等）先转换

### 🔧 故障排查

**问题：`DEEPSEEK_API_KEY 未配置`**
- 检查 `.env` 文件是否存在且正确填写 `DEEPSEEK_API_KEY`
- 确保 API Key 有效且有足够额度

**问题：`API 请求超时`**
- 可能是网络问题或 DeepSeek API 繁忙
- 尝试增加 `LLM_TIMEOUT` 配置值
- 检查是否启用了思考模式（会增加耗时）

**问题：`翻译结果为空或原文`**
- 可能是 API 返回错误，查看服务日志找原因
- 检查 API 额度是否充足
- 尝试重新上传并翻译

## 项目结构

```
pdf-translator/
├── backend/                  # 后端代码
│   ├── __init__.py
│   ├── models.py            # TextBlock 共享数据结构
│   ├── config.py            # 配置管理
│   ├── pdf_engine.py        # PDF 解析与回填
│   ├── translator.py        # LLM 翻译引擎
│   ├── jobs.py              # 任务管理
│   └── main.py              # FastAPI 路由
├── frontend/                # 前端代码（HTML/CSS/JS）
├── data/                    # 运行时数据目录
│   └── jobs/                # 每个任务的数据目录
│       └── {job_id}/
│           ├── source.pdf   # 原始 PDF
│           └── result.pdf   # 翻译结果 PDF
├── requirements.txt         # Python 依赖
├── .env                     # 本地配置（不提交）
├── .env.example             # 配置示例
└── README.md                # 本文件
```

## 常见使用场景

- **学习资料**：翻译英文教材、论文、教程，边看原文边对照译文
- **文档翻译**：翻译项目文档、API 文档、使用指南等专业文档
- **书籍翻译**：翻译 PDF 电子书，保留原有版式和排版
- **代码文档**：翻译代码注释和文档，保留代码本身不变

## 开发与测试

### 运行单元测试

各模块在 `if __name__ == "__main__":` 块中包含冒烟测试：

```bash
# 测试 PDF 引擎
.venv/bin/python -m backend.pdf_engine

# 测试翻译器
.venv/bin/python -m backend.translator

# 测试任务管理
.venv/bin/python -m backend.jobs
```

### 查看日志

后端使用 Python `logging` 模块记录日志，logger 名为模块名。启动服务时会在控制台输出详细日志。

## 许可证

本项目仅供学习和非商业用途使用。

## 支持

如遇问题，请检查：
1. `.env` 配置是否正确
2. DeepSeek API Key 是否有效
3. 网络连接是否正常
4. PDF 文件是否有效（非扫描版）
