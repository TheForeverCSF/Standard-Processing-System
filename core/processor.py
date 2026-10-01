# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import os
import re
import traceback
import threading
from concurrent.futures import (ThreadPoolExecutor, as_completed,
                               wait, FIRST_COMPLETED)
from collections import defaultdict
from typing import Callable, Dict, Any, List

from core.ai_handler import LocalAIHandler
from core.document_router import DocumentRouter
from core.docx_handler import DocxHandler
from core.excel_handler import ExcelHandler
from core.pdf_handler import PdfHandler
from core.pptx_handler import PptxHandler
from core.text_handler import TextHandler
from core.ocr_handler import OcrHandler
from utils.helpers import (_load_standard_dict, lookup_standard_dict,
                           normalize_std_no, standard_no_of)

# ── 超时参数（抽成模块常量，便于按机器性能调整与自动化测试）──
# 单个文件的处理时限 = 页数 × PER_PAGE_TIMEOUT_SEC，再钳制到上下限之间。
PER_PAGE_TIMEOUT_SEC = 30
MIN_FILE_TIMEOUT = 300        # 5 分钟
MAX_FILE_TIMEOUT = 1800       # 30 分钟
# 并行模式下"完全没有任何文件完成"的容忍时间：超过就判定卡住，
# 跳过剩余文件并交回已完成的结果（避免界面永远转圈）。
# 注意它统计的是"距上次有文件完成"的时间，不是整批总时长，
# 所以正常的大批量处理不会被误判。
PARALLEL_STALL_LIMIT = 1800   # 30 分钟


_STD_NO_RE = re.compile(r"^[A-Z]{1,5}(?:\s*/\s*[A-Z]{1,6})?\s*\d")


def _looks_like_std_no(s: str) -> bool:
    """判断字符串是否像标准编号（用于拦掉 AI 的不可信输出）"""
    t = (s or "").strip().upper()
    return bool(t) and bool(_STD_NO_RE.match(t))


def _adapt_log(fn) -> Callable[..., None]:
    """统一 on_log 的调用签名。

    调用方既可能传 on_log(msg)，也可能传 on_log(msg, level)（例如 log_print）。
    历史代码里两种写法混用，多传一个 level 就会抛
    `TypeError: <lambda>() takes 1 positional argument but 2 were given`，
    而它偏偏出现在"超时 / 取消"这些异常分支里 —— 一抛就把整批任务拖成"处理失败"。
    这里按目标函数的真实签名做适配，彻底避免这类误用。
    """
    if fn is None:
        return lambda *a, **k: None
    try:
        import inspect
        params = list(inspect.signature(fn).parameters.values())
        has_var = any(p.kind == p.VAR_POSITIONAL for p in params)
        if not has_var and len(params) <= 1:
            return lambda msg, level=None: fn(msg)
    except (TypeError, ValueError):
        pass
    return fn


