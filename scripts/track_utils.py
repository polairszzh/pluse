"""track 通用辅助函数（从 search_ai.py 拆分，行为不变）

包含 Markdown/Shell 转义、我的内容标识匹配、未核实断言提取、
多采样聚合（Wilson 区间/多数派）、URL 规范化比较、argparse 校验。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shlex
from collections import Counter
from urllib.parse import urlsplit

from track_models import ProbeResult


def _md_cell(text: object) -> str:
    """Markdown 表格单元格转义：竖线 -> \\|，换行折叠为空格"""
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _shell_quote(value: str) -> str:
    """用 shlex.quote 生成可安全复现的 shell 参数（单引号包裹，$、反引号、反斜杠、引号均安全）"""
    return shlex.quote(str(value or ""))


def _parse_mine_ids(raw: str | None) -> list[str]:
    """解析 DB 里的 mine_ids JSON；空值/坏数据一律按 [] 处理，不让趋势功能崩溃"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


def _truncate(text: str, limit: int) -> str:
    """折叠空白并截断到 limit 字符"""
    compact = " ".join(str(text or "").split())
    return compact[:limit]


def _detect_mine(text: str, mine_ids: list[str]) -> list[str]:
    """在文本中查找我的内容标识（URL/标题/作者名）

    URL 标识：域名大小写不敏感、路径大小写敏感（URL 路径区分大小写，避免误报）；
    标题/作者名：不区分大小写。
    """
    if not mine_ids:
        return []
    lower_text = str(text or "").lower()
    stripped = [mid.strip() for mid in mine_ids if mid.strip()]
    matched = []
    for mid in stripped:
        if mid.lower().startswith(("http://", "https://")):
            if _url_present(mid, text):
                matched.append(mid)
        elif mid.lower() in lower_text:
            matched.append(mid)
    return list(dict.fromkeys(matched))


def _classify_cited_type(matched: list[str], owned_ids: list[str]) -> str | None:
    """mine 命中时区分引用类型：earned（原创被引）/ owned（转载或自有渠道被引）

    只要命中任一非 owned 标识，按更高价值口径记为 earned。
    """
    if not matched:
        return None
    owned = set(owned_ids or [])
    if any(m not in owned for m in matched):
        return "earned"
    return "owned"


_FACT_VERSION_RE = re.compile(
    r"(?:版本|version)\s*[:：]?\s*(\d+(?:\.\d+)+)", re.IGNORECASE
)
_FACT_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    # 数量级（万亿/亿/万）与对象单位（元/积分/用户/次…）解耦：
    # 任意组合自动成立（100亿次、5000万人、3亿次…），无需枚举全部组合
    r"((?:万亿|亿|万)?(?:元|积分|用户|粉丝|人次|人|次|下载|安装|GB|MB|TB|%)|万亿|亿|万)"
)


def _extract_fact_risks(answer: str, query: str, limit: int = 5) -> list[str]:
    """从 AI 回答中提取「关于品牌」的未核实数字断言（版本号、价格、数量等），供人工复核

    只做「风险提示」不做事实判定：回答里出现这类断言即列入清单，
    报告标注「未经核实」并附断言上下文，由发布前人工核查。
    年/月/天/小时/分钟等时间单位不提取，避免把「2026 年」「3 天前」「5 分钟后」当风险噪音。
    只提取品牌词（query）附近 ±80 字内的断言，避免把「需要 16GB 内存」等与品牌无关的
    数字当作风险（与字段注释「关于品牌」一致）。
    """
    text = str(answer or "")
    q = str(query or "").lower()
    if not q or q not in text.lower():
        return []
    # 品牌词出现位置 ±80 字构成候选窗口，窗口外断言不提取
    windows: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = text.lower().find(q, start)
        if idx < 0:
            break
        windows.append((max(0, idx - 80), min(len(text), idx + len(q) + 80)))
        start = idx + len(q)
    merged: list[tuple[int, int]] = []
    for w in sorted(windows):
        if merged and w[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], w[1]))
        else:
            merged.append(w)
    risks: list[str] = []
    seen: set[str] = set()
    patterns = (
        (_FACT_VERSION_RE, lambda m: f"版本 {m.group(1)}"),
        (_FACT_UNIT_RE, lambda m: f"{m.group(1)}{m.group(2)}"),
    )
    for pattern, fmt in patterns:
        for m in pattern.finditer(text):
            if not any(w[0] <= m.start() and m.end() <= w[1] for w in merged):
                continue
            label = fmt(m)
            if label in seen:
                continue
            seen.add(label)
            start = max(0, m.start() - 12)
            end = min(len(text), m.end() + 12)
            context = " ".join(text[start:end].split())
            risks.append(f"{label}（…{context}…）")
            if len(risks) >= limit:
                return risks
    return risks


