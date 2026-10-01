# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""录入模式：将"标准目录型 PDF"（每行 = 一条标准）批量导入主字典。

典型形态如：
| 序号 | 标准编号      | 标准名称          |
| 1    | TB 10001-2016 | 铁路路基设计规范   |
| ...  | ...          | ...              |

该类 PDF 的特征：表格的每一行本身就是一条标准条目，不存在"上下文"分析需求。
我们把整张表视为数据源，识别列位置后批量写入 standard_dict.main。
"""
import os
import re
import sqlite3
import threading
from typing import Dict, Any, List, Callable, Tuple, Optional

from core.quiet_io import quiet_stderr_once, suppressed_stderr
from utils.helpers import search_standard_names


# 录入模式专用锁：避免与标准提取同时写字典导致混乱
_import_lock = threading.Lock()


class ImportHandler:
    """目录型 PDF 批量导入主字典的处理管道。"""

    def __init__(self, on_log: Callable[[str], None] = None,
                 on_progress: Callable[[int], None] = None,
                 on_status: Callable[[str], None] = None):
        self.on_log = on_log or (lambda x: None)
        self.on_status = on_status or (lambda x: None)
        self.on_progress = on_progress or (lambda x: None)
        self.cancel_check = lambda: None

    @staticmethod
    def _quiet_fitz():
        """静默导入 fitz（进程级一次性重定向，避免多线程互相关闭描述符）"""
        quiet_stderr_once()
        import fitz as _fitz
        return _fitz

    def _suppress_stderr(self):
        """包裹 fitz 操作的 stderr 抑制上下文（进程级一次性，之后为空操作）"""
        return suppressed_stderr()

    # ───────────────────────── 主入口 ─────────────────────────
    def process(self, file_path: str, options: Dict[str, Any]) -> Dict[str, Any]:
        """处理单个目录型 PDF 文件。

        options 可用字段：
          - update_existing: bool，是否覆盖已存在的标准号（默认 False，跳过）
          - start_page / end_page: int，页码范围
          - header_keywords: List[str]，表头关键词（自定义时使用）
        """
        import pdfplumber

        result = {
            "tables": [],     # 每条导入记录（与标准提取模式兼容：name/content/page）
            "imported": [],    # [{standard_no, display_name, page, row}]
            "skipped": [],     # [{standard_no, display_name, reason}]
            "logs": [],
            "text": "",
        }
        start = max(1, options.get("start_page", 1))
        end = options.get("end_page", 9999)
        update_existing = bool(options.get("update_existing", False))
        header_kw = options.get("header_keywords")  # 可选自定义

        self.on_log(f"  [录入模式] {file_path}")
        self.on_status("录入模式：解析目录表...")

        # 先取总页数
        try:
            with pdfplumber.open(file_path) as pdf:
                total_pages = len(pdf.pages)
        except Exception as e:
            self.on_log(f"  [录入模式] 打开 PDF 失败: {e}")
            result["logs"].append(f"打开 PDF 失败: {e}")
            return result

        if end == 9999 or end > total_pages:
            end = total_pages
        self.on_log(f"  [录入模式] 共 {total_pages} 页，处理范围 {start}-{end}")

        for page_idx, page_num in enumerate(range(start, end + 1)):
            self.cancel_check()
            try:
                with pdfplumber.open(file_path) as pdf:
                    page = pdf.pages[page_num - 1]
                    tables = page.extract_tables() or []
            except Exception as e:
                self.on_log(f"  [录入模式] 第 {page_num} 页解析失败: {e}")
                continue

            for t_idx, raw_table in enumerate(tables, start=1):
                self.cancel_check()
                if not raw_table:
                    continue
                # ⚠️ 录入模式不用 fill_merged_cells_down：真实目录 PDF 经常有
                # "分组标题行"（如 "铁路行业标准" 跨列合并的单元格），
                # 这些行原 PDF 是 col_no/col_name 列空的，fill 会把表头文本
                # 向下填充进空列，导致分组标题行被识别为标准数据行而污染主字典。
                # 这里只做 None→"" 规范化，保留原始空单元格以便被识别为空号跳过。
                rows = [[("" if c is None else c) for c in row] for row in raw_table]
                header_idx, col_no, col_name = self._identify_columns(
                    rows, header_kw, self.on_log
                )
                if col_no is None or col_name is None:
                    self.on_log(f"  [录入模式] 第 {page_num} 页 表{t_idx} 未识别到编号/名称列，跳过")
                    continue
                # 数据行：从表头下一行开始
                data_rows = rows[header_idx + 1:] if header_idx is not None else rows[1:]
                for r_offset, row in enumerate(data_rows, start=1):
                    self.cancel_check()
                    cells = [self._cell_text(c) for c in row]
                    # 跳过空行
                    if not any(cells):
                        result["skipped"].append({
                            "standard_no": "",
                            "display_name": "",
                            "reason": "空行",
                            "page": f"第{page_num}页 表{t_idx} 行{r_offset}",
                        })
                        continue
                    raw_no = cells[col_no] if col_no < len(cells) else ""
                    raw_name = cells[col_name] if col_name < len(cells) else ""
                    raw_row = " | ".join(c for c in cells if c)
                    std_no = self._clean_standard_no(raw_no)
                    # 检测"分组标题行"：标准号列为空（典型跨列合并标题）或等
                    # 于表头同列的列名文本（fill 残影）。这种情况归类为"分组标题"。
                    if not std_no:
                        result["skipped"].append({
                            "standard_no": "",
                            "display_name": raw_name[:60],
                            "reason": "分组标题/无编号",
                            "page": f"第{page_num}页 表{t_idx} 行{r_offset}",
                            "raw": raw_row[:120],
                        })
                        continue
                    # 检测：std_no 等于表头列名（即被错误填充进来的表头文本）
                    header_row = rows[header_idx] if header_idx is not None else []
                    if col_no < len(header_row):
                        header_text = self._cell_text(header_row[col_no])
                        if std_no and header_text and std_no == header_text:
                            result["skipped"].append({
                                "standard_no": std_no,
                                "display_name": raw_name[:60],
                                "reason": "分组标题行(填充残影)",
                                "page": f"第{page_num}页 表{t_idx} 行{r_offset}",
                                "raw": raw_row[:120],
                            })
                            continue
                    std_name = self._clean_standard_name(raw_name, std_no)
                    if not std_name:
                        result["skipped"].append({
                            "standard_no": std_no,
                            "display_name": "",
                            "reason": "无名称",
                            "page": f"第{page_num}页 表{t_idx} 行{r_offset}",
                            "raw": raw_row[:120],
                        })
                        continue
                    page_label = f"第{page_num}页 表{t_idx} 行{r_offset}"
                    result["tables"].append({
                        "name": std_name,
                        "content": raw_row,
                        "page": page_label,
                    })
            self.on_progress(int((page_idx + 1) / max(1, (end - start + 1)) * 70 + 0.5))

        # 批量写库
        self.on_status("录入模式：写入主字典...")
        imported, insert_skipped = self._bulk_insert(
            [t["name"] for t in result["tables"]],
            [t["content"] for t in result["tables"]],
            [t["page"] for t in result["tables"]],
            update_existing=update_existing,
        )
        # 合并写库前的解析级跳过（空行/无法解析）与写库级跳过
        all_skipped = result["skipped"] + insert_skipped
        result["imported"] = imported
        result["skipped"] = all_skipped
        result["stats"] = {
            "imported": len(imported),
            "skipped": len(all_skipped),
            "total": len(result["tables"]),
            "update_existing": update_existing,
        }
        self.on_log(
            f"  [录入模式] 完成：解析 {len(result['tables'])} 条，"
            f"导入 {len(imported)} 条，跳过 {len(all_skipped)} 条"
        )
        self.on_progress(100)
        self.on_status("录入完成")
        return result

    # ───────────────────────── 辅助：表头/列识别 ─────────────────────────
    @staticmethod
    def _cell_text(c) -> str:
        """把单元格统一成字符串，并去掉两端空白和换行"""
        if c is None:
            return ""
        return re.sub(r"\s+", " ", str(c)).strip()

    @staticmethod
    def _identify_columns(
        rows: List[List[Any]],
        header_keywords: Optional[List[str]] = None,
        on_log: Callable[[str], None] = None,
    ) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        """识别 (表头行索引, 标准号列索引, 名称列索引)。

        策略：
          1) 用第一行作为表头候选，匹配"标准编号 / 标准号 / Standard No"等关键词，
             以及"标准名称 / 名称 / Name"等关键词。
          2) 若首行没有匹配，降级：扫描前 3 行，每行内寻找同时包含 标准号关键词 + 名称关键词。
          3) 仍找不到则根据列内容启发——
             哪一列包含最多的"看起来像标准号"（被 search_standard_names 命中），
             哪一列是中文为主的字符串，认为是名称列。
        """
        kw_no = ["标准编号", "标准号", "编号", "代号", "标准代码", "standard no", "standard number", "code"]
        kw_name = ["标准名称", "标准名", "名称", "name", "title"]
        if header_keywords:
            kw_no = list(header_keywords.get("no", kw_no)) + kw_no
            kw_name = list(header_keywords.get("name", kw_name)) + kw_name

        def _match_kw(text: str, kws: List[str]) -> bool:
            t = text.lower()
            return any(kw.lower() in t for kw in kws)

        def _row_has(row_cells, kws: List[str]) -> int:
            for ci, cell in enumerate(row_cells):
                if _match_kw(cell, kws):
                    return ci
            return -1

        # ── 1) 首行作为表头
        if rows:
            first = [ImportHandler._cell_text(c) for c in rows[0]]
            c_no = _row_has(first, kw_no)
            c_name = _row_has(first, kw_name)
            if c_no != -1 and c_name != -1 and c_no != c_name:
                if on_log:
                    on_log(f"  [录入模式] 表头识别（首行）：编号列={c_no} 名称列={c_name}")
                return 0, c_no, c_name

        # ── 2) 扫描前 3 行
        for hi in range(min(3, len(rows))):
            cells = [ImportHandler._cell_text(c) for c in rows[hi]]
            c_no = _row_has(cells, kw_no)
            c_name = _row_has(cells, kw_name)
            if c_no != -1 and c_name != -1 and c_no != c_name:
                if on_log:
                    on_log(f"  [录入模式] 表头识别（第{hi+1}行）：编号列={c_no} 名称列={c_name}")
                return hi, c_no, c_name

        # ── 3) 内容启发：找列内含最多"标准号"的列作为 no 列；最长的中文列作为 name 列
        if not rows:
            return None, None, None
        max_cols = max(len(r) for r in rows)
        col_no_score = [0] * max_cols
        col_name_score = [0.0] * max_cols
        for r in rows:
            cells = [ImportHandler._cell_text(c) for c in r]
            for ci, cell in enumerate(cells):
                if not cell:
                    continue
                hits = search_standard_names(cell)
                if hits:
                    col_no_score[ci] += len(hits)
                # 中文比例
                ch = sum(1 for ch in cell if '\u4e00' <= ch <= '\u9fff')
                if ch >= 2:
                    col_name_score[ci] += ch
        # 标准号列：score 最高
        if max(col_no_score) == 0:
            if on_log:
                on_log(f"  [录入模式] 无法识别表头（未找到含标准号的列）")
            return None, None, None
        c_no = col_no_score.index(max(col_no_score))
        # 名称列：除编号列外 score 最高
        best_name_idx = -1
        best_name_score = -1.0
        for ci, sc in enumerate(col_name_score):
            if ci == c_no:
                continue
            if sc > best_name_score:
                best_name_score = sc
                best_name_idx = ci
        if best_name_idx == -1:
            if on_log:
                on_log(f"  [录入模式] 无法识别名称列")
            return None, None, None
        if on_log:
            on_log(f"  [录入模式] 表头识别（启发式）：编号列={c_no} 名称列={best_name_idx}")
        return 0, c_no, best_name_idx

    # ───────────────────────── 辅助：清洗 ─────────────────────────
    @staticmethod
    def _clean_standard_no(text: str) -> str:
        """清洗标准号：把连续空白（含 PDF 折行产生的换行）折叠为单个空格。

        注意：不能删除全部空白，否则 "TB/T 46-2020" 会变成 "TB/T46-2020"，
        与标准提取时从原文识别出的编号形式不一致，导致字典查询命中失败。
        """
        if not text:
            return ""
        return re.sub(r"\s+", " ", str(text)).strip()

    @staticmethod
    def _clean_standard_name(name: str, std_no: str) -> str:
        """组装"标准号《名称》"形式。若名称里已经含《》则保留。

        返回值形如：GB/T 12345-2020《标准名称》
        """
        name = (name or "").strip()
        name = re.sub(r"\s+", " ", name)
        if not name:
            # 极端兜底：纯标准号当名称
            return f"{std_no}"
        # 已有《》且标准号在前的，保留
        if "《" in name and name.startswith(std_no):
            return name
        # 已有《》但无标准号前缀：补上标准号
        if "《" in name and "》" in name:
            if std_no and std_no not in name[:name.index("《")]:
                return f"{std_no}{name}"
            return name
        return f"{std_no}《{name}》"

    # ───────────────────────── 辅助：批量入库 ─────────────────────────
    @staticmethod
    def _bulk_insert(
        names: List[str], contents: List[str], pages: List[str],
        update_existing: bool = False,
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        """将解析到的多条标准写入 主字典.main。

        返回 (imported, skipped)。
          imported: [{standard_no, display_name, page}]
          skipped:  [{standard_no, display_name, reason, page}]
        """
        imported: List[Dict[str, str]] = []
        skipped: List[Dict[str, str]] = []

        # 定位数据库（统一走可写数据目录，避免 %ProgramFiles% 下写入失败）
        try:
            from utils.helpers import user_data_dir  # type: ignore
            db_path = os.path.join(user_data_dir(), "标准名称字典.db")
        except Exception:
            from web.server import _root_dir  # type: ignore
            db_path = os.path.join(_root_dir(), "标准名称字典.db")
        if not os.path.exists(db_path):
            # 若库不存在则创建空表结构（与 handle_dict_add 一致）
            try:
                _init_dict_db(db_path)
            except Exception as e:
                skipped.append({
                    "standard_no": "-",
                    "display_name": "-",
                    "reason": f"数据库不可用: {e}",
                    "page": "",
                })
                return imported, skipped

        with _import_lock:
            # timeout：与提取模式/界面写库并发时，SQLite 默认 5 秒就放弃并抛
            # "database is locked"，会让整个文件的行全部导入失败
            conn = sqlite3.connect(db_path, timeout=30)
            try:
                # 解析标准号 / 显示名
                rows = []
                for n, c, p in zip(names, contents, pages):
                    m = re.match(r"^(.+?)《(.+?)》\s*$", n or "")
                    if m:
                        std_no = m.group(1).strip()
                        display = n.strip()
                    else:
                        std_no = (n or "").strip()
                        display = std_no
                    rows.append((std_no, display, c, p))

                # 批量预检（避免每行一次 SELECT）
                unique_nos = sorted({r[0] for r in rows if r[0]})
                existing_nos = set()
                if unique_nos:
                    placeholders = ",".join("?" for _ in unique_nos)
                    cur = conn.execute(
                        f"SELECT standard_no FROM main WHERE standard_no IN ({placeholders})",
                        unique_nos,
                    )
                    existing_nos = {r[0] for r in cur.fetchall()}

                to_insert: List[Tuple[str, str, str]] = []  # (std_no, display, page)
                for std_no, display, raw, page in rows:
                    if not std_no:
                        skipped.append({
                            "standard_no": "",
                            "display_name": display,
                            "reason": "空标准号",
                            "page": page,
                        })
                        continue
                    if std_no in existing_nos:
                        if update_existing:
                            conn.execute(
                                "UPDATE main SET display_name=? WHERE standard_no=?",
                                (display, std_no),
                            )
                            imported.append({
                                "standard_no": std_no,
                                "display_name": display,
                                "page": page,
                            })
                            # 这里**不要**把 std_no 从 existing_nos 里移除：
                            # 同一批里该标准号再出现时会走 INSERT，撞主键后被记成
                            # "写库失败: UNIQUE constraint failed"，把正常的重复行
                            # 误报成故障。保留在集合里，重复行会正常归为 UPDATE。
                        else:
                            skipped.append({
                                "standard_no": std_no,
                                "display_name": display,
                                "reason": "已存在",
                                "page": page,
                            })
                        continue
                    to_insert.append((std_no, display, page))

                # 去重：同一文件内同一标准号只插入一次（保留最先出现的）；
                # 后续重复行不再静默丢弃，而是如实计入 skipped，便于用户核对数量
                seen = set()
                deduped: List[Tuple[str, str, str]] = []  # (std_no, display, page)
                for std_no, display, page in to_insert:
                    if std_no in seen:
                        skipped.append({
                            "standard_no": std_no,
                            "display_name": display,
                            "reason": "本文件内重复",
                            "page": page,
                        })
                        continue
                    seen.add(std_no)
                    deduped.append((std_no, display, page))

                for std_no, display, page_match in deduped:
                    try:
                        conn.execute(
                            "INSERT INTO main (standard_no, display_name) VALUES (?, ?)",
                            (std_no, display),
                        )
                    except Exception as e:
                        skipped.append({
                            "standard_no": std_no,
                            "display_name": display,
                            "reason": f"写库失败: {e}",
                            "page": page_match,
                        })
                        continue
                    imported.append({
                        "standard_no": std_no,
                        "display_name": display,
                        "page": page_match,
                    })
                conn.commit()
                # 关键：本函数直接写 main 表，绕过了 add_standard_to_main，
                # 必须主动让字典缓存失效，否则后续的标准提取/结果合并仍会
                # 用旧缓存判定"该标准号不在库中"，导致只显示标准号不显示名称。
                try:
                    from utils.helpers import invalidate_standard_dict_cache
                    invalidate_standard_dict_cache()
                except Exception:
                    pass
            finally:
                conn.close()
        return imported, skipped


def _init_dict_db(db_path: str):
    """初始化主字典 SQLite（与 handle_dict_add 中隐含 schema 一致）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS main (
                standard_no TEXT PRIMARY KEY,
                display_name TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review (
                standard_no TEXT PRIMARY KEY,
                display_name TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()