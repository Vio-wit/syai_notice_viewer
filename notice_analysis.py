"""通知分类和行动建议。

默认使用本地规则，确保没有外部模型或 API Key 时也能工作。
其他 Agent 可以通过 HTTP API 写入 source=agent:* 的分析结果覆盖规则结果。
"""

from __future__ import annotations

import re
from typing import Any


CATEGORY_LABELS = {
    "competition": "比赛竞赛",
    "patent": "专利知识产权",
    "employment": "就业招聘",
    "scholarship": "奖助学金",
    "exam": "考试课程",
    "activity": "校园活动",
    "safety": "安全与管理",
    "personnel": "人事任免",
    "publicity": "公示",
    "other": "其他",
}

CATEGORY_RULES = {
    "competition": ("比赛竞赛", ("比赛", "竞赛", "大赛", "参赛", "赛项", "作品提交", "评分标准")),
    "patent": ("专利知识产权", ("专利", "知识产权", "发明", "实用新型", "软著", "软件著作权")),
    "employment": ("就业招聘", ("就业", "招聘", "求职", "双选会", "宣讲会", "实习", "岗位", "招聘会")),
    "scholarship": ("奖助学金", ("奖学金", "助学金", "资助", "困难生", "评奖", "评优")),
    "exam": ("考试课程", ("考试", "考核", "课程", "试卷", "成绩", "补考", "重修")),
    "activity": ("校园活动", ("活动", "讲座", "培训班", "茶话会", "志愿", "报名")),
    "safety": ("安全与管理", ("安全", "网络安全", "教学事故", "值班", "防汛", "消防")),
    "personnel": ("人事任免", ("聘任", "聘用", "免职", "任职", "职务", "退学", "转专业")),
    "publicity": ("公示", ("公示",)),
}

CATEGORY_ADVICE = {
    "competition": "确认参赛对象、报名截止时间、作品格式和提交渠道；符合条件就提前准备材料。",
    "patent": "确认成果归属、申请类型、材料清单和截止时间；涉及共同成果时先核对署名与权属。",
    "employment": "确认招聘对象、岗位要求、宣讲或双选会时间，准备简历和证件材料。",
    "scholarship": "确认评审条件、名额、证明材料和提交截止时间，提前整理成绩与获奖证明。",
    "exam": "重点确认考试时间、提交材料、课程范围和负责部门，避免错过学校统一安排。",
    "activity": "确认活动对象、报名方式、时间地点和是否需要提交作品或材料。",
    "safety": "按通知要求完成安全检查或材料报送，并留意具体时间窗口和责任部门。",
    "personnel": "如与本人或所在部门相关，重点核对名单、任免日期和后续办理要求。",
    "publicity": "核对公示对象、异议期限和反馈渠道；在期限内提出需要核实的问题。",
    "other": "先查看原文和附件，确认是否存在与本人相关的时间、材料或报名要求。",
}


def _source_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("title") or ""),
        str(item.get("content") or ""),
        str(item.get("department") or ""),
    ]
    return "\n".join(parts)


def analyze_notice(item: dict[str, Any]) -> dict[str, Any]:
    """对一条通知生成稳定、可解释的本地分类和建议。"""

    text = _source_text(item)
    matches: dict[str, list[str]] = {}
    scores: dict[str, int] = {}
    for category, (_, keywords) in CATEGORY_RULES.items():
        found = [keyword for keyword in keywords if keyword in text]
        if found:
            matches[category] = found
            scores[category] = sum(2 if len(keyword) >= 3 else 1 for keyword in found)

    if not scores:
        categories = ["other"]
        primary = "other"
        confidence = 0.2
        keywords: list[str] = []
    else:
        ordered = sorted(scores, key=lambda category: (-scores[category], list(CATEGORY_RULES).index(category)))
        categories = ordered[:3]
        primary = categories[0]
        confidence = round(min(0.98, 0.45 + scores[primary] * 0.08), 2)
        keywords = [keyword for category in categories for keyword in matches[category]]

    deadline_matches = re.findall(
        r"(?:截止|截至|报名截止|提交截止|申报截止|申请截止)[：:：]?[^。；;\n]{0,40}",
        text,
    )
    deadline_hint = "；".join(dict.fromkeys(match.strip() for match in deadline_matches))
    advice = CATEGORY_ADVICE[primary]
    if deadline_hint:
        advice += f" 文中发现时间提示：{deadline_hint}"

    return {
        "categories": categories,
        "primary_category": primary,
        "confidence": confidence,
        "keywords": keywords,
        "advice": advice,
        "source": "rules-v1",
    }


def normalize_agent_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """校验其他 Agent 写回的分析结果，避免污染看板数据。"""

    raw_categories = payload.get("categories")
    if isinstance(raw_categories, str):
        raw_categories = [raw_categories]
    if not isinstance(raw_categories, list):
        raw_categories = []
    categories = [str(item) for item in raw_categories if str(item) in CATEGORY_LABELS]
    if not categories:
        categories = ["other"]
    primary = str(payload.get("primary_category") or categories[0])
    if primary not in categories:
        primary = categories[0]
    try:
        confidence = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    keywords = payload.get("keywords", [])
    if isinstance(keywords, str):
        keywords = [keywords]
    if not isinstance(keywords, list):
        keywords = []
    advice = str(payload.get("advice") or CATEGORY_ADVICE[primary]).strip()[:2000]
    source = str(payload.get("source") or "agent:unknown").strip()[:100]
    return {
        "categories": categories[:5],
        "primary_category": primary,
        "confidence": round(confidence, 2),
        "keywords": [str(item)[:80] for item in keywords[:30]],
        "advice": advice,
        "source": source,
    }


def normalize_agent_brief(payload: dict[str, Any]) -> dict[str, Any]:
    """校验附件/通知纪要的结构化写回结果。"""

    def string_list(value: Any, limit: int = 30) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:500] for item in value[:limit] if str(item).strip()]

    deadlines: list[dict[str, str]] = []
    raw_deadlines = payload.get("deadlines", [])
    if isinstance(raw_deadlines, dict):
        raw_deadlines = [raw_deadlines]
    if isinstance(raw_deadlines, list):
        for item in raw_deadlines[:30]:
            if not isinstance(item, dict):
                continue
            deadlines.append(
                {
                    "date": str(item.get("date") or "待确认")[:80],
                    "event": str(item.get("event") or "待确认")[:300],
                    "evidence": str(item.get("evidence") or "需查看原文或附件确认")[:500],
                }
            )
    try:
        confidence = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    status = str(payload.get("status") or "partial").strip().lower()
    if status not in {"complete", "partial", "needs_ocr", "failed", "rule_baseline"}:
        status = "partial"
    return {
        "summary": str(payload.get("summary") or "").strip()[:3000],
        "key_points": string_list(payload.get("key_points")),
        "deadlines": deadlines,
        "required_materials": string_list(payload.get("required_materials")),
        "audience": string_list(payload.get("audience"), 10),
        "evidence": string_list(payload.get("evidence")),
        "status": status,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "source": str(payload.get("source") or "agent:unknown").strip()[:100],
        "model": str(payload.get("model") or "").strip()[:100],
    }
