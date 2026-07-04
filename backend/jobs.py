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
import hashlib
import json
import logging
import os
import re
import shutil
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
    # ---- v3 新增字段：上传字节的 sha256（哈希缓存命中判定 + 持久化 rehydrate 依据）----
    file_sha256: str = ""
    # 本 job 创建时是否命中内容哈希缓存文件（预载了历史译文）——供前端一次性提示。
    cache_hit: bool = False
    # ---- v3 新增字段：模型覆盖（DESIGN.md v3 增补契约「模型覆盖」）----
    # 生效模型名 = create() 的 model 覆盖参数（非空时）或 settings.model（默认）；
    # 公开、进 to_dict。base_url/api_key 的覆盖值敏感，只存 _JobRuntime，绝不进这里。
    model: str = ""


@dataclass
class _JobRuntime:
    """每个 Job 的私有协调对象（**不进 to_dict**）。

    - event：调度循环的唤醒事件，focus / finalize / page_pdf_path 命中未译页时 set；
    - blocks_by_page：extract 后按页分组的 TextBlock 缓存；
    - translations：逐页翻译 merge 而成的全量译文 dict；
    - force_pages：待强制重译的页集合（重译请求加入；选页时跳过「缓存已覆盖」判断，
      无条件送 LLM；该页译完后移除）；
    - task：当前 job 的调度任务引用（v3 惰性恢复用以判断「是否已有 _run 在跑」，
      杜绝同一 job 并发两个 _run；None 或已 done 表示可以重新拉起）。
    - base_url/api_key：v3 模型覆盖的敏感覆盖值（create() 透传，空串=不覆盖，由
      _run 构造 Translator 时决定是否回退 settings）——绝不进 Job/to_dict/日志，
      也不随 meta.json 持久化（重启后这两项覆盖天然失效，仅 model 的生效值会持久化）。
    """

    event: asyncio.Event = field(default_factory=asyncio.Event)
    blocks_by_page: dict[int, list[TextBlock]] = field(default_factory=dict)
    translations: dict[str, str] = field(default_factory=dict)
    force_pages: set[int] = field(default_factory=set)
    task: Optional[asyncio.Task] = None
    base_url: str = ""
    api_key: str = ""


# 进度阶段固定值（见 DESIGN.md v2「progress 语义」）
_PROGRESS_EXTRACTING = 0.02
# finalizing 阶段 pages_done/page_count 的上限：cap 到 0.96，确保
# finalizing(<=0.96) → rendering(0.97) → done(1.0) 三段严格单调不回退。
# 若不 cap，页数 >= 34 时 (page_count-1)/page_count 会超过 0.97，导致进入 rendering 时回退。
_PROGRESS_FINALIZING_CAP = 0.96
_PROGRESS_RENDERING = 0.97
_PROGRESS_DONE = 1.0

_SOURCE_FILENAME = "source.pdf"
_RESULT_FILENAME = "result.pdf"
_PAGES_DIRNAME = "pages"
# v3 持久化落盘文件名（原子写：先写 <name>.tmp 再 os.replace）
_META_FILENAME = "meta.json"
_TRANSLATIONS_FILENAME = "translations.json"

# 页级上下文（传给 translate_blocks 的 context_before/after）字符上限
_PAGE_CONTEXT_CHAR_LIMIT = 400


def _page_pdf_filename(page_index: int) -> str:
    return f"page_{page_index}.pdf"


# ----------------------------------------------------------------------------
# v3 内容哈希缓存（data/cache/<sha256>/<direction>__<safe_model>.json）
#
# 同一文件、同一 (direction, model) 的译文跨 job、跨进程复用：命中缓存的页可跳过 LLM
# 直接渲染。缓存文件内容：
#   {"version": 1, "model": ..., "direction": ..., "translations": {key: 译文}}
# 读写辅助为模块级函数（纯文件系统操作，均可放入 asyncio.to_thread；写为原子替换）。
# ----------------------------------------------------------------------------
_CACHE_DIRNAME = "cache"
_CACHE_VERSION = 1


