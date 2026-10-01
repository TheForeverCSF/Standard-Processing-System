# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import re
import os
import sys
import hashlib
import threading
import unicodedata
from typing import List, Dict, Any


def extract_text_from_runs(paragraphs) -> str:
    """从段落列表中提取完整文本，保留超链接和样式文本。"""
    texts = []
    for para in paragraphs:
        texts.append(para.text.strip())
    return " ".join(texts).strip()


_search_cache: dict = {}

# ── 标准编号模式表 ─────────────────────────────────────────
# 三条硬要求（都是踩过的坑）：
#   1) 字母前缀前必须加词界 (?<![A-Za-z])：否则英文正文里的 "then 2020"、
#      "beautiful 2" 会被当成 EN 2020 / UL 2，凭空造出标准号并触发联网查询。
#   2) 数字部分要允许 . / - / : 多段重复：GB/T 1.1-2020（分部号）、
#      GB/T 1234:2020（冒号年份）、EN 1992-1-1（多段）。
#   3) 必须覆盖**行业/地方/团体/企业标准**：本系统面向铁路/中车场景，
#      TB/T、Q/CR、DB 这类编号是最常见的一类，原先的正则只认 GB/ISO/EN 等，
#      等于这类文档提取结果为 0。
_STD_NUM = r"\d+(?:\.\d+)*(?:\s*[-:/]\s*\d+)*"
_STD_PATTERNS = [
    # ── 中国国家标准（GB/T、GB/Z 指导性文件、GB、GBT 粘连写法）──
    r"(?<![A-Za-z])GB\s*/?\s*(?:T|Z)?\s*" + _STD_NUM,
    r"(?<![A-Za-z])GBT\s*" + _STD_NUM,
    # ── 行业标准（TB/T 铁路、JT/T 交通、HG/T 化工 …）──
    r"(?<![A-Za-z])(?:TB|JT|HG|SH|QB|SN|YY|JC|HJ|JG|SL|NY|LY|WS|GA|DL|NB|SY"
    r"|MT|AQ|FZ|JB|HY|SC|SB|WB|YC|YS|YZ|ZY|CJ|CY|DZ|EJ|GC|GF|TJ|YD|CH"
    r"|JJF|JJG|CJJ|CECS)\s*(?:/\s*[TZ])?\s*" + _STD_NUM,
    # ── 地方标准 DB11/T 123-2020 / 企业标准 Q/CR 9001-2020 / 团体标准 T/CECS ──
    r"(?<![A-Za-z])DB\s*\d{1,2}\s*(?:/\s*T)?\s*" + _STD_NUM,
    r"(?<![A-Za-z])Q\s*/\s*[A-Z]{1,6}\s*" + _STD_NUM,
    r"(?<![A-Za-z])T\s*/\s*[A-Z]{2,8}\s*" + _STD_NUM,
    # ── 国际/国外标准 ──
    # ISO/IEC 必须排在 ISO 之前，否则会被截成 IEC xxx（编号错了会查错标准）
    r"(?<![A-Za-z])ISO\s*/\s*IEC\s*" + _STD_NUM,
    r"(?<![A-Za-z])ISO\s*" + _STD_NUM,
    r"(?<![A-Za-z])IEC\s*" + _STD_NUM,
    # EN/EU/UL 要求编号 ≥2 位，避免 "then 2020" 这类误报
    r"(?<![A-Za-z])EU\s+\d{2,}(?:\.\d+)*(?:\s*[-:/]\s*\d+)*",
    r"(?<![A-Za-z])EN\s+\d{2,}(?:\.\d+)*(?:\s*[-:/]\s*\d+)*",
    r"(?<![A-Za-z])ASTM\s*[A-Z]?\s*\d+(?:\s*[-/\s]*\d+)*",
    r"(?<![A-Za-z])DIN\s*(?:EN\s*)?\d+(?:\s*[-/\s]*\d+)*",
    r"(?<![A-Za-z])JIS\s*[A-Z]\s*\d+(?:\s*[-\s]*\d+)*",
    r"(?<![A-Za-z])IEEE\s*\d+(?:\.\d+)*",
    r"(?<![A-Za-z])UL\s+\d{2,}(?:\s*[-\s]*\d+)*",
    r"(?<![A-Za-z])MIL\s*-\s*STD\s*-\s*\d+",
]

# ── 前缀开关 → 正则编译 ─────────────────────────────────────
# 背景（非常重要）：设置页的「标准管理」可以逐项开关 76 类行业前缀、7 个外国标准、
# 团体标准，并保存到 builtin_prefixes.json；但识别链路一直是**写死的正则表**，
# 于是出现两种对称的信任损失：
#   · 打开也不认：SJ(电子)、JGJ/JTG(建筑公路)、MH(民航)、CB(船舶)、DA(档案)… 38 类
#     压根不在正则里；BS/UNE/NF/KS/AS/CAN/ANSI 与 GJB 也同样缺失；
#   · 关掉也认：Q/、T/xxx、ISO 等命中正则就无条件命中，与开关无关。
# 下面按"启用的前缀集合"现编译正则，无配置时仍用上面的默认表（保持旧行为）。
_SPECIAL_PATTERNS = {
    # 这些前缀有各自的专用形态，不能套用通用模板（语义与下方默认表完全一致）
    "GB": [r"(?<![A-Za-z])GB\s*/?\s*(?:T|Z)?\s*" + _STD_NUM,
           r"(?<![A-Za-z])GBT\s*" + _STD_NUM],
    "DB": [r"(?<![A-Za-z])DB\s*\d{1,2}\s*(?:/\s*T)?\s*" + _STD_NUM],
    "EU": [r"(?<![A-Za-z])EU\s+\d{2,}(?:\.\d+)*(?:\s*[-:/]\s*\d+)*"],
    "EN": [r"(?<![A-Za-z])EN\s+\d{2,}(?:\.\d+)*(?:\s*[-:/]\s*\d+)*"],
    "ASTM": [r"(?<![A-Za-z])ASTM\s*[A-Z]?\s*\d+(?:\s*[-/\s]*\d+)*"],
    "DIN": [r"(?<![A-Za-z])DIN\s*(?:EN\s*)?\d+(?:\s*[-/\s]*\d+)*"],
    "JIS": [r"(?<![A-Za-z])JIS\s*[A-Z]\s*\d+(?:\s*[-\s]*\d+)*"],
    "IEEE": [r"(?<![A-Za-z])IEEE\s*\d+(?:\.\d+)*"],
    "UL": [r"(?<![A-Za-z])UL\s+\d{2,}(?:\s*[-\s]*\d+)*"],
    "MIL": [r"(?<![A-Za-z])MIL\s*-\s*STD\s*-\s*\d+"],
}


# 编号前可能带"设计代字"的前缀（ANSI Z87.1 / KS B 1234 / AS 1234 之类）。
# 顾问 N-5：JTG/JTS（公路行业，中交系统常用）整族都带设计代字
#   JGJ 系列：JTG D60-2015、JTG/T D60-01-2004、JTG E30-2005
# 不在这个集合里就永远认不出（配置里声明了也白搭）——"打开也不认"最伤信任。
_DESIGNATOR_PREFIXES = {"ANSI", "KS", "AS", "CAN", "UNE", "NF", "BS", "JTG", "JTS"}


