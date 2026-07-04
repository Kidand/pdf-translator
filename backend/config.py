"""配置模块。

负责：
1. 手动解析项目根目录下的 `.env` 文件（若存在），逐行 `KEY=VALUE`，
   跳过空行与 `#` 注释行；仅当 `os.environ` 中尚不存在该 key 时才注入
   （即真实环境变量优先级高于 `.env` 文件）。
2. 提供 `Settings` dataclass 与 `get_settings()` 工厂函数——`get_settings()` 是
   **进程内单例**：首次调用解析 `.env`/`os.environ` 构造 `Settings` 并缓存，
   之后每次调用都返回同一个实例（不再重新解析）。
3. 提供 `apply_updates()`：运行时更新单例的部分字段并持久化写回项目根 `.env`，
   供 `/api/config` 等接口调用，实现「改配置不重启进程」。

不引入契约之外的第三方依赖（仅用标准库）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 项目根目录：backend/config.py 的上一级目录
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")

# 布尔真值集合，不区分大小写
_TRUE_VALUES = {"true", "1", "yes"}


def _env_path() -> str:
    """返回项目根 `.env` 的路径。

    单独抽成函数（而非到处直接用 `_ENV_PATH` 常量）是为了给测试留口子：
    测试可以直接重新赋值模块级的 `_env_path` 名字（例如
    `backend.config._env_path = lambda: "/tmp/xxx/.env"`），
    让 `get_settings()`/`apply_updates()` 在不触碰真实项目 `.env` 的前提下
    验证持久化逻辑。
    """
    return _ENV_PATH


def _load_dotenv(path: str) -> None:
    """手动解析 .env 文件并注入 os.environ。

    规则：
    - 跳过空行与以 `#` 开头的注释行；
    - 按第一个 `=` 切分 KEY/VALUE；
    - 若该 KEY 已存在于 os.environ，则不覆盖（环境变量优先）。
    """
    if not os.path.isfile(path):
        logger.info("未找到 .env 文件：%s，跳过加载", path)
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # 去除包裹的引号（若有）
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if not key:
                    continue
                if key in os.environ:
                    continue
                os.environ[key] = value
    except OSError as e:
        logger.warning("读取 .env 文件失败：%s（%s）", path, e)


def _parse_bool(value: str) -> bool:
    """解析布尔字符串："true"/"1"/"yes"（不区分大小写）→ True，其余 → False。"""
    return value.strip().lower() in _TRUE_VALUES


@dataclass
class Settings:
    api_key: str            # env DEEPSEEK_API_KEY
    base_url: str           # env DEEPSEEK_BASE_URL, 默认 "https://api.deepseek.com"
    model: str               # env DEEPSEEK_MODEL, 默认 "deepseek-v4-flash"
    thinking_enabled: bool  # env THINKING_ENABLED, 默认 False（可被每次请求覆盖）
    concurrency: int        # env TRANSLATE_CONCURRENCY, 默认 6（并发 LLM 请求数）
    batch_char_budget: int  # env BATCH_CHAR_BUDGET, 默认 2200（每批原文字符预算）
    data_dir: str            # env DATA_DIR, 默认 "<项目根>/data"
    request_timeout: float  # env LLM_TIMEOUT, 默认 300.0
    prefetch_pages: int      # env PREFETCH_PAGES, 默认 3（焦点页向后预取的页数窗口，v2 架构）
    # env JOB_RETENTION_HOURS, 默认 48：data/jobs 下 mtime 超此时长的任务目录会被惰性清理；
    # <=0 表示永不清理。给出默认值以兼容不显式传该字段的历史构造点。
    job_retention_hours: float = 48.0


def _build_settings() -> Settings:
    """读取 .env（若存在）与 os.environ，构造一个全新的 Settings。

    不做缓存——每次调用都重新解析。`get_settings()` 在此之上加了一层
    进程内单例缓存；本函数保留给测试直接调用，用来反复验证解析逻辑
    （布尔解析、默认值等），而不必绕开 `get_settings()` 的单例缓存。
    """
    _load_dotenv(_env_path())

    default_data_dir = os.path.join(_PROJECT_ROOT, "data")

    settings = Settings(
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        thinking_enabled=_parse_bool(os.environ.get("THINKING_ENABLED", "false")),
        concurrency=int(os.environ.get("TRANSLATE_CONCURRENCY", "6")),
        batch_char_budget=int(os.environ.get("BATCH_CHAR_BUDGET", "2200")),
        data_dir=os.environ.get("DATA_DIR", default_data_dir),
        request_timeout=float(os.environ.get("LLM_TIMEOUT", "300.0")),
        prefetch_pages=int(os.environ.get("PREFETCH_PAGES", "3")),
        job_retention_hours=float(os.environ.get("JOB_RETENTION_HOURS", "48")),
    )
    logger.debug("已加载配置：%s", settings)
    return settings


# 进程内单例缓存；None 表示尚未初始化。
_settings_singleton: Settings | None = None


def get_settings() -> Settings:
    """返回进程内单例 `Settings`。

    首次调用时解析 `.env`（`_env_path()` 指向的文件）与 `os.environ` 构造
    实例并缓存；之后每次调用都直接返回同一个对象，不再重新解析——这样
    `apply_updates()` 对单例字段的原地修改才能被所有持有该实例的调用方
    （main.py / JobManager / Translator 等）立刻感知到。
    """
    global _settings_singleton
    if _settings_singleton is None:
        _settings_singleton = _build_settings()
    return _settings_singleton


# --- 运行时配置更新与持久化 -------------------------------------------------

# 白名单：可通过 apply_updates() 更新的字段名 → 对应 .env 中的 KEY。
_UPDATABLE_ENV_KEYS: dict[str, str] = {
    "api_key": "DEEPSEEK_API_KEY",
    "base_url": "DEEPSEEK_BASE_URL",
    "model": "DEEPSEEK_MODEL",
    "thinking_enabled": "THINKING_ENABLED",
    "concurrency": "TRANSLATE_CONCURRENCY",
}


def _persist_env_updates(env_updates: dict[str, str]) -> None:
    """把 env_updates（env KEY → 新值）合并写回 `.env` 文件。

    规则：
    - 读现有文件的每一行（不存在则视为空文件）；
    - 命中 `KEY=` 开头的行（KEY 与 env_updates 中的某个键相同）就地替换该行，
      其余行（含注释、空行、不相关的 KEY）原样保留、顺序不变；
    - env_updates 中在现有文件里找不到对应行的 key，追加到文件末尾；
    - 值原样写入，不加引号；
    - 原子写：先写 `<path>.tmp` 再 `os.replace` 覆盖原文件，避免中途崩溃损坏配置。

    注意：本函数只接受「env KEY → 值」的字典，调用方（`apply_updates`）负责
    校验合法性；这里不记录任何值到日志，只记录被更新的 KEY 名单。
    """
    path = _env_path()

    try:
        with open(path, "r", encoding="utf-8") as f:
            existing_lines = f.read().splitlines()
    except OSError:
        existing_lines = []

    remaining = dict(env_updates)
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}")
                continue
        new_lines.append(line)
    # 文件中不存在的 key 追加到末尾
    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")

    tmp_path = path + ".tmp"
    content = "\n".join(new_lines)
    if content:
        content += "\n"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    logger.info("已更新 .env 配置项：%s", sorted(env_updates.keys()))


def apply_updates(updates: dict) -> Settings:
    """运行时更新配置单例并持久化写回项目根 `.env`。

    参数 `updates` 的键必须是白名单子集：
    `api_key` / `base_url` / `model` / `thinking_enabled` / `concurrency`。

    校验规则：
    - 出现白名单之外的键 → `ValueError`（消息中文，列出未知键名）；
    - `base_url`：必须是非空字符串，且以 `http` 开头；
    - `model`：必须是非空字符串；
    - `api_key`：必须是字符串；**空串表示「不修改」**，会被直接跳过（不校验、
      不写入、不报错）——这是给前端「留空=沿用现有 key」的场景用的；
      非空时必须是字符串（当前不做非空校验之外的额外限制）；
    - `thinking_enabled`：必须是 `bool`；
    - `concurrency`：必须是 `1~64` 之间的 `int`（`bool` 不算合法的 int）。

    校验采用「先全部校验、再统一生效」：只要 `updates` 中任意一个键值非法，
    整个调用都会在修改单例任何字段之前抛出 `ValueError`，不会出现部分生效。

    合法则原地更新 `get_settings()` 单例对应字段（不是重新构造一个新的
    `Settings` 对象——这样所有已经持有该单例引用的调用方都能立刻看到新值），
    并把变更持久化写回 `.env`，最后返回这个单例。

    若 `updates` 为空、或全部键都是「api_key 空串」这种被跳过的情况，
    则不发生任何字段修改也不触碰 `.env` 文件，直接返回当前单例。
    """
    settings = get_settings()

    unknown_keys = set(updates) - set(_UPDATABLE_ENV_KEYS)
    if unknown_keys:
        raise ValueError(f"不支持更新以下配置项：{', '.join(sorted(unknown_keys))}")

    # 第一遍：只校验、不修改任何状态，计算出待生效的 (属性名, 新值, env KEY, env 值)。
    pending: list[tuple[str, object, str, str]] = []

    if "api_key" in updates:
        value = updates["api_key"]
        if not isinstance(value, str):
            raise ValueError("配置项 api_key 必须是字符串")
        if value != "":
            pending.append(("api_key", value, "DEEPSEEK_API_KEY", value))
        # 空串：约定为「不修改 api_key」，跳过不校验、不报错。

    if "base_url" in updates:
        value = updates["base_url"]
        if not isinstance(value, str) or not value:
            raise ValueError("配置项 base_url 必须是非空字符串")
        if not value.startswith("http"):
            raise ValueError("配置项 base_url 必须以 http 开头")
        pending.append(("base_url", value, "DEEPSEEK_BASE_URL", value))

    if "model" in updates:
        value = updates["model"]
        if not isinstance(value, str) or not value:
            raise ValueError("配置项 model 必须是非空字符串")
        pending.append(("model", value, "DEEPSEEK_MODEL", value))

    if "thinking_enabled" in updates:
        value = updates["thinking_enabled"]
        if not isinstance(value, bool):
            raise ValueError("配置项 thinking_enabled 必须是布尔值")
        pending.append(
            ("thinking_enabled", value, "THINKING_ENABLED", "true" if value else "false")
        )

    if "concurrency" in updates:
        value = updates["concurrency"]
        if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 64):
            raise ValueError("配置项 concurrency 必须是 1~64 之间的整数")
        pending.append(("concurrency", value, "TRANSLATE_CONCURRENCY", str(value)))

    if not pending:
        return settings

    # 第二遍：全部校验通过，统一生效。
    for attr, value, _env_key, _env_value in pending:
        setattr(settings, attr, value)

    env_updates = {env_key: env_value for _, _, env_key, env_value in pending}
    _persist_env_updates(env_updates)

    return settings


if __name__ == "__main__":
    # 冒烟测试：临时设置环境变量，断言各字段与默认值均符合预期；
    # 再验证 get_settings() 的单例缓存语义与 apply_updates() 的校验/持久化逻辑。
    import shutil
    import tempfile

    logging.basicConfig(level=logging.INFO)

    _keys = [
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "THINKING_ENABLED",
        "TRANSLATE_CONCURRENCY",
        "BATCH_CHAR_BUDGET",
        "DATA_DIR",
        "LLM_TIMEOUT",
        "PREFETCH_PAGES",
        "JOB_RETENTION_HOURS",
    ]
    # 备份现有环境变量，测试结束后恢复，避免污染当前进程环境
    _backup = {k: os.environ.get(k) for k in _keys}
    _orig_env_path = _env_path

    try:
        # --- 场景 1：显式设置全部环境变量，断言 _build_settings 解析覆盖生效 ---
        os.environ["DEEPSEEK_API_KEY"] = "test-key-123"
        os.environ["DEEPSEEK_BASE_URL"] = "https://example.com"
        os.environ["DEEPSEEK_MODEL"] = "test-model"
        os.environ["THINKING_ENABLED"] = "TRUE"
        os.environ["TRANSLATE_CONCURRENCY"] = "9"
        os.environ["BATCH_CHAR_BUDGET"] = "1234"
        os.environ["DATA_DIR"] = "/tmp/custom_data_dir"
        os.environ["LLM_TIMEOUT"] = "12.5"
        os.environ["PREFETCH_PAGES"] = "5"
        os.environ["JOB_RETENTION_HOURS"] = "12"

        s = _build_settings()
        assert s.api_key == "test-key-123", s.api_key
        assert s.base_url == "https://example.com", s.base_url
        assert s.model == "test-model", s.model
        assert s.thinking_enabled is True, s.thinking_enabled
        assert s.concurrency == 9, s.concurrency
        assert s.batch_char_budget == 1234, s.batch_char_budget
        assert s.data_dir == "/tmp/custom_data_dir", s.data_dir
        assert s.request_timeout == 12.5, s.request_timeout
        assert s.prefetch_pages == 5, s.prefetch_pages
        assert s.job_retention_hours == 12.0, s.job_retention_hours
        print("场景1（显式覆盖，_build_settings）通过：", s)

        # --- 场景 2：布尔值多种写法 ---
        for truthy in ["true", "True", "1", "yes", "YES"]:
            assert _parse_bool(truthy) is True, truthy
        for falsy in ["false", "False", "0", "no", "", "random"]:
            assert _parse_bool(falsy) is False, falsy
        print("场景2（布尔解析）通过")

        # --- 场景 3：未设置的字段使用默认值（选取项目 .env 中不存在的 key）---
        for k in ("DATA_DIR", "BATCH_CHAR_BUDGET", "LLM_TIMEOUT", "PREFETCH_PAGES",
                  "JOB_RETENTION_HOURS"):
            os.environ.pop(k, None)
        s2 = _build_settings()
        assert s2.data_dir == os.path.join(_PROJECT_ROOT, "data"), s2.data_dir
        assert s2.batch_char_budget == 2200, s2.batch_char_budget
        assert s2.request_timeout == 300.0, s2.request_timeout
        assert s2.prefetch_pages == 3, s2.prefetch_pages
        assert s2.job_retention_hours == 48.0, s2.job_retention_hours
        print("场景3（默认值）通过：", s2)

        # --- 场景 4：prefetch_pages 显式设置为其他整数值时正确解析 ---
        os.environ["PREFETCH_PAGES"] = "10"
        s3 = _build_settings()
        assert s3.prefetch_pages == 10, s3.prefetch_pages
        os.environ.pop("PREFETCH_PAGES", None)
        print("场景4（prefetch_pages 解析）通过")

        # --- 场景 5：get_settings() 的进程内单例缓存语义 ---
        assert _settings_singleton is None, "测试开始前单例应尚未初始化"
        os.environ["DEEPSEEK_MODEL"] = "singleton-first-call"
        first = get_settings()
        assert first.model == "singleton-first-call", first.model
        assert get_settings() is first, "多次调用应返回同一对象"

        # 首次调用之后再改环境变量，单例不应该被影响（不会重新解析 .env/环境变量）
        os.environ["DEEPSEEK_MODEL"] = "should-not-be-picked-up"
        second = get_settings()
        assert second is first, "单例应保持同一对象"
        assert second.model == "singleton-first-call", (
            f"单例不应随后续环境变量变化而改变，实际为 {second.model}"
        )
        print("场景5（get_settings 单例缓存）通过")

        # --- 场景 6：apply_updates() 校验与 .env 持久化 ---
        _tmp_dir = tempfile.mkdtemp(prefix="pdf_translator_config_test_")
        try:
            _fake_env_path = os.path.join(_tmp_dir, ".env")
            with open(_fake_env_path, "w", encoding="utf-8") as f:
                f.write(
                    "# 这是一行注释，应当被保留\n"
                    "FOO=bar\n"
                    "DEEPSEEK_MODEL=old-model\n"
                    "TRANSLATE_CONCURRENCY=6\n"
                    "\n"
                    "BAZ=qux\n"
                )

            # 用测试口子把 _env_path 重定向到临时文件，
            # apply_updates/_persist_env_updates 内部都是通过调用 _env_path() 取路径的，
            # 因此这里直接重新绑定模块级名字即可让二者都读写临时文件。
            globals()["_env_path"] = lambda: _fake_env_path

            # 6a：更新 model + concurrency，单例字段与 .env 文件都应生效
            updated = apply_updates({"model": "new-model", "concurrency": 12})
            assert updated is first, "apply_updates 应原地更新同一个单例，而不是新建对象"
            assert updated.model == "new-model", updated.model
            assert updated.concurrency == 12, updated.concurrency
            assert get_settings().model == "new-model"
            assert get_settings().concurrency == 12

            with open(_fake_env_path, "r", encoding="utf-8") as f:
                _content_after = f.read()
            assert "DEEPSEEK_MODEL=new-model" in _content_after, _content_after
            assert "TRANSLATE_CONCURRENCY=12" in _content_after, _content_after
            assert "FOO=bar" in _content_after, "其他行应被保留"
            assert "BAZ=qux" in _content_after, "其他行应被保留"
            assert "# 这是一行注释，应当被保留" in _content_after, "注释应被保留"
            assert "old-model" not in _content_after, "旧值不应残留"
            # 没有引入陈旧的 .tmp 文件
            assert not os.path.exists(_fake_env_path + ".tmp")
            print("场景6a（model/concurrency 更新 + .env 持久化）通过")

            # 6b：非法值——各类校验失败必须抛 ValueError，且不改动任何已生效字段
            _model_before = get_settings().model
            _concurrency_before = get_settings().concurrency

            try:
                apply_updates({"concurrency": 0})
                raise AssertionError("concurrency=0 应该抛 ValueError")
            except ValueError:
                pass

            try:
                apply_updates({"concurrency": 65})
                raise AssertionError("concurrency=65 应该抛 ValueError")
            except ValueError:
                pass

            try:
                apply_updates({"base_url": "ftp://example.com"})
                raise AssertionError("非 http 开头的 base_url 应该抛 ValueError")
            except ValueError:
                pass

            try:
                apply_updates({"unknown_field": "x"})
                raise AssertionError("白名单之外的键应该抛 ValueError")
            except ValueError:
                pass

            try:
                # 混合一个合法键 + 一个非法键：整体必须原子失败，合法键也不能生效
                apply_updates({"model": "should-not-apply", "concurrency": 0})
                raise AssertionError("混合非法键值时应整体抛 ValueError")
            except ValueError:
                pass

            assert get_settings().model == _model_before, "校验失败不应改动 model"
            assert get_settings().concurrency == _concurrency_before, "校验失败不应改动 concurrency"
            print("场景6b（非法值校验 + 原子性）通过")

            # 6c：api_key 空串跳过不更新；非空则正常更新且不进日志（这里只做行为断言）
            _api_key_before = get_settings().api_key
            apply_updates({"api_key": ""})
            assert get_settings().api_key == _api_key_before, (
                "空串应跳过不更新，api_key 不应改变"
            )
            with open(_fake_env_path, "r", encoding="utf-8") as f:
                _content_no_key = f.read()
            assert "DEEPSEEK_API_KEY" not in _content_no_key, "空串不应写入 .env"

            apply_updates({"api_key": "sk-test-fake-000111"})
            assert get_settings().api_key == "sk-test-fake-000111"
            with open(_fake_env_path, "r", encoding="utf-8") as f:
                _content_with_key = f.read()
            assert "DEEPSEEK_API_KEY=sk-test-fake-000111" in _content_with_key

            # 再次更新 api_key，应该替换已有行而不是重复追加
            apply_updates({"api_key": "sk-test-fake-999888"})
            with open(_fake_env_path, "r", encoding="utf-8") as f:
                _content_key_replaced = f.read()
            assert _content_key_replaced.count("DEEPSEEK_API_KEY=") == 1, _content_key_replaced
            assert "DEEPSEEK_API_KEY=sk-test-fake-999888" in _content_key_replaced
            print("场景6c（api_key 空串跳过 / 非空更新 / 重复更新不重复追加）通过")

            # 6d：thinking_enabled 布尔更新
            apply_updates({"thinking_enabled": True})
            assert get_settings().thinking_enabled is True
            with open(_fake_env_path, "r", encoding="utf-8") as f:
                _content_thinking = f.read()
            assert "THINKING_ENABLED=true" in _content_thinking
            try:
                apply_updates({"thinking_enabled": "true"})  # 字符串而非 bool，应报错
                raise AssertionError("thinking_enabled 传字符串应该抛 ValueError")
            except ValueError:
                pass
            print("场景6d（thinking_enabled 布尔校验）通过")

            # 6e：空更新 / 全部被跳过时不应改动文件
            with open(_fake_env_path, "r", encoding="utf-8") as f:
                _before_noop = f.read()
            apply_updates({})
            apply_updates({"api_key": ""})
            with open(_fake_env_path, "r", encoding="utf-8") as f:
                _after_noop = f.read()
            assert _before_noop == _after_noop, "空更新不应改动 .env 文件"
            print("场景6e（空更新 / 全跳过不落盘）通过")
        finally:
            globals()["_env_path"] = _orig_env_path
            shutil.rmtree(_tmp_dir, ignore_errors=True)

        print("config.py 全部冒烟测试通过")
    finally:
        # 恢复原始环境变量
        for k, v in _backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # 恢复单例，避免影响同进程内后续可能的其他代码（本文件作为脚本运行，
        # 影响范围仅限本次进程，但仍归位以保持整洁）
        _settings_singleton = None
        globals()["_env_path"] = _orig_env_path
