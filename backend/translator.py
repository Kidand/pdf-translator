"""LLM 翻译模块。

负责把 `PdfEngine.extract_blocks()` 产出的 `TextBlock` 列表批量、并发地
翻译为目标语言，遵守 DESIGN.md「translator.py 契约」：

- 跳过无需翻译的块（空白 / 纯数字符号 / 纯 URL）；
- 按字符预算分批，代码块内只翻译注释，代码本身原样保留；
- 用 `openai.AsyncOpenAI` 调用 DeepSeek 兼容接口，`response_format=json_object`；
- 并发受 `Semaphore(settings.concurrency)` 限制，失败重试 3 次（2/4/8s 退避）；
- 最终失败的批次以原文回退，保证流水线不中断。

注意：本模块不在顶层 `import backend.config`（config.py 可能由其他人并行开发，
尚未就绪），只在 `TYPE_CHECKING` 下引入类型，`Translator.__init__` 通过
鸭子类型访问 `settings` 的各字段。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Callable, TYPE_CHECKING

from openai import AsyncOpenAI

from backend.models import TextBlock

if TYPE_CHECKING:  # 仅供类型检查，不在运行时触发真实 import
    from backend.config import Settings

logger = logging.getLogger(__name__)

# progress_cb 签名：Callable[[已完成块数, 送翻译总块数], None]
ProgressCb = Callable[[int, int], None]

# --- 文本判定用正则 --------------------------------------------------------
# CJK：中日韩统一表意文字 + 扩展 A + 兼容表意文字 + 平假名/片假名 + 谚文
_CJK_PATTERN = re.compile(
    "[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)
_LETTER_PATTERN = re.compile(r"[A-Za-z]")
# 纯 URL：整段文本（去空白后）就是一个 http(s):// 或 www. 开头、不含空白的链接
_URL_PATTERN = re.compile(r"^(https?://|www\.)\S+$", re.IGNORECASE)

# 重试退避秒数（第 1/2/3 次失败后依次等待）
_RETRY_DELAYS: tuple[float, ...] = (2.0, 4.0, 8.0)
# 初次尝试 + 最多 3 次重试 = 共 4 次调用，恰好用满 2s/4s/8s 三档退避
_MAX_ATTEMPTS = 4

# 批级上下文（context_before/context_after）字符上限：各自截断到该长度
_CONTEXT_CHAR_LIMIT = 400


def should_skip_text(text: str) -> bool:
    """判断一段文本是否无需翻译（空白 / 无字母无 CJK 的纯符号数字 / 纯 URL）。

    提为模块级函数，供 `Translator._should_skip` 委托，也供 jobs.py 判断
    「页内送翻块」以计算页级上下文时复用（避免两处判定逻辑漂移）。
    """
    stripped = text.strip()
    if not stripped:
        return True
    if not (_LETTER_PATTERN.search(stripped) or _CJK_PATTERN.search(stripped)):
        return True
    if _URL_PATTERN.match(stripped):
        return True
    return False


def _tail(text: str, limit: int) -> str:
    """取字符串尾部至多 limit 个字符（按字符截断，不按词切）。"""
    return text[-limit:] if len(text) > limit else text


def _head(text: str, limit: int) -> str:
    """取字符串头部至多 limit 个字符（按字符截断，不按词切）。"""
    return text[:limit]

_SYSTEM_PROMPT = (
    "You are a professional translator specializing in books, technical manuals, and "
    "software documentation. You receive a JSON object describing a batch of text blocks "
    "extracted from a PDF, and you must translate every item into the requested target "
    "language.\n\n"
    "STRICT OUTPUT FORMAT:\n"
    "- Respond with ONLY a single valid JSON object. No markdown code fences, no extra "
    "commentary before or after the JSON.\n"
    "- The JSON object must have exactly one top-level key \"translations\", whose value "
    "is a JSON object mapping every input item's \"id\" to its translated \"text\".\n"
    "- Every id present in the input \"items\" array MUST appear as a key in "
    "\"translations\"; never omit an id and never invent extra ids.\n\n"
    "TRANSLATION RULES:\n"
    "1. Preserve numbers, proper nouns, and formatting/punctuation symbols as faithfully "
    "as possible.\n"
    "2. If an item has \"code\": true, its text is a source-code block: keep all "
    "identifiers, keywords, string literals, and syntax structure EXACTLY as-is; "
    "translate ONLY the natural-language comment text (for example the text after //, "
    "after #, inside /* ... */, or inside triple-quoted docstrings \"\"\"...\"\"\"), and "
    "leave the comment markers themselves untouched. This applies to EVERY comment in "
    "the block without exception: full-line comments (lines that are entirely a comment, "
    "including a leading comment line above the code), trailing comments at the end of a "
    "code line, block comments, and docstrings must ALL be translated.\n"
    "3. If an item has \"code\": false but its text contains small inline pieces of code, "
    "shell commands, file paths, or URLs, leave those exact pieces untouched and only "
    "translate the surrounding natural-language text.\n"
    "4. Keep the number of lines (split by \\n) in each translated text close to the "
    "number of lines in its original text, so the original page layout is preserved.\n"
    "5. Do not add explanations, notes, disclaimers, or any text beyond the translation "
    "itself.\n"
    "6. The optional \"context_before\" and \"context_after\" fields, when present, are "
    "raw source-text snippets from the adjacent page or the surrounding text. They are "
    "provided ONLY to help you understand sentences split across page boundaries, "
    "resolve pronoun references, and keep terminology consistent. They MUST NOT be "
    "translated and MUST NOT appear as keys or values in the output JSON; translate only "
    "the items in the \"items\" array.\n"
    "7. Treat those context snippets as continuous with the items: the END of "
    "\"context_before\" and the BEGINNING of \"context_after\" may be the two halves of "
    "the SAME sentence, phrase, proper noun, or personal name that was split across a "
    "page or column boundary. When an item's text begins or ends mid-sentence, translate "
    "it as a CONTINUATION so that personal names, proper nouns, and sentence structure "
    "stay semantically coherent across the break; never treat a trailing half-sentence as "
    "a new standalone subject or start a fresh sentence for it. For example, if "
    "\"context_before\" ends with \"...and Viton\" and the item begins with \"Vitanis for "
    "their...\", understand \"Viton Vitanis\" as ONE person's full name spanning the "
    "boundary (not two unrelated words), and render the item's translation accordingly. "
    "This rule never overrides the output-format rules above: still respond with only the "
    "single JSON object and never translate or emit the context snippets themselves.\n"
)


class Translator:
    """封装批处理 + 并发 + 重试的 LLM 翻译器。"""

    def __init__(
        self,
        settings: "Settings",
        thinking: bool | None = None,
        semaphore: asyncio.Semaphore | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """`model`/`base_url`/`api_key` 为 None 时回退到 `settings` 对应字段（每次调用/
        每个 job 可各自覆盖，供 v3「模型覆盖」用：DESIGN.md v3 增补契约「模型覆盖」）。
        生效值分别存 `self.model`/`self.base_url`/`self.api_key`；`api_key` 绝不打日志。
        """
        self.settings = settings
        self.thinking: bool = settings.thinking_enabled if thinking is None else thinking
        self.model: str = settings.model if model is None else model
        self.base_url: str = settings.base_url if base_url is None else base_url
        self.api_key: str = settings.api_key if api_key is None else api_key
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=settings.request_timeout,
        )
        # 外部注入的全局并发限流器（跨 job 共享）；未提供时 translate_blocks 自建一个
        # 仅限本次调用使用的 Semaphore（同一事件循环内，行为与此前一致）。
        self._semaphore = semaphore

    # ------------------------------------------------------------------ #
    # 对外主入口
    # ------------------------------------------------------------------ #
    async def translate_blocks(
        self,
        blocks: list[TextBlock],
        direction: str,
        progress_cb: ProgressCb | None = None,
        context_before: str = "",
        context_after: str = "",
    ) -> dict[str, str]:
        """翻译一批 TextBlock，返回 `{block.key: 译文}`（跳过的块不出现在结果中）。

        `context_before`/`context_after` 是紧邻本页之前/之后的**原文片段**（调用方已截好，
        ≤400 字符），仅供模型理解跨页断句、指代与术语一致；不翻译、不出现在返回 dict 中。
        分批后每批会把页级上下文与同页相邻批的原文拼接成批级上下文（见 `_build_batch_contexts`）。
        """
        to_translate = [b for b in blocks if not self._should_skip(b.text)]
        total = len(to_translate)
        translations: dict[str, str] = {}
        if total == 0:
            return translations

        batches = self._make_batches(to_translate)
        batch_contexts = self._build_batch_contexts(
            to_translate, batches, context_before, context_after
        )
        semaphore = (
            self._semaphore
            if self._semaphore is not None
            else asyncio.Semaphore(max(1, self.settings.concurrency))
        )
        progress_lock = asyncio.Lock()
        done_blocks = 0

        async def run_one(
            batch: list[TextBlock], ctx_before: str, ctx_after: str
        ) -> None:
            nonlocal done_blocks
            async with semaphore:
                batch_result = await self._translate_batch_with_retry(
                    batch, direction, ctx_before, ctx_after
                )
            translations.update(batch_result)
            async with progress_lock:
                done_blocks += len(batch)
                current_done = done_blocks
            if progress_cb is not None:
                progress_cb(current_done, total)

        await asyncio.gather(
            *(
                run_one(batch, ctx_before, ctx_after)
                for batch, (ctx_before, ctx_after) in zip(batches, batch_contexts)
            )
        )
        return translations

    # ------------------------------------------------------------------ #
    # 跳过规则
    # ------------------------------------------------------------------ #
    @staticmethod
    def _should_skip(text: str) -> bool:
        return should_skip_text(text)

    # ------------------------------------------------------------------ #
    # 分批：按字符预算，单块超预算独占一批
    # ------------------------------------------------------------------ #
    def _make_batches(self, blocks: list[TextBlock]) -> list[list[TextBlock]]:
        budget = self.settings.batch_char_budget
        batches: list[list[TextBlock]] = []
        current: list[TextBlock] = []
        current_chars = 0

        for block in blocks:
            block_len = len(block.text)
            if block_len > budget:
                if current:
                    batches.append(current)
                    current, current_chars = [], 0
                batches.append([block])
                continue
            if current and current_chars + block_len > budget:
                batches.append(current)
                current, current_chars = [], 0
            current.append(block)
            current_chars += block_len

        if current:
            batches.append(current)
        return batches

    # ------------------------------------------------------------------ #
    # 批级上下文：页级上下文 + 同页相邻批原文的拼接（各截 ≤400 字符）
    # ------------------------------------------------------------------ #
    def _build_batch_contexts(
        self,
        to_translate: list[TextBlock],
        batches: list[list[TextBlock]],
        context_before: str,
        context_after: str,
    ) -> list[tuple[str, str]]:
        """为每批构造 `(before_i, after_i)` 批级上下文，与 `batches` 一一对应。

        送翻块顺序为 to_translate；`_make_batches` 保持顺序且连续切分，故批 i 恰好覆盖
        其中连续区间 [start, end]：
        - before_i = tail(context_before + 之前各块原文, 400)
        - after_i  = head(之后各块原文 + context_after, 400)

        空串表示该字段为空（`_call_llm` 据此决定不写入 payload）。
        """
        contexts: list[tuple[str, str]] = []
        idx = 0
        for batch in batches:
            start = idx
            end = idx + len(batch) - 1
            preceding = "\n".join(b.text for b in to_translate[:start])
            following = "\n".join(b.text for b in to_translate[end + 1 :])
            before_src = "\n".join(p for p in (context_before, preceding) if p)
            after_src = "\n".join(p for p in (following, context_after) if p)
            contexts.append(
                (
                    _tail(before_src, _CONTEXT_CHAR_LIMIT),
                    _head(after_src, _CONTEXT_CHAR_LIMIT),
                )
            )
            idx = end + 1
        return contexts

    # ------------------------------------------------------------------ #
    # direction=auto 判定：批内 CJK 字符占比 > 0.3 → zh2en，否则 en2zh
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_direction(batch: list[TextBlock], direction: str) -> str:
        if direction in ("en2zh", "zh2en"):
            return direction
        total_chars = sum(len(b.text) for b in batch) or 1
        cjk_chars = sum(len(_CJK_PATTERN.findall(b.text)) for b in batch)
        ratio = cjk_chars / total_chars
        return "zh2en" if ratio > 0.3 else "en2zh"

    # ------------------------------------------------------------------ #
    # 单批翻译 + 重试
    # ------------------------------------------------------------------ #
    async def _translate_batch_with_retry(
        self,
        batch: list[TextBlock],
        direction: str,
        context_before: str = "",
        context_after: str = "",
    ) -> dict[str, str]:
        resolved_direction = self._resolve_direction(batch, direction)
        target_lang = "Chinese" if resolved_direction == "en2zh" else "English"

        last_error: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                raw_content = await self._call_llm(
                    batch, target_lang, context_before, context_after
                )
                parsed = self._parse_response(raw_content)

                result: dict[str, str] = {}
                missing_ids: list[str] = []
                for block in batch:
                    value = parsed.get(block.key)
                    if value is None:
                        missing_ids.append(block.key)
                        continue
                    if not isinstance(value, str):
                        # 非字符串译文（dict/list/number/bool 等）视为格式错误，
                        # 与缺 id 同路径触发该批重试，不做 str() 强转直通。
                        missing_ids.append(block.key)
                        continue
                    result[block.key] = value if value.strip() else block.text
                if missing_ids:
                    raise ValueError(f"response missing/invalid ids: {missing_ids}")
                return result
            except Exception as exc:  # noqa: BLE001 - 统一走重试/兜底逻辑
                last_error = exc
                logger.warning(
                    "translate batch attempt %d/%d failed (keys=%s): %s",
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    [b.key for b in batch],
                    exc,
                )
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_DELAYS[attempt])

        logger.warning(
            "translate batch permanently failed after %d attempts (keys=%s), "
            "falling back to original text: %s",
            _MAX_ATTEMPTS,
            [b.key for b in batch],
            last_error,
        )
        return {b.key: b.text for b in batch}

    # ------------------------------------------------------------------ #
    # 实际 LLM 调用
    # ------------------------------------------------------------------ #
    async def _call_llm(
        self,
        batch: list[TextBlock],
        target_lang: str,
        context_before: str = "",
        context_after: str = "",
    ) -> str:
        items = [
            {"id": block.key, "code": block.is_code, "text": block.text}
            for block in batch
        ]
        user_payload: dict[str, object] = {"target_lang": target_lang, "items": items}
        # 仅在非空时带上上下文字段（空串省略，减少无谓 token）
        if context_before:
            user_payload["context_before"] = context_before
        if context_after:
            user_payload["context_after"] = context_after
        user_content = json.dumps(user_payload, ensure_ascii=False)

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            extra_body={
                "thinking": {"type": "enabled" if self.thinking else "disabled"}
            },
        )
        return response.choices[0].message.content or ""

    # ------------------------------------------------------------------ #
    # 响应解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """剥掉可能存在的 ```json ... ``` / ``` ... ``` markdown 代码围栏。"""
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", stripped)
            if stripped.endswith("```"):
                stripped = stripped[: -3]
        return stripped.strip()

    @classmethod
    def _parse_response(cls, raw_content: str) -> dict[str, str]:
        cleaned = cls._strip_code_fence(raw_content)
        data = json.loads(cleaned)
        translations = data.get("translations")
        if not isinstance(translations, dict):
            raise ValueError("response JSON missing 'translations' object")
        return translations


# ---------------------------------------------------------------------- #
# 冒烟测试：真实调用 API
# ---------------------------------------------------------------------- #
if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _load_dotenv(path: str) -> None:
        """手动解析 .env：逐行 KEY=VALUE，跳过 # 注释，仅在 os.environ 缺失时注入。"""
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value

    _load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

    try:
        from backend.config import get_settings  # type: ignore

        _settings = get_settings()
        logger.info("使用 backend.config.get_settings() 构造的 Settings")
    except Exception as exc:  # config.py 可能尚未由其他人写好，手动构造同构对象
        logger.info("backend.config 不可用（%s），手动构造 Settings 同构对象", exc)

        from dataclasses import dataclass as _dataclass

        @_dataclass
        class _Settings:
            api_key: str
            base_url: str
            model: str
            thinking_enabled: bool
            concurrency: int
            batch_char_budget: int
            data_dir: str
            request_timeout: float

        _settings = _Settings(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            thinking_enabled=os.environ.get("THINKING_ENABLED", "false").strip().lower()
            == "true",
            concurrency=int(os.environ.get("TRANSLATE_CONCURRENCY", "6")),
            batch_char_budget=int(os.environ.get("BATCH_CHAR_BUDGET", "2200")),
            data_dir=os.environ.get("DATA_DIR", os.path.join(_PROJECT_ROOT, "data")),
            request_timeout=float(os.environ.get("LLM_TIMEOUT", "300.0")),
        )

    # --- 模型覆盖：__init__ 的 model/base_url/api_key 解析语义（离线，无需网络）------
    # None → 回退 settings 对应字段；显式传值（含空串这种非 None 但"假值"）→ 直接生效，
    # 不做二次回退（"" 由调用方 jobs.py 决定是否传 None，Translator 自身只认 None 语义）。
    _default_translator = Translator(_settings)
    assert _default_translator.model == _settings.model, _default_translator.model
    assert _default_translator.base_url == _settings.base_url, _default_translator.base_url
    assert _default_translator.api_key == _settings.api_key, _default_translator.api_key

    _override_translator = Translator(
        _settings,
        model="custom-model-x",
        base_url="https://custom.example.com",
        api_key="sk-custom-override-key",
    )
    assert _override_translator.model == "custom-model-x", _override_translator.model
    assert _override_translator.base_url == "https://custom.example.com"
    assert _override_translator.api_key == "sk-custom-override-key"
    # settings 单例本身不应被覆盖调用污染（覆盖只影响本实例）
    assert _settings.model != "custom-model-x"
    print("模型覆盖：__init__ None→settings 回退 / 显式覆盖生效，均通过")

    async def _smoke_test() -> None:
        assert _settings.api_key, "冒烟测试需要 .env 中配置真实 DEEPSEEK_API_KEY"

        blocks = [
            TextBlock(
                page_index=0,
                block_id=0,
                bbox=(0, 0, 200, 20),
                text=(
                    "The quick brown fox jumps over the lazy dog. This paragraph is "
                    "ordinary English prose used to test the translation pipeline."
                ),
                font_size=11.0,
                font_name="Helvetica",
                color="#000000",
                bold=False,
                italic=False,
                is_code=False,
                align="left",
                line_count=1,
            ),
            TextBlock(
                page_index=0,
                block_id=1,
                bbox=(0, 30, 200, 70),
                text=(
                    "def add(a, b):\n"
                    "    # add two numbers and return the result\n"
                    "    return a + b"
                ),
                font_size=10.0,
                font_name="Courier New",
                color="#000000",
                bold=False,
                italic=False,
                is_code=True,
                align="left",
                line_count=3,
            ),
            TextBlock(
                page_index=0,
                block_id=2,
                bbox=(0, 80, 200, 90),
                text="12",
                font_size=9.0,
                font_name="Helvetica",
                color="#000000",
                bold=False,
                italic=False,
                is_code=False,
                align="center",
                line_count=1,
            ),
        ]

        translator = Translator(_settings)

        progress_events: list[tuple[int, int]] = []

        def _progress(done: int, total: int) -> None:
            progress_events.append((done, total))
            print(f"progress: {done}/{total}")

        result = await translator.translate_blocks(
            blocks, direction="en2zh", progress_cb=_progress
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

        # 页码块（纯数字，无字母无 CJK）应被跳过，不进入结果
        assert blocks[2].key not in result, "纯数字页码块应被跳过"

        # 代码块：结构原样保留，注释被翻译为中文
        code_translation = result.get(blocks[1].key, "")
        assert code_translation, "代码块应有译文"
        assert "def add(a, b):" in code_translation, "代码定义行应原样保留"
        assert "return a + b" in code_translation, "代码语句应原样保留"
        assert _CJK_PATTERN.search(code_translation), "代码注释应被翻译为中文"

        # 普通英文段落应翻译为中文
        prose_translation = result.get(blocks[0].key, "")
        assert prose_translation, "普通段落应有译文"
        assert _CJK_PATTERN.search(prose_translation), "普通段落应翻译为中文"

        # progress_cb 应至少被调用一次，且最后一次 total 等于送翻译块数（2，页码块被跳过）
        assert progress_events, "progress_cb 应被调用"
        assert progress_events[-1][1] == 2, "total 应为送翻译的块数（跳过页码块后为 2）"
        assert progress_events[-1][0] == 2, "最终 done_blocks 应等于 total"

        # --- 跨页上下文：上页结尾半句 + 本页开头半句 ---------------------------
        # 本页开头块本身是半句（承接上页结尾），单独翻译易走偏；带上下文应能理解成整句。
        ctx_block = TextBlock(
            page_index=1,
            block_id=0,
            bbox=(0, 0, 200, 20),
            text="element in the array and returns the fully sorted result.",
            font_size=11.0,
            font_name="Helvetica",
            color="#000000",
            bold=False,
            italic=False,
            is_code=False,
            align="left",
            line_count=1,
        )
        ctx_result = await translator.translate_blocks(
            [ctx_block],
            direction="en2zh",
            context_before="The merge step then walks through every ",
            context_after=" This concludes the description of the sorting routine.",
        )
        print(json.dumps(ctx_result, ensure_ascii=False, indent=2))
        ctx_translation = ctx_result.get(ctx_block.key, "")
        assert ctx_translation, "带上下文的跨页半句应有译文"
        assert _CJK_PATTERN.search(ctx_translation), "带上下文的译文应为中文"
        # 上下文原文（英文半句）不得混入译文
        assert "context_before" not in ctx_translation
        assert "This concludes" not in ctx_translation, "context_after 原文不应出现在译文中"
        print("跨页上下文翻译通过：", ctx_translation)

        # --- v3.1 跨页续句：同一人名被跨页拆成两半（"…and Viton" | "Vitanis for…"）------
        # 本段以承接的半句开头，context_before 结尾是同一人名的前半。带上下文时模型应把
        # 「Viton Vitanis」理解为同一人名的连贯续接，而非把「Vitanis」当独立主语另起一句。
        # 弱断言（允许模型波动）：有中文译文 + 不把 context_before 英文原文整段泄漏进译文；
        # 连贯性由打印结果人工核对（对应 fix-context.md「修复前『感谢 Vitanis…』」对比）。
        cont_block = TextBlock(
            page_index=17,
            block_id=2,
            bbox=(0, 0, 400, 20),
            text="Vitanis for their thorough feedback on the drafts.",
            font_size=11.0,
            font_name="Helvetica",
            color="#000000",
            bold=False,
            italic=False,
            is_code=False,
            align="left",
            line_count=1,
        )
        cont_result = await translator.translate_blocks(
            [cont_block],
            direction="en2zh",
            context_before="I would also like to thank the reviewers, and Viton",
        )
        cont_translation = cont_result.get(cont_block.key, "")
        print("跨页续句译文（人名跨页拆分）：", cont_translation)
        assert cont_translation, "跨页续句应有译文"
        assert _CJK_PATTERN.search(cont_translation), "跨页续句译文应为中文"
        # context_before 的英文原文不得整段泄漏进译文
        assert "reviewers, and Viton" not in cont_translation, (
            "context_before 原文不应出现在译文中"
        )
        print("跨页续句翻译通过（连贯性人工核对）")

        print("SMOKE TEST PASSED")

    asyncio.run(_smoke_test())