def _compile_patterns(prefixes) -> List[str]:
    """把"启用的前缀集合"编译成正则列表（顺序敏感，不要随意重排）。"""
    up = {str(p).strip().upper() for p in (prefixes or []) if str(p).strip()}
    if not up:
        return list(_STD_PATTERNS)
    pats: List[str] = []

    # 1) ISO/IEC 组合形态必须最先尝试：否则 ISO 先命中，把编号截成 IEC xxx
    if {"ISO", "IEC"} <= up:
        pats.append(r"(?<![A-Za-z])ISO\s*/\s*IEC\s*" + _STD_NUM)

    # 2) 专用形态（含 EN/EU/UL 要求编号 ≥2 位，避免 then 2020 → EN 2020 这类误报）
    for p in ("GB", "DB", "EU", "EN", "ASTM", "DIN", "JIS", "IEEE", "UL", "MIL"):
        if p in up:
            pats.extend(_SPECIAL_PATTERNS[p])

    # 3) 通用形态。按长度倒序，保证 T/CRSC 先于 T/CRS 命中
    for p in sorted(up - set(_SPECIAL_PATTERNS), key=len, reverse=True):
        if p in ("ISO", "IEC"):
            pats.append(r"(?<![A-Za-z])" + p + r"\s*" + _STD_NUM)
        elif p.endswith("/"):
            # 只声明了 "Q/" 这种：允许任意机构字母（与旧正则 Q\s*/\s*[A-Z]{1,6} 一致）
            head = re.escape(p[:-1])
            pats.append(r"(?<![A-Za-z])" + head + r"\s*/\s*[A-Z]{1,6}\s*" + _STD_NUM)
        elif "/" in p:
            # 具体机构：Q/CR、T/CRS、GB/T …
            head, tail = p.split("/", 1)
            pats.append(r"(?<![A-Za-z])" + re.escape(head) + r"\s*/\s*"
                        + re.escape(tail) + r"\s*" + _STD_NUM)
        else:
            # 纯字母前缀（TB/JT/SJ/JGJ/GJB…）：允许 /T、/Z 变体
            # 国外标准常在编号前带一个设计代字（ANSI Z87.1、KS B 1234），
            # 这里**只允许单个字母**：允许多字母会重新引入
            # "then 2020 → EN 2020" 那类凭空造号的误报（踩过的坑）。
            if p in _DESIGNATOR_PREFIXES:
                # 外国标准（BS/UNE/NF/KS/AS/CAN/ANSI）必须要求编号 ≥2 位 ——
                # 与 EN/EU/UL 同理（见 _SPECIAL_PATTERNS 里的注释）。
                # 顾问实测：1 位数字时英文正文会被误判成标准号
                # （"we would be 3" → AS 3、"can 2"、"bs 5"），
                # 而真实号段全是 ≥2 位（ANSI Z87.1、KS B 1234、BS 1234:2020、
                # AS 1234、CAN 1234、UNE 1234、NF 1234）。
                # 假阳性比漏识别更伤结果可信度：用户会把 "AS 3" 当成真标准去查。
                designator = r"[A-Z]?\s*"
                pats.append(r"(?<![A-Za-z])" + re.escape(p) + r"\s*(?:/\s*[TZ])?\s*"
                            + designator + r"\d{2,}(?:\.\d+)*(?:\s*[-:/]\s*\d+)*")
                continue
            # 其它纯字母前缀（TB/JT/SJ/JGJ/GJB…）：允许 /T、/Z 变体。
            # 这里**不允许设计代字**：允许多字母会重新引入
            # "then 2020 → EN 2020" 那类凭空造号的误报（踩过的坑）。
            pats.append(r"(?<![A-Za-z])" + re.escape(p) + r"\s*(?:/\s*[TZ])?\s*"
                        + _STD_NUM)
    return pats


# 当前生效的正则表；None 表示"未配置，用内置默认表"
_ACTIVE_PREFIXES = None
_ACTIVE_PATTERNS = list(_STD_PATTERNS)


def set_enabled_prefixes(prefixes) -> int:
    """注入"当前启用的前缀集合"（由 web/server.py 在启动与保存设置时调用）。

    prefixes 为空 / None → 回到内置默认表，避免"一个前缀都没启用"导致
    整份文档一条都识别不出来（那比开关不生效更糟）。
    返回实际生效的正则条数，便于日志与测试。
    """
    global _ACTIVE_PREFIXES, _ACTIVE_PATTERNS
    if not prefixes:
        _ACTIVE_PREFIXES = None
        _ACTIVE_PATTERNS = list(_STD_PATTERNS)
    else:
        _ACTIVE_PREFIXES = frozenset(str(p).strip().upper() for p in prefixes if str(p).strip())
        _ACTIVE_PATTERNS = _compile_patterns(_ACTIVE_PREFIXES)
    _search_cache.clear()   # 正则变了，缓存必须失效
    return len(_ACTIVE_PATTERNS)


def get_enabled_prefixes():
    """返回当前生效的前缀集合（None = 内置默认）"""
    return _ACTIVE_PREFIXES


def search_standard_names(text: str, prefixes=None) -> List[str]:
    """全局搜索标准名称，例如 GB/T、ISO、IEC 等。

    prefixes=None 时使用 `set_enabled_prefixes()` 注入的集合（未注入则用内置默认表）；
    显式传入时按该集合现编译，便于单测与"只认某几类"的场景。
    """
    if not text:
        return []
    patterns = _compile_patterns(prefixes) if prefixes else _ACTIVE_PATTERNS
    # 小文本（<500字符）不缓存，大文本缓存结果避免重复计算。
    # 用 sha1 而不是 hash(text) % 10_000_000：后者键空间只有一千万，
    # 不同文本撞键会返回**其它文档**的标准号列表（静默的错误结果）。
    use_cache = len(text) > 500
    # 缓存键带上"生效的前缀集合"：否则显式传不同 prefixes 的调用会互相命中缓存，
    # 返回另一套前缀下的标准号（静默错误结果）
    _disc = ("|" + ",".join(sorted(str(p).strip().upper() for p in prefixes))) if prefixes else "|active"
    key = None
    if use_cache:
        key = hashlib.sha1((_disc + "\x00" + text).encode("utf-8", "ignore")).hexdigest()
        if key in _search_cache:
            return _search_cache[key]
    # 统一全角、特殊空白和连字符，避免漏识别
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"[\s\u200b\u00a0]+", " ", normalized)
    # 统一各种连字符为普通短横线
    normalized = re.sub(r"[-–—−]", "-", normalized)
    found = set()
    for pat in patterns:
        for m in re.finditer(pat, normalized, re.IGNORECASE):
            # 清理后保留标准格式
            name = m.group(0).upper()
            # 统一连字符周围空格
            name = re.sub(r"\s*-\s*", "-", name)
            # 统一「前缀 + 数字」之间的空格。
            # 顺序很重要：GB 系列必须先归一到 GB/T｜GB，再做通用规则，
            # 否则通用的"字母+数字切分"会把 GBT1234 切成 "GB/T 12 34"。
            name = re.sub(r"^GB\s*/\s*T\s*(\d)", r"GB/T \1", name)
            name = re.sub(r"^GBT\s*(\d)", r"GB/T \1", name)
            name = re.sub(r"^GB\s*(\d)", r"GB \1", name)
            # 斜杠式前缀：TB/T46-2020 → TB/T 46-2020、Q/CR9001 → Q/CR 9001
            name = re.sub(r"^([A-Z]{1,5}\s*/\s*[A-Z]{1,6})\s*(\d)", r"\1 \2", name)
            # 地方标准：DB11/T123 → DB11/T 123
            name = re.sub(r"^(DB\d{1,2}\s*/\s*[A-Z])\s*(\d)", r"\1 \2", name)
            # 纯字母前缀粘连四位以上数字：TB10001-2016 → TB 10001-2016
            name = re.sub(r"^([A-Z]{2,5})(\d{4,})", r"\1 \2", name)
            # 去除多余空格但保留前缀与数字之间的空格
            name = re.sub(r"\s+", " ", name).strip()
            name = re.sub(r"\s*/\s*", "/", name)
            found.add(name)
    # 去掉"被更长匹配整体包含"的短匹配：
    # 例如 ISO/IEC 27001 命中后，IEC 27001 是它的子串，留着会多出一条错误编号。
    result = sorted(n for n in found
                    if not any(n != o and n in o for o in found))
    if use_cache:
        _search_cache[key] = result
    return result


