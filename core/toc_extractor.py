# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""章节（目录层级）提取与标注。

目标：让结果页的每条标准都能显示"它出现在文档的哪个章节下"，
例如 "3 技术要求 › 3.2 主要引用标准"。

设计要点
--------
1. toc 与 chapter 是两个不同的东西：
   - toc     : 文档**全局**目录树（level / title / page）
   - chapter : **每条记录**所属的章节路径（从 toc 反查得到，写入 record["chapter"]）
2. PDF 没有 Heading 样式，用 fitz 的字号 + 加粗 + 编号模式启发式识别。
3. DOCX 有 Heading 样式但没有页码，改用**正文字符偏移**匹配。
4. 全程确定性规则，不依赖任何模型；任何环节出错都不影响主流程。
"""
import re
from collections import Counter
from typing import Any, Dict, List, Optional


# ───────────────────────── 章节标题识别模式 ─────────────────────────
# 阿拉伯数字分级：1 / 1.1 / 1.1.1 / 1.1.1.1
_NUM_HEADING = re.compile(r"^\d{1,2}(?:\.\d{1,2}){0,4}[\s.、　 ]+")
# 中文章节：第一章 / 第2部分 / 第三节
_CN_CHAPTER = re.compile(r"^第\s*[一二三四五六七八九十百千零0-9]{1,6}\s*[章部分篇节]")
# 中文序号：一、 / 二.
_CN_NUM = re.compile(r"^[一二三四五六七八九十]{1,3}\s*[、.．]")
# 附录 A / 附件 3
_APPENDIX = re.compile(r"^附录\s*[A-Za-z一二三四五六七八九十]?")
_ATTACH = re.compile(r"^附件\s*\d*")
# 常见的一级标题词（无编号时的兜底）
_KEYWORD_TITLE = re.compile(
    r"^(总则|范围|规范性引用文件|术语和定义|技术要求|试验方法|检验规则|"
    r"引用标准|规范性引用|参考文献|编制说明|技术要求及试验方法|"
    r"标志|包装|运输|贮存|附录|前言|引言|目次|目　录|目录)$"
)

# 明显不是标题的行（页眉页脚、纯数字、表格内容等）
_NOT_HEADING = re.compile(r"^[\d\s\-—._/|]+$")


def _looks_like_heading(text: str) -> bool:
    """判断一行文本是否像章节标题。"""
    if not text or len(text) > 60 or len(text) < 2:
        return False
    if _NOT_HEADING.match(text):
        return False
    if _NUM_HEADING.match(text):
        return True
    if _CN_CHAPTER.match(text):
        return True
    if _APPENDIX.match(text) or _ATTACH.match(text):
        return True
    # 纯中文序号（一、二、）容易误伤正文，要求较短
    if _CN_NUM.match(text) and len(text) <= 30:
        return True
    if _KEYWORD_TITLE.match(text):
        return True
    return False


def _heading_level(text: str) -> int:
    """从标题文本推断层级（1 最粗）。"""
    m = re.match(r"^(\d{1,2}(?:\.\d{1,2})*)", text)
    if m:
        return min(m.group(1).count(".") + 1, 6)
    if _CN_CHAPTER.match(text):
        return 1
    if _APPENDIX.match(text) or _ATTACH.match(text):
        return 1
    if _CN_NUM.match(text):
        return 2
    return 2


def _format_chapter_path(stack: Dict[int, str], max_depth: int = 2) -> str:
    """把层级栈格式化成 "父 › 子" 形式的路径（最深优先，最多 max_depth 级）。"""
    if not stack:
        return ""
    levels = sorted(stack.keys())
    picked = levels[-max_depth:]
    parts = [stack[lv] for lv in picked if stack.get(lv)]
    # 标题过长时截断中间，保留首尾
    parts = [_shorten(p, 24) for p in parts]
    return " › ".join(parts)


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ───────────────────────── PDF 章节提取 ─────────────────────────
def extract_pdf_toc(doc, start: int = 1, end: Optional[int] = None,
                    on_log=None) -> List[Dict[str, Any]]:
    """从已打开的 fitz Document 中提取章节目录。

    识别信号（叠加使用）：
      1. 符合编号/关键词模式（必需）
      2. 整行较短（<=60 字符）（必需）
      3. 字号大于正文 或 加粗（文档无字号差异时自动放宽）

    返回 [{"level": int, "title": str, "page": int}, ...]，按出现顺序。
    """
    log = on_log or (lambda x: None)
    try:
        total = len(doc)
    except Exception:
        return []
    if total <= 0:
        return []
    start = max(1, start)
    end = total if end is None else min(end, total)
    if end < start:
        return []

    # ── 1) 采样前 10 页估计正文字号（取众数）
    sizes: List[float] = []
    for pno in range(start - 1, min(end, start + 9)):
        try:
            data = doc[pno].get_text("dict")
        except Exception:
            continue
        for blk in data.get("blocks", []):
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    if (span.get("text") or "").strip():
                        try:
                            sizes.append(round(float(span.get("size", 0)), 1))
                        except Exception:
                            pass
    if not sizes:
        # 纯扫描件等无文本情况，交给 OCR 路径，这里直接返回
        return []
    counter = Counter(sizes)
    body_size = counter.most_common(1)[0][0]
    max_size = max(sizes)
    # 文档整体字号无差异（如纯文本导出）时，不能依赖字号信号
    has_size_signal = (max_size - body_size) >= 0.5

    # ── 2) 逐页扫描，按「行」聚合 span 后判断
    toc: List[Dict[str, Any]] = []
    seen = set()
    for pno in range(start - 1, end):
        page_no = pno + 1
        try:
            data = doc[pno].get_text("dict")
        except Exception:
            continue
        for blk in data.get("blocks", []):
            for line in blk.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = "".join((s.get("text") or "") for s in spans)
                line_text = re.sub(r"\s+", " ", line_text).strip()
                if not _looks_like_heading(line_text):
                    continue
                line_size = 0.0
                for s in spans:
                    try:
                        line_size = max(line_size, float(s.get("size", 0)))
                    except Exception:
                        pass
                bold = False
                for s in spans:
                    flags = s.get("flags", 0) or 0
                    font = (s.get("font") or "")
                    if (flags & 16) or "Bold" in font or "黑" in font or "Hei" in font:
                        bold = True
                        break
                if has_size_signal and not bold and line_size <= body_size + 0.1:
                    continue
                key = (page_no, line_text)
                if key in seen:
                    continue
                seen.add(key)
                toc.append({
                    "level": _heading_level(line_text),
                    "title": line_text,
                    "page": page_no,
                })

    if toc:
        log(f"  章节提取: {len(toc)} 项（正文字号≈{body_size}）")
    return toc


# ───────────────────────── DOCX 章节标记 ─────────────────────────
def build_chapter_marks_from_docx(doc, body: str) -> List[Dict[str, Any]]:
    """遍历 DOCX 段落，记录每个 Heading 在 body 拼接串中的字符偏移。

    body 由 `_extract_body` 以 "\\n".join(非空段落) 生成，这里用同样规则
    累加偏移，保证 mark["offset"] 可以直接用于 body.find 结果比较。
    """
    marks: List[Dict[str, Any]] = []
    offset = 0
    try:
        paragraphs = list(doc.paragraphs)
    except Exception:
        return marks
    for para in paragraphs:
        try:
            text = (para.text or "").strip()
        except Exception:
            continue
        if not text:
            continue
        style = ""
        try:
            style = para.style.name if para.style else ""
        except Exception:
            style = ""
        if "Heading" in style:
            level = 1
            try:
                level = int(style.replace("Heading", "").strip())
            except Exception:
                level = 1
            marks.append({"offset": offset, "level": level, "title": text})
        elif _looks_like_heading(text) and len(text) <= 30:
            # 兼容"正文样式但排版成标题"的情况（常见于从 PDF 转来的 docx）
            marks.append({"offset": offset, "level": _heading_level(text), "title": text})
        offset += len(text) + 1  # +1 对应 join 的 "\n"
    return marks


# ───────────────────────── 标注：打章节 ─────────────────────────
def _build_page_chapter_map(toc: List[Dict[str, Any]], max_page: int) -> Dict[int, str]:
    """把 toc 展开成 {页码: 章节路径}，未出现新标题的页继承上一页路径。"""
    page_map: Dict[int, str] = {}
    if not toc:
        return page_map
    toc_sorted = sorted(toc, key=lambda x: (x.get("page", 0),))
    idx = 0
    stack: Dict[int, str] = {}
    for p in range(1, max_page + 1):
        while idx < len(toc_sorted) and toc_sorted[idx].get("page", 0) <= p:
            item = toc_sorted[idx]
            lv = int(item.get("level", 1) or 1)
            stack[lv] = item.get("title", "")
            for k in [k for k in stack if k > lv]:
                del stack[k]
            idx += 1
        if stack:
            page_map[p] = _format_chapter_path(stack)
    return page_map


def attach_chapter_by_page(tables: List[Dict[str, Any]], toc: List[Dict[str, Any]],
                           on_log=None) -> int:
    """按页码为 PDF 记录标注 chapter。page 形如 "第3页" / "第3页 表1 行2"。

    返回被标注的记录数。
    """
    if not tables or not toc:
        return 0
    max_page = 0
    page_nums: List[int] = []
    for r in tables:
        m = re.search(r"第\s*(\d+)\s*页", str(r.get("page", "") or ""))
        if m:
            pno = int(m.group(1))
            page_nums.append(pno)
            if pno > max_page:
                max_page = pno
    if not page_nums:
        return 0
    page_map = _build_page_chapter_map(toc, max_page)
    if not page_map:
        return 0

    count = 0
    for r in tables:
        m = re.search(r"第\s*(\d+)\s*页", str(r.get("page", "") or ""))
        if not m:
            continue
        chapter = page_map.get(int(m.group(1)), "")
        if chapter:
            r["chapter"] = chapter
            count += 1
    if count and on_log:
        on_log(f"  已为 {count} 条记录标注所在章节")
    return count


def attach_chapter_by_offset(tables: List[Dict[str, Any]], body: str,
                             marks: List[Dict[str, Any]], on_log=None) -> int:
    """按正文字符偏移为 DOCX 记录标注 chapter（DOCX 没有页码）。

    用 record["content"] 在 body 中定位，再找它之前最近的标题。
    """
    if not tables or not body or not marks:
        return 0
    marks_sorted = sorted(marks, key=lambda x: x["offset"])
    count = 0
    for r in tables:
        pos = -1
        content = (r.get("content") or "").strip()
        if content:
            pos = body.find(content)
        if pos < 0:
            # 退化：用标准号定位
            raw = str(r.get("name", "") or "")
            std_no = raw.split("《")[0].strip()
            if std_no:
                pos = body.find(std_no)
        if pos < 0:
            continue
        stack: Dict[int, str] = {}
        for mk in marks_sorted:
            if mk["offset"] > pos:
                break
            lv = int(mk.get("level", 1) or 1)
            stack[lv] = mk.get("title", "")
            for k in [k for k in stack if k > lv]:
                del stack[k]
        if stack:
            r["chapter"] = _format_chapter_path(stack)
            count += 1
    if count and on_log:
        on_log(f"  已为 {count} 条记录标注所在章节")
    return count



