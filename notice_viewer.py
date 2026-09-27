#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个人学院通知查看器。

默认只绑定本机地址。抓取结果落到 SQLite，附件落到本地目录；不接入项目的
Spring、MySQL、MinIO 或 RagFlow。命令行和本地 HTTP API 都可以触发更新。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from document_pipeline import EXTRACTED_DIR, extract_text, write_notice_artifacts
from notice_analysis import CATEGORY_LABELS, analyze_notice, normalize_agent_analysis, normalize_agent_brief


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ATTACHMENT_DIR = BASE_DIR / "attachments"
CONFIG_PATH = BASE_DIR / "config.local.json"
DB_PATH = DATA_DIR / "notices.sqlite3"
UI_CREDENTIALS_PATH = DATA_DIR / "ui_credentials.json"

NOTICE_TYPES = {
    "notice": "通知公告",
    "fw": "学校发文",
    "xngs": "校内公示",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "base_url": "http://lb.tec.suda.edu.cn",
    "sso_base": "https://sso.tec.suda.edu.cn",
    "service_url": "http://lb.tec.suda.edu.cn/login",
    "credentials_file": "",
    "notice_types": ["notice", "fw", "xngs"],
    "max_items_per_type": 200,
    "download_attachments": True,
    "max_attachment_bytes": 52428800,
    "request_interval_seconds": 0.5,
    "user_agent": "Personal-College-Notice-Viewer/0.1",
    "collection_start_date": "",
    "bind_host": "127.0.0.1",
    "port": 8765,
    "notifications": {"enabled": True, "desktop": False},
}

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

DATE_PATTERN = re.compile(
    r"(?P<y>(?:19|20)\d{2})[年/.-](?P<m>\d{1,2})[月/.-](?P<d>\d{1,2})(?:日)?"
    r"(?:[ T](?P<h>\d{1,2}):(?P<minute>\d{1,2})(?::(?P<s>\d{1,2}))?)?"
)
NOTICE_ID_PATTERN = re.compile(r"/uc/(?:notice|pub)/(\d+)/entity", re.I)
DOWNLOAD_PATTERN = re.compile(r"/uc/notice/\d+/download", re.I)
LIST_PATHS = {
    "notice": "/uc/notice/index",
    "fw": "/uc/user/index/dispatch",
    "xngs": "/uc/pub/index",
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def merge_config(raw: dict[str, Any]) -> dict[str, Any]:
    result = dict(DEFAULT_CONFIG)
    result["notifications"] = dict(DEFAULT_CONFIG["notifications"])
    result.update(raw)
    if isinstance(raw.get("notifications"), dict):
        result["notifications"].update(raw["notifications"])
    return result


def resolve_config_path() -> Path:
    configured = os.environ.get("NOTICE_VIEWER_CONFIG", "").strip()
    if not configured:
        return CONFIG_PATH
    path = Path(os.path.expandvars(configured))
    return path if path.is_absolute() else BASE_DIR / path


def load_config() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    path = resolve_config_path()
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    result = merge_config(raw)
    if UI_CREDENTIALS_PATH.exists():
        result["credentials_file"] = str(UI_CREDENTIALS_PATH)
    return result


def _credential_sources(config: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    sources: list[tuple[str, dict[str, Any]]] = []
    credential_file = str(config.get("credentials_file") or "").strip()
    if credential_file:
        path = Path(os.path.expandvars(credential_file))
        if not path.is_absolute():
            path = BASE_DIR / path
        if not path.exists():
            raise RuntimeError(f"凭据文件不存在: {path}")
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"凭据文件无法读取: {path}") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(f"凭据文件必须是 JSON 对象: {path}")
        sources.append(("credentials_file", loaded))
    sources.append(("config", config))
    runtime_credentials = config.get("_runtime_credentials")
    if isinstance(runtime_credentials, dict):
        sources.append(("runtime", {"credentials": runtime_credentials}))
    return sources


def _credential_values(source: dict[str, Any]) -> dict[str, Any]:
    nested = source.get("credentials")
    return nested if isinstance(nested, dict) else source


def resolve_credentials(config: dict[str, Any]) -> tuple[str, str]:
    sources = _credential_sources(config)
    username = os.environ.get("TEC_USERNAME", "").strip()
    password = os.environ.get("TEC_PASSWORD", "")
    for _, source in reversed(sources):
        credentials = _credential_values(source)
        if not username:
            username = str(credentials.get("username") or "").strip()
        if not password:
            password = str(credentials.get("password") or "")
    if not username or not password:
        raise RuntimeError(
            "未找到学院通知账号。请设置 TEC_USERNAME/TEC_PASSWORD，或在 config.local.json 配置 credentials_file。"
        )
    return username, password


def credential_status(config: dict[str, Any]) -> dict[str, Any]:
    """返回脱敏的账号配置状态，不返回密码或密码内容。"""

    try:
        sources = _credential_sources(config)
    except RuntimeError as exc:
        return {"status": "FAILED", "config_path": str(resolve_config_path()), "error": str(exc)}

    username = os.environ.get("TEC_USERNAME", "").strip()
    password = os.environ.get("TEC_PASSWORD", "")
    username_source = "environment" if username else "MISSING"
    password_source = "environment" if password else "MISSING"
    if not username or not password:
        for source_name, source in reversed(sources):
            credentials = _credential_values(source)
            if not username and str(credentials.get("username") or "").strip():
                username = str(credentials.get("username") or "").strip()
                username_source = source_name
            if not password and str(credentials.get("password") or ""):
                password = str(credentials.get("password") or "")
                password_source = source_name

    masked_username = ""
    if username:
        masked_username = username if len(username) <= 4 else f"{username[:2]}***{username[-2:]}"
    ready = bool(username and password)
    return {
        "status": "READY" if ready else "MISSING",
        "config_path": str(resolve_config_path()),
        "username": masked_username,
        "username_source": username_source,
        "password_configured": bool(password),
        "password_source": password_source,
        "persisted": UI_CREDENTIALS_PATH.exists(),
        "login_tested": False,
    }


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[ \t\u00a0]+", " ", value.replace("\r", "")).strip()


def normalize_date(value: str | None) -> str:
    text = normalize_text(value)
    match = DATE_PATTERN.search(text)
    if not match:
        return text
    year = match.group("y")
    if len(year) == 2:
        year = "20" + year
    result = f"{year}-{int(match.group('m')):02d}-{int(match.group('d')):02d}"
    if match.group("h") is not None:
        result += f" {int(match.group('h')):02d}:{int(match.group('minute')):02d}"
    return result


def safe_filename(value: str, fallback: str = "附件") -> str:
    text = normalize_text(value) or fallback
    text = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", text).strip(" .")
    return (text or fallback)[:150]


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def extract_label(text: str, labels: list[str]) -> str:
    source = normalize_text(text)
    if not source:
        return ""
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_pattern})\s*[:：]?\s*(.*?)(?=\s*(?:发布时间|发布日期|发布于|部门|发布部门|发布单位|来源单位|来源)\s*[:：]|$)",
        source,
        re.I,
    )
    return normalize_text(match.group(1)) if match else ""


