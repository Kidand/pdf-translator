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
from typing import Callable, Optional

import pymupdf

from backend.models import TextBlock

logger = logging.getLogger("backend.pdf_engine")

# build_output 进度回调签名：Callable[[已完成页, 总页], None]
RenderProgressCb = Callable[[int, int], None]


def _shrink_doc(doc: "pymupdf.Document") -> None:
    """就地压缩文档体积：子集化嵌入字体（insert_htmlbox 的 CJK 回退会把整套中文字体
    嵌进**每一页/每一次调用**，不子集化时单页可达数 MB～数十 MB，是磁盘暴涨的根因）。

    subset_fonts() 只保留实际用到的字形，实测使回填中文后的单页由 ~1.8MB 降到 ~100KB。
    该调用依赖字体子集化后端，个别损坏/不可子集化的字体可能抛异常——best-effort 包裹，
    失败仅记录、不影响出图（大不了体积没缩小）。保存时另配 garbage=4 做对象去重。
    """
    try:
        doc.subset_fonts()
    except Exception:  # noqa: BLE001 - 子集化失败不应阻断出图，仅体积不缩小
        logger.warning("字体子集化失败（体积可能偏大）", exc_info=True)

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
        # 加密（用户口令保护）的 PDF：page_count 虽有值，但后续 get_text 会抛
        # "document closed or encrypted"。此处提前拦截，关闭文档并抛出可读错误，
        # jobs._run 的异常路径会把它转成任务 ERROR（error 文案即此消息）。
        if self.doc.needs_pass:
            self.doc.close()
            raise ValueError("PDF 已加密，无法处理")
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
            candidates: list[TextBlock] = []  # 本页候选块（block_id 稍后统一编号）
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
                        page_index, 0, {"lines": group, "bbox": (gx0, gy0, gx1, gy1)}
                    )
                    if tb is None:  # 空白块跳过
                        continue
                    candidates.append(tb)
            # 丢弃被更靠后的块大面积覆盖的块（内容流靠后 ≈ 绘制在上层）：
            # 这类块在原始渲染中被上层文字/填充遮住不可见（常见于图形编辑残留），
            # 若照常翻译回填会浮到最上层与可见文字重叠。跳过它（不翻译、不 redact），
            # 让它保持原样继续被遮挡。
            kept = self._drop_covered_blocks(candidates, page_index)
            self._compute_expansions(kept, page.rect, page.rotation)
            for block_id, tb in enumerate(kept):
                tb.block_id = block_id
                blocks.append(tb)
        self._blocks = blocks
        logger.info("提取到 %d 个文本块", len(blocks))
        return blocks

    # 判定「被覆盖」的交叠阈值：交叠面积 / 被覆盖块自身面积
    _COVERED_RATIO = 0.55

    def _drop_covered_blocks(
        self, candidates: list[TextBlock], page_index: int
    ) -> list[TextBlock]:
        """丢弃被内容流中更靠后的块大面积覆盖的候选块。"""
        kept: list[TextBlock] = []
        for i, tb in enumerate(candidates):
            x0, y0, x1, y1 = tb.bbox
            area = max((x1 - x0) * (y1 - y0), 1e-6)
            covered = False
            for later in candidates[i + 1:]:
                lx0, ly0, lx1, ly1 = later.bbox
                inter = max(0.0, min(x1, lx1) - max(x0, lx0)) * max(
                    0.0, min(y1, ly1) - max(y0, ly0)
                )
                if inter / area > self._COVERED_RATIO:
                    covered = True
                    logger.info(
                        "第 %d 页疑似被遮挡的隐藏文本，跳过翻译：%r（被 %r 覆盖）",
                        page_index + 1, tb.text[:40], later.text[:40],
                    )
                    break
            if not covered:
                kept.append(tb)
        return kept

    @staticmethod
    def _compute_expansions(
        page_blocks: list[TextBlock], page_rect: pymupdf.Rect, rotation: int = 0
    ) -> None:
        """计算每块回填时可安全扩展的余量（pt）。

        中文全宽字符常使译文比紧贴原文的 bbox 略宽而折行，高度随之增长，
        insert_htmlbox 只能靠缩小字号来适配。两个方向的吸收策略：
        - y_expand（所有块）：到下方最近块（x 区间有重叠者才构成遮挡）
          之间的空隙，上限约 1.2 行高，让折行被下方空白吸收；
        - x_expand（仅代码块，恒左对齐）：到右侧最近块之间的空隙，
          上限 4 倍字号，让略超宽的代码行免于折行。

        旋转页（rotation 90/270）的块 bbox 在未旋转坐标系，而 page_rect 是旋转后的
        尺寸（宽高互换），基于 page_rect 的 x_expand 右界会错轴；保守起见旋转页
        （rotation != 0）一律不做扩展，保留默认 y_expand=x_expand=0。
        """
        if rotation:
            return
        for tb in page_blocks:
            x0, y0, x1, y1 = tb.bbox
            line_h = (y1 - y0) / max(tb.line_count, 1)
            nearest_below: float | None = None
            nearest_right: float | None = None
            for other in page_blocks:
                if other is tb:
                    continue
                ox0, oy0, ox1, oy1 = other.bbox
                # 下方最近块：x 区间有重叠才构成遮挡
                if oy0 >= y1 - 1e-6 and min(x1, ox1) - max(x0, ox0) > 1.0:
                    if nearest_below is None or oy0 < nearest_below:
                        nearest_below = oy0
                # 右侧最近块：y 区间有重叠才构成遮挡
                if ox0 >= x1 - 1e-6 and min(y1, oy1) - max(y0, oy0) > 1.0:
                    if nearest_right is None or ox0 < nearest_right:
                        nearest_right = ox0
            gap_below = line_h if nearest_below is None else max(0.0, nearest_below - y1 - 1.0)
            tb.y_expand = min(gap_below, line_h * 1.2)
            if tb.is_code:
                right_limit = (page_rect.x1 - 4.0) if nearest_right is None else nearest_right - 1.0
                tb.x_expand = min(max(0.0, right_limit - x1), tb.font_size * 4.0)

    # 判定两行是否「并排」的垂直重叠阈值（占较矮行高的比例）：
    # 重叠 > 该比例视为同一水平带上的并排文字（分属不同列），否则视为上下续行。
    _SIDE_BY_SIDE_V_OVERLAP = 0.5

    @classmethod
    def _split_side_by_side_lines(cls, raw: dict) -> list[list[dict]]:
        """把 raw block 的 lines 按「列」分组（列感知，避免跨列错并）。

        正常段落的行自上而下堆叠（相邻行 x 区间重叠、垂直几乎不重叠），应聚成一组；
        而 MuPDF 会把同一水平带上左右并排的独立文字（图示标签、表格单元格）合并进
        同一 block，此时行按行优先序（左1、右1、左2、右2…）到达。若像旧实现那样只跟
        「最后一组的最后一行」比较、非并排就并入最后一组，第二行会被错并进上一列的组
        （产出 [左1] / [右1,左2] / [右2]）：该组 bbox 横跨整页宽，redact 抹掉相邻
        单元格的原文，译文也横跨两列错位。

        改为列感知分组：对每个新行，在所有既有组里找「其最后一行与新行垂直相续」
        的组 —— 新行在其下方、垂直重叠小于阈值（不是并排）、且 x 区间有重叠（同一列）——
        取 x 重叠最大者加入；找不到就新开一组。这样各列各自成组，不跨列合并。
        """
        groups: list[list[dict]] = []
        for line in raw.get("lines", []):
            if not line.get("spans") or not line.get("bbox"):
                continue
            lx0, ly0, lx1, ly1 = line["bbox"]
            best_group: Optional[list[dict]] = None
            best_overlap = 0.0
            for group in groups:
                gx0, gy0, gx1, gy1 = group[-1]["bbox"]  # 与该组最后一行比较
                # 新行须在该组最后一行下方（顶边不高于其顶边，留微小容差）
                if ly0 < gy0 - 1e-3:
                    continue
                # 垂直重叠须小于较矮行高的一半，否则是并排（不同列）而非上下续行
                v_overlap = min(gy1, ly1) - max(gy0, ly0)
                min_height = max(min(gy1 - gy0, ly1 - ly0), 1e-3)
                if v_overlap > cls._SIDE_BY_SIDE_V_OVERLAP * min_height:
                    continue
                # x 区间须有重叠（同一列）；取重叠最大的组作为归属
                x_overlap = min(gx1, lx1) - max(gx0, lx0)
                if x_overlap <= 0.0:
                    continue
                if best_group is None or x_overlap > best_overlap:
                    best_group = group
                    best_overlap = x_overlap
            if best_group is None:
                groups.append([line])
            else:
                best_group.append(line)
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
    def _line_height(block: TextBlock) -> float:
        """数据驱动的行高：按块自身 bbox 高度/行数/字号 推算原始行距倍数。

        写死 1.25 会普遍大于 PDF 原生行距（bbox 高 ≈1.15~1.2 倍字号），
        导致译文明明放得下也被 insert_htmlbox 整体缩小（实测标题缩 13%）。
        """
        if block.line_count > 0 and block.font_size > 0:
            natural = (block.bbox[3] - block.bbox[1]) / block.line_count / block.font_size
            return max(1.0, min(1.3, natural * 0.97))
        return 1.15

    @classmethod
    def _build_html(cls, block: TextBlock, translation: str) -> str:
        """构造 insert_htmlbox 用的 HTML（inline style，含字号/颜色/对齐/粗斜体）。"""
        style_parts = [
            f"font-size:{block.font_size:.1f}pt",
            f"color:{block.color}",
            f"text-align:{block.align}",
            f"line-height:{cls._line_height(block):.2f}",
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

    @staticmethod
    def _has_translation(block: TextBlock, translations: dict[str, str]) -> bool:
        """块是否有非空译文（决定是否 redact + 回填）；否则保持原样。"""
        translation = translations.get(block.key)
        return bool(translation and translation.strip())

    def _apply_page_translations(
        self,
        page: pymupdf.Page,
        page_blocks: list[TextBlock],
        translations: dict[str, str],
    ) -> None:
        """对单页应用译文：redact（保住图片/矢量线条）后按原始 bbox 回填译文。

        page_blocks 为该页中已确认有非空译文的块（bbox 为其在所属页坐标系下的原始
        bbox，单页新文档中坐标不变）；translations 提供译文文本。
        build_output 与 render_page_pdf 共用此私有回填逻辑，避免两份复制。
        """
        if not page_blocks:
            return

        # 第一遍：加 redact 注解并统一 apply（保住图片与矢量线条）
        for block in page_blocks:
            page.add_redact_annot(self._shrink_rect(block.bbox))
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )

        # 第二遍：用原始 bbox 回填译文（向下/向右加安全扩展余量，吸收译文折行）
        # 旋转页（rotation 90/270）的块 bbox 在未旋转坐标系，而 page.rect 是旋转后的
        # 尺寸，两者混用会错轴夹取（静默丢掉本该有的 expand）；旋转页保守地不扩展，
        # 直接用原始 bbox 回填。
        rotated = page.rotation != 0
        for block in page_blocks:
            x0, y0, x1, y1 = block.bbox
            if not rotated:
                y1 = max(y1, min(y1 + block.y_expand, page.rect.y1 - 2.0))
                x1 = max(x1, min(x1 + block.x_expand, page.rect.x1 - 2.0))
            rect = pymupdf.Rect(x0, y0, x1, y1)
            html_content = self._build_html(block, translations[block.key])
            ret = page.insert_htmlbox(rect, html_content, scale_low=0.1)
            # insert_htmlbox 返回 (spare_height, scale)；spare_height<0 表示放不下，已尽力缩小，忽略
            spare = ret[0] if isinstance(ret, (tuple, list)) else ret
            if spare is not None and spare < 0:
                logger.debug("块 %s 译文放不下（spare=%.2f），已尽力缩小", block.key, spare)

    def _build_translated_doc(
        self,
        translations: dict[str, str],
        progress_cb: "RenderProgressCb | None" = None,
    ) -> pymupdf.Document:
        """在源文档干净副本上做 redaction + 译文回填，返回该文档。

        progress_cb(done_pages, total_pages)：可选，逐页回填后报告进度（供合成进度条）。
        无译文的页也计入 done（组装过程仍逐页推进）。
        """
        doc = pymupdf.open(self.src_path)

        # 按页收集需要回填的块（key 在 translations 且译文非空）
        by_page: dict[int, list[TextBlock]] = defaultdict(list)
        for block in self._blocks_for_build():
            if self._has_translation(block, translations):
                by_page[block.page_index].append(block)

        total = doc.page_count
        for page_index in range(total):
            page_blocks = by_page.get(page_index)
            if page_blocks:
                self._apply_page_translations(doc[page_index], page_blocks, translations)
            if progress_cb is not None:
                progress_cb(page_index + 1, total)

        return doc

    def build_output(
        self,
        translations: dict[str, str],
        mode: str,
        out_path: str,
        progress_cb: "RenderProgressCb | None" = None,
    ) -> None:
        """组装输出文档并保存。

        mode:
          "translated"  —— 纯译文。
          "interleaved" —— 原文第 i 页、译文第 i 页交替。

        progress_cb(done_pages, total_pages)：可选，报告组装进度（供合成进度条）。
        total 以**源页数**计（interleaved 每源页含原文+译文两页，仍按源页粒度报告）；
        子集化 + 保存作为最后一步在到达 total 后进行（该步无法细分页进度）。
        """
        if mode not in ("translated", "interleaved"):
            raise ValueError(f"未知 mode: {mode!r}")

        total = self.page_count

        def _report(done: int, total_pages: int) -> None:
            if progress_cb is not None:
                try:
                    progress_cb(done, total_pages)
                except Exception:  # noqa: BLE001 - 进度回调异常不应阻断出图
                    logger.debug("build_output 进度回调异常", exc_info=True)

        translated_doc = self._build_translated_doc(translations, progress_cb=_report)
        try:
            # 字体子集化**只作用于译文文档**（它才逐页嵌了整套 CJK 字体）。绝不能对含原文页的
            # 交错文档整体子集化——subset_fonts 会改写原文页的字体，实测破坏原文渲染（用户反馈
            # 「原文样式崩溃」的根因）。原文页从 self.doc 原样插入、全程不被触碰。
            _shrink_doc(translated_doc)
            if mode == "translated":
                translated_doc.save(out_path, garbage=4, deflate=True)
                logger.info("已保存纯译文文档：%s（%d 页）", out_path, translated_doc.page_count)
            else:  # interleaved
                out_doc = pymupdf.open()
                try:
                    for i in range(self.page_count):
                        out_doc.insert_pdf(self.doc, from_page=i, to_page=i)          # 原文页：原样，不子集化
                        out_doc.insert_pdf(translated_doc, from_page=i, to_page=i)    # 译文页：已子集化
                    # 仅 garbage=4 做对象去重（无损，不改字形）；不再对合并文档 subset_fonts。
                    out_doc.save(out_path, garbage=4, deflate=True)
                    logger.info(
                        "已保存交错文档：%s（%d 页）", out_path, out_doc.page_count
                    )
                finally:
                    out_doc.close()
        finally:
            translated_doc.close()

    def render_page_pdf(self, page_index: int, translations: dict[str, str]) -> bytes:
        """渲染单页译文 PDF 字节流（v2 增量按需翻译用）。

        新建单页文档 ← insert_pdf 源文档该页；对新文档第 0 页按本页缓存块回填译文
        （与 build_output 完全复用同一套 _apply_page_translations 私有方法）→
        tobytes(garbage=3, deflate=True)。

        - 块来源为 _blocks_for_build() 缓存；块 bbox 在单页新文档中坐标不变。
        - 该页无可译块或该页块均无译文 → 返回原样单页 bytes（不 redact、不回填）。
        - page_index 越界 → 抛 ValueError。

        线程安全：方法内不引入全局可变状态；本方法会被 asyncio.to_thread 调用，
        但同一 job 内调度器保证同一时刻只有一次 PDF 操作。
        """
        if page_index < 0 or page_index >= self.page_count:
            raise ValueError(
                f"page_index 越界: {page_index}（共 {self.page_count} 页）"
            )

        # 本页需要回填的块（key 在 translations 且译文非空）；bbox 在单页文档中不变
        page_blocks = [
            block
            for block in self._blocks_for_build()
            if block.page_index == page_index
            and self._has_translation(block, translations)
        ]

        single = pymupdf.open()
        try:
            # 新文档仅含源文档该页，成为第 0 页；坐标系与源页一致
            single.insert_pdf(self.doc, from_page=page_index, to_page=page_index)
            # page_blocks 为空时 _apply_page_translations 直接返回，等价于原样单页
            self._apply_page_translations(single[0], page_blocks, translations)
            # 有回填才需子集化（回填才嵌 CJK 字体）；无译文的原样单页跳过省时。
            if page_blocks:
                _shrink_doc(single)
            return single.tobytes(garbage=4, deflate=True)
        finally:
            single.close()

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

        # ---- v2：render_page_pdf 单页按需渲染 ---- #
        # 只对第 2 页（page_index=1）构造译文，其余页不放入 dict
        page2_translations: dict[str, str] = {
            b.key: f"{sentinel} 第2页译文 {b.block_id}"
            for b in blocks
            if b.page_index == 1
        }

        # 第 2 页：应回填译文
        page2_bytes = engine.render_page_pdf(1, page2_translations)
        assert isinstance(page2_bytes, (bytes, bytearray)), "render_page_pdf 应返回 bytes"
        p2 = pymupdf.open(stream=page2_bytes, filetype="pdf")
        try:
            assert p2.page_count == 1, f"render_page_pdf 应返回单页，实为 {p2.page_count}"
            p2_text = p2[0].get_text()
            assert sentinel in p2_text, "第 2 页单页 PDF 应能重新提取到译文哨兵"
            assert "第2页译文" in p2_text, "第 2 页单页 PDF 应能重新提取到 CJK 译文"
            assert p2[0].get_drawings(), "单页译文 PDF 应保留矢量图形"
        finally:
            p2.close()

        # 无译文页（page_index=0 不在 page2_translations 中）→ 返回原样单页
        plain_bytes = engine.render_page_pdf(0, page2_translations)
        p0 = pymupdf.open(stream=plain_bytes, filetype="pdf")
        try:
            assert p0.page_count == 1, "无译文页也应返回单页 PDF"
            p0_text = p0[0].get_text()
            assert sentinel not in p0_text, "无译文页不应含译文哨兵"
            assert "normal paragraph" in p0_text, "无译文页应原样保留原文"
        finally:
            p0.close()

        # 空 translations 时任意页返回原样单页
        empty_bytes = engine.render_page_pdf(1, {})
        pe = pymupdf.open(stream=empty_bytes, filetype="pdf")
        try:
            assert pe.page_count == 1, "空译文应返回单页 PDF"
            assert sentinel not in pe[0].get_text(), "空译文页不应含译文哨兵"
        finally:
            pe.close()

        # 越界 page_index → ValueError
        for bad in (-1, engine.page_count):
            raised = False
            try:
                engine.render_page_pdf(bad, page2_translations)
            except ValueError:
                raised = True
            assert raised, f"越界 page_index={bad} 应抛 ValueError"

        print("v2 断言通过：render_page_pdf 单页译文可提取、无译文页原样、越界抛 ValueError")
    finally:
        engine.close()


if __name__ == "__main__":
    _smoke_test()
