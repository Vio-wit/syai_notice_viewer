#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""附件文本提取和通知原文/纪要文件输出。

这里不调用模型，也不执行附件中的任何程序。模型只消费提取后的文本，
并通过 API 写回结构化纪要。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


BASE_DIR = Path(__file__).resolve().parent
ORIGINAL_DIR = BASE_DIR / "data" / "originals"
SUMMARY_DIR = BASE_DIR / "data" / "summaries"
EXTRACTED_DIR = BASE_DIR / "data" / "extracted"

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"}
OOXML_SUFFIXES = {".docx", ".xlsx", ".pptx"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


def artifact_stem(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "notice"))
    return text.strip("._")[:120] or "notice"


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(data: bytes) -> str:
    root = ElementTree.fromstring(data)
    return " ".join((node.text or "").strip() for node in root.iter() if _local_name(node.tag) in {"t", "v"} and (node.text or "").strip())


def _extract_docx(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.startswith(("word/document", "word/header", "word/footer")) and name.endswith(".xml")]
        for name in sorted(names):
            root = ElementTree.fromstring(archive.read(name))
            paragraphs: list[str] = []
            current: list[str] = []
            for node in root.iter():
                local = _local_name(node.tag)
                if local == "t" and node.text:
                    current.append(node.text)
                elif local == "p" and current:
                    paragraphs.append("".join(current).strip())
                    current = []
            if current:
                paragraphs.append("".join(current).strip())
            parts.extend(item for item in paragraphs if item)
    return "\n".join(parts).strip()


def _extract_xlsx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                shared.append("".join(node.text or "" for node in item.iter() if _local_name(node.tag) == "t").strip())
        rows: list[str] = []
        for name in sorted(item for item in archive.namelist() if item.startswith("xl/worksheets/") and item.endswith(".xml")):
            root = ElementTree.fromstring(archive.read(name))
            for row in root.iter():
                if _local_name(row.tag) != "row":
                    continue
                values: list[str] = []
                for cell in row:
                    if _local_name(cell.tag) != "c":
                        continue
                    cell_type = cell.attrib.get("t")
                    value = next((node.text or "" for node in cell if _local_name(node.tag) == "v"), "")
                    if cell_type == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    elif cell_type == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter() if _local_name(node.tag) == "t")
                    values.append(value.strip())
                if any(values):
                    rows.append("\t".join(values))
    return "\n".join(rows).strip()