# ---- 本地标准名称字典（SQLite 数据库）----
# 主库表 main(standard_no, display_name) — 人工审核确认的标准
# 副库表 review(standard_no, display_name) — 程序发现的新标准，需人工审核后移入主库

import sqlite3

_standard_dict_cache = None  # {标准号: 完整显示名称}
_DB_PATH = None


def program_dir() -> str:
    """程序所在目录（只读资源目录，与 web.server._root_dir() 保持一致）。

    打包后（PyInstaller）__file__ 指向 _MEIPASS，若按 __file__ 推导路径，
    读的是 `_internal/...`，因此这里显式区分打包与源码两种模式。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 用户数据目录（可写）──────────────────────────────────
# 安装目录可能位于 %ProgramFiles%：普通用户没有写权限，Windows 还会把写入
# 重定向到 VirtualStore，表现就是"设置改完重启又变回去、词典录进去查不到"。
# 所以用户数据统一放 %LOCALAPPDATA%\标准处理系统；旧版本写在程序目录里的数据
# 首次运行会**复制**一份过来（不删原文件），保证老用户数据不丢。
DATA_DIR_ENV = "CSF_DATA_DIR"     # 便于测试 / 绿色部署覆盖
DATA_FILE_NAMES = (
    ".app_settings.json",
    "标准名称字典.db", "标准名称字典.db-wal", "标准名称字典.db-shm",
    "标准名称字典.txt", "标准名称字典_待审核.txt", "标准名称字典.txt.sha256",
    ".tutorial_state.json", ".pending_import.json",
    "firewall_managed.json", ".classified_pending.json",
)
# 说明：builtin_prefixes.json **不属于用户数据** —— 它是随程序分发的默认值。
# 用户在前缀开关上的选择存在 .app_settings.json 的 builtinPrefixes 里，
# 运行时按「默认值 + 用户覆盖」合并（见 web/server.py 的 _effective_prefix_groups）。
# 若把它当用户数据搬走，升级后新分组（TB/T、ISO）就永远不生效。
DATA_DIR_NAMES = (".webdata",)     # 需要整体搬过去的目录（WebEngine 缓存/存储）

_data_dir_cache = None
_migrate_done = False


def user_data_dir(create: bool = True) -> str:
    """用户数据目录（可写）：%LOCALAPPDATA%\\标准处理系统。

    优先使用环境变量 CSF_DATA_DIR；不可写时退回程序目录（保持旧行为，绝不因
    权限问题让程序起不来）。首次调用会触发一次旧数据迁移。
    """
    global _data_dir_cache
    if _data_dir_cache:
        return _data_dir_cache

    override = (os.environ.get(DATA_DIR_ENV) or "").strip()
    candidate = override or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "标准处理系统")

    writable = False
    if create:
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write_probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            writable = True
        except Exception:
            writable = False
    else:
        writable = os.path.isdir(candidate)
    if not writable:
        candidate = program_dir()
    _data_dir_cache = candidate

    if writable:
        try:
            migrate_legacy_data(candidate)
        except Exception:
            pass
    return _data_dir_cache


def migrate_legacy_data(data_dir: str = None) -> list:
    """把旧版本写在程序目录里的用户数据复制到数据目录（只复制、不删除，只做一次）。

    返回实际搬过来的文件/目录名列表。数据目录里已有的文件不会被覆盖，
    因此用户在新版本里改过的内容不会被旧数据顶掉。
    """
    global _migrate_done
    if _migrate_done:
        return []
    _migrate_done = True

    import shutil

    src = program_dir()
    dst = data_dir or user_data_dir(create=False)
    if os.path.abspath(src) == os.path.abspath(dst) or not os.path.isdir(dst):
        return []

    marker = os.path.join(dst, ".migrated_from_program_dir")
    moved = []
    for name in DATA_FILE_NAMES:
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isfile(s) and not os.path.exists(d):
            try:
                shutil.copy2(s, d)
                moved.append(name)
            except Exception:
                pass
    for name in DATA_DIR_NAMES:
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s) and not os.path.isdir(d):
            try:
                shutil.copytree(s, d)
                moved.append(name)
            except Exception:
                pass
    try:
        if moved and not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8") as f:
                f.write(src)
    except Exception:
        pass
    return moved


def _get_db_path() -> str:
    """获取 SQLite 数据库路径（与写入路径必须是同一个文件）"""
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = os.path.join(user_data_dir(), "标准名称字典.db")
    return _DB_PATH


def _get_connection() -> sqlite3.Connection:
    """获取数据库连接（自动创建表）"""
    db = _get_db_path()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("""CREATE TABLE IF NOT EXISTS main (
        standard_no TEXT PRIMARY KEY,
        display_name TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS review (
        standard_no TEXT PRIMARY KEY,
        display_name TEXT NOT NULL
    )""")
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_from_txt():
    """将旧版 txt 文件中的数据迁移到 SQLite（自动跳过已存在的）"""
    base = user_data_dir()   # 与数据库同目录（可写数据目录）
    txt_path = os.path.join(base, "标准名称字典.txt")
    review_path = os.path.join(base, "标准名称字典_待审核.txt")
    if not os.path.exists(txt_path) and not os.path.exists(review_path):
        return
    conn = _get_connection()
    try:
        # 迁移主库
        if os.path.exists(txt_path):
            with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = re.match(r'^(.+?)《([^》]+)》$', line)
                    if m:
                        std_no = m.group(1).strip()
                        conn.execute("INSERT OR IGNORE INTO main (standard_no, display_name) VALUES (?, ?)",
                                     (std_no, line))
        # 迁移待审核
        if os.path.exists(review_path):
            with open(review_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or "《" not in line:
                        continue
                    std_no = line.split("《")[0].strip()
                    conn.execute("INSERT OR IGNORE INTO review (standard_no, display_name) VALUES (?, ?)",
                                 (std_no, line))
        conn.commit()
    finally:
        conn.close()


# 用可重入锁：规范化索引构建时会嵌套调用 _load_standard_dict
_standard_dict_lock = threading.RLock()

def _load_standard_dict() -> dict:
    """从 SQLite 加载标准名称字典，返回 {标准号: 完整显示名}"""
    global _standard_dict_cache
    if _standard_dict_cache is not None:
        return _standard_dict_cache
    with _standard_dict_lock:
        # 双重检查
        if _standard_dict_cache is not None:
            return _standard_dict_cache
        loaded = {}
        try:
            conn = _get_connection()
            try:
                cursor = conn.execute("SELECT standard_no, display_name FROM main")
                for row in cursor:
                    loaded[row["standard_no"]] = row["display_name"]
            finally:
                conn.close()
        except Exception as e:
            # 读失败（数据库被占用/权限不足等）时**不要**把空结果缓存下来，
            # 否则本次运行内所有标准名都会消失且无法自愈，必须重启才行。
            print(f"[WARN] 标准名称字典读取失败，本次不缓存以便重试: {e}")
            return loaded
        _standard_dict_cache = loaded
        return _standard_dict_cache


_standard_dict_norm_cache = None  # {规范化标准号: 完整显示名}


def _load_standard_dict_norm() -> dict:
    """基于主库字典构建"规范化标准号 -> 显示名"索引，用于容忍空白/全角等写法差异。"""
    global _standard_dict_norm_cache
    if _standard_dict_norm_cache is not None:
        return _standard_dict_norm_cache
    with _standard_dict_lock:
        if _standard_dict_norm_cache is not None:
            return _standard_dict_norm_cache
        index = {}
        for std_no, display in _load_standard_dict().items():
            key = normalize_std_no(std_no)
            if key:
                index.setdefault(key, display)
        _standard_dict_norm_cache = index
        return _standard_dict_norm_cache


def lookup_standard_dict(std_no: str):
    """按标准号查主库字典，命中返回显示名，未命中返回 None。

    兼容写法差异：PDF 折行（TB/T\\n46-2020）、多余空格、全角冒号等
    都能与库中 "TB/T 46-2020" 正确匹配。
    """
    if not std_no:
        return None
    data = _load_standard_dict()
    if not data:
        return None
    key = str(std_no).strip()
    if key in data:
        return data[key]
    return _load_standard_dict_norm().get(normalize_std_no(key))


def invalidate_standard_dict_cache():
    """主库/待审核库被外部改动后调用，清空字典缓存以便下次重新加载。"""
    global _standard_dict_cache, _standard_dict_norm_cache
    with _standard_dict_lock:
        _standard_dict_cache = None
        _standard_dict_norm_cache = None


def add_standard_to_review(display_name: str):
    """将新发现的标准写入待审核表（不修改主库）"""
    if not display_name or "《" not in display_name:
        return
    m = re.match(r'^(.+?)《', display_name)
    if not m:
        return
    std_num = m.group(1).strip()
    # 检查是否已在主库
    existing = _load_standard_dict()
    if std_num in existing:
        return
    try:
        conn = _get_connection()
        conn.execute("INSERT OR IGNORE INTO review (standard_no, display_name) VALUES (?, ?)",
                     (std_num, display_name))
        conn.commit()
        conn.close()
    except Exception:
        pass


def add_standard_to_main(display_name: str):
    """将确认的标准直接写入主库，同时从待审核中移除"""
    if not display_name or "《" not in display_name:
        return False
    m = re.match(r'^(.+?)《', display_name)
    if not m:
        return False
    std_num = m.group(1).strip()
    existing = _load_standard_dict()
    if std_num in existing:
        return False
    try:
        conn = _get_connection()
        conn.execute("INSERT OR IGNORE INTO main (standard_no, display_name) VALUES (?, ?)",
                     (std_num, display_name))
        conn.execute("DELETE FROM review WHERE standard_no = ?", (std_num,))
        conn.commit()
        conn.close()
        invalidate_standard_dict_cache()  # 清除缓存
        return True
    except Exception:
        return False


def _remove_from_review(display_name: str):
    """从待审核表中移除指定的标准"""
    m = re.match(r'^(.+?)《', display_name)
    if not m:
        return
    std_num = m.group(1).strip()
    try:
        conn = _get_connection()
        conn.execute("DELETE FROM review WHERE standard_no = ?", (std_num,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _migrate_cleanup_txt():
    """迁移完成后重命名旧 txt 文件（标记为已迁移）"""
    base = user_data_dir()   # txt 与数据库同目录（可写数据目录）
    for name in ("标准名称字典.txt", "标准名称字典_待审核.txt", "标准名称字典.txt.sha256"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            try:
                os.rename(path, path + ".migrated")
            except Exception:
                pass
        pass


def resolve_standard_display(text: str, name: str) -> str:
    """从文档文本中提取标准编号 name 后面的全名，返回格式化字符串。
    优先查本地字典 → 再从文本提取 → 新名称写入待审核。
    共 4 种文本搜索策略：
      1. 紧跟编号后面（跨行合并，最多往前看 300 字符）
      2. 包含编号的整句中搜索（按句号/换行分割）
      3. 编号前有《》（形如《XX标准》(GB/T ...)）
      4. 直接返回编号
    自动将新识别的名称存入待审核文件。
    """
    if not text or not name:
        return name

    # ===== 第0步：先查本地字典（最高优先级） =====
    dict_data = _load_standard_dict()
    if name in dict_data:
        return dict_data[name]

    # ===== 文本提取（字典未命中时） =====
    idx = text.find(name)

    # 策略1：编号后面提取
    if idx >= 0:
        result = _extract_after(text, idx, name)
        if result:
            add_standard_to_review(result)
            return result

    # 策略2：包含编号的句子中搜索
    if idx >= 0:
        result = _extract_from_sentence(text, name)
        if result:
            add_standard_to_review(result)
            return result

    # 策略3：编号前有《》（编号在括号中）
    if idx >= 0:
        result = _extract_before(text, idx, name)
        if result:
            add_standard_to_review(result)
            return result

    # ===== 全都没命中，返回原始编号 =====
    return name


def _try_make_display(name: str, title: str) -> str | None:
    """给定标准编号和标题文本，校验长度并返回《》格式"""
    title = re.sub(r'[《》<>（）()\[\]【】""\' \t\r\n\f\v]', '', title).strip()
    if not title or len(title) > 60:
        return None
    return f"{name}《{title}》"


def _extract_after(text: str, idx: int, name: str) -> str | None:
    """策略1：编号后面提取，跨行合并，最多看 300 字符"""
    end = min(idx + len(name) + 300, len(text))
    after = text[idx + len(name):end]
    # 跨行合并（去掉换行，保留空格）
    after = after.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ').strip()
    if not after:
        return None

    # 1a. 书名号：《XX标准》
    m = re.match(r'\s*[《（<]["\']?([^》）>"\']+)["\']?[》）>]', after)
    if m:
        return _try_make_display(name, m.group(1)) or None

    # 1b. 引号："XX标准"、'XX标准'
    m = re.match(r'\s*["\'"]([^"\']+)["\'"]', after)
    if m:
        return _try_make_display(name, m.group(1)) or None

    # 1c. 破折号或空格后的中文标题：GB/T 12345-2020 — XX标准
    #     先跳过开头的空格/破折号/连字符/冒号
    after_clean = re.sub(r'^[\s—–\-：:、，,]+', '', after)
    if after_clean != after:
        # 有前缀分隔符，直接取中文
        m = re.match(r'([\u4e00-\u9fff][\u4e00-\u9fff\w\s\-—]*)', after_clean)
        if m:
            return _try_make_display(name, m.group(1)) or None
        # 也可能分隔符后直接是书名号
        m = re.match(r'\s*[《（<]["\']?([^》）>"\']+)["\']?[》）>]', after_clean)
        if m:
            return _try_make_display(name, m.group(1)) or None

    # 1d. 通用提取：到下一个句号/分号/换行或另一个标准号为止
    #     注意不能遇到数字就停，因为标准号本身包含数字
    #     改用：遇到另一个标准号模式（大写字母+空格+数字）才停
    title_end = re.search(r'[，。；、！？\n\r,.;:!?]|(?=\b[A-Z]{2,}\s*\d)', after)
    title = after[:title_end.start()].strip() if title_end else after.strip()
    # 只保留中文和连接符
    title = re.sub(r'[^《》\u4e00-\u9fff\w\s\-—]', '', title).strip()
    if title and len(title) <= 60:
        # 标题至少要有2个汉字
        if re.search(r'[\u4e00-\u9fff]{2,}', title):
            return _try_make_display(name, title)

    return None


def _extract_from_sentence(text: str, name: str) -> str | None:
    """策略2：在包含标准编号的句子中提取标题"""
    # 按句号、分号、换行分割为句子
    sentences = re.split(r'(?<=[。；！？\n\r])\s*', text)
    for sent in sentences:
        if name not in sent:
            continue
        nidx = sent.find(name)
        after = sent[nidx + len(name):].strip()
        if not after:
            continue
        # 2a. 书名号
        m = re.match(r'[《（<]["\']?([^》）>"\']+)["\']?[》）>]', after)
        if m:
            return _try_make_display(name, m.group(1))
        # 2b. 引号
        m = re.match(r'["\'"]([^"\']+)["\'"]', after)
        if m:
            return _try_make_display(name, m.group(1))
        # 2c. 破折号/空格后的标题
        after_clean = re.sub(r'^[\s—–\-：:、，,]+', '', after)
        if after_clean != after:
            m = re.match(r'([\u4e00-\u9fff][\u4e00-\u9fff\w\s\-—]*)', after_clean)
            if m:
                return _try_make_display(name, m.group(1))
        # 2d. 句子里取到句尾
        m = re.search(r'[，。；、！？,.;:!?]', after)
        title = after[:m.start()].strip() if m else after.strip()
        title = re.sub(r'[^《》\u4e00-\u9fff\w\s\-]', '', title).strip()
        if re.search(r'[\u4e00-\u9fff]{2,}', title):
            result = _try_make_display(name, title)
            if result:
                return result
    return None


def _extract_before(text: str, idx: int, name: str) -> str | None:
    """策略3：检查编号前是否有《》（编号在括号中，如《XX标准》(GB/T ...)）"""
    before = text[max(0, idx - 200):idx].rstrip()
    # 3a. 《书名》 (GB/T ...)  或  《书名》(GB/T ...)
    m = re.search(r'[《（<]["\']?([^》）>"\']+)["\']?[》）>]\s*[（(]\s*$', before)
    if m:
        return _try_make_display(name, m.group(1))
    # 3b. 书名 (GB/T ...)  不带书名号
    m = re.search(r'([\u4e00-\u9fff]{2,})\s*[（(]\s*$', before)
    if m:
        title = m.group(1).strip()
        return _try_make_display(name, title)
    # 3c. 行末的中文（同一行，编号在行尾标题在行首）
    #     扫描编号所在行的前面部分
    line_start = text.rfind('\n', 0, idx) + 1 if '\n' in text[:idx] else 0
    line_before = text[line_start:idx].strip()
    if line_before:
        # 匹配行内中文内容（可能包含数字/字母但不是标准号格式）
        m = re.search(r'([\u4e00-\u9fff]{2,}[\u4e00-\u9fff\w\s\-—]*)', line_before)
        if m:
            title = m.group(1).strip()
            # 确保不是标准号本身
            if not re.match(r'^(GB|ISO|IEC|EN|ASTM|DIN|JIS|IEEE|UL|MIL)\b', title, re.IGNORECASE):
                return _try_make_display(name, title)
    return None


def normalize_table_row(cells: List[Any]) -> str:
    return " | ".join(str(c) if c is not None else "" for c in cells)


def normalize_std_no(std_no: str) -> str:
    """规范化标准号，仅用于去重比较（不是显示用）。

    不同提取路径会得到写法不同但实为同一条的标准号，例如：
      TB/T 46-2020 / TB/T46-2020 / 折行 TB/T\\n46-2020 / GB/T 1.1：2020
    统一处理为：全角转半角 → 去全部空白 → 分隔符/冒号统一为 '-' → 大写。
    """
    if not std_no:
        return ""
    s = str(std_no)
    # 只取标准号部分，去掉《标准名称》
    if "《" in s:
        s = s.split("《", 1)[0]
    # 全角字符（如 ： － ／）转半角
    s = unicodedata.normalize("NFKC", s)
    # 去掉所有空白（含折行产生的换行符）
    s = re.sub(r"\s+", "", s)
    # 各种连字符/破折号/冒号统一为 '-'
    s = re.sub(r"[\u2010-\u2015\u2212\uFF0D:：]", "-", s)
    # 去掉首尾多余分隔符（如 "GB/T 1.1-"）
    return s.strip("-").upper()


def standard_no_of(name: str) -> str:
    """从"编号《名称》"里取出标准编号部分（保持原始写法，不改写）。

    结果行的 name 有两种形态：纯编号 "GB/T 1234-2020" 或带名称
    "GB/T 1234-2020《某某规范》"。凡是需要按编号匹配的地方（联网补名、
    统计键重命名等）统一走这里，避免各处手写 split("《") 造成口径漂移。
    与 normalize_std_no 的区别：这里只截取、不做去重归一化。
    """
    s = str(name or "")
    if "《" in s:
        return s.split("《", 1)[0].strip()
    return s.strip()


def find_context(text: str, keyword: str, window: int = 80) -> str:
    """截取关键词前后 window 字符的上下文（结果"证据句"的基础）。

    PDF / DOCX / OCR 三条提取路径共用同一实现：
      - 原先 pdf_handler 与 docx_handler 各有一份逐字相同的拷贝；
      - OCR 路径用的是"整页开头 200 字符"，与标准号所在位置无关，
        导致结果页"内容"列和 AI 增强拿到的上下文对不上标准号。
    """
    if not text or not keyword:
        return ""
    idx = text.find(keyword)
    if idx == -1:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(keyword) + window)
    return text[start:end]


# 页数缓存（键含 mtime+size，文件被替换后自动失效）。
# 处理阶段对"页数未知"的文件会回退到 get_document_page_count()，而它必须真的
# 打开并解析文件（PDF/Excel/docx/pptx）——一次几百个文件时，这一步会在开始处理
# 前串行跑完，界面表现就是"点了处理很久没动静"（卡顿专项 L1）。
# 放到 helpers 里而不是 server 里，是为了让**处理链路**也能复用同一份缓存。
_PAGE_COUNT_CACHE = {}
_PAGE_COUNT_CACHE_MAX = 20000


def get_document_page_count_cached(path: str) -> int:
    """带缓存的页数统计。

    返回值语义（**据实说明**）：
      · 0 = 文件不可访问（连 os.stat 都失败），确定为"未知"；
      · 对**存在但解析失败**的文件（加密/损坏），底层
        `get_document_page_count()` 目前统一返回 1 —— 与"真的只有 1 页"
        无法区分（复审批 3-1）。这是个已知的契约冲突，等 helpers 内部改成
        失败返回 0 之后，这里会一并变成 0，界面也才能显示"无法统计"。
    """
    try:
        st = os.stat(path)
        key = f"{os.path.normcase(os.path.abspath(path))}|{int(st.st_mtime)}|{st.st_size}"
    except Exception:
        key = os.path.normcase(os.path.abspath(str(path)))
    hit = _PAGE_COUNT_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        pages = int(get_document_page_count(path))
    except Exception:
        pages = 0
    if len(_PAGE_COUNT_CACHE) >= _PAGE_COUNT_CACHE_MAX:
        _PAGE_COUNT_CACHE.clear()
    _PAGE_COUNT_CACHE[key] = pages
    return pages


def get_document_page_count(file_path: str) -> int:
    """获取文档的总页数/段数/行数。失败时返回 1。"""
    if not file_path or not os.path.exists(file_path):
        return 1
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == ".pdf":
            import fitz
            with fitz.open(file_path) as doc:
                return max(1, doc.page_count)
        elif ext in (".docx", ".doc"):
            try:
                from docx import Document
                doc = Document(file_path)
                return max(1, len(doc.paragraphs) // 40 + (1 if len(doc.paragraphs) % 40 else 0))
            except Exception:
                return 1
        elif ext in (".xlsx", ".xlsm"):
            wb = None
            try:
                import openpyxl
                wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
                ws = wb.active
                return max(1, ws.max_row)
            except Exception:
                return 1
            finally:
                # 不关闭会在 Windows 上一直占住该文件（用户无法重命名/删除/覆盖）
                if wb is not None:
                    try:
                        wb.close()
                    except Exception:
                        pass
        elif ext == ".xls":
            # 旧版 .xls 用 openpyxl 读不了（会抛异常），这里不去碰它，
            # 交给 ExcelHandler 走 Excel COM 转换后再统计
            return 1
        elif ext in (".pptx", ".pptm"):
            try:
                from core.pptx_handler import PptxHandler
                return PptxHandler.slide_count(file_path)
            except Exception:
                return 1
        elif ext in (".txt", ".text", ".csv", ".tsv", ".md", ".markdown",
                     ".log", ".rtf"):
            try:
                from core.text_handler import TextHandler
                return TextHandler.line_count(file_path)
            except Exception:
                return 1
    except Exception:
        pass
    return 1


def fill_merged_cells_down(rows: List[List[Any]]) -> List[List[Any]]:
    """将表格中同一列向下的空值用上一个非空值填充（模拟合并单元格向下填充）。"""
    if not rows:
        return rows
    filled = [list(row) for row in rows]
    col_count = max(len(r) for r in filled)
    for row in filled:
        while len(row) < col_count:
            row.append("")
    for col in range(col_count):
        last = ""
        for row in filled:
            val = str(row[col]) if row[col] is not None else ""
            if val.strip():
                last = val
            else:
                row[col] = last
    return filled


def build_toc_from_docx(doc) -> List[Dict[str, Any]]:
    """根据 DOCX 段落样式提取目录结构。"""
    toc = []
    for para in doc.paragraphs:
        style = para.style.name if para.style else ""
        text = para.text.strip()
        if not text:
            continue
        level = 0
        if "Heading" in style:
            try:
                level = int(style.replace("Heading", "").strip())
            except Exception:
                level = 1
        if level > 0:
            toc.append({"level": level, "title": text})
    return toc


def safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


# ---- 联网查询标准名称（全国标准信息公共服务平台） ----
import urllib.parse
import requests
from bs4 import BeautifulSoup

# 联网查询超时（秒）
_ONLINE_LOOKUP_TIMEOUT = 15

# ── 对外请求标识：如实标识，不伪装浏览器 ──────────────────────
# 法务意见 P0-5 建议 3：原先伪造 Chrome 125 的 User-Agent，并把 Referer 伪造成
# openstd / ANSI / ISO / std.samr —— 既属"伪造身份"，也可能招致目标站点反制
# （封禁 UA、验证码、限流），而且它是文档里一条无法自证的承诺。
# 现在：UA 如实标识程序与版本，**不发送 Referer**。
#
# 联系邮箱与源码地址。最终 UA 形如：
#   StandardProcessingSystem/<版本号> (+<源码地址>; contact: <邮箱>)
# 两者同时载于《许可、隐私与免责说明》第三条与第三十九条。
# 说明：部分站点（如维基百科）明确要求 UA 中给出可联系的网址，
#       仅写程序名可能被拒绝或限流。
_UA_CONTACT = "the_forever_csf@126.com"
_UA_REPO_URL = "https://github.com/TheForeverCSF/Standard-Processing-System"

_APP_UA_CACHE = None


def _read_app_version() -> str:
    """读取随包 version.txt 的版本号（读不到返回空串）。

    必须**多路径尝试**：`program_dir()` 在 PyInstaller 打包后返回的是 **exe 目录**，
    而 version.txt 被 spec 打进 **_internal/**（datas 目标 '.'）。只试单一路径会
    读不到，UA 就退化成 `StandardProcessingSystem/0` —— 看似"如实"，其实对外报了
    一个错误的版本号，比不报更糟。
    """
    cands = []
    try:
        cands.append(os.path.join(program_dir(), "version.txt"))
    except Exception:
        pass
    try:
        base = getattr(sys, "_MEIPASS", "") or ""
        if base:
            cands.append(os.path.join(base, "version.txt"))     # 打包后的资源目录
    except Exception:
        pass
    try:
        cands.append(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "version.txt"))
    except Exception:
        pass
    for p in cands:
        try:
            with open(p, "r", encoding="utf-8-sig") as f:       # utf-8-sig 容忍 BOM
                v = (f.read() or "").strip()
            if v:
                return v
        except Exception:
            continue
    return ""


def app_user_agent() -> str:
    """返回对外请求使用的 User-Agent：StandardProcessingSystem/<版本> (+<源码地址>; contact: <邮箱>)。

    版本号取自随包的 version.txt（**单一来源**），避免 UA 里的版本与程序版本漂移。
    读不到版本时**省略版本段**（而不是写 `/0`），并打一条可见告警 —— 不做静默降级。
    """
    global _APP_UA_CACHE
    if _APP_UA_CACHE is None:
        ver = _read_app_version()
        if ver:
            ua = f"StandardProcessingSystem/{ver}"
        else:
            ua = "StandardProcessingSystem"
            try:
                log_print("[联网] 未能读取 version.txt，UA 将不带版本号"
                          "（不写死一个可能错误的版本）", "WARN")
            except Exception:
                try:
                    print("[联网] 未能读取 version.txt，UA 将不带版本号")
                except Exception:
                    pass
        if _UA_REPO_URL:
            ua += f" (+{_UA_REPO_URL}"
            if _UA_CONTACT:
                ua += f"; contact: {_UA_CONTACT}"
            ua += ")"
        elif _UA_CONTACT:
            ua += f" (contact: {_UA_CONTACT})"
        _APP_UA_CACHE = ua
    return _APP_UA_CACHE


_ONLINE_HEADERS = {
    "User-Agent": app_user_agent(),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _strip_year_suffix(standard_no: str) -> str:
    """去掉标准号末尾的年代号：GB/T 1234-2020 → GB/T 1234。

    同时兼容中文破折号与冒号年份（GB/T 1234—2020 / GB/T 1234:2020）。
    """
    return re.sub(r"[-—–－:：]\s*(?:19|20)\d{2}\s*$", "", standard_no or "").strip()


def online_lookup_standard(standard_no: str, strip_year: bool = False) -> str | None:
    """通过全国标准信息公共服务平台联网查询标准名称。

    先查 openstd.samr.gov.cn（国家标准全文公开系统），
    如果没查到再查 std.samr.gov.cn（全国标准信息公共服务平台）。

    Args:
        standard_no: 标准编号，如 "GB/T 19001-2016"、"GB 18030-2022"
        strip_year: 是否"忽略年代号" —— 带年代号查不到时，用去掉年代号的
            编号再查一轮（GB/T 1234-2020 → GB/T 1234）。查到的名称以**实际
            命中的编号**返回，便于用户判断匹配到的是哪个版本。

    Returns:
        格式化字符串，如 "GB/T 19001-2016《质量管理体系 要求》"
        未查到返回 None
    """
    if not standard_no or not standard_no.strip():
        return None
    std = standard_no.strip()

    # 待查询的编号序列：先按原样，勾选"忽略年代号"时再补一轮去掉年代号的
    candidates = [std]
    if strip_year:
        _bare = _strip_year_suffix(std)
        if _bare and _bare != std:
            candidates.append(_bare)

    for cand in candidates:
        # 1) 查 openstd.samr.gov.cn
        result = _lookup_openstd(cand)
        if result:
            return result

        # 2) 回退查 std.samr.gov.cn
        result = _lookup_stdportal(cand)
        if result:
            return result

        # 3) 国际标准（ISO / IEC / ASTM 等）
        result = _lookup_international(cand)
        if result:
            return result

    return None


def _lookup_openstd(standard_no: str) -> str | None:
    """查国家标准全文公开系统 openstd.samr.gov.cn"""
    try:
        # 第一步：搜索列表
        search_url = "https://openstd.samr.gov.cn/bzgk/std/std_list"
        params = {"p.p2": standard_no}
        resp = requests.get(
            search_url,
            params=params,
            headers=_ONLINE_HEADERS,
            timeout=_ONLINE_LOOKUP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # 页面结构：每个搜索结果是连续的 <td> 元素
        # 标准号在 TD 中，标准名称在后面的 TD 中（间隔空的 TD 和标签 TD）
        # 实际是通过匹配 showInfo onclick 来关联标准号和名称

        # 方法1：找到所有包含 showInfo 的 <a> 标签
        all_links = soup.find_all("a", onclick=lambda v: v and "showInfo" in v)
        std_links = []
        for link in all_links:
            text = link.get_text(strip=True)
            if text and _is_std_number(text):
                std_links.append(text)
            elif text and not std_links:
                continue  # 还没遇到标准号，跳过
            else:
                # 这是标准名称
                if std_links:
                    last_std = std_links[-1]
                    # 检查是否匹配
                    if standard_no.upper() in last_std.upper() or last_std.upper() in standard_no.upper():
                        return f"{last_std}《{text}》"

        # 方法2：如果在结果中没有找到，尝试直接找标准号和名称在同一 <td> 中相邻的情况
        # 把所有 TD 中的文本提取出来，按顺序查找
        all_tds = soup.find_all("td")
        td_texts = [td.get_text(strip=True) for td in all_tds]

        for i, txt in enumerate(td_texts):
            # 检查当前 TD 是否包含标准号
            if _is_std_number(txt) and standard_no.upper() in txt.upper():
                # 找接下来的标准名称（跳过为空或太短的 TD）
                for j in range(i + 1, min(i + 8, len(td_texts))):
                    candidate = td_texts[j]
                    if candidate and len(candidate) > 1 and _is_std_name(candidate):
                        return f"{txt}《{candidate}》"
                break

        return None
    except requests.RequestException:
        return None
    except Exception:
        return None


def _is_std_number(text: str) -> bool:
    """判断文本是否看起来像标准编号。

    直接复用上面的统一模式表：原先这里写死了 GB|ISO|IEC|EN|… 一小串前缀，
    导致联网查询页面里 TB/T、DB、Q/CR 这类链接文本一律被判为"不是标准号"
    而被丢弃 —— 行业标准永远查不到名称。
    """
    if not text:
        return False
    t = text.strip()
    if len(t) > 40:
        return False
    return any(re.match(pat, t, re.IGNORECASE) for pat in _STD_PATTERNS)


def _is_std_name(text: str) -> bool:
    """判断文本是否看起来像标准名称（中文名或英文名都接受）。

    原实现只认"汉字 ≥2 且占比 >30%"，于是 ISO/ASTM/EN 的英文名称
    全被拒绝，国际标准查回来也显示不出名称。
    """
    t = (text or "").strip()
    if len(t) < 2:
        return False
    chinese_chars = sum(1 for c in t if '\u4e00' <= c <= '\u9fff')
    if chinese_chars >= 2 and chinese_chars / max(len(t), 1) > 0.3:
        return True
    # 英文名称：至少 2 个单词、字母占比够高，且不是"纯编号"
    if re.search(r'[A-Za-z]{3,}', t) and len(t.split()) >= 2:
        if re.match(r'^[A-Za-z0-9\s\-/.:()]+$', t) and not _is_std_number(t):
            return True
    return False


def _lookup_stdportal(standard_no: str) -> str | None:
    """查全国标准信息公共服务平台 std.samr.gov.cn（备用，使用搜索页面）"""
    try:
        # 使用搜索页面
        search_url = "https://std.samr.gov.cn/gb/gbQuery"
        params = {"p.p2": standard_no}
        resp = requests.get(
            search_url,
            params=params,
            headers=_ONLINE_HEADERS,
            timeout=_ONLINE_LOOKUP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None

        # 同样的 HTML 解析方式
        soup = BeautifulSoup(resp.text, "html.parser")
        all_links = soup.find_all("a", onclick=lambda v: v and "showInfo" in v)
        std_links = []
        for link in all_links:
            text = link.get_text(strip=True)
            if text and _is_std_number(text):
                std_links.append(text)
            elif text and std_links:
                last_std = std_links[-1]
                if standard_no.upper() in last_std.upper() or last_std.upper() in standard_no.upper():
                    return f"{last_std}《{text}》"
        return None
    except requests.RequestException:
        return None
    except Exception:
        return None




# ---- 国际标准联网查询（Wikipedia + ANSI + ISO官网等）----

# Wikipedia REST API 基础 URL
_WIKI_API_BASE = "https://en.wikipedia.org/api/rest_v1/page/summary"
# Wikipedia Action API（搜索用）
_WIKI_ACTION_API = "https://en.wikipedia.org/w/api.php"
# ANSI webstore 搜索基础
_ANSI_SEARCH_BASE = "https://webstore.ansi.org/search"

# 常用国际标准前缀列表
_INTERNATIONAL_PREFIXES = ("ISO", "IEC", "ASTM", "IEEE", "UL", "DIN", "JIS", "MIL", "EN")


def _lookup_international(standard_no: str) -> str | None:
    """查国际标准名称（ISO/IEC/ASTM 等），多源依次尝试。"""
    # 只有国际标准前缀才走此路
    prefix = standard_no.split()[0].upper() if standard_no.split() else ""
    if prefix not in _INTERNATIONAL_PREFIXES and not any(
        standard_no.upper().startswith(p) for p in _INTERNATIONAL_PREFIXES
    ):
        return None

    result = None

    # 来源1：Wikipedia Action API 搜索（最可靠，免费）
    result = _lookup_via_wikipedia(standard_no)
    if result:
        return result

    # 来源2：ANSI webstore 搜索（ISO / IEC / ASTM 都有）
    result = _lookup_via_ansi(standard_no)
    if result:
        return result

    # 来源3：直接尝试 ISO 官网短链接（仅 ISO）
    if prefix == "ISO" or standard_no.upper().startswith("ISO"):
        result = _lookup_iso_direct(standard_no)
        if result:
            return result

    # 来源4：通过全国标准信息公共服务平台的国际标准查询（收录部分采标数据）
    result = _lookup_china_international(standard_no)
    if result:
        return result

    return None


def _clean_std_number(standard_no: str) -> str:
    """将标准号中的冒号替换为短横线，去掉多余空格，便于匹配 URL。"""
    s = standard_no.strip().upper()
    s = re.sub(r"\s*[:-]\s*", "-", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _lookup_via_wikipedia(standard_no: str) -> str | None:
    """通过 Wikipedia Action API 搜索标准名称。"""
    try:
        clean_no = standard_no.strip().upper()
        # 去掉版本后缀（如 ":2015"、"-2015"）提高搜索命中率
        base_no = re.sub(r"[\s:–—-]*\d{4}\s*$", "", clean_no).strip()
        base_no = re.sub(r"\s+", " ", base_no)
        if not base_no:
            base_no = clean_no

        search_params = {
            "action": "query",
            "list": "search",
            "srsearch": f'"{base_no}" standards',
            "format": "json",
            "srlimit": 3,
            "srprop": "snippet",
        }
        resp = requests.get(
            _WIKI_ACTION_API,
            params=search_params,
            headers=_ONLINE_HEADERS,
            timeout=_ONLINE_LOOKUP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        pages = data.get("query", {}).get("search", [])
        if not pages:
            return None

        # 取匹配度最高的结果
        best_page = pages[0]
        page_title = best_page.get("title", "")
        snippet = best_page.get("snippet", "")

        # 确认标题或摘要包含标准号
        page_upper = page_title.upper()
        if base_no.upper() not in page_upper and clean_no not in page_upper:
            # 再放宽一点匹配：纯数字部分匹配就够了
            nums = re.findall(r"\d+", base_no)
            if not any(n in page_upper for n in nums):
                return None

        # 用 REST API 获取详细信息
        rest_url = f"{_WIKI_API_BASE}/{requests.utils.quote(page_title.replace(' ', '_'))}"
        detail_resp = requests.get(
            rest_url,
            headers=_ONLINE_HEADERS,
            timeout=_ONLINE_LOOKUP_TIMEOUT,
        )
        if detail_resp.status_code != 200:
            return None
        detail = detail_resp.json()
        wiki_title = detail.get("title", "") or page_title
        extract = detail.get("extract", "") or snippet

        # 从 extract 中提取标准完整标题（通常是第一句）
        # Wikipedia 第一句格式通常为 "ISO 9001:2015 Quality management systems — Requirements"
        first_sentence = extract.split(".")[0].strip() if extract else ""
        # 检查第一句是否包含标准号
        if clean_no.split("-")[0].split(":")[0] in first_sentence.upper():
            display_title = first_sentence
        else:
            display_title = f"{base_no} {wiki_title}"

        # 去短化：如果名称太长就截取合理长度
        display_title = display_title.strip()
        if len(display_title) > 120:
            display_title = display_title[:117] + "..."

        # 用书名号包装
        if "《" not in display_title:
            # 从标题中提取简明的标准名称（去掉标准号本身）
            title_only = display_title
            for prefix in _INTERNATIONAL_PREFIXES:
                if display_title.upper().startswith(prefix):
                    m = re.match(r'^[A-Za-z0-9\s/:–—\-]+\s+(.+)$', display_title)
                    if m:
                        title_only = m.group(1).strip()
                    break
            # 如果标题太长或非英文，直接返回标准号+原标题
            return f"{clean_no}《{title_only}》"

        return display_title

    except requests.RequestException:
        return None
    except Exception:
        return None


def _lookup_via_ansi(standard_no: str) -> str | None:
    """通过 ANSI webstore 搜索标准名称。"""
    try:
        clean_no = _clean_std_number(standard_no)
        search_url = f"{_ANSI_SEARCH_BASE}?q={requests.utils.quote(clean_no)}"
        resp = requests.get(
            search_url,
            # 不再伪造 Referer（法务意见 P0-5 建议 3）
            headers=_ONLINE_HEADERS,
            timeout=_ONLINE_LOOKUP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # ANSI 搜索结果项通常在带有标准号的标题链接中
        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True)
            if not text:
                continue
            # 检查链接文本是否包含标准号
            nums = re.findall(r"\d+", clean_no)
            if any(n in text for n in nums):
                # 解析标题和名称
                title = text.strip()
                # 去掉 HTML 实体
                title = title.replace("\u2014", "—").replace("\u2013", "–")
                if len(title) > 5 and len(title) < 200:
                    return f"{clean_no}《{title}》"

        return None
    except requests.RequestException:
        return None
    except Exception:
        return None


def _lookup_iso_direct(standard_no: str) -> str | None:
    """直接通过 ISO 官网短链接获取标准名称。"""
    try:
        # 尝试标准 URL 格式：https://www.iso.org/iso-{number}.html
        # 例如 ISO 9001 → https://www.iso.org/iso-9001.html
        parts = standard_no.upper().replace("ISO", "").strip()
        parts = re.sub(r"^[\s/:–—-]+", "", parts)
        # 取主编号（去掉版本号）
        main_no = re.match(r"(\d+(?:\.\d+)*)", parts)
        if not main_no:
            return None
        std_number = main_no.group(1)

        urls_to_try = [
            f"https://www.iso.org/iso-{std_number}.html",
            f"https://www.iso.org/standard/{std_number}.html",
        ]

        for url in urls_to_try:
            resp = requests.get(
                url,
                # 不再伪造 Referer（法务意见 P0-5 建议 3）
                headers=_ONLINE_HEADERS,
                timeout=_ONLINE_LOOKUP_TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                continue

            # 从页面标题中提取标准名称
            soup = BeautifulSoup(resp.text, "html.parser")
            title_tag = soup.find("title")
            if title_tag:
                title_text = title_tag.get_text(strip=True)
                # ISO 页面标题格式: "ISO 9001:2015 - Quality management systems — Requirements"
                if "ISO" in title_text.upper() and std_number in title_text:
                    # 去掉尾部的 " - ISO" 或类似尾缀
                    title_text = re.sub(r"\s*[-–—|]\s*ISO\s*$", "", title_text, flags=re.IGNORECASE)
                    title_text = title_text.strip()
                    title_clean = re.sub(r"[\u200e\u200f]", "", title_text)
                    return f"ISO {std_number}《{title_clean}》"

        return None
    except requests.RequestException:
        return None
    except Exception:
        return None


def _lookup_china_international(standard_no: str) -> str | None:
    """通过全国标准信息公共服务平台的国际标准查询页面搜索。"""
    try:
        prefix = standard_no.split()[0].upper() if standard_no.split() else ""
        if prefix not in ("ISO", "IEC"):
            return None

        # 提取主编号
        nums = re.findall(r"\d+", standard_no)
        if not nums:
            return None
        main_num = nums[0]

        op = prefix  # "ISO" 或 "IEC"
        url = f"https://std.samr.gov.cn/gj/std?op={op}&key={main_num}"
        resp = requests.get(
            url,
            # 不再伪造 Referer（法务意见 P0-5 建议 3）
            headers=_ONLINE_HEADERS,
            timeout=_ONLINE_LOOKUP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # 尝试从页面中找到标准号和名称
        # 常见的结构是表格行
        for td in soup.find_all("td"):
            text = td.get_text(strip=True)
            if standard_no.upper() in text.upper() or main_num in text:
                # 找同行的其他 td 获取名称
                parent = td.parent
                if parent:
                    all_tds = parent.find_all("td")
                    for sibling in all_tds:
                        sib_text = sibling.get_text(strip=True)
                        if sib_text and sib_text != text and len(sib_text) > 5:
                            return f"{standard_no}《{sib_text}》"

        return None
    except requests.RequestException:
        return None
    except Exception:
        return None


def batch_online_lookup(standard_nos: list, max_workers: int = 3,
                        strip_year: bool = False) -> dict:
    """批量并发联网查询标准名称。

    Args:
        standard_nos: 标准编号列表
        max_workers: 并发线程数，默认 3（降低并发避免 UI 卡顿）
        strip_year: 是否"忽略年代号"，透传给 online_lookup_standard

    Returns:
        {标准号: 格式化名称或None}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(online_lookup_standard, std, strip_year=strip_year): std
            for std in standard_nos
        }
        for future in as_completed(futures):
            std = futures[future]
            try:
                results[std] = future.result()
            except Exception:
                results[std] = None
    return results


# ---- 模块初始化：创建/迁移 SQLite 数据库 ----
_db_path = _get_db_path()
if not os.path.exists(_db_path):
    # 首次运行：从旧 txt 迁移到 SQLite
    _migrate_from_txt()
    if os.path.exists(_db_path):
        # 迁移成功后清理旧文件
        _migrate_cleanup_txt()
