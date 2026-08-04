"""Pulse 评分引擎 — 0-100 量化内容可见度

Phase 1: 基于 API 可用字段（ContentText 摘要 + 结构化互动数据）。
评分维度权重、子维度定义来自 docs/design.md 第 5 节。
"""
import re
from dataclasses import dataclass, field

# ── 常量 ────────────────────────────────────────────────

TITLE_OPT_MIN, TITLE_OPT_MAX = 15, 35             # 知乎标题最佳字数区间


# ── 打分输出 ────────────────────────────────────────────

@dataclass
class DimensionScore:
    """单个评分维度的分数 + 说明"""
    score: int                                # 0-100
    label: str                                # 维度名（中文）
    sub_scores: dict[str, int] = field(default_factory=dict)  # 子维度分
    details: list[str] = field(default_factory=list)


@dataclass
class AuditScores:
    """完整审计评分结果"""
    overall: int                              # 综合分 0-100
    grade: str                                # A+ 到 D
    ai_citability: int = 0
    content_quality: int = 0
    keyword_coverage: int = 0
    structure: int = 0
    engagement: int = 0
    sub_scores: dict[str, DimensionScore] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)


def grade(score: int) -> str:
    """分数 → 等级"""
    if score >= 90:
        return "A+"
    if score >= 85:
        return "A"
    if score >= 75:
        return "B+"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def clamp(v: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, v))


# ── 子维度评分函数 ──────────────────────────────────────

def _passage_citability(text: str) -> tuple[int, str]:
    """评估摘要中是否存在 AI 可引用的自包含段落

    无法做逐段分析（无全文），退而评估摘要整体的"段落感"。
    300-800 字的优质摘要对应原文通常有良好段落结构。
    """
    length = len(text)
    if length >= 500:
        return 85, "摘要丰富（≥500字），推断原文段落结构良好"
    if length >= 300:
        return 70, "摘要适中（300-500字），段落结构基本可引用"
    if length >= 150:
        return 50, "摘要偏短（150-300字），AI 可引用的素材有限"
    return 30, "摘要过短（<150字），不足以被 AI 作为独立来源引用"


def _qa_structure(title: str, text: str) -> tuple[int, str]:
    """评估问题-答案结构的对齐程度"""
    is_question = "?" in title or "？" in title
    opens_with_answer = False
    first_sentence = text.split("。")[0] if text else ""
    if len(first_sentence) > 15 and len(first_sentence) < 200:
        # 开篇直接给出结论或定义 — Q&A 友好
        opens_with_answer = True

    if is_question and opens_with_answer:
        return 90, "标题是问题，正文首句直接给出答案 — 最优 Q&A 结构"
    if is_question:
        return 75, "标题是问题，但正文首句未直接回答"
    if opens_with_answer:
        return 65, "标题不是问题，但首句直接给出核心观点"
    return 45, "缺少明确的问题-答案结构，不利于 AI 匹配用户问题"


def _citation_density(text: str) -> tuple[int, str]:
    """评估引用密度 — 文章是否引用了外部权威来源"""
    # 在摘要中检测引用/来源标记
    cite_patterns = [
        r"https?://", r"\[[\d,\s]+\]", r"参考", r"引用", r"来源", r"出处",
        r"据\S{2,4}[报说称指]", r"《[^》]+》", r"「[^」]+」",
    ]
    matches = sum(len(re.findall(p, text)) for p in cite_patterns)
    if matches >= 4:
        return 85, f"检测到 {matches} 处引用标记，引用密度高"
    if matches >= 2:
        return 65, f"检测到 {matches} 处引用标记"
    if matches >= 1:
        return 45, "引用标记极少，可能为纯观点输出"
    return 25, "未检测到任何引用标记，内容缺乏可追溯来源"