def clean_body_text(element: Any) -> str:
    for node in element.select(
        "script, style, noscript, nav, header, footer, .layui-header, .layui-side, "
        ".layui-nav, .layui-layout-left, .layui-layout-right, .layui-btn, "
        ".layui-icon, [class*='logout'], [class*='password'], [class*='user-center']"
    ):
        node.decompose()
    lines: list[str] = []
    for raw in element.get_text("\n", strip=True).splitlines():
        line = normalize_text(raw)
        if not line:
            continue
        if line in {"个人中心", "密码修改", "退出登录", "开始收藏", "收藏"}:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def parse_detail_html(html_text: str, url: str, item: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    title = ""
    for selector in [".layui-custom-h1", "meta[property='og:title']", "h1", "h2", "title"]:
        element = soup.select_one(selector)
        candidate = element.get("content", "") if element and element.name == "meta" else element.get_text(" ", strip=True) if element else ""
        candidate = normalize_text(candidate)
        if candidate and len(candidate) < 300:
            title = candidate
            break

    info_element = soup.select_one(".layui-custom-h2")
    info_text = info_element.get_text(" ", strip=True) if info_element else ""
    published_text = extract_label(info_text, ["发布时间", "发布日期", "发布于"])
    department = extract_label(info_text, ["部门", "发布部门", "发布单位", "来源单位", "来源"])

    attachments: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in soup.select("a[href]"):
        href = link.get("href", "").strip()
        absolute = normalize_url(urljoin(url, href))
        if not absolute or not (DOWNLOAD_PATTERN.search(href) or re.search(r"\.(pdf|docx?|xlsx?|pptx?|zip|rar)(?:$|[?#])", href, re.I)):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        name = normalize_text(link.get_text(" ", strip=True)) or Path(urlparse(absolute).path).name or "附件"
        attachments.append({"name": safe_filename(name), "url": absolute})

    for node in soup.select("script, style, noscript, nav, header, footer, .layui-header, .layui-side"):
        node.decompose()

    candidates: list[Any] = []
    for selector in [
        ".layui-card > .layui-card-body",
        ".article-content",
        ".article-body",
        ".news-content",
        "article",
        "main",
        "div[class*='content']",
    ]:
        candidates.extend(soup.select(selector))
    if not candidates and soup.body:
        candidates = [soup.body]
    body = max((clean_body_text(element) for element in candidates), key=len, default="")
    if not title:
        title = normalize_text(item.get("title")) or "未命名通知"
    if not published_text:
        published_text = normalize_text(item.get("date"))
    if not department:
        department = normalize_text(item.get("department"))

    metadata = [f"标题：{title}"]
    if published_text:
        metadata.append(f"发布时间：{published_text}")
    if department:
        metadata.append(f"发布部门：{department}")
    metadata.append(f"来源：{url}")
    content = "\n".join(metadata) + "\n\n" + body
    return {
        **item,
        "title": title,
        "published_text": published_text,
        "published_at": normalize_date(published_text),
        "department": department,
        "content": content.strip(),
        "attachments": attachments,
    }


def notice_key(item: dict[str, Any]) -> str:
    external_id = str(item.get("id") or "").strip()
    if not external_id:
        external_id = hashlib.sha256(str(item.get("url", "")).encode("utf-8")).hexdigest()[:24]
    return f"{item.get('type', 'notice')}:{external_id}"


def normalize_date_bound(value: str | None, name: str) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    normalized = normalize_date(text)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized[:10]):
        raise ValueError(f"{name} 必须使用 YYYY-MM-DD 格式")
    return normalized[:10]