def _wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项分布 Wilson score 区间（小样本也稳定），返回 (low, high)"""
    if n <= 0 or hits < 0 or hits > n:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _majority(values: list) -> object | None:
    """非 None 值的多数派（平局按首次出现顺序），全 None 返回 None"""
    cnt = Counter(v for v in values if v is not None)
    return cnt.most_common(1)[0][0] if cnt else None


def _aggregate_bool_tristate(values: list) -> bool | None:
    """布尔三态聚合：任一 True→True；有 False 无 True→False；全 None→None"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return bool(any(vals))


def _union_strings(groups: list[list[str]]) -> list[str]:
    """合并去重字符串列表（风险信号保守并集）"""
    return list(dict.fromkeys(x for group in groups for x in group))


def _aggregate_binary(valid_values: list) -> dict:
    """二元值（cited/mine_cited）聚合：n/hits/prob/ci + 严格多数判定

    _aggregate_samples 与 build_trend 共用，保证口径一致：
    概率 = 命中/有效样本；cited = 命中数 × 2 > 有效样本数（平局为否）；
    全部无效时 prob/cited 为 None（未知）。
    """
    n = len(valid_values)
    hits = sum(1 for v in valid_values if v)
    if n:
        prob = hits / n
        ci_low, ci_high = _wilson_interval(hits, n)
    else:
        prob, ci_low, ci_high = None, None, None
    return {
        "n": n,
        "hits": hits,
        "prob": prob,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "cited": hits * 2 > n if n else None,
    }


def _aggregate_samples(samples: list[ProbeResult]) -> ProbeResult:
    """把同一 run 的 N 个采样样本聚合为带概率/置信区间的单条结果

    cited / mine_cited / sentiment / cited_type 取多数派（仅有效样本）；
    competitor_matched / fact_risks 保守取任一命中 / 合并（风险信号不因多数而漏报）；
    prob / ci_low / ci_high / sample_count 基于有效样本（cited 非 None）命中数；
    cited=None 的失败/未配置样本不计入分母，全部无效时概率为 None（显示未知）。
    """
    if not samples:
        raise ValueError("aggregate requires at least one sample")
    agg = _aggregate_binary([
        s.cited for s in samples if s.cited is not None
    ])
    invalid = len(samples) - agg["n"]
    n, hits = agg["n"], agg["hits"]
    prob, ci_low, ci_high = agg["prob"], agg["ci_low"], agg["ci_high"]
    base = samples[0]
    cited = agg["cited"]
    # mine_cited 与 cited 统一严格多数（平局为否），避免偶样本平局时口径矛盾
    mine_cited = _aggregate_binary([
        s.mine_cited for s in samples if s.mine_cited is not None
    ])["cited"]
    sentiment = _majority([s.sentiment for s in samples])
    cited_type = _majority([s.cited_type for s in samples])
    competitor_matched = _aggregate_bool_tristate(
        [s.competitor_matched for s in samples]
    )
    fact_risks = _union_strings([s.fact_risks for s in samples])
    status = _majority([s.status for s in samples]) or "error"
    # 元信息取多数派：samples[0] 可能是少数失败样本，confidence/source 不能从它继承
    confidence = _majority([s.confidence for s in samples])
    source = _majority([s.source for s in samples]) or base.source
    if status != "ok":
        # 整体失败/未配置：cited/mine_cited 统一无效（表格显示未知，趋势不参与），
        # 避免与部分有效样本的命中结果口径不一致；上下文同时清空，
        # 不展示部分样本的命中/未命中上下文（与「未知」状态矛盾）
        cited = None
        mine_cited = None
        hit_context = ""
    else:
        # 上下文必须与最终判定一致：cited=True 取命中样本，False 取未命中样本，
        # 避免「否 (40%)」却展示命中内容；无匹配上下文时留空，不回退到相反判定的样本
        hit_context = next(
            (
                s.context
                for s in samples
                if s.cited is not None and bool(s.cited) == bool(cited) and s.context
            ),
            "",
        )
    # 样本原文关联命中状态，便于复核定位具体哪次采样命中
    answers = [
        {"answer": s.meta.get("answer"), "cited": s.cited, "sample_idx": s.sample_idx}
        for s in samples
        if isinstance(s.meta.get("answer"), str) and s.meta["answer"]
    ]
    meta = dict(base.meta)
    meta["sample_answers"] = answers
    meta["sample_count"] = n
    meta["sample_hits"] = hits
    meta["sample_invalid"] = invalid
    return ProbeResult(
        query=base.query,
        platform=base.platform,
        status=status,
        cited=cited,
        sentiment=sentiment,
        context=hit_context,
        source=source,
        degraded=_aggregate_binary([s.degraded for s in samples])["cited"],
        error=next(
            (s.error for s in samples if s.status == status and s.error),
            None,
        ),
        meta=meta,
        mine_cited=mine_cited,
        mine_ids=base.mine_ids,
        confidence=confidence,
        cited_type=cited_type,
        owned_ids=base.owned_ids,
        competitor_matched=competitor_matched,
        competitor_ids=base.competitor_ids,
        fact_risks=fact_risks,
        prob=prob,
        ci_low=ci_low,
        ci_high=ci_high,
        sample_count=n,
    )


