"""可选的 OpenAI 兼容接口适配器。

默认不调用外部模型。只有用户配置 API Key 并打开 auto_brief 后，通知更新才会
对新增或内容变化的通知生成结构化分析和纪要。API Key 不进入 Git 跟踪文件。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UI_AI_CONFIG_PATH = DATA_DIR / "ui_ai_config.json"

DEFAULT_AI_CONFIG: dict[str, Any] = {
    "enabled": False,
    "auto_brief": False,
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "api_key": "",
    "timeout_seconds": 90,
    "max_tokens": 2400,
    "max_input_chars": 40000,
}


class NoticeAIError(RuntimeError):
    """模型调用或模型输出不符合约定时使用的错误。"""


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "y"}:
            return True
        if lowered in {"0", "false", "no", "off", "n"}:
            return False
    return default


def _load_persisted_config() -> dict[str, Any]:
    if not UI_AI_CONFIG_PATH.exists():
        return {}
    try:
        value = json.loads(UI_AI_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return value.split("?", 1)[0].split("#", 1)[0]


def resolve_ai_config(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """合并默认、本机文件、项目配置、运行时配置和环境变量。"""

    result = dict(DEFAULT_AI_CONFIG)
    sources: dict[str, str] = {}
    for source_name, source in (
        ("persisted", _load_persisted_config()),
        ("config", config.get("ai") if isinstance(config.get("ai"), dict) else {}),
        ("runtime", config.get("_runtime_ai") if isinstance(config.get("_runtime_ai"), dict) else {}),
    ):
        for key, value in source.items():
            if key in result and value is not None:
                result[key] = value
                sources[key] = source_name

    env_values = {
        "api_key": os.environ.get("NOTICE_AI_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        "base_url": os.environ.get("NOTICE_AI_BASE_URL") or os.environ.get("OPENAI_BASE_URL"),
        "model": os.environ.get("NOTICE_AI_MODEL") or os.environ.get("OPENAI_MODEL"),
        "enabled": os.environ.get("NOTICE_AI_ENABLED"),
        "auto_brief": os.environ.get("NOTICE_AI_AUTO_BRIEF"),
    }
    for key, value in env_values.items():
        if value is None or value == "":
            continue
        result[key] = _as_bool(value) if key in {"enabled", "auto_brief"} else value
        sources[key] = "environment"

    result["enabled"] = _as_bool(result.get("enabled"), False)
    result["auto_brief"] = _as_bool(result.get("auto_brief"), False)
    result["base_url"] = str(result.get("base_url") or DEFAULT_AI_CONFIG["base_url"]).strip()
    result["model"] = str(result.get("model") or DEFAULT_AI_CONFIG["model"]).strip()[:100]
    result["api_key"] = str(result.get("api_key") or "").strip()
    try:
        result["timeout_seconds"] = max(5, min(int(result.get("timeout_seconds", 90)), 600))
    except (TypeError, ValueError):
        result["timeout_seconds"] = DEFAULT_AI_CONFIG["timeout_seconds"]
    try:
        result["max_tokens"] = max(256, min(int(result.get("max_tokens", 2400)), 12000))
    except (TypeError, ValueError):
        result["max_tokens"] = DEFAULT_AI_CONFIG["max_tokens"]
    try:
        result["max_input_chars"] = max(4000, min(int(result.get("max_input_chars", 40000)), 120000))
    except (TypeError, ValueError):
        result["max_input_chars"] = DEFAULT_AI_CONFIG["max_input_chars"]
    return result, sources


def ai_config_status(config: dict[str, Any]) -> dict[str, Any]:
    """返回 AI 配置状态，不返回 API Key。"""

    settings, sources = resolve_ai_config(config)
    key = str(settings.get("api_key") or "")
    return {
        "status": "READY" if settings["enabled"] and key else "MISSING",
        "enabled": settings["enabled"],
        "auto_brief": settings["auto_brief"],
        "api_key_configured": bool(key),
        "api_key_source": sources.get("api_key", "MISSING") if key else "MISSING",
        "base_url": _redact_url(settings["base_url"]),
        "model": settings["model"],
        "timeout_seconds": settings["timeout_seconds"],
        "max_tokens": settings["max_tokens"],
        "persisted": UI_AI_CONFIG_PATH.exists(),
    }


def save_ai_config(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """保存或清除本机 AI 配置，返回脱敏状态。"""

    if payload.get("clear"):
        UI_AI_CONFIG_PATH.unlink(missing_ok=True)
        config.pop("_runtime_ai", None)
        return ai_config_status(config)

    current, _ = resolve_ai_config(config)
    base_url = str(payload.get("base_url") or current["base_url"]).strip()
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url 必须是 http 或 https 地址")
    model = str(payload.get("model") or current["model"]).strip()
    if not model or len(model) > 100:
        raise ValueError("model 不能为空且长度不能超过 100")
    api_key = str(payload.get("api_key") or current["api_key"]).strip()
    if len(api_key) > 500:
        raise ValueError("api_key 长度超出限制")
    try:
        timeout_seconds = max(5, min(int(payload.get("timeout_seconds", current["timeout_seconds"])), 600))
        max_tokens = max(256, min(int(payload.get("max_tokens", current["max_tokens"])), 12000))
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds 或 max_tokens 必须是整数") from exc

    values = {
        "enabled": _as_bool(payload.get("enabled"), bool(api_key)),
        "auto_brief": _as_bool(payload.get("auto_brief"), current["auto_brief"]),
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "timeout_seconds": timeout_seconds,
        "max_tokens": max_tokens,
        "max_input_chars": current["max_input_chars"],
    }
    config["_runtime_ai"] = values
    if payload.get("remember"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UI_AI_CONFIG_PATH.write_text(
            json.dumps(values, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return ai_config_status(config)


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)] + "\n...[内容已截断]"


def _attachment_context(detail: dict[str, Any], limit: int) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    remaining = limit
    for attachment in detail.get("attachments", []) or []:
        item = {
            "name": str(attachment.get("name") or "附件"),
            "status": str(attachment.get("status") or "UNKNOWN"),
        }
        extraction = attachment.get("extraction") or {}
        text_path = str(extraction.get("text_path") or "")
        if text_path and remaining > 200:
            try:
                text = Path(text_path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            text = _truncate(text, min(10000, remaining))
            if text:
                item["extracted_text"] = text
                remaining -= len(text)
        result.append(item)
    return result


def build_prompt(detail: dict[str, Any], max_input_chars: int = 40000) -> str:
    """构造只基于通知原文和附件提取文本的模型输入。"""

    attachment_budget = max(1000, max_input_chars // 3)
    context = {
        "notice_key": str(detail.get("notice_key") or ""),
        "title": _truncate(detail.get("title"), 1000),
        "department": _truncate(detail.get("department"), 500),
        "published_at": _truncate(detail.get("published_at") or detail.get("published_text"), 100),
        "source_url": _truncate(detail.get("url"), 1000),
        "content": _truncate(detail.get("content"), max_input_chars - attachment_budget),
        "attachments": _attachment_context(detail, attachment_budget),
    }
    return json.dumps(context, ensure_ascii=False, indent=2)


SYSTEM_PROMPT = """你是学院通知解读助手。只根据提供的通知原文和附件提取文本回答，不要补写原文没有的日期、资格、金额、地点或流程。无法确认的内容必须写入 evidence，并将 brief.status 设为 partial；如果关键内容只在扫描件或图片中，设为 needs_ocr。只输出 JSON，不要 Markdown 代码围栏。