def _extract_pptx(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(item for item in archive.namelist() if item.startswith("ppt/slides/slide") and item.endswith(".xml")):
            root = ElementTree.fromstring(archive.read(name))
            text = " ".join(node.text or "" for node in root.iter() if _local_name(node.tag) == "t")
            if text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def extract_text(path: Path) -> dict[str, Any]:
    """提取一个附件的文本；无法提取时返回明确状态，不猜内容。"""

    suffix = path.suffix.lower()
    if not path.exists():
        return {"status": "MISSING", "extractor": "none", "text": "", "error": "文件不存在"}
    try:
        if suffix in TEXT_SUFFIXES:
            text = decode_text(path.read_bytes()).strip()
            return {"status": "TEXT_EXTRACTED" if text else "EMPTY", "extractor": "text", "text": text, "error": ""}
        if suffix == ".docx":
            text = _extract_docx(path)
            return {"status": "TEXT_EXTRACTED" if text else "EMPTY", "extractor": "ooxml-docx", "text": text, "error": ""}
        if suffix == ".xlsx":
            text = _extract_xlsx(path)
            return {"status": "TEXT_EXTRACTED" if text else "EMPTY", "extractor": "ooxml-xlsx", "text": text, "error": ""}
        if suffix == ".pptx":
            text = _extract_pptx(path)
            return {"status": "TEXT_EXTRACTED" if text else "EMPTY", "extractor": "ooxml-pptx", "text": text, "error": ""}
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return {"status": "NEEDS_PDF_DEPENDENCY", "extractor": "pypdf", "text": "", "error": "请安装 pypdf"}
            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
            status = "TEXT_EXTRACTED" if text else "NEEDS_OCR"
            return {"status": status, "extractor": "pypdf", "text": text, "error": "" if text else "PDF 没有可直接提取的文字"}
        if suffix in IMAGE_SUFFIXES:
            return {"status": "NEEDS_OCR", "extractor": "none", "text": "", "error": "图片需要 OCR"}
        if suffix in {".doc", ".xls", ".ppt"}:
            return {"status": "LEGACY_BINARY_UNSUPPORTED", "extractor": "none", "text": "", "error": "旧版 Office 二进制格式需要 LibreOffice 或专用转换器"}
        return {"status": "UNSUPPORTED", "extractor": "none", "text": "", "error": f"暂不支持 {suffix or '无扩展名'}"}
    except Exception as exc:  # 单个坏附件不能中断整批任务
        return {"status": "FAILED", "extractor": "unknown", "text": "", "error": f"{type(exc).__name__}: {exc}"}


def write_notice_original(detail: dict[str, Any]) -> Path:
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    key = artifact_stem(detail.get("notice_key", "notice"))
    attachments = detail.get("attachments") or []
    lines = [
        "# 原文快照",
        "",
        f"- 标题：{detail.get('title', '')}",
        f"- 类型：{detail.get('notice_type', '')}",
        f"- 发布时间：{detail.get('published_text', '')}",
        f"- 部门：{detail.get('department', '')}",
        f"- 来源：{detail.get('url', '')}",
        "",
        "## 正文",
        "",
        str(detail.get("content") or "").strip(),
        "",
        "## 附件索引",
        "",
    ]
    lines.extend(
        f"- {item.get('name', '附件')} | 下载：{item.get('status', 'UNKNOWN')} | 提取：{(item.get('extraction') or {}).get('status', 'NOT_PROCESSED')} | {item.get('url', '')}"
        for item in attachments
    )
    path = ORIGINAL_DIR / f"{key}.md"
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path


def write_notice_summary(detail: dict[str, Any]) -> Path:
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    key = artifact_stem(detail.get("notice_key", "notice"))
    brief = detail.get("brief") or {}
    analysis = detail.get("analysis") or {}
    points = brief.get("key_points") or []
    if not points:
        points = [f"分类：{', '.join(analysis.get('categories') or []) or 'other'}", f"建议：{analysis.get('advice') or '等待 Agent 深度解析附件'}"]
    lines = [
        "# 通知纪要",
        "",
        f"- 标题：{detail.get('title', '')}",
        f"- 来源：{detail.get('url', '')}",
        f"- 纪要来源：{brief.get('source') or analysis.get('source') or 'rules-v1'}",
        f"- 状态：{brief.get('status') or 'RULE_BASELINE'}",
        "",
        "## 一句话总结",
        "",
        str(brief.get("summary") or analysis.get("advice") or "尚未生成模型纪要；请 Agent 读取原文和附件后补充。"),
        "",
        "## 重点",
        "",
    ]
    lines.extend(f"- {item}" for item in points)
    if brief.get("deadlines"):
        lines.extend(["", "## 时间节点", ""])
        lines.extend(f"- {item.get('date', '待确认')}：{item.get('event', '待确认')}（证据：{item.get('evidence', '需查看原文')}）" for item in brief["deadlines"])
    if brief.get("required_materials"):
        lines.extend(["", "## 所需材料", ""])
        lines.extend(f"- {item}" for item in brief["required_materials"])
    path = SUMMARY_DIR / f"{key}.md"
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path


def write_notice_artifacts(detail: dict[str, Any]) -> dict[str, str]:
    return {
        "original_path": str(write_notice_original(detail)),
        "summary_path": str(write_notice_summary(detail)),
    }
