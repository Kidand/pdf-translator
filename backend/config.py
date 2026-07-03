"""配置模块。

负责：
1. 手动解析项目根目录下的 `.env` 文件（若存在），逐行 `KEY=VALUE`，
   跳过空行与 `#` 注释行；仅当 `os.environ` 中尚不存在该 key 时才注入
   （即真实环境变量优先级高于 `.env` 文件）。
2. 提供 `Settings` dataclass 与 `get_settings()` 工厂函数，供其余模块统一读取配置。

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


def get_settings() -> Settings:
    """读取 .env（若存在）与 os.environ，构造并返回 Settings。

    每次调用都会重新解析 .env 与读取 os.environ（不做全局缓存），
    以便调用方在测试中动态修改环境变量。
    """
    _load_dotenv(_ENV_PATH)

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
    )
    logger.debug("已加载配置：%s", settings)
    return settings


if __name__ == "__main__":
    # 冒烟测试：临时设置环境变量，断言各字段与默认值均符合预期。
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
    ]
    # 备份现有环境变量，测试结束后恢复，避免污染当前进程环境
    _backup = {k: os.environ.get(k) for k in _keys}

    try:
        # --- 场景 1：显式设置全部环境变量，断言覆盖生效 ---
        os.environ["DEEPSEEK_API_KEY"] = "test-key-123"
        os.environ["DEEPSEEK_BASE_URL"] = "https://example.com"
        os.environ["DEEPSEEK_MODEL"] = "test-model"
        os.environ["THINKING_ENABLED"] = "TRUE"
        os.environ["TRANSLATE_CONCURRENCY"] = "9"
        os.environ["BATCH_CHAR_BUDGET"] = "1234"
        os.environ["DATA_DIR"] = "/tmp/custom_data_dir"
        os.environ["LLM_TIMEOUT"] = "12.5"
        os.environ["PREFETCH_PAGES"] = "5"

        s = get_settings()
        assert s.api_key == "test-key-123", s.api_key
        assert s.base_url == "https://example.com", s.base_url
        assert s.model == "test-model", s.model
        assert s.thinking_enabled is True, s.thinking_enabled
        assert s.concurrency == 9, s.concurrency
        assert s.batch_char_budget == 1234, s.batch_char_budget
        assert s.data_dir == "/tmp/custom_data_dir", s.data_dir
        assert s.request_timeout == 12.5, s.request_timeout
        assert s.prefetch_pages == 5, s.prefetch_pages
        print("场景1（显式覆盖）通过：", s)

        # --- 场景 2：布尔值多种写法 ---
        for truthy in ["true", "True", "1", "yes", "YES"]:
            os.environ["THINKING_ENABLED"] = truthy
            assert get_settings().thinking_enabled is True, truthy
        for falsy in ["false", "False", "0", "no", "", "random"]:
            os.environ["THINKING_ENABLED"] = falsy
            assert get_settings().thinking_enabled is False, falsy
        print("场景2（布尔解析）通过")

        # --- 场景 3：未设置的字段使用默认值（选取项目 .env 中不存在的 key）---
        for k in ("DATA_DIR", "BATCH_CHAR_BUDGET", "LLM_TIMEOUT", "PREFETCH_PAGES"):
            os.environ.pop(k, None)
        s2 = get_settings()
        assert s2.data_dir == os.path.join(_PROJECT_ROOT, "data"), s2.data_dir
        assert s2.batch_char_budget == 2200, s2.batch_char_budget
        assert s2.request_timeout == 300.0, s2.request_timeout
        assert s2.prefetch_pages == 3, s2.prefetch_pages
        print("场景3（默认值）通过：", s2)

        # --- 场景 4：prefetch_pages 显式设置为其他整数值时正确解析 ---
        os.environ["PREFETCH_PAGES"] = "10"
        s3 = get_settings()
        assert s3.prefetch_pages == 10, s3.prefetch_pages
        os.environ.pop("PREFETCH_PAGES", None)
        print("场景4（prefetch_pages 解析）通过")

        print("config.py 全部冒烟测试通过")
    finally:
        # 恢复原始环境变量
        for k, v in _backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
