"""FastAPI 路由入口。

职责：
- POST /api/translate 接收上传的 PDF，创建翻译任务；
- GET /api/jobs、/api/jobs/{id} 查询任务状态；
- GET /api/jobs/{id}/file/original、/file/result、/download 提供文件预览与下载；
- GET /api/jobs/{id}/page/{page_index} 按需获取单页译文 PDF（v2 增量按需翻译）；
- POST /api/jobs/{id}/focus、/finalize 上报浏览焦点页 / 触发全量翻译（v2）；
- GET /api/config、PUT /api/config 读取 / 运行时更新模型配置（v3 §3）；
- GET /api/health 健康检查；
- 最后挂载 frontend/ 静态目录（若存在）。
"""
from __future__ import annotations

import logging
import os
import urllib.parse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from backend.config import apply_updates, get_settings
from backend.jobs import JobManager, JobPhase

logger = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 80 * 1024 * 1024  # 80MB
_UPLOAD_CHUNK_SIZE = 1024 * 1024
_SOURCE_FILENAME = "source.pdf"
_RESULT_FILENAME = "result.pdf"
_TRUE_VALUES = {"true", "1", "yes"}


def _parse_bool(value: str) -> bool:
    """解析 "true"/"1"/"yes"（不区分大小写）为 True，其余为 False。"""
    return value.strip().lower() in _TRUE_VALUES


def _rfc5987_content_disposition(disposition_type: str, filename: str) -> str:
    """构造同时含 ASCII fallback 与 RFC5987 `filename*` 的 Content-Disposition 头，
    以正确处理非 ASCII（如中文）文件名。
    """
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii")
    # 过滤会破坏 quoted-string / 注入 header 的字符：双引号、反斜杠，以及所有控制字符
    # （含 CR/LF 与 DEL）。否则文件名里的 `"` 会提前闭合 quoted-string，甚至 CRLF 注入 header。
    ascii_fallback = "".join(
        ch for ch in ascii_fallback if 0x20 <= ord(ch) != 0x7f and ch not in '"\\'
    ).strip()
    if not ascii_fallback:
        ascii_fallback = "download.pdf"
    # filename* 部分对整个文件名做 percent-encode，本就不含裸控制符/引号，无需额外过滤。
    quoted_utf8 = urllib.parse.quote(filename, safe="")
    return f'{disposition_type}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quoted_utf8}'


class FocusRequest(BaseModel):
    """POST /api/jobs/{job_id}/focus 请求体：{"page": <int>}（0-based 浏览页）。"""

    page: int


class RetranslateRequest(BaseModel):
    """POST /api/jobs/{job_id}/retranslate 请求体：

    - `{"scope": "all"}`：全量重译；
    - `{"scope": "page", "page": <0-based int>}`：仅重译某页。
    """

    scope: str
    page: int | None = None