def _safe_model_name(model: str) -> str:
    """把模型名中非 [A-Za-z0-9._-] 的字符替换为 `_`，用作缓存文件名的一部分。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", model)


def _cache_file_path(data_dir: str, file_sha256: str, direction: str, model: str) -> str:
    """内容哈希缓存文件路径：<data_dir>/cache/<sha256>/<direction>__<safe_model>.json。"""
    return os.path.join(
        data_dir,
        _CACHE_DIRNAME,
        file_sha256,
        f"{direction}__{_safe_model_name(model)}.json",
    )


def _load_cache_translations(path: str) -> dict[str, str]:
    """读取缓存文件的 translations 映射；缺失 / 损坏 / 结构非法 → 空 dict（幂等降级）。

    纯文件系统读、无副作用，可安全放入 asyncio.to_thread。
    """
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("载入缓存文件失败，忽略：%s（%s）", path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    translations = data.get("translations")
    if not isinstance(translations, dict):
        return {}
    return {str(k): str(v) for k, v in translations.items()}


def _merge_cache_translations(
    path: str, model: str, direction: str, new_entries: dict[str, str]
) -> None:
    """读-改-写缓存文件（原子）：把 new_entries 合并进已有 translations 后落盘。

    调用方须持有该缓存路径对应的 asyncio.Lock 串行化并发 job 的读-改-写（否则同一文件
    的并发写会互相丢条目）。本函数只做同步文件系统操作，放入 asyncio.to_thread 执行；
    临时文件名带随机后缀 + os.replace，避免中途崩溃损坏缓存文件。
    """
    if not new_entries:
        return
    existing = _load_cache_translations(path)
    existing.update(new_entries)
    payload = {
        "version": _CACHE_VERSION,
        "model": model,
        "direction": direction,
        "translations": existing,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


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
        # 全局共享的 LLM 并发限流器：所有 job 的 Translator 共用同一个 Semaphore，
        # 使跨 job 的在途 LLM 请求总数被 settings.concurrency 封顶（否则 K 个并发 job
        # 会放大为 K×concurrency 个请求，自诱发限流/429 → 静默回退原文的质量事故）。
        # 于 __init__（可能在事件循环启动前）构造：3.10+ 的 Semaphore 延迟绑定事件循环，
        # 首次 acquire 时才绑定，故此处构造安全。
        self._llm_semaphore = asyncio.Semaphore(max(1, settings.concurrency))
        # 内容哈希缓存文件的按路径分锁：串行化同一缓存文件的读-改-写，避免并发 job
        # （同一 sha + direction + model）写回时互相丢条目。惰性创建（见 _cache_lock）。
        self._cache_locks: dict[str, asyncio.Lock] = {}
        # 启动期惰性清理一次过期任务目录（此时无事件循环，只能同步执行）。
        self._cleanup_stale_jobs_blocking()
        # v3 持久化：清理之后扫描 data/jobs/*/meta.json 重建内存 _jobs（进程重启不丢任务、
        # 哈希缓存跨重启可用）。rehydrate 出的 job 不在此处启动调度任务——待 focus /
        # finalize / page_pdf_path 触及时惰性恢复（见 _maybe_recover）。
        self._rehydrate_jobs()

    # ------------------------------------------------------------------
    # 过期任务目录清理（磁盘保留策略）
    # ------------------------------------------------------------------
    def _active_job_ids(self) -> set[str]:
        """当前仍在进行中的 job id 集合（非终态）——清理时保护，避免误删在途任务目录。

        必须在事件循环线程内调用（读 self._jobs 快照），再把结果传给线程内的扫描。
        """
        return {
            jid
            for jid, j in self._jobs.items()
            if j.status not in (JobPhase.DONE, JobPhase.ERROR)
        }

    def _find_stale_job_dirs(self, protected: set[str]) -> list[tuple[str, str]]:
        """扫描 data/jobs，返回 mtime 超龄的 (job_id, dir_path) 列表（纯文件系统读，无副作用）。

        job_retention_hours <= 0 表示不清理；protected 内的 job（在途任务）永不入选。
        可安全放入线程执行：不触碰 self._jobs / self._runtimes。
        """
        retention = self.settings.job_retention_hours
        if retention <= 0:
            return []
        jobs_root = os.path.join(self.settings.data_dir, "jobs")
        if not os.path.isdir(jobs_root):
            return []
        cutoff = time.time() - retention * 3600.0
        stale: list[tuple[str, str]] = []
        try:
            entries = os.listdir(jobs_root)
        except OSError as e:
            logger.warning("扫描任务目录失败：%s（%s）", jobs_root, e)
            return []
        for name in entries:
            if name in protected:
                continue
            dir_path = os.path.join(jobs_root, name)
            try:
                if not os.path.isdir(dir_path):
                    continue
                if os.path.getmtime(dir_path) < cutoff:
                    stale.append((name, dir_path))
            except OSError:
                continue
        return stale

    @staticmethod
    def _rmtree_dirs(stale: list[tuple[str, str]]) -> list[str]:
        """删除给定目录（容错），返回成功删除的 job_id 列表。可放入线程执行。"""
        removed_ids: list[str] = []
        for job_id, dir_path in stale:
            try:
                shutil.rmtree(dir_path)
                removed_ids.append(job_id)
            except OSError as e:  # 容错：单个目录删除失败不影响其余
                logger.warning("清理过期任务目录失败：%s（%s）", dir_path, e)
        return removed_ids

    def _forget_jobs(self, job_ids: list[str]) -> None:
        """从内存 store 移除被清理目录对应的 job 条目（须在事件循环线程内调用）。"""
        for jid in job_ids:
            self._jobs.pop(jid, None)
            self._runtimes.pop(jid, None)

    def _cleanup_stale_jobs_blocking(self) -> None:
        """同步执行一次清理（扫描+删除+移除内存条目）。仅用于无事件循环的启动期。"""
        removed_ids = self._rmtree_dirs(self._find_stale_job_dirs(self._active_job_ids()))
        self._forget_jobs(removed_ids)
        if removed_ids:
            logger.info("清理了 %d 个过期任务目录（保留 %.1fh）",
                        len(removed_ids), self.settings.job_retention_hours)

    async def _cleanup_stale_jobs_async(self) -> None:
        """在事件循环中执行一次清理：扫描/删除放线程，改内存回到循环线程。"""
        protected = self._active_job_ids()
        stale = await asyncio.to_thread(self._find_stale_job_dirs, protected)
        if not stale:
            return
        removed_ids = await asyncio.to_thread(self._rmtree_dirs, stale)
        self._forget_jobs(removed_ids)
        if removed_ids:
            logger.info("清理了 %d 个过期任务目录（保留 %.1fh）",
                        len(removed_ids), self.settings.job_retention_hours)

    # ------------------------------------------------------------------
    # 持久化（meta.json / translations.json 原子写）与 rehydrate
    # ------------------------------------------------------------------
    @staticmethod
    def _atomic_write_json(path: str, obj: object) -> None:
        """原子写 JSON：先写同目录唯一临时文件再 os.replace 覆盖，避免中途崩溃损坏文件。

        临时文件名带随机后缀（`<path>.<uuid>.tmp`）而非固定 `<path>.tmp`：同一 job 的
        meta.json 可能同时被 `_run` 内 await 的 `_persist_meta` 与 `finalize` 触发的
        fire-and-forget `_schedule_persist_meta` 并发写；若共用固定临时名，先完成的
        os.replace 会把临时文件移走，后完成者的 os.replace 便因临时文件已不存在而报
        ENOENT。唯一临时名让两者各写各的临时文件，os.replace 到同一目标即「后者胜」，
        均不报错。纯文件系统操作，可安全放入 asyncio.to_thread。
        """
        tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except OSError:
            # 失败（如目标目录已被删除）时清理可能残留的临时文件，再向上抛给调用方告警。
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    async def _persist_meta(self, job: Job) -> None:
        """把 job 的可序列化快照（含 file_sha256）原子写到 <job.dir>/meta.json。

        每次状态 / 页进度变化时调用；写盘放线程，避免阻塞事件循环。
        """
        data = self.to_dict(job)
        path = os.path.join(job.dir, _META_FILENAME)
        try:
            await asyncio.to_thread(self._atomic_write_json, path, data)
        except OSError as e:  # 落盘失败不应中断翻译流水线，仅告警
            logger.warning("任务 %s 持久化 meta.json 失败：%s", job.id, e)

    async def _persist_translations(self, job: Job, translations: dict[str, str]) -> None:
        """把累计全量译文原子写到 <job.dir>/translations.json（每页译完调用）。

        传入快照（调用方已保证无并发修改），写盘放线程。
        """
        path = os.path.join(job.dir, _TRANSLATIONS_FILENAME)
        snapshot = dict(translations)
        try:
            await asyncio.to_thread(self._atomic_write_json, path, snapshot)
        except OSError as e:
            logger.warning("任务 %s 持久化 translations.json 失败：%s", job.id, e)

    def _schedule_persist_meta(self, job: Job) -> None:
        """从同步上下文（focus/finalize 等）触发一次 meta 落盘。

        有事件循环 → create_task 火后不理（强引用挂到 _tasks 防早 GC）；
        无事件循环（如同步测试）→ 直接同步落盘。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._atomic_write_json(
                    os.path.join(job.dir, _META_FILENAME), self.to_dict(job)
                )
            except OSError as e:
                logger.warning("任务 %s 同步持久化 meta.json 失败：%s", job.id, e)
            return
        task = loop.create_task(self._persist_meta(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _schedule_persist_translations(
        self, job: Job, translations: dict[str, str]
    ) -> None:
        """从同步上下文（retranslate 等）触发一次 translations.json 落盘（与内存对齐）。

        与 `_schedule_persist_meta` 同构：先取快照（`dict(...)`）再落盘，避免后续内存
        修改污染写入内容。有事件循环 → create_task 火后不理（强引用挂到 _tasks 防早 GC）；
        无事件循环（如同步测试）→ 直接同步落盘。retranslate 清空对应页译文后必须调用，
        否则磁盘 translations.json 仍含旧译文，重启 rehydrate 会把它读回、coverage-skip
        复活（force_pages 不持久化）。
        """
        snapshot = dict(translations)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._atomic_write_json(
                    os.path.join(job.dir, _TRANSLATIONS_FILENAME), snapshot
                )
            except OSError as e:
                logger.warning("任务 %s 同步持久化 translations.json 失败：%s", job.id, e)
            return
        task = loop.create_task(self._persist_translations(job, snapshot))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _load_translations(job_dir: str) -> dict[str, str]:
        """从 <job_dir>/translations.json 载入已持久化的全量译文（恢复场景用）。

        文件缺失 / 解析失败 / 结构非法 → 返回空 dict（幂等降级，恢复时最多重译）。
        """
        path = os.path.join(job_dir, _TRANSLATIONS_FILENAME)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning("载入 translations.json 失败，忽略：%s（%s）", path, e)
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def _job_from_meta(self, data: dict, dir_path: str) -> Optional[Job]:
        """从 meta.json 解析出的 dict 重建 Job；字段缺失/非法 → None（跳过该目录）。

        `dir` 一律用实际扫描到的目录路径（而非 meta 里记录的绝对路径），
        以兼容 data_dir 迁移后仍能定位到文件。
        """
        try:
            page_status = [str(s) for s in data.get("page_status", [])]
            # 中断的 translating 中间态在重启后无意义，规整回 pending（恢复时重译该页）
            page_status = [
                _PAGE_PENDING if s == _PAGE_TRANSLATING else s for s in page_status
            ]
            job = Job(
                id=str(data["id"]),
                filename=str(data.get("filename", "")),
                mode=str(data.get("mode", "translated")),
                direction=str(data.get("direction", "auto")),
                thinking=bool(data.get("thinking", False)),
                status=str(data.get("status", JobPhase.SERVING)),
                progress=float(data.get("progress", 0.0)),
                page_count=int(data.get("page_count", 0)),
                total_blocks=int(data.get("total_blocks", 0)),
                done_blocks=int(data.get("done_blocks", 0)),
                error=data.get("error"),
                created_at=float(data.get("created_at", time.time())),
                dir=dir_path,
                page_status=page_status,
                pages_done=sum(1 for s in page_status if s == _PAGE_DONE),
                focus_page=int(data.get("focus_page", 0)),
                finalize_requested=bool(data.get("finalize_requested", False)),
                file_sha256=str(data.get("file_sha256", "")),
                cache_hit=bool(data.get("cache_hit", False)),
                # 旧 meta.json（本字段引入前落盘）缺失/为空 → 回退 settings.model，
                # 保证恢复出的 job.model 恒非空、缓存路径与 Translator 构造不受影响。
                model=str(data.get("model") or self.settings.model),
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("meta.json 字段缺失/非法，跳过目录 %s（%s）", dir_path, e)
            return None
        return job

    def _rehydrate_jobs(self) -> None:
        """扫描 data/jobs/*/meta.json，重建内存 _jobs（同步，仅在 __init__ 调用）。

        - 缺 meta.json 的旧目录（v2 及更早）直接跳过；
        - 非终态 job（extracting/serving/finalizing/rendering）统一修正为 SERVING，
          finalize_requested 原样保留；DONE/ERROR 保持不变；
        - 不在此处启动调度任务（惰性恢复，见 _maybe_recover）。
        """
        jobs_root = os.path.join(self.settings.data_dir, "jobs")
        if not os.path.isdir(jobs_root):
            return
        try:
            entries = os.listdir(jobs_root)
        except OSError as e:
            logger.warning("rehydrate 扫描任务目录失败：%s（%s）", jobs_root, e)
            return

        restored = 0
        for name in entries:
            dir_path = os.path.join(jobs_root, name)
            meta_path = os.path.join(dir_path, _META_FILENAME)
            if not os.path.isfile(meta_path):
                continue  # 无 meta.json 的旧目录：跳过
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                logger.warning("跳过无法解析的 meta.json：%s（%s）", meta_path, e)
                continue
            job = self._job_from_meta(data, dir_path)
            if job is None or job.id in self._jobs:
                continue
            # 非终态修正为 SERVING（重启后需重新恢复调度），finalize_requested 保留
            if job.status not in (JobPhase.DONE, JobPhase.ERROR):
                job.status = JobPhase.SERVING
                self._update_progress(job)
            self._jobs[job.id] = job
            self._runtimes[job.id] = _JobRuntime()
            restored += 1

        if restored:
            logger.info("从磁盘 rehydrate %d 个历史任务", restored)

    # ------------------------------------------------------------------
    # 惰性恢复 / 调度任务启动
    # ------------------------------------------------------------------
    def _start_run(self, job: Job) -> None:
        """启动该 job 的调度任务；已有运行中任务则不重复启动（杜绝同 job 并发 _run）。

        任务引用同时挂到 runtime.task（并发判定用）与 self._tasks（强引用防早 GC）。
        """
        runtime = self._runtimes.get(job.id)
        if runtime is None:
            return
        if runtime.task is not None and not runtime.task.done():
            return
        task = asyncio.create_task(self._run(job))
        runtime.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _maybe_recover(self, job_id: str) -> None:
        """惰性恢复：非终态 job 若无运行中的调度任务则（重新）拉起 _run。

        触发点：focus / finalize / page_pdf_path。用于两类进程内没有活跃调度循环的 job：
          - rehydrate 出的历史任务（重启后从未启动过 _run）；
          - 命中哈希缓存后从 DONE 回退到 SERVING 的任务（原 _run 早已结束）。
        DONE / ERROR 终态不恢复；已有运行中任务的 job 不受影响（不产生第二个 _run）。
        """
        job = self._jobs.get(job_id)
        runtime = self._runtimes.get(job_id)
        if job is None or runtime is None:
            return
        if job.status in (JobPhase.DONE, JobPhase.ERROR):
            return
        if runtime.task is not None and not runtime.task.done():
            return
        logger.info("惰性恢复任务 %s 的调度循环（status=%s）", job_id, job.status)
        self._start_run(job)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    async def create(
        self,
        file_bytes: bytes,
        filename: str,
        mode: str,
        direction: str,
        thinking: bool,
        use_cache: bool = True,
        model: str = "",
        base_url: str = "",
        api_key: str = "",
    ) -> Job:
        """创建新任务：写入 source.pdf，进入 extracting，并调度后台按需翻译循环。

        v3 内容哈希缓存（缓存文件方案）：对上传字节算 sha256；`use_cache` 且存在
        `data/cache/<sha256>/<direction>__<safe_model>.json` 缓存文件时，把其中的历史
        译文**预载**进本 job 的 `runtime.translations` 并置 `job.cache_hit=True`。调度循环
        选到某页时，若该页所有送翻块的 key 均已被预载覆盖，则跳过 LLM 直接渲染标 done
        （见 `_translate_and_render_page`）。每次上传都新建独立 job（不复用旧 job）；缓存的
        复用发生在译文层面（跨 job 共享同一份缓存文件），而非 job 层面。

        v3 模型覆盖（DESIGN.md v3 增补契约「模型覆盖」）：`model`/`base_url`/`api_key`
        均为空串表示「不覆盖」，此时用 `self.settings` 对应值。`model` 的生效值（覆盖或
        默认）存公开字段 `job.model`（进 to_dict，缓存文件路径按它区分）；`base_url`/
        `api_key` 的覆盖值敏感，只存 `_JobRuntime`，绝不进 `Job`/`to_dict`/日志。

        阻塞文件系统操作（makedirs + 写 source.pdf，上传可达 80MB）一律用
        asyncio.to_thread 包装，避免在请求路径上阻塞事件循环；调度逻辑
        （create_task）仍留在事件循环内。创建前先惰性清理过期任务目录。
        """
        # 每次创建前惰性清理过期目录（扫描/删除放线程）。
        await self._cleanup_stale_jobs_async()

        file_sha256 = hashlib.sha256(file_bytes).hexdigest()

        job_id = uuid.uuid4().hex
        job_dir = os.path.join(self.settings.data_dir, "jobs", job_id)
        source_path = os.path.join(job_dir, _SOURCE_FILENAME)

        def _write_source() -> None:
            os.makedirs(job_dir, exist_ok=True)
            with open(source_path, "wb") as f:
                f.write(file_bytes)

        await asyncio.to_thread(_write_source)

        resolved_model = model if model else self.settings.model

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
            file_sha256=file_sha256,
            cache_hit=False,
            model=resolved_model,
        )
        self._jobs[job_id] = job
        # 私有协调对象须在调度任务启动前就位，
        # 使 focus/finalize/page_pdf_path 在 extracting 早期被调用也能拿到 event。
        # base_url/api_key 覆盖（空串=不覆盖）只存这里，绝不进 Job。
        runtime = _JobRuntime(base_url=base_url, api_key=api_key)
        self._runtimes[job_id] = runtime

        # 内容哈希缓存预载：命中缓存文件 → 预载历史译文、标记 cache_hit（文件读放线程）。
        # 缓存路径按 job.model（覆盖或默认的生效模型）区分，而非 self.settings.model。
        if use_cache:
            cache_path = _cache_file_path(
                self.settings.data_dir, file_sha256, direction, job.model
            )
            cached = await asyncio.to_thread(_load_cache_translations, cache_path)
            if cached:
                runtime.translations.update(cached)
                job.cache_hit = True
                job.done_blocks = len(runtime.translations)
                logger.info(
                    "任务 %s 命中内容哈希缓存（预载 %d 条译文，sha=%s…）",
                    job_id, len(cached), file_sha256[:8],
                )

        logger.info(
            "创建任务 %s（%s，mode=%s，direction=%s，model=%s，use_cache=%s，cache_hit=%s，"
            "sha=%s…）",
            job_id, filename, mode, direction, job.model, use_cache, job.cache_hit,
            file_sha256[:8],
        )

        # 初始 meta 落盘（status=extracting），随后调度任务在各阶段自行覆盖重写。
        await self._persist_meta(job)
        self._start_run(job)
        return job

    def _cache_lock(self, path: str) -> asyncio.Lock:
        """获取（惰性创建）某缓存路径对应的 asyncio.Lock，串行化该文件的读-改-写。"""
        lock = self._cache_locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._cache_locks[path] = lock
        return lock

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
        # 触及点惰性恢复：rehydrate / 缓存回退的 job 在此拉起调度循环。
        self._maybe_recover(job_id)
        logger.debug("任务 %s 焦点更新为第 %d 页", job_id, job.focus_page)
        return True

    def finalize(self, job_id: str) -> bool:
        """置 finalize_requested 并唤醒调度器（幂等）；job 不存在返回 False。

        done / finalizing / rendering 等任意存在的状态下重复调用均返回 True。
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        changed = not job.finalize_requested
        if changed:
            logger.info("任务 %s 触发 finalize（全量翻译）", job_id)
        job.finalize_requested = True
        self._wake(job_id)
        # 触及点惰性恢复：rehydrate / 缓存回退（含 DONE→SERVING）的 job 需在此拉起调度循环，
        # 否则 finalize_requested 无人消费、永远不会进入 RENDERING。
        self._maybe_recover(job_id)
        # finalize_requested 变更须持久化：重启 rehydrate 时该标志按契约保留。
        if changed:
            self._schedule_persist_meta(job)
        return True

    def reconfigure(self, settings: Settings) -> None:
        """运行时配置变更（`PUT /api/config`）后由 main.py 调用，使新配置生效。

        `self.settings` 与 main.py 模块级 `settings` 本就是 `config.get_settings()`
        返回的同一个单例对象——`config.apply_updates()` 是原地 `setattr` 修改该单例的
        字段，因此 base_url/model/api_key/thinking_enabled 的新值无需在这里做任何事，
        所有后续读取 `self.settings.xxx` 的地方（包括新建 job 时构造的 `Translator`）
        自动就能看到新值。

        唯一例外是 `concurrency`：全局限流器 `self._llm_semaphore` 是一个
        `asyncio.Semaphore` 实例，其内部计数在构造时就固定了，不支持动态调整，
        所以并发数变化必须整体换一个新的 Semaphore 对象才能生效。

        在途 job：其 `_run` 已经在 SERVING 阶段用旧的 `self._llm_semaphore` 引用构造好
        了 `Translator`（见 `_run` 内 `translator = Translator(..., semaphore=self._llm_semaphore)`），
        这里替换 `self._llm_semaphore` 不会影响它们已经持有的旧对象引用，故「在途 job
        沿用旧 Translator，不受影响」；新建 job 在自己的 `_run` 到达 SERVING 阶段时才会
        读取 `self._llm_semaphore`，因此会拿到这里刚重建的新对象。
        """
        self.settings = settings
        self._llm_semaphore = asyncio.Semaphore(max(1, settings.concurrency))
        logger.info(
            "运行时配置已更新，已重建全局 LLM 并发限流器（concurrency=%d）", settings.concurrency
        )

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
        # 触及点惰性恢复：rehydrate / 缓存回退的 job 在此拉起调度循环去翻这页。
        self._maybe_recover(job_id)
        return None

    def retranslate(
        self, job_id: str, scope: str, page: Optional[int]
    ) -> tuple[bool, str]:
        """页级 / 全量重译：清对应页译文、置回 pending、加入 force_pages、唤醒调度器。

        返回 `(是否成功, 失败原因)`；失败原因供 main.py 路由映射状态码：
          - "not_found"（404）：job 不存在；
          - "invalid_scope"（400）：scope 非 "all"/"page"；
          - "extracting"（409）：页表尚未就绪（仍在提取）；
          - "busy"（409）：job 处于 finalizing/rendering（正在补翻/合成，不接受重译）；
          - "page_out_of_range"（400）：scope=page 时页码非法。

        语义（见 DESIGN.md v3「重译」）：
          - 删除 `runtime.translations` 中对应页（`key.split(":")[0] == str(page)`）的条目、
            `page_status` 置回 pending、加入 `runtime.force_pages`（选页时跳过「缓存已覆盖」
            判断、强制走 LLM、译完移除）、`pages_done` 重算、focus 移到重译页（scope=page）
            或 0，并唤醒调度器；
          - job 已 DONE/ERROR：状态拨回 SERVING、finalize_requested=False、error=None、
            progress 重算，并（经 _maybe_recover）重启 `_run` 恢复模式补翻这些页。
        """
        job = self._jobs.get(job_id)
        runtime = self._runtimes.get(job_id)
        if job is None or runtime is None:
            return (False, "not_found")
        if scope not in ("all", "page"):
            return (False, "invalid_scope")
        if not job.page_status:
            return (False, "extracting")
        # FINALIZING / RENDERING 阶段状态机复杂（正在按序补翻剩余页 / 合成 result.pdf），
        # 若此时接受重译会与 _run 的补翻循环、收尾竞态；直接拒绝，main.py 映射 409。
        # 允许重译的状态：SERVING、DONE、ERROR（后两者仍按下方逻辑拨回 SERVING）。
        if job.status in (JobPhase.FINALIZING, JobPhase.RENDERING):
            return (False, "busy")

        if scope == "page":
            if page is None or not (0 <= page < len(job.page_status)):
                return (False, "page_out_of_range")
            target_pages = [page]
        else:
            target_pages = list(range(len(job.page_status)))

        # 清对应页译文（按 key 前缀即页码）、页状态置回 pending、加入强制重译集合。
        prefixes = {str(p) for p in target_pages}
        for key in [k for k in runtime.translations if k.split(":", 1)[0] in prefixes]:
            runtime.translations.pop(key, None)
        for p in target_pages:
            job.page_status[p] = _PAGE_PENDING
            runtime.force_pages.add(p)

        self._recompute_pages_done(job)
        job.done_blocks = len(runtime.translations)
        job.focus_page = target_pages[0] if scope == "page" else 0

        # 终态：拨回 serving，清 finalize / error，随后由 _maybe_recover 重启调度（恢复模式）。
        if job.status in (JobPhase.DONE, JobPhase.ERROR):
            job.status = JobPhase.SERVING
            job.finalize_requested = False
            job.error = None
        self._update_progress(job)

        logger.info(
            "任务 %s 重译（scope=%s，page=%s）：%d 页置回 pending",
            job_id, scope, page, len(target_pages),
        )
        self._wake(job_id)
        # done/error 已被拨回 serving → 在此重启 _run（恢复模式）；serving 若已有 _run 在跑则
        # _maybe_recover 为空操作，唤醒即可让其消费新的 pending 页。
        self._maybe_recover(job_id)
        self._schedule_persist_meta(job)
        # 同步计划持久化被清空的 translations.json：使磁盘与内存一致（对应页译文已删），
        # 否则重启 rehydrate 会读回旧译文令 coverage-skip 复活（force_pages 不落盘）。
        self._schedule_persist_translations(job, runtime.translations)
        return (True, "")

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
            # 恢复模式判定：runtime 已持有块缓存与页表（in-process 重启，如 retranslate 把
            # DONE 拨回 SERVING 后 _maybe_recover 重启 _run）→ 不重置 page_status/translations、
            # 不重载 translations.json（避免把 retranslate 刚清掉的条目重新读回）；仅重开引擎 +
            # extract_blocks() 填充 engine 内部 _blocks 缓存（render_page_pdf 依赖它），
            # 随后直接进 SERVING 循环。反之（rehydrate 出的历史任务 / 全新任务）走完整重建。
            resume = bool(runtime.blocks_by_page) and bool(job.page_status)

            # ---------------- EXTRACTING（幂等；支持中途恢复） ----------------
            if not resume:
                job.status = JobPhase.EXTRACTING
                job.progress = _PROGRESS_EXTRACTING
            logger.info("任务 %s 开始提取文本块（resume=%s）", job.id, resume)

            engine = await asyncio.to_thread(PdfEngine, source_path)
            job.page_count = engine.page_count
            # extract_blocks 无论是否恢复都要调用一次：render_page_pdf 依赖 engine 内部 _blocks。
            blocks: list[TextBlock] = await asyncio.to_thread(engine.extract_blocks)
            job.total_blocks = len(blocks)
            os.makedirs(pages_dir, exist_ok=True)

            if resume:
                # 恢复模式：沿用已有 page_status / translations / blocks_by_page，仅重算计数。
                self._recompute_pages_done(job)
                job.done_blocks = len(runtime.translations)
                logger.info(
                    "任务 %s 恢复调度（沿用页表：%d/%d 页已就绪）",
                    job.id, job.pages_done, job.page_count,
                )
            else:
                # key 由「页码:块号」决定天然幂等；从 translations.json 载入已有译文；
                # 沿用已持久化的 page_status（已 done 页跳过翻译，仅确保其单页 PDF 存在）。
                runtime.blocks_by_page = {}  # 幂等重建（恢复时避免重复累加）
                for block in blocks:
                    runtime.blocks_by_page.setdefault(block.page_index, []).append(block)

                # 载入已持久化的全量译文（已 done 页据此重渲染，无需再次翻译）；create 阶段
                # 预载的哈希缓存译文（cache_hit）已在 runtime.translations 中，此处 update 叠加。
                loaded = await asyncio.to_thread(self._load_translations, job.dir)
                if loaded:
                    runtime.translations.update(loaded)

                # page_status 与 page_count 对齐：长度不符（新任务 / 页数变化）→ 全部 pending；
                # 长度一致（恢复）→ 沿用，仅把中断的 translating 中间态回退为 pending。
                if len(job.page_status) != job.page_count:
                    job.page_status = [_PAGE_PENDING] * job.page_count
                else:
                    job.page_status = [
                        _PAGE_PENDING if s == _PAGE_TRANSLATING else s
                        for s in job.page_status
                    ]

                # 逐页对齐磁盘：
                #  - 已 done 页：确保单页 PDF 存在，缺失则用已载入译文重渲染（不重新翻译）；
                #  - 未 done 且无可译块的页（空白 / 纯图片）：渲染原样单页并直接标 done。
                # 契约要求 page_pdf_path 对任意 done 页都返回真实存在的单页 PDF，故这两类页都
                # 要主动落盘（render_page_pdf 对无可译块的页按契约返回原样单页）。含可译块的
                # cache_hit 页在此保持 pending，由调度循环 coverage-skip 渲染标 done（浏览到哪
                # 渲到哪，不预渲染全部命中页）。
                for page_index in range(job.page_count):
                    out_path = os.path.join(pages_dir, _page_pdf_filename(page_index))
                    if job.page_status[page_index] == _PAGE_DONE:
                        if not os.path.isfile(out_path):
                            page_bytes = await asyncio.to_thread(
                                engine.render_page_pdf,
                                page_index,
                                dict(runtime.translations),
                            )
                            with open(out_path, "wb") as f:
                                f.write(page_bytes)
                        continue
                    if runtime.blocks_by_page.get(page_index):
                        continue
                    page_bytes = await asyncio.to_thread(
                        engine.render_page_pdf, page_index, dict(runtime.translations)
                    )
                    with open(out_path, "wb") as f:
                        f.write(page_bytes)
                    job.page_status[page_index] = _PAGE_DONE

                # pages_done / done_blocks 以权威来源重算（恢复 / 新建统一口径）
                self._recompute_pages_done(job)
                job.done_blocks = len(runtime.translations)
                logger.info(
                    "任务 %s 提取到 %d 页 / %d 个文本块（%d 页已就绪）",
                    job.id, job.page_count, len(blocks), job.pages_done,
                )

            # 提取/恢复完成即落盘 meta（page_status/page_count 就绪），供重启 rehydrate。
            await self._persist_meta(job)

            # ---------------- SERVING / FINALIZING ----------------
            job.status = JobPhase.SERVING
            self._update_progress(job)
            await self._persist_meta(job)
            # 传入全局共享 semaphore：跨 job 的 LLM 在途请求总数被 settings.concurrency 封顶。
            # v3 模型覆盖：job.model 是已解析的生效模型名（覆盖或 settings.model 默认，
            # 恒非空，直接传即可）；base_url/api_key 覆盖存于 runtime，空串（未覆盖）转
            # None 让 Translator 自行回退 settings 对应值。
            translator = Translator(
                self.settings,
                thinking=job.thinking,
                semaphore=self._llm_semaphore,
                model=job.model,
                base_url=runtime.base_url or None,
                api_key=runtime.api_key or None,
            )

            while True:
                # finalize 一旦请求即进入 finalizing 阶段（按序补翻剩余页）
                if job.finalize_requested and job.status == JobPhase.SERVING:
                    job.status = JobPhase.FINALIZING
                    self._update_progress(job)
                    await self._persist_meta(job)

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
            await self._persist_meta(job)
            logger.info("任务 %s 全部页翻译完成，开始组装输出（mode=%s）", job.id, job.mode)
            await asyncio.to_thread(
                engine.build_output, dict(runtime.translations), job.mode, result_path
            )
            logger.info("任务 %s 输出已写入 %s", job.id, result_path)

            # ---------------- DONE ----------------
            job.status = JobPhase.DONE
            job.progress = _PROGRESS_DONE
            await self._persist_meta(job)
        except Exception as e:  # noqa: BLE001 - 任何异常都要落到任务状态上
            job.status = JobPhase.ERROR
            job.error = str(e)
            logger.exception("任务 %s 执行失败", job.id)
            try:
                await self._persist_meta(job)
            except Exception:  # noqa: BLE001 - 持久化失败不应掩盖原始异常
                logger.exception("任务 %s 持久化 error 状态失败", job.id)
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
        """翻译并渲染单页：按缓存覆盖 / 强制重译决定是否送 LLM → render_page_pdf 落盘。

        决策（页为原子单位，见 DESIGN.md v3「内容哈希缓存」）：
          - 该页所有送翻块（`not should_skip_text`）的 key 均已在 runtime.translations 中
            且不在 force_pages → 跳过 LLM，直接用已有译文（预载缓存 / 之前翻译）渲染标 done；
          - 任一送翻块未覆盖，或该页在 force_pages 中 → 整页所有送翻块送 LLM，译完把该页
            新条目 merge 写回哈希缓存文件（同目录临时文件 + os.replace，按缓存路径分锁）。

        一个 job 同一时刻只翻译一页（页内批次由 Translator 内部并发）；translate 失败时
        Translator 已内部兜底回退原文，调度器无需特判。
        """
        # 延迟 import：与 _run 内的 Translator/PdfEngine import 保持一致，使 jobs.py 顶层
        # 不依赖 translator（模块可独立 import/测试）。
        from backend.translator import should_skip_text

        job.page_status[page_index] = _PAGE_TRANSLATING
        page_blocks = runtime.blocks_by_page.get(page_index, [])
        # 送翻块：跳过不该翻译的块（页码/URL/纯符号）后剩余的块。
        send_blocks = [b for b in page_blocks if not should_skip_text(b.text)]
        forced = page_index in runtime.force_pages
        # 缓存覆盖判定：该页所有送翻块的 key 均已在累计译文（预载缓存 / 之前翻译）中。
        covered = bool(send_blocks) and all(
            b.key in runtime.translations for b in send_blocks
        )

        did_translate = False
        if send_blocks and (forced or not covered):
            # 页级跨页上下文（原文片段，不翻译，仅供模型理解跨页断句/指代/术语）：
            #  - context_before = 上一页最后一个送翻块文本的尾部（≤400 字符）
            #  - context_after  = 下一页第一个送翻块文本的头部（≤400 字符）
            # 相邻页无块 / 无送翻块 → 空串。数据源 runtime.blocks_by_page（extract 阶段已就绪）。
            context_before = ""
            for prev_block in reversed(runtime.blocks_by_page.get(page_index - 1, [])):
                if not should_skip_text(prev_block.text):
                    context_before = prev_block.text[-_PAGE_CONTEXT_CHAR_LIMIT:]
                    break

            context_after = ""
            for next_block in runtime.blocks_by_page.get(page_index + 1, []):
                if not should_skip_text(next_block.text):
                    context_after = next_block.text[:_PAGE_CONTEXT_CHAR_LIMIT]
                    break

            page_translations = await translator.translate_blocks(
                page_blocks,
                job.direction,
                progress_cb=None,
                context_before=context_before,
                context_after=context_after,
            )
            # 复检点 1：translate_blocks 的 await 期间若被 retranslate 抢占——唯一能把
            # page_status[page] 从 translating 改走的并发路径就是它（拨回 pending 并加入
            # force_pages）——本次结果作废：不写译文、不清 force、不写单页 PDF、不标 done、
            # 不 persist，且不改 done_blocks/pages_done（保留 retranslate 重算后的值）。
            # 调度器随后会重新 pick 这个 pending（且 forced）页重新翻译。
            if job.page_status[page_index] != _PAGE_TRANSLATING:
                logger.info(
                    "任务 %s 第 %d 页翻译在 await 期间被重译抢占，放弃本次结果", job.id, page_index
                )
                return
            runtime.translations.update(page_translations)
            job.done_blocks = len(runtime.translations)
            did_translate = True

        # 渲染单页译文 PDF（同步 PDF 操作放到线程，避免阻塞事件循环）；传 translations 浅拷贝
        # 快照，避免线程内 pdf_engine 逐 key 读 dict 时被事件循环线程的 retranslate.pop 并发
        # 改写导致 KeyError（任务误判 ERROR）。
        page_bytes = await asyncio.to_thread(
            engine.render_page_pdf, page_index, dict(runtime.translations)
        )
        # 复检点 2：渲染 await 期间若被 retranslate 抢占，同样作废——不写文件、不清 force、
        # 不标 done、不 persist（保留 retranslate 刚加的 force 标记供调度器重新翻译该页）。
        if job.page_status[page_index] != _PAGE_TRANSLATING:
            logger.info(
                "任务 %s 第 %d 页渲染在 await 期间被重译抢占，放弃本次结果", job.id, page_index
            )
            return

        # 两处复检均通过、确认本次要标 done：此刻才移出强制重译集合（无论是否真的送 LLM——
        # 无送翻块的强制页也在此清标记）。放到成功路径末端，避免被抢占时误清 retranslate
        # 刚加的 force 标记。
        runtime.force_pages.discard(page_index)

        out_path = os.path.join(pages_dir, _page_pdf_filename(page_index))
        with open(out_path, "wb") as f:
            f.write(page_bytes)

        job.page_status[page_index] = _PAGE_DONE
        # pages_done 每次从 page_status 重算（重译会把 done 页拨回 pending，增量计数会算错）。
        self._recompute_pages_done(job)
        self._update_progress(job)
        # 每页完成先写全量 translations.json，再写 meta.json：保证 meta 标注的 done 页
        # 始终有对应译文与单页 PDF 落盘，崩溃恢复时不会用缺失译文重渲染。
        await self._persist_translations(job, runtime.translations)
        await self._persist_meta(job)

        # 确有新增译文时把该页条目 merge 写回哈希缓存文件（命中缓存跳过 LLM 的页无需重复落盘）。
        if did_translate:
            page_entries = {
                b.key: runtime.translations[b.key]
                for b in send_blocks
                if b.key in runtime.translations
            }
            if page_entries:
                cache_path = _cache_file_path(
                    self.settings.data_dir, job.file_sha256, job.direction, job.model
                )
                async with self._cache_lock(cache_path):
                    await asyncio.to_thread(
                        _merge_cache_translations,
                        cache_path,
                        job.model,
                        job.direction,
                        page_entries,
                    )

        logger.info(
            "任务 %s 第 %d 页已完成（%d/%d，%s）",
            job.id, page_index, job.pages_done, job.page_count,
            "LLM" if did_translate else "cache/skip",
        )

    @staticmethod
    def _recompute_pages_done(job: Job) -> None:
        """从 page_status 重算 pages_done（重译会把 done 页拨回 pending，增量计数会算错）。"""
        job.pages_done = sum(1 for s in job.page_status if s == _PAGE_DONE)

    def _update_progress(self, job: Job) -> None:
        """按 v2 progress 语义刷新 job.progress（error 阶段保持不变）。"""
        if job.status == JobPhase.EXTRACTING:
            job.progress = _PROGRESS_EXTRACTING
        elif job.status == JobPhase.SERVING:
            # serving 阶段进度仅供展示，不参与 finalize→done 的单调性约束。
            job.progress = (job.pages_done / job.page_count) if job.page_count else 1.0
        elif job.status == JobPhase.FINALIZING:
            # cap 到 0.96，保证进入 rendering(0.97) 时进度不回退（页数多时 pages_done/page_count
            # 会逼近 1.0，若不 cap 会高于 0.97 造成 finalizing→rendering 的回退闪烁）。
            ratio = (job.pages_done / job.page_count) if job.page_count else 1.0
            job.progress = min(ratio, _PROGRESS_FINALIZING_CAP)
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
            # v3 新增
            "file_sha256": job.file_sha256,
            "cache_hit": job.cache_hit,
            "model": job.model,
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
            "file_sha256", "cache_hit", "model",
        }
        assert set(d.keys()) == expected_keys, set(d.keys())
        assert d["page_status"] == ["done", "done", "done"]
        assert d["pages_done"] == 3
        assert d["focus_page"] == 1
        assert d["finalize_requested"] is True
        assert d["file_sha256"] == ""  # 未显式设置时为空串（默认值）
        assert d["cache_hit"] is False  # 未显式设置时为 False（默认值）
        assert d["model"] == ""  # 未显式设置时为空串（默认值）
        assert "base_url" not in d and "api_key" not in d, (
            "base_url/api_key 覆盖值绝不能进 to_dict"
        )
        print("to_dict 字段齐全（含 v2/v3 新字段）：", d)

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

        def __init__(
            self,
            settings: Settings,
            thinking: bool | None = None,
            semaphore: "asyncio.Semaphore | None" = None,
            model: str | None = None,
            base_url: str | None = None,
            api_key: str | None = None,
        ) -> None:
            self.settings = settings
            self.semaphore = semaphore
            # 与真实 Translator 同构的 None→settings 回退语义，供模型覆盖测试断言。
            self.model = settings.model if model is None else model
            self.base_url = settings.base_url if base_url is None else base_url
            self.api_key = settings.api_key if api_key is None else api_key

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
                job = await manager.create(pdf_bytes, "test.pdf", "translated", "auto", False)

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

                job = await manager.create(pdf_bytes, "blank.pdf", "translated", "auto", False)

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

    async def _context_smoke_test() -> None:
        """跨页上下文：调度器为每页调用 translate_blocks 时应传入正确的页级上下文。

        用一个「记录收到的 context_before/after」的桩替换 Translator，跑满 3 页文档后断言：
          * 第 0 页无上一页 → context_before 为空；有下一页 → context_after 非空；
          * 第 1 页前后都有页 → 两者均非空（spec 的核心断言）；
          * 末页（第 2 页）无下一页 → context_after 为空。
        """
        records: dict[int, tuple[str, str]] = {}

        class _RecordingTranslator(_FakeTranslator):
            async def translate_blocks(
                self, blocks, direction, progress_cb=None,
                context_before="", context_after="",
            ):
                if blocks:
                    records[blocks[0].page_index] = (context_before, context_after)
                return {b.key: "译文" + b.key for b in blocks}

        _orig_translator = _translator_mod.Translator
        _translator_mod.Translator = _RecordingTranslator
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                settings = _make_settings(data_dir, prefetch_pages=1)
                manager = JobManager(settings)

                pdf_bytes = _make_test_pdf_bytes(3)
                job = await manager.create(pdf_bytes, "ctx.pdf", "translated", "auto", False)

                # finalize 强制全量翻译，确保三页都经过 translate_blocks
                assert manager.finalize(job.id) is True
                ok_done = await _wait_until(
                    lambda: job.status == JobPhase.DONE, timeout=15.0
                )
                assert ok_done, f"finalize 后未到达 done：status={job.status} err={job.error}"

                assert set(records) == {0, 1, 2}, records.keys()
                cb0, ca0 = records[0]
                cb1, ca1 = records[1]
                cb2, ca2 = records[2]
                # 第 0 页：无上一页 → before 空；有下一页 → after 非空
                assert cb0 == "", f"第 0 页 context_before 应为空：{cb0!r}"
                assert ca0 != "", f"第 0 页 context_after 应非空：{ca0!r}"
                # 第 1 页：前后都有页 → 两者均非空
                assert cb1 != "", f"第 1 页 context_before 应非空：{cb1!r}"
                assert ca1 != "", f"第 1 页 context_after 应非空：{ca1!r}"
                # 末页：无下一页 → after 空
                assert cb2 != "", f"第 2 页 context_before 应非空：{cb2!r}"
                assert ca2 == "", f"第 2 页 context_after 应为空：{ca2!r}"
                # 长度约束 ≤400 字符
                assert all(
                    len(v) <= _PAGE_CONTEXT_CHAR_LIMIT
                    for v in (cb0, ca0, cb1, ca1, cb2, ca2)
                )
                print("(g) 通过：页级上下文按前后页正确注入：", records)

                assert await _wait_until(lambda: manager._tasks == set(), timeout=2.0), (
                    manager._tasks
                )
        finally:
            _translator_mod.Translator = _orig_translator

    async def _drain_tasks(manager: JobManager) -> None:
        """取消并回收 manager 的全部后台任务，模拟进程退出（供 rehydrate 测试用）。"""
        for t in list(manager._tasks):
            t.cancel()
        if manager._tasks:
            await asyncio.gather(*list(manager._tasks), return_exceptions=True)

    async def _cache_smoke_test() -> None:
        """(h) 内容哈希缓存（缓存文件方案）：首个 job 翻页写回缓存文件；同文件 + 同
        direction + 同 model 二次 create 命中缓存文件（新建 job、cache_hit=True、页秒 done、
        未 finalize）；use_cache=False 不预载；不同 direction 独立缓存不互相命中。"""
        _orig_translator = _translator_mod.Translator
        _translator_mod.Translator = _FakeTranslator
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                # 焦点窗口覆盖全部页，命中缓存的页可被逐一选中并秒 done（无需 finalize）。
                settings = _make_settings(data_dir, prefetch_pages=5)
                manager = JobManager(settings)
                pdf_bytes = _make_test_pdf_bytes(3)

                # 首个 job：无缓存文件，cache_hit=False。finalize 到 DONE 使缓存文件含全部页。
                job1 = await manager.create(pdf_bytes, "same.pdf", "translated", "auto", False)
                assert job1.file_sha256, "file_sha256 应已计算"
                assert job1.cache_hit is False, "首次上传无缓存文件，不应命中"
                assert manager.finalize(job1.id) is True
                assert await _wait_until(
                    lambda: job1.status == JobPhase.DONE, timeout=15.0
                ), f"job1 未到 done：{job1.status} err={job1.error}"

                cache_path = _cache_file_path(data_dir, job1.file_sha256, "auto", settings.model)
                assert os.path.isfile(cache_path), f"翻译后应写回缓存文件：{cache_path}"
                cached_tr = _load_cache_translations(cache_path)
                assert len(cached_tr) >= 3, cached_tr  # 每页至少一个可译块
                assert all(":" in k for k in cached_tr), list(cached_tr)[:3]
                print("(h1) 通过：首个 job 翻译并写回缓存文件（%d 条）" % len(cached_tr))

                # 二次 create（同参 + use_cache）→ 新建独立 job，命中缓存文件。
                job2 = await manager.create(pdf_bytes, "same.pdf", "translated", "auto", False)
                assert job2.id != job1.id, "缓存文件方案：二次上传应新建独立 job"
                assert job2.file_sha256 == job1.file_sha256
                assert job2.cache_hit is True, "二次上传应命中缓存文件 cache_hit=True"
                # 未 finalize，命中缓存的页也应秒 done（跳过 LLM 直接渲染）。
                assert await _wait_until(
                    lambda: len(job2.page_status) == 3
                    and all(s == _PAGE_DONE for s in job2.page_status),
                    timeout=8.0,
                ), f"命中缓存的页未秒 done：{job2.page_status}"
                assert job2.finalize_requested is False, "命中缓存无需 finalize 即可全部 done"
                assert job2.status == JobPhase.SERVING, job2.status
                print("(h2) 通过：二次上传新建 job、cache_hit=True、页秒 done（未 finalize）")

                # use_cache=False → 不预载缓存，cache_hit=False。
                job3 = await manager.create(
                    pdf_bytes, "same.pdf", "translated", "auto", False, use_cache=False
                )
                assert job3.cache_hit is False, "use_cache=False 不应预载缓存"
                print("(h3) 通过：use_cache=False 不命中缓存")

                # 不同 direction → 独立缓存文件，不命中（cache_hit=False）。
                job4 = await manager.create(pdf_bytes, "same.pdf", "translated", "en2zh", False)
                assert job4.cache_hit is False, "不同 direction 应使用独立缓存文件，不互相命中"
                print("(h4) 通过：direction 区分缓存文件")

                # 收尾其余任务，避免后台任务泄漏；等 _tasks 自然清空（含后台持久化写盘）
                for j in (job2, job3, job4):
                    manager.finalize(j.id)
                await _wait_until(
                    lambda: all(
                        manager.get(j.id).status == JobPhase.DONE
                        for j in (job2, job3, job4)
                    ),
                    timeout=15.0,
                )
                await _wait_until(lambda: manager._tasks == set(), timeout=3.0)
        finally:
            _translator_mod.Translator = _orig_translator

    async def _retranslate_smoke_test() -> None:
        """(k) 重译：page / all 校验与状态映射；DONE→SERVING 恢复模式补翻；force_pages
        跳过缓存覆盖强制重走 LLM；重译后可再 finalize 到 done。"""
        calls = {"count": 0}

        class _CountingTranslator(_FakeTranslator):
            async def translate_blocks(
                self, blocks, direction, progress_cb=None,
                context_before="", context_after="",
            ):
                if blocks:
                    calls["count"] += 1
                return {b.key: "译文" + b.key for b in blocks}

        _orig_translator = _translator_mod.Translator
        _translator_mod.Translator = _CountingTranslator
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                settings = _make_settings(data_dir, prefetch_pages=5)
                manager = JobManager(settings)
                pdf_bytes = _make_test_pdf_bytes(3)

                job = await manager.create(pdf_bytes, "re.pdf", "translated", "auto", False)
                assert manager.finalize(job.id) is True
                assert await _wait_until(
                    lambda: job.status == JobPhase.DONE, timeout=15.0
                ), f"未到 done：{job.status} err={job.error}"

                # 校验：未知 job / 非法 scope / 越界页
                assert manager.retranslate("nope", "all", None) == (False, "not_found")
                assert manager.retranslate(job.id, "bogus", None) == (False, "invalid_scope")
                ok, reason = manager.retranslate(job.id, "page", 99)
                assert (ok, reason) == (False, "page_out_of_range"), (ok, reason)
                ok, reason = manager.retranslate(job.id, "page", -1)
                assert (ok, reason) == (False, "page_out_of_range"), (ok, reason)

                # retranslate(page=1)：DONE → 同步拨回 serving，页 1 回 pending（其余保持 done）
                calls_before = calls["count"]
                ok, reason = manager.retranslate(job.id, "page", 1)
                assert ok is True, reason
                assert job.status == JobPhase.SERVING
                assert job.finalize_requested is False
                assert job.page_status[1] == _PAGE_PENDING, job.page_status
                assert job.page_status[0] == _PAGE_DONE and job.page_status[2] == _PAGE_DONE
                assert job.focus_page == 1
                # 恢复模式调度：页 1 重新 done，且确有再次调用 LLM（force_pages 强制重走）
                assert await _wait_until(
                    lambda: job.page_status[1] == _PAGE_DONE, timeout=10.0
                ), f"重译页未回 done：{job.page_status}"
                assert calls["count"] > calls_before, "force_pages 应强制重走 LLM"
                assert job.page_status[0] == _PAGE_DONE and job.page_status[2] == _PAGE_DONE
                print("(k1) 通过：retranslate(page) 后 done→serving，页回 pending→done，force 重走 LLM")

                # retranslate(all)：全部页置回 pending（同步观察，随后调度补翻）
                ok, reason = manager.retranslate(job.id, "all", None)
                assert ok is True, reason
                assert all(s == _PAGE_PENDING for s in job.page_status), job.page_status
                assert job.focus_page == 0
                assert await _wait_until(
                    lambda: all(s == _PAGE_DONE for s in job.page_status), timeout=10.0
                ), f"全部重译后未全 done：{job.page_status}"
                # 再 finalize → 重新合成到 DONE
                assert manager.finalize(job.id) is True
                assert await _wait_until(
                    lambda: job.status == JobPhase.DONE, timeout=15.0
                ), f"重译后未再到 done：{job.status} err={job.error}"
                result_path = os.path.join(job.dir, _RESULT_FILENAME)
                assert os.path.isfile(result_path), result_path
                print("(k2) 通过：retranslate(all) 后 serving，可再 finalize 到 done")

                # extracting 阶段（页表未就绪）→ 返回 extracting
                job_x = await manager.create(pdf_bytes, "re2.pdf", "translated", "auto", False)
                job_x.page_status = []  # 人为制造「页表未就绪」以确定性覆盖该分支
                assert manager.retranslate(job_x.id, "all", None) == (False, "extracting")
                manager.finalize(job_x.id)
                await _wait_until(
                    lambda: manager.get(job_x.id).status == JobPhase.DONE, timeout=15.0
                )
                print("(k3) 通过：extracting 阶段重译返回 extracting")

                # (k4) FINALIZING / RENDERING 态拒绝重译，返回 busy。用已 DONE 的 job
                # 手动置临时状态（此时其 _run 已退出，不会与调度循环竞态），逐一断言后复原。
                for st in (JobPhase.FINALIZING, JobPhase.RENDERING):
                    job.status = st
                    assert manager.retranslate(job.id, "page", 1) == (False, "busy"), st
                    assert manager.retranslate(job.id, "all", None) == (False, "busy"), st
                job.status = JobPhase.DONE  # 复原，避免影响收尾
                print("(k4) 通过：FINALIZING/RENDERING 态 retranslate 返回 busy")

                await _drain_tasks(manager)
        finally:
            _translator_mod.Translator = _orig_translator

    async def _retranslate_inflight_smoke_test() -> None:
        """(l) in-flight 重译不被静默吞：某页卡在 translate_blocks（挂在 asyncio.Event 上）
        期间对该页 retranslate，释放后该页必须被**重新翻译**（translate 调用计数 +1），
        page_status 最终 done、translations 含该页新译文、force_pages 不残留。

        这是修复「retranslate 落进 _translate_and_render_page 的 await 窗口、in-flight 结果
        顶替重译」竞态的核心回归：若无 post-await 复检，卡住的旧调用返回后会把该页标 done
        并清掉 force，重译被静默吞。
        """
        gate = asyncio.Event()
        calls = {"page0": 0}

        class _GatedTranslator(_FakeTranslator):
            async def translate_blocks(
                self, blocks, direction, progress_cb=None,
                context_before="", context_after="",
            ):
                if blocks and blocks[0].page_index == 0:
                    calls["page0"] += 1
                # 第 0 页的调用挂在 gate 上模拟 in-flight；gate 被 set 后所有调用直通。
                await gate.wait()
                return {b.key: "译文" + b.key for b in blocks}

        _orig_translator = _translator_mod.Translator
        _translator_mod.Translator = _GatedTranslator
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                # prefetch_pages=1：只翻焦点页，确保第 0 页先被卡住、其余页不并行占用调度。
                settings = _make_settings(data_dir, prefetch_pages=1)
                manager = JobManager(settings)
                pdf_bytes = _make_test_pdf_bytes(3)
                job = await manager.create(
                    pdf_bytes, "inflight.pdf", "translated", "auto", False
                )
                runtime = manager._runtimes[job.id]

                # 等第 0 页进入 in-flight：page_status=translating 且已进入 translate_blocks。
                assert await _wait_until(
                    lambda: len(job.page_status) == 3
                    and job.page_status[0] == _PAGE_TRANSLATING
                    and calls["page0"] == 1,
                    timeout=10.0,
                ), f"第 0 页未进入 in-flight：status={job.page_status} calls={calls}"

                # in-flight 期间对第 0 页 retranslate：同步置回 pending + 加入 force_pages。
                ok, reason = manager.retranslate(job.id, "page", 0)
                assert (ok, reason) == (True, ""), (ok, reason)
                assert job.page_status[0] == _PAGE_PENDING, job.page_status
                assert 0 in runtime.force_pages, runtime.force_pages

                calls_before = calls["page0"]  # == 1
                gate.set()  # 释放：卡住的旧调用返回后应被复检点 1 丢弃，随后调度器重译第 0 页。

                assert await _wait_until(
                    lambda: job.page_status[0] == _PAGE_DONE, timeout=10.0
                ), f"重译页未回 done（被静默吞？）：{job.page_status}"
                # 核心断言：第 0 页确被重新翻译（计数 +1），未被 in-flight 旧结果静默顶替。
                assert calls["page0"] > calls_before, (
                    f"第 0 页应被重新翻译（force 重走 LLM），calls={calls}"
                )
                # force_pages 不残留、translations 含第 0 页新译文、单页 PDF 落盘。
                assert 0 not in runtime.force_pages, runtime.force_pages
                assert any(k.split(":", 1)[0] == "0" for k in runtime.translations), (
                    list(runtime.translations)[:5]
                )
                p0 = manager.page_pdf_path(job.id, 0)
                assert p0 is not None and os.path.isfile(p0), p0
                print("(l) 通过：in-flight 重译不被静默吞，第 0 页被重新翻译并落盘")

                await _drain_tasks(manager)
        finally:
            _translator_mod.Translator = _orig_translator
            gate.set()  # 兜底：确保任何遗留 await 不会悬挂

    async def _persistence_smoke_test() -> None:
        """(i) 持久化：meta.json / translations.json 落盘存在且可解析，内容与 job 一致。"""
        _orig_translator = _translator_mod.Translator
        _translator_mod.Translator = _FakeTranslator
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                settings = _make_settings(data_dir, prefetch_pages=1)
                manager = JobManager(settings)
                pdf_bytes = _make_test_pdf_bytes(3)
                job = await manager.create(pdf_bytes, "persist.pdf", "translated", "auto", False)
                assert manager.finalize(job.id) is True
                assert await _wait_until(
                    lambda: job.status == JobPhase.DONE, timeout=15.0
                ), f"未到 done：{job.status} err={job.error}"

                meta_path = os.path.join(job.dir, _META_FILENAME)
                tr_path = os.path.join(job.dir, _TRANSLATIONS_FILENAME)
                assert os.path.isfile(meta_path), meta_path
                assert os.path.isfile(tr_path), tr_path

                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                assert meta["id"] == job.id
                assert meta["file_sha256"] == job.file_sha256
                assert meta["status"] == JobPhase.DONE
                assert meta["page_status"] == [_PAGE_DONE] * 3, meta["page_status"]

                with open(tr_path, encoding="utf-8") as f:
                    tr = json.load(f)
                assert isinstance(tr, dict) and len(tr) >= 3, tr  # 每页至少一个可译块
                # translations.json 的 key 应为「页码:块号」形式
                assert all(":" in k for k in tr), list(tr)[:3]
                print("(i) 通过：meta.json / translations.json 落盘且可解析：", len(tr), "条译文")

                await _wait_until(lambda: manager._tasks == set(), timeout=3.0)
        finally:
            _translator_mod.Translator = _orig_translator

    async def _rehydrate_smoke_test() -> None:
        """(j) 模拟重启：新建 JobManager（同 data_dir）→ rehydrate 出 job；
        rehydrate 不立即启动调度任务；focus 触发惰性恢复；最终 finalize 到 done。"""
        _orig_translator = _translator_mod.Translator
        _translator_mod.Translator = _FakeTranslator
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                settings = _make_settings(data_dir, prefetch_pages=1)

                # ---- 进程 A：创建任务，等提取完成（meta 落盘），不 finalize（留有 pending 页）----
                manager_a = JobManager(settings)
                pdf_bytes = _make_test_pdf_bytes(5)
                job_a = await manager_a.create(
                    pdf_bytes, "restart.pdf", "translated", "auto", False
                )
                sha = job_a.file_sha256
                job_id = job_a.id
                meta_path = os.path.join(job_a.dir, _META_FILENAME)

                def _meta_extracted() -> bool:
                    try:
                        with open(meta_path, encoding="utf-8") as f:
                            return json.load(f).get("page_count") == 5
                    except (OSError, ValueError):
                        return False

                assert await _wait_until(_meta_extracted), "提取后 meta.json 未落盘"
                # 模拟进程退出：取消 A 的全部后台任务
                await _drain_tasks(manager_a)

                # ---- 进程 B：同 data_dir 新建 manager → rehydrate ----
                manager_b = JobManager(settings)
                rj = manager_b.get(job_id)
                assert rj is not None, "rehydrate 应恢复出历史任务"
                assert rj.status == JobPhase.SERVING, f"非终态应修正为 serving：{rj.status}"
                assert rj.file_sha256 == sha, (rj.file_sha256, sha)
                assert rj.page_count == 5, rj.page_count
                # rehydrate 不立即启动调度任务
                assert manager_b._runtimes[job_id].task is None, "rehydrate 不应启动调度任务"

                # focus 触发惰性恢复（启动 _run）
                assert manager_b.focus(job_id, 0) is True
                assert await _wait_until(
                    lambda: manager_b._runtimes[job_id].task is not None
                    and not manager_b._runtimes[job_id].task.done(),
                    timeout=5.0,
                ), "focus 应惰性拉起调度任务"

                # finalize → 恢复后的调度循环补翻剩余页并合成 → done
                assert manager_b.finalize(job_id) is True
                assert await _wait_until(
                    lambda: rj.status == JobPhase.DONE, timeout=20.0
                ), f"恢复后未到 done：status={rj.status} err={rj.error}"
                assert all(s == _PAGE_DONE for s in rj.page_status), rj.page_status
                result_path = os.path.join(rj.dir, _RESULT_FILENAME)
                assert os.path.isfile(result_path), result_path
                rdoc = pymupdf.open(result_path)
                try:
                    assert rdoc.page_count == 5, rdoc.page_count
                finally:
                    rdoc.close()
                # 全部 done 页的单页 PDF 都应存在（含恢复期补渲染的页）
                for n in range(5):
                    p = manager_b.page_pdf_path(job_id, n)
                    assert p is not None and os.path.isfile(p), (n, p)
                print("(j) 通过：rehydrate + 惰性恢复 + finalize 到 done")

                await _wait_until(lambda: manager_b._tasks == set(), timeout=3.0)
        finally:
            _translator_mod.Translator = _orig_translator

    async def _model_override_smoke_test() -> None:
        """(m) 模型覆盖（DESIGN.md v3 增补契约「模型覆盖」）：
        - 不传覆盖 → job.model 落回 settings.model，Translator 收到的 model/base_url/
          api_key 与 settings 一致；
        - 传 model/base_url/api_key 覆盖 → job.model 为覆盖值（进 to_dict），Translator
          收到覆盖后的三项；base_url/api_key 绝不出现在 to_dict 中；
        - 缓存文件路径按 job.model（而非 settings.model）区分，覆盖模型写入独立缓存文件。
        """
        captured: list[dict] = []

        class _RecordingTranslator(_FakeTranslator):
            def __init__(self, settings, thinking=None, semaphore=None,
                         model=None, base_url=None, api_key=None):
                super().__init__(settings, thinking, semaphore, model, base_url, api_key)
                captured.append(
                    {"model": self.model, "base_url": self.base_url, "api_key": self.api_key}
                )

        _orig_translator = _translator_mod.Translator
        _translator_mod.Translator = _RecordingTranslator
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                settings = _make_settings(data_dir, prefetch_pages=5)
                manager = JobManager(settings)
                pdf_bytes = _make_test_pdf_bytes(2)

                # 不覆盖：job.model 落回 settings.model；Translator 收到 settings 三项原值。
                job_default = await manager.create(
                    pdf_bytes, "override_d.pdf", "translated", "auto", False
                )
                assert job_default.model == settings.model, job_default.model
                assert manager.finalize(job_default.id) is True
                assert await _wait_until(
                    lambda: job_default.status == JobPhase.DONE, timeout=15.0
                ), f"未到 done：{job_default.status} err={job_default.error}"
                assert captured, "Translator 应已被构造并记录"
                assert captured[-1]["model"] == settings.model
                assert captured[-1]["base_url"] == settings.base_url
                assert captured[-1]["api_key"] == settings.api_key
                print("(m1) 通过：不覆盖时 job.model 落回 settings.model")

                # 覆盖：model/base_url/api_key 均生效；base_url/api_key 绝不进 to_dict。
                job_ov = await manager.create(
                    pdf_bytes, "override_o.pdf", "translated", "auto", False,
                    model="custom-model-x",
                    base_url="https://custom.example.com",
                    api_key="sk-custom-secret-999",
                )
                assert job_ov.model == "custom-model-x", job_ov.model
                d = JobManager.to_dict(job_ov)
                assert d["model"] == "custom-model-x"
                assert "base_url" not in d and "api_key" not in d, (
                    "base_url/api_key 覆盖值绝不能进 to_dict：" + str(d)
                )
                assert manager.finalize(job_ov.id) is True
                assert await _wait_until(
                    lambda: job_ov.status == JobPhase.DONE, timeout=15.0
                ), f"未到 done：{job_ov.status} err={job_ov.error}"
                assert captured[-1]["model"] == "custom-model-x"
                assert captured[-1]["base_url"] == "https://custom.example.com"
                assert captured[-1]["api_key"] == "sk-custom-secret-999"
                print("(m2) 通过：model/base_url/api_key 覆盖均生效，且不进 to_dict")

                # 缓存文件路径按 job.model 区分：覆盖模型独立缓存文件已写回。
                cache_path_override = _cache_file_path(
                    data_dir, job_ov.file_sha256, "auto", job_ov.model
                )
                assert os.path.isfile(cache_path_override), (
                    "覆盖模型应写入按 job.model 区分的独立缓存文件：" + cache_path_override
                )
                cached = _load_cache_translations(cache_path_override)
                assert cached, cached
                print("(m3) 通过：缓存文件路径按 job.model 区分")

                await _wait_until(lambda: manager._tasks == set(), timeout=3.0)
        finally:
            _translator_mod.Translator = _orig_translator

    asyncio.run(_scheduling_smoke_test())
    asyncio.run(_no_block_page_smoke_test())
    asyncio.run(_context_smoke_test())
    asyncio.run(_cache_smoke_test())
    asyncio.run(_retranslate_smoke_test())
    asyncio.run(_retranslate_inflight_smoke_test())
    asyncio.run(_persistence_smoke_test())
    asyncio.run(_rehydrate_smoke_test())
    asyncio.run(_model_override_smoke_test())

    print("jobs.py 全部冒烟测试通过")
