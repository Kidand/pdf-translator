"""共享数据结构：PDF 文本块。

translator / jobs / pdf_engine 均从本模块 import TextBlock，
避免模块间产生环形或时序依赖。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextBlock:
    page_index: int      # 0-based 页码
    block_id: int        # 页内 0-based 序号
    bbox: tuple          # (x0, y0, x1, y1) PDF 坐标
    text: str            # 原文，块内多行以 "\n" 连接
    font_size: float     # 主字号（按字符数取众数）
    font_name: str       # 主字体名
    color: str           # "#rrggbb"
    bold: bool
    italic: bool
    is_code: bool        # 等宽字体启发式
    align: str           # "left" | "center" | "right"
    line_count: int
    # 回填时允许向下扩展的安全余量（pt）：到同页下方最近块之间的空隙，
    # 上限约一行高。用于吸收译文折行带来的高度增长，避免字号被缩小。
    y_expand: float = 0.0
    # 回填时允许向右扩展的安全余量（pt，仅代码块）：CJK 注释使代码行略超
    # 紧贴原文的 bbox 宽度时，用右侧空白吸收，避免代码行折行。
    x_expand: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.page_index}:{self.block_id}"
