"""“专业模式”通用网页采集器。

这个模块把原项目中的安全边界、认证会话、分页、深度采集、内容选择器和附件
引用提取能力压缩成一个本地 Python 版本。它只负责采集和解析，通知入库、规则
分析及可选 AI 纪要由 notice_viewer.py 统一处理。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PROFESSIONAL_CONFIG_PATH = DATA_DIR / "professional_config.json"

ATTACHMENT_SUFFIXES = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip", "rar", "txt", "csv", "md"
}
DATE_PATTERN = re.compile(
    r"((?:19|20)\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})(?:日)?"
    r"(?:[ T]?(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?"
)
PUBLISHED_LABEL_PATTERN = re.compile(
    r"(?:发布时间|发布日期|发布于)\s*[:：]?\s*(" + DATE_PATTERN.pattern + r")",
    re.I,
)
DEPARTMENT_LABEL_PATTERN = re.compile(
    r"(?:发布部门|部门|发布单位|来源单位|来源)\s*[:：]?\s*([^\s|,，;；]+)",
    re.I,
)

DEFAULT_PROFESSIONAL_CONFIG: dict[str, Any] = {
    "enabled": False,
    "profile_name": "default",
    "site_url": "",
    "entry_url": "",
    "allowed_domains": [],
    "include_url_patterns": [],
    "exclude_url_patterns": ["**/login/**", "**/logout**", "**/signout**", "**/logoff**"],
    "auth": {
        "type": "NONE",
        "login_url": "",
        "username": "",
        "password": "",
        "username_field": "username",
        "password_field": "password",
        "direct_submit": False,
        "login_via_entry": True,
        "extra_params": {},
        "token": "",
        "header_name": "Authorization",
        "token_prefix": "Bearer ",
        "headers": {},
    },
    "parser": {
        "type": "HTML",
        "title_selector": "",
        "published_selector": "",
        "department_selector": "",
        "content_selector": "",
        "include_selectors": [],
        "remove_selectors": ["script", "style", "noscript", "nav", "header", "footer"],
        "attachment_selectors": [],
        "json_paths": {"title": "", "published": "", "department": "", "content": ""},
        "pagination_rules": [],
    },
    "security": {
        "same_origin_only": True,
        "private_network_enabled": False,
        "private_network_allowlist": [],
        "verify_tls": True,
        "respect_robots_txt": True,
        "allow_redirects": True,
        "max_redirects": 5,
    },
    "limits": {
        "max_depth": 3,
        "max_pages": 200,
        "max_run_seconds": 1800,
        "max_response_bytes": 10485760,
        "max_attachment_bytes": 52428800,
        "max_total_bytes": 536870912,
        "request_interval_ms": 500,
        "connect_timeout_seconds": 10,
        "read_timeout_seconds": 30,
        "retry_count": 2,
    },
    "output": {
        "download_attachments": True,
        "incremental_enabled": True,
        "file_name_template": "{linkTitle}",
        "dedup_strategy": "URL_CONTENT_HASH",
        "published_from": "",
        "published_to": "",
        "user_agent": "Personal-College-Notice-Viewer/Professional/1.0",
    },
    "logout": {"enabled": False, "url": "", "method": "GET"},
}


class ProfessionalConfigError(ValueError):
    pass


class ProfessionalCrawlError(RuntimeError):
    pass


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_professional_config(config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """合并默认、被忽略的本机配置、项目本地配置和运行时配置。"""

    persisted = _load_json_file(PROFESSIONAL_CONFIG_PATH)
    configured = config.get("professional") if isinstance(config.get("professional"), dict) else {}
    runtime = config.get("_runtime_professional") if isinstance(config.get("_runtime_professional"), dict) else {}
    result = _deep_merge(DEFAULT_PROFESSIONAL_CONFIG, persisted)
    if configured:
        result = _deep_merge(result, configured)
    if runtime:
        result = _deep_merge(result, runtime)
    source = "runtime" if runtime else "config" if configured else "persisted" if persisted else "default"
    return normalize_professional_config(result), source


def _string_list(value: Any, limit: int = 200) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:500] for item in value[:limit] if str(item).strip()]


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower().strip() in {"1", "true", "yes", "on"}:
            return True
        if value.lower().strip() in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int):
        return value != 0
    return default


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def normalize_professional_config(raw: dict[str, Any]) -> dict[str, Any]:
    result = _deep_merge(DEFAULT_PROFESSIONAL_CONFIG, raw)
    result["enabled"] = _bool(result.get("enabled"), False)
    result["profile_name"] = re.sub(r"[^\w.-]+", "_", str(result.get("profile_name") or "default"), flags=re.UNICODE)[:60] or "default"
    result["site_url"] = str(result.get("site_url") or "").strip()
    result["entry_url"] = str(result.get("entry_url") or "").strip()
    result["allowed_domains"] = [item.lower() for item in _string_list(result.get("allowed_domains"), 50)]
    result["include_url_patterns"] = _string_list(result.get("include_url_patterns"), 100)
    result["exclude_url_patterns"] = _string_list(result.get("exclude_url_patterns"), 100)

    auth = result["auth"] if isinstance(result.get("auth"), dict) else {}
    auth["type"] = str(auth.get("type") or "NONE").upper()
    auth["login_url"] = str(auth.get("login_url") or "").strip()
    auth["username"] = str(auth.get("username") or "")
    auth["password"] = str(auth.get("password") or "")
    auth["username_field"] = str(auth.get("username_field") or "username")[:128]
    auth["password_field"] = str(auth.get("password_field") or "password")[:128]
    auth["direct_submit"] = _bool(auth.get("direct_submit"), False)
    auth["login_via_entry"] = _bool(auth.get("login_via_entry"), True)
    auth["extra_params"] = auth.get("extra_params") if isinstance(auth.get("extra_params"), dict) else {}
    auth["token"] = str(auth.get("token") or "")
    auth["header_name"] = str(auth.get("header_name") or "Authorization")[:128]
    auth["token_prefix"] = str(auth.get("token_prefix") if auth.get("token_prefix") is not None else "Bearer ")[:100]
    auth["headers"] = auth.get("headers") if isinstance(auth.get("headers"), dict) else {}
    auth["headers"] = {str(k)[:128]: str(v)[:2000] for k, v in auth["headers"].items() if str(k).strip() and str(v).strip()}
    result["auth"] = auth

    parser = result["parser"] if isinstance(result.get("parser"), dict) else {}
    parser["type"] = str(parser.get("type") or "HTML").upper()
    if parser["type"] not in {"HTML", "JSON", "TEXT"}:
        parser["type"] = "HTML"
    for key in ("title_selector", "published_selector", "department_selector", "content_selector"):
        parser[key] = str(parser.get(key) or "").strip()[:1000]
    for key in ("include_selectors", "remove_selectors", "attachment_selectors"):
        parser[key] = _string_list(parser.get(key), 100)
    paths = parser.get("json_paths") if isinstance(parser.get("json_paths"), dict) else {}
    parser["json_paths"] = {key: str(paths.get(key) or "").strip()[:300] for key in ("title", "published", "department", "content")}
    parser["pagination_rules"] = parser.get("pagination_rules") if isinstance(parser.get("pagination_rules"), list) else []
    result["parser"] = parser

    security = result["security"] if isinstance(result.get("security"), dict) else {}
    for key, default in (("same_origin_only", True), ("private_network_enabled", False), ("verify_tls", True), ("respect_robots_txt", True), ("allow_redirects", True)):
        security[key] = _bool(security.get(key), default)
    security["private_network_allowlist"] = [item.lower() for item in _string_list(security.get("private_network_allowlist"), 100)]
    security["max_redirects"] = _int(security.get("max_redirects"), 5, 0, 20)
    result["security"] = security

    limits = result["limits"] if isinstance(result.get("limits"), dict) else {}
    for key, default, minimum, maximum in (
        ("max_depth", 3, 0, 20),
        ("max_pages", 200, 1, 10000),
        ("max_run_seconds", 1800, 10, 86400),
        ("max_response_bytes", 10485760, 1024, 200000000),
        ("max_attachment_bytes", 52428800, 1024, 200000000),
        ("max_total_bytes", 536870912, 1024, 2000000000),
        ("request_interval_ms", 500, 0, 60000),
        ("connect_timeout_seconds", 10, 1, 120),
        ("read_timeout_seconds", 30, 1, 600),
        ("retry_count", 2, 0, 5),
    ):
        limits[key] = _int(limits.get(key), default, minimum, maximum)
    result["limits"] = limits

    output = result["output"] if isinstance(result.get("output"), dict) else {}
    output["download_attachments"] = _bool(output.get("download_attachments"), True)
    output["incremental_enabled"] = _bool(output.get("incremental_enabled"), True)
    output["file_name_template"] = str(output.get("file_name_template") or "{linkTitle}")[:200]
    output["dedup_strategy"] = str(output.get("dedup_strategy") or "URL_CONTENT_HASH").upper()
    output["published_from"] = str(output.get("published_from") or "")[:20]
    output["published_to"] = str(output.get("published_to") or "")[:20]
    output["user_agent"] = str(output.get("user_agent") or DEFAULT_PROFESSIONAL_CONFIG["output"]["user_agent"])[:200]
    result["output"] = output

    logout = result["logout"] if isinstance(result.get("logout"), dict) else {}
    logout["enabled"] = _bool(logout.get("enabled"), False)
    logout["url"] = str(logout.get("url") or "").strip()
    logout["method"] = "POST" if str(logout.get("method") or "GET").upper() == "POST" else "GET"
    result["logout"] = logout
    return result


def validate_professional_config(profile: dict[str, Any]) -> dict[str, Any]:
    profile = normalize_professional_config(profile)
    for name in ("site_url", "entry_url"):
        parsed = urlparse(profile[name])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ProfessionalConfigError(f"{name} 必须是不含用户信息的 HTTP/HTTPS 地址")
    site_host = urlparse(profile["site_url"]).hostname.lower()
    if not profile["allowed_domains"]:
        profile["allowed_domains"] = [site_host]
    if not any(site_host == domain or site_host.endswith("." + domain) for domain in profile["allowed_domains"]):
        raise ProfessionalConfigError("目标网站不在 allowed_domains 白名单中")
    entry_host = urlparse(profile["entry_url"]).hostname.lower()
    if not any(entry_host == domain or entry_host.endswith("." + domain) for domain in profile["allowed_domains"]):
        raise ProfessionalConfigError("采集入口不在 allowed_domains 白名单中")
    auth_type = profile["auth"]["type"]
    if auth_type not in {"NONE", "BEARER", "FORM_PASSWORD", "CUSTOM_HEADERS"}:
        raise ProfessionalConfigError("auth.type 仅支持 NONE、BEARER、FORM_PASSWORD、CUSTOM_HEADERS")
    if auth_type == "FORM_PASSWORD":
        if not profile["auth"]["login_url"]:
            raise ProfessionalConfigError("FORM_PASSWORD 必须配置 auth.login_url")
        if not profile["auth"]["username"] or not profile["auth"]["password"]:
            raise ProfessionalConfigError("FORM_PASSWORD 必须配置账号和密码")
    if auth_type == "BEARER" and not profile["auth"]["token"]:
        raise ProfessionalConfigError("BEARER 必须配置 auth.token")
    if auth_type == "CUSTOM_HEADERS" and not profile["auth"]["headers"]:
        raise ProfessionalConfigError("CUSTOM_HEADERS 必须配置 auth.headers")
    if profile["security"]["private_network_enabled"] and not profile["security"]["private_network_allowlist"]:
        raise ProfessionalConfigError("启用 private_network_enabled 时必须配置 private_network_allowlist")
    for name in ("published_from", "published_to"):
        if profile["output"][name] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", profile["output"][name]):
            raise ProfessionalConfigError(f"output.{name} 必须使用 YYYY-MM-DD")
    if profile["output"]["published_from"] and profile["output"]["published_to"] and profile["output"]["published_from"] > profile["output"]["published_to"]:
        raise ProfessionalConfigError("发布时间开始不能晚于结束")
    if profile["logout"]["enabled"] and not profile["logout"]["url"]:
        raise ProfessionalConfigError("启用 logout 时必须配置 logout.url")
    return profile


def professional_config_status(config: dict[str, Any]) -> dict[str, Any]:
    try:
        profile, source = resolve_professional_config(config)
        validated = validate_professional_config(profile)
        status = "READY" if validated.get("enabled") else "DISABLED"
        error = ""
    except ProfessionalConfigError as exc:
        profile, source = resolve_professional_config(config)
        if not profile.get("enabled") and not profile.get("site_url") and not profile.get("entry_url"):
            status = "DISABLED"
            error = ""
        else:
            status = "INVALID"
            error = str(exc)
    auth = profile.get("auth") if isinstance(profile.get("auth"), dict) else {}
    safe_auth = {
        "type": auth.get("type", "NONE"),
        "login_url": safe_url(auth.get("login_url", "")),
        "username_configured": bool(auth.get("username")),
        "password_configured": bool(auth.get("password")),
        "token_configured": bool(auth.get("token")),
        "header_names": sorted(str(key) for key in (auth.get("headers") or {}).keys()),
    }
    return {
        "status": status,
        "error": error,
        "source": source,
        "enabled": bool(profile.get("enabled")),
        "profile_name": profile.get("profile_name", "default"),
        "site_url": safe_url(profile.get("site_url", "")),
        "entry_url": safe_url(profile.get("entry_url", "")),
        "allowed_domains": profile.get("allowed_domains", []),
        "auth": safe_auth,
        "parser_type": profile.get("parser", {}).get("type", "HTML"),
        "max_depth": profile.get("limits", {}).get("max_depth"),
        "max_pages": profile.get("limits", {}).get("max_pages"),
        "persisted": PROFESSIONAL_CONFIG_PATH.exists(),
    }


def save_professional_config(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("clear"):
        PROFESSIONAL_CONFIG_PATH.unlink(missing_ok=True)
        config.pop("_runtime_professional", None)
        return professional_config_status(config)
    if isinstance(payload.get("profile"), dict):
        raw = payload["profile"]
    elif isinstance(payload.get("professional"), dict):
        raw = payload["professional"]
    else:
        raw = {key: value for key, value in payload.items() if key not in {"remember", "clear"}}
    current, _ = resolve_professional_config(config)
    candidate = _deep_merge(current, raw)
    candidate = validate_professional_config(candidate)
    config["_runtime_professional"] = candidate
    if payload.get("remember"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PROFESSIONAL_CONFIG_PATH.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return professional_config_status(config)


def normalize_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.params, parsed.query, ""))


def safe_url(value: str) -> str:
    try:
        parsed = urlparse(str(value or ""))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except ValueError:
        return ""


def safe_filename(value: str, fallback: str = "crawled-page") -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", str(value or fallback)).strip(" .") or fallback
    return text[:150]


def _has_attachment_suffix(value: str) -> bool:
    path = urlparse(value).path.lower()
    suffix = path.rsplit(".", 1)[-1] if "." in path else ""
    return suffix in ATTACHMENT_SUFFIXES


def _text(value: Any) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", str(value or "").replace("\r", "")).strip()


def _date(value: str) -> str:
    match = DATE_PATTERN.search(_text(value))
    if not match:
        return _text(value)
    result = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    if match.group(4) is not None:
        result += f" {int(match.group(4)):02d}:{int(match.group(5)):02d}"
    return result


def _in_date_range(value: str, start: str, end: str) -> bool:
    date_value = _date(value)[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        return True
    return (not start or date_value >= start) and (not end or date_value <= end)


def _glob_match(value: str, rule: str) -> bool:
    if not rule:
        return False
    if rule.startswith("regex:"):
        try:
            return bool(re.search(rule[6:], value))
        except re.error:
            return False
    regex = ""
    index = 0
    while index < len(rule):
        char = rule[index]
        if char == "*":
            if index + 1 < len(rule) and rule[index + 1] == "*":
                regex += ".*"
                index += 1
            else:
                regex += "[^/]*"
        elif char == "?":
            regex += "."
        else:
            regex += re.escape(char)
        index += 1
    return bool(re.search(regex, value))


def _json_path(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            return None
    return current


class ProfessionalSession:
    def __init__(self, profile: dict[str, Any]):
        self.profile = profile
        self.session = requests.Session()
        self.site = urlparse(profile["site_url"])
        self.login_uri = urlparse(profile["auth"].get("login_url") or "") if profile["auth"].get("login_url") else None
        self.total_bytes = 0
        self.robots: RobotFileParser | None = None
        self.robots_loaded = False
        self.session.headers.update({"User-Agent": profile["output"]["user_agent"], "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8"})

    def same_origin(self, url: str, other: str) -> bool:
        left, right = urlparse(url), urlparse(other)
        left_port = left.port or (443 if left.scheme == "https" else 80)
        right_port = right.port or (443 if right.scheme == "https" else 80)
        return left.scheme.lower() == right.scheme.lower() and left.hostname and right.hostname and left.hostname.lower() == right.hostname.lower() and left_port == right_port

    def _private_allowed(self, host: str, address: str) -> bool:
        allowlist = self.profile["security"]["private_network_allowlist"]
        host = host.lower()
        address = address.lower()
        if host in allowlist or address in allowlist:
            return True
        candidate = ipaddress.ip_address(address)
        for rule in allowlist:
            try:
                if candidate in ipaddress.ip_network(rule, strict=False):
                    return True
            except ValueError:
                continue
        return False

    def validate_url(self, url: str, authentication_flow: bool = False) -> str:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ProfessionalCrawlError("只允许不含用户信息的 HTTP/HTTPS 地址")
        host = parsed.hostname.lower()
        allowed = self.profile["allowed_domains"]
        if not any(host == domain or host.endswith("." + domain) for domain in allowed):
            raise ProfessionalCrawlError(f"目标域名不在采集白名单中: {host}")
        if not authentication_flow and self.profile["security"]["same_origin_only"] and not self.same_origin(self.profile["site_url"], url):
            raise ProfessionalCrawlError(f"当前配置仅允许访问目标网站同源地址: {safe_url(url)}")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))}
        except socket.gaierror as exc:
            raise ProfessionalCrawlError(f"无法解析目标域名: {host}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            private = ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            if private and not self.profile["security"]["private_network_enabled"]:
                raise ProfessionalCrawlError(f"默认禁止访问本机或内网地址: {host}")
            if private and not self._private_allowed(host, address):
                raise ProfessionalCrawlError(f"内网地址不在显式白名单中: {host}")
        return normalize_url(url)

    def _auth_headers(self, url: str, authentication_flow: bool) -> dict[str, str]:
        auth = self.profile["auth"]
        auth_type = auth["type"]
        if auth_type == "BEARER":
            base = auth.get("login_url") or self.profile["site_url"]
            if self.profile["security"]["same_origin_only"] and not self.same_origin(base, url):
                return {}
            return {auth["header_name"]: auth["token_prefix"] + auth["token"]}
        if auth_type == "CUSTOM_HEADERS" and self.same_origin(self.profile["site_url"], url):
            return {str(k): str(v) for k, v in auth.get("headers", {}).items()}
        return {}

    def _read_robots(self) -> None:
        if self.robots_loaded or not self.profile["security"]["respect_robots_txt"]:
            return
        self.robots_loaded = True
        robots_url = f"{self.site.scheme}://{self.site.netloc}/robots.txt"
        try:
            response = self._request(robots_url, authentication_flow=True, skip_robots=True, retry=False)
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            self.robots = parser
        except Exception:
            self.robots = None

    def _robots_allowed(self, url: str) -> bool:
        self._read_robots()
        return self.robots is None or self.robots.can_fetch(self.profile["output"]["user_agent"], url)

    def _request(
        self,
        url: str,
        method: str = "GET",
        form_data: dict[str, str] | None = None,
        authentication_flow: bool = False,
        skip_robots: bool = False,
        retry: bool = True,
    ) -> requests.Response:
        current = self.validate_url(url, authentication_flow)
        if not skip_robots and not authentication_flow and not self._robots_allowed(current):
            raise ProfessionalCrawlError(f"robots.txt 禁止采集该地址: {safe_url(current)}")
        limits = self.profile["limits"]
        attempts = limits["retry_count"] + 1 if retry else 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                for redirect_count in range(limits["max_redirects"] + 1):
                    headers = self._auth_headers(current, authentication_flow)
                    response = self.session.request(
                        method.upper(),
                        current,
                        data=form_data if method.upper() == "POST" else None,
                        headers=headers,
                        timeout=(limits["connect_timeout_seconds"], limits["read_timeout_seconds"]),
                        allow_redirects=False,
                        verify=self.profile["security"]["verify_tls"],
                    )
                    if 300 <= response.status_code < 400 and response.headers.get("Location"):
                        if not self.profile["security"]["allow_redirects"]:
                            raise ProfessionalCrawlError("采集请求发生了未允许的重定向")
                        if redirect_count >= limits["max_redirects"]:
                            raise ProfessionalCrawlError("采集请求重定向次数超限")
                        current = self.validate_url(urljoin(current, response.headers["Location"]), authentication_flow)
                        method = "GET"
                        form_data = None
                        continue
                    if response.status_code < 200 or response.status_code >= 400:
                        raise ProfessionalCrawlError(f"采集请求失败，HTTP {response.status_code}: {safe_url(current)}")
                    self.validate_url(response.url, authentication_flow)
                    if len(response.content) > limits["max_response_bytes"]:
                        raise ProfessionalCrawlError("响应超过配置大小上限")
                    self.total_bytes += len(response.content)
                    if self.total_bytes > limits["max_total_bytes"]:
                        raise ProfessionalCrawlError("本次任务累计下载量超过配置上限")
                    return response
                raise ProfessionalCrawlError("采集请求重定向处理失败")
            except requests.RequestException as exc:
                last = exc
                if attempt + 1 < attempts:
                    time.sleep(min(1 + attempt, 5))
            except Exception as exc:
                last = exc
                if attempt + 1 < attempts and isinstance(exc, ProfessionalCrawlError) and "HTTP 5" in str(exc):
                    time.sleep(min(1 + attempt, 5))
                else:
                    raise
        raise ProfessionalCrawlError(f"请求失败: {last.__class__.__name__ if last else 'unknown'}") from last

    def login(self) -> None:
        auth = self.profile["auth"]
        if auth["type"] == "NONE":
            return
        if auth["type"] == "CUSTOM_HEADERS":
            self._request(self.profile["entry_url"])
            return
        if auth["type"] == "BEARER":
            if auth.get("login_url"):
                self._request(auth["login_url"], authentication_flow=True)
            self._request(self.profile["entry_url"])
            return
        login_url = auth["login_url"]
        login_start = self.profile["entry_url"] if auth["login_via_entry"] else login_url
        page = self._request(login_start, authentication_flow=True)
        soup = BeautifulSoup(page.content, "html.parser")
        form = next((item for item in soup.select("form") if item.select_one("input[type=password]")), soup.select_one("form"))
        if form is None and login_start != login_url:
            page = self._request(login_url, authentication_flow=True)
            soup = BeautifulSoup(page.content, "html.parser")
            form = next((item for item in soup.select("form") if item.select_one("input[type=password]")), soup.select_one("form"))
        if form is None:
            raise ProfessionalCrawlError("登录页面中未找到登录表单")
        action = form.get("action") or page.url
        action = self.validate_url(urljoin(page.url, action), authentication_flow=True)
        fields: dict[str, str] = {}
        for input_node in form.select("input[name]"):
            input_type = str(input_node.get("type") or "").lower()
            if input_type not in {"submit", "button", "image", "file"}:
                fields[str(input_node.get("name"))] = str(input_node.get("value") or "")
        username_field = auth["username_field"] or next((str(item.get("name")) for item in form.select("input[name]") if item.get("type") != "password"), "username")
        password_field = auth["password_field"] or str((form.select_one("input[type=password][name]") or {}).get("name") or "password")
        fields[username_field] = auth["username"]
        fields[password_field] = auth["password"]
        fields.update({str(k): str(v) for k, v in auth.get("extra_params", {}).items()})
        if str(form.get("method") or "POST").upper() == "GET":
            raise ProfessionalCrawlError("安全策略禁止通过 GET 提交登录账号和密码")
        self._request(action, method="POST", form_data=fields, authentication_flow=True)
        self._request(self.profile["entry_url"])

    def logout(self) -> None:
        logout = self.profile["logout"]
        try:
            if logout["enabled"] and logout["url"]:
                self._request(logout["url"], method=logout["method"], authentication_flow=True, skip_robots=True, retry=False)
        finally:
            self.session.close()

    def fetch_text(self, url: str, method: str = "GET", form_data: dict[str, str] | None = None) -> requests.Response:
        response = self._request(url, method=method, form_data=form_data)
        if self.profile["auth"]["type"] in {"FORM_PASSWORD", "CUSTOM_HEADERS"}:
            if BeautifulSoup(response.content, "html.parser").select_one("form input[type=password]") is not None:
                raise ProfessionalCrawlError("认证未成功或已过期，目标页面仍返回登录表单")
        return response

    def fetch_bytes(self, url: str) -> bytes:
        return self._request(url).content


def _selected_text(soup: BeautifulSoup, selector: str) -> str:
    if not selector:
        return ""
    try:
        element = soup.select_one(selector)
    except Exception as exc:
        raise ProfessionalConfigError(f"CSS 选择器无效: {selector}") from exc
    if element is None:
        return ""
    return _text(element.get("content") if element.name == "meta" else element.get_text(" ", strip=True))


def _clean_element(element: Any, remove_selectors: list[str]) -> str:
    clone = BeautifulSoup(str(element), "html.parser")
    for selector in remove_selectors:
        try:
            for child in clone.select(selector):
                child.decompose()
        except Exception:
            continue
    lines = [_text(line) for line in clone.get_text("\n", strip=True).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _extract_html(response: requests.Response, profile: dict[str, Any]) -> tuple[str, str, str, str, BeautifulSoup]:
    soup = BeautifulSoup(response.content, "html.parser")
    parser = profile["parser"]
    title = _selected_text(soup, parser["title_selector"])
    if not title:
        for selector in ["meta[property='og:title']", "h1", "h2", "title"]:
            title = _selected_text(soup, selector)
            if title:
                break
    published = _selected_text(soup, parser["published_selector"])
    department = _selected_text(soup, parser["department_selector"])
    candidates: list[Any] = []
    for selector in parser["include_selectors"]:
        candidates.extend(soup.select(selector))
    if not candidates and parser["content_selector"]:
        candidates = soup.select(parser["content_selector"])
    if not candidates:
        candidates = soup.select("article, main, [class*='content'], [class*='article'], body")
    if not candidates and soup.body:
        candidates = [soup.body]
    for node in soup.select(",".join(parser["remove_selectors"]) if parser["remove_selectors"] else "script,style,noscript"):
        node.decompose()
    body = max((_clean_element(element, []) for element in candidates), key=len, default="")
    if not published:
        match = PUBLISHED_LABEL_PATTERN.search(body)
        published = match.group(1) if match else (DATE_PATTERN.search(body).group(0) if DATE_PATTERN.search(body) else "")
    if not department:
        match = DEPARTMENT_LABEL_PATTERN.search(body)
        department = match.group(1) if match else ""
    return title, _date(published), _text(department), body, soup


def _extract_json(response: requests.Response, profile: dict[str, Any]) -> tuple[str, str, str, str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise ProfessionalCrawlError("JSON 解析失败") from exc
    paths = profile["parser"]["json_paths"]
    title = _text(_json_path(value, paths["title"]))
    published = _text(_json_path(value, paths["published"]))
    department = _text(_json_path(value, paths["department"]))
    content_value = _json_path(value, paths["content"])
    content = content_value if isinstance(content_value, str) else json.dumps(content_value if content_value is not None else value, ensure_ascii=False, indent=2)
    return title, _date(published), department, _text(content), value


def extract_page(response: requests.Response, profile: dict[str, Any]) -> dict[str, Any]:
    parser_type = profile["parser"]["type"]
    if parser_type == "JSON":
        title, published, department, body, document = _extract_json(response, profile)
    elif parser_type == "TEXT":
        title, published, department, body, document = "", "", "", _text(response.text), None
    else:
        title, published, department, body, document = _extract_html(response, profile)
    if not title:
        title = _text(urlparse(response.url).path.rstrip("/").rsplit("/", 1)[-1]) or "未命名页面"
    if not body:
        raise ProfessionalCrawlError("页面没有提取到正文")
    return {
        "title": title[:300],
        "published_text": published[:100],
        "published_at": published[:100],
        "department": department[:200],
        "content": body[:200000],
        "document": document,
    }


def attachment_name(link: Any, url: str) -> str:
    for candidate in (_text(link.get_text(" ", strip=True)), _text(link.get("title")), _text(link.get("download")), urlparse(url).path.rsplit("/", 1)[-1]):
        if candidate and candidate.lower() not in {"download", "下载", "点击下载", "附件"}:
            return safe_filename(candidate, "附件")
    return "附件"


def extract_attachments(soup: BeautifulSoup | None, page_url: str, profile: dict[str, Any], session: ProfessionalSession) -> list[dict[str, str]]:
    if soup is None:
        return []
    parser = profile["parser"]
    candidates: list[Any] = []
    selectors = parser["attachment_selectors"]
    if selectors:
        for selector in selectors:
            candidates.extend(soup.select(selector))
    else:
        candidates = soup.select("a[href]")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for element in candidates:
        links = [element] if getattr(element, "name", "") == "a" else element.select("a[href]")
        for link in links:
            url = normalize_url(urljoin(page_url, link.get("href", "")))
            if not url or url in seen or session.is_logout_url(url):
                continue
            marker = " ".join([_text(link.get_text(" ", strip=True)), str(link.get("download") or ""), url])
            if not selectors and not (_has_attachment_suffix(url) or "download" in url.lower() or "attachment" in url.lower()):
                continue
            try:
                session.validate_url(url)
            except ProfessionalCrawlError:
                continue
            seen.add(url)
            result.append({"name": attachment_name(link, url), "url": url})
    return result


def _form_fields(form: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in form.select("input[name],select[name],textarea[name]"):
        name = _text(field.get("name"))
        if not name or len(values) >= 100:
            continue
        field_type = str(field.get("type") or "").lower()
        if field_type in {"password", "file", "submit", "button", "image", "reset"}:
            continue
        if field_type in {"checkbox", "radio"} and not field.has_attr("checked"):
            continue
        if field.name == "select":
            option = field.select_one("option[selected]") or field.select_one("option")
            values[name] = str(option.get("value") if option else "")
        elif field.name == "textarea":
            values[name] = field.get_text()
        else:
            values[name] = str(field.get("value") or "")
    return values


def _pagination_targets(soup: BeautifulSoup | None, page_url: str, profile: dict[str, Any], current_form: dict[str, str]) -> list[dict[str, Any]]:
    if soup is None:
        return []
    result: list[dict[str, Any]] = []
    for rule in profile["parser"]["pagination_rules"]:
        if not isinstance(rule, dict):
            continue
        selector = str(rule.get("selector") or "").strip()
        target_type = str(rule.get("targetType") or rule.get("target_type") or "ATTRIBUTE").upper()
        if not selector:
            continue
        for element in soup.select(selector):
            if target_type == "PAGE_TEMPLATE":
                page_attribute = str(rule.get("pageAttribute") or rule.get("page_attribute") or "data-page")
                page = str(element.get(page_attribute) or "").strip()
                target = str(rule.get("urlTemplate") or rule.get("url_template") or "").replace("{page}", page) if page else ""
                form_data = {}
                method = "GET"
            elif target_type == "POST_FORM":
                page_attribute = str(rule.get("pageAttribute") or rule.get("page_attribute") or "data-page")
                page = str(element.get(page_attribute) or "").strip()
                target = str(rule.get("requestUrl") or rule.get("request_url") or page_url)
                form_data = _form_fields(element if getattr(element, "name", "") == "form" else soup)
                page_param = str(rule.get("pageParam") or rule.get("page_param") or "current")
                if page:
                    form_data[page_param] = page
                method = "POST"
            else:
                attribute = str(rule.get("targetAttribute") or rule.get("target_attribute") or "href")
                target = str(element.get(attribute) or "")
                form_data = {}
                method = "GET"
            resolved = normalize_url(urljoin(page_url, target))
            if resolved:
                result.append({"url": resolved, "method": method, "form_data": form_data, "title": _text(element.get("title")), "text": _text(element.get_text(" ", strip=True))})
        if target_type == "POST_FORM" and not result and rule.get("sequentialFallback", rule.get("sequential_fallback", True)):
            page_param = str(rule.get("pageParam") or rule.get("page_param") or "current")
            current_page = int(current_form.get(page_param) or rule.get("startPage") or 1)
            form_data = _form_fields(soup)
            form_data[page_param] = str(current_page + 1)
            target = normalize_url(urljoin(page_url, str(rule.get("requestUrl") or rule.get("request_url") or page_url)))
            if target:
                result.append({"url": target, "method": "POST", "form_data": form_data, "title": "", "text": "自动递增分页"})
    unique: dict[str, dict[str, Any]] = {}
    for item in result:
        unique[item["method"] + " " + item["url"] + " " + json.dumps(item["form_data"], sort_keys=True)] = item
    return list(unique.values())


@dataclass
class ProfessionalCrawlSummary:
    processed: int = 0
    created: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: int = 0
    attachment_references: int = 0
    new_items: list[dict[str, Any]] | None = None
    errors: list[dict[str, str]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "created": self.created,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "attachment_references": self.attachment_references,
            "new_items": self.new_items or [],
            "errors": self.errors or [],
        }


class ProfessionalCrawler:
    def __init__(self, profile: dict[str, Any]):
        self.profile = validate_professional_config(profile)
        self.session = ProfessionalSession(self.profile)

    def is_logout_url(self, url: str) -> bool:
        marker = urlparse(url).path.lower() + "?" + urlparse(url).query.lower()
        if re.search(r"(?:^|[/_.?&=-])(logout|log-out|signout|sign-out|logoff|log-off)(?:$|[/_.?&=-])", marker):
            return True
        logout = self.profile["logout"]
        return bool(logout["enabled"] and logout["url"] and normalize_url(url) == normalize_url(logout["url"]))

    def matches_rules(self, url: str) -> bool:
        if self.is_logout_url(url):
            return False
        if any(_glob_match(url, rule) for rule in self.profile["exclude_url_patterns"]):
            return False
        includes = self.profile["include_url_patterns"]
        return not includes or any(_glob_match(url, rule) for rule in includes)

    def test_login(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            self.session.login()
            self.session.fetch_text(self.profile["entry_url"])
            return {
                "success": True,
                "profile_name": self.profile["profile_name"],
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "message": "登录会话建立成功",
            }
        except Exception as exc:
            return {
                "success": False,
                "profile_name": self.profile["profile_name"],
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "message": str(exc)[:500],
            }
        finally:
            self.session.logout()
    def download_attachment(self, key: str, attachment: dict[str, str], store: Any) -> None:
        store.upsert_attachment(key, attachment)
        old = store.attachment(key, attachment["url"])
        if old and old["status"] == "DOWNLOADED" and old["local_path"] and Path(old["local_path"]).exists():
            return
        target: Path | None = None
        try:
            body = self.session.fetch_bytes(attachment["url"])
            max_bytes = self.profile["limits"]["max_attachment_bytes"]
            if len(body) > max_bytes:
                raise ProfessionalCrawlError("附件超过大小上限")
            target = BASE_DIR / "attachments" / f"{safe_filename(key)}_{safe_filename(attachment.get('name'), '附件')}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            digest = hashlib.sha256(body).hexdigest()
            store.update_attachment(key, attachment["url"], str(target), digest, "DOWNLOADED")
        except Exception as exc:
            if target:
                target.unlink(missing_ok=True)
            store.update_attachment(key, attachment["url"], None, None, "FAILED", str(exc))

    def _item_from_page(self, url: str, page: dict[str, Any], attachments: list[dict[str, str]]) -> dict[str, Any]:
        profile_name = self.profile["profile_name"]
        external_id = f"{profile_name}:{hashlib.sha256(normalize_url(url).encode('utf-8')).hexdigest()[:24]}"
        title = page["title"]
        metadata = [f"标题：{title}"]
        if page["published_text"]:
            metadata.append(f"发布时间：{page['published_text']}")
        if page["department"]:
            metadata.append(f"发布部门：{page['department']}")
        metadata.append(f"来源：{url}")
        return {
            "id": external_id,
            "type": "professional",
            "title": title,
            "department": page["department"],
            "date": page["published_text"],
            "published_text": page["published_text"],
            "published_at": page["published_at"],
            "url": url,
            "content": "\n".join(metadata) + "\n\n" + page["content"],
            "attachments": attachments,
        }

    def crawl(self, item_handler: Callable[[dict[str, Any], "ProfessionalCrawler"], tuple[bool, bool, int]]) -> dict[str, Any]:
        limits = self.profile["limits"]
        deadline = time.monotonic() + limits["max_run_seconds"]
        summary = ProfessionalCrawlSummary(new_items=[], errors=[])
        queue: deque[dict[str, Any]] = deque([{"url": normalize_url(self.profile["entry_url"]), "method": "GET", "form_data": {}, "depth": 0, "discovery_only": False, "title": "", "text": ""}])
        queued = {"GET " + normalize_url(self.profile["entry_url"]) + " {}"}
        try:
            self.session.login()
            while queue and summary.processed < limits["max_pages"]:
                if time.monotonic() > deadline:
                    raise ProfessionalCrawlError("采集任务超过最大运行时间")
                current = queue.popleft()
                try:
                    response = self.session.fetch_text(current["url"], current["method"], current["form_data"])
                    page = extract_page(response, self.profile)
                    document = page["document"]
                    if not current["discovery_only"] and _in_date_range(
                        page["published_text"], self.profile["output"]["published_from"], self.profile["output"]["published_to"]
                    ):
                        attachments = extract_attachments(document, response.url, self.profile, self.session)
                        item = self._item_from_page(response.url, page, attachments)
                        created, changed, attachment_count = item_handler(item, self)
                        summary.created += int(created)
                        summary.changed += int(changed)
                        summary.unchanged += int(not created and not changed)
                        summary.attachment_references += attachment_count
                        if created or changed:
                            summary.new_items.append(item)
                    elif not current["discovery_only"]:
                        summary.unchanged += 1

                    if current["depth"] < limits["max_depth"] and isinstance(document, BeautifulSoup):
                        pagination = _pagination_targets(document, response.url, self.profile, current["form_data"])
                        pagination_urls = {item["url"] for item in pagination}
                        for link in document.select("a[href]"):
                            next_url = normalize_url(urljoin(response.url, link.get("href", "")))
                            if not next_url or next_url in pagination_urls or _has_attachment_suffix(next_url) or not self.matches_rules(next_url):
                                continue
                            try:
                                self.session.validate_url(next_url)
                            except ProfessionalCrawlError:
                                continue
                            request_key = "GET " + next_url + " {}"
                            if request_key not in queued:
                                queued.add(request_key)
                                queue.append({"url": next_url, "method": "GET", "form_data": {}, "depth": current["depth"] + 1, "discovery_only": False, "title": _text(link.get("title")), "text": _text(link.get_text(" ", strip=True))})
                        for target in pagination:
                            if not self.matches_rules(target["url"]):
                                continue
                            self.session.validate_url(target["url"])
                            request_key = target["method"] + " " + target["url"] + " " + json.dumps(target["form_data"], sort_keys=True)
                            if request_key not in queued:
                                queued.add(request_key)
                                queue.append({"url": target["url"], "method": target["method"], "form_data": target["form_data"], "depth": current["depth"], "discovery_only": True, "title": target.get("title", ""), "text": target.get("text", "")})
                except Exception as exc:
                    summary.failed += 1
                    summary.errors.append({"url": safe_url(current["url"]), "error": str(exc)[:500]})
                summary.processed += 1
                if queue and limits["request_interval_ms"]:
                    time.sleep(limits["request_interval_ms"] / 1000)
            return summary.as_dict()
        finally:
            self.session.logout()
