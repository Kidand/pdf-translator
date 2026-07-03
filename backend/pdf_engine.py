"""PDF 解析、译文回填、输出组装。

本模块是版式保留的核心：
  * extract_blocks —— 用 PyMuPDF 的 dict 结构按 block 聚合出 TextBlock 列表，
    记录字号 / 字体 / 颜色 / 粗斜体 / 对齐等版式信息。
  * build_output —— 在源文档的干净副本上，对有译文的块做 redaction（保住图片与
    矢量线条）再用 insert_htmlbox 回填译文，支持「纯译文」与「原文/译文交错」两种输出。

PyMuPDF 1.28，统一 `import pymupdf`（不用旧的 `import fitz`）。
"""
from __future__ import annotations

import html
import logging
from collections import defaultdict
from typing import Optional

import pymupdf

from backend.models import TextBlock

logger = logging.getLogger("backend.pdf_engine")

# 等宽 / 代码字体启发式关键字（字体名小写后包含任一即视为代码）
_CODE_FONT_MARKERS = (
    "mono",
    "courier",
    "consol",
    "menlo",
    "code",
    "mplus",
    "nimbusmono",
)

# span flags 位：bit4=bold(16)，bit1=italic(2)
_FLAG_BOLD = 16
_FLAG_ITALIC = 2

# redaction 时每边向内收缩量（pt），避免误删相邻内容
_REDACT_SHRINK = 0.5


def _int_to_hex(color: int) -> str:
    """PyMuPDF span 的 sRGB 整数颜色 → "#rrggbb"。"""
    return "#{:06x}".format(int(color) & 0xFFFFFF)


def _weighted_mode(counter: dict) -> object:
    """返回权重（字符数）最大的 key；空则返回 None。"""
    if not counter:
        return None
    return max(counter.items(), key=lambda kv: kv[1])[0]


def _is_code_font(font_name: str) -> bool:
    """字体名（小写）含等宽 / 代码关键字则判为代码。"""
    low = (font_name or "").lower()
    return any(marker in low for marker in _CODE_FONT_MARKERS)