class DocumentProcessor:
    """文档标准化处理核心控制器。

    处理流程：
    1. 文档类型路由（DOC/DOCX/Excel/PDF）
    2. 格式转换（DOC -> DOCX）
    3. 按文档类型进入专用处理管道
    4. 提取表格、正文、图片、文本框
    5. 识别合并单元格并向下填充
    6. 全局搜索标准名称
    7. 本地 AI 修正标准名称和表格结构
    8. 聚合统计并返回结果
    """

    def __init__(self, on_progress: Callable[[int], None] = None,
                 on_status: Callable[[str], None] = None,
                 on_log: Callable[[str], None] = None,
                 cancel_check: Callable[[], None] = None,
                 on_file_complete: Callable[[str, dict], None] = None,
                 on_file_progress: Callable[[str, int], None] = None):
        self.on_progress = on_progress or (lambda x: None)
        self.on_status = on_status or (lambda x: None)
        self.on_log = _adapt_log(on_log)
        self.cancel_check = cancel_check or (lambda: None)
        self.on_file_complete = on_file_complete or (lambda f, r: None)
        self.on_file_progress = on_file_progress or (lambda f, p: None)
        self.router = DocumentRouter()
        self.ai = LocalAIHandler(on_log)
        self.ocr = OcrHandler(on_log)

    def process(self, task: Dict[str, Any]) -> Dict[str, Any]:
        files = task.get("files", [])
        start_page = task.get("start_page", 1)
        end_page = task.get("end_page", 9999)
        max_workers = task.get("max_workers", 1)  # 默认单线程，>1 为并行
        process_mode = task.get("process_mode", "extract")  # extract | import
        # 录入模式：独立走专用处理管道
        if process_mode == "import":
            return self._process_import(task)
        options = {
            "extract_table": task.get("extract_table", True),
            "extract_body": task.get("extract_body", True),
            "extract_image": task.get("extract_image", True),
            "extract_textbox": task.get("extract_textbox", True),
            "ocr_scanned": task.get("ocr_scanned", True),
            "ai_enhance": task.get("ai_enhance", True),
            "ocr_images": task.get("ocr_images", True),
            "start_page": start_page,
            "end_page": end_page,
        }

        all_results = {"tables": [], "text": "", "logs": [], "stats": {}, "toc": []}
        if not files:
            self.on_log("[警告] 未选择任何文件")
            self.on_status("未选择文件")
            self.on_progress(0)
            return all_results

        total = len(files)
        self.on_progress(0)
        self._cancel_flag = False

        # 统一计算文件权重（页数）
        # 用带缓存的版本：前端页数未统计完时（end=9999）这里会回退到真实页数，
        # 若走无缓存的 get_document_page_count()，几百个文件会在开始处理前
        # 串行解析一遍（"点了处理很久没动静"）。缓存与扫描阶段共用（见 L1）。
        from utils.helpers import get_document_page_count_cached
        file_page_opts = task.get("file_page_options") or {}
        file_weights = []
        for f in files:
            opts = file_page_opts.get(f, {})
            pages = opts.get("end", 9999) - opts.get("start", 1) + 1
            # N3：前端页数未统计完时会传 end=9999（约定"到文件末尾"），它不是真实页数。
            # 原来写的是 > 9999，导致 9999 被当成真实值 → 权重被算成 9999 →
            # 并行模式下进度条几乎不动（最后突然跳 100%）、单文件超时上限失真、
            # 日志出现"(5/10044页)"这种怪异数字。必须 >= 9999 才判未知。
            if pages >= 9999 or pages < 1:
                pages = get_document_page_count_cached(f)
            file_weights.append(max(1, pages))

        # 逐文件超时：每页最多 PER_PAGE_TIMEOUT 秒，整体钳制在
        # [MIN_FILE_TIMEOUT, MAX_FILE_TIMEOUT] 之间（见模块顶部常量）
        PER_PAGE_TIMEOUT = PER_PAGE_TIMEOUT_SEC
        cancelled = False

        if total == 1 or max_workers <= 1:
            # ── 单文件顺序处理（每个文件有独立超时）──
            for idx, file_path in enumerate(files):
                # 用户终止时不再抛异常中断整批：只记下取消，保留已完成文件的结果
                # （原先 cancel_check 抛出的异常会一路冒泡，process() 整体失败，
                #  前面所有已汇总的结果一并丢掉）
                try:
                    self.cancel_check()
                except Exception:
                    cancelled = True
                    break
                if self._cancel_flag:
                    cancelled = True
                    break
                file_name = os.path.basename(file_path)
                self.on_status(f"正在处理: {file_name}")
                self.on_log(f"[开始] {file_path}")
                self.on_file_progress(file_name, 0)

                def _sub_progress(pct: int):
                    raw = (idx + pct / 100) / total * 100
                    overall = max(1, int(raw + 0.5)) if pct > 0 and raw < 1 else int(raw + 0.5)
                    self.on_progress(overall)
                    self.on_file_progress(file_name, pct)

                file_opts = options.copy()
                per_file = task.get("file_options") or {}
                if file_path in per_file:
                    file_opts.update(per_file[file_path])
                # 合并每文件独立页码范围
                per_page = task.get("file_page_options") or {}
                if file_path in per_page:
                    pp = per_page[file_path]
                    file_opts["start_page"] = pp.get("start", 1)
                    file_opts["end_page"] = pp.get("end", 9999)

                timeout_sec = max(MIN_FILE_TIMEOUT,
                                  min(MAX_FILE_TIMEOUT, PER_PAGE_TIMEOUT * file_weights[idx]))
                # 用"可超时的守护线程"代替 ThreadPoolExecutor(1)：
                #   1) 原先 `with _ThreadPool(1)` 在超时退出时会 shutdown(wait=True)，
                #      必须等那个挂死的线程结束 —— 单个坏文件就能让整个任务永久卡住
                #      （界面一直转圈、进度不再前进，用户只能杀进程）。
                #   2) 守护线程 + join(timeout) 才能真正做到"超时就跳过"；遗留线程是
                #      守护线程，不会阻止程序退出。
                #   3) 超时后把 dead 置位，遗留线程之后的进度/结果回调全部作废，
                #      不让它继续污染进度条或重复提交结果。
                dead = threading.Event()
                box = {}

                def _guarded(fn):
                    def _w(*a, **k):
                        if dead.is_set():
                            return
                        try:
                            fn(*a, **k)
                        except Exception:
                            pass
                    return _w

                guarded_progress = _guarded(_sub_progress)

                def _run_one():
                    try:
                        box["result"] = self._process_single(
                            file_path, file_opts, guarded_progress)
                    except BaseException as exc:      # noqa: BLE001 - 完整转交给主线程
                        box["error"] = exc
                        box["tb"] = traceback.format_exc()

                worker = threading.Thread(target=_run_one, daemon=True,
                                          name=f"csf-file-{idx}")
                worker.start()
                worker.join(timeout_sec)
                try:
                    if worker.is_alive():
                        dead.set()                    # 作废遗留线程的一切回调
                        raise TimeoutError(f"处理超时（{timeout_sec}s），已跳过")
                    if "error" in box:
                        raise box["error"]
                    result = box.get("result") or {}
                    self._merge_result(all_results, file_path, result)
                    self.on_file_complete(file_path, {
                        "tables": result.get("tables", []),
                        "text": result.get("text", ""),
                        "failed": False,
                    })
                except TimeoutError as te:
                    err_msg = str(te)
                    self.on_log(f"[超时] [WARN] {file_path}: {err_msg}")
                    all_results["logs"].append(err_msg)
                    self.on_file_complete(file_path, {
                        "tables": [], "text": "", "failed": True, "error": err_msg,
                    })
                except Exception as e:
                    err_msg = str(e)
                    err_tb = box.get("tb") or traceback.format_exc()
                    self.on_log(f"[错误] {file_path}: {e}\n{err_tb}")
                    all_results["logs"].append(err_msg)
                    self.on_file_complete(file_path, {
                        "tables": [], "text": "", "failed": True, "error": err_msg,
                    })
                self.on_progress(int((idx + 1) / total * 100 + 0.5))
        else:
            # ── 多文件并行 ──
            self.on_status(f"并行处理 {total} 个文件（{max_workers} 线程）...")
            self.on_log(f"[并行] 启动 {max_workers} 线程处理 {total} 个文件")

            total_weight = sum(file_weights)
            cum_weights = []
            cw = 0
            for w in file_weights:
                cum_weights.append(cw)
                cw += w

            result_lock = threading.Lock()
            progress_lock = threading.Lock()
            last_progress = [0]

            def _progress_callback(file_idx, file_path):
                base = cum_weights[file_idx] / total_weight * 100
                weight = file_weights[file_idx] / total_weight * 100
                file_name = os.path.basename(file_path)
                def cb(pct: int):
                    with progress_lock:
                        # 四舍五入，且保证 pct>0 时至少 1%
                        raw = base + (pct / 100) * weight
                        val = max(1, int(raw + 0.5)) if pct > 0 and raw < 1 else int(raw + 0.5)
                        if val > last_progress[0]:
                            last_progress[0] = val
                            self.on_progress(val)
                        self.on_file_progress(file_name, pct)
                return cb

            def _process_one(file_path: str, file_idx: int):
                # 取消时不抛异常：抛出去会被 future.result() 重新抛出，
                # 导致整批已完成的汇总结果一起丢掉
                try:
                    self.cancel_check()
                except Exception:
                    return None
                if self._cancel_flag:
                    return None
                file_name = os.path.basename(file_path)
                self.on_file_progress(file_name, 0)
                # 每个线程单独创建 OCR 实例，避免冲突
                from core.ocr_handler import OcrHandler
                ocr = OcrHandler(self.on_log)
                file_opts = options.copy()
                per_file = task.get("file_options") or {}
                if file_path in per_file:
                    file_opts.update(per_file[file_path])
                # 合并每文件独立页码范围
                per_page = task.get("file_page_options") or {}
                if file_path in per_page:
                    pp = per_page[file_path]
                    file_opts["start_page"] = pp.get("start", 1)
                    file_opts["end_page"] = pp.get("end", 9999)
                try:
                    result = self._process_single_with_ocr(file_path, file_opts,
                                                           _progress_callback(file_idx, file_path), ocr)
                    self.on_log(f"  [返回] {os.path.basename(file_path)}: tables={len(result.get('tables',[]))}, text={len(result.get('text',''))}")
                    return (file_path, result)
                except Exception as e:
                    err_tb = traceback.format_exc()
                    self.on_log(f"  [异常] {os.path.basename(file_path)}: {e}")
                    self.on_log(f"  [异常堆栈] {err_tb}")
                    return (file_path, {"tables": [], "text": "", "logs": [err_tb], "toc": []})

            executor = ThreadPoolExecutor(max_workers=max_workers)
            try:
                futures = {
                    executor.submit(_process_one, f, i): (f, i)
                    for i, f in enumerate(files)
                }
                done_weight = [0]
                pending = set(futures.keys())
                # 用 wait(FIRST_COMPLETED) 自己轮转，而不是 as_completed：
                # 这样"卡死判定"是"距上次有文件完成超过 STALL_LIMIT 秒"，
                # 而不是整批的总时限 —— 大批量正常处理不会被误判，某个文件
                # 挂死时又能及时跳过并交回已完成的结果（界面不至于永远转圈）。
                STALL_LIMIT = PARALLEL_STALL_LIMIT
                while pending:
                    done, pending = wait(pending, timeout=STALL_LIMIT,
                                         return_when=FIRST_COMPLETED)
                    if not done:
                        self.on_log(f"[超时] [WARN] 已 {STALL_LIMIT}s 没有任何文件完成，"
                                    f"跳过剩余 {len(pending)} 个文件")
                        all_results["logs"].append(
                            f"处理卡住：{STALL_LIMIT}s 内无进展，已跳过剩余 {len(pending)} 个文件")
                        for f in pending:
                            fpath, _i = futures[f]
                            self.on_file_complete(fpath, {
                                "tables": [], "text": "", "failed": True,
                                "error": "处理超时，已跳过",
                            })
                        break

                    for future in done:
                        file_path, file_idx = futures[future]
                        try:
                            got = future.result()
                        except Exception as e:
                            # 单个文件异常不再让整批失败（原先前一个失败会把
                            # 已完成文件的结果一起丢掉）
                            err_tb = traceback.format_exc()
                            self.on_log(f"  [异常] {os.path.basename(file_path)}: {e}\n{err_tb}")
                            all_results["logs"].append(str(e))
                            self.on_file_complete(file_path, {
                                "tables": [], "text": "", "failed": True, "error": str(e),
                            })
                            got = None
                        if got is None:
                            # 用户取消（_process_one 返回 None）→ 不再计入进度
                            cancelled = True
                            continue
                        _fp, result = got
                        file_tables = len(result.get("tables", []))
                        self.on_log(f"  => {os.path.basename(_fp)}: 提取 {file_tables} 条标准记录, 文本 {len(result.get('text',''))} 字符")
                        with result_lock:
                            self._merge_result(all_results, _fp, result)
                        self.on_file_complete(_fp, {
                            "tables": result.get("tables", []),
                            "text": result.get("text", ""),
                            # 与顺序模式保持一致：只有真异常才算失败，
                            # "有文本但没提取到标准"不算失败
                            "failed": False,
                            "error": None,
                        })
                        with progress_lock:
                            # 用 futures 里已存的索引，避免 files.index() 在
                            # 同一路径重复出现时取错权重
                            done_weight[0] += file_weights[file_idx]
                        done_pct = int(done_weight[0] / total_weight * 100 + 0.5)
                        if done_pct > last_progress[0]:
                            last_progress[0] = done_pct
                            self.on_progress(done_pct)
                        self.on_log(f"[完成] {os.path.basename(_fp)} ({done_weight[0]}/{total_weight}页)")
            finally:
                # 不再等待仍在跑的文件：cancel_futures 会取消排队中的任务，
                # 已在跑的线程被放弃（守护性质由进程退出兜底），
                # 这样"某个文件挂死"不会让整个任务永远结束不了。
                executor.shutdown(wait=False, cancel_futures=True)

        # 聚合统计 + AI 增强（单线程，后续步骤）
        total_tables = len(all_results.get("tables", []))
        self.on_log(f"[最终] 所有文件处理完毕: 累计 {total_tables} 条标准记录, 文本 {len(all_results.get('text',''))} 字符")
        all_results["stats"] = self._aggregate_stats(all_results["tables"])
        stats_keys = list(all_results.get("stats", {}).keys())
        self.on_log(f"[最终] 统计信息: {len(stats_keys)} 个不同标准 {' '.join(stats_keys[:5])}{'...' if len(stats_keys)>5 else ''}")
        if cancelled:
            # 用户终止：把上面已汇总的部分结果原样交出去（不再抛异常），
            # 跳过逐行走网络的 AI 增强，避免"点了终止还要等联网"
            all_results["cancelled"] = True
            self.on_log("[已取消] [WARN] 处理被用户终止，已完成文件的结果已保留")
            self.on_status("已取消")
            return all_results

        if options.get("ai_enhance", True):
            all_results = self._ai_enhance(all_results)
        self.on_status("处理完成")
        return all_results

        # 去重 key：规范化标准号（去掉所有空白、统一连字符）+ 位置 + 文件。
    # 用规范化标准号而非完整显示名，是因为同一条标准可能被不同提取路径
    # 记成不同写法（TB/T 46-2020 / TB/T46-2020 / 折行 TB/T\n46-2020），
    # 若按字面比较会导致同一位置重复计数 2 次。
    @staticmethod
    def _dedup_key(row: dict) -> tuple:
        std_num = standard_no_of(row.get("name", "") or "")
        return (normalize_std_no(std_num), row.get("page", ""), row.get("file", ""))

    def _merge_result(self, all_results: dict, file_path: str, result: dict):
        """合并单个文件结果到总结果，附带 (规范化标准号, page, file) 去重"""
        # 从已有结果建立去重 key 集合
        existing = set()
        for row in all_results.get("tables", []):
            existing.add(self._dedup_key(row))

        for row in result.get("tables", []):
            row_name = row.get("name", "")
            # 处理逻辑：
            #   无《》→ 纯标准号（名称缺失），直接保留
            #   有《》且标准号在大库中 → 保留完整《名称》（已确认）
            #   有《》但标准号不在大库中 → **保留候选名并标记待核**：
            #     原先直接剥掉名称只留标准号，用户看到裸露编号会以为
            #     "系统没识别出来"（P0-9），名称信息被静默丢弃。
            #     现在保留候选 + name_pending，界面显示"待核"角标，
            #     联网/AI 补名成功后会自动清除该标记（见 server 补名链路）。
            # 用 lookup_standard_dict 兼容空白差异（PDF 折行 / 录入清洗）
            if "《" in row_name:
                std_num = standard_no_of(row_name)
                if lookup_standard_dict(std_num) is None:
                    row["name_pending"] = True
                    row.setdefault("name_source", "原文")
            row.setdefault("file", file_path)
            row.setdefault("file_name", os.path.basename(file_path))
            key = self._dedup_key(row)
            if key in existing:
                continue
            existing.add(key)
            all_results["tables"].append(row)
        all_results["text"] += f"\n\n===== {os.path.basename(file_path)} =====\n"
        all_results["text"] += result.get("text", "")
        all_results["logs"].extend(result.get("logs", []))
        all_results["toc"].extend(result.get("toc", []))

    def _process_single_with_ocr(self, file_path: str, options: Dict[str, Any],
                                  on_progress: Callable[[int], None],
                                  ocr: 'OcrHandler') -> Dict[str, Any]:
        """用指定的 OCR 实例处理单文件（多线程用）"""
        doc_type = self.router.detect_type(file_path)
        if doc_type == "doc":
            file_path = DocxHandler.convert_doc_to_docx(file_path, self.on_log)
            doc_type = "docx"
        elif doc_type == "excel" and ExcelHandler.is_legacy_xls(file_path):
            # 旧版 .xls：openpyxl 读不了，先用 Excel COM 转成 .xlsx
            file_path = ExcelHandler.convert_xls_to_xlsx(file_path, self.on_log)
        if doc_type == "docx":
            handler = DocxHandler(self.on_log, ocr, on_progress)
            handler.cancel_check = self.cancel_check
            return handler.process(file_path, options)
        elif doc_type == "excel":
            handler = ExcelHandler(self.on_log, on_progress)
            handler.cancel_check = self.cancel_check
            return handler.process(file_path, options)
        elif doc_type == "pptx":
            handler = PptxHandler(self.on_log, on_progress)
            handler.cancel_check = self.cancel_check
            return handler.process(file_path, options)
        elif doc_type == "text":
            handler = TextHandler(self.on_log, on_progress)
            handler.cancel_check = self.cancel_check
            return handler.process(file_path, options)
        elif doc_type == "pdf":
            handler = PdfHandler(self.on_log, ocr, on_progress, on_status=self.on_status)
            handler.cancel_check = self.cancel_check
            return handler.process(file_path, options)
        elif doc_type == "ppt":
            msg = "不支持的旧版 .ppt 格式，请用 PowerPoint 另存为 .pptx 后重试"
            self.on_log(f"  [跳过] {msg}")
            return {"tables": [], "text": "", "logs": [msg], "toc": []}
        else:
            return {"tables": [], "text": "", "logs": [f"不支持的文件类型: {doc_type}"], "toc": []}

    def _process_single(self, file_path: str, options: Dict[str, Any],
                        on_progress: Callable[[int], None]) -> Dict[str, Any]:
        doc_type = self.router.detect_type(file_path)
        self.on_log(f"  文档类型: {doc_type}")

        if doc_type == "doc":
            file_path = DocxHandler.convert_doc_to_docx(file_path, self.on_log)
            doc_type = "docx"
        elif doc_type == "excel" and ExcelHandler.is_legacy_xls(file_path):
            # 旧版 .xls：openpyxl 读不了，先用 Excel COM 转成 .xlsx
            file_path = ExcelHandler.convert_xls_to_xlsx(file_path, self.on_log)

        if doc_type == "docx":
            handler = DocxHandler(self.on_log, self.ocr, on_progress)
            handler.cancel_check = self.cancel_check
            return handler.process(file_path, options)
        elif doc_type == "excel":
            handler = ExcelHandler(self.on_log, on_progress)
            handler.cancel_check = self.cancel_check
            return handler.process(file_path, options)
        elif doc_type == "pptx":
            handler = PptxHandler(self.on_log, on_progress)
            handler.cancel_check = self.cancel_check
            return handler.process(file_path, options)
        elif doc_type == "text":
            handler = TextHandler(self.on_log, on_progress)
            handler.cancel_check = self.cancel_check
            return handler.process(file_path, options)
        elif doc_type == "pdf":
            handler = PdfHandler(self.on_log, self.ocr, on_progress, on_status=self.on_status)
            handler.cancel_check = self.cancel_check
            return handler.process(file_path, options)
        elif doc_type == "ppt":
            msg = "不支持的旧版 .ppt 格式，请用 PowerPoint 另存为 .pptx 后重试"
            self.on_log(f"  [跳过] {msg}")
            return {"tables": [], "text": "", "logs": [msg], "toc": []}
        else:
            return {"tables": [], "text": "", "logs": [f"不支持的文件类型: {doc_type}"], "toc": []}



    def _ai_enhance(self, all_results: Dict[str, Any]) -> Dict[str, Any]:
        """用本地 AI 补全「只有编号、没有名称」的条目。

        改造要点（依智能化评估报告 E-1）：
          · **只对没有名称的行调用**：原来对全部行逐条发请求，已有《名称》的行
            纯属白花时间 —— 通常可直接砍掉 60~90% 的请求量，且零风险；
          · 连续 3 次失败即整体放弃：服务不可用 / 内网不通时不再逐条空等；
          · 进度写成"AI 正在核对 x/N 条名称"，不再是含糊的"AI 解析表格中…"。
        """
        if not self.ai._available():
            self.on_log("  本地 AI 未配置或已按策略禁用，跳过智能解析")
            return all_results

        tables = all_results.get("tables", [])
        # 只挑"没有名称"的：name 不含《》说明还是裸编号（字典与联网都没补上）
        pending = [r for r in tables if r.get("name") and "《" not in r.get("name", "")]
        if not pending:
            self.on_log("  [AI] 没有需要补全的条目（均已有名称），跳过")
            return all_results

        total_pending = len(pending)
        self.on_log(f"  [AI] 待核对 {total_pending} 条（共 {len(tables)} 条，"
                    f"已有名称的 {len(tables) - total_pending} 条不重复请求）")
        self.on_status(f"AI 正在核对 0/{total_pending} 条名称（已出结果可先查看）")

        consecutive_fail = 0
        done = 0
        for row in pending:
            self.cancel_check()
            try:
                ai_result = self.ai.complete_table(row.get("content", ""), row.get("page", ""))
            except Exception as e:
                self.on_log(f"  [AI] 调用失败（第 {consecutive_fail + 1} 次连续）: {e}")
                ai_result = None
            if not ai_result:
                consecutive_fail += 1
                if consecutive_fail >= 3:
                    self.on_log("  [AI] 连续 3 次无有效返回，判定服务不可用，"
                                "本次 AI 增强已跳过（条目保持原编号）")
                    self.on_status("AI 增强已跳过（服务不可用）")
                    return all_results
                continue
            consecutive_fail = 0
            done += 1
            if ai_result.get("standard_name"):
                cand = str(ai_result["standard_name"]).strip()
                # 校验后再覆盖：本地模型可能返回"无法确定"/整句自由文本/幻觉编号，
                # 直接写进 name 会把原本正确的标准号改错，统计计数也跟着改错，
                # 而且没有留痕（原实现就是这样，且 ai_handler 的 validate_result
                # 从未被调用）。
                if _looks_like_std_no(cand):
                    row["name"] = cand
                    row["source"] = "AI"
                    row.pop("name_pending", None)
                else:
                    self.on_log(f"  [AI] 忽略不可信的编号建议: {cand[:40]}")
            row["ai_confidence"] = ai_result.get("confidence", 0.0)
            if ai_result.get("columns"):
                row["ai_columns"] = ai_result["columns"]
            if ai_result.get("rows"):
                row["ai_rows"] = ai_result["rows"]
            self.on_status(f"AI 正在核对 {done}/{total_pending} 条名称（已出结果可先查看）")
        self.on_log(f"  [AI] 增强完成，实际核对 {done}/{total_pending} 条")
        # AI 可能修正了标准名称，重新统计
        all_results["stats"] = self._aggregate_stats(all_results["tables"])
        return all_results

    def _process_import(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """录入模式：把目录型 PDF 中的每条标准批量导入主字典。

        与标准提取模式不同的是：
          - 不进行正文/上下文搜索
          - 每张表行 = 一条标准条目，直接写库
          - 返回结构保留 `tables`（兼容前端展示）+ `imported/skipped/stats`
        """
        from core.import_handler import ImportHandler

        files = task.get("files", [])
        start_page = task.get("start_page", 1)
        end_page = task.get("end_page", 9999)
        update_existing = bool(task.get("update_existing", False))
        self.on_status("录入模式启动...")
        self.on_log(f"[录入模式] 收到 {len(files)} 个文件")

        all_results = {
            "tables": [], "text": "", "logs": [],
            "imported": [], "skipped": [],
            "stats": {}, "toc": [],
            "process_mode": "import",
        }
        if not files:
            self.on_log("[警告] 未选择任何文件")
            self.on_status("未选择文件")
            return all_results

        total = len(files)
        self.on_progress(0)
        cancelled = False
        for idx, file_path in enumerate(files):
            # 取消时保留已录入的部分（与标准提取模式一致，不再整批失败）
            try:
                self.cancel_check()
            except Exception:
                cancelled = True
                break
            file_name = os.path.basename(file_path)
            self.on_log(f"[录入] 开始处理 {file_path}")
            self.on_status(f"正在录入: {file_name}")

            def _sub_progress(pct: int):
                overall = int((idx + pct / 100) / total * 100 + 0.5)
                self.on_progress(overall)
                self.on_file_progress(file_name, pct)

            try:
                handler = ImportHandler(self.on_log, _sub_progress, self.on_status)
                handler.cancel_check = self.cancel_check
                result = handler.process(file_path, {
                    "start_page": start_page,
                    "end_page": end_page,
                    "update_existing": update_existing,
                })
                # 合并到总结果
                for row in result.get("tables", []):
                    row["file"] = file_path
                    row["file_name"] = file_name
                    all_results["tables"].append(row)
                # imported/skipped 附带来源文件名（前端按文件分组展示、小目录定位用）
                for item in result.get("imported", []):
                    item["file_name"] = file_name
                    all_results["imported"].append(item)
                for item in result.get("skipped", []):
                    item["file_name"] = file_name
                    all_results["skipped"].append(item)
                all_results["logs"].extend(result.get("logs", []))
                self.on_file_complete(file_path, {
                    "tables": result.get("tables", []),
                    "text": "",
                    "failed": False,
                    "imported_count": len(result.get("imported", [])),
                    "skipped_count": len(result.get("skipped", [])),
                })
            except Exception as e:
                err_tb = ""
                try:
                    import traceback as _tb
                    err_tb = _tb.format_exc()
                except Exception:
                    pass
                self.on_log(f"[录入] {file_path} 处理异常: {e}\n{err_tb}")
                all_results["logs"].append(str(e))
                self.on_file_complete(file_path, {
                    "tables": [], "text": "", "failed": True, "error": str(e),
                })
            self.on_progress(int((idx + 1) / total * 100 + 0.5))

        # 统计
        all_results["stats"] = {
            "imported": len(all_results["imported"]),
            "skipped": len(all_results["skipped"]),
            "total_rows": len(all_results["tables"]),
            "update_existing": update_existing,
        }
        self.on_log(
            f"[录入完成] 共解析 {len(all_results['tables'])} 行，"
            f"成功导入 {len(all_results['imported'])} 条，跳过 {len(all_results['skipped'])} 条"
        )
        if cancelled:
            all_results["cancelled"] = True
            self.on_log("[已取消] [WARN] 录入被用户终止，已完成部分已保留")
            self.on_status("已取消")
            return all_results
        self.on_status("录入完成")
        return all_results

    def _aggregate_stats(self, tables: List[Dict[str, Any]]) -> Dict[str, Any]:
        """聚合统计：标准名称 -> 出现次数、出现位置、按文件/文件夹统计。"""
        stats = defaultdict(lambda: {
            "count": 0,
            "locations": [],
            "file_counts": defaultdict(int),
            "folder_counts": defaultdict(int),
        })
        for row in tables:
            name = row.get("name", "")
            if not name:
                continue
            stats[name]["count"] += 1
            file_path = row.get("file", "")
            file_name = row.get("file_name", "")
            folder = os.path.dirname(file_path) if file_path else "未知文件夹"
            folder_name = os.path.basename(folder) if folder else "未知文件夹"
            stats[name]["locations"].append({
                "file": file_path,
                "file_name": file_name,
                "page": row.get("page", ""),
                "content": row.get("content", "")[:200],
            })
            stats[name]["file_counts"][file_name] += 1
            stats[name]["folder_counts"][folder_name] += 1
        # 将 defaultdict 转换为普通 dict 以便 JSON 序列化
        result = {}
        for k, v in sorted(stats.items(), key=lambda x: -x[1]["count"]):
            v["file_counts"] = dict(v["file_counts"])
            v["folder_counts"] = dict(v["folder_counts"])
            result[k] = dict(v)
        return result
