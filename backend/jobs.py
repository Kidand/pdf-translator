"""任务管理：内存 Job store + asyncio 后台任务驱动整条翻译流水线。

流程：QUEUED → EXTRACTING → TRANSLATING → RENDERING → DONE（或任意阶段 → ERROR）。

注意：PdfEngine 与 Translator 在 `_run` 内部使用函数级（延迟）import，
使得 jobs.py 模块本身可以独立 import/测试，不依赖这两个可能尚未就绪的模块。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from backend.config import Settings
from backend.models import TextBlock

logger = logging.getLogger(__name__)


class JobStatus:
    """字符串常量枚举（非 Enum 类，保持与 JSON 序列化一致的裸字符串）。"""

    QUEUED = "queued"
    EXTRACTING = "extracting"
    TRANSLATING = "translating"
    RENDERING = "rendering"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    id: str                # uuid4 hex
    filename: str           # 原始文件名
    mode: str                # "translated" | "interleaved"
    direction: str
    thinking: bool
    status: str              # JobStatus.*
    progress: float          # 0.0~1.0
    page_count: int
    total_blocks: int
    done_blocks: int
    error: Optional[str]
    created_at: float
    dir: str                 # data/jobs/<id>


# 进度阶段固定值（非 translating 阶段）
_PROGRESS_QUEUED = 0.0
_PROGRESS_EXTRACTING = 0.02
_PROGRESS_TRANSLATING_BASE = 0.05
_PROGRESS_TRANSLATING_SPAN = 0.85
_PROGRESS_RENDERING = 0.92
_PROGRESS_DONE = 1.0

_SOURCE_FILENAME = "source.pdf"
_RESULT_FILENAME = "result.pdf"


class JobManager:
    """内存中的任务管理器：创建任务、驱动后台流水线、查询状态。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jobs: dict[str, Job] = {}
        # 持有后台任务的强引用，防止事件循环仅持弱引用导致 Task 被提前垃圾回收
        # （见 asyncio 官方文档 create_task 说明）。
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def create(
        self,
        file_bytes: bytes,
        filename: str,
        mode: str,
        direction: str,
        thinking: bool,
    ) -> Job:
        """创建新任务：写入 source.pdf，状态置为 QUEUED，并调度后台执行。"""
        job_id = uuid.uuid4().hex
        job_dir = os.path.join(self.settings.data_dir, "jobs", job_id)
        os.makedirs(job_dir, exist_ok=True)

        source_path = os.path.join(job_dir, _SOURCE_FILENAME)
        with open(source_path, "wb") as f:
            f.write(file_bytes)

        job = Job(
            id=job_id,
            filename=filename,
            mode=mode,
            direction=direction,
            thinking=thinking,
            status=JobStatus.QUEUED,
            progress=_PROGRESS_QUEUED,
            page_count=0,
            total_blocks=0,
            done_blocks=0,
            error=None,
            created_at=time.time(),
            dir=job_dir,
        )
        self._jobs[job_id] = job
        logger.info("创建任务 %s（%s，mode=%s，direction=%s）", job_id, filename, mode, direction)

        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        """按 created_at 倒序返回全部任务。"""
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    # ------------------------------------------------------------------
    # 后台流水线
    # ------------------------------------------------------------------
    async def _run(self, job: Job) -> None:
        # 延迟导入：保证 jobs.py 本身可独立 import，不受 pdf_engine/translator 是否就绪影响
        from backend.pdf_engine import PdfEngine
        from backend.translator import Translator

        source_path = os.path.join(job.dir, _SOURCE_FILENAME)
        result_path = os.path.join(job.dir, _RESULT_FILENAME)

        engine: Optional["PdfEngine"] = None
        try:
            # ---------------- EXTRACTING ----------------
            job.status = JobStatus.EXTRACTING
            job.progress = _PROGRESS_EXTRACTING
            logger.info("任务 %s 开始提取文本块", job.id)

            engine = await asyncio.to_thread(PdfEngine, source_path)
            job.page_count = engine.page_count
            blocks: list[TextBlock] = await asyncio.to_thread(engine.extract_blocks)
            job.total_blocks = len(blocks)
            logger.info("任务 %s 提取到 %d 页 / %d 个文本块", job.id, job.page_count, len(blocks))

            # ---------------- TRANSLATING ----------------
            job.status = JobStatus.TRANSLATING
            job.progress = _PROGRESS_TRANSLATING_BASE
            translator = Translator(self.settings, thinking=job.thinking)

            def _progress_cb(done_blocks: int, total_blocks: int) -> None:
                job.done_blocks = done_blocks
                job.total_blocks = total_blocks
                ratio = (done_blocks / total_blocks) if total_blocks else 1.0
                job.progress = _PROGRESS_TRANSLATING_BASE + _PROGRESS_TRANSLATING_SPAN * ratio

            translations = await translator.translate_blocks(
                blocks, job.direction, progress_cb=_progress_cb
            )
            logger.info("任务 %s 翻译完成，共 %d 条译文", job.id, len(translations))

            # ---------------- RENDERING ----------------
            job.status = JobStatus.RENDERING
            job.progress = _PROGRESS_RENDERING
            await asyncio.to_thread(engine.build_output, translations, job.mode, result_path)
            logger.info("任务 %s 输出已写入 %s", job.id, result_path)

            # ---------------- DONE ----------------
            job.status = JobStatus.DONE
            job.progress = _PROGRESS_DONE
        except Exception as e:  # noqa: BLE001 - 任何异常都要落到任务状态上
            job.status = JobStatus.ERROR
            job.error = str(e)
            logger.exception("任务 %s 执行失败", job.id)
        finally:
            if engine is not None:
                try:
                    await asyncio.to_thread(engine.close)
                except Exception:  # noqa: BLE001 - 关闭失败不应掩盖原始异常
                    logger.exception("任务 %s 关闭 PdfEngine 失败", job.id)

    # ------------------------------------------------------------------
    @staticmethod
    def to_dict(job: Job) -> dict:
        """将 Job 转为可 JSON 序列化的 dict，供 API 返回。"""
        return {
            "id": job.id,
            "filename": job.filename,
            "mode": job.mode,
            "direction": job.direction,
            "thinking": job.thinking,
            "status": job.status,
            "progress": job.progress,
            "page_count": job.page_count,
            "total_blocks": job.total_blocks,
            "done_blocks": job.done_blocks,
            "error": job.error,
            "created_at": job.created_at,
            "dir": job.dir,
        }