def _entity_presence(text: str, title: str) -> tuple[int, str]:
    """评估命名实体在文本中的出现频率"""
    entities = re.findall(
        r"(?:[A-Z][a-z]+[\s\-]){1,3}[A-Z][a-z]+|"           # 英文专有名词
        r"[A-Z]{2,}|"                                         # 英文缩写
        r"(?:阿里|腾讯|字节|百度|华为|美团|小米|京东|网易|滴滴|"
        r"知乎|小红书|抖音|B站|微信|公众号|"
        r"DeepSeek|Kimi|豆包|元宝|ChatGPT|Claude|Gemini|Perplexity)",
        text + title,
    )
    count = len(set(entities))
    if count >= 8:
        return 90, f"{count} 个独立实体，覆盖面广"
    if count >= 4:
        return 70, f"{count} 个独立实体"
    if count >= 1:
        return 50, f"仅 {count} 个实体，话题聚焦度不足"
    return 30, "未检测到明显实体，内容泛化"


def _data_citability(text: str) -> tuple[int, str]:
    """评估数据可引用性"""
    numbers = re.findall(r"\d+[%％\.]?\d*", text)
    stats = re.findall(r"\d+\.\d+|\d+%|\d+％|\d+亿|\d+万|\d+千", text)
    if len(stats) >= 3:
        return 85, f"包含 {len(stats)} 处统计数据（如百分比/数量级），AI 可直接引用"
    if len(numbers) >= 8:
        return 70, f"包含 {len(numbers)} 处数字，有一定数据密度"
    if len(numbers) >= 3:
        return 50, "包含少量数字"
    return 30, "缺乏可被 AI 引用的具体数据"


def _timeliness(updated_at: int | None) -> tuple[int, str]:
    """评估时效性"""
    if updated_at is None:
        return 40, "无更新时间信息"
    import time
    age_days = (time.time() - updated_at) / 86400
    if age_days < 30:
        return 90, f"发布于 {age_days:.0f} 天前，内容新鲜"
    if age_days < 90:
        return 75, f"发布于 {age_days:.0f} 天前，仍在时效窗口内"
    if age_days < 365:
        return 55, f"发布于 {age_days:.0f} 天前，时效性一般"
    return 35, f"发布于 {age_days:.0f} 天前，可能过时"


def score_ai_citability(
    title: str,
    content_text: str,
    updated_at: int | None = None,
) -> DimensionScore:
    """AI 可引用性评分（6 子维度加权）"""
    pc_s, pc_d  = _passage_citability(content_text)
    qa_s, qa_d  = _qa_structure(title, content_text)
    cd_s, cd_d  = _citation_density(content_text)
    ep_s, ep_d  = _entity_presence(content_text, title)
    dc_s, dc_d  = _data_citability(content_text)
    tm_s, tm_d  = _timeliness(updated_at)

    score = int(
        0.30 * pc_s + 0.20 * qa_s + 0.20 * cd_s +
        0.15 * ep_s + 0.10 * dc_s + 0.05 * tm_s
    )

    return DimensionScore(
        score=clamp(score),
        label="AI 可引用性",
        sub_scores={
            "Passage Citability": pc_s, "问题-答案结构": qa_s,
            "引用密度": cd_s, "实体存在感": ep_s,
            "数据可引用性": dc_s, "时效性": tm_s,
        },
        details=[pc_d, qa_d, cd_d, ep_d, dc_d, tm_d],
    )


# ── 内容质量 (E-E-A-T) ──────────────────────────────────

def _trust(text: str) -> tuple[int, str]:
    """评估可信度"""
    cite_count = len(re.findall(r"https?://|参考|引用|来源|出处|据\S{2,4}[报说称指]", text))
    disclosure = bool(re.search(r"声明|披露|免责|利益相关|笔者|本文|作者", text))
    score = 50
    if cite_count >= 3:
        score += 25
    elif cite_count >= 1:
        score += 15
    if disclosure:
        score += 15
        return score, "有来源引用 + 信息披露标记，可信度较高"
    return score, f"来源引用 {cite_count} 处" if cite_count else "缺少可验证的来源引用"


def _experience(text: str) -> tuple[int, str]:
    """评估经验信号"""
    first_person = bool(re.search(r"我[曾从在]|笔者|我们实践|我的经验|我做|我们做|实战|亲测", text))
    case_study = bool(re.search(r"案例|实战|项目|落地|上线|客户|甲方|乙方", text))
    original_data = len(re.findall(r"\d+\.\d+|\d+%|\d+亿|\d+万", text))
    score = 35
    reasons = []
    if first_person:
        score += 20
        reasons.append("第一人称经验叙述")
    if case_study:
        score += 20
        reasons.append("包含案例/实战标记")
    if original_data >= 2:
        score += 15
        reasons.append(f"{original_data} 条原创数据点")
    detail = "、".join(reasons) if reasons else "缺少第一手经验信号"
    return clamp(score), detail