def in_date_range(value: str | None, since: str = "", until: str = "") -> bool:
    normalized = normalize_date(value)
    date_text = normalized[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        return True
    return (not since or date_text >= since) and (not until or date_text <= until)


class NoticeStore:
    def __init__(self, path: Path = DB_PATH):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS notices (
                    notice_key TEXT PRIMARY KEY,
                    external_id TEXT NOT NULL,
                    notice_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    department TEXT,
                    published_text TEXT,
                    published_at TEXT,
                    url TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    is_unread INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notices_published ON notices(published_at DESC, first_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_notices_unread ON notices(is_unread, published_at DESC);
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    local_path TEXT,
                    content_hash TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(notice_key, url)
                );
                 CREATE TABLE IF NOT EXISTS runs (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     started_at TEXT NOT NULL,
                     finished_at TEXT,
                     status TEXT NOT NULL,
                    created_count INTEGER NOT NULL DEFAULT 0,
                    changed_count INTEGER NOT NULL DEFAULT 0,
                     failed_count INTEGER NOT NULL DEFAULT 0,
                     message TEXT
                 );
                 CREATE TABLE IF NOT EXISTS notice_analysis (
                     notice_key TEXT PRIMARY KEY,
                     categories TEXT NOT NULL,
                     primary_category TEXT NOT NULL,
                     confidence REAL NOT NULL DEFAULT 0,
                     keywords TEXT NOT NULL,
                     advice TEXT NOT NULL,
                     source TEXT NOT NULL,
                     updated_at TEXT NOT NULL
                 );
                 CREATE TABLE IF NOT EXISTS notice_briefs (
                     notice_key TEXT PRIMARY KEY,
                     summary TEXT NOT NULL DEFAULT '',
                     key_points TEXT NOT NULL DEFAULT '[]',
                     deadlines TEXT NOT NULL DEFAULT '[]',
                     required_materials TEXT NOT NULL DEFAULT '[]',
                     audience TEXT NOT NULL DEFAULT '[]',
                     evidence TEXT NOT NULL DEFAULT '[]',
                     status TEXT NOT NULL DEFAULT 'partial',
                     confidence REAL NOT NULL DEFAULT 0,
                     source TEXT NOT NULL DEFAULT 'agent:unknown',
                     model TEXT NOT NULL DEFAULT '',
                     updated_at TEXT NOT NULL
                 );
                 CREATE TABLE IF NOT EXISTS attachment_extractions (
                     attachment_id INTEGER PRIMARY KEY,
                     notice_key TEXT NOT NULL,
                     text_path TEXT,
                     text_hash TEXT,
                     character_count INTEGER NOT NULL DEFAULT 0,
                     status TEXT NOT NULL,
                     extractor TEXT NOT NULL,
                     error_message TEXT,
                     updated_at TEXT NOT NULL
                 );
                 """
             )

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, key: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM notices WHERE notice_key = ?", (key,)).fetchone()

    def get_notice_detail(self, key: str) -> dict[str, Any] | None:
        row = self.get(key)
        if row is None:
            return None
        with self.connect() as conn:
            attachments = conn.execute(
                "SELECT id, name, url, local_path, status FROM attachments WHERE notice_key=? ORDER BY id",
                (key,),
            ).fetchall()
            analysis_row = conn.execute(
                "SELECT categories, primary_category, confidence, keywords, advice, source, updated_at "
                "FROM notice_analysis WHERE notice_key=?",
                (key,),
            ).fetchone()
            brief_row = conn.execute(
                "SELECT summary, key_points, deadlines, required_materials, audience, evidence, "
                "status, confidence, source, model, updated_at FROM notice_briefs WHERE notice_key=?",
                (key,),
            ).fetchone()
            extraction_rows = conn.execute(
                "SELECT attachment_id, text_path, text_hash, character_count, status, extractor, error_message, updated_at "
                "FROM attachment_extractions WHERE notice_key=?",
                (key,),
            ).fetchall()
        data = dict(row)
        extraction_map = {item["attachment_id"]: dict(item) for item in extraction_rows}
        data["attachments"] = []
        for item in attachments:
            attachment = dict(item)
            attachment["extraction"] = extraction_map.get(item["id"])
            data["attachments"].append(attachment)
        data["analysis"] = None
        data["brief"] = None
        if analysis_row:
            data["analysis"] = {
                "categories": json.loads(analysis_row["categories"] or "[]"),
                "primary_category": analysis_row["primary_category"],
                "confidence": analysis_row["confidence"],
                "keywords": json.loads(analysis_row["keywords"] or "[]"),
                "advice": analysis_row["advice"],
                "source": analysis_row["source"],
                "updated_at": analysis_row["updated_at"],
            }
        if brief_row:
            data["brief"] = {
                "summary": brief_row["summary"],
                "key_points": json.loads(brief_row["key_points"] or "[]"),
                "deadlines": json.loads(brief_row["deadlines"] or "[]"),
                "required_materials": json.loads(brief_row["required_materials"] or "[]"),
                "audience": json.loads(brief_row["audience"] or "[]"),
                "evidence": json.loads(brief_row["evidence"] or "[]"),
                "status": brief_row["status"],
                "confidence": brief_row["confidence"],
                "source": brief_row["source"],
                "model": brief_row["model"],
                "updated_at": brief_row["updated_at"],
            }
        return data

    def touch(self, key: str, item: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE notices SET title=?, published_text=?, published_at=?, department=?, last_seen_at=? WHERE notice_key=?",
                (item.get("title", ""), item.get("date", ""), normalize_date(item.get("date", "")), item.get("department", ""), now_text(), key),
            )

    def upsert(self, item: dict[str, Any]) -> tuple[bool, bool]:
        key = notice_key(item)
        content = str(item.get("content") or "").strip()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        timestamp = now_text()
        with self.connect() as conn:
            old = conn.execute("SELECT content_hash FROM notices WHERE notice_key=?", (key,)).fetchone()
            created = old is None
            changed = created or old["content_hash"] != content_hash
            if created:
                conn.execute(
                    """INSERT INTO notices
                    (notice_key, external_id, notice_type, title, department, published_text, published_at, url, content,
                     content_hash, first_seen_at, last_seen_at, is_unread, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (key, str(item.get("id") or ""), item.get("type", "notice"), item.get("title", "未命名通知"),
                     item.get("department", ""), item.get("published_text", item.get("date", "")),
                     item.get("published_at", normalize_date(item.get("date", ""))), item.get("url", ""), content,
                     content_hash, timestamp, timestamp, timestamp),
                )
            else:
                conn.execute(
                    """UPDATE notices SET title=?, department=?, published_text=?, published_at=?, url=?, content=?,
                    content_hash=?, last_seen_at=?, is_unread=?, updated_at=? WHERE notice_key=?""",
                    (item.get("title", "未命名通知"), item.get("department", ""),
                     item.get("published_text", item.get("date", "")), item.get("published_at", normalize_date(item.get("date", ""))),
                     item.get("url", ""), content, content_hash, timestamp, 1 if changed else 0, timestamp, key),
                )
        return created, (changed and not created)

    def upsert_attachment(self, key: str, attachment: dict[str, Any]) -> None:
        timestamp = now_text()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO attachments (notice_key, name, url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(notice_key, url) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at""",
                (key, attachment.get("name", "附件"), attachment.get("url", ""), timestamp, timestamp),
            )

    def attachment(self, key: str, url: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM attachments WHERE notice_key=? AND url=?", (key, url)).fetchone()

    def update_attachment(self, key: str, url: str, local_path: str | None, content_hash: str | None, status: str, error: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE attachments SET local_path=?, content_hash=?, status=?, error_message=?, updated_at=?
                WHERE notice_key=? AND url=?""",
                (local_path, content_hash, status, error[:1000], now_text(), key, url),
            )

    def save_analysis(self, key: str, analysis: dict[str, Any]) -> None:
        categories = [str(item) for item in analysis.get("categories", [])]
        keywords = [str(item) for item in analysis.get("keywords", [])]
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO notice_analysis
                (notice_key, categories, primary_category, confidence, keywords, advice, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(notice_key) DO UPDATE SET categories=excluded.categories,
                primary_category=excluded.primary_category, confidence=excluded.confidence,
                keywords=excluded.keywords, advice=excluded.advice, source=excluded.source,
                updated_at=excluded.updated_at""",
                (
                    key,
                    json.dumps(categories, ensure_ascii=False),
                    str(analysis.get("primary_category") or "other"),
                    float(analysis.get("confidence", 0)),
                    json.dumps(keywords, ensure_ascii=False),
                    str(analysis.get("advice") or "")[:2000],
                    str(analysis.get("source") or "rules-v1")[:100],
                    now_text(),
                ),
            )

    def save_brief(self, key: str, brief: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO notice_briefs
                (notice_key, summary, key_points, deadlines, required_materials, audience, evidence,
                 status, confidence, source, model, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(notice_key) DO UPDATE SET summary=excluded.summary,
                key_points=excluded.key_points, deadlines=excluded.deadlines,
                required_materials=excluded.required_materials, audience=excluded.audience,
                evidence=excluded.evidence, status=excluded.status, confidence=excluded.confidence,
                source=excluded.source, model=excluded.model, updated_at=excluded.updated_at""",
                (
                    key,
                    str(brief.get("summary") or "")[:3000],
                    json.dumps(brief.get("key_points", []), ensure_ascii=False),
                    json.dumps(brief.get("deadlines", []), ensure_ascii=False),
                    json.dumps(brief.get("required_materials", []), ensure_ascii=False),
                    json.dumps(brief.get("audience", []), ensure_ascii=False),
                    json.dumps(brief.get("evidence", []), ensure_ascii=False),
                    str(brief.get("status") or "partial"),
                    float(brief.get("confidence", 0)),
                    str(brief.get("source") or "agent:unknown")[:100],
                    str(brief.get("model") or "")[:100],
                    now_text(),
                ),
            )

    def downloaded_attachments(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT id, notice_key, name, local_path, status FROM attachments "
                "WHERE status='DOWNLOADED' AND local_path IS NOT NULL ORDER BY id"
            ).fetchall()

    def save_extraction(self, attachment_id: int, key: str, result: dict[str, Any], text_path: str = "") -> None:
        text = str(result.get("text") or "")
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO attachment_extractions
                (attachment_id, notice_key, text_path, text_hash, character_count, status, extractor, error_message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attachment_id) DO UPDATE SET notice_key=excluded.notice_key,
                text_path=excluded.text_path, text_hash=excluded.text_hash,
                character_count=excluded.character_count, status=excluded.status,
                extractor=excluded.extractor, error_message=excluded.error_message,
                updated_at=excluded.updated_at""",
                (
                    attachment_id,
                    key,
                    text_path,
                    text_hash,
                    len(text),
                    str(result.get("status") or "UNKNOWN"),
                    str(result.get("extractor") or "none"),
                    str(result.get("error") or "")[:1000],
                    now_text(),
                ),
            )

    def list_notices(
        self,
        unread: bool = False,
        limit: int = 100,
        offset: int = 0,
        category: str = "",
        since: str = "",
        until: str = "",
        q: str = "",
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            query = "SELECT * FROM notices"
            args: list[Any] = []
            conditions: list[str] = []
            if unread:
                conditions.append("is_unread=1")
            q = normalize_text(q)[:100]
            if q:
                like = f"%{q}%"
                conditions.append(
                    "(title LIKE ? OR department LIKE ? OR content LIKE "
                    "? OR EXISTS (SELECT 1 FROM attachments a "
                    "WHERE a.notice_key=notices.notice_key AND (a.name LIKE ? OR a.url LIKE ?)))"
                )
                args.extend([like, like, like, like, like])
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY COALESCE(published_at, first_seen_at) DESC, first_seen_at DESC LIMIT ?"
            args.append(500)
            rows = conn.execute(query, args).fetchall()
            result: list[dict[str, Any]] = []
            matched = 0
            for row in rows:
                attachments = conn.execute(
                    "SELECT name, url, local_path, status FROM attachments WHERE notice_key=? ORDER BY id",
                    (row["notice_key"],),
                ).fetchall()
                data = dict(row)
                data["attachments"] = [dict(item) for item in attachments]
                analysis_row = conn.execute(
                    "SELECT categories, primary_category, confidence, keywords, advice, source, updated_at "
                    "FROM notice_analysis WHERE notice_key=?",
                    (row["notice_key"],),
                ).fetchone()
                if analysis_row:
                    data["analysis"] = {
                        "categories": json.loads(analysis_row["categories"] or "[]"),
                        "primary_category": analysis_row["primary_category"],
                        "confidence": analysis_row["confidence"],
                        "keywords": json.loads(analysis_row["keywords"] or "[]"),
                        "advice": analysis_row["advice"],
                        "source": analysis_row["source"],
                        "updated_at": analysis_row["updated_at"],
                    }
                else:
                    data["analysis"] = None
                brief_row = conn.execute(
                    "SELECT summary, status, confidence, source, model, updated_at "
                    "FROM notice_briefs WHERE notice_key=?",
                    (row["notice_key"],),
                ).fetchone()
                data["brief"] = {
                    "summary": brief_row["summary"],
                    "status": brief_row["status"],
                    "confidence": brief_row["confidence"],
                    "source": brief_row["source"],
                    "model": brief_row["model"],
                    "updated_at": brief_row["updated_at"],
                } if brief_row else None
                notice_date = str(data.get("published_at") or "")[:10]
                if since and notice_date and notice_date < since:
                    continue
                if until and notice_date and notice_date > until:
                    continue
                if category and (not data["analysis"] or category not in data["analysis"]["categories"]):
                    continue
                if matched < max(0, offset):
                    matched += 1
                    continue
                result.append(data)
                matched += 1
                if len(result) >= max(1, min(limit, 500)):
                    break
            return result

    def mark_read(self, key: str | None = None) -> None:
        with self.connect() as conn:
            if key:
                conn.execute("UPDATE notices SET is_unread=0 WHERE notice_key=?", (key,))
            else:
                conn.execute("UPDATE notices SET is_unread=0 WHERE is_unread=1")

    def start_run(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'RUNNING')", (now_text(),))
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, created: int, changed: int, failed: int, message: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, created_count=?, changed_count=?, failed_count=?, message=? WHERE id=?",
                (now_text(), status, created, changed, failed, message[:2000], run_id),
            )


class NoticeSpider:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.base_url = str(config["base_url"]).rstrip("/")
        self.sso_base = str(config["sso_base"]).rstrip("/")
        self.service_url = str(config["service_url"])
        self.session = requests.Session()
        self.session.headers.update({**HEADERS, "User-Agent": str(config["user_agent"])})
        self.logged_in = False

    def login(self) -> None:
        username, password = resolve_credentials(self.config)
        login_url = f"{self.sso_base}/login?{urlencode({'service': self.service_url})}"
        response = self.session.get(login_url, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        form = soup.select_one("form")
        if form is None:
            raise RuntimeError("SSO 登录页没有找到表单，可能已变更或要求浏览器验证码")
        data: dict[str, str] = {}
        for field in form.select("input[name]"):
            name = field.get("name", "")
            if name and field.get("type", "text").lower() not in {"submit", "button"}:
                data[name] = field.get("value", "")
        username_field = next((x for x in ("username", "user", "account") if x in data), "username")
        password_field = next((x for x in ("password", "passwd", "pwd") if x in data), "password")
        data[username_field] = username
        data[password_field] = password
        data.setdefault("execution", form.select_one("input[name=execution]").get("value", "") if form.select_one("input[name=execution]") else "_" )
        data.setdefault("_eventId", "submit")
        action = urljoin(login_url, form.get("action") or login_url)
        response = self.session.post(action, data=data, timeout=30, allow_redirects=True)
        response.raise_for_status()
        verify = self.session.get(f"{self.base_url}/uc/user/index", timeout=20, allow_redirects=True)
        if self._looks_like_login(verify):
            raise RuntimeError("学院通知系统登录失败，仍返回登录页；请检查账号、密码或验证码")
        self.logged_in = True

    @staticmethod
    def _looks_like_login(response: requests.Response) -> bool:
        if response.status_code in {401, 403}:
            return True
        if "/login" in response.url.lower():
            return True
        soup = BeautifulSoup(response.content[:500000], "html.parser")
        return soup.select_one("form input[type=password]") is not None

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        if not self.logged_in:
            self.login()
        response = self.session.get(url, timeout=kwargs.pop("timeout", 20), **kwargs)
        if self._looks_like_login(response):
            self.logged_in = False
            raise RuntimeError("登录态已失效，请重新登录或检查账号配置")
        response.raise_for_status()
        return response

    def fetch_list(self, notice_type: str, since: str = "", max_items: int | None = None) -> list[dict[str, Any]]:
        if notice_type not in LIST_PATHS:
            return []
        configured_limit = max_items if max_items is not None else self.config.get("max_items_per_type", 50)
        try:
            item_limit = max(1, min(int(configured_limit), 10000))
        except (TypeError, ValueError):
            raise ValueError("max_items_per_type 必须是正整数")
        result: list[dict[str, Any]] = []
        max_pages = 20 if since else 1
        for page in range(1, max_pages + 1):
            url = f"{self.base_url}{LIST_PATHS[notice_type]}?current={page}"
            response = self._get(url)
            soup = BeautifulSoup(response.content, "html.parser")
            page_result: list[dict[str, Any]] = []
            if notice_type == "fw":
                links = soup.select("a.layui-custom-card-li[href]") or soup.select("a[href]")
                for link in links:
                    href = link.get("href", "").strip()
                    if not href or href.startswith("#"):
                        continue
                    columns = [normalize_text(x.get_text(" ", strip=True)) for x in link.select("div[class*='layui-col']")]
                    title = columns[1] if len(columns) > 1 else normalize_text(link.get_text(" ", strip=True))
                    if not title:
                        continue
                    page_result.append({
                        "id": "",
                        "type": notice_type,
                        "title": title,
                        "department": "",
                        "date": columns[2] if len(columns) > 2 else "",
                        "url": normalize_url(urljoin(self.base_url, href)),
                        "doc_number": columns[0] if columns else "",
                    })
            else:
                for link in soup.select("a[href]"):
                    href = link.get("href", "").strip()
                    match = NOTICE_ID_PATTERN.search(href)
                    if not match:
                        continue
                    columns = [normalize_text(x.get_text(" ", strip=True)) for x in link.select("div[class*='layui-col']")]
                    if notice_type == "xngs":
                        title = normalize_text(link.get("title") or (columns[0] if columns else link.get_text(" ", strip=True)))
                        date = columns[1] if len(columns) > 1 else ""
                        department = ""
                    else:
                        title = normalize_text(link.get("title") or (columns[0] if columns else link.get_text(" ", strip=True)))
                        date = columns[2] if len(columns) > 2 else ""
                        department = columns[1] if len(columns) > 1 else ""
                    page_result.append({
                        "id": match.group(1),
                        "type": notice_type,
                        "title": title,
                        "department": department,
                        "date": date,
                        "url": normalize_url(urljoin(self.base_url, href)),
                    })
            if not page_result:
                break
            result.extend(page_result)
            if len(result) >= item_limit:
                break
            if not since:
                break
            page_dates = [normalize_date(item.get("date"))[:10] for item in page_result]
            known_dates = [date for date in page_dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)]
            if known_dates and min(known_dates) < since:
                break
        unique: dict[str, dict[str, Any]] = {}
        for item in result:
            unique[notice_key(item)] = item
        return list(unique.values())[:item_limit]

    def fetch_detail(self, item: dict[str, Any]) -> dict[str, Any]:
        response = self._get(item["url"])
        return parse_detail_html(response.text, item["url"], item)

    def download(self, key: str, attachment: dict[str, Any], store: NoticeStore) -> None:
        store.upsert_attachment(key, attachment)
        old = store.attachment(key, attachment["url"])
        if old and old["status"] == "DOWNLOADED" and old["local_path"] and Path(old["local_path"]).exists():
            return
        try:
            response = self._get(attachment["url"], stream=True, timeout=60)
            max_bytes = int(self.config.get("max_attachment_bytes", 52428800))
            content_length = int(response.headers.get("Content-Length", "0") or 0)
            if content_length > max_bytes:
                raise RuntimeError(f"附件超过大小上限: {content_length} bytes")
            filename = safe_filename(attachment.get("name") or Path(urlparse(attachment["url"]).path).name)
            target = ATTACHMENT_DIR / f"{safe_filename(key, 'notice')}_{filename}"
            temporary = target.with_suffix(target.suffix + ".part")
            digest = hashlib.sha256()
            size = 0
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise RuntimeError("附件超过大小上限")
                    digest.update(chunk)
                    handle.write(chunk)
            temporary.replace(target)
            store.update_attachment(key, attachment["url"], str(target), digest.hexdigest(), "DOWNLOADED")
        except Exception as exc:
            try:
                temporary.unlink(missing_ok=True)
            except UnboundLocalError:
                pass
            store.update_attachment(key, attachment["url"], None, None, "FAILED", str(exc))


UPDATE_LOCK = threading.Lock()


def analyze_existing_notices() -> dict[str, Any]:
    store = NoticeStore()
    rows = store.list_notices(limit=500)
    for row in rows:
        store.save_analysis(row["notice_key"], analyze_notice(row))
        refresh_notice_artifacts(store, row["notice_key"])
    return {"status": "SUCCESS", "analyzed": len(rows), "artifacts": len(rows), "finished_at": now_text()}


def refresh_notice_artifacts(store: NoticeStore, key: str) -> None:
    detail = store.get_notice_detail(key)
    if detail is None:
        return
    try:
        write_notice_artifacts(detail)
    except Exception as exc:
        logging.getLogger("notice_viewer").warning("写入原文/纪要文件失败 key=%s: %s", key, exc)


def export_existing_artifacts() -> dict[str, Any]:
    store = NoticeStore()
    rows = store.list_notices(limit=500)
    for row in rows:
        refresh_notice_artifacts(store, row["notice_key"])
    return {"status": "SUCCESS", "exported": len(rows), "finished_at": now_text()}


def extract_downloaded_attachments() -> dict[str, Any]:
    store = NoticeStore()
    EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for row in store.downloaded_attachments():
        result = extract_text(Path(row["local_path"]))
        text_path = ""
        text = str(result.get("text") or "")
        if text:
            target = EXTRACTED_DIR / f"{row['id']}.txt"
            target.write_text(text + "\n", encoding="utf-8")
            text_path = str(target)
        store.save_extraction(row["id"], row["notice_key"], result, text_path)
        status = str(result.get("status") or "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    return {"status": "SUCCESS", "processed": sum(counts.values()), "by_status": counts, "finished_at": now_text()}


def perform_update(
    config: dict[str, Any],
    force: bool = False,
    notify: bool = False,
    since: str | None = None,
    until: str | None = None,
    max_items_per_type: int | None = None,
) -> dict[str, Any]:
    store = NoticeStore()
    run_id = store.start_run()
    created_count = 0
    changed_count = 0
    unchanged_count = 0
    failed_count = 0
    new_items: list[dict[str, Any]] = []
    try:
        since = normalize_date_bound(since or config.get("collection_start_date"), "since")
        until = normalize_date_bound(until, "until")
        if since and until and since > until:
            raise ValueError("since 不能晚于 until")
        spider = NoticeSpider(config)
        spider.login()
        items: list[dict[str, Any]] = []
        if max_items_per_type is not None and max_items_per_type < 1:
            raise ValueError("max_items_per_type 必须是正整数")
        for notice_type in config.get("notice_types", list(NOTICE_TYPES)):
            if notice_type not in NOTICE_TYPES:
                continue
            items.extend(spider.fetch_list(notice_type, since=since, max_items=max_items_per_type))
            time.sleep(max(0.0, float(config.get("request_interval_seconds", 0.5))))
        listed_before_filter = len(items)
        unknown_date_count = sum(not re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", str(item.get("date") or "")) for item in items)
        items = [item for item in items if in_date_range(item.get("date"), since, until)]
        for item in items:
            key = notice_key(item)
            old = store.get(key)
            should_fetch = force or old is None or old["title"] != item.get("title", "") or old["published_text"] != item.get("date", "")
            try:
                detail = spider.fetch_detail(item) if should_fetch and item["type"] != "fw" else {
                    **item,
                    "published_text": item.get("date", ""),
                    "published_at": normalize_date(item.get("date", "")),
                    "content": f"标题：{item.get('title', '未命名通知')}\n来源：{item.get('url', '')}",
                    "attachments": [{"name": safe_filename(item.get("title", "学校发文")) + ".pdf", "url": item["url"]}],
                }
                if not should_fetch and old is not None:
                    store.touch(key, item)
                    existing = dict(old)
                    existing.update(item)
                    store.save_analysis(key, analyze_notice(existing))
                    refresh_notice_artifacts(store, key)
                    unchanged_count += 1
                    continue
                was_created, was_changed = store.upsert(detail)
                store.save_analysis(key, analyze_notice(detail))
                created_count += int(was_created)
                changed_count += int(was_changed)
                unchanged_count += int(not was_created and not was_changed)
                if was_created or was_changed:
                    new_items.append(detail)
                if config.get("download_attachments", True):
                    for attachment in detail.get("attachments", []):
                        spider.download(key, attachment, store)
                        time.sleep(max(0.0, float(config.get("request_interval_seconds", 0.5))))
                refresh_notice_artifacts(store, key)
            except Exception as exc:
                failed_count += 1
                logging.getLogger("notice_viewer").warning("处理通知失败 key=%s: %s", key, exc)

        result = {
            "status": "SUCCESS" if failed_count == 0 else "PARTIAL",
            "run_id": run_id,
            "finished_at": now_text(),
            "listed": len(items),
            "scope": {
                "since": since,
                "until": until,
                "max_items_per_type": max_items_per_type or config.get("max_items_per_type", 50),
                "listed_before_filter": listed_before_filter,
                "unknown_date_count": unknown_date_count,
            },
            "created": created_count,
            "changed": changed_count,
            "unchanged": unchanged_count,
            "failed": failed_count,
            "new_notice_keys": [notice_key(item) for item in new_items],
        }
        store.finish_run(run_id, result["status"], created_count, changed_count, failed_count)
        (DATA_DIR / "last_update.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if notify and new_items:
            send_notifications(config, new_items)
        return result
    except Exception as exc:
        store.finish_run(run_id, "FAILED", created_count, changed_count, failed_count, str(exc))
        result = {"status": "FAILED", "run_id": run_id, "finished_at": now_text(), "error": str(exc)}
        (DATA_DIR / "last_update.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


def send_notifications(config: dict[str, Any], items: list[dict[str, Any]]) -> None:
    if not config.get("notifications", {}).get("enabled", True):
        return
    summary = "\n".join(f"- {item.get('title', '未命名通知')}" for item in items[:5])
    if len(items) > 5:
        summary += f"\n- 还有 {len(items) - 5} 条"
    print(f"[通知提醒] 新增 {len(items)} 条学院通知\n{summary}", file=sys.stderr)
    if not config.get("notifications", {}).get("desktop", False) or os.name != "nt":
        return
    title = f"学院通知：新增 {len(items)} 条"
    message = "；".join(item.get("title", "未命名通知") for item in items[:3])
    title_ps = title.replace("'", "''")
    message_ps = message.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
        "$n=New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
        f"$n.ShowBalloonTip(7000, '{title_ps}', '{message_ps}', [System.Windows.Forms.ToolTipIcon]::Info); "
        "Start-Sleep -Seconds 8; $n.Dispose()"
    )
    subprocess.Popen(["powershell.exe", "-NoProfile", "-STA", "-Command", script], creationflags=0x08000000)


class UpdateManager:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.state: dict[str, Any] = {"running": False, "last": None, "error": None}
        self.lock = threading.Lock()

    def start(
        self,
        force: bool = False,
        notify: bool = False,
        since: str | None = None,
        until: str | None = None,
        max_items_per_type: int | None = None,
    ) -> bool:
        with self.lock:
            if self.state["running"] or not UPDATE_LOCK.acquire(blocking=False):
                return False
            self.state.update({"running": True, "started_at": now_text(), "error": None})
        def worker() -> None:
            try:
                result = perform_update(
                    self.config,
                    force=force,
                    notify=notify,
                    since=since,
                    until=until,
                    max_items_per_type=max_items_per_type,
                )
                with self.lock:
                    self.state["last"] = result
                    self.state["error"] = result.get("error")
            finally:
                with self.lock:
                    self.state["running"] = False
                UPDATE_LOCK.release()
        threading.Thread(target=worker, name="notice-update", daemon=True).start()
        return True

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.state)


def render_home(
    store: NoticeStore,
    manager: UpdateManager,
    q: str = "",
    since: str = "",
    until: str = "",
    category: str = "",
    limit: int = 20,
    page: int = 1,
) -> str:
    limit = max(1, min(int(limit), 200))
    page = max(1, int(page))
    rows = store.list_notices(limit=limit, offset=(page - 1) * limit, category=category, since=since, until=until, q=q)
    unread = sum(1 for row in rows if row["is_unread"])
    category_counts: dict[str, int] = {}
    cards: list[str] = []
    for row in rows:
        notice_key_value = str(row["notice_key"])
        notice_key_attr = html.escape(notice_key_value, quote=True)
        attachment_count = len(row["attachments"])
        attachment_summary = f"附件：{attachment_count} 个" if attachment_count else "无附件"
        analysis = row.get("analysis") or {}
        categories = [item for item in analysis.get("categories", []) if item in CATEGORY_LABELS]
        for matched_category in categories:
            category_counts[matched_category] = category_counts.get(matched_category, 0) + 1
        category_data = ",".join(categories)
        badges = "".join(f"<span class='badge'>{html.escape(CATEGORY_LABELS[item])}</span>" for item in categories)
        advice = html.escape(str(analysis.get("advice") or "暂无建议"))
        source = html.escape(str(analysis.get("source") or "未分析"))
        brief = row.get("brief") or {}
        brief_preview = ""
        if brief.get("summary"):
            brief_preview = (
                f"<p class='brief brief-preview'><strong>Agent纪要：</strong>{html.escape(str(brief['summary']))} "
                f"<small>最近解析：{html.escape(str(brief.get('updated_at') or '未记录'))}</small></p>"
            )
        notice_type = html.escape(NOTICE_TYPES.get(row["notice_type"], row["notice_type"]))
        excerpt = re.sub(r"\s+", " ", str(row["content"] or "")).strip()
        if len(excerpt) > 180:
            excerpt = excerpt[:180].rstrip() + "…"
        cards.append(
            f"<article class='card {'unread' if row['is_unread'] else ''}' data-categories='{html.escape(category_data)}' "
            f"data-key='{notice_key_attr}' onclick=\"openDetail('{notice_key_attr}')\" onkeydown=\"(event.key==='Enter'||event.key===' ')&&openDetail('{notice_key_attr}')\" tabindex='0' role='button'>"
            f"<div class='meta'>{notice_type} · {html.escape(row['published_text'] or row['first_seen_at'])} · {html.escape(row['department'] or '部门未标注')}</div>"
            f"<h2><a href='{html.escape(row['url'])}' target='_blank' rel='noreferrer' onclick='event.stopPropagation()'>{html.escape(row['title'])}</a></h2>"
            f"<div class='badges'>{badges or '<span class=\'badge muted\'>未分类</span>'}</div>"
            f"<p class='excerpt'>{html.escape(excerpt)}</p>"
            f"<p class='advice'><strong>智能建议：</strong>{advice} <small>来源：{source}</small></p>"
            f"{brief_preview}"
            f"<div class='card-actions'><span class='attachment-summary'>{attachment_summary}</span>"
            f"<button type='button' onclick=\"event.stopPropagation();openDetail('{notice_key_attr}')\">查看详情</button>"
            f"<button type='button' onclick=\"event.stopPropagation();markRead('{notice_key_attr}')\">标为已读</button></div>"
            "</article>"
        )
    def page_url(target_page: int) -> str:
        params = {"q": q, "since": since, "until": until, "category": category, "limit": str(limit), "page": str(target_page)}
        return "/?" + urlencode({key: value for key, value in params.items() if value})

    pagination_parts = [f"<span class='page-current'>第 {page} 页</span>"]
    if page > 1:
        pagination_parts.insert(0, f"<a href='{html.escape(page_url(page - 1), quote=True)}'>上一页</a>")
    else:
        pagination_parts.insert(0, "<span class='page-disabled'>上一页</span>")
    if len(rows) >= limit:
        pagination_parts.append(f"<a href='{html.escape(page_url(page + 1), quote=True)}'>下一页</a>")
    else:
        pagination_parts.append("<span class='page-disabled'>下一页</span>")
    cards.append("<nav class='pagination' aria-label='通知分页'>" + "".join(pagination_parts) + "</nav>")
    selected_category = category if category in CATEGORY_LABELS else ""
    selected_category_suffix = " selected" if selected_category else ""
    category_options = "".join(
        f"<option value='{html.escape(option_category)}'{selected_category_suffix if option_category == selected_category else ''}>{html.escape(CATEGORY_LABELS[option_category])}（{category_counts.get(option_category, 0)}）</option>"
        for option_category in CATEGORY_LABELS
    )
    query_value = html.escape(q, quote=True)
    since_value = html.escape(since, quote=True)
    until_value = html.escape(until, quote=True)
    limit_options = "".join(
        f"<option value='{value}'{' selected' if limit == value else ''}>{value} 条</option>"
        for value in (20, 50, 100, 200)
    )
    status = manager.snapshot()
    credential_info = credential_status(manager.config)
    if credential_info.get("status") == "READY":
        account_state = f"账号已配置：{credential_info.get('username', '')}"
    else:
        account_state = "账号未配置"
    return """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'><title>学院通知查看器</title>
    <style>body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;background:#f5f7fb;color:#202938;margin:0}.wrap{max-width:1100px;margin:auto;padding:24px}.toolbar,.filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.toolbar button,.filters button{padding:8px 14px;border:0;border-radius:8px;background:#2563eb;color:white;cursor:pointer}.toolbar select,.filters select,.filters input{padding:8px;border:1px solid #cbd5e1;border-radius:8px;background:white}.filters{padding:14px;background:#eaf0f8;border-radius:10px}.filters input[type=text]{min-width:260px}.filters a{color:#2563eb;text-decoration:none}.hint{color:#64748b}.account-state{color:#475569;font-size:13px}.card{background:white;border-radius:12px;padding:18px;margin:16px 0;box-shadow:0 2px 12px #10204012;cursor:pointer}.card:focus{outline:3px solid #93c5fd}.card.unread{border-left:5px solid #2563eb}.meta{color:#64748b;font-size:13px}.card h2{font-size:20px;margin-bottom:8px}.card a{color:#172554;text-decoration:none}.excerpt{color:#475569;line-height:1.55;margin:8px 0}.card-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}.attachment-summary{color:#64748b;font-size:13px;margin-right:auto}.card button{border:1px solid #cbd5e1;background:white;border-radius:6px;padding:5px 10px;cursor:pointer}.badges,.detail-badges{display:flex;gap:6px;flex-wrap:wrap}.badge{display:inline-block;padding:3px 8px;border-radius:999px;background:#dbeafe;color:#1e40af;font-size:12px}.badge.muted{background:#e2e8f0;color:#475569}.advice{padding:10px 12px;background:#f0fdf4;border-left:3px solid #22c55e;line-height:1.6}.advice small{color:#64748b}.brief{padding:12px;background:#eff6ff;border-left:3px solid #3b82f6;line-height:1.6}.brief h3,.brief h4{margin:6px 0}.brief ul{margin-top:4px}.brief small{color:#64748b}.modal{display:none;position:fixed;inset:0;background:#0f172a99;z-index:10;align-items:center;justify-content:center;padding:16px}.modal.open{display:flex}.modal-card{background:white;border-radius:14px;width:min(900px,calc(100vw - 32px));max-height:90vh;overflow:auto;padding:22px;box-shadow:0 20px 60px #02061755}.config-card{width:min(560px,calc(100vw - 32px))}.config-card label{display:block;margin:12px 0 6px}.config-card input[type=text],.config-card input[type=password]{box-sizing:border-box;width:100%%;padding:9px;border:1px solid #cbd5e1;border-radius:8px}.config-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.modal-head{display:flex;gap:16px;align-items:flex-start;justify-content:space-between}.modal-head h2{margin:0;font-size:22px;line-height:1.4}.modal-close{border:0;background:#e2e8f0;border-radius:50%%;width:32px;height:32px;font-size:20px;cursor:pointer}.detail-meta{color:#64748b;font-size:13px;margin:10px 0}.detail-content{white-space:pre-wrap;line-height:1.75;background:#f8fafc;border-radius:8px;padding:16px;max-height:55vh;overflow:auto}.detail-attachments{padding-left:20px}.modal-source{display:inline-block;margin:12px 0;color:#2563eb}.modal-open{overflow:hidden}</style></head>
    <body><main class='wrap'><h1>学院通知查看器</h1>
    <div class='toolbar'><button onclick='updateNow()'>立即更新</button><button onclick='readAll()'>全部标为已读</button><button onclick='openConfig()'>账号配置</button>
    <span id='accountState' class='account-state'>%s</span><span id='status'>匹配：%d 条；未读：%d；更新状态：%s</span></div>
    <form class='filters' method='get' action='/'><input type='text' name='q' value='%s' placeholder='关键词/模糊搜索：标题、正文、部门、附件'><label>从 <input type='date' name='since' value='%s'></label><label>到 <input type='date' name='until' value='%s'></label><label>分类：<select name='category'><option value=''>全部</option>%s</select></label><label>显示：<select name='limit'>%s</select></label><button type='submit'>筛选</button><a href='/'>清除</a></form>
    <p class='hint'>本地运行 · 仅绑定 127.0.0.1 · 数据保存在 SQLite · 智能建议当前为本地规则分析，可由 Agent 覆盖</p>
    <section id='list'>%s</section></main>
    <div id='detailModal' class='modal' role='dialog' aria-modal='true' aria-labelledby='detailTitle' onclick='if(event.target===this)closeDetail()'>
      <div class='modal-card'><div class='modal-head'><h2 id='detailTitle'>通知详情</h2><button class='modal-close' type='button' aria-label='关闭' onclick='closeDetail()'>×</button></div><div id='detailBody'>正在加载…</div></div>
    </div><script>
    async function openConfig(){const modal=document.querySelector('#configModal');modal.classList.add('open');document.body.classList.add('modal-open');const state=document.querySelector('#configState');state.textContent='正在读取配置状态…';try{const response=await fetch('/api/config/status');const data=await response.json();document.querySelector('#configUsername').value='';document.querySelector('#configPassword').value='';document.querySelector('#configRemember').checked=Boolean(data.persisted);state.textContent=data.status==='READY'?'当前已配置账号：'+(data.username||'已脱敏'):'当前尚未配置账号';}catch(error){state.textContent='配置状态读取失败：'+error.message}}
    async function saveConfig(event){event.preventDefault();const state=document.querySelector('#configState');const username=document.querySelector('#configUsername').value.trim();const password=document.querySelector('#configPassword').value;const remember=document.querySelector('#configRemember').checked;if(!username||!password){state.textContent='请输入账号和密码';return}state.textContent='正在保存…';const response=await fetch('/api/config/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username,password:password,remember:remember})});const data=await response.json();if(!response.ok){state.textContent=data.error||'保存失败';return}state.textContent=remember?'已保存到本机忽略文件。':'已保存到当前运行实例，重启后需要重新配置。';document.querySelector('#configPassword').value='';document.querySelector('#accountState').textContent='账号已配置：'+(data.status.username||'已脱敏');setTimeout(closeConfig,700)}
    async function clearConfig(){const state=document.querySelector('#configState');state.textContent='正在清除本机保存的账号…';const response=await fetch('/api/config/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({clear:true})});const data=await response.json();state.textContent=response.ok?'本机保存的账号已清除。':(data.error||'清除失败');if(response.ok){document.querySelector('#accountState').textContent='账号未配置';document.querySelector('#configRemember').checked=false}}
    function closeConfig(){document.querySelector('#configModal').classList.remove('open');document.body.classList.remove('modal-open')}
    document.addEventListener('DOMContentLoaded',function(){const style=document.createElement('style');style.textContent='.config-actions button{padding:8px 14px;border:1px solid #cbd5e1;border-radius:8px;background:white;color:#334155;cursor:pointer}.config-actions button[type=submit]{background:#2563eb;border-color:#2563eb;color:white}.config-actions button:hover{filter:brightness(.97)}.pagination{display:flex;justify-content:center;align-items:center;gap:12px;margin:24px 0}.pagination a,.pagination span{padding:7px 12px;border-radius:8px;text-decoration:none}.pagination a{background:white;border:1px solid #cbd5e1;color:#2563eb}.pagination .page-current{color:#475569}.pagination .page-disabled{color:#94a3b8;background:#e2e8f0}.analysis-meta{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0;padding:10px 12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;color:#475569;font-size:13px}.analysis-meta strong{color:#1e3a8a}';document.head.appendChild(style)})
    async function updateNow(){document.querySelector('#status').textContent='更新任务已提交，请稍候刷新';await fetch('/api/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({notify:true})});setTimeout(()=>location.reload(),1500)}
    async function markRead(k){await fetch('/api/notices/'+encodeURIComponent(k)+'/read',{method:'POST'});location.reload()}
    async function readAll(){await fetch('/api/notices/read-all',{method:'POST'});location.reload()}
    function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,function(ch){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]})}
    function renderBrief(brief){if(!brief)return '';const points=(brief.key_points||[]).map(x=>'<li>'+escapeHtml(x)+'</li>').join('');const deadlines=(brief.deadlines||[]).map(x=>'<li>'+escapeHtml(x.date||'待确认')+'：'+escapeHtml(x.event||'待确认')+' <small>证据：'+escapeHtml(x.evidence||'')+'</small></li>').join('');const materials=(brief.required_materials||[]).map(x=>'<li>'+escapeHtml(x)+'</li>').join('');return '<section class="brief"><h3>纪要</h3><p>'+escapeHtml(brief.summary||'')+'</p>'+(points?'<h4>重点</h4><ul>'+points+'</ul>':'')+(deadlines?'<h4>时间节点</h4><ul>'+deadlines+'</ul>':'')+(materials?'<h4>所需材料</h4><ul>'+materials+'</ul>':'')+'<small>来源：'+escapeHtml(brief.source||'')+' · 状态：'+escapeHtml(brief.status||'')+'</small></section>'}
    function renderDetail(row){const analysis=row.analysis||{};const labels={competition:'比赛竞赛',patent:'专利知识产权',employment:'就业招聘',scholarship:'奖助学金',exam:'考试课程',activity:'校园活动',safety:'安全与管理',personnel:'人事任免',publicity:'公示',other:'其他'};const badges=(analysis.categories||[]).map(x=>'<span class="badge">'+escapeHtml(labels[x]||x)+'</span>').join('');const attachments=(row.attachments||[]).map(a=>'<li><a href="'+escapeHtml(a.url)+'" target="_blank" rel="noreferrer">'+escapeHtml(a.name)+'</a> <small>'+escapeHtml(a.status||'')+'</small></li>').join('');return '<div class="detail-meta">'+escapeHtml(row.notice_type||'')+' · '+escapeHtml(row.published_text||'')+' · '+escapeHtml(row.department||'部门未标注')+'</div><div class="detail-badges">'+badges+'</div><p class="advice"><strong>智能建议：</strong>'+escapeHtml(analysis.advice||'暂无建议')+' <small>来源：'+escapeHtml(analysis.source||'未分析')+'</small></p>'+renderBrief(row.brief)+'<pre class="detail-content">'+escapeHtml(row.content||'')+'</pre>'+(attachments?'<h3>附件</h3><ul class="detail-attachments">'+attachments+'</ul>':'<p class="hint">暂无附件</p>')+'<a class="modal-source" href="'+escapeHtml(row.url||'')+'" target="_blank" rel="noreferrer">打开源站原文</a>'}
    async function openDetail(k){const modal=document.querySelector('#detailModal');const body=document.querySelector('#detailBody');modal.classList.add('open');document.body.classList.add('modal-open');body.innerHTML='<p>正在加载详情…</p>';try{const response=await fetch('/api/notices/'+encodeURIComponent(k));const data=await response.json();if(!response.ok)throw new Error(data.error||'加载失败');document.querySelector('#detailTitle').textContent=data.title||'通知详情';body.innerHTML=renderDetail(data)}catch(error){body.innerHTML='<p class="hint">详情加载失败：'+escapeHtml(error.message)+'</p>'}}
    function closeDetail(){document.querySelector('#detailModal').classList.remove('open');document.body.classList.remove('modal-open')}
    document.addEventListener('keydown',function(event){if(event.key==='Escape')closeDetail()})
    const originalRenderBrief=renderBrief;function renderAgentBrief(brief){if(!brief)return '<section class="brief"><h3>Agent纪要</h3><p>尚未生成 Agent 纪要；当前内容来自本地规则分析。</p><small>最近解析：暂无</small></section>';const base=originalRenderBrief(brief).replace('<h3>纪要</h3>','<h3>Agent纪要</h3>');const audience=(brief.audience||[]).map(x=>escapeHtml(x)).join('、');const evidence=(brief.evidence||[]).map(x=>'<li>'+escapeHtml(x)+'</li>').join('');const extra=(audience?'<p><strong>面向对象：</strong>'+audience+'</p>':'')+(evidence?'<h4>证据与待确认</h4><ul>'+evidence+'</ul>':'')+'<small>最近解析：'+escapeHtml(brief.updated_at||'未记录')+' · 模型：'+escapeHtml(brief.model||'未填写')+' · 置信度：'+escapeHtml(brief.confidence??'未记录')+'</small>';return base.replace('</section>',extra+'</section>')}renderBrief=renderAgentBrief;
    function renderAnalysisMeta(analysis){if(!analysis||!analysis.updated_at)return '';const labels={competition:'比赛竞赛',patent:'专利知识产权',employment:'就业招聘',scholarship:'奖助学金',exam:'考试课程',activity:'校园活动',safety:'安全与管理',personnel:'人事任免',publicity:'公示',other:'其他'};const keywords=(analysis.keywords||[]).map(x=>escapeHtml(x)).join('、');const categories=(analysis.categories||[]).map(x=>labels[x]||x).join('、');return '<section class="analysis-meta"><strong>规则/智能分析</strong><span>分类：'+escapeHtml(categories||'未分类')+'</span>'+(keywords?'<span>关键词：'+keywords+'</span>':'')+'<span>置信度：'+escapeHtml(analysis.confidence??'未记录')+'</span><span>最近更新：'+escapeHtml(analysis.updated_at)+'</span><span>来源：'+escapeHtml(analysis.source||'未记录')+'</span></section>'}const originalRenderDetail=renderDetail;renderDetail=function(row){let content=originalRenderDetail(row);const briefStart=content.indexOf('<section class="brief">');const adviceStart=content.indexOf('<p class="advice">');if(briefStart>=0&&adviceStart>=0&&briefStart<adviceStart){const briefEnd=content.indexOf('</section>',briefStart)+10;content=content.slice(0,briefStart)+content.slice(briefEnd,adviceStart)+content.slice(briefStart,briefEnd)+content.slice(adviceStart)}return renderAnalysisMeta(row.analysis||{})+content};
    </script><div id='configModal' class='modal' role='dialog' aria-modal='true' aria-labelledby='configTitle' onclick='if(event.target===this)closeConfig()'><div class='modal-card config-card'><div class='modal-head'><h2 id='configTitle'>账号配置</h2><button class='modal-close' type='button' aria-label='关闭' onclick='closeConfig()'>×</button></div><p class='hint'>仅用于本机通知采集。密码不会回显，也不会通过状态接口返回。</p><form onsubmit='saveConfig(event)'><label for='configUsername'>学校账号</label><input id='configUsername' type='text' autocomplete='username' maxlength='100'><label for='configPassword'>密码</label><input id='configPassword' type='password' autocomplete='current-password' maxlength='200'><label><input id='configRemember' type='checkbox'> 保存到本机（写入 data/ui_credentials.json）</label><p id='configState' class='hint'>正在读取配置状态…</p><div class='config-actions'><button type='submit'>保存配置</button><button type='button' onclick='clearConfig()'>清除本机保存</button><button type='button' onclick='closeConfig()'>取消</button></div></form></div></div></body></html>""" % (account_state, len(rows), unread, "运行中" if status["running"] else "空闲", query_value, since_value, until_value, category_options, limit_options, "\n".join(cards) or "<p>没有符合条件的通知。</p>")


class RequestHandler(BaseHTTPRequestHandler):
    manager: UpdateManager
    store: NoticeStore

    def log_message(self, format: str, *args: Any) -> None:
        logging.getLogger("notice_viewer.http").info("%s - %s", self.address_string(), format % args)

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def is_local_request(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1", "localhost"}

    def read_json_body(self, maximum: int = 20000) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(length, maximum)) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            query = parse_qs(parsed.query)
            q = query.get("q", [""])[0].strip()
            category = query.get("category", [""])[0].strip()
            since = query.get("since", [""])[0].strip()
            until = query.get("until", [""])[0].strip()
            try:
                since = normalize_date_bound(since, "since")
                until = normalize_date_bound(until, "until")
                if since and until and since > until:
                    raise ValueError("since 不能晚于 until")
                limit = max(1, min(int(query.get("limit", ["20"])[0]), 200))
                page = max(1, int(query.get("page", ["1"])[0]))
            except (TypeError, ValueError) as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            body = render_home(
                self.store,
                self.manager,
                q=q,
                since=since,
                until=until,
                category=category,
                limit=limit,
                page=page,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/status":
            self.send_json(self.manager.snapshot())
            return
        if parsed.path == "/api/config/status":
            if not self.is_local_request():
                self.send_json({"error": "账号配置只允许本机访问"}, 403)
                return
            self.send_json(credential_status(self.manager.config))
            return
        if parsed.path == "/api/notices":
            query = parse_qs(parsed.query)
            unread = query.get("unread", ["0"])[0] == "1"
            try:
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                self.send_json({"error": "limit must be an integer"}, 400)
                return
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
            except ValueError:
                self.send_json({"error": "page must be an integer"}, 400)
                return
            category = query.get("category", [""])[0].strip()
            since = query.get("since", [""])[0].strip()
            until = query.get("until", [""])[0].strip()
            q = query.get("q", [""])[0].strip()
            try:
                since = normalize_date_bound(since, "since")
                until = normalize_date_bound(until, "until")
                if since and until and since > until:
                    raise ValueError("since 不能晚于 until")
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            items = self.store.list_notices(
                        unread=unread,
                        limit=limit,
                        offset=(page - 1) * max(1, limit),
                        category=category,
                        since=since,
                        until=until,
                        q=q,
                    )
            self.send_json({"items": items, "page": page, "limit": limit, "has_next": len(items) >= max(1, limit)})
            return
        prefix = "/api/notices/"
        if parsed.path.startswith(prefix):
            key = unquote(parsed.path[len(prefix):]).strip("/")
            if key and "/" not in key:
                detail = self.store.get_notice_detail(key)
                if detail is None:
                    self.send_json({"error": "notice not found", "notice_key": key}, 404)
                else:
                    self.send_json(detail)
                return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/config/credentials":
            if not self.is_local_request():
                self.send_json({"error": "账号配置只允许本机访问"}, 403)
                return
            payload = self.read_json_body(10000)
            if payload.get("clear"):
                if UI_CREDENTIALS_PATH.exists():
                    UI_CREDENTIALS_PATH.unlink()
                self.manager.config.pop("_runtime_credentials", None)
                if self.manager.config.get("credentials_file") == str(UI_CREDENTIALS_PATH):
                    self.manager.config["credentials_file"] = ""
                self.send_json({"ok": True, "status": credential_status(self.manager.config)})
                return
            username = str(payload.get("username") or "").strip()
            password = str(payload.get("password") or "")
            if not username or not password:
                self.send_json({"error": "账号和密码不能为空"}, 400)
                return
            if len(username) > 100 or len(password) > 200:
                self.send_json({"error": "账号或密码长度超出限制"}, 400)
                return
            remember = bool(payload.get("remember"))
            self.manager.config["_runtime_credentials"] = {"username": username, "password": password}
            if remember:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                UI_CREDENTIALS_PATH.write_text(
                    json.dumps({"username": username, "password": password}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                self.manager.config["credentials_file"] = str(UI_CREDENTIALS_PATH)
            self.send_json({"ok": True, "remembered": remember, "status": credential_status(self.manager.config)})
            return
        if parsed.path == "/api/update":
            payload = self.read_json_body(10000)
            try:
                max_items_per_type = (
                    int(payload["max_items_per_type"])
                    if payload.get("max_items_per_type") is not None
                    else None
                )
            except (TypeError, ValueError):
                self.send_json({"error": "max_items_per_type must be an integer"}, 400)
                return
            started = self.manager.start(
                force=bool(payload.get("force")),
                notify=bool(payload.get("notify")),
                since=str(payload.get("since") or "") or None,
                until=str(payload.get("until") or "") or None,
                max_items_per_type=max_items_per_type,
            )
            self.send_json({"accepted": started, "status": self.manager.snapshot()}, 202 if started else 409)
            return
        if parsed.path == "/api/notices/read-all":
            self.store.mark_read()
            self.send_json({"ok": True})
            return
        prefix = "/api/notices/"
        if parsed.path.startswith(prefix) and parsed.path.endswith("/read"):
            key = unquote(parsed.path[len(prefix):-len("/read")])
            self.store.mark_read(key)
            self.send_json({"ok": True, "notice_key": key})
            return
        if parsed.path.startswith(prefix) and parsed.path.endswith("/analysis"):
            key = unquote(parsed.path[len(prefix):-len("/analysis")])
            if self.store.get(key) is None:
                self.send_json({"error": "notice not found", "notice_key": key}, 404)
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(min(length, 20000)) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                payload = {}
            analysis = normalize_agent_analysis(payload if isinstance(payload, dict) else {})
            self.store.save_analysis(key, analysis)
            refresh_notice_artifacts(self.store, key)
            self.send_json({"ok": True, "notice_key": key, "analysis": analysis})
            return
        if parsed.path.startswith(prefix) and parsed.path.endswith("/brief"):
            key = unquote(parsed.path[len(prefix):-len("/brief")])
            if self.store.get(key) is None:
                self.send_json({"error": "notice not found", "notice_key": key}, 404)
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(min(length, 30000)) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                payload = {}
            brief = normalize_agent_brief(payload if isinstance(payload, dict) else {})
            self.store.save_brief(key, brief)
            refresh_notice_artifacts(self.store, key)
            self.send_json({"ok": True, "notice_key": key, "brief": brief})
            return
        self.send_json({"error": "not found"}, 404)


def run_server(config: dict[str, Any]) -> None:
    manager = UpdateManager(config)
    store = NoticeStore()
    handler = type("ConfiguredRequestHandler", (RequestHandler,), {"manager": manager, "store": store})
    server = ThreadingHTTPServer((str(config.get("bind_host", "127.0.0.1")), int(config.get("port", 8765))), handler)
    print(f"学院通知查看器已启动: http://{config.get('bind_host', '127.0.0.1')}:{config.get('port', 8765)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="个人学院通知查看器")
    sub = parser.add_subparsers(dest="command", required=True)
    update = sub.add_parser("update", help="登录并增量更新通知")
    update.add_argument("--force", action="store_true", help="重新抓取已有通知详情")
    update.add_argument("--notify", action="store_true", help="有新增时打印提醒并按配置尝试桌面提醒")
    update.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    update.add_argument("--since", help="只处理该日期（含）之后的通知，格式 YYYY-MM-DD")
    update.add_argument("--until", help="只处理该日期（含）之前的通知，格式 YYYY-MM-DD")
    update.add_argument("--max-items", type=int, help="每个来源类型最多处理多少条")
    sub.add_parser("serve", help="启动本地查看页面和 HTTP API")
    sub.add_parser("analyze", help="对数据库中的现有通知重新生成分类和建议")
    sub.add_parser("export", help="为现有通知生成原文快照和纪要 Markdown")
    sub.add_parser("extract-attachments", help="提取已下载附件的文本，记录不支持的格式")
    sub.add_parser("check-config", help="检查账号和本地配置是否齐全，不执行登录")
    notify = sub.add_parser("notify", help="输出当前未读通知")
    notify.add_argument("--desktop", action="store_true", help="尝试 Windows 桌面提醒")
    read_all = sub.add_parser("read-all", help="把全部通知标为已读")
    args = parser.parse_args()
    config = load_config()
    if args.command == "check-config":
        result = credential_status(config)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "READY" else 1
    if args.command == "update":
        result = perform_update(
            config,
            force=args.force,
            notify=args.notify,
            since=args.since,
            until=args.until,
            max_items_per_type=args.max_items,
        )
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
        return 0 if result["status"] != "FAILED" else 1
    if args.command == "serve":
        run_server(config)
        return 0
    store = NoticeStore()
    if args.command == "analyze":
        result = analyze_existing_notices()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "export":
        print(json.dumps(export_existing_artifacts(), ensure_ascii=False))
        return 0
    if args.command == "extract-attachments":
        print(json.dumps(extract_downloaded_attachments(), ensure_ascii=False))
        return 0
    if args.command == "notify":
        items = store.list_notices(unread=True)
        print(json.dumps({"unread": len(items), "items": items}, ensure_ascii=False, indent=2))
        if args.desktop and items:
            send_notifications(config, items)
        return 0
    if args.command == "read-all":
        store.mark_read()
        print(json.dumps({"ok": True}, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
