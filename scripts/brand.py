"""Pulse 品牌可见度命令执行层 — /pulse brand 的脚本端。

输入品牌名（个人名/产品名），输出品牌在知乎搜索里的可见度快照：
搜索存在率、份额占比、话题覆盖缺口、互动基准，以及带验证方式的 P0/P1/P2 行动清单。

Phase 1 边界：
  - 只做单次快照，不做时间趋势（那是 /pulse track + dashboard 的活）
  - 话题与竞品手动指定，不做自动发现
  - 「自己」识别 = 本人内容 URL/作者名交叉引用；「竞品」识别 = 作者名包含竞品名
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
import zhihu_api
from audit import Recommendation
from scorer import grade

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "snapshots"

_FALLBACK_ERRORS = (
    zhihu_api.ZhihuAPIError,
    requests.exceptions.RequestException,
    FileNotFoundError,
    ValueError,
    OSError,
)

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

DIMENSION_WEIGHTS = {
    "搜索存在率": 0.30,
    "份额占比": 0.20,
    "话题覆盖": 0.30,
    "互动基准": 0.20,
}


def _md_cell(text: object) -> str:
    """Markdown 表格单元格转义：竖线 -> \\|，换行折叠为空格"""
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _fmt_pct(pct: float) -> str:
    """百分比显示：整数不带小数，非整数保留一位（与阈值判断的 raw 一致）"""
    if abs(pct - round(pct)) < 1e-9:
        return f"{pct:.0f}"
    return f"{pct:.1f}"


def _round_half_up(value: float) -> int:
    """常规四舍五入（避免 round() 的银行家舍入：2.5 -> 3 而非 2）"""
    return int(value + 0.5) if value >= 0 else int(value - 0.5)


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass
class Dimension:
    """单个品牌维度得分"""

    name: str
    score: int
    detail: str
    raw: float = 0.0


@dataclass
class BrandResult:
    """品牌可见度审计结果"""

    brand: str
    overall: int
    grade: str
    brand_search_error: str | None = None
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    brand_search: list[dict] = field(default_factory=list)
    topic_coverage: list[dict] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 本人/竞品识别
# --------------------------------------------------------------------------


def build_own_index() -> tuple[set[str], set[str], bool, list[str]]:
    """拉本人内容，返回 (URL id 集合, 作者名集合, 是否可用, 提示信息)"""
    notes: list[str] = []
    try:
        items = zhihu_api.get_my_contents(content_type="all", limit=50).items
    except _FALLBACK_ERRORS as exc:
        return set(), set(), False, [f"本人内容拉取失败，「自己」识别不可用：{exc}"]
    url_ids = {zhihu_api.extract_article_id(it.url) for it in items if it.url}
    authors = {it.author_name for it in items if it.author_name}
    if not url_ids and not authors:
        notes.append("本人账号暂无创作内容（get_my_contents 为空），所有结果都标记为「不是自己」")
    return url_ids, authors, True, notes


def is_own(item: zhihu_api.ArticleItem, own_url_ids: set[str], own_authors: set[str]) -> bool:
    """URL 交叉引用优先，作者名兜底"""
    if item.url:
        article_id = zhihu_api.extract_article_id(item.url)
        if article_id and article_id in own_url_ids:
            return True
    return bool(item.author_name and item.author_name in own_authors)


def _own_key(item: zhihu_api.ArticleItem) -> str:
    """去重键：文章 ID > URL > 作者|标题（URL 可能为空时兜底）"""
    if item.url:
        article_id = zhihu_api.extract_article_id(item.url)
        if article_id:
            return article_id
        return item.url
    return f"{item.author_name}|{item.title}"


class OwnDedupe:
    """去重索引：URL 键与「作者|标题」键互通；带 URL 版本优先，与搜索顺序无关"""

    def __init__(self) -> None:
        self.items: dict[str, zhihu_api.ArticleItem] = {}
        self._titles: dict[tuple[str, str], str] = {}

    def add(self, item: zhihu_api.ArticleItem) -> None:
        key = _own_key(item)
        title_key = ((item.author_name or "").strip(), (item.title or "").strip())
        dup_key = None
        if key in self.items:
            dup_key = key
        elif title_key[0] and title_key in self._titles:
            dup_key = self._titles[title_key]
        if dup_key is None:
            self.items[key] = item
            if title_key[0]:
                self._titles[title_key] = key
            return
        existing = self.items[dup_key]
        if not existing.url and item.url:
            del self.items[dup_key]
            self.items[key] = item
            if title_key[0]:
                self._titles[title_key] = key

    def __len__(self) -> int:
        return len(self.items)


def _is_word_char(ch: str) -> bool:
    """ASCII 单词字符（字母/数字/下划线）；中文不算，便于 ASCII 竞品名在中文前后匹配"""
    return ch.isascii() and (ch.isalnum() or ch == "_")


def is_competitor(item: zhihu_api.ArticleItem, competitors: list[str]) -> str | None:
    """返回命中的竞品名（作者名包含竞品名，不区分大小写），否则 None

    ASCII 竞品名做边界匹配（避免 "AI" 误命中 "openai"/"detail"），
    中文竞品名保持子串匹配。
    """
    author = (item.author_name or "").lower()
    for comp in competitors:
        c = comp.lower()
        if not c:
            continue
        if c.isascii() and c.isalnum():
            for m in re.finditer(re.escape(c), author):
                before = author[m.start() - 1] if m.start() > 0 else ""
                after = author[m.end()] if m.end() < len(author) else ""
                if not _is_word_char(before) and not _is_word_char(after):
                    return comp
        elif c in author:
            return comp
    return None


# --------------------------------------------------------------------------
# 维度打分
# --------------------------------------------------------------------------


def score_presence(own_rank: int | None) -> Dimension:
    """搜索存在率：品牌词结果里自己的首条排名"""
    if own_rank is None:
        return Dimension("搜索存在率", 0, "品牌词搜索结果里没有你的内容", raw=0.0)
    if own_rank <= 1:
        return Dimension("搜索存在率", 100, f"品牌词结果首条就是你的内容（第 {own_rank} 位）", raw=100.0)
    if own_rank <= 3:
        return Dimension("搜索存在率", 85, f"你的内容排在品牌词结果第 {own_rank} 位", raw=85.0)
    if own_rank <= 5:
        return Dimension("搜索存在率", 70, f"你的内容排在品牌词结果第 {own_rank} 位", raw=70.0)
    return Dimension("搜索存在率", 55, f"你的内容排在品牌词结果第 {own_rank} 位（偏后）", raw=55.0)


def score_share(own_count: int, total: int) -> Dimension:
    """份额占比：品牌词 Top N 里自己占多少"""
    if total <= 0:
        return Dimension("份额占比", 0, "没有搜索结果可统计", raw=0.0)
    pct = own_count / total * 100
    return Dimension(
        "份额占比", _round_half_up(pct),
        f"品牌词 Top {total} 里你的内容占 {own_count} 条（{_fmt_pct(pct)}%）",
        raw=pct,
    )


def score_coverage(covered: int, total_topics: int, note: str | None = None) -> Dimension:
    """话题覆盖：指定话题里自己上榜的比例"""
    if total_topics <= 0:
        detail = note or "未指定话题，覆盖维度按中性处理；建议加 --topics 做覆盖分析"
        return Dimension("话题覆盖", 50, detail, raw=50.0)
    pct = covered / total_topics * 100
    return Dimension(
        "话题覆盖", _round_half_up(pct),
        f"{covered}/{total_topics} 个话题搜索 Top 10 里出现你的内容",
        raw=pct,
    )


def score_engagement(own_items: list[zhihu_api.ArticleItem], benchmark_avg_votes: float) -> Dimension:
    """互动基准：自己的内容平均赞同 vs 品牌词话题基准

    raw 为相对基准的百分比（100 = 与基准持平），与份额/覆盖的百分比语义一致。
    """
    if not own_items:
        return Dimension("互动基准", 10, "搜索结果里没有你的内容，无互动可评", raw=0.0)
    avg_votes = sum(it.vote_count for it in own_items) / len(own_items)
    effective_avg = max(float(benchmark_avg_votes), 5.0)
    ratio = avg_votes / effective_avg
    if ratio >= 2.0:
        score = 90
        label = f"你的内容平均赞同（{avg_votes:.0f}）远超品牌词基准（{effective_avg:.0f}）"
    elif ratio > 1.0:
        score = 70
        label = f"你的内容平均赞同（{avg_votes:.0f}）高于品牌词基准（{effective_avg:.0f}）"
    elif abs(ratio - 1.0) < 1e-9:
        score = 70
        label = f"你的内容平均赞同（{avg_votes:.0f}）与品牌词基准持平（{effective_avg:.0f}）"
    elif ratio >= 0.5:
        score = 50
        label = f"你的内容平均赞同（{avg_votes:.0f}）低于品牌词基准（{effective_avg:.0f}）"
    else:
        score = 30
        label = f"你的内容平均赞同（{avg_votes:.0f}）远低于品牌词基准（{effective_avg:.0f}）"
    return Dimension("互动基准", score, label, raw=ratio * 100.0)


def combine(dimensions: dict[str, Dimension]) -> tuple[int, str]:
    """按权重合成综合分与等级"""
    overall = _round_half_up(sum(dim.score * DIMENSION_WEIGHTS[name] for name, dim in dimensions.items()))
    return overall, grade(overall)


# --------------------------------------------------------------------------
# 建议
# --------------------------------------------------------------------------


def _push(recs: list[Recommendation], priority: str, dimension: str, action: str,
          impact: str, verify: str) -> None:
    recs.append(Recommendation(
        priority=priority,
        dimension=dimension,
        action=action,
        expected_impact=impact,
        falsifiability_check=verify,
    ))


def build_recommendations(
    brand: str,
    dimensions: dict[str, Dimension],
    own_count: int,
    total_brand_results: int,
    gaps: list[str],
    own_items: list[zhihu_api.ArticleItem],
    benchmark_avg_votes: float,
    topics_requested: bool = False,
    coverage_analyzed: bool = False,
    presence_data_ok: bool = True,
) -> list[Recommendation]:
    """按维度得分生成带验证方式的行动清单"""
    recs: list[Recommendation] = []
    presence = dimensions["搜索存在率"]
    share = dimensions["份额占比"]
    coverage = dimensions["话题覆盖"]
    engagement = dimensions["互动基准"]

    if presence_data_ok:
        if presence.score == 0:
            _push(
                recs, "P0", "搜索存在率",
                f"品牌词「{brand}」搜索结果里没有你的内容：在标题/正文加入品牌词，并回答带品牌词的相关问题",
                "先解决「搜得到」",
                f"重跑 /pulse brand --brand {brand}，搜索存在率 > 0（首条排名 ≤ 10）",
            )
        elif presence.score < 85:
            _push(
                recs, "P1", "搜索存在率",
                "你的内容在品牌词结果里排名偏后：提升该内容的互动与时效性，或发布更贴品牌词的新内容",
                "+搜索存在率 15-30 分",
                "重跑 /pulse brand，首条自己的内容排名进入前 3",
            )

        if total_brand_results > 0 and own_count > 0 and share.raw < 20:
            _push(
                recs, "P1", "份额占比",
                f"品牌词 Top {total_brand_results} 里你只占 {own_count} 条（{_fmt_pct(share.raw)}%）：围绕品牌词扩充内容数量",
                "+份额占比 10-30 分",
                "重跑 /pulse brand，份额占比 ≥ 20%",
            )

    if gaps:
        _push(
            recs, "P0", "话题覆盖",
            f"竞品在以下话题有内容而你没有：{'、'.join(gaps[:5])}——每个都是一篇选题",
            "补齐话题覆盖，兑现竞品差距分析",
            "重跑 /pulse brand，覆盖缺口清零（这些话题搜索 Top 10 出现你的内容）",
        )
    elif coverage_analyzed and coverage.raw < 80:
        _push(
            recs, "P1", "话题覆盖",
            f"话题覆盖只有 {_fmt_pct(coverage.raw)}%：在未覆盖话题发布内容，或改进现有内容的关键词",
            "+话题覆盖 20-40 分",
            "重跑 /pulse brand，覆盖达到 100%",
        )
    elif not coverage_analyzed:
        if topics_requested:
            _push(
                recs, "P2", "话题覆盖",
                "指定的话题全部搜索失败，本次无覆盖数据：检查配额/网络后重跑，或换用其他话题",
                "恢复覆盖维度",
                "重跑 /pulse brand，话题覆盖明细出现数据",
            )
        else:
            _push(
                recs, "P2", "话题覆盖",
                "本次未指定话题，覆盖维度没有分析；建议加 --topics 做覆盖与竞品差距分析",
                "让报告覆盖维度生效",
                "带 --topics 重跑，报告出现话题覆盖明细",
            )

    if presence_data_ok and own_items and engagement.score < 60:
        _push(
            recs, "P1", "互动基准",
            f"你的内容平均赞同（{sum(i.vote_count for i in own_items) / len(own_items):.0f}）"
            f"低于品牌词基准（{max(float(benchmark_avg_votes), 5.0):.0f}）：选题或标题未命中需求，换更具体的问题",
            "+互动基准 20-40 分",
            "重跑 /pulse brand，互动基准 ≥ 60；或 2 周内赞同提升 50%+",
        )

    recs.sort(key=lambda r: PRIORITY_ORDER.get(r.priority, 9))
    return recs


# --------------------------------------------------------------------------
# 取数与聚合
# --------------------------------------------------------------------------


def _snapshot_item(
    item: zhihu_api.ArticleItem,
    index: int,
    own: bool,
    competitor: str | None,
) -> dict:
    return {
        "rank": index + 1,
        "title": item.title,
        "url": item.url,
        "author": item.author_name,
        "mine": own,
        "competitor": competitor,
        "votes": item.vote_count,
        "ranking_score": item.ranking_score,
    }


def run_brand(
    brand: str,
    topics: list[str] | None = None,
    competitors: list[str] | None = None,
) -> BrandResult:
    """执行品牌可见度审计"""
    topics = topics or []
    competitors = competitors or []
    own_url_ids, own_authors, own_index_ok, notes = build_own_index()

    # 1) 品牌词搜索结果快照（失败降级：存在率/份额/互动置为「无法判断」）
    brand_search_error: str | None = None
    brand_items: list[zhihu_api.ArticleItem] = []
    try:
        brand_items = zhihu_api.search(brand, count=10).items
    except _FALLBACK_ERRORS as exc:
        brand_search_error = str(exc)
        notes.append(f"品牌词搜索失败：{exc}")
    brand_snapshot: list[dict] = []
    own_first_rank: int | None = None
    brand_dedupe = OwnDedupe()

    for idx, item in enumerate(brand_items):
        own = is_own(item, own_url_ids, own_authors)
        competitor = is_competitor(item, competitors)
        brand_snapshot.append(_snapshot_item(item, idx, own, competitor))
        if own:
            brand_dedupe.add(item)
            if own_first_rank is None:
                own_first_rank = idx + 1

    # 2) 话题覆盖 + 竞品差距
    topic_coverage: list[dict] = []
    covered_topics = 0
    searched_topics = 0
    gaps: list[str] = []
    for topic in topics:
        try:
            items = zhihu_api.search(topic, count=10).items
        except _FALLBACK_ERRORS as exc:
            notes.append(f"话题「{topic}」搜索失败，已跳过：{exc}")
            topic_coverage.append({
                "topic": topic, "own_count": -1, "competitors": {},
                "avg_votes": 0, "error": str(exc),
            })
            continue
        searched_topics += 1
        own_count = 0
        comp_counts: dict[str, int] = {}
        for item in items:
            if is_own(item, own_url_ids, own_authors):
                own_count += 1
            comp = is_competitor(item, competitors)
            if comp:
                comp_counts[comp] = comp_counts.get(comp, 0) + 1
        if own_count > 0:
            covered_topics += 1
        if own_count == 0 and comp_counts:
            gaps.append(topic)
        topic_coverage.append({
            "topic": topic,
            "own_count": own_count,
            "competitors": comp_counts,
            "avg_votes": round(sum(i.vote_count for i in items) / len(items), 1) if items else 0,
        })

    # 3) 基准：品牌词话题平均赞同
    benchmark_avg_votes = 0.0
    try:
        benchmark = zhihu_api.topic_benchmark(brand, count=10)
        benchmark_avg_votes = benchmark.get("avg_votes", 0) or 0.0
    except _FALLBACK_ERRORS:
        notes.append("品牌词基准拉取失败，互动维度按最低基准（5）计算")

    # 互动基准只看品牌词结果里的自己内容（去重后），不混入话题搜索
    own_all = list(brand_dedupe.items.values())
    own_brand_unique = len(brand_dedupe)

    # 4) 维度与综合
    coverage_note = None
    if topics and searched_topics == 0:
        coverage_note = "指定的话题全部搜索失败，覆盖维度按中性处理（详见数据说明）"
    data_ok = brand_search_error is None and own_index_ok
    if not data_ok:
        reason = "品牌词搜索失败" if brand_search_error else "本人内容识别不可用"
        dimensions = {
            "搜索存在率": Dimension("搜索存在率", 50, f"{reason}，存在率无法判断", raw=50.0),
            "份额占比": Dimension("份额占比", 50, f"{reason}，份额无法判断", raw=50.0),
            "话题覆盖": score_coverage(covered_topics, searched_topics, coverage_note),
            "互动基准": Dimension("互动基准", 50, f"{reason}，互动无法判断", raw=50.0),
        }
    else:
        dimensions = {
            "搜索存在率": score_presence(own_first_rank),
            "份额占比": score_share(own_brand_unique, len(brand_items)),
            "话题覆盖": score_coverage(covered_topics, searched_topics, coverage_note),
            "互动基准": score_engagement(own_all, benchmark_avg_votes),
        }
    overall, overall_grade = combine(dimensions)
    recs = build_recommendations(
        brand,
        dimensions,
        own_brand_unique,
        len(brand_items),
        gaps,
        own_all,
        benchmark_avg_votes,
        topics_requested=bool(topics),
        coverage_analyzed=searched_topics > 0,
        presence_data_ok=data_ok,
    )
    return BrandResult(
        brand=brand,
        overall=overall,
        grade=overall_grade,
        brand_search_error=brand_search_error,
        dimensions=dimensions,
        brand_search=brand_snapshot,
        topic_coverage=topic_coverage,
        recommendations=recs,
        notes=notes,
    )


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


def render_markdown(result: BrandResult, competitors: list[str], topics: list[str]) -> str:
    lines = ["# 品牌可见度报告", ""]
    lines.append(f"- **品牌**：{result.brand}")
    lines.append(f"- **竞品**：{'、'.join(competitors) if competitors else '-'}")
    lines.append(f"- **话题**：{'、'.join(topics) if topics else '-'}")
    lines.append(f"- **审计时间**：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    lines.append(f"## 综合得分：{result.overall}/100 · {result.grade}")
    lines.append("")
    lines.append("| 维度 | 得分 | 说明 |")
    lines.append("|---|---|---|")
    for name in ("搜索存在率", "份额占比", "话题覆盖", "互动基准"):
        dim = result.dimensions[name]
        lines.append(f"| {_md_cell(dim.name)} | {dim.score}/100 | {_md_cell(dim.detail)} |")
    if result.notes:
        lines.append("")
        lines.append("> " + "\n> ".join(result.notes))
    lines.append("")

    lines.append("## 品牌词搜索结果（Top 10 快照）")
    lines.append("")
    if result.brand_search_error:
        lines.append(f"品牌词搜索失败，无法获取快照：{_md_cell(result.brand_search_error)}")
    elif not result.brand_search:
        lines.append("品牌词没有搜到任何结果。")
    else:
        lines.append("| # | 自己 | 竞品 | 标题 | 作者 | 赞同 | 排名分 |")
        lines.append("|---|---|---|---|---|---|---|")
        for s in result.brand_search:
            mine = "✓" if s["mine"] else ""
            comp = _md_cell(s["competitor"] or "")
            lines.append(
                f"| {s['rank']} | {mine} | {comp} | {_md_cell(s['title'][:38])} | "
                f"{_md_cell(s['author'])} | {s['votes']} | {s['ranking_score']} |"
            )
    lines.append("")

    lines.append("## 话题覆盖明细")
    lines.append("")
    if not result.topic_coverage:
        lines.append("未指定话题（加 --topics 做覆盖分析）。")
        lines.append("")
    for tc in result.topic_coverage:
        topic = _md_cell(tc["topic"])
        lines.append(f"### {topic}")
        if tc.get("error"):
            lines.append(f"- 搜索失败已跳过：{_md_cell(tc['error'])}")
        else:
            comp_txt = "、".join(f"{_md_cell(k)} {v} 条" for k, v in tc["competitors"].items()) or "-"
            lines.append(
                f"- 自己：{tc['own_count']} 条 | 竞品：{comp_txt} | Top10 平均赞同：{tc['avg_votes']}"
            )
        lines.append("")

    lines.append("## 行动清单（每条都带验证方式）")
    lines.append("")
    if not result.recommendations:
        lines.append("当前没有需要优先处理的项。")
    for priority in ("P0", "P1", "P2"):
        bucket = [r for r in result.recommendations if r.priority == priority]
        if not bucket:
            continue
        label = {"P0": "立刻处理", "P1": "优先处理", "P2": "顺手优化"}[priority]
        lines.append(f"### {priority} · {label}")
        lines.append("")
        for i, rec in enumerate(bucket, 1):
            lines.append(f"{i}. **[ {_md_cell(rec.dimension)} ]** {_md_cell(rec.action)}")
            lines.append(f"   - 预期效果：{_md_cell(rec.expected_impact)}")
            lines.append(f"   - 验证方式：{_md_cell(rec.falsifiability_check)}")
        lines.append("")

    lines.append("## 数据说明")
    lines.append("")
    lines.append("- 数据来源：知乎开放平台 API 搜索（Top 10 可见切片）+ 本人内容接口。")
    lines.append("- 「自己」识别：本人内容 URL/作者名交叉引用；「竞品」识别：作者名包含竞品名（v1）。")
    lines.append("- 单次快照，无趋势；自动竞品发现留后续版本。")
    lines.append("")
    return "\n".join(lines)


def render_json(result: BrandResult, competitors: list[str], topics: list[str]) -> dict:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "brand": result.brand,
        "competitors": competitors,
        "topics": topics,
        "overall": result.overall,
        "grade": result.grade,
        "dimensions": {name: {"score": d.score, "detail": d.detail}
                       for name, d in result.dimensions.items()},
        "brand_search": result.brand_search,
        "topic_coverage": result.topic_coverage,
        "recommendations": [r.__dict__ for r in result.recommendations],
        "notes": result.notes,
        "source_note": "知乎开放平台 API 搜索 Top 10 可见切片；本人/竞品按作者识别。",
    }


def save_report(
    result: BrandResult,
    competitors: list[str],
    topics: list[str],
    out_dir: Path | None = None,
) -> list[Path]:
    out_dir = out_dir or SNAPSHOT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    slug = re_slug(result.brand)
    md_path = out_dir / f"brand-{slug}-{ts}.md"
    json_path = out_dir / f"brand-{slug}-{ts}.json"
    md_path.write_text(render_markdown(result, competitors, topics), encoding="utf-8")
    json_path.write_text(
        json.dumps(render_json(result, competitors, topics), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return [md_path, json_path]


def re_slug(brand: str, max_len: int = 40) -> str:
    """品牌名 -> 文件名 slug（保留中文）"""
    slug = re.sub(r"[^\w]+", "-", brand or "untitled").strip("-")
    return slug[:max_len] or "untitled"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse-brand",
        description="Pulse 品牌可见度 — 品牌在知乎搜索里的存在率/份额/覆盖缺口/互动基准",
    )
    parser.add_argument("--brand", required=True, help="品牌名（个人名或产品名）")
    parser.add_argument("--topics", help="要分析的话题，逗号分隔")
    parser.add_argument("--competitors", help="竞品名单，逗号分隔（按作者名匹配）")
    parser.add_argument("--output", help="输出目录（默认 data/snapshots/）")
    return parser


def _parse_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    topics = _parse_list(args.topics)
    competitors = _parse_list(args.competitors)
    out_dir = Path(args.output) if args.output else None
    try:
        result = run_brand(args.brand, topics, competitors)
        paths = save_report(result, competitors, topics, out_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (zhihu_api.ZhihuAPIError, requests.exceptions.RequestException) as exc:
        print(f"知乎 API 调用失败：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"本地读写失败：{exc}", file=sys.stderr)
        return 1

    print(f"品牌：{result.brand}")
    print(f"综合得分：{result.overall}/100 · {result.grade}")
    for name in ("搜索存在率", "份额占比", "话题覆盖", "互动基准"):
        dim = result.dimensions[name]
        print(f"  {dim.name}: {dim.score}/100 — {dim.detail}")
    print(f"行动建议：{len(result.recommendations)} 条（P0={sum(1 for r in result.recommendations if r.priority == 'P0')}）")
    for p in paths:
        print(f"已保存：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