def _expertise(text: str) -> tuple[int, str]:
    """评估专业度"""
    terms = re.findall(
        r"[A-Z]{2,8}(?:\s[A-Z][a-z]+)?|"                           # 英文术语
        r"(?:算法|模型|架构|框架|系统|引擎|平台|协议|接口|"
        r"架构|模块|组件|管道|索引|缓存|队列|"
        r"数据库|后端|前端|API|SDK|CLI|RAG|LLM|GEO|SEO)",
        text,
    )
    term_count = len(set(terms))
    if term_count >= 8:
        return 85, f"{term_count} 个专业术语，密度高"
    if term_count >= 4:
        return 65, f"{term_count} 个专业术语"
    if term_count >= 1:
        return 45, "专业术语密度偏低"
    return 30, "未检测到明显专业术语"


def _authority(author_name: str, author_badge: str) -> tuple[int, str]:
    """评估权威性（基于可用的作者信息）"""
    score = 30
    reasons = []
    if author_name:
        score += 20
        reasons.append("有署名")
    if author_badge:
        score += 30
        reasons.append(f"认证: {author_badge}")
    if not reasons:
        reasons.append("缺少作者信息")
    return clamp(score), "、".join(reasons)


def score_content_quality(
    content_text: str,
    author_name: str = "",
    author_badge: str = "",
) -> DimensionScore:
    """内容质量评分（E-E-A-T 中文改编，4 子维度加权）"""
    tr_s, tr_d = _trust(content_text)
    ex_s, ex_d = _experience(content_text)
    ep_s, ep_d = _expertise(content_text)
    au_s, au_d = _authority(author_name, author_badge)

    score = int(0.30 * tr_s + 0.25 * ex_s + 0.25 * ep_s + 0.20 * au_s)

    return DimensionScore(
        score=clamp(score),
        label="内容质量 (E-E-A-T)",
        sub_scores={"可信度": tr_s, "经验": ex_s, "专业度": ep_s, "权威性": au_s},
        details=[tr_d, ex_d, ep_d, au_d],
    )


# ── 关键词覆盖 ──────────────────────────────────────────

def score_keyword_coverage(
    title: str,
    content_text: str,
    keywords: list[str] | None = None,
) -> DimensionScore:
    """关键词覆盖评分"""
    if not keywords:
        return DimensionScore(score=50, label="关键词覆盖", details=["未提供目标关键词"])

    full_text = title + " " + content_text
    found, missing = [], []
    for kw in keywords:
        (found if kw.lower() in full_text.lower() else missing).append(kw)

    if not found:
        return DimensionScore(
            score=10, label="关键词覆盖",
            details=[f"未覆盖任何目标关键词: {', '.join(missing)}"],
        )

    rate = len(found) / len(keywords)
    score = clamp(int(rate * 80 + 10))          # 最少 10 分

    detail = f"覆盖 {len(found)}/{len(keywords)} ({rate:.0%})"
    if found:
        detail += f" — 命中: {', '.join(found[:5])}"
    if missing:
        detail += f" — 缺失: {', '.join(missing[:5])}"

    return DimensionScore(score=score, label="关键词覆盖", details=[detail])


# ── 结构 ────────────────────────────────────────────────

def score_structure(title: str, content_text: str) -> DimensionScore:
    """基于可用信号推断结构质量"""
    details = []
    score = 50

    title_len = len(title)
    if TITLE_OPT_MIN <= title_len <= TITLE_OPT_MAX:
        score += 15
        details.append(f"标题 {title_len} 字，在最佳区间")
    elif 10 <= title_len <= 50:
        score += 8
        details.append(f"标题 {title_len} 字，可接受")
    else:
        details.append(f"标题 {title_len} 字，偏{'短' if title_len < 10 else '长'}")

    para_count = content_text.count("\n") + 1
    if 5 <= para_count <= 15:
        score += 15
        details.append(f"~{para_count} 段，结构层次合理")
    elif para_count > 15:
        score += 5
        details.append(f"~{para_count} 段，段落偏多")

    if re.search(r"[•·\-—①②③]|[1-9]\.[\s]|[一二三四五六七八九十]、", content_text):
        score += 10
        details.append("使用了列表/分点结构")

    return DimensionScore(score=clamp(score), label="结构与格式", details=details)


