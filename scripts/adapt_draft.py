"""adapt 草稿解析与四维打分（从 content_adapter.py 拆分，行为不变）"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from scorer import BLOCKED_CEILING, audit_article, grade

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTENT_FORMAT_PATH = PROJECT_ROOT / "skills" / "visibility" / "references" / "content-format.md"

DRAFT_WEIGHTS = {
    "AI 可引用性": 0.389,
    "内容质量 (E-E-A-T)": 0.278,
    "关键词覆盖": 0.222,
    "结构与格式": 0.111,
}

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

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


def _strip_frontmatter(text: str) -> str:
    """去掉 YAML frontmatter，返回正文（含标题与 H2）"""
    lines = (text or "").splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
        if end is not None:
            return "\n".join(lines[end + 1:])
    return text


def _slug(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title or "untitled").strip("-")
    return slug[:max_len] or "untitled"


def _load_content_format() -> str:
    """读取平台格式知识库（references/content-format.md）；缺失时返回空"""
    try:
        return CONTENT_FORMAT_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _content_format_updated() -> str | None:
    """从知识库头部提取 Updated: YYYY-MM-DD（兼容旧「最后更新」标记）"""
    m = re.search(
        r"(?:最后更新|Updated)\s*[:：]\s*(\d{4}-\d{2}-\d{2})",
        _load_content_format(),
        re.IGNORECASE,
    )
    return m.group(1) if m else None


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
    fence_start = ""

    def flush() -> None:
        nonlocal current_lines
        if current_heading:
            sections.append((current_heading, current_lines))
        current_lines = []

    for line in lines:
        s = line.strip()
        if s.startswith("```"):
            if in_code:
                code = "\n".join(code_buffer)
                code_blocks.append(code)
                block_lines = [fence_start] + code_buffer + [line]
                if saw_heading:
                    current_lines.extend(block_lines)
                else:
                    intro.extend(block_lines)  # 首个 H2 前的代码块进 intro，不丢失
                code_buffer = []
                in_code = False
            else:
                in_code = True
                fence_start = line
            continue
        if in_code:
            code_buffer.append(line)
            continue
        inline_imgs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", s)
        images.extend(inline_imgs)
        if s.startswith("![") and inline_imgs:
            # 行首图片：整行作为图片块（行归属随 saw_heading）
            if saw_heading:
                current_lines.append(s)
            else:
                intro.append(s)  # 首个 H2 前的图片进 intro，不丢失
            continue
        # 行内图片：URL 已收集进 images，行本身按普通文本继续处理
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
            if saw_heading:
                current_lines.append(s)
            else:
                intro.append(s)  # 首个 H2 前的子标题进 intro，不丢失
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


def _draft_recommendations(
    title: str, dims: dict[str, int], query: str | None = None
) -> list[dict]:
    """按四维得分生成带 falsifiability check 的建议（草稿版，无互动维度）"""
    recs: list[dict] = []
    target = query or title  # 关键词优化目标：优先 --query，与改写指令一致

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
            f"标题/首段/H2 自然覆盖目标关键词（如「{target}」及其子话题词），不做堆砌",
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
    blockers = list(base.blockers)
    if blockers:
        # 一票封顶：草稿存在阻断项时总分封在低档
        overall = min(overall, BLOCKED_CEILING)
    return {
        "title": title,
        "overall": overall,
        "grade": grade(overall),
        "blockers": blockers,
        "dimensions": dims,
        "engagement": {"status": "未发布", "note": "互动数据需发布后重跑 audit 获取"},
        "recommendations": _draft_recommendations(
            title, dims, keywords[0] if keywords else None
        ),
    }


def _query_keywords(query: str) -> list[str]:
    """把 query 拆成「整串 + 覆盖词元」用于关键词覆盖打分"""
    q = (query or "").strip()
    if not q:
        return []
    out = [q] + [t for t in QUERY_COVERAGE_TERMS if t in q]
    return list(dict.fromkeys(out))  # 去重保序，避免同一词元重复计权
