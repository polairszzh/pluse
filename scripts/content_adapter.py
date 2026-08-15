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

import bottleneck_diag
import requests
import search_ai
from fact_checker import risk_severity, verify_facts
from scorer import BLOCKED_CEILING, audit_article, grade

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data" / "output"
CONTENT_FORMAT_PATH = PROJECT_ROOT / "skills" / "visibility" / "references" / "content-format.md"

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

# 营销转化/投毒信号：高危 = 明确的转化引导（必须人工处理）；中危 = 营销话术（提示核实）
PROMOTIONAL_HIGH_PATTERNS = [
    "闭眼入", "无脑入", "必买", "人手一份", "手慢无", "库存告急",
    "限时秒杀", "限时抢购", "错过等一年", "赶紧下单", "立即下单",
    "扫码购买", "扫码下单", "扫码领取", "扫码添加", "扫码加",
    "扫码报名", "扫码进群",
    "私信领取", "私信报名", "私信购买", "私信获取",
    "私信我",
    "优惠码", "优惠券", "付款码", "立即购买",
    "加微信", "加vx", "加VX",
]
PROMOTIONAL_MEDIUM_PATTERNS = [
    "强烈推荐", "一定要买", "一定要入手", "必入", "全网第一", "史上第一",
    "唯一", "100%", "免费领取", "限时", "报名通道", "课程报名",
]