class ConfigUpdateRequest(BaseModel):
    """PUT /api/config 请求体：白名单键同 `config.apply_updates`，均为可选——
    未出现在请求 JSON 中的字段视为「不修改」（用 `model_dump(exclude_unset=True)`
    区分「未传」与「传了但是空值/False」）。

    `extra="forbid"`：白名单之外的键必须在这里就被拒绝（422）。pydantic 默认会
    静默丢弃未声明字段，若不禁止，`config.apply_updates()` 内部「未知键 → ValueError」
    的校验分支将永远无法通过 HTTP 触发到——白名单外键会被 FastAPI 在到达 apply_updates
    之前就悄悄吃掉，而不是报错。
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    thinking_enabled: bool | None = None
    concurrency: int | None = None


def _mask_api_key(api_key: str) -> str:
    """掩码 api_key，格式类似 "sk-****last4"：仅保留前缀（若够长）与末 4 位，
    中间固定用 4 个 `*` 遮蔽——绝不允许通过掩码反推出完整 key。

    空串 → 空串；长度 <=4 → 全部替换为等长 `*`（避免短 key 被完整暴露在「末 4 位」里）。
    """
    if not api_key:
        return ""
    if len(api_key) <= 4:
        return "*" * len(api_key)
    prefix = api_key[:3] if len(api_key) > 8 else ""
    return f"{prefix}****{api_key[-4:]}"


def _config_response(cfg) -> dict:
    """GET /api/config 与 PUT /api/config 成功响应共用的序列化——绝不包含完整 api_key。"""
    return {
        "base_url": cfg.base_url,
        "model": cfg.model,
        "thinking_enabled": cfg.thinking_enabled,
        "concurrency": cfg.concurrency,
        "api_key_masked": _mask_api_key(cfg.api_key),
    }


# ---------------------------------------------------------------------------
# 全局对象（模块级初始化）
# ---------------------------------------------------------------------------
settings = get_settings()
job_manager = JobManager(settings)

app = FastAPI(title="PDF 中英翻译器")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.post("/api/translate", status_code=201)
async def create_translate_job(
    file: UploadFile = File(...),
    mode: str = Form(...),
    direction: str = Form(...),
    thinking: str = Form(...),
    # v3 内容哈希缓存：use_cache=true（默认）时，若存在同文件 + 同 direction + 同 model 的
    # 缓存文件，则把历史译文预载进本 job，命中页跳过 LLM 直接渲染（cache_hit=True）；
    # use_cache=false 时忽略缓存，整篇重新翻译。
    use_cache: str = Form("true"),
) -> JSONResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")

    chunks = bytearray()
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="文件超过 80MB 限制")
    file_bytes = bytes(chunks)

    job = await job_manager.create(
        file_bytes=file_bytes,
        filename=file.filename,
        mode=mode,
        direction=direction,
        thinking=_parse_bool(thinking),
        use_cache=_parse_bool(use_cache),
    )
    logger.info("已创建任务 %s：%s", job.id, file.filename)
    return JSONResponse(status_code=201, content={"job_id": job.id})


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JobManager.to_dict(job)


@app.get("/api/jobs")
async def list_jobs() -> dict:
    return {"jobs": [JobManager.to_dict(j) for j in job_manager.list()]}


@app.get("/api/jobs/{job_id}/file/original")
async def get_original_file(job_id: str) -> FileResponse:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    path = os.path.join(job.dir, _SOURCE_FILENAME)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="原始文件不存在")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=_SOURCE_FILENAME,
        content_disposition_type="inline",
    )


@app.get("/api/jobs/{job_id}/file/result")
async def get_result_file(job_id: str) -> FileResponse:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != JobPhase.DONE:
        raise HTTPException(status_code=409, detail="任务尚未完成")
    path = os.path.join(job.dir, _RESULT_FILENAME)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="结果文件不存在")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=_RESULT_FILENAME,
        content_disposition_type="inline",
    )


@app.get("/api/jobs/{job_id}/download")
async def download_result_file(job_id: str) -> FileResponse:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != JobPhase.DONE:
        raise HTTPException(status_code=409, detail="任务尚未完成")
    path = os.path.join(job.dir, _RESULT_FILENAME)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="结果文件不存在")

    base_name = job.filename
    if base_name.lower().endswith(".pdf"):
        base_name = base_name[: -len(".pdf")]
    download_name = f"{base_name}_translated.pdf"

    response = FileResponse(path, media_type="application/pdf")
    response.headers["content-disposition"] = _rfc5987_content_disposition(
        "attachment", download_name
    )
    return response


# ---------------------------------------------------------------------------
# v2 增量按需翻译路由：预览按需翻译（浏览到哪翻到哪 + 预取窗口），
# 只有下载（finalize）才触发全量翻译。
# ---------------------------------------------------------------------------
@app.get("/api/jobs/{job_id}/page/{page_index}", response_model=None)
async def get_job_page(job_id: str, page_index: int) -> FileResponse | JSONResponse:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if page_index < 0 or page_index >= job.page_count:
        raise HTTPException(status_code=404, detail="页码越界")

    path = job_manager.page_pdf_path(job_id, page_index)
    if path is not None:
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="页面文件不存在")
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"page_{page_index}.pdf",
            content_disposition_type="inline",
        )

    # 该页未译好（path 为 None ⟹ page_status[page_index] != "done"）。
    # 若任务已失败，后台调度器已退出，这些 pending/translating 页永远不会再变 done——
    # 必须返回 409 让前端停止无限轮询并展示错误，而不是永久 202。
    # （已 done 的页在上面的分支已正常返回文件，不受影响。）
    if job.status == JobPhase.ERROR:
        return JSONResponse(
            status_code=409, content={"status": "error", "error": job.error}
        )

    # 该页尚未译好：202 + 当前页状态
    # （job_manager.page_pdf_path 内部已顺带把 focus 提示到该页，此处无需重复调用 focus）
    try:
        page_status = job.page_status[page_index]
    except (AttributeError, IndexError):
        page_status = "pending"
    return JSONResponse(status_code=202, content={"status": page_status})


@app.post("/api/jobs/{job_id}/focus")
async def focus_job(job_id: str, payload: FocusRequest) -> dict:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 越界夹到合法范围（page_count 为 0 时至少夹到 0）
    page_index = payload.page
    if job.page_count > 0:
        page_index = max(0, min(page_index, job.page_count - 1))
    else:
        page_index = max(0, page_index)

    if not job_manager.focus(job_id, page_index):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/finalize")
async def finalize_job(job_id: str) -> dict:
    if not job_manager.finalize(job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retranslate")
async def retranslate_job(job_id: str, payload: RetranslateRequest) -> dict:
    """页级 / 全量重译：清对应页译文、置回 pending、强制重走 LLM（跳过缓存覆盖）。

    映射：job 不存在 → 404；extracting 阶段（页表未就绪）→ 409；scope 非法或页越界 → 400。
    """
    ok, reason = job_manager.retranslate(job_id, payload.scope, payload.page)
    if ok:
        return {"ok": True}
    if reason == "not_found":
        raise HTTPException(status_code=404, detail="任务不存在")
    if reason == "extracting":
        raise HTTPException(status_code=409, detail="任务正在解析版式，稍后再试")
    if reason == "page_out_of_range":
        raise HTTPException(status_code=400, detail="页码越界")
    # invalid_scope 及其它未知原因
    raise HTTPException(status_code=400, detail="重译请求参数无效")


# ---------------------------------------------------------------------------
# v3 增补路由：运行时模型配置（DESIGN.md v3 §3「运行时模型配置」）。
# main.py 模块级 `settings` 与 `job_manager.settings` 是 `config.get_settings()` 返回的
# 同一个单例；`apply_updates()` 原地修改该单例字段，`job_manager.reconfigure()` 额外
# 重建全局 LLM 并发限流器（Semaphore 不支持动态调容，只能整体替换）。
# ---------------------------------------------------------------------------
@app.get("/api/config")
async def get_config() -> dict:
    return _config_response(settings)


@app.put("/api/config")
async def update_config(payload: ConfigUpdateRequest) -> dict:
    # exclude_unset：未出现在请求 JSON 中的字段不进 updates dict（视为「不修改」），
    # 与 "传了空串 api_key"（约定为「不修改 api_key」，由 apply_updates 内部处理）区分开。
    updates = payload.model_dump(exclude_unset=True)
    try:
        new_settings = apply_updates(updates)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    job_manager.reconfigure(new_settings)
    logger.info("运行时配置已通过 /api/config 更新：%s", sorted(updates.keys()))
    return _config_response(new_settings)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "model": settings.model}


# ---------------------------------------------------------------------------
# 静态托管前端：必须放在全部 API 路由之后挂载，避免遮蔽 /api/* 路由。
# frontend 目录可能尚不存在（由主线并行开发中），挂载前先检查目录是否存在。
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONTEND_DIR = os.path.join(_PROJECT_ROOT, "frontend")
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
    logger.info("已挂载前端静态目录：%s", _FRONTEND_DIR)
else:
    logger.warning("前端目录不存在，跳过静态挂载：%s", _FRONTEND_DIR)


if __name__ == "__main__":
    # ------------------------------------------------------------------
    # 冒烟测试：用 FastAPI TestClient 驱动全部路由，重点覆盖 v2 增量按需翻译
    # 三条新路由（page/focus/finalize）及其与 job 生命周期的联动。
    #
    # 为避免真实调用 LLM 与污染项目 data/ 目录：
    #   - monkeypatch backend.translator.Translator 为立即返回译文的桩
    #     （JobManager._run 内部延迟 import 会在调用时拿到这个桩）；
    #   - 把模块级 settings.data_dir 临时指向 tmp 目录
    #     （job_manager.settings 与全局 settings 是同一个对象引用）；
    #   - 临时调小 _MAX_UPLOAD_BYTES，避免真的构造 80MB 请求体拖慢测试。
    # ------------------------------------------------------------------
    import tempfile
    import time as _time

    import pymupdf
    from fastapi.testclient import TestClient

    import backend.translator as _translator_mod

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    class _FakeTranslator:
        """立即返回 {key: "译文"+key} 的桩：不触发任何网络请求。"""

        def __init__(self, settings, thinking=None, semaphore=None) -> None:
            self.settings = settings
            self.semaphore = semaphore

        async def translate_blocks(
            self, blocks, direction, progress_cb=None,
            context_before="", context_after="",
        ):
            return {b.key: "译文" + b.key for b in blocks}

    def _make_test_pdf_bytes(pages: int) -> bytes:
        """现场生成 pages 页测试 PDF，每页含一段英文（保证每页都有可译块）。"""
        doc = pymupdf.open()
        for i in range(pages):
            page = doc.new_page(width=595, height=842)  # A4
            page.insert_textbox(
                pymupdf.Rect(50, 60, 545, 160),
                f"This is page {i + 1}. Ordinary English text for smoke testing.",
                fontname="helv",
                fontsize=12,
            )
        data = doc.tobytes(garbage=3, deflate=True)
        doc.close()
        return data

    def _wait_until(pred, timeout: float = 10.0, interval: float = 0.05) -> bool:
        """轮询直到 pred() 为真或超时；返回最终 pred() 结果。"""
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if pred():
                return True
            _time.sleep(interval)
        return pred()

    _orig_translator = _translator_mod.Translator
    _orig_data_dir = settings.data_dir
    _orig_max_upload = _MAX_UPLOAD_BYTES
    _translator_mod.Translator = _FakeTranslator
    _MAX_UPLOAD_BYTES = 2048  # noqa: F811 - 有意重绑模块全局，缩短 413 场景耗时

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings.data_dir = tmp_dir

            with TestClient(app) as client:
                # --- /api/health ---
                r = client.get("/api/health")
                assert r.status_code == 200, r.text
                assert r.json() == {"status": "ok", "model": settings.model}, r.json()
                print("/api/health 通过：", r.json())

                # --- /api/translate：非 PDF → 400 ---
                r = client.post(
                    "/api/translate",
                    files={"file": ("test.txt", b"hello", "text/plain")},
                    data={"mode": "translated", "direction": "auto", "thinking": "false"},
                )
                assert r.status_code == 400, r.text
                print("非 PDF 文件 400 通过")

                # --- /api/translate：超过（调小后的）上传上限 → 413 ---
                r = client.post(
                    "/api/translate",
                    files={"file": ("big.pdf", b"0" * (_MAX_UPLOAD_BYTES + 1), "application/pdf")},
                    data={"mode": "translated", "direction": "auto", "thinking": "false"},
                )
                assert r.status_code == 413, r.status_code
                print("超过上传上限 413 通过")

                # --- /api/translate：正常创建任务 → 201 ---
                pdf_bytes = _make_test_pdf_bytes(4)
                r = client.post(
                    "/api/translate",
                    files={"file": ("demo.pdf", pdf_bytes, "application/pdf")},
                    data={"mode": "translated", "direction": "auto", "thinking": "false"},
                )
                assert r.status_code == 201, r.text
                job_id = r.json()["job_id"]
                assert job_id
                print("创建任务 201 通过：job_id=", job_id)

                # --- GET /api/jobs/{id} 与 /api/jobs：v2 字段齐全 ---
                expected_keys = {
                    "id", "filename", "mode", "direction", "thinking", "status",
                    "progress", "page_count", "total_blocks", "done_blocks",
                    "error", "created_at", "dir",
                    "page_status", "pages_done", "focus_page", "finalize_requested",
                    "file_sha256", "cache_hit",
                }
                r = client.get(f"/api/jobs/{job_id}")
                assert r.status_code == 200, r.text
                assert set(r.json().keys()) == expected_keys, r.json().keys()

                r = client.get("/api/jobs")
                assert r.status_code == 200
                assert any(j["id"] == job_id for j in r.json()["jobs"])
                print("GET /api/jobs(/{id}) 字段齐全（含 v2 新字段）")

                assert client.get("/api/jobs/does-not-exist").status_code == 404

                # --- 等待提取完成（page_count 就绪） ---
                assert _wait_until(
                    lambda: client.get(f"/api/jobs/{job_id}").json()["page_count"] == 4
                ), client.get(f"/api/jobs/{job_id}").json()

                # --- file/result、download 在未 done 前 → 409（不变契约） ---
                assert client.get(f"/api/jobs/{job_id}/file/result").status_code == 409
                assert client.get(f"/api/jobs/{job_id}/download").status_code == 409

                # --- file/original → 200 ---
                r = client.get(f"/api/jobs/{job_id}/file/original")
                assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
                print("file/original 200、file/result|download 未 done 时 409 通过")

                # --- v2: GET page：job 不存在 / 页码越界 → 404 ---
                assert client.get(f"/api/jobs/{job_id}/page/99").status_code == 404
                assert client.get(f"/api/jobs/{job_id}/page/-1").status_code == 404
                assert client.get("/api/jobs/does-not-exist/page/0").status_code == 404
                print("GET page 越界/job 不存在 404 通过")

                # --- v2: focus：越界夹到合法范围；job 不存在 → 404 ---
                r = client.post(f"/api/jobs/{job_id}/focus", json={"page": 999})
                assert r.status_code == 200 and r.json() == {"ok": True}, r.text
                assert client.get(f"/api/jobs/{job_id}").json()["focus_page"] == 3

                r = client.post(f"/api/jobs/{job_id}/focus", json={"page": 0})
                assert r.json() == {"ok": True}
                assert client.get(f"/api/jobs/{job_id}").json()["focus_page"] == 0

                assert client.post("/api/jobs/does-not-exist/focus", json={"page": 0}).status_code == 404
                print("POST focus 越界夹取/job 不存在 404 通过")

                # --- v2: GET page：该页已译好 → 200 PDF；未译好 → 202 + status ---
                assert _wait_until(
                    lambda: client.get(f"/api/jobs/{job_id}").json()["page_status"][0] == "done"
                ), client.get(f"/api/jobs/{job_id}").json()["page_status"]
                r = client.get(f"/api/jobs/{job_id}/page/0")
                assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"

                # 焦点已切回 0（窗口未覆盖第 3 页），第 3 页大概率仍未翻译 → 202
                r = client.get(f"/api/jobs/{job_id}/page/3")
                assert r.status_code in (200, 202), r.status_code
                if r.status_code == 202:
                    assert r.json()["status"] in ("pending", "translating", "done"), r.json()
                print("GET page 已译好 200 / 未译好 202 通过")

                # --- v2: finalize：幂等触发全量翻译，job 不存在 → 404 ---
                r = client.post(f"/api/jobs/{job_id}/finalize")
                assert r.status_code == 200 and r.json() == {"ok": True}, r.text
                r2 = client.post(f"/api/jobs/{job_id}/finalize")
                assert r2.json() == {"ok": True}
                assert client.post("/api/jobs/does-not-exist/finalize").status_code == 404

                assert _wait_until(
                    lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "done",
                    timeout=20.0,
                ), client.get(f"/api/jobs/{job_id}").json()
                print("POST finalize 后任务到达 done")

                # --- done 之后：file/result、download → 200，且下载文件名符合 RFC5987 契约 ---
                r = client.get(f"/api/jobs/{job_id}/file/result")
                assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"

                r = client.get(f"/api/jobs/{job_id}/download")
                assert r.status_code == 200
                cd = r.headers["content-disposition"]
                assert "attachment" in cd and "demo_translated.pdf" in cd, cd
                print("done 后 file/result 200、download 200 且文件名正确")

                # --- done 之后：全部页 GET page 均应 200 ---
                for n in range(4):
                    r = client.get(f"/api/jobs/{job_id}/page/{n}")
                    assert r.status_code == 200, (n, r.status_code, r.text)
                print("done 后全部页 GET page 均 200 通过")

                # --- v3: 内容哈希缓存（缓存文件方案）+ 页级/全量重译 ---
                import re as _re

                # 首次上传一份新 PDF（3 页，与 demo 不同 → 不同 sha），等其页翻译并写回缓存文件。
                pdf_cache = _make_test_pdf_bytes(3)
                r = client.post(
                    "/api/translate",
                    files={"file": ("cache.pdf", pdf_cache, "application/pdf")},
                    data={"mode": "translated", "direction": "auto",
                          "thinking": "false", "use_cache": "true"},
                )
                assert r.status_code == 201, r.text
                cache_a = r.json()["job_id"]
                assert _wait_until(
                    lambda: (lambda j: bool(j.get("page_status")) and j["page_status"][0] == "done")(
                        client.get(f"/api/jobs/{cache_a}").json()
                    )
                ), client.get(f"/api/jobs/{cache_a}").json()
                sha = client.get(f"/api/jobs/{cache_a}").json()["file_sha256"]
                assert sha, "缓存 job 应有 sha256"
                safe_model = _re.sub(r"[^A-Za-z0-9._-]", "_", settings.model)
                cache_file = os.path.join(
                    settings.data_dir, "cache", sha, f"auto__{safe_model}.json"
                )
                assert _wait_until(lambda: os.path.isfile(cache_file)), (
                    "翻译后应写回缓存文件：" + cache_file
                )
                print("v3 缓存：首次上传翻译并写回缓存文件通过")

                # 二次上传同一文件（use_cache=true）→ 新建独立 job，命中缓存文件（cache_hit=True）。
                r = client.post(
                    "/api/translate",
                    files={"file": ("cache.pdf", pdf_cache, "application/pdf")},
                    data={"mode": "translated", "direction": "auto",
                          "thinking": "false", "use_cache": "true"},
                )
                assert r.status_code == 201, r.text
                cache_b = r.json()["job_id"]
                assert cache_b != cache_a, "缓存文件方案：二次上传应新建独立 job"
                assert _wait_until(
                    lambda: client.get(f"/api/jobs/{cache_b}").json()["cache_hit"] is True
                ), "二次上传应命中缓存文件 cache_hit=True"
                # 未 finalize，命中缓存的页 0 也应秒 done（跳过 LLM 直接渲染）。
                assert _wait_until(
                    lambda: (lambda j: bool(j.get("page_status")) and j["page_status"][0] == "done")(
                        client.get(f"/api/jobs/{cache_b}").json()
                    )
                ), client.get(f"/api/jobs/{cache_b}").json()
                assert client.get(f"/api/jobs/{cache_b}").json()["finalize_requested"] is False
                print("v3 缓存：二次上传 cache_hit=True 且页秒 done（未 finalize）通过")

                # use_cache=false → 不预载缓存，cache_hit=False。
                r = client.post(
                    "/api/translate",
                    files={"file": ("cache.pdf", pdf_cache, "application/pdf")},
                    data={"mode": "translated", "direction": "auto",
                          "thinking": "false", "use_cache": "false"},
                )
                assert r.status_code == 201, r.text
                cache_c = r.json()["job_id"]
                assert _wait_until(
                    lambda: client.get(f"/api/jobs/{cache_c}").json().get("page_count") == 3
                )
                assert client.get(f"/api/jobs/{cache_c}").json()["cache_hit"] is False, (
                    "use_cache=false 不应命中缓存"
                )
                print("v3 缓存：use_cache=false 不命中缓存通过")

                # --- v3 重译：page / all + 状态映射 ---
                # job 不存在 → 404；scope=page 越界 → 400
                assert client.post(
                    "/api/jobs/does-not-exist/retranslate", json={"scope": "all"}
                ).status_code == 404
                assert client.post(
                    f"/api/jobs/{cache_b}/retranslate", json={"scope": "page", "page": 999}
                ).status_code == 400

                # 用已 done 的 demo job（job_id）测「done → serving → 页回 pending 再 done」
                assert client.get(f"/api/jobs/{job_id}").json()["status"] == "done"
                r = client.post(
                    f"/api/jobs/{job_id}/retranslate", json={"scope": "page", "page": 0}
                )
                assert r.status_code == 200 and r.json() == {"ok": True}, r.text
                # done job 重译 → 回 serving
                assert _wait_until(
                    lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "serving"
                ), client.get(f"/api/jobs/{job_id}").json()
                # 该页最终重新 done
                assert _wait_until(
                    lambda: client.get(f"/api/jobs/{job_id}").json()["page_status"][0] == "done"
                )
                print("v3 重译：retranslate(page) 后 done→serving，页回 pending 再 done 通过")

                # 全部重译 → 回 serving，再 finalize → done
                r = client.post(f"/api/jobs/{job_id}/retranslate", json={"scope": "all"})
                assert r.status_code == 200 and r.json() == {"ok": True}, r.text
                assert _wait_until(
                    lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "serving"
                )
                assert client.post(f"/api/jobs/{job_id}/finalize").status_code == 200
                assert _wait_until(
                    lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "done",
                    timeout=20.0,
                ), client.get(f"/api/jobs/{job_id}").json()
                print("v3 重译：retranslate(all) 后 serving，可再 finalize 到 done 通过")

                # 收尾：把命中缓存 / use_cache=false 但未 finalize 的 job 也 finalize 掉，
                # 避免其后台调度任务悬挂到进程退出。
                for _jid in (cache_a, cache_b, cache_c):
                    client.post(f"/api/jobs/{_jid}/finalize")
                    assert _wait_until(
                        lambda _jid=_jid: client.get(f"/api/jobs/{_jid}").json()["status"] == "done",
                        timeout=20.0,
                    ), client.get(f"/api/jobs/{_jid}").json()

                # --- v3 §3: GET/PUT /api/config（运行时模型配置）---
                # 必须把 config._env_path 重定向到临时文件，绝不能让本次冒烟测试
                # 写坏项目根目录的真实 .env（含真实 DEEPSEEK_API_KEY）。
                import backend.config as _config_mod

                _cfg_orig_env_path = _config_mod._env_path
                _cfg_orig_model = settings.model
                _cfg_orig_concurrency = settings.concurrency
                _cfg_orig_thinking = settings.thinking_enabled
                _cfg_orig_api_key = settings.api_key
                _cfg_orig_base_url = settings.base_url
                try:
                    with tempfile.TemporaryDirectory() as _cfg_tmp_dir:
                        _cfg_tmp_env = os.path.join(_cfg_tmp_dir, ".env")
                        with open(_cfg_tmp_env, "w", encoding="utf-8") as f:
                            f.write("DEEPSEEK_API_KEY=sk-original-fake-key-0000\n")
                        _config_mod._env_path = lambda: _cfg_tmp_env

                        # GET：字段齐全，且绝不含完整 api_key（无 "api_key" 键）
                        r = client.get("/api/config")
                        assert r.status_code == 200, r.text
                        cfg = r.json()
                        assert set(cfg.keys()) == {
                            "base_url", "model", "thinking_enabled",
                            "concurrency", "api_key_masked",
                        }, cfg.keys()
                        assert "api_key" not in cfg, cfg
                        print("GET /api/config 字段齐全（不含完整 key）通过：", cfg)

                        # PUT：合法更新 → 200，且立即反映到 main.py/job_manager 共享的
                        # settings 单例（不需要重启进程）。
                        r = client.put(
                            "/api/config",
                            json={
                                "base_url": "https://example.com",
                                "model": "test-model-x",
                                "thinking_enabled": True,
                                "concurrency": 3,
                                "api_key": "sk-newkey-1234",
                            },
                        )
                        assert r.status_code == 200, r.text
                        data = r.json()
                        assert data["base_url"] == "https://example.com", data
                        assert data["model"] == "test-model-x", data
                        assert data["thinking_enabled"] is True, data
                        assert data["concurrency"] == 3, data
                        assert "api_key" not in data, data
                        assert data["api_key_masked"] != "sk-newkey-1234", "不能回传完整 key"
                        assert data["api_key_masked"].endswith("1234"), data
                        assert settings.model == "test-model-x", "应立即反映到共享 settings 单例"
                        assert settings.concurrency == 3
                        assert job_manager.settings is settings, "job_manager 应共享同一单例"
                        with open(_cfg_tmp_env, "r", encoding="utf-8") as f:
                            _cfg_env_content = f.read()
                        assert "DEEPSEEK_MODEL=test-model-x" in _cfg_env_content
                        assert "DEEPSEEK_API_KEY=sk-newkey-1234" in _cfg_env_content
                        print("PUT /api/config 合法更新通过（单例即时生效 + .env 持久化）")

                        # 非法值 → 422 {"detail": ...}，且不改动已生效配置
                        r = client.put("/api/config", json={"concurrency": 0})
                        assert r.status_code == 422, r.text
                        assert "detail" in r.json(), r.json()
                        assert settings.concurrency == 3, "422 不应改动已生效的 concurrency"
                        print("PUT /api/config 非法值 422 通过（原子失败，不影响已生效配置）")

                        # api_key 缺省/空串 = 不修改，沿用已保存的 key
                        r = client.put("/api/config", json={"model": "test-model-y"})
                        assert r.status_code == 200, r.text
                        assert r.json()["api_key_masked"].endswith("1234"), (
                            "未传 api_key 不应改变已存 key"
                        )
                        print("PUT /api/config 未传 api_key 时不修改通过")
                finally:
                    # 还原共享单例字段与全局限流器，避免污染同进程内后续可能的其他逻辑。
                    settings.model = _cfg_orig_model
                    settings.concurrency = _cfg_orig_concurrency
                    settings.thinking_enabled = _cfg_orig_thinking
                    settings.api_key = _cfg_orig_api_key
                    settings.base_url = _cfg_orig_base_url
                    job_manager.reconfigure(settings)
                    _config_mod._env_path = _cfg_orig_env_path

        print("main.py 全部冒烟测试通过")
    finally:
        _translator_mod.Translator = _orig_translator
        settings.data_dir = _orig_data_dir
        _MAX_UPLOAD_BYTES = _orig_max_upload
