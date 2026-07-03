"""FastAPI 路由入口。

职责：
- POST /api/translate 接收上传的 PDF，创建翻译任务；
- GET /api/jobs、/api/jobs/{id} 查询任务状态；
- GET /api/jobs/{id}/file/original、/file/result、/download 提供文件预览与下载；
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

from backend.config import get_settings
from backend.jobs import JobManager, JobStatus

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
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii").strip()
    if not ascii_fallback:
        ascii_fallback = "download.pdf"
    quoted_utf8 = urllib.parse.quote(filename, safe="")
    return f'{disposition_type}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quoted_utf8}'


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

    job = job_manager.create(
        file_bytes=file_bytes,
        filename=file.filename,
        mode=mode,
        direction=direction,
        thinking=_parse_bool(thinking),
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
    if job.status != JobStatus.DONE:
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
    if job.status != JobStatus.DONE:
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