if __name__ == "__main__":
    # 冒烟测试：不依赖 PdfEngine / Translator 是否就绪，
    # 仅验证模块可 import、Job 字段完整、to_dict 输出齐全，以及 JobManager 基本查询行为。
    import tempfile

    logging.basicConfig(level=logging.INFO)

    with tempfile.TemporaryDirectory() as tmp_data_dir:
        fake_settings = Settings(
            api_key="fake",
            base_url="https://example.com",
            model="fake-model",
            thinking_enabled=False,
            concurrency=2,
            batch_char_budget=2200,
            data_dir=tmp_data_dir,
            request_timeout=10.0,
        )

        manager = JobManager(fake_settings)

        # 未知 job id 应返回 None
        assert manager.get("does-not-exist") is None
        # 初始任务列表应为空
        assert manager.list() == []

        # 用极简假 Job 对象（不经过 create()，从而不触发 _run/后台 asyncio 任务）测试 to_dict
        fake_job = Job(
            id="deadbeef",
            filename="demo.pdf",
            mode="translated",
            direction="auto",
            thinking=False,
            status=JobStatus.DONE,
            progress=1.0,
            page_count=3,
            total_blocks=10,
            done_blocks=10,
            error=None,
            created_at=time.time(),
            dir=os.path.join(tmp_data_dir, "jobs", "deadbeef"),
        )

        d = JobManager.to_dict(fake_job)
        expected_keys = {
            "id", "filename", "mode", "direction", "thinking", "status",
            "progress", "page_count", "total_blocks", "done_blocks",
            "error", "created_at", "dir",
        }
        assert set(d.keys()) == expected_keys, set(d.keys())
        assert d["id"] == "deadbeef"
        assert d["status"] == JobStatus.DONE
        assert d["progress"] == 1.0
        print("to_dict 字段齐全：", d)

        # 手动把 fake_job 塞进 manager 内部字典，验证 get()/list() 行为（不触发后台任务）
        manager._jobs[fake_job.id] = fake_job
        assert manager.get("deadbeef") is fake_job
        assert manager.list() == [fake_job]

        # JobStatus 常量完整性检查
        assert JobStatus.QUEUED == "queued"
        assert JobStatus.EXTRACTING == "extracting"
        assert JobStatus.TRANSLATING == "translating"
        assert JobStatus.RENDERING == "rendering"
        assert JobStatus.DONE == "done"
        assert JobStatus.ERROR == "error"

    async def _run_task_reference_smoke_test() -> None:
        """验证 create() 产生的后台 Task 被 `_tasks` 强引用持有（防止事件循环
        仅持弱引用导致 Task 被提前 GC 而使流水线悄无声息中途消失），
        且任务结束后通过 `add_done_callback` 从 `_tasks` 中被正确移除。

        用一个不含任何文本的空白页 PDF：extract_blocks 得到 0 个块，
        translate_blocks 在 total==0 时立即返回空 dict，不会触发真实网络请求，
        因此可用 fake settings 快速跑完整条流水线到 DONE。
        """
        import pymupdf  # 仅用于现场生成测试用空白 PDF，不影响 jobs.py 对其余模块的延迟 import 约定

        with tempfile.TemporaryDirectory() as tmp_dir2:
            settings2 = Settings(
                api_key="fake",
                base_url="https://example.com",
                model="fake-model",
                thinking_enabled=False,
                concurrency=2,
                batch_char_budget=2200,
                data_dir=tmp_dir2,
                request_timeout=10.0,
            )
            manager2 = JobManager(settings2)

            blank_doc = pymupdf.open()
            blank_doc.new_page()
            pdf_bytes = blank_doc.tobytes()
            blank_doc.close()

            job2 = manager2.create(pdf_bytes, "blank.pdf", "translated", "auto", False)

            # create() 应同步地把后台 Task 加入 _tasks（此刻大概率尚未跑完）
            assert len(manager2._tasks) == 1, "后台 Task 未被 _tasks 强引用持有"
            pending_task = next(iter(manager2._tasks))
            assert isinstance(pending_task, asyncio.Task)

            # 等待流水线跑完（最多 5 秒）
            for _ in range(100):
                current = manager2.get(job2.id)
                if current is not None and current.status in (JobStatus.DONE, JobStatus.ERROR):
                    break
                await asyncio.sleep(0.05)

            finished_job = manager2.get(job2.id)
            assert finished_job is not None
            assert finished_job.status == JobStatus.DONE, finished_job.error
            assert finished_job.total_blocks == 0

            # done_callback 是同步回调，Task 完成时由事件循环直接调用，
            # 此时应已从 _tasks 中移除。
            assert manager2._tasks == set(), manager2._tasks

        print("create() 后台任务引用持有与自动清理验证通过")

    asyncio.run(_run_task_reference_smoke_test())

    print("jobs.py 全部冒烟测试通过")
