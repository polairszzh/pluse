"""Pulse 审计命令执行层 — /pulse audit 的脚本端。

把 zhihu_api 取数 + scorer 评分串成一条命令：
  1. 解析目标文章（URL / 本人内容 / 话题搜索）
  2. 拉取话题基准（平均赞同）
  3. 跑 0-100 五维评分
  4. 按低分子维度生成带 falsifiability check 的 P0/P1/P2 行动清单
  5. 落盘 Markdown 报告 + JSON 快照到 data/snapshots/

Phase 1 有意取舍：数据来自知乎开放平台 API 的 ContentText 摘要（300-800 字），
评分粒度为整篇，不做逐段（全文抓取被 zh-zse-ck 反爬拦截，留到 Phase 3）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
import zhihu_api
from fetch_zhihu_full import fetch_full_content
from scorer import AuditScores, audit_article, grade

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "snapshots"


# --------------------------------------------------------------------------
# 行动建议
# --------------------------------------------------------------------------


@dataclass
class Recommendation:
    """一条可验证的行动建议"""

    priority: str            # P0 | P1 | P2
    dimension: str           # 维度名（中文）
    action: str              # 具体动作
    expected_impact: str     # 预期效果
    falsifiability_check: str  # 怎么知道这条建议没效果


# 子维度阈值表：子分低于阈值且所属维度低于警戒线时才出建议，避免噪音。
CITABILITY_SUB_RULES = {
    "Passage Citability": (
        60,
        "摘要/正文信息量不足，AI 缺少可独立引用的素材",
        "扩写为 500 字以上的自包含段落，结论放在最前面",
        "+10-20 AI 可引用性",
        "重跑 /pulse audit，Passage Citability 达到 70+；或用 DeepSeek/Kimi 提问该话题，看是否引用本文观点",
    ),
    "问题-答案结构": (
        70,
        "标题不是具体问题，或首句没有直接给答案",
        "把标题改成用户会搜的具体问题，首句直接给出结论/定义",
        "+5-15 AI 可引用性",
        "重跑 /pulse audit，问题-答案结构达到 75+",
    ),
    "引用密度": (
        60,
        "引用/来源标注过少，AI 难以追根溯源",
        "添加 3 处以上可验证引用（官方文档、论文、数据页、链接）",
        "+10-20 AI 可引用性",
        "重跑 /pulse audit，引用密度达到 65+",
    ),
    "实体存在感": (
        60,
        "平台/产品/方法等命名实体覆盖少",
        "正文明确写出涉及的平台、产品、工具、方法的准确名称",
        "+5-15 AI 可引用性",
        "重跑 /pulse audit，实体存在感达到 70+",
    ),
    "数据可引用性": (
        60,
        "缺少可被 AI 直接引用的具体数据",
        "补充 3 组以上数字/百分比/数量级，并注明口径",
        "+5-15 AI 可引用性",
        "重跑 /pulse audit，数据可引用性达到 70+",
    ),
    "时效性": (
        60,
        "内容发布时间久，或缺少时间信息",
        "更新时效性数据，明确发布时间，必要时发布修订版",
        "+5-15 AI 可引用性",
        "重跑 /pulse audit，时效性达到 75+",
    ),
}

QUALITY_SUB_RULES = {
    "可信度": (
        65,
        "来源引用或披露声明不足",
        "补充可验证来源引用（至少 3 处）和作者/利益声明",
        "+10-20 内容质量",
        "重跑 /pulse audit，可信度达到 70+",
    ),
    "经验": (
        65,
        "缺少第一人称经验或案例",
        "加入实战案例、踩坑记录或一手数据",
        "+5-15 内容质量",
        "重跑 /pulse audit，经验达到 70+",
    ),
    "专业度": (
        65,
        "领域术语密度偏低",
        "引入该领域核心术语并准确使用，避免泛泛而谈",
        "+5-15 内容质量",
        "重跑 /pulse audit，专业度达到 70+",
    ),
    "权威性": (
        60,
        "作者信息/认证不足",
        "完善知乎作者主页、领域认证、历史作品链接",
        "+5-15 内容质量",
        "重跑 /pulse audit，权威性达到 70+；或在知乎搜索作者名能定位到主页",
    ),
}

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# 列表/分点结构检测：Markdown 无序列表（-/*）、Unicode 项目符号、数字与中文序号
LIST_PATTERN = re.compile(
    r"(?:^|\n)\s*[-*+]\s+|"
    r"[\u2022\u00b7\u2013\u2014]|"
    r"[1-9](?:\.\s|[、)])|"
    r"[一二三四五六七八九十]、"
)

# 降级路径可接受的失败：任何 API/网络/凭据问题都不应中断整个审计。
_FALLBACK_ERRORS = (
    zhihu_api.ZhihuAPIError,
    requests.exceptions.RequestException,
    FileNotFoundError,
    ValueError,
    OSError,
)


def _push(
    recs: list[Recommendation],
    priority: str,
    dimension: str,
    action: str,
    expected_impact: str,
    falsifiability_check: str,
) -> None:
    recs.append(
        Recommendation(
            priority=priority,
            dimension=dimension,
            action=action,
            expected_impact=expected_impact,
            falsifiability_check=falsifiability_check,
        )
    )


def build_recommendations(
    scores: AuditScores,
    item: zhihu_api.ArticleItem,
    benchmark: dict,
    keywords: list[str] | None = None,
) -> list[Recommendation]:
    """根据评分结果生成带验证方式的行动清单（P0 > P1 > P2）"""
    recs: list[Recommendation] = []
    citability = scores.sub_scores.get("AI 可引用性")
    quality = scores.sub_scores.get("内容质量")

    if citability is not None and citability.score < 75:
        for sub, (threshold, why, action, impact, verify) in CITABILITY_SUB_RULES.items():
            if citability.sub_scores.get(sub, 100) < threshold:
                _push(recs, "P1", "AI 可引用性", f"{action}（{why}）", impact, verify)

    if quality is not None and quality.score < 70:
        for sub, (threshold, why, action, impact, verify) in QUALITY_SUB_RULES.items():
            if quality.sub_scores.get(sub, 100) < threshold:
                _push(recs, "P1", "内容质量 (E-E-A-T)", f"{action}（{why}）", impact, verify)

    if keywords:
        full = f"{item.title} {item.content_text}".lower()
        missing = [kw for kw in keywords if kw.strip() and kw.lower() not in full]
        if missing:
            _push(
                recs,
                "P0",
                "关键词覆盖",
                f"在标题/正文自然加入缺失关键词：{', '.join(missing[:5])}",
                "补齐目标检索词覆盖，提升被 AI/搜索匹配的概率",
                "重跑 /pulse audit，关键词覆盖达到 80+ 且缺失词清零；或该话题搜索 Top 20 中出现本文",
            )

    title_len = len(item.title)
    if not 15 <= title_len <= 35:
        _push(
            recs,
            "P2",
            "结构",
            f"标题当前 {title_len} 字，不在 15-35 字最佳区间，压缩或补足核心信息",
            "提升标题信息密度与检索匹配",
            "重跑 /pulse audit，结构子分提升；或对比调整前后点击率",
        )
    para_count = item.content_text.count("\n") + 1
    if para_count < 5:
        _push(
            recs,
            "P2",
            "结构",
            f"仅约 {para_count} 段，信息未分层；拆成多个短段落并加小标题",
            "改善可读性，降低 AI 跳过概率",
            "重跑 /pulse audit，结构子分提升 10+",
        )
    if not LIST_PATTERN.search(item.content_text):
        _push(
            recs,
            "P2",
            "结构",
            "正文没有列表/分点结构；把并列要点改成列表，方便阅读和引用",
            "提升结构清晰度",
            "重跑 /pulse audit，结构子分提升 10+",
        )

    avg_votes = benchmark.get("avg_votes") or 0
    effective_avg = max(float(avg_votes), 5.0)
    ratio = item.vote_count / effective_avg if effective_avg else 0.0
    if scores.engagement < 60 and ratio < 0.5:
        if avg_votes:
            _push(
                recs,
                "P1",
                "互动数据",
                f"赞同（{item.vote_count}）明显低于话题均值（{avg_votes:.1f}），选题或标题未命中需求；换更具体的痛点标题或重新选题",
                "+互动分 20-40",
                "重跑 /pulse audit，互动达到 60+；或 2 周内赞同提升 50%+",
            )
        else:
            _push(
                recs,
                "P2",
                "互动数据",
                f"赞同（{item.vote_count}）偏低，文末增加一个具体问题引导讨论",
                "提升评论与互动",
                "重跑 /pulse audit，互动达到 60+；或 2 周内评论数提升",
            )

    recs.sort(key=lambda r: PRIORITY_ORDER.get(r.priority, 9))
    return recs


# --------------------------------------------------------------------------
# 取数
# --------------------------------------------------------------------------


def make_slug(title: str, max_len: int = 40) -> str:
    """标题 -> 文件名 slug（保留中文）"""
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title or "untitled").strip("-")
    return slug[:max_len] or "untitled"


def resolve_article(url: str, query: str | None = None) -> zhihu_api.ArticleItem | None:
    """按 URL 定位文章：先查本人内容，再退回搜索匹配（需 query）"""
    if not url:
        return None
    article_id = zhihu_api.extract_article_id(url)
    try:
        mine = zhihu_api.get_my_contents(content_type="all", limit=50)
    except _FALLBACK_ERRORS:
        mine = None
    if mine:
        for item in mine.items:
            if article_id and article_id in item.url:
                return item
    if query:
        return zhihu_api.find_article_by_url(query, url)
    return None


def load_my_articles(limit: int = 10) -> list[zhihu_api.ArticleItem]:
    """拉取本人最近创作（自动限制到 API 上限）"""
    return zhihu_api.get_my_contents(content_type="all", limit=max(1, min(limit, 50))).items


def audit_one(
    item: zhihu_api.ArticleItem,
    query: str | None = None,
    keywords: list[str] | None = None,
) -> tuple[AuditScores, dict, list[Recommendation]]:
    """对单篇文章完整评分（含话题基准）"""
    benchmark: dict = {}
    if query:
        try:
            benchmark = zhihu_api.topic_benchmark(query, count=10)
        except _FALLBACK_ERRORS:
            benchmark = {}
    scores = audit_article(
        title=item.title,
        content_text=item.content_text,
        votes=item.vote_count,
        comments=item.comment_count,
        favorites=item.favorite_count,
        author_name=item.author_name,
        author_badge=item.author_badge,
        updated_at=item.updated_at,
        keywords=keywords,
        benchmark_avg_votes=benchmark.get("avg_votes", 0),
    )
    recs = build_recommendations(scores, item, benchmark, keywords)
    return scores, benchmark, recs


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


def render_markdown(
    item: zhihu_api.ArticleItem,
    scores: AuditScores,
    benchmark: dict,
    recs: list[Recommendation],
    query: str | None = None,
    content_source: str = "api_summary",
    fetch_note: str | None = None,
) -> str:
    """生成 Markdown 审计报告"""
    lines = ["# 可见度审计报告", ""]
    lines.append(f"- **标题**：{item.title}")
    lines.append(f"- **链接**：{item.url}")
    author = item.author_name or "-"
    if item.author_badge:
        author += f"（{item.author_badge}）"
    lines.append(f"- **作者**：{author}")
    lines.append(f"- **类型**：{item.content_type or '-'}")
    lines.append(f"- **话题基准**：{query or '-'}")
    lines.append(f"- **审计时间**：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    cap_note = "（已封顶）" if scores.blockers else ""
    lines.append(f"## 综合得分：{scores.overall}/100 · {scores.grade}{cap_note}")
    if scores.blockers:
        lines.append("")
        lines.append("**阻断原因（一票封顶）**：")
        for b in scores.blockers:
            lines.append(f"- {b}")
    lines.append("")
    lines.append("| 维度 | 得分 | 等级 |")
    lines.append("|---|---|---|")
    for dim_score in scores.sub_scores.values():
        lines.append(f"| {dim_score.label} | {dim_score.score}/100 | {grade(dim_score.score)} |")
    if benchmark:
        lines.append("")
        lines.append(
            "基准（同一话题 Top 内容）：平均赞同 "
            f"{benchmark.get('avg_votes', 0)}，平均排名分 {benchmark.get('avg_ranking_score', 0)}"
        )
    lines.append("")

    lines.append("## 子维度明细")
    lines.append("")
    for dim_score in scores.sub_scores.values():
        lines.append(f"### {dim_score.label} — {dim_score.score}/100")
        lines.append("")
        for sub_name, sub_score in dim_score.sub_scores.items():
            lines.append(f"- {sub_name}：{sub_score}/100")
        for detail in dim_score.details:
            lines.append(f"  - {detail}")
        lines.append("")

    lines.append("## 行动清单（每条都带验证方式）")
    lines.append("")
    if not recs:
        lines.append("当前没有需要优先处理的项。")
    for priority in ("P0", "P1", "P2"):
        bucket = [r for r in recs if r.priority == priority]
        if not bucket:
            continue
        label = {"P0": "立刻处理", "P1": "优先处理", "P2": "顺手优化"}[priority]
        lines.append(f"### {priority} · {label}")
        lines.append("")
        for i, rec in enumerate(bucket, 1):
            lines.append(f"{i}. **[ {rec.dimension} ]** {rec.action}")
            lines.append(f"   - 预期效果：{rec.expected_impact}")
            lines.append(f"   - 验证方式：{rec.falsifiability_check}")
        lines.append("")

    lines.append("## 数据说明")
    lines.append("")
    source_note = {
        "browser": "- 数据来源：本机浏览器采集的完整正文（audit --full）+ 结构化互动数据。",
        "api_summary_fallback": "- 数据来源：API 摘要（浏览器全文抓取失败已降级）+ 结构化互动数据。",
        "api_summary": (
            "- 数据来源：知乎开放平台 API 的 ContentText 摘要（约 300-800 字）"
            " + 结构化互动数据（赞同/评论/收藏）。"
        ),
    }.get(content_source, "- 数据来源：API 摘要 + 结构化互动数据。")
    lines.append(source_note)
    granularity = (
        "- 评分粒度：整篇正文打分，非逐段。"
        if content_source == "browser"
        else "- 评分粒度：整篇摘要打分，非逐段（Phase 1 有意取舍，全文分析在 Phase 3 路线）。"
    )
    lines.append(granularity)
    lines.append("- 评分性质：AI 可引用性为规则推断，不代表真实 AI 平台引用情况；实测引用在 Phase 2 落地。")
    channel_note = {
        "browser": (
            "- 浏览器采集：只读 + 低频不批量；隐藏自动化特征（禁用自动化标记、"
            "移除 webdriver、UA 去 HeadlessChrome 标记——不伪造特定版本）；"
            "失败自动降级 API 摘要。"
        ),
        "api_summary_fallback": (
            "- 本次 --full 全文抓取未成功，已降级 API 摘要（原因见 CLI 输出）。"
        ),
        "api_summary": (
            "- 反爬说明：知乎 zh-zse-ck 拦截全文抓取，"
            "可用 `audit --url <url> --full` 尝试浏览器全文通道。"
        ),
    }.get(content_source, "- 数据说明：见 CLI 输出。")
    # 已尝试过 --full（如 URL 不适用被跳过）：由「全文抓取说明」一行承担，
    # 不输出独立的 channel_note，避免语义重复
    if not (content_source == "api_summary" and fetch_note):
        lines.append(channel_note)
    if fetch_note:
        lines.append(f"- 全文抓取说明：{fetch_note}")
    lines.append("")
    return "\n".join(lines)


def render_json(
    item: zhihu_api.ArticleItem,
    scores: AuditScores,
    benchmark: dict,
    recs: list[Recommendation],
    query: str | None = None,
    content_source: str = "api_summary",
    fetch_note: str | None = None,
) -> dict:
    """生成 JSON 快照（供 Dashboard/趋势分析）"""
    dims = {}
    for dim_score in scores.sub_scores.values():
        dims[dim_score.label] = {
            "score": dim_score.score,
            "grade": grade(dim_score.score),
            "sub_scores": dim_score.sub_scores,
            "details": dim_score.details,
        }
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "query": query,
        "article": {
            "title": item.title,
            "url": item.url,
            "content_type": item.content_type,
            "author_name": item.author_name,
            "author_badge": item.author_badge,
            "vote_count": item.vote_count,
            "comment_count": item.comment_count,
            "favorite_count": item.favorite_count,
        },
        "benchmark": benchmark,
        "scores": {
            "overall": scores.overall,
            "grade": scores.grade,
            "blockers": scores.blockers,
            "ai_citability": scores.ai_citability,
            "content_quality": scores.content_quality,
            "keyword_coverage": scores.keyword_coverage,
            "structure": scores.structure,
            "engagement": scores.engagement,
            "dimensions": dims,
        },
        "recommendations": [r.__dict__ for r in recs],
        "content_source": content_source,
        "fetch_note": fetch_note,
        "source_note": {
            "browser": "本机浏览器采集的完整正文（audit --full），整篇粒度评分，非逐段",
            "api_summary_fallback": "API 摘要（浏览器全文抓取失败已降级），整篇粒度评分，非逐段",
            "api_summary": "知乎开放平台 API ContentText 摘要（300-800 字），整篇粒度评分，非逐段",
        }.get(content_source, "未知内容来源，整篇粒度评分，非逐段"),
    }


def save_report(
    item: zhihu_api.ArticleItem,
    scores: AuditScores,
    benchmark: dict,
    recs: list[Recommendation],
    query: str | None = None,
    out_dir: Path | None = None,
    content_source: str = "api_summary",
    fetch_note: str | None = None,
) -> list[Path]:
    """报告落盘：Markdown + JSON，返回生成的文件路径列表"""
    out_dir = Path(out_dir) if out_dir else SNAPSHOT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    slug = make_slug(item.title)
    md_path = out_dir / f"audit-{slug}-{ts}.md"
    json_path = out_dir / f"audit-{slug}-{ts}.json"
    md_path.write_text(
        render_markdown(
            item, scores, benchmark, recs, query, content_source, fetch_note
        ),
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(
            render_json(
                item, scores, benchmark, recs, query, content_source, fetch_note
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return [md_path, json_path]


def _print_single_summary(
    item: zhihu_api.ArticleItem,
    scores: AuditScores,
    recs: list[Recommendation],
    paths: list[Path],
    content_source: str = "api_summary",
    fetch_note: str | None = None,
) -> None:
    print(f"标题：{item.title}")
    source_label = {
        "browser": "浏览器全文",
        "api_summary_fallback": "API 摘要（全文失败已降级）",
        "api_summary": "API 摘要",
    }.get(content_source, "未知")
    print(f"内容来源：{source_label}")
    if fetch_note:
        print(f"全文抓取说明：{fetch_note}")
    print(f"综合得分：{scores.overall}/100 · {scores.grade}")
    for b in scores.blockers:
        print(f"  [阻断] {b}（总分已封顶）")
    for dim_score in scores.sub_scores.values():
        print(f"  {dim_score.label}: {dim_score.score}/100")
    print(f"行动建议：{len(recs)} 条（P0={sum(1 for r in recs if r.priority == 'P0')}）")
    for p in paths:
        print(f"已保存：{p}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    """argparse 类型校验：必须是 >= 1 的整数"""
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("必须是 >= 1 的整数")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse-audit",
        description="Pulse 可见度审计 — 知乎单篇内容评分与行动建议",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="知乎文章/回答 URL")
    source.add_argument("--me", action="store_true", help="审计本人最近的创作内容")
    source.add_argument("--topic", help="搜索一个话题并审计返回的文章")
    parser.add_argument("--query", help="定位 URL 用的搜索关键词（URL 不在本人内容中时必须提供）")
    parser.add_argument("--keywords", help="目标关键词，逗号分隔")
    parser.add_argument("--limit", type=_positive_int, default=10, help="--me 拉取数量（默认 10，上限 50）")
    parser.add_argument("--index", type=int, help="--me 时只审计第 N 篇（从 0 开始）")
    parser.add_argument("--top", type=_positive_int, default=1, help="--topic 时审计前 N 篇（默认 1）")
    parser.add_argument("--output", help="输出目录（默认 data/snapshots/）")
    parser.add_argument(
        "--full",
        action="store_true",
        help="仅 --url 模式：用本机浏览器抓取完整正文后评分（失败自动降级 API 摘要）",
    )
    return parser


def _parse_keywords(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    keywords = [k.strip() for k in raw.split(",") if k.strip()]
    return keywords or None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.full and not args.url:
        print("--full 仅支持 --url 模式（需指定文章 URL）", file=sys.stderr)
        return 2
    keywords = _parse_keywords(args.keywords)
    out_dir = Path(args.output) if args.output else None

    try:
        if args.url:
            item = resolve_article(args.url, args.query)
            if item is None:
                print(f"未找到 URL 对应的内容：{args.url}", file=sys.stderr)
                if not args.query:
                    print("提示：URL 不在本人创作中时，请加 --query <关键词> 帮助定位", file=sys.stderr)
                return 2
            content_source = "api_summary"
            fetch_note = None
            if args.full:
                fetched = fetch_full_content(args.url)
                if "content" in fetched:
                    item.content_text = fetched["content"]
                    content_source = "browser"
                else:
                    err = fetched.get("error", "")
                    if "仅支持知乎" in err:
                        content_source = "api_summary"
                        fetch_note = f"--full 已跳过：{err}"
                        print(
                            f"  [提示] {err}（--full 已跳过，使用 API 摘要）",
                            file=sys.stderr,
                        )
                    else:
                        content_source = "api_summary_fallback"
                        fetch_note = f"--full 全文抓取失败：{err}"
                        print(
                            f"  [提示] 浏览器全文抓取失败（{err}），已降级 API 摘要",
                            file=sys.stderr,
                        )
            scores, benchmark, recs = audit_one(item, args.query, keywords)
            paths = save_report(
                item, scores, benchmark, recs, args.query, out_dir,
                content_source, fetch_note,
            )
            _print_single_summary(item, scores, recs, paths, content_source, fetch_note)
            return 0

        if args.me:
            items = load_my_articles(args.limit)
            if not items:
                print("本人创作列表为空（API 未返回内容）。", file=sys.stderr)
                return 2
            if args.index is not None:
                if not 0 <= args.index < len(items):
                    print(f"--index {args.index} 超出范围（共 {len(items)} 篇）。", file=sys.stderr)
                    return 2
                items = [items[args.index]]
            for item in items:
                scores, benchmark, recs = audit_one(item, None, keywords)
                paths = save_report(item, scores, benchmark, recs, None, out_dir)
                _print_single_summary(item, scores, recs, paths)
            return 0

        items = zhihu_api.search(args.topic, count=max(1, min(args.top, 10))).items
        if not items:
            print(f"话题「{args.topic}」没有搜索到内容。", file=sys.stderr)
            return 2
        for item in items[: args.top]:
            scores, benchmark, recs = audit_one(item, args.topic, keywords)
            paths = save_report(item, scores, benchmark, recs, args.topic, out_dir)
            _print_single_summary(item, scores, recs, paths)
        return 0
    except zhihu_api.AuthError as exc:
        print(f"知乎鉴权失败：{exc}", file=sys.stderr)
        return 1
    except zhihu_api.QuotaExceeded as exc:
        print(f"知乎配额/频率限制：{exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (zhihu_api.ZhihuAPIError, requests.exceptions.RequestException) as exc:
        print(f"知乎 API 调用失败：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"本地读写失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