# 词边界：命中后若紧跟这些字，视为子串误报（如「私信我们」「唯一的办法」）
PATTERN_SUFFIX_BLOCKS = {
    "私信我": {"们"},
    "唯一": {"的"},
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


# --------------------------------------------------------------------------
# 素材缺口检测（发布前必须处理：占位图/待核实链接/query 覆盖缺失/图片 alt）
# --------------------------------------------------------------------------


def _links_in_text(text: str) -> list[str]:
    """提取正文中的所有 http(s) 链接，去掉尾部标点"""
    # URL 字符集近似 ASCII：排除空白/括号/引号/中文字符与中文标点，避免吞掉紧贴的文本
    found = re.findall(r"https?://[^\s)\]>\"'\u4e00-\u9fff，。；：！？、]+", text)
    return [u.rstrip(".,;:!?。，；：！？") for u in found]


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _image_alt(line: str) -> str:
    m = re.match(r"!\[([^\]]*)\]\([^)]+\)", line.strip())
    return m.group(1).strip() if m else ""


def detect_promotional_signals(text: str) -> list[dict]:
    """检测营销转化/投毒信号（强烈推荐课程/网站/购买引导），返回按严重度排序的清单。

    高危：明确的转化动作（扫码/私信/优惠码/限时抢购等）——发布前必须处理；
    中危：营销话术（强烈推荐/绝对化用语）——提示核实利益关系。
    """
    signals: list[dict] = []
    low = (text or "").lower()
    high_spans: list[tuple[int, int]] = []

    def scan(patterns: list[str], severity: str, skip: list[tuple[int, int]]) -> None:
        for pat in patterns:
            needle = pat.lower()
            start = 0
            while True:
                idx = low.find(needle, start)
                if idx == -1:
                    break
                end = idx + len(needle)
                # 跳过已被高危覆盖的区间，以及同位置重复命中（如「加vx」与「加VX」）
                if any(s <= idx < e or s < end <= e for s, e in skip):
                    start = end
                    continue
                if severity == "high" and any(s <= idx < e or s < end <= e for s, e in high_spans):
                    start = end
                    continue
                follow = low[end:end + 1]
                if follow in PATTERN_SUFFIX_BLOCKS.get(pat, set()):
                    # 子串误报（「私信我们」「唯一的办法」），跳过
                    start = end
                    continue
                ctx_start = max(0, idx - 12)
                ctx_end = min(len(text), end + 12)
                signals.append({
                    "severity": severity,
                    "pattern": pat,
                    "context": text[ctx_start:ctx_end].replace("\n", " "),
                })
                if severity == "high":
                    high_spans.append((idx, end))
                start = end

    scan(PROMOTIONAL_HIGH_PATTERNS, "high", [])
    scan(PROMOTIONAL_MEDIUM_PATTERNS, "medium", high_spans)
    severity_order = {"high": 0, "medium": 1}
    signals.sort(key=lambda s: severity_order.get(s["severity"], 9))
    return signals


def detect_material_gaps(doc: DraftDoc, query: str | None = None) -> list[dict]:
    """识别草稿里的素材缺口，返回按严重度排序的清单（发布前处理）。

    类型：
      - placeholder_image: 占位图服务链接（high）
      - unverified_links: 外部链接未验证（medium）
      - query_coverage_missing: 目标话题要求的内容章节缺失（high）
      - image_alt_missing: 图片 alt 缺失或过泛（low）
    """
    gaps: list[dict] = []
    # 用剥 frontmatter、去代码块的正文扫描，避免元数据 URL/关键词误报，同时保留 H2 标题
    scan_text = _text_without_code(_strip_frontmatter(doc.raw))

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
    # 只排除占位图服务的 host（已报高危缺口），非占位图图片链接仍计入待核实
    placeholder_hosts = {
        _host_of(i) for i in doc.images if _host_of(i) in PLACEHOLDER_IMAGE_HOSTS
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

    # 5) 营销转化/投毒信号
    for sig in detect_promotional_signals(scan_text):
        gaps.append({
            "type": "promotional_signal",
            "severity": sig["severity"],
            "detail": f"疑似营销转化话术「{sig['pattern']}」：…{sig['context']}…",
            "suggestion": (
                "移除营销转化引导（扫码/私信/优惠码/绝对化推荐），或补充明确的利益关系声明后再发布"
                if sig["severity"] == "high"
                else "核实是否夹带付费推广，建议补充利益关系声明"
            ),
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


def _rewrite_triggers(dims: dict[str, int] | None) -> list[str]:
    """与 _rewrite_instructions 同阈值的触发维度清单（供 manifest 记录，保证一致）"""
    if not dims:
        return []
    out: list[str] = []
    if dims.get("关键词覆盖", 100) < 60:
        out.append("关键词覆盖")
    if dims.get("AI 可引用性", 100) < 70:
        out.append("AI 可引用性")
    if dims.get("内容质量 (E-E-A-T)", 100) < 60:
        out.append("内容质量 (E-E-A-T)")
    if dims.get("结构与格式", 100) < 60:
        out.append("结构与格式")
    return out


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


def _llm_rewrite(
    system: str, user: str, timeout: int = 90, enabled: bool = True
) -> str | None:
    """调用 DeepSeek 生成；enabled=False 或任何失败返回 None（调用方回退脚手架）"""
    if not enabled:
        return None
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
    except (
        requests.exceptions.RequestException, ValueError, KeyError,
        IndexError, TypeError, AttributeError,
    ):
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
    parts = [AI_SYSTEM]
    if extra:
        parts.append(extra)
    rules = _load_content_format()
    if rules:
        parts.append(
            "平台格式规则（来自 references/content-format.md，AI 优化版请遵循表格「AI 优化版」列）：\n" + rules
        )
    return "\n\n".join(parts)


def _first_150(text: str) -> tuple[str, str]:
    """取 ≤150 字内最后一个完整句作为首段，避免硬截断断句；返回 (first, rest)"""
    if len(text) <= 150:
        return text, ""
    cut = text[:150]
    idx_cn = max(
        cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("；")
    )
    if idx_cn != -1:
        return cut[: idx_cn + 1], text[idx_cn + 1:]
    # 无中文句读时才考虑英文句点，且要求句点后是空白/句尾（避免版本号/小数点在句中截断）
    idx_en = -1
    for i in range(len(cut) - 1, -1, -1):
        if cut[i] == "." and (i == len(cut) - 1 or cut[i + 1].isspace()):
            idx_en = i
            break
    if idx_en == -1:
        return cut, text[150:]
    return cut[: idx_en + 1], text[idx_en + 1:]


def _fallback_ai_version(doc: DraftDoc) -> str:
    """无 LLM 时的规则脚手架：把每个 H2 话题重组为问答对 + 自包含首段"""
    lines = [f"# {doc.title}：快速了解与使用指南", ""]
    intro_text = _clean_ai_text(" ".join(doc.intro))
    intro_target: str | None = None
    if not doc.sections:
        # 无任何小节：整篇引言即「它是什么」自包含答案，不生成默认话题占位
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
    if intro_text:
        target = next(
            (h for h, _ in doc.sections if ("是什么" in h or "介绍" in h)),
            None,
        )
        if target:
            # 已有「是什么/介绍」H2：引言合并进该节回答开头，不单独成块也不丢弃
            intro_target = target
        else:
            # 引言回答「它是什么」，是 AI 引用最可能摘取的内容，先补一个问答块
            first, rest = _first_150(intro_text)
            lines.append("## 它是什么？")
            lines.append("")
            lines.append(first)
            lines.append("")
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
        if heading == intro_target and intro_text:
            body = intro_text + "\n" + body
        body = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", body)  # AI 优化版去图
        answer = re.sub(r"\s+", " ", body).strip()
        if heading not in doc.topics:
            # 后续 H1 分节标题：不转问答，保留为子节直接输出
            lines.append(f"## {heading}")
            lines.append("")
            lines.append(answer or "（本节内容待补充）")
            lines.append("")
            continue
        if len(answer) < 60:
            answer = f"{topic}是本文介绍的核心内容：{answer or '具体信息以官方文档为准。'}"
        first, rest = _first_150(answer)
        lines.append(f"## {q}")
        lines.append("")
        lines.append(first)
        lines.append("")
        if rest:
            lines.append(rest)
            lines.append("")
    if not any("总结" in h for h, _ in doc.sections):
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
    llm: bool = True,
) -> tuple[str, bool]:
    """生成 AI 优化版；返回 (markdown, 是否走了 LLM)"""
    out = _llm_rewrite(_ai_system(dims, query), _strip_frontmatter(doc.raw), enabled=llm)
    if out:
        return out, True
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
    parts = [ZHIHU_SYSTEM]
    if extra:
        parts.append(extra)
    rules = _load_content_format()
    if rules:
        parts.append(
            "平台格式规则（来自 references/content-format.md，知乎版请遵循「知乎」列与注意事项）：\n" + rules
        )
    return "\n\n".join(parts)


def _format_zhihu_lines(lines_in: list[str]) -> list[str]:
    """把引言/节内行格式化为知乎版：代码块独立成块、表格连续、列表/引用/图片/标题原样、普通文本拼段"""
    out: list[str] = []
    in_code = False
    para: list[str] = []
    table_mode = False

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append(re.sub(r"\s+", " ", " ".join(para)).strip())
            out.append("")
            para = []

    for line in lines_in:
        s = line.strip()
        if s.startswith("```"):
            flush_para()
            table_mode = False
            in_code = not in_code
            out.append(line)
            if not in_code:
                _ensure_blank(out)
            continue
        if in_code:
            out.append(line)
            continue
        if not s:
            flush_para()  # 空行作为段落分隔，不并入段落
            continue
        if s.startswith("|"):
            # 表格行连续输出，行间不加空行（避免拆散表格）
            flush_para()
            if not table_mode:
                _ensure_blank(out)
            out.append(line)
            table_mode = True
            continue
        table_mode = False
        if s == "---" or s.startswith(("![", "#", "- ", "* ", "+ ", ">")) or re.match(
            r"^\d+[.、)]\s", s
        ):
            # 列表/引用/图片/标题：原样输出，前后空行分隔
            flush_para()
            out.append(line)
            out.append("")
            continue
        para.append(line)
    flush_para()
    return out


def _ensure_blank(lines_out: list[str]) -> None:
    """仅当末行非空时才追加空行，避免连续空行"""
    if lines_out and lines_out[-1] != "":
        lines_out.append("")


def _fallback_zhihu_version(doc: DraftDoc) -> str:
    """无 LLM 时的规则脚手架：结构化重排 + 段落切分 + 保留代码/图片"""
    lines = [f"# {doc.title}", ""]
    # 引言：首段
    if doc.intro:
        lines.extend(_format_zhihu_lines(doc.intro))
    for heading, body in doc.sections:
        lines.append(f"## {heading}")
        lines.append("")
        in_code = False
        table_mode = False
        for line in body:
            if line.strip().startswith("```"):
                # 代码块围栏：原样保留，结束围栏后补空行分隔，代码行不切分不加空行
                in_code = not in_code
                lines.append(line)
                if not in_code:
                    _ensure_blank(lines)
                continue
            if in_code:
                lines.append(line)
                continue
            if not line.strip():
                continue  # 正文空行跳过，避免连续空行（段落间由行尾空行分隔）
            if line.startswith("|"):
                # 表格行连续输出，行间不加空行
                if not table_mode:
                    _ensure_blank(lines)
                lines.append(line)
                table_mode = True
                continue
            table_mode = False
            if line.strip() == "---" or line.startswith(
                ("![", "#", "- ", "* ", "+ ", ">")
            ) or re.match(r"^\d+[.、)]\s", line):
                # 列表/引用/图片/标题：原样输出，不切分，保留前缀
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
    llm: bool = True,
) -> tuple[str, bool]:
    """生成知乎版；返回 (markdown, 是否走了 LLM)"""
    out = _llm_rewrite(_zhihu_system(dims, query), _strip_frontmatter(doc.raw), enabled=llm)
    if out:
        return out, True
    return _fallback_zhihu_version(doc), False


# --------------------------------------------------------------------------
# 输出与清单
# --------------------------------------------------------------------------


def _postprocess_llm_output(content: str, version: str) -> tuple[str, list[str]]:
    """LLM 输出确定性后处理：AI 版去图、规整连续空行；返回 (content, warnings)"""
    warnings: list[str] = []
    text = _strip_frontmatter(content or "")  # LLM 若复制 frontmatter，剥掉
    if version == "ai":
        imgs = re.findall(r"!\[[^\]]*\]\([^)]+\)", text)
        if imgs:
            text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
            warnings.append(f"AI 版移除了 {len(imgs)} 处图片引用")
    text = _collapse_blank_lines(text)  # 跳过代码块压缩空行，避免破坏代码排版
    return text.strip(), warnings


def _collapse_blank_lines(text: str) -> str:
    """代码块外连续空行压缩为至多一个空行，代码块内原样保留"""
    out: list[str] = []
    in_code = False
    blank_run = 0
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if blank_run:
                out.append("")
            blank_run = 0
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        if not line.strip():
            blank_run += 1
            continue
        if blank_run:
            out.append("")
        blank_run = 0
        out.append(line)
    if blank_run:
        out.append("")
    return "\n".join(out)


def build_manifest(
    doc: DraftDoc,
    query: str | None,
    versions: dict[str, tuple[str, bool]],
    draft_score: dict,
    out_dir: Path,
    gaps: list[dict] | None = None,
    checklist_path: Path | None = None,
    postprocess_warnings: list[str] | None = None,
    bottleneck: dict | None = None,
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
        "bottleneck": bottleneck,
        "draft_score": draft_score,
        "material_gaps": gaps,
        "llm_postprocess_warnings": postprocess_warnings or [],
        "content_format": {
            "file": str(CONTENT_FORMAT_PATH),
            "loaded": bool(_load_content_format()),
            "updated": _content_format_updated(),
        },
        "rewrite_triggers": _rewrite_triggers(draft_score["dimensions"]),
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
    if draft_score.get("blockers"):
        idx = lines.index("## 1. 素材缺口（发布前必须处理）")
        lines[idx:idx] = (
            ["**阻断原因（一票封顶）**："]
            + [f"- {b}" for b in draft_score["blockers"]]
            + [""]
        )
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
        # A2 瓶颈定位：改写前先看话题在 AI 检索库里的状态（尽力而为，失败降级不阻断）
        try:
            bottleneck_result = bottleneck_diag.diagnose(query)
        except Exception:  # noqa: BLE001 — 诊断失败不影响生成
            bottleneck_result = None

        high_promo = [
            g for g in gaps
            if g["type"] == "promotional_signal" and g["severity"] == "high"
        ]
        if high_promo:
            print("拒绝生成：检测到明确的营销转化信号，Pulse 不帮这类内容做适配。", file=sys.stderr)
            for gap in high_promo:
                print(f"  [拒绝] {gap['detail']}", file=sys.stderr)
            print(
                "请移除营销转化引导（扫码/私信/优惠码/必买/限时抢购等），"
                "或补充明确的利益关系声明后重试。",
                file=sys.stderr,
            )
            return 3

        # 置信度防火墙：对草稿中的公开声明型数字断言做多源交叉验证（尽力而为，失败降级不阻断）
        try:
            scan_text = _text_without_code(_strip_frontmatter(doc.raw))
            fact_results = verify_facts(scan_text, query)
        except Exception:  # noqa: BLE001 — 防火墙尽力而为：任何异常都降级，不阻断生成
            fact_results = []
        conflicts = [r for r in fact_results if r["status"] == "conflict"]
        if conflicts:
            print("拒绝生成：检测到数据断言与检索结果冲突，Pulse 不帮无法核实的数据做适配。", file=sys.stderr)
            for r in conflicts:
                snippet = "；".join(r["reject_snippets"][:2])
                print(
                    f"  [拒绝] 断言「{r['fact']}」（{r['context'][:30]}…）与来源矛盾：{snippet}",
                    file=sys.stderr,
                )
            print("请核实数据来源或移除该断言后重试。", file=sys.stderr)
            return 3
        for r in fact_results:
            if r["status"] in ("unverified", "untrusted"):
                risk_note = f"（{r['risk']}领域）" if r["risk"] else ""
                if r["status"] == "untrusted":
                    detail = f"数据断言「{r['fact']}」仅检索到普通来源，可信度不足{risk_note}（普通网页可能为投毒/灌水来源）"
                    suggestion = "发布前通过权威渠道核验，或移除该断言"
                else:
                    detail = f"数据断言「{r['fact']}」无法核实{risk_note}：{r.get('reason', '未检索到来源')}"
                    suggestion = "发布前通过官方渠道核验，或在正文标注不确定性"
                gaps.append({
                    "type": "fact_unverified",
                    "severity": risk_severity(r["risk"]),
                    "detail": detail,
                    "suggestion": suggestion,
                })
        severity_order = {"high": 0, "medium": 1, "low": 2}
        gaps.sort(key=lambda g: severity_order.get(g["severity"], 9))

        out_dir.mkdir(parents=True, exist_ok=True)

        versions: dict[str, tuple[str, bool]] = {}
        if "ai" in platforms:
            versions["ai"] = generate_ai_version(
                doc, draft_score["dimensions"], query, llm=not args.no_llm
            )
        if "zhihu" in platforms:
            versions["zhihu"] = generate_zhihu_version(
                doc, draft_score["dimensions"], query, llm=not args.no_llm
            )
        if not versions:
            print("未生成任何版本。", file=sys.stderr)
            return 2

        postprocess_warnings: list[str] = []
        for name in list(versions):
            content, used = versions[name]
            cleaned, warns = _postprocess_llm_output(content, name)
            versions[name] = (cleaned, used)
            postprocess_warnings.extend(warns)
            # LLM 改写产物补扫营销信号：改写引入转化话术同样拒绝（exit 3）
            high_sigs = [
                s for s in detect_promotional_signals(cleaned)
                if s["severity"] == "high"
            ]
            if high_sigs:
                print("拒绝生成：LLM 改写输出含营销转化信号，不产出版本。", file=sys.stderr)
                for sig in high_sigs:
                    print(
                        f"  [拒绝] 疑似营销转化话术「{sig['pattern']}」：…{sig['context']}…",
                        file=sys.stderr,
                    )
                return 3

        checklist_path = build_review_checklist(
            doc, query, gaps, draft_score, versions, out_dir
        )
        manifest = build_manifest(
            doc, query, versions, draft_score, out_dir, gaps, checklist_path,
            postprocess_warnings, bottleneck_result,
        )
        manifest_path = save_manifest(manifest, out_dir)
    except OSError as exc:
        print(f"本地读写失败：{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"数据/配置异常：{exc}", file=sys.stderr)
        return 1

    print(f"草稿：{doc.title}（{len(doc.raw)} 字）")
    bn = manifest.get("bottleneck")
    if bn:
        print(f"瓶颈定位：{bn['layer_label']} —— {bn['reason']}")
        print(f"  方向：{bn['direction']}")
    print(f"草稿得分：{draft_score['overall']}/100 · {draft_score['grade']}（互动维度未发布）")
    for b in draft_score.get("blockers", []):
        print(f"  [阻断] {b}（总分已封顶）")
    for name in ("AI 可引用性", "内容质量 (E-E-A-T)", "关键词覆盖", "结构与格式"):
        print(f"  {name}: {draft_score['dimensions'][name]}/100")
    for name in versions:
        used = "LLM" if versions[name][1] else "规则脚手架"
        print(f"  {name} 版：{manifest['versions'][name]['file']}（{used}）")
    for warn in manifest["llm_postprocess_warnings"]:
        print(f"  [后处理] {warn}")
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
