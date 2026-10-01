# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import os
import asyncio
from typing import Dict, Any, List, Callable

from utils.helpers import search_standard_names, resolve_standard_display, find_context


class OcrHandler:
    """OCR 处理引擎：优先 Windows 内置 OCR，其次 Tesseract，最后 PaddleOCR。"""

    def __init__(self, on_log: Callable[[str], None] = None):
        self.on_log = on_log or (lambda x: None)
        self._engine = None
        self._engine_type = None
        self._available = False
        self._init_error = None

    def _load_engine(self):
        if self._engine is not None or self._available:
            return
        # 优先 Tesseract（快，用户已安装）
        try:
            import pytesseract
            # 设置 tesseract 可执行文件路径（避免环境变量 PATH 未配置）
            common_paths = [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]
            for p in common_paths:
                if os.path.exists(p):
                    pytesseract.pytesseract.tesseract_cmd = p
                    break
            pytesseract.get_tesseract_version()
            self._engine = pytesseract
            self._engine_type = "tesseract"
            self._available = True
            self.on_log("  OCR 引擎加载成功 (Tesseract)")
            return
        except Exception as e:
            self.on_log(f"  Tesseract 加载失败: {e}")
        # 其次 PaddleOCR（中文效果好，但较慢）
        try:
            from paddleocr import PaddleOCR
            try:
                self._engine = PaddleOCR(lang="ch")
            except (TypeError, ValueError):
                try:
                    self._engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
                except (TypeError, ValueError):
                    self._engine = PaddleOCR(use_angle_cls=True, lang="ch")
            self._engine_type = "paddleocr"
            self._available = True
            self.on_log("  OCR 引擎加载成功 (PaddleOCR)")
            return
        except Exception as e:
            self.on_log(f"  PaddleOCR 加载失败: {e}")
        # 最后 Windows 内置 OCR
        try:
            import winsdk.windows.media.ocr as ocr
            engine = ocr.OcrEngine.try_create_from_user_profile_languages()
            if engine is not None:
                self._engine = "windows"
                self._engine_type = "windows"
                self._available = True
                self.on_log("  OCR 引擎加载成功 (Windows 内置 OCR)")
            else:
                self.on_log("  Windows 内置 OCR 不可用，可能缺少中文语言包")
        except Exception as e:
            self.on_log(f"  Windows 内置 OCR 加载失败: {e}")
            self._init_error = f"Tesseract: {e}; PaddleOCR: {e}; Windows: {e}"
            self.on_log(f"  未找到可用的 OCR 引擎，扫描页将跳过 OCR 识别")

    def is_available(self) -> bool:
        self._load_engine()
        return self._available

    def recognize(self, image_path: str) -> Dict[str, Any]:
        self._load_engine()
        result = {"tables": [], "text": ""}
        if not self._available:
            self.on_log(f"  [OCR跳过] 未安装 OCR 引擎，跳过图片识别")
            result["text"] = "\n[OCR跳过] 未安装 OCR 引擎，请安装 paddleocr 或 pytesseract+tesseract-ocr"
            return result
        self.on_log(f"  [OCR] {image_path}")
        try:
            if self._engine_type == "paddleocr":
                # 新版 PaddleOCR (pipelines) 的 predict/ocr 方法签名不同
                try:
                    ocr_result = self._engine.ocr(image_path, cls=True)
                except TypeError:
                    ocr_result = self._engine.ocr(image_path)
                texts = []
                for line in (ocr_result[0] or []):
                    if line:
                        texts.append(line[1][0])
                full_text = "\n".join(texts)
            elif self._engine_type == "windows":
                # 直接从文件路径加载，绕过 DataWriter/bytes 转换的坑
                async def _win_ocr_async(path):
                    import winsdk.windows.storage as storage
                    import winsdk.windows.graphics.imaging as imaging
                    import winsdk.windows.media.ocr as ocr

                    file = await storage.StorageFile.get_file_from_path_async(path)
                    stream = await file.open_async(storage.FileAccessMode.READ)
                    bitmap = await imaging.BitmapDecoder.create_async(stream)
                    sb = await bitmap.get_software_bitmap_async()

                    engine = ocr.OcrEngine.try_create_from_user_profile_languages()
                    if engine is None:
                        return "[Windows OCR 失败] 无法创建 OCR 引擎，请检查系统语言包"
                    result = await engine.recognize_async(sb)
                    return result.text

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    full_text = loop.run_until_complete(_win_ocr_async(image_path))
                finally:
                    loop.close()
            else:
                full_text = self._engine.image_to_string(image_path, lang="chi_sim+eng")

            result["text"] = f"\n[OCR 识别]\n{full_text}"
            for name in search_standard_names(full_text):
                display_name = resolve_standard_display(full_text, name)
                result["tables"].append({
                    "name": display_name,
                    # 证据句取标准号周围 80 字窗口（与 PDF/DOCX 文本路径一致）。
                    # 原先取的是"整页开头 200 字符"，与标准号位置无关，
                    # 结果页"内容"列和 AI 增强拿到的上下文都是错的。
                    "content": find_context(full_text, name),
                    "page": os.path.basename(image_path),
                    "source": "OCR",
                })
        except Exception as e:
            self.on_log(f"  OCR 识别失败: {e}")
            result["text"] = f"\n[OCR 失败] {e}"
        return result

    def recognize_table(self, image_path: str) -> List[List[Any]]:
        """尝试识别图片中的表格结构，返回二维数组。"""
        self._load_engine()
        if not self._available:
            self.on_log(f"  [OCR跳过] 未安装 OCR 引擎，跳过表格识别")
            return []
        try:
            if self._engine_type == "paddleocr":
                # PaddleOCR 表格识别需要 ppstructure，简单返回文本行
                result = self.recognize(image_path)
                lines = result.get("text", "").split("\n")
                return [[line] for line in lines if line.strip()]
            elif self._engine_type == "windows":
                result = self.recognize(image_path)
                lines = result.get("text", "").split("\n")
                return [[line] for line in lines if line.strip()]
            else:
                text = self._engine.image_to_string(image_path, lang="chi_sim+eng")
                return [[line] for line in text.split("\n") if line.strip()]
        except Exception as e:
            self.on_log(f"  表格 OCR 失败: {e}")
            return []
