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
)


class Translator:
    """封装批处理 + 并发 + 重试的 LLM 翻译器。"""

    def __init__(self, settings: "Settings", thinking: bool | None = None) -> None:
        self.settings = settings
        self.thinking: bool = settings.thinking_enabled if thinking is None else thinking
        self.client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout,
        )

    # ------------------------------------------------------------------ #
    # 对外主入口
    # ------------------------------------------------------------------ #
    async def translate_blocks(
        self,
        blocks: list[TextBlock],
        direction: str,
        progress_cb: ProgressCb | None = None,
    ) -> dict[str, str]:
        """翻译一批 TextBlock，返回 `{block.key: 译文}`（跳过的块不出现在结果中）。"""
        to_translate = [b for b in blocks if not self._should_skip(b.text)]
        total = len(to_translate)
        translations: dict[str, str] = {}
        if total == 0:
            return translations

        batches = self._make_batches(to_translate)
        semaphore = asyncio.Semaphore(max(1, self.settings.concurrency))
        progress_lock = asyncio.Lock()
        done_blocks = 0

        async def run_one(batch: list[TextBlock]) -> None:
            nonlocal done_blocks
            async with semaphore:
                batch_result = await self._translate_batch_with_retry(batch, direction)
            translations.update(batch_result)
            async with progress_lock:
                done_blocks += len(batch)
                current_done = done_blocks
            if progress_cb is not None:
                progress_cb(current_done, total)

        await asyncio.gather(*(run_one(batch) for batch in batches))
        return translations

    # ------------------------------------------------------------------ #
    # 跳过规则
    # ------------------------------------------------------------------ #
    @staticmethod
    def _should_skip(text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return True
        if not (_LETTER_PATTERN.search(stripped) or _CJK_PATTERN.search(stripped)):
            return True
        if _URL_PATTERN.match(stripped):
            return True
        return False

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
        self, batch: list[TextBlock], direction: str
    ) -> dict[str, str]:
        resolved_direction = self._resolve_direction(batch, direction)
        target_lang = "Chinese" if resolved_direction == "en2zh" else "English"

        last_error: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                raw_content = await self._call_llm(batch, target_lang)
                parsed = self._parse_response(raw_content)

                result: dict[str, str] = {}
                missing_ids: list[str] = []
                for block in batch:
                    value = parsed.get(block.key)
                    if value is None:
                        missing_ids.append(block.key)
                        continue
                    text_value = value if isinstance(value, str) else str(value)
                    result[block.key] = text_value if text_value.strip() else block.text
                if missing_ids:
                    raise ValueError(f"response missing ids: {missing_ids}")
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
    async def _call_llm(self, batch: list[TextBlock], target_lang: str) -> str:
        items = [
            {"id": block.key, "code": block.is_code, "text": block.text}
            for block in batch
        ]
        user_payload = {"target_lang": target_lang, "items": items}
        user_content = json.dumps(user_payload, ensure_ascii=False)

        response = await self.client.chat.completions.create(
            model=self.settings.model,
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

        print("SMOKE TEST PASSED")

    asyncio.run(_smoke_test())
