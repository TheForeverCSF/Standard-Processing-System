# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import os
import sys
import json
import time
from typing import Dict, Any, List, Callable, Optional

# 设置页写入的配置文件（与 web/server.py 用的是同一个 .app_settings.json）
_SETTINGS_NAME = ".app_settings.json"
DEFAULT_URL = "http://localhost:8000/v1/chat/completions"
DEFAULT_MODEL = "default"


def _exe_dir() -> str:
    """程序所在目录（打包后为 exe 目录，源码运行时为项目根目录）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _settings_dir() -> str:
    """设置文件所在目录：可写数据目录优先（与 web/server.py 写入位置一致）。

    旧版本写在程序目录；这里两者都看，数据目录优先，避免 %ProgramFiles%
    下"设置页保存了、程序读不到"。
    """
    try:
        from utils.helpers import user_data_dir
        d = user_data_dir()
        if os.path.exists(os.path.join(d, _SETTINGS_NAME)):
            return d
    except Exception:
        pass
    return _app_dir()


def load_ai_config() -> Dict[str, Any]:
    """解析本地 AI 配置。优先级：**设置页配置 > 环境变量 > 默认值**。

    历史上只能通过环境变量 LOCAL_AI_URL / LOCAL_AI_KEY / LOCAL_AI_MODEL 配置，
    用户得手工设置系统环境变量（打包成 exe 后还要重启资源管理器才会继承），
    设置页里完全看不到这一项。现在设置页可直接配置，环境变量仅作兼容回退。
    """
    cfg = {"enabled": False, "url": "", "model": DEFAULT_MODEL,
           "apiKey": "", "source": "none", "settings_present": False}

    # 1) 设置页保存的配置。只要配置文件里出现过 localAI 字段，就以它为准
    #    （哪怕用户是显式"关闭"，也不再回退环境变量，否则关不掉）
    try:
        path = os.path.join(_settings_dir(), _SETTINGS_NAME)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            ai = data.get("localAI")
            if isinstance(ai, dict):
                url = str(ai.get("url") or "").strip()
                cfg.update({
                    "enabled": bool(ai.get("enabled", False)),
                    "url": url,
                    "model": (str(ai.get("model") or "").strip() or DEFAULT_MODEL),
                    "apiKey": str(ai.get("apiKey") or ""),
                    "source": "settings",
                    "settings_present": True,
                    "allowRemote": bool(ai.get("allowRemote", False)),
                })
                return cfg
    except Exception:
        pass

    # 2) 环境变量（旧用法，保持向后兼容）
    env_url = (os.environ.get("LOCAL_AI_URL") or "").strip()
    if env_url:
        cfg.update({
            "enabled": True,
            "url": env_url,
            "model": (os.environ.get("LOCAL_AI_MODEL") or "").strip() or DEFAULT_MODEL,
            "apiKey": os.environ.get("LOCAL_AI_KEY", ""),
            "source": "env",
        })
    return cfg


class LocalAIHandler:
    """本地 AI 表格解析接口。

    配置来源（优先级从高到低）：
      1. 设置 → 安全 → 本地 AI（写入 .app_settings.json 的 localAI 字段）
      2. 环境变量 LOCAL_AI_URL / LOCAL_AI_MODEL / LOCAL_AI_KEY
      3. 默认 http://localhost:8000/v1/chat/completions

    仅当"已启用且填了地址"才真正调用，避免默认地址被误当成已配置。
    """

    def __init__(self, on_log: Callable[[str], None] = None):
        self.on_log = on_log or (lambda x: None)
        self.reload()

    def reload(self) -> Dict[str, Any]:
        """重新读取配置（设置保存后无需重启即可生效）"""
        cfg = load_ai_config()
        self.enabled = cfg["enabled"]
        self.url = cfg["url"] or DEFAULT_URL
        self.model = cfg["model"]
        self.api_key = cfg["apiKey"]
        self.source = cfg["source"]
        return cfg

    def complete_table(self, raw_text: str, context: str = "") -> Dict[str, Any]:
        """根据原始文本补全表格：识别标准名称、列含义、行关系。"""
        if not self._available():
            self.on_log("  本地 AI 未配置，跳过智能解析")
            return self._empty_result()

        prompt = self._build_prompt(raw_text, context)
        try:
            return self._call(prompt)
        except Exception as e:
            self.on_log(f"  本地 AI 调用失败: {e}")
            return self._empty_result()

    def _available(self) -> bool:
        """是否可以对本地 AI 发起调用。

        三个条件：已启用、填了地址、且不处于涉密模式。
        涉密模式会强制禁用所有联网/AI 功能（core/security_config.allow_ai_enhance），
        以前这里没检查，被防火墙拦下来只会得到一句含糊的"调用失败"。
        """
        if not self.enabled or not self.url:
            return False
        try:
            from core.security_config import allow_ai_enhance
            if not allow_ai_enhance():
                self.on_log("  涉密模式已启用，已按策略跳过本地 AI 调用")
                return False
        except Exception:
            pass
        return True

    def test_connection(self, timeout: float = 8.0,
                        overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """设置页「测试连接」用：返回可读结果，不抛异常。

        overrides: 用界面上**当前填写但尚未保存**的值来测试（url/model/apiKey/enabled）。
        带上非空 url 时即视为"用户就是要测这个地址"，忽略启用勾选状态。
        """
        self.reload()
        if isinstance(overrides, dict):
            url = str(overrides.get("url") or "").strip()
            if url:
                self.url = url
                self.enabled = True      # 明确要测这个地址，就不再纠结勾选状态
            if overrides.get("model"):
                self.model = str(overrides["model"]).strip()
            k = overrides.get("apiKey")
            if k:
                self.api_key = str(k)
            if overrides.get("enabled") is False and not url:
                self.enabled = False
        info = {"ok": False, "url": self.url, "model": self.model,
                "source": self.source, "enabled": self.enabled,
                "error": "", "latency_ms": 0, "status_code": 0}
        if not self.enabled:
            info["error"] = "本地 AI 未启用（请勾选「启用本地 AI」并保存）"
            return info
        if not self.url.lower().startswith(("http://", "https://")):
            info["error"] = "服务地址必须以 http:// 或 https:// 开头"
            return info
        try:
            from core.security_config import allow_ai_enhance
            if not allow_ai_enhance():
                info["error"] = "涉密模式已启用，联网 / AI 功能被强制禁用"
                return info
        except Exception:
            pass

        import requests
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model,
                   "messages": [{"role": "user", "content": "ping"}],
                   "max_tokens": 1, "temperature": 0}
        t0 = time.time()
        try:
            resp = requests.post(self.url, headers=headers, json=payload, timeout=timeout)
            info["latency_ms"] = int((time.time() - t0) * 1000)
            info["status_code"] = resp.status_code
            if resp.status_code >= 400:
                info["error"] = f"服务返回 HTTP {resp.status_code}：{(resp.text or '')[:200]}"
                return info
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("model"):
                    info["model_echo"] = data["model"]
            except Exception:
                pass
            info["ok"] = True
        except Exception as e:
            info["latency_ms"] = int((time.time() - t0) * 1000)
            info["error"] = self._friendly_error(e)
        return info

    @staticmethod
    def _friendly_error(e: Exception) -> str:
        """把底层异常翻译成用户能照着排查的话"""
        msg = str(e)
        name = type(e).__name__
        if "Connection" in name or "ConnectionError" in msg or "Max retries" in msg:
            return (f"连不上服务，请确认本地 AI 已启动、地址与端口正确"
                    f"（原始错误：{msg}）")
        if "Timeout" in name or "timeout" in msg.lower():
            return f"请求超时，服务未在预期时间内响应（原始错误：{msg}）"
        return msg

    def _empty_result(self) -> Dict[str, Any]:
        return {"standard_name": "", "columns": [], "rows": [], "confidence": 0.0}

    def _build_prompt(self, raw_text: str, context: str) -> str:
        return f"""请从以下文档片段中提取标准信息并以 JSON 返回。