# ── 互动 ────────────────────────────────────────────────

def score_engagement(
    votes: int,
    comments: int,
    favorites: int,
    benchmark_avg_votes: float = 0,
) -> DimensionScore:
    """相对于话题基准的互动评分"""
    details = []
    effective_avg = max(benchmark_avg_votes, 5.0)
    ratio = votes / effective_avg

    if ratio >= 2.0:
        score = 90
        label_text = f"赞同({votes}) 远超话题均值({effective_avg:.0f})"
    elif ratio >= 1.0:
        score = 70
        label_text = f"赞同({votes}) 高于话题均值"
    elif ratio >= 0.5:
        score = 50
        label_text = f"赞同({votes}) 低于话题均值({effective_avg:.0f})"
    elif ratio >= 0.1:
        score = 30
        label_text = f"赞同({votes}) 远低于话题均值({effective_avg:.0f})"
    else:
        score = 10
        label_text = f"赞同({votes}) 极少，内容未被发现"

    details.append(label_text)

    if votes > 0 and favorites > votes * 0.3:
        score = clamp(score + 8)
        details.append("收藏/赞同比 ≥ 0.3 — 高参考价值信号")

    if comments >= 5:
        score = clamp(score + 5)
        details.append(f"{comments} 条评论，有讨论热度")

    return DimensionScore(score=clamp(score), label="互动数据", details=details)


# ── 综合 ────────────────────────────────────────────────

def audit_article(
    title: str,
    content_text: str,
    votes: int = 0,
    comments: int = 0,
    favorites: int = 0,
    author_name: str = "",
    author_badge: str = "",
    updated_at: int | None = None,
    keywords: list[str] | None = None,
    benchmark_avg_votes: float = 0,
) -> AuditScores:
    """对单篇文章进行全维度评分

    Args:
        title:        文章标题
        content_text: API 返回的 ContentText 摘要 (300-800 字)
        votes:        VoteUpCount / LikeCount
        comments:     CommentCount
        favorites:    FavoriteCount
        author_name:  作者昵称（API 可能返回空）
        author_badge: 作者认证标识
        updated_at:   EditTime Unix 时间戳
        keywords:     目标关键词列表
        benchmark_avg_votes: 话题平均赞同数（来自 topic_benchmark）

    Returns:
        AuditScores — 包含 overall、grade、五个维度分、子维度细节
    """
    citability  = score_ai_citability(title, content_text, updated_at)
    quality     = score_content_quality(content_text, author_name, author_badge)
    kw          = score_keyword_coverage(title, content_text, keywords)
    struct      = score_structure(title, content_text)
    engagement  = score_engagement(votes, comments, favorites, benchmark_avg_votes)

    overall = int(
        0.35 * citability.score +
        0.25 * quality.score +
        0.20 * kw.score +
        0.10 * struct.score +
        0.10 * engagement.score
    )

    all_details = []
    for dim in [citability, quality, kw, struct, engagement]:
        all_details.append(f"**{dim.label}**: {dim.score}/100")
        for sub_name, sub_score in dim.sub_scores.items():
            all_details.append(f"  - {sub_name}: {sub_score}/100")
        for d in dim.details:
            all_details.append(f"  - {d}")

    return AuditScores(
        overall=overall,
        grade=grade(overall),
        ai_citability=citability.score,
        content_quality=quality.score,
        keyword_coverage=kw.score,
        structure=struct.score,
        engagement=engagement.score,
        sub_scores={
            "AI 可引用性": citability,
            "内容质量": quality,
            "关键词覆盖": kw,
            "结构与格式": struct,
            "互动数据": engagement,
        },
        details=all_details,
    )