_URL_TOKEN_RE = re.compile(r"https?://[^\s<>\"'，。；：！？（）【】「」『』《》]+", re.IGNORECASE)
_URL_TRAIL = ".,;:!?)]}"


def _url_identity(url: str) -> tuple[str, str, str] | None:
    """URL 规范化身份：(scheme, netloc, path) —— scheme/host 大小写不敏感，path 大小写敏感；
    尾斜杠等价（/p 与 /p/、根域与根域加斜杠），query 不参与比较（跟踪参数不造成漏报）"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    path = parts.path or ""
    if path in ("", "/"):
        path = ""
    elif path.endswith("/"):
        path = path[:-1]
    return (parts.scheme.lower(), parts.netloc.lower(), path)


def _url_present(url: str, text: str) -> bool:
    """URL 是否出现在文本中：抽取文本里的 URL token 后按规范化身份比较

    原则性实现，一次覆盖整类边界问题：
      - 前缀误匹配（example.com vs example.com.evil.com、/p/123 vs /p/1234 vs /p/123/456）
      - 根域不匹配子路径，但根域尾斜杠等价
      - 尾部句子标点（.,;:!?)]} 及中文标点）不参与比较
      - query 跟踪参数不参与比较
    """
    # 配置标识按原样规范化（不做尾部标点剥离）：用户明确给的 URL 末尾标点是路径的一部分；
    # 只有文本里抽取的 token 才剥离尾部标点（那里才有句子标点歧义）
    target = _url_identity(url)
    if target is None:
        return False
    for token in _URL_TOKEN_RE.findall(str(text or "")):
        if _url_identity(token.rstrip(_URL_TRAIL)) == target:
            return True
    return False


def _non_empty_query(value: str) -> str:
    """argparse 校验：查询词去掉首尾空白后不能为空"""
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("查询词不能为空")
    return value


def _positive_int(value: str) -> int:
    """argparse 校验：正整数（--samples 等）"""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是正整数")
    if n < 1:
        raise argparse.ArgumentTypeError("必须 >= 1")
    return n


def re_slug(text: str, max_len: int = 40) -> str:
    """查询词 -> 文件名 slug（保留中文）"""
    slug = re.sub(r"[^\w]+", "-", text or "untitled").strip("-")
    return slug[:max_len] or "untitled"