要求：
1. standard_name: 识别出的标准编号/名称（如 GB/T 12345-2020）。
2. columns: 表格或列表的列名数组。
3. rows: 每行数据，每个元素为与列名对应的字典。
4. confidence: 0-1 的置信度。

上下文：{context}

原文：
{raw_text}

请只返回 JSON，不要其他解释。"""

    def _call(self, prompt: str) -> Dict[str, Any]:
        import requests
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }
        # 单条请求超时 10 秒（原为 60 秒）：本地模型正常都在秒级返回，
        # 60 秒只会让"服务不可用"时的空等被放大 N 倍（逐条 × 60s）
        resp = requests.post(self.url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"AI 返回非字典: {type(data)}")
        choices = data.get("choices")
        if not choices or not isinstance(choices, list) or len(choices) == 0:
            raise ValueError("AI 返回缺少 choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not message or not isinstance(message, dict):
            raise ValueError("AI 返回缺少 message")
        content = message.get("content", "")
        return self._parse_json(content)

    def _parse_json(self, content: str) -> Dict[str, Any]:
        # 尝试直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # 尝试截取第一个 { 和最后一个 } 之间的内容
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(content[start:end + 1])
            except json.JSONDecodeError:
                pass
        # 如果仍然失败，返回空结果
        return self._empty_result()

    def validate_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """校验并补齐 AI 返回字段。"""
        if not isinstance(result, dict):
            return self._empty_result()
        return {
            "standard_name": str(result.get("standard_name", "")).strip(),
            "columns": result.get("columns", []) if isinstance(result.get("columns"), list) else [],
            "rows": result.get("rows", []) if isinstance(result.get("rows"), list) else [],
            "confidence": float(result.get("confidence", 0.0)),
        }
