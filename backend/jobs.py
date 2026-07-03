"""任务管理（v2 增量按需翻译）：内存 Job store + 每 job 一个 asyncio 调度任务。

v2 生命周期（取代 v1「上传即全量翻译」）：

    extracting →（提取完成，秒级）→ serving →（用户点下载 finalize）
              → finalizing → rendering → done            （任意阶段异常 → error）

核心思想：**预览按需翻译（浏览到哪翻到哪 + 焦点预取窗口），只有下载才全量翻译。**

调度循环 `_run` 每次从「下一个要翻译的页」中选一页翻译并渲染单页译文 PDF：
    1. 焦点窗口 [focus_page, focus_page + prefetch_pages) 内最小的 pending 页；
    2. 若 finalize_requested：全文档最小的 pending 页；
    3. 都没有：await 唤醒事件（focus / finalize / page_pdf_path 命中未译页时 set）。

注意：PdfEngine 与 Translator 在 `_run` 内部使用函数级（延迟）import，
使得 jobs.py 模块本身可以独立 import/测试，不依赖这两个模块是否就绪。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from backend.config import Settings
from backend.models import TextBlock

logger = logging.getLogger(__name__)


class JobPhase:
    """字符串常量枚举（非 Enum 类，保持与 JSON 序列化一致的裸字符串）。

    v2 生命周期阶段，`Job.status` 直接存本类的值。
    """

    EXTRACTING = "extracting"
    SERVING = "serving"
    FINALIZING = "finalizing"
    RENDERING = "rendering"
    DONE = "done"
    ERROR = "error"


# 每页翻译状态（page_status 列表元素）
_PAGE_PENDING = "pending"
_PAGE_TRANSLATING = "translating"
_PAGE_DONE = "done"


@dataclass
class Job:
    # ---- v1 字段（全部保留；status 语义改为 JobPhase.* 值）----
    id: str                    # uuid4 hex
    filename: str              # 原始文件名
    mode: str                  # "translated" | "interleaved"
    direction: str
    thinking: bool
    status: str                # JobPhase.*
    progress: float            # 0.0~1.0
    page_count: int
    total_blocks: int
    done_blocks: int
    error: Optional[str]
    created_at: float
    dir: str                   # data/jobs/<id>
    # ---- v2 新增字段 ----
    page_status: list[str] = field(default_factory=list)  # 每页 pending|translating|done
    pages_done: int = 0
    focus_page: int = 0        # 前端最近上报的浏览页（0-based）
    finalize_requested: bool = False


@dataclass
class _JobRuntime:
    """每个 Job 的私有协调对象（**不进 to_dict**）。

    - event：调度循环的唤醒事件，focus / finalize / page_pdf_path 命中未译页时 set；
    - blocks_by_page：extract 后按页分组的 TextBlock 缓存；
    - translations：逐页翻译 merge 而成的全量译文 dict。
    """

    event: asyncio.Event = field(default_factory=asyncio.Event)
    blocks_by_page: dict[int, list[TextBlock]] = field(default_factory=dict)
    translations: dict[str, str] = field(default_factory=dict)


# 进度阶段固定值（见 DESIGN.md v2「progress 语义」）
_PROGRESS_EXTRACTING = 0.02
_PROGRESS_RENDERING = 0.97
_PROGRESS_DONE = 1.0

_SOURCE_FILENAME = "source.pdf"
_RESULT_FILENAME = "result.pdf"
_PAGES_DIRNAME = "pages"


def _page_pdf_filename(page_index: int) -> str:
    return f"page_{page_index}.pdf"


class JobManager:
    """内存中的任务管理器：创建任务、驱动 v2 调度循环、按需翻译与查询状态。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jobs: dict[str, Job] = {}
        # 每个 job 的私有协调对象（event / 块缓存 / 全量译文），与 Job 分离以免泄漏进 to_dict
        self._runtimes: dict[str, _JobRuntime] = {}
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
        """创建新任务：写入 source.pdf，进入 extracting，并调度后台按需翻译循环。"""
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
            status=JobPhase.EXTRACTING,
            progress=_PROGRESS_EXTRACTING,
            page_count=0,
            total_blocks=0,
            done_blocks=0,
            error=None,
            created_at=time.time(),
            dir=job_dir,
            page_status=[],
            pages_done=0,
            focus_page=0,
            finalize_requested=False,
        )
        self._jobs[job_id] = job
        # 私有协调对象须在调度任务启动前就位，
        # 使 focus/finalize/page_pdf_path 在 extracting 早期被调用也能拿到 event。
        self._runtimes[job_id] = _JobRuntime()
        logger.info(
            "创建任务 %s（%s，mode=%s，direction=%s）", job_id, filename, mode, direction
        )

        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        """按 created_at 倒序返回全部任务。"""
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def focus(self, job_id: str, page_index: int) -> bool:
        """更新前端焦点页并唤醒调度器；job 不存在返回 False。

        page_index 夹到 [0, page_count-1]；extracting 阶段（page_count 尚为 0）也允许调用，
        此时仅记录焦点意图（夹到 >=0），待 page_count 就绪后由调度器按窗口消费。
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.page_count > 0:
            job.focus_page = max(0, min(page_index, job.page_count - 1))
        else:
            job.focus_page = max(0, page_index)
        self._wake(job_id)
        logger.debug("任务 %s 焦点更新为第 %d 页", job_id, job.focus_page)
        return True

    def finalize(self, job_id: str) -> bool:
        """置 finalize_requested 并唤醒调度器（幂等）；job 不存在返回 False。

        done / finalizing / rendering 等任意存在的状态下重复调用均返回 True。
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if not job.finalize_requested:
            logger.info("任务 %s 触发 finalize（全量翻译）", job_id)
        job.finalize_requested = True
        self._wake(job_id)
        return True

    def page_pdf_path(self, job_id: str, page_index: int) -> Optional[str]:
        """该页已译好 → 返回单页 PDF 路径；否则 None，并把焦点提示到该页且唤醒调度器。

        job 不存在、page_status 尚未初始化或页码越界 → 返回 None（不改焦点）。
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if not (0 <= page_index < len(job.page_status)):
            return None
        if job.page_status[page_index] == _PAGE_DONE:
            return os.path.join(job.dir, _PAGES_DIRNAME, _page_pdf_filename(page_index))
        # 未译好：等效一次焦点提示，促使调度器尽快翻到该页
        job.focus_page = page_index
        self._wake(job_id)
        return None

    def _wake(self, job_id: str) -> None:
        """set 该 job 的唤醒事件（若运行时对象仍在）。"""
        runtime = self._runtimes.get(job_id)
        if runtime is not None:
            runtime.event.set()

    # ------------------------------------------------------------------
    # 后台调度循环
    # ------------------------------------------------------------------
    async def _run(self, job: Job) -> None:
        # 延迟导入：保证 jobs.py 本身可独立 import，不受 pdf_engine/translator 是否就绪影响
        from backend.pdf_engine import PdfEngine
        from backend.translator import Translator

        runtime = self._runtimes[job.id]
        source_path = os.path.join(job.dir, _SOURCE_FILENAME)
        result_path = os.path.join(job.dir, _RESULT_FILENAME)
        pages_dir = os.path.join(job.dir, _PAGES_DIRNAME)

        engine: Optional["PdfEngine"] = None
        try:
            # ---------------- EXTRACTING ----------------
            job.status = JobPhase.EXTRACTING
            job.progress = _PROGRESS_EXTRACTING
            logger.info("任务 %s 开始提取文本块", job.id)

            engine = await asyncio.to_thread(PdfEngine, source_path)
            job.page_count = engine.page_count
            job.page_status = [_PAGE_PENDING] * job.page_count

            blocks: list[TextBlock] = await asyncio.to_thread(engine.extract_blocks)
            job.total_blocks = len(blocks)
            for block in blocks:
                runtime.blocks_by_page.setdefault(block.page_index, []).append(block)

            # 无块页：渲染「原样单页 PDF」落盘后再标 done。
            # 契约要求 page_pdf_path 对任意 done 页都返回真实存在的单页 PDF；这类页不会
            # 走调度循环的 _translate_and_render_page，故此处主动补渲染（render_page_pdf 对
            # 无可译块的页按契约返回原样单页）。先落盘再标 done，避免出现「已 done 但文件
            # 尚未写入」的观测窗口，使空白 / 纯图片页的预览不至于指向不存在的文件。
            os.makedirs(pages_dir, exist_ok=True)
            for page_index in range(job.page_count):
                if runtime.blocks_by_page.get(page_index):
                    continue
                page_bytes = await asyncio.to_thread(
                    engine.render_page_pdf, page_index, runtime.translations
                )
                out_path = os.path.join(pages_dir, _page_pdf_filename(page_index))
                with open(out_path, "wb") as f:
                    f.write(page_bytes)
                job.page_status[page_index] = _PAGE_DONE
                job.pages_done += 1
            logger.info(
                "任务 %s 提取到 %d 页 / %d 个文本块（%d 页无块直接完成）",
                job.id, job.page_count, len(blocks), job.pages_done,
            )

            # ---------------- SERVING / FINALIZING ----------------
            job.status = JobPhase.SERVING
            self._update_progress(job)
            translator = Translator(self.settings, thinking=job.thinking)

            while True:
                # finalize 一旦请求即进入 finalizing 阶段（按序补翻剩余页）
                if job.finalize_requested and job.status == JobPhase.SERVING:
                    job.status = JobPhase.FINALIZING
                    self._update_progress(job)

                # 先 clear 再 pick：保证 focus/finalize 在 pick 之后 set 的事件不会丢失唤醒
                runtime.event.clear()
                page_index = self._pick_next_page(job)

                if page_index is not None:
                    await self._translate_and_render_page(
                        job, runtime, engine, translator, page_index, pages_dir
                    )
                    continue

                # 无可翻译页：finalize 且全部完成 → 收尾；否则挂起等待唤醒
                if job.finalize_requested and job.pages_done >= job.page_count:
                    break
                await runtime.event.wait()

            # ---------------- RENDERING ----------------
            job.status = JobPhase.RENDERING
            job.progress = _PROGRESS_RENDERING
            logger.info("任务 %s 全部页翻译完成，开始组装输出（mode=%s）", job.id, job.mode)
            await asyncio.to_thread(
                engine.build_output, runtime.translations, job.mode, result_path
            )
            logger.info("任务 %s 输出已写入 %s", job.id, result_path)

            # ---------------- DONE ----------------
            job.status = JobPhase.DONE
            job.progress = _PROGRESS_DONE
        except Exception as e:  # noqa: BLE001 - 任何异常都要落到任务状态上
            job.status = JobPhase.ERROR
            job.error = str(e)
            logger.exception("任务 %s 执行失败", job.id)
        finally:
            if engine is not None:
                try:
                    await asyncio.to_thread(engine.close)
                except Exception:  # noqa: BLE001 - 关闭失败不应掩盖原始异常
                    logger.exception("任务 %s 关闭 PdfEngine 失败", job.id)

    def _pick_next_page(self, job: Job) -> Optional[int]:
        """按 v2 优先级选出下一个要翻译的页（纯函数，无副作用、无 await）。

        1) 焦点窗口 [focus_page, focus_page + prefetch_pages) 内最小的 pending 页；
        2) finalize_requested 时全文档最小的 pending 页；
        3) 都没有 → None（由调用方决定挂起等待或收尾）。
        """
        window_end = min(job.page_count, job.focus_page + self.settings.prefetch_pages)
        for n in range(max(0, job.focus_page), window_end):
            if job.page_status[n] == _PAGE_PENDING:
                return n

        if job.finalize_requested:
            for n in range(job.page_count):
                if job.page_status[n] == _PAGE_PENDING:
                    return n

        return None

    async def _translate_and_render_page(
        self,
        job: Job,
        runtime: _JobRuntime,
        engine: "object",
        translator: "object",
        page_index: int,
        pages_dir: str,
    ) -> None:
        """翻译并渲染单页：translate_blocks → merge 全量译文 → render_page_pdf 落盘。

        一个 job 同一时刻只翻译一页（页内批次由 Translator 内部并发）；
        translate 失败时 Translator 已内部兜底回退原文，调度器无需特判。
        """
        job.page_status[page_index] = _PAGE_TRANSLATING
        page_blocks = runtime.blocks_by_page.get(page_index, [])

        if page_blocks:
            page_translations = await translator.translate_blocks(
                page_blocks, job.direction, progress_cb=None
            )
            runtime.translations.update(page_translations)
            job.done_blocks = len(runtime.translations)

        # 渲染单页译文 PDF（同步 PDF 操作放到线程，避免阻塞事件循环）
        page_bytes = await asyncio.to_thread(
            engine.render_page_pdf, page_index, runtime.translations
        )
        out_path = os.path.join(pages_dir, _page_pdf_filename(page_index))
        with open(out_path, "wb") as f:
            f.write(page_bytes)

        job.page_status[page_index] = _PAGE_DONE
        job.pages_done += 1
        self._update_progress(job)
        logger.info(
            "任务 %s 第 %d 页已完成（%d/%d）",
            job.id, page_index, job.pages_done, job.page_count,
        )

    def _update_progress(self, job: Job) -> None:
        """按 v2 progress 语义刷新 job.progress（error 阶段保持不变）。"""
        if job.status == JobPhase.EXTRACTING:
            job.progress = _PROGRESS_EXTRACTING
        elif job.status in (JobPhase.SERVING, JobPhase.FINALIZING):
            job.progress = (job.pages_done / job.page_count) if job.page_count else 1.0
        elif job.status == JobPhase.RENDERING:
            job.progress = _PROGRESS_RENDERING
        elif job.status == JobPhase.DONE:
            job.progress = _PROGRESS_DONE

    # ------------------------------------------------------------------
    @staticmethod
    def to_dict(job: Job) -> dict:
        """将 Job 转为可 JSON 序列化的 dict，供 API 返回（不含私有协调对象）。"""
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
            # v2 新增
            "page_status": job.page_status,
            "pages_done": job.pages_done,
            "focus_page": job.focus_page,
            "finalize_requested": job.finalize_requested,
        }


if __name__ == "__main__":
    # ------------------------------------------------------------------
    # 冒烟测试（v2）：
    #   * 静态部分：Job 字段完整、to_dict 含 v2 新字段、JobPhase 常量齐全、get/list 行为；
    #   * 调度部分：monkeypatch backend.translator.Translator 为立即返回译文的 stub，
    #     现场生成 5 页测试 PDF，走真实 PdfEngine（含 render_page_pdf / build_output），
    #     验证 serving 按需翻译、焦点优先、未 finalize 不全量、finalize 后全量到 done。
    # ------------------------------------------------------------------
    import tempfile

    import pymupdf

    import backend.pdf_engine as _pdf_engine_mod
    import backend.translator as _translator_mod

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    def _make_settings(data_dir: str, prefetch_pages: int) -> Settings:
        return Settings(
            api_key="fake",
            base_url="https://example.com",
            model="fake-model",
            thinking_enabled=False,
            concurrency=2,
            batch_char_budget=2200,
            data_dir=data_dir,
            request_timeout=10.0,
            prefetch_pages=prefetch_pages,
        )

    # ---------------- 静态断言（不触发后台任务） ----------------
    with tempfile.TemporaryDirectory() as tmp_data_dir:
        manager = JobManager(_make_settings(tmp_data_dir, prefetch_pages=3))

        assert manager.get("does-not-exist") is None
        assert manager.list() == []
        # 未知 job 的 focus/finalize/page_pdf_path 行为
        assert manager.focus("nope", 1) is False
        assert manager.finalize("nope") is False
        assert manager.page_pdf_path("nope", 0) is None

        fake_job = Job(
            id="deadbeef",
            filename="demo.pdf",
            mode="translated",
            direction="auto",
            thinking=False,
            status=JobPhase.DONE,
            progress=1.0,
            page_count=3,
            total_blocks=10,
            done_blocks=10,
            error=None,
            created_at=time.time(),
            dir=os.path.join(tmp_data_dir, "jobs", "deadbeef"),
            page_status=["done", "done", "done"],
            pages_done=3,
            focus_page=1,
            finalize_requested=True,
        )
        d = JobManager.to_dict(fake_job)
        expected_keys = {
            "id", "filename", "mode", "direction", "thinking", "status",
            "progress", "page_count", "total_blocks", "done_blocks",
            "error", "created_at", "dir",
            "page_status", "pages_done", "focus_page", "finalize_requested",
        }
        assert set(d.keys()) == expected_keys, set(d.keys())
        assert d["page_status"] == ["done", "done", "done"]
        assert d["pages_done"] == 3
        assert d["focus_page"] == 1
        assert d["finalize_requested"] is True
        print("to_dict 字段齐全（含 v2 新字段）：", d)

        manager._jobs[fake_job.id] = fake_job
        assert manager.get("deadbeef") is fake_job
        assert manager.list() == [fake_job]

        assert JobPhase.EXTRACTING == "extracting"
        assert JobPhase.SERVING == "serving"
        assert JobPhase.FINALIZING == "finalizing"
        assert JobPhase.RENDERING == "rendering"
        assert JobPhase.DONE == "done"
        assert JobPhase.ERROR == "error"
        print("JobPhase 常量与静态查询断言通过")

    # ---------------- 调度循环断言（假 Translator + 真 PdfEngine） ----------------
    class _FakeTranslator:
        """立即返回 {key: "译文"+key} 的桩：不触发任何网络请求。"""

        def __init__(self, settings: Settings, thinking: bool | None = None) -> None:
            self.settings = settings

        async def translate_blocks(self, blocks, direction, progress_cb=None):
            return {b.key: "译文" + b.key for b in blocks}

    def _make_test_pdf_bytes(pages: int) -> bytes:
        """现场生成 pages 页测试 PDF，每页含一段英文（保证每页都有可译块）。"""
        doc = pymupdf.open()
        for i in range(pages):
            page = doc.new_page(width=595, height=842)  # A4
            page.insert_textbox(
                pymupdf.Rect(50, 60, 545, 160),
                f"This is page {i + 1}. It has ordinary English text to translate "
                "so that every page yields at least one translatable block.",
                fontname="helv",
                fontsize=12,
            )
        data = doc.tobytes(garbage=3, deflate=True)
        doc.close()
        return data

    async def _wait_until(pred, timeout: float = 8.0, interval: float = 0.02) -> bool:
        """轮询直到 pred() 为真或超时；返回最终 pred() 结果。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if pred():
                return True
            await asyncio.sleep(interval)
        return pred()

    async def _scheduling_smoke_test() -> None:
        # monkeypatch 延迟 import 目标：调度器 _run 内 `from backend.translator import Translator`
        # 会取到这里替换后的桩；PdfEngine 用真实实现。
        _orig_translator = _translator_mod.Translator
        _translator_mod.Translator = _FakeTranslator
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                # prefetch_pages=1：使焦点窗口仅含当前页，从而「焦点优先」与
                # 「未 finalize 不全量」两条行为可被确定性地观察。
                settings = _make_settings(data_dir, prefetch_pages=1)
                manager = JobManager(settings)

                pdf_bytes = _make_test_pdf_bytes(5)
                job = manager.create(pdf_bytes, "test.pdf", "translated", "auto", False)

                # (a) create 后进入 serving，且第 0 页很快 done
                ok0 = await _wait_until(
                    lambda: len(job.page_status) == 5
                    and job.page_status[0] == _PAGE_DONE
                )
                assert ok0, f"第 0 页未按时完成：status={job.status} page_status={job.page_status}"
                assert job.status == JobPhase.SERVING, job.status
                # 焦点窗口=1，第 0 页外的页应仍 pending（不会自动全量）
                assert job.page_status[1] == _PAGE_PENDING, job.page_status
                # 已完成页的单页 PDF 应已落盘
                p0 = manager.page_pdf_path(job.id, 0)
                assert p0 is not None and os.path.isfile(p0), p0
                print("(a) 通过：进入 serving，第 0 页 done：", job.page_status)

                # (b) focus(3) 后，第 3 页优先于第 1 页 done
                assert manager.focus(job.id, 3) is True
                ok3 = await _wait_until(lambda: job.page_status[3] == _PAGE_DONE)
                assert ok3, f"焦点第 3 页未按时完成：{job.page_status}"
                assert job.page_status[1] == _PAGE_PENDING, (
                    f"第 3 页应优先于第 1 页，但第 1 页已被翻译：{job.page_status}"
                )
                print("(b) 通过：focus(3) 后第 3 页先于第 1 页 done：", job.page_status)

                # (c) 未 finalize 时不会全量翻完（等待一小段后仍有 pending）
                await asyncio.sleep(0.4)
                assert not job.finalize_requested
                assert any(s == _PAGE_PENDING for s in job.page_status), job.page_status
                assert job.pages_done < job.page_count
                assert job.status == JobPhase.SERVING, job.status
                print("(c) 通过：未 finalize 仍有 pending：", job.page_status)

                # (d) finalize 后全部 done → result.pdf 存在、phase=done
                assert manager.finalize(job.id) is True
                assert manager.finalize(job.id) is True  # 幂等
                ok_done = await _wait_until(
                    lambda: job.status == JobPhase.DONE, timeout=15.0
                )
                assert ok_done, f"finalize 后未到达 done：status={job.status} err={job.error}"
                assert all(s == _PAGE_DONE for s in job.page_status), job.page_status
                assert job.pages_done == job.page_count == 5
                assert abs(job.progress - 1.0) < 1e-9, job.progress
                result_path = os.path.join(job.dir, _RESULT_FILENAME)
                assert os.path.isfile(result_path), result_path
                # result.pdf 页数正确（translated 模式 = 源页数）
                rdoc = pymupdf.open(result_path)
                try:
                    assert rdoc.page_count == 5, rdoc.page_count
                finally:
                    rdoc.close()
                print("(d) 通过：finalize 后全部 done，result.pdf 存在：", result_path)

                # (e) page_pdf_path 对 done 页返回存在的文件
                for n in range(5):
                    p = manager.page_pdf_path(job.id, n)
                    assert p is not None and os.path.isfile(p), (n, p)
                # 越界与未知 job 返回 None
                assert manager.page_pdf_path(job.id, 99) is None
                assert manager.page_pdf_path(job.id, -1) is None
                print("(e) 通过：page_pdf_path 对全部 done 页返回存在文件")

                # 后台任务应已完成并从 _tasks 中移除
                assert await _wait_until(lambda: manager._tasks == set(), timeout=2.0), (
                    manager._tasks
                )
        finally:
            _translator_mod.Translator = _orig_translator

    async def _no_block_page_smoke_test() -> None:
        """回归：含「无可译块页」（空白/纯图片页）时，其单页 PDF 也必须落盘。

        这类页在 extracting 阶段即被标 done、计入 pages_done，但不经调度循环的
        _translate_and_render_page；契约要求 page_pdf_path 对任意 done 页返回真实存在
        的单页 PDF，故 _run 必须为其主动补渲染原样单页。此前缺失该渲染会导致
        page_pdf_path 返回不存在的文件路径，破坏空白/纯图片页预览。
        """
        _orig_translator = _translator_mod.Translator
        _translator_mod.Translator = _FakeTranslator
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                settings = _make_settings(data_dir, prefetch_pages=1)
                manager = JobManager(settings)

                # 现场生成 2 页 PDF：第 0 页有文本，第 1 页完全空白（无可译块）
                doc = pymupdf.open()
                page0 = doc.new_page(width=595, height=842)
                page0.insert_textbox(
                    pymupdf.Rect(50, 60, 545, 160),
                    "Page one has ordinary English text to translate.",
                    fontname="helv",
                    fontsize=12,
                )
                doc.new_page(width=595, height=842)  # 第 1 页留空
                pdf_bytes = doc.tobytes(garbage=3, deflate=True)
                doc.close()

                job = manager.create(pdf_bytes, "blank.pdf", "translated", "auto", False)

                # 空白页在 extracting 阶段即被标 done，且其单页 PDF 必须已落盘
                ok = await _wait_until(
                    lambda: len(job.page_status) == 2
                    and job.page_status[1] == _PAGE_DONE
                )
                assert ok, f"空白页未按时标记 done：{job.page_status}"
                p1 = manager.page_pdf_path(job.id, 1)
                assert p1 is not None and os.path.isfile(p1), (
                    f"空白页单页 PDF 未落盘（page_pdf_path 指向不存在文件）：{p1}"
                )
                # 该单页 PDF 可被 pymupdf 打开且恰为 1 页（原样单页）
                p1doc = pymupdf.open(p1)
                try:
                    assert p1doc.page_count == 1, p1doc.page_count
                finally:
                    p1doc.close()
                print("(f) 通过：空白页单页 PDF 已落盘且可打开：", p1)

                # finalize 收尾，避免后台任务泄漏；有文本页正常翻译到 done
                assert manager.finalize(job.id) is True
                ok_done = await _wait_until(
                    lambda: job.status == JobPhase.DONE, timeout=15.0
                )
                assert ok_done, f"finalize 后未到达 done：status={job.status} err={job.error}"
                # 全部 done 页（含空白页）的单页 PDF 都应存在
                for n in range(2):
                    p = manager.page_pdf_path(job.id, n)
                    assert p is not None and os.path.isfile(p), (n, p)
                assert await _wait_until(lambda: manager._tasks == set(), timeout=2.0), (
                    manager._tasks
                )
        finally:
            _translator_mod.Translator = _orig_translator

    asyncio.run(_scheduling_smoke_test())
    asyncio.run(_no_block_page_smoke_test())

    print("jobs.py 全部冒烟测试通过")
