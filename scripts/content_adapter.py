"""Pulse 多平台内容适配执行层 —— /pulse adapt 的脚本端。

输入本地 Markdown 草稿（--source），输出：
  - 知乎版：结构化改写 + 适度扩写（H2/H3 层级、段落 120-180 字、保留图片引用与代码块）
  - AI 优化版：每个 H2 一个问答对，首段 130-170 字自包含答案（写给 AI 摘取，不用图）
每个生成版本都带 falsifiability check（发布后如何验证被引用）。

生成策略（混合）：
  - 规则脚手架：结构/长度/关键词/平台禁则来自 skills/visibility/references/content-format.md，
    确定、可测、无 LLM 也可跑（回退路径）。
  - LLM 改写：DeepSeek 可用时对语气/措辞做自然化改写；失败自动回退脚手架。

Phase 3 边界（诚实标注）：
  - 输出是「待发布的草稿」，不是「保证被引用的成品」；发布后 track --mine 才是裁判。
  - 草稿审计只含四个文本维度（AI 可引用性/内容质量/关键词覆盖/结构），互动维度标「未发布」。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
import search_ai
from scorer import audit_article, grade

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data" / "output"

DRAFT_WEIGHTS = {
    "AI 可引用性": 0.389,
    "内容质量 (E-E-A-T)": 0.278,
    "关键词覆盖": 0.222,
    "结构与格式": 0.111,
}

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# AI 优化版用途标注：内部参考，非直接发布物
AI_PURPOSE_NOTE = (
    "<!-- AI 优化版 · 内部参考，非直接发布物：用于校验 AI 可引用性，"
    "及未来网站/多平台分发的可引用骨架。发布请用知乎版或自行加工。 -->\n\n"
)

# 常见占位图服务（发布前必须替换为真实截图）
PLACEHOLDER_IMAGE_HOSTS = {
    "picsum.photos", "placehold.co", "via.placeholder.com", "placeholder.com",
    "dummyimage.com", "loremflickr.com", "placekitten.com", "fakeimg.pl",
}

# 泛化/空 alt 文本（检测图片可访问性缺口）
GENERIC_ALT = {"图", "图片", "截图", "效果图", "示意图", "img", "image", "pic", "screenshot"}

# query 要求覆盖的操作性话题 → 正文中任一出现即视为覆盖
QUERY_COVERAGE_TERMS = {
    "安装": ("安装", "setup", "安装步骤", "安装包"),
    "配置": ("配置", "设置", "初始化"),
    "教程": ("教程", "步骤", "入门", "guide", "tutorial"),
    "下载": ("下载", "安装包", "获取"),
    "使用": ("使用", "操作", "用法"),
}


def _round_half_up(value: float) -> int:
    return int(value + 0.5) if value >= 0 else int(value - 0.5)


def _text_without_code(text: str) -> str:
    """去掉围栏代码块（含未闭合），避免代码里的 URL/数字/关键词被误报或误计分"""
    out: list[str] = []
    in_code = False
    for line in (text or "").splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code:
            out.append(line)
    return "\n".join(out)


def _slug(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title or "untitled").strip("-")
    return slug[:max_len] or "untitled"


# --------------------------------------------------------------------------
# 草稿解析
# --------------------------------------------------------------------------


@dataclass
class DraftDoc:
    """解析后的本地草稿"""

    title: str
    topics: list[str]                      # H2 标题
    sections: list[tuple[str, list[str]]]  # (H2, 正文行)
    intro: list[str]                               # 首个 H2 之前的引言
    images: list[str]                      # 图片引用（路径或 URL）
    code_blocks: list[str]                 # 代码块原文
    raw: str
    body_text: str = ""                    # 纯正文（打分用）


def parse_markdown(text: str) -> DraftDoc:
    """解析 Markdown 草稿：标题、H2、图片、代码块、正文"""
    raw = text or ""
    lines = raw.splitlines()
    # 剥 frontmatter
    if lines and lines[0].strip() == "---":
        end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
        if end is not None:
            lines = lines[end + 1:]

    title = ""
    topics: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    images: list[str] = []
    code_blocks: list[str] = []
    current_heading = ""
    current_lines: list[str] = []
    intro: list[str] = []
    saw_heading = False
    in_code = False
    code_buffer: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        if current_heading and current_lines:
            sections.append((current_heading, current_lines))
        current_lines = []

    for line in lines:
        s = line.strip()
        if s.startswith("```"):
            if in_code:
                code = "\n".join(code_buffer)
                code_blocks.append(code)
                current_lines.append("```")
                current_lines.extend(code_buffer)
                current_lines.append("```")
                code_buffer = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_buffer.append(line)
            continue
        img = re.match(r"!\[[^\]]*\]\(([^)]+)\)", s)
        if img:
            images.append(img.group(1))
            current_lines.append(s)
            continue
        if s.startswith("# "):
            flush()
            heading = s[2:].strip()
            if not title:
                title = heading
            else:
                # 后续 H1 视为独立节标题（用户误用 H1 分节时保留内容，不归入前一 H2）
                current_heading = heading
                saw_heading = True
            continue
        if s.startswith("## "):
            flush()
            saw_heading = True
            current_heading = s[3:].strip()
            topics.append(current_heading)
            continue
        if s.startswith("### "):
            current_lines.append(s)
            continue
        if s:
            if saw_heading:
                current_lines.append(s)
            else:
                intro.append(s)
    flush()

    body_lines = intro + [ln for _, body in sections for ln in body]
    body_text = "\n".join(body_lines)
    return DraftDoc(
        title=title or "untitled",
        topics=topics,
        sections=sections,
        intro=intro,
        images=images,
        code_blocks=code_blocks,
        raw=raw,
        body_text=body_text,
    )


# --------------------------------------------------------------------------
# 草稿打分（四维 + 互动未发布）
# --------------------------------------------------------------------------


def _draft_recommendations(title: str, dims: dict[str, int]) -> list[dict]:
    """按四维得分生成带 falsifiability check 的建议（草稿版，无互动维度）"""
    recs: list[dict] = []

    def push(priority: str, dimension: str, action: str, impact: str, verify: str) -> None:
        recs.append({
            "priority": priority,
            "dimension": dimension,
            "action": action,
            "expected_impact": impact,
            "falsifiability_check": verify,
        })

    cit = dims["AI 可引用性"]
    if cit < 60:
        push(
            "P0", "AI 可引用性",
            "每个 H2 下首段补一段 130-170 字自包含答案块（结论前置 + 具体数据/案例），让 AI 可直接摘取",
            "提升被 AI 引用概率",
            "重跑 /pulse adapt --source <草稿>，AI 可引用性 ≥ 60；发布后跑 /pulse track --query <话题> --mine <文章URL>",
        )
    kw = dims["关键词覆盖"]
    if kw < 60:
        push(
            "P0", "关键词覆盖",
            f"标题/首段/H2 自然覆盖目标关键词（如「{title}」及其子话题词），不做堆砌",
            "提升搜索与 AI 检索命中",
            "重跑草稿审计，关键词覆盖 ≥ 60；发布后该话题搜索 Top 出现本文",
        )
    qual = dims["内容质量 (E-E-A-T)"]
    if qual < 60:
        push(
            "P1", "内容质量 (E-E-A-T)",
            "补充第一手经验/踩坑记录/具体版本号或数据，增强可信度",
            "+内容质量分",
            "重跑草稿审计，内容质量 ≥ 60",
        )
    struct = dims["结构与格式"]
    if struct < 60:
        push(
            "P1", "结构与格式",
            "按「是什么 → 优势 → 安装配置」的 H2/H3 层级重排，段落控制在 120-180 字",
            "+结构与可读性",
            "重跑草稿审计，结构与格式 ≥ 60",
        )
    recs.sort(key=lambda r: PRIORITY_ORDER.get(r["priority"], 9))
    return recs


def score_draft(title: str, text: str, keywords: list[str] | None = None) -> dict:
    """草稿四维打分：AI 可引用性/内容质量/关键词覆盖/结构（归一化），互动维度标未发布"""
    base = audit_article(
        title=title, content_text=_text_without_code(text), keywords=keywords
    )
    sub = base.sub_scores
    dims = {
        "AI 可引用性": sub["AI 可引用性"].score,
        "内容质量 (E-E-A-T)": sub["内容质量"].score,
        "关键词覆盖": sub["关键词覆盖"].score,
        "结构与格式": sub["结构与格式"].score,
    }
    overall = _round_half_up(
        sum(dims[name] * DRAFT_WEIGHTS[name] for name in dims)
    )
    return {
        "title": title,
        "overall": overall,
        "grade": grade(overall),
        "dimensions": dims,
        "engagement": {"status": "未发布", "note": "互动数据需发布后重跑 audit 获取"},
        "recommendations": _draft_recommendations(title, dims),
    }


def _query_keywords(query: str) -> list[str]:
    """把 query 拆成「整串 + 覆盖词元」用于关键词覆盖打分"""
    q = (query or "").strip()
    if not q:
        return []
    return [q] + [t for t in QUERY_COVERAGE_TERMS if t in q]


# --------------------------------------------------------------------------
# 素材缺口检测（发布前必须处理：占位图/待核实链接/query 覆盖缺失/图片 alt）
# --------------------------------------------------------------------------


def _links_in_text(text: str) -> list[str]:
    """提取正文中的所有 http(s) 链接，去掉尾部标点"""
    found = re.findall(r"https?://[^\s)\]>\"']+", text)
    return [u.rstrip(".,;:!?") for u in found]


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _image_alt(line: str) -> str:
    m = re.match(r"!\[([^\]]*)\]\([^)]+\)", line.strip())
    return m.group(1).strip() if m else ""


def detect_material_gaps(doc: DraftDoc, query: str | None = None) -> list[dict]:
    """识别草稿里的素材缺口，返回按严重度排序的清单（发布前处理）。

    类型：
      - placeholder_image: 占位图服务链接（high）
      - unverified_links: 外部链接未验证（medium）
      - query_coverage_missing: 目标话题要求的内容章节缺失（high）
      - image_alt_missing: 图片 alt 缺失或过泛（low）
    """
    gaps: list[dict] = []
    raw = doc.raw or ""
    scan_text = _text_without_code(raw)

    # 1) 占位图
    for img in doc.images:
        host = _host_of(img)
        if host in PLACEHOLDER_IMAGE_HOSTS:
            gaps.append({
                "type": "placeholder_image",
                "severity": "high",
                "detail": f"占位图 {img} 需替换为真实截图",
                "suggestion": "替换为真实截图或官方图片",
            })

    # 2) 外部链接待核实（排除占位图 URL，避免重复报告）
    placeholder_hosts = {
        _host_of(i) for i in doc.images if urllib.parse.urlparse(i).scheme in ("http", "https")
    }
    urls = sorted({
        u for u in _links_in_text(scan_text)
        if _host_of(u) and _host_of(u) not in placeholder_hosts
    })
    if urls:
        shown = "、".join(urls[:5]) + ("…" if len(urls) > 5 else "")
        gaps.append({
            "type": "unverified_links",
            "severity": "medium",
            "detail": f"正文 {len(urls)} 个外部链接未验证：{shown}",
            "suggestion": "发布前逐条核实链接域名与内容一致，尤其官网/下载链接",
        })

    # 3) query 要求的话题覆盖
    q = (query or "").strip()
    if q:
        low = scan_text.lower()
        missing = [
            term for term, words in QUERY_COVERAGE_TERMS.items()
            if term in q and not any(w in low for w in words)
        ]
        if missing:
            gaps.append({
                "type": "query_coverage_missing",
                "severity": "high",
                "detail": f"目标话题要求覆盖「{'、'.join(missing)}」，正文未见相关内容",
                "suggestion": "补充对应章节后再发布",
            })

    # 4) 图片 alt
    for line in re.findall(r"!\[[^\]]*\]\([^)]+\)", scan_text):
        alt = _image_alt(line)
        if not alt or alt.lower() in GENERIC_ALT:
            gaps.append({
                "type": "image_alt_missing",
                "severity": "low",
                "detail": f"图片 alt 缺失或过泛：{line[:60]}",
                "suggestion": "为图片补充描述性 alt 文本",
            })

    # 去重 + 按严重度排序
    seen: set[tuple[str, str]] = set()
    dedup: list[dict] = []
    for gap in gaps:
        key = (gap["type"], gap["detail"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(gap)
    severity_order = {"high": 0, "medium": 1, "low": 2}
    dedup.sort(key=lambda g: severity_order.get(g["severity"], 9))
    return dedup


def _gap_block(gaps: list[dict]) -> str:
    """生成发布前不可见的 HTML 注释块（编辑器可见，发布页无影响）"""
    if not gaps:
        return ""
    lines = ["<!-- 素材缺口（发布前处理，发布时删除本块） -->"]
    for gap in gaps:
        lines.append(
            f"<!-- [{gap['severity'].upper()}] {gap['detail']} → {gap['suggestion']} -->"
        )
    return "\n".join(lines) + "\n"


def _scan_facts(text: str) -> list[str]:
    """提取正文中带单位的具体数字断言，供人工核对（LLM 不验证事实）"""
    pattern = r"(\d+(?:\.\d+)?\s*(?:多\s*)?(?:积分|元|分钟|小时|天|秒|GB|MB|%|万|亿|字|行))"
    seen: set[str] = set()
    facts: list[str] = []
    for m in re.findall(pattern, text):
        norm = re.sub(r"\s+", "", m)
        if norm not in seen:
            seen.add(norm)
            facts.append(norm)
    return facts


# --------------------------------------------------------------------------
# 评分驱动改写指令（A1：低分维度强制修正，全高分才轻度润色）
# --------------------------------------------------------------------------


def _rewrite_instructions(
    dims: dict[str, int] | None,
    query: str | None = None,
) -> str:
    """按四维得分生成改写强度指令；dims 为 None 时返回空（贴近原稿）"""
    if not dims:
        return ""
    parts: list[str] = []
    kw = dims.get("关键词覆盖", 100)
    cit = dims.get("AI 可引用性", 100)
    qual = dims.get("内容质量 (E-E-A-T)", 100)
    struct = dims.get("结构与格式", 100)

    if kw < 60:
        q = (query or "").strip()
        if q:
            parts.append(f"标题必须包含目标关键词「{q}」或其自然变体，首段前 50 字内自然出现一次；禁止堆砌关键词")
        else:
            parts.append("标题必须包含核心关键词，首段前 50 字内自然出现一次；禁止堆砌关键词")
    if cit < 70:
        parts.append("每个 H2 下第一段写成 130-170 字自包含答案块：结论前置、直接回答、带具体信息")
    if qual < 60:
        parts.append(
            "补充具体数字、案例或第一手经验增强可信度；不得编造数据/链接/引用，"
            "缺失素材在文末用 HTML 注释列明，不要额外生成可见的素材缺口章节；"
            "第一手经验以第三人称转述（如「作者实测…」），不使用第一人称"
        )
    if struct < 60:
        parts.append("按「是什么 → 为什么用 → 怎么安装配置」重排 H2/H3，段落 120-180 字")

    if not parts:
        return "本次贴近原稿做轻度润色：修正语病、统一语气，不改变结构与篇幅。"
    return "改写强度较高，以下为必须逐条落实的规则：" + "".join(f"\n- {p}" for p in parts)


# --------------------------------------------------------------------------
# LLM 改写（DeepSeek；失败回退规则脚手架）
# --------------------------------------------------------------------------


def _llm_rewrite(system: str, user: str, timeout: int = 90) -> str | None:
    """调用 DeepSeek 生成；任何失败返回 None（调用方回退脚手架）"""
    key = search_ai._load_key()
    if key is None:
        return None
    payload = {
        "model": search_ai.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(
            search_ai.DEEPSEEK_BASE, json=payload, headers=headers, timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        return content if isinstance(content, str) and content.strip() else None
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None


# --------------------------------------------------------------------------
# AI 优化版生成
# --------------------------------------------------------------------------


AI_SYSTEM = (
    "你是 AI 可见度优化编辑。把用户草稿改写成「AI 搜索优化版」：写给 AI 在合成答案时能准确摘取，"
    "不是写给人类通读。规则：每个 H2 是一个完整问答对；H2 下第一段 130-170 字、自包含、"
    "直接给答案并带具体信息；不使用第一人称；不使用表格和复杂列表；不依赖链接；"
    "总长 1200-2000 字。只输出 Markdown，不要额外说明。"
)


def _clean_ai_text(text: str) -> str:
    """AI 版文本清理：去图片引用 + 压缩空白（AI 版不用图）"""
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _ai_system(dims: dict[str, int] | None = None, query: str | None = None) -> str:
    extra = _rewrite_instructions(dims, query)
    return AI_SYSTEM.rstrip() + (f"\n\n{extra}" if extra else "")


def _fallback_ai_version(doc: DraftDoc) -> str:
    """无 LLM 时的规则脚手架：把每个 H2 话题重组为问答对 + 自包含首段"""
    lines = [f"# {doc.title}：快速了解与使用指南", ""]
    if not doc.sections:
        # 无任何小节：整篇引言即「它是什么」自包含答案，不生成默认话题占位
        intro_text = _clean_ai_text(" ".join(doc.intro))
        if intro_text:
            lines.append("## 它是什么？")
            lines.append("")
            lines.append(intro_text)
            lines.append("")
        lines.append("## 总结")
        lines.append("")
        lines.append(
            f"{doc.title}的核心要点已整理为自包含问答块，"
            "发布后可观察 AI 平台对该话题的回答是否引用本文。"
        )
        lines.append("")
        return "\n".join(lines)
    if doc.intro and not any(("是什么" in h or "介绍" in h) for h, _ in doc.sections):
        # 引言回答「它是什么」，是 AI 引用最可能摘取的内容，先补一个问答块
        intro_text = _clean_ai_text(" ".join(doc.intro))
        if intro_text:
            lines.append("## 它是什么？")
            lines.append("")
            lines.append(intro_text[:150])
            lines.append("")
            rest = intro_text[150:]
            if rest:
                lines.append(rest)
                lines.append("")
    used: list[str] = []
    for heading, _ in doc.sections:
        topic = heading
        if topic in used:
            continue
        used.append(topic)
        q = topic if re.search(r"[？?]$", topic) else f"{topic}？"
        # 重复 H2 标题的内容合并进同一个问答块，不丢弃
        body = "\n".join(
            "\n".join(sec_body) for h, sec_body in doc.sections if h == topic
        )
        body = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", body)  # AI 优化版去图
        answer = re.sub(r"\s+", " ", body).strip()
        if len(answer) < 60:
            answer = f"{topic}是本文介绍的核心内容：{answer or '具体信息以官方文档为准。'}"
        first = answer[:150]
        lines.append(f"## {q}")
        lines.append("")
        lines.append(first)
        lines.append("")
        rest = answer[150:]
        if rest:
            lines.append(rest)
            lines.append("")
    lines.append("## 总结")
    lines.append("")
    lines.append(
        f"{doc.title}的核心要点已按「是什么、优势、安装配置」整理为自包含问答块，"
        "发布后可观察 AI 平台对该话题的回答是否引用本文。"
    )
    lines.append("")
    return "\n".join(lines)


def generate_ai_version(
    doc: DraftDoc,
    dims: dict[str, int] | None = None,
    query: str | None = None,
) -> tuple[str, bool]:
    """生成 AI 优化版；返回 (markdown, 是否走了 LLM)"""
    llm = _llm_rewrite(_ai_system(dims, query), doc.raw)
    if llm:
        return llm, True
    return _fallback_ai_version(doc), False


# --------------------------------------------------------------------------
# 知乎版生成
# --------------------------------------------------------------------------


ZHIHU_SYSTEM = (
    "你是知乎长文编辑。把用户草稿改写成知乎版：2000-4000 字为上限，本次贴近原稿做结构化改写与适度扩写，"
    "不硬凑字数。规则：H2/H3 层级清晰；段落 120-180 字；标题含核心关键词；"
    "正文可有数据、引用、案例；文末可以提问引导互动但不要诱导点赞；不写营销号话术。"
    "保留原稿中的代码块与图片引用（图片引用保持 ![alt](path) 形式）。只输出 Markdown。"
)


def _zhihu_system(dims: dict[str, int] | None = None, query: str | None = None) -> str:
    extra = _rewrite_instructions(dims, query)
    return ZHIHU_SYSTEM.rstrip() + (f"\n\n{extra}" if extra else "")


def _fallback_zhihu_version(doc: DraftDoc) -> str:
    """无 LLM 时的规则脚手架：结构化重排 + 段落切分 + 保留代码/图片"""
    lines = [f"# {doc.title}", ""]
    # 引言：首段
    if doc.intro:
        intro = re.sub(r"\s+", " ", " ".join(doc.intro)).strip()
        lines.append(intro)
        lines.append("")
    for heading, body in doc.sections:
        lines.append(f"## {heading}")
        lines.append("")
        in_code = False
        for line in body:
            if line.strip().startswith("```"):
                # 代码块围栏：原样保留，结束围栏后补空行分隔，代码行不切分不加空行
                in_code = not in_code
                lines.append(line)
                if not in_code:
                    lines.append("")
                continue
            if in_code:
                lines.append(line)
                continue
            if line.startswith(("![", "#")):
                lines.append(line)
                lines.append("")
                continue
            if len(line) > 180:
                # 超长段落按句切分到 ≤180 字
                chunks = _split_paragraph(line, 180)
                for chunk in chunks:
                    lines.append(chunk)
                    lines.append("")
            else:
                lines.append(line)
                lines.append("")
    lines.append("## 写在最后")
    lines.append("")
    lines.append("你在使用过程中遇到过什么问题？欢迎在评论区交流。")
    lines.append("")
    return "\n".join(lines)


def _split_paragraph(text: str, limit: int) -> list[str]:
    """按句号/分号切分长段落到 ≤limit 字符"""
    sentences = re.split(r"(?<=[。；!?！？])", text)
    chunks: list[str] = []
    buf = ""
    for sent in sentences:
        if not sent:
            continue
        if len(buf) + len(sent) <= limit:
            buf += sent
        else:
            if buf:
                chunks.append(buf.strip())
            if len(sent) > limit:
                # 超长单句直接硬切
                while len(sent) > limit:
                    chunks.append(sent[:limit].strip())
                    sent = sent[limit:]
                buf = sent
            else:
                buf = sent
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [text[:limit]]


def generate_zhihu_version(
    doc: DraftDoc,
    dims: dict[str, int] | None = None,
    query: str | None = None,
) -> tuple[str, bool]:
    """生成知乎版；返回 (markdown, 是否走了 LLM)"""
    llm = _llm_rewrite(_zhihu_system(dims, query), doc.raw)
    if llm:
        return llm, True
    return _fallback_zhihu_version(doc), False


# --------------------------------------------------------------------------
# 输出与清单
# --------------------------------------------------------------------------


def build_manifest(
    doc: DraftDoc,
    query: str | None,
    versions: dict[str, tuple[str, bool]],
    draft_score: dict,
    out_dir: Path,
    gaps: list[dict] | None = None,
    checklist_path: Path | None = None,
) -> dict:
    """生成输出清单：每个版本带 falsifiability check"""
    gaps = gaps or []
    slug = _slug(doc.title)
    paths: dict[str, str] = {}
    for name, (content, used_llm) in versions.items():
        path = out_dir / f"{name}-{slug}.md"
        body = content.rstrip() + "\n\n" + _gap_block(gaps)
        if name == "ai":
            body = AI_PURPOSE_NOTE + body
        path.write_text(body, encoding="utf-8")
        paths[name] = str(path)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_title": doc.title,
        "source_chars": len(doc.raw),
        "query": query,
        "draft_score": draft_score,
        "material_gaps": gaps,
        "rewrite_triggers": [
            name for name, score in draft_score["dimensions"].items() if score < 60
        ],
        "human_review": {
            "status": "pending",
            "checklist": str(checklist_path) if checklist_path else None,
            "note": "LLM 改写输出为待人工审阅的草稿；发布前按检查清单逐项确认。",
        },
        "versions": {
            name: {
                "file": paths[name],
                "used_llm": used_llm,
                "purpose": (
                    "publish（处理素材缺口后可直接发布）"
                    if name == "zhihu"
                    else "internal_reference（内部参考，非直接发布物）"
                ),
                "falsifiability_check": (
                    f"发布对应版本后，重跑 /pulse track --query {search_ai._shell_quote(query or doc.title)} "
                    f"--mine <文章URL>，AI 回答该话题时引用来源里出现你的内容"
                ),
            }
            for name, (_, used_llm) in versions.items()
        },
        "note": "输出为待发布的草稿，不是保证被引用的成品；互动维度需发布后重跑 audit 补全。",
    }


def build_review_checklist(
    doc: DraftDoc,
    query: str | None,
    gaps: list[dict],
    draft_score: dict,
    versions: dict[str, tuple[str, bool]],
    out_dir: Path,
) -> Path:
    """生成发布前人工审阅清单（LLM 改写版的人工介入入口）"""
    lines = [
        f"# 发布前检查清单 · {doc.title}",
        "",
        "> 本清单由 /pulse adapt 自动生成。LLM 改写输出是「待人工审阅的草稿」，发布前请逐项确认。",
        "",
        f"**目标话题**：{query or doc.title}",
        f"**草稿评分**：{draft_score['overall']}/100 · {draft_score['grade']}",
        "",
        "## 1. 素材缺口（发布前必须处理）",
        "",
    ]
    if gaps:
        lines.extend(
            f"- [ ] [{g['severity'].upper()}] {g['detail']} → {g['suggestion']}"
            for g in gaps
        )
    else:
        lines.append("- [x] 无自动检测到的素材缺口（仍建议通读全文）")
    lines += ["", "## 2. 事实核对（LLM 只改文风、不验证事实，请人工核实）", ""]
    facts = _scan_facts(_text_without_code(doc.body_text or doc.raw))
    if facts:
        lines.append("以下带单位的具体数字断言来自草稿，请与官网/官方文档核对：")
        lines.extend(f"- [ ] {fact}" for fact in facts)
    else:
        lines.append("- [ ] 未检测到带单位的数字断言，仍建议核对正文事实")
    lines += ["", "## 3. 版本用途", ""]
    for name, (_, used_llm) in versions.items():
        purpose = (
            "可发布物（处理完素材缺口后）"
            if name == "zhihu"
            else "内部参考，非直接发布物（用于校验与未来多平台骨架）"
        )
        engine = "LLM 改写" if used_llm else "规则脚手架"
        lines.append(f"- **{name} 版**（{engine}）：{purpose}")
    lines += [
        "",
        "## 4. 发布确认",
        "",
        "- [ ] 已处理全部素材缺口（替换占位图/核实链接/补齐缺失章节）",
        "- [ ] 已核对正文数字、链接与官方信息一致",
        "- [ ] 已通读 LLM 改写内容，确认无事实偏差与风格问题",
        "",
    ]
    path = out_dir / f"review-checklist-{_slug(doc.title)}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def save_manifest(manifest: dict, out_dir: Path) -> Path:
    ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    path = out_dir / f"adapt-{_slug(manifest['source_title'])}-{ts}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _existing_file(value: str) -> str:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"文件不存在或不是文件：{value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse-adapt",
        description="Pulse 多平台内容适配 —— 本地 Markdown 草稿 → 知乎版 + AI 优化版",
    )
    parser.add_argument("--source", required=True, type=_existing_file, help="本地 Markdown 草稿路径")
    parser.add_argument("--query", help="目标话题关键词（用于打分与验证命令，默认取草稿标题）")
    parser.add_argument(
        "--platforms",
        help="逗号分隔的版本列表，默认 zhihu,ai（当前支持 zhihu/ai）",
    )
    parser.add_argument("--no-llm", action="store_true", help="强制走规则脚手架，不调用 LLM")
    parser.add_argument("--output", help="输出目录（默认 data/output/）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_llm:
        search_ai._load_key = lambda: None  # type: ignore[assignment]  # 测试/离线模式
    platforms = [p.strip().lower() for p in (args.platforms or "zhihu,ai").split(",") if p.strip()]
    platforms = list(dict.fromkeys(platforms))
    supported = {"zhihu", "ai"}
    bad = [p for p in platforms if p not in supported]
    if bad:
        print(f"暂不支持的版本：{'、'.join(bad)}（可用：{'、'.join(sorted(supported))}）", file=sys.stderr)
        return 2
    out_dir = Path(args.output) if args.output else OUTPUT_DIR

    try:
        doc = parse_markdown(Path(args.source).read_text(encoding="utf-8"))
        if not doc.title or doc.title == "untitled":
            print("草稿缺少一级标题（# 标题）。", file=sys.stderr)
            return 2
        query = args.query or doc.title
        keywords = _query_keywords(query)
        draft_score = score_draft(doc.title, doc.body_text or doc.raw, keywords)
        gaps = detect_material_gaps(doc, query)
        out_dir.mkdir(parents=True, exist_ok=True)

        versions: dict[str, tuple[str, bool]] = {}
        if "ai" in platforms:
            versions["ai"] = generate_ai_version(doc, draft_score["dimensions"], query)
        if "zhihu" in platforms:
            versions["zhihu"] = generate_zhihu_version(doc, draft_score["dimensions"], query)
        if not versions:
            print("未生成任何版本。", file=sys.stderr)
            return 2

        checklist_path = build_review_checklist(
            doc, query, gaps, draft_score, versions, out_dir
        )
        manifest = build_manifest(
            doc, query, versions, draft_score, out_dir, gaps, checklist_path
        )
        manifest_path = save_manifest(manifest, out_dir)
    except OSError as exc:
        print(f"本地读写失败：{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"数据/配置异常：{exc}", file=sys.stderr)
        return 1

    print(f"草稿：{doc.title}（{len(doc.raw)} 字）")
    print(f"草稿得分：{draft_score['overall']}/100 · {draft_score['grade']}（互动维度未发布）")
    for name in ("AI 可引用性", "内容质量 (E-E-A-T)", "关键词覆盖", "结构与格式"):
        print(f"  {name}: {draft_score['dimensions'][name]}/100")
    for name in versions:
        used = "LLM" if versions[name][1] else "规则脚手架"
        print(f"  {name} 版：{manifest['versions'][name]['file']}（{used}）")
    print(f"行动建议：{len(draft_score['recommendations'])} 条（P0={sum(1 for r in draft_score['recommendations'] if r['priority'] == 'P0')}）")
    if gaps:
        high = sum(1 for g in gaps if g["severity"] == "high")
        print(f"素材缺口：{len(gaps)} 项（高危={high}）")
        for gap in gaps:
            print(f"  [{gap['severity']}] {gap['detail']}")
    print(f"发布前检查清单：{manifest['human_review']['checklist']}")
    print(f"已保存清单：{manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