class PdfEngine:
    """封装单个 PDF 的解析与译文回填。"""

    def __init__(self, src_path: str) -> None:
        self.src_path: str = src_path
        # 提取用文档：只读，不做修改，供 interleaved 输出复用原始页
        self.doc: pymupdf.Document = pymupdf.open(src_path)
        self.page_count: int = self.doc.page_count
        # extract_blocks 结果缓存，供 build_output 复用（保证 key 一致）
        self._blocks: Optional[list[TextBlock]] = None
        logger.info("PdfEngine 打开 %s，共 %d 页", src_path, self.page_count)

    # ------------------------------------------------------------------ #
    # 提取
    # ------------------------------------------------------------------ #
    def extract_blocks(self) -> list[TextBlock]:
        """按 block 聚合文本，产出 TextBlock 列表并缓存。"""
        blocks: list[TextBlock] = []
        for page_index in range(self.page_count):
            page = self.doc[page_index]
            data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT)
            block_id = 0  # 页内 0-based，仅对保留的文本块递增
            for raw in data.get("blocks", []):
                # 忽略非文本块（type!=0：图片等）
                if raw.get("type", 0) != 0:
                    continue
                # MuPDF 可能把同一水平带上相距很远的文字（图示标签、表格单元格）
                # 合并进同一 block；先按垂直流拆分，避免译文全部挤进一个 bbox。
                for group in self._split_side_by_side_lines(raw):
                    gx0 = min(l["bbox"][0] for l in group)
                    gy0 = min(l["bbox"][1] for l in group)
                    gx1 = max(l["bbox"][2] for l in group)
                    gy1 = max(l["bbox"][3] for l in group)
                    tb = self._build_block(
                        page_index, block_id, {"lines": group, "bbox": (gx0, gy0, gx1, gy1)}
                    )
                    if tb is None:  # 空白块跳过
                        continue
                    blocks.append(tb)
                    block_id += 1
        self._blocks = blocks
        logger.info("提取到 %d 个文本块", len(blocks))
        return blocks

    @staticmethod
    def _split_side_by_side_lines(raw: dict) -> list[list[dict]]:
        """把 raw block 的 lines 按垂直流分组。

        正常段落的行是自上而下堆叠的（相邻行几乎无垂直重叠）；
        若下一行与上一行的 y 区间重叠超过较矮行高的一半，说明两行
        其实是同一水平带上左右并排的独立文字（图示标签、表格单元格等），
        应各自独立成块，否则译文会被整体塞进合并后的 bbox 造成错位。
        """
        groups: list[list[dict]] = []
        for line in raw.get("lines", []):
            if not line.get("spans") or not line.get("bbox"):
                continue
            if groups:
                prev_bbox = groups[-1][-1]["bbox"]
                py0, py1 = prev_bbox[1], prev_bbox[3]
                ly0, ly1 = line["bbox"][1], line["bbox"][3]
                v_overlap = min(py1, ly1) - max(py0, ly0)
                min_height = max(min(py1 - py0, ly1 - ly0), 1e-3)
                if v_overlap > 0.5 * min_height:
                    groups.append([line])  # 与上一行并排 → 独立成组
                else:
                    groups[-1].append(line)
            else:
                groups.append([line])
        return groups

    def _build_block(
        self, page_index: int, block_id: int, raw: dict
    ) -> Optional[TextBlock]:
        """从 get_text('dict') 的单个 block 构造 TextBlock；空白块返回 None。"""
        line_texts: list[str] = []
        line_bboxes: list[tuple] = []

        # 按字符数加权统计版式属性
        size_w: dict[float, int] = defaultdict(int)
        font_w: dict[str, int] = defaultdict(int)
        color_w: dict[int, int] = defaultdict(int)
        bold_chars = 0
        italic_chars = 0
        total_chars = 0

        for line in raw.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            parts: list[str] = []
            for span in spans:
                text = span.get("text", "")
                parts.append(text)
                n = len(text)
                if n == 0:
                    continue
                size_w[round(float(span.get("size", 0.0)), 2)] += n
                font_w[span.get("font", "")] += n
                color_w[int(span.get("color", 0))] += n
                flags = int(span.get("flags", 0))
                if flags & _FLAG_BOLD:
                    bold_chars += n
                if flags & _FLAG_ITALIC:
                    italic_chars += n
                total_chars += n
            line_texts.append("".join(parts))
            line_bboxes.append(tuple(line.get("bbox", raw.get("bbox"))))

        text = "\n".join(line_texts)
        if not text.strip():
            return None

        font_size = _weighted_mode(size_w) or 0.0
        font_name = _weighted_mode(font_w) or ""
        color_int = _weighted_mode(color_w)
        color = _int_to_hex(color_int if color_int is not None else 0)
        bold = total_chars > 0 and bold_chars * 2 > total_chars
        italic = total_chars > 0 and italic_chars * 2 > total_chars
        is_code = _is_code_font(font_name)

        bbox = tuple(raw.get("bbox"))
        align = self._detect_align(bbox, line_bboxes)

        return TextBlock(
            page_index=page_index,
            block_id=block_id,
            bbox=bbox,
            text=text,
            font_size=float(font_size),
            font_name=font_name,
            color=color,
            bold=bold,
            italic=italic,
            is_code=is_code,
            align=align,
            line_count=len(line_texts),
        )

    @staticmethod
    def _detect_align(bbox: tuple, line_bboxes: list[tuple]) -> str:
        """启发式判断对齐：各行中心相对 bbox 中心 → center；右缘对齐 → right；默认 left。"""
        if len(line_bboxes) < 2:
            return "left"
        bx0, _, bx1, _ = bbox
        block_center = (bx0 + bx1) / 2.0
        tol = 3.0  # pt 容差

        center_offsets = [abs((lx0 + lx1) / 2.0 - block_center) for lx0, _, lx1, _ in line_bboxes]
        left_gaps = [lx0 - bx0 for lx0, _, _, _ in line_bboxes]
        right_gaps = [bx1 - lx1 for _, _, lx1, _ in line_bboxes]

        max_left = max(left_gaps)
        # 居中：所有行中心都贴近块中心，且并非整体左贴边（存在明显左缩进）
        if all(c <= tol for c in center_offsets) and max_left > tol:
            return "center"
        # 右对齐：所有行右缘贴近块右缘，且左缘参差（有行左缩进明显）
        if all(r <= tol for r in right_gaps) and max_left > tol:
            return "right"
        return "left"

    # ------------------------------------------------------------------ #
    # 回填 / 输出
    # ------------------------------------------------------------------ #
    def _blocks_for_build(self) -> list[TextBlock]:
        """build_output 用的块列表：优先复用缓存，未提取则现提取。"""
        if self._blocks is None:
            self.extract_blocks()
        return self._blocks or []

    @staticmethod
    def _build_html(block: TextBlock, translation: str) -> str:
        """构造 insert_htmlbox 用的 HTML（inline style，含字号/颜色/对齐/粗斜体）。"""
        style_parts = [
            f"font-size:{block.font_size:.1f}pt",
            f"color:{block.color}",
            f"text-align:{block.align}",
            "line-height:1.25",
            "margin:0",
        ]
        if block.bold:
            style_parts.append("font-weight:bold")
        if block.italic:
            style_parts.append("font-style:italic")

        escaped = html.escape(translation)
        if block.is_code:
            # 代码块：<pre> + white-space:pre-wrap 保留原始换行
            style = ";".join(["font-family:monospace", "white-space:pre-wrap", *style_parts])
            return f'<pre style="{style}">{escaped}</pre>'
        # 普通块：\n → <br>
        style = ";".join(style_parts)
        body = escaped.replace("\n", "<br>")
        return f'<div style="{style}">{body}</div>'

    def _shrink_rect(self, bbox: tuple) -> pymupdf.Rect:
        """redaction rect：bbox 各边向内收缩 0.5pt；过小则退回原始 bbox。"""
        x0, y0, x1, y1 = bbox
        sx0, sy0 = x0 + _REDACT_SHRINK, y0 + _REDACT_SHRINK
        sx1, sy1 = x1 - _REDACT_SHRINK, y1 - _REDACT_SHRINK
        if sx1 <= sx0 or sy1 <= sy0:
            return pymupdf.Rect(x0, y0, x1, y1)
        return pymupdf.Rect(sx0, sy0, sx1, sy1)

    def _build_translated_doc(self, translations: dict[str, str]) -> pymupdf.Document:
        """在源文档干净副本上做 redaction + 译文回填，返回该文档。"""
        doc = pymupdf.open(self.src_path)

        # 按页收集需要回填的块（key 在 translations 且译文非空）
        by_page: dict[int, list[TextBlock]] = defaultdict(list)
        for block in self._blocks_for_build():
            translation = translations.get(block.key)
            if translation is None or not translation.strip():
                continue  # 不在 dict 或空译文：保持原样
            by_page[block.page_index].append(block)

        for page_index in range(doc.page_count):
            page_blocks = by_page.get(page_index)
            if not page_blocks:
                continue
            page = doc[page_index]

            # 第一遍：加 redact 注解并统一 apply（保住图片与矢量线条）
            for block in page_blocks:
                page.add_redact_annot(self._shrink_rect(block.bbox))
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            )

            # 第二遍：用原始 bbox 回填译文
            for block in page_blocks:
                rect = pymupdf.Rect(*block.bbox)
                html_content = self._build_html(block, translations[block.key])
                ret = page.insert_htmlbox(rect, html_content, scale_low=0.1)
                # insert_htmlbox 返回 (spare_height, scale)；spare_height<0 表示放不下，已尽力缩小，忽略
                spare = ret[0] if isinstance(ret, (tuple, list)) else ret
                if spare is not None and spare < 0:
                    logger.debug("块 %s 译文放不下（spare=%.2f），已尽力缩小", block.key, spare)

        return doc

    def build_output(self, translations: dict[str, str], mode: str, out_path: str) -> None:
        """组装输出文档并保存。

        mode:
          "translated"  —— 纯译文。
          "interleaved" —— 原文第 i 页、译文第 i 页交替。
        """
        if mode not in ("translated", "interleaved"):
            raise ValueError(f"未知 mode: {mode!r}")

        translated_doc = self._build_translated_doc(translations)
        try:
            if mode == "translated":
                translated_doc.save(out_path, garbage=3, deflate=True)
                logger.info("已保存纯译文文档：%s（%d 页）", out_path, translated_doc.page_count)
            else:  # interleaved
                out_doc = pymupdf.open()
                try:
                    for i in range(self.page_count):
                        out_doc.insert_pdf(self.doc, from_page=i, to_page=i)          # 原文页
                        out_doc.insert_pdf(translated_doc, from_page=i, to_page=i)    # 译文页
                    out_doc.save(out_path, garbage=3, deflate=True)
                    logger.info(
                        "已保存交错文档：%s（%d 页）", out_path, out_doc.page_count
                    )
                finally:
                    out_doc.close()
        finally:
            translated_doc.close()

    def close(self) -> None:
        """关闭底层文档。"""
        try:
            self.doc.close()
        except Exception:  # noqa: BLE001 —— 关闭幂等，异常仅记录
            logger.debug("关闭文档时出现异常", exc_info=True)