JSON 顶层必须包含 analysis 和 brief：
analysis.categories 只能使用 competition、patent、employment、scholarship、exam、activity、safety、personnel、publicity、other；analysis.primary_category 必须是其中一个；analysis.advice 是给学生的可执行建议；analysis.keywords 是原文关键词；analysis.confidence 为 0 到 1。
brief.summary 是简明总结；brief.key_points 是重点；brief.deadlines 的每项必须包含 date、event、evidence；brief.required_materials、brief.audience、brief.evidence 为数组；brief.status 只能是 complete、partial、needs_ocr、failed；brief.confidence 为 0 到 1。不要伪造引用，evidence 尽量使用原文中的短语。"""


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or item.get("value") or ""))
        return "".join(parts)
    return ""


def parse_json_output(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise NoticeAIError("模型没有返回有效 JSON")
        try:
            parsed = json.loads(value[start : end + 1])
        except json.JSONDecodeError as exc:
            raise NoticeAIError("模型返回的 JSON 无法解析") from exc
    if not isinstance(parsed, dict):
        raise NoticeAIError("模型返回的 JSON 顶层必须是对象")
    return parsed


class NoticeAIClient:
    def __init__(self, config: dict[str, Any]):
        self.settings, _ = resolve_ai_config(config)

    @property
    def ready(self) -> bool:
        return bool(self.settings["enabled"] and self.settings["api_key"])

    @property
    def auto_brief(self) -> bool:
        return bool(self.settings["auto_brief"])

    def generate(self, detail: dict[str, Any]) -> dict[str, Any]:
        if not self.ready:
            raise NoticeAIError("AI 未配置或未启用")
        endpoint = self.settings["base_url"].rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        payload = {
            "model": self.settings["model"],
            "temperature": 0.1,
            "max_tokens": self.settings["max_tokens"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(detail, self.settings["max_input_chars"])},
            ],
        }
        try:
            response = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self.settings['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.settings["timeout_seconds"],
            )
            response.raise_for_status()
            response_data = response.json()
        except requests.RequestException as exc:
            raise NoticeAIError(f"AI 请求失败: {exc.__class__.__name__}") from exc
        except (ValueError, TypeError) as exc:
            raise NoticeAIError("AI 返回不是有效 JSON") from exc

        choices = response_data.get("choices") if isinstance(response_data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise NoticeAIError("AI 返回缺少 choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        text = _content_text(message.get("content") if isinstance(message, dict) else "")
        result = parse_json_output(text)
        model = str(response_data.get("model") or self.settings["model"])[:100]
        analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
        brief = result.get("brief") if isinstance(result.get("brief"), dict) else result
        analysis.setdefault("source", f"agent:ai:{model}")
        brief.setdefault("source", f"agent:ai:{model}")
        brief.setdefault("model", model)
        return {"analysis": analysis, "brief": brief, "model": model}