# ---------------------------------------------------------------------- #
# 冒烟测试
# ---------------------------------------------------------------------- #
def _make_test_pdf(path: str) -> None:
    """现场生成 3 页测试 PDF：普通段落 + Courier 代码块 + 矩形图形。"""
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page(width=595, height=842)  # A4
        # 普通段落
        page.insert_textbox(
            pymupdf.Rect(50, 60, 545, 140),
            f"This is a normal paragraph on page {i + 1}. "
            "It contains several English words to translate.",
            fontname="helv",
            fontsize=12,
        )
        # Courier 代码块
        page.insert_textbox(
            pymupdf.Rect(50, 170, 545, 260),
            "def add(a, b):\n    # add two numbers\n    return a + b",
            fontname="cour",
            fontsize=11,
        )
        # 矩形图形（矢量线条）
        page.draw_rect(
            pymupdf.Rect(50, 300, 300, 420),
            color=(0, 0, 1),
            fill=(0.85, 0.92, 1.0),
            width=1.5,
        )
    doc.save(path, garbage=3, deflate=True)
    doc.close()


def _smoke_test() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    src = "/tmp/pdfeng_test.pdf"
    out_translated = "/tmp/pdfeng_translated.pdf"
    out_interleaved = "/tmp/pdfeng_interleaved.pdf"
    _make_test_pdf(src)

    engine = PdfEngine(src)
    try:
        blocks = engine.extract_blocks()
        assert blocks, "应提取到文本块"
        assert engine.page_count == 3, f"源应为 3 页，实为 {engine.page_count}"

        # 校验代码块被识别
        code_blocks = [b for b in blocks if b.is_code]
        assert code_blocks, "应识别出 Courier 代码块（is_code=True）"
        assert any("def add" in b.text for b in code_blocks), "代码块文本应含 'def add'"
        print(f"提取 {len(blocks)} 块，其中代码块 {len(code_blocks)} 个")

        # 伪造 translations：含 ASCII 哨兵（保证可靠再提取）+ CJK
        sentinel = "ZZXLATEDZZ"
        translations: dict[str, str] = {}
        for b in blocks:
            translations[b.key] = f"{sentinel} 译文内容 {b.block_id}"

        engine.build_output(translations, "translated", out_translated)
        engine.build_output(translations, "interleaved", out_interleaved)

        # 断言：纯译文页数 = 3
        tdoc = pymupdf.open(out_translated)
        try:
            assert tdoc.page_count == 3, f"translated 应 3 页，实为 {tdoc.page_count}"
            # 译文可被重新提取
            full = "".join(tdoc[i].get_text() for i in range(tdoc.page_count))
            assert sentinel in full, "translated 中应能重新提取到译文哨兵"
            assert "译文内容" in full, "translated 中应能重新提取到 CJK 译文"
            # 图形仍存在：每页 get_drawings() 非空
            for i in range(tdoc.page_count):
                dr = tdoc[i].get_drawings()
                assert dr, f"translated 第 {i} 页矢量图形应保留，实为空"
        finally:
            tdoc.close()

        # 断言：交错页数 = 6
        idoc = pymupdf.open(out_interleaved)
        try:
            assert idoc.page_count == 6, f"interleaved 应 6 页，实为 {idoc.page_count}"
            ifull = "".join(idoc[i].get_text() for i in range(idoc.page_count))
            assert sentinel in ifull, "interleaved 中应能重新提取到译文哨兵"
            # 奇数页（译文页）应含哨兵，偶数页（原文页）应含原文
            odd_text = "".join(idoc[i].get_text() for i in range(1, idoc.page_count, 2))
            even_text = "".join(idoc[i].get_text() for i in range(0, idoc.page_count, 2))
            assert sentinel in odd_text, "交错译文页应含译文"
            assert "normal paragraph" in even_text, "交错原文页应保留原文"
        finally:
            idoc.close()

        print("全部断言通过：translated=3 页、interleaved=6 页、译文可再提取、矢量图形保留")
    finally:
        engine.close()


if __name__ == "__main__":
    _smoke_test()
