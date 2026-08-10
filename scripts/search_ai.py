"""Pulse AI 平台引用监控执行层 —— /pulse track 的脚本端。

输入品牌/关键词，探测其在 DeepSeek、Kimi、豆包、元宝里的被提及情况：
  - DeepSeek：有公开 API（OpenAI 兼容），直接发固定探测问题，
    判断回答里是否出现品牌、情感倾向，并截取上下文。
  - Kimi / 豆包 / 元宝：无公开 API，用 Bing 搜索结果推断其检索库中
    是否存在品牌内容（存在信号，不是该平台的真实引用）。

每次探测结果写入 data/monitor.db（SQLite）；重跑同一品牌时与历史快照
对比，输出趋势。报告落盘 data/snapshots/track-*.md + track-*.json。

Phase 2 边界（诚实标注，不夸大）：
  - DeepSeek 被提及 = 回答正文出现品牌名（精确匹配），原始回答存入 meta 供人工复核；
  - Kimi/豆包/元宝 是搜索引擎存在信号推断，报告中必须保留这条局限说明。
"""
from __future__ import annotations

import argparse
import html as html_module
import json
import os
import re
import shlex
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
from audit import Recommendation
from verifier import dedupe_recommendations, detect_conflicts, sort_recommendations

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "snapshots"
DEFAULT_DB = PROJECT_ROOT / "data" / "monitor.db"

DEEPSEEK_BASE = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

PROBE_PROMPT = (
    "请搜索并介绍「{query}」：它是什么、有什么特点、有哪些值得注意的信息。"
    "如果可获取的信息有限，请如实说明。"
)

POSITIVE_WORDS = (
    "推荐", "优秀", "领先", "好用", "强大", "认可", "称赞",
    "好评", "值得", "高效", "出色", "首选",
)
NEGATIVE_WORDS = (
    "失望", "投诉", "诈骗", "骗局", "糟糕", "劣质", "后悔", "差评", "坑人", "翻车", "踩坑",
)

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# 否定前缀（正面词/负面词共用）：1 字、2 字、3 字三档
_NEG_1CHAR = ("不", "没", "无", "未")
_NEG_2CHAR = ("不太", "并不", "并非", "没有", "并无")
_NEG_3CHAR = ("谈不上", "说不上", "算不上")


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


_FACT_VERSION_RE = re.compile(r"(?:版本|version)\s*(\d+(?:\.\d+)+)", re.IGNORECASE)
_FACT_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(元|万|亿|积分|用户|粉丝|下载|安装|人|次|GB|MB|TB|%)"
)


def _extract_fact_risks(answer: str, limit: int = 5) -> list[str]:
    """从 AI 回答中提取未核实的数字断言（版本号、价格、数量等），供人工复核

    只做「风险提示」不做事实判定：回答里出现这类断言即列入清单，
    报告标注「未经核实」并附断言上下文，由发布前人工核查。
    年/月/天/小时/分钟等时间单位不提取，避免把「2026 年」「3 天前」「5 分钟后」当风险噪音。
    """
    text = str(answer or "")
    risks: list[str] = []
    seen: set[str] = set()
    patterns = (
        (_FACT_VERSION_RE, lambda m: f"版本 {m.group(1)}"),
        (_FACT_UNIT_RE, lambda m: f"{m.group(1)}{m.group(2)}"),
    )
    for pattern, fmt in patterns:
        for m in pattern.finditer(text):
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


def re_slug(text: str, max_len: int = 40) -> str:
    """查询词 -> 文件名 slug（保留中文）"""
    slug = re.sub(r"[^\w]+", "-", text or "untitled").strip("-")
    return slug[:max_len] or "untitled"


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """单个平台的一次探测结果"""

    query: str
    platform: str
    status: str                  # ok | no_key | error
    cited: bool | None           # 是否被提及；未知时为 None
    sentiment: str | None        # positive | neutral | negative | None
    context: str                 # 上下文/说明摘要
    source: str                  # api | search_inference
    degraded: bool               # 是否为降级信号（非真实引用）
    error: str | None = None
    meta: dict = field(default_factory=dict)
    mine_cited: bool | None = None      # 我的内容标识是否出现在探测结果中
    mine_ids: list[str] = field(default_factory=list)  # 本次检查的我的内容标识
    confidence: str | None = None      # confirmed(真实API) | likely(搜索推断) | hypothesis(启发式)
    cited_type: str | None = None       # mine 命中时的引用类型：earned(原创被引) | owned(转载/自有渠道被引)
    owned_ids: list[str] = field(default_factory=list)  # 本次检查的转载/自有渠道标识
    competitor_matched: bool | None = None  # 本次探测是否检测到竞品内容出现
    fact_risks: list[str] = field(default_factory=list)  # 回答中关于品牌的未核实数字断言


# --------------------------------------------------------------------------
# 配置加载
# --------------------------------------------------------------------------


def _load_key() -> str | None:
    """按顺序从环境变量和 .env 读取 DEEPSEEK_API_KEY / LLM_API_KEY"""
    candidates = ("DEEPSEEK_API_KEY", "LLM_API_KEY")
    for key in candidates:
        value = os.environ.get(key, "").strip().strip('"').strip("'")
        if value and value != "your_api_key_here":
            return value
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() in candidates:
                    value = v.strip().strip('"').strip("'")
                    if value and value != "your_api_key_here":
                        return value
    return None


# --------------------------------------------------------------------------
# DeepSeek 探测（真实 API）
# --------------------------------------------------------------------------


def classify_sentiment(text: str) -> str:
    """关键词启发式情感分类。

    模型：
      - 未被否定的正面词计正面（「优秀」「值得推荐」）；
      - 被否定的正面词计负面（「并不优秀」= 批评、「不推荐」= 负面）；
      - 被否定的负面词不计（「并非差评」「没有投诉」= 中性，不是负面）。
    正面分 >= 负面分 -> positive；负面分更大 -> negative；都没有 -> neutral。
    """
    pos = _count_mentions(text, POSITIVE_WORDS, negated=False)
    negated_pos = _count_mentions(text, POSITIVE_WORDS, negated=True)
    neg = _count_mentions(text, NEGATIVE_WORDS, negated=False)
    total_neg = neg + negated_pos
    if pos and pos >= total_neg:
        return "positive"
    if total_neg and total_neg > pos:
        return "negative"
    return "neutral"


def _is_negated(text: str, idx: int) -> bool:
    before1 = text[idx - 1] if idx > 0 else ""
    before2 = text[idx - 2:idx] if idx > 1 else ""
    before3 = text[idx - 3:idx] if idx > 2 else ""
    return (
        before1 in _NEG_1CHAR
        or before2 in _NEG_2CHAR
        or before3 in _NEG_3CHAR
    )


def _count_mentions(text: str, words: tuple[str, ...], negated: bool) -> int:
    """统计关键词出现次数；negated=True 只计紧邻否定前缀的命中，False 只计未被否定的"""
    count = 0
    for w in words:
        start = 0
        while True:
            idx = text.find(w, start)
            if idx < 0:
                break
            if _is_negated(text, idx) == negated:
                count += 1
            start = idx + len(w)
    return count


def probe_deepseek(
    query: str,
    timeout: int = 60,
    session: requests.Session | None = None,
    mine_ids: list[str] | None = None,
    owned_ids: list[str] | None = None,
    competitor_ids: list[str] | None = None,
) -> ProbeResult:
    """调用 DeepSeek API 探测话题是否被提及、我的内容（原创/转载）与竞品是否出现在回答中"""
    mine_ids = mine_ids or []
    owned_ids = owned_ids or []
    competitor_ids = competitor_ids or []
    key = _load_key()
    if key is None:
        return ProbeResult(
            query=query, platform="deepseek", status="no_key", cited=None,
            sentiment=None, context="未配置 DEEPSEEK_API_KEY / LLM_API_KEY，跳过真实调用",
            source="api", degraded=False,
            meta={"note": "在 .env 中配置 DEEPSEEK_API_KEY 后重跑可拿到真实引用判断"},
            mine_ids=mine_ids,
            owned_ids=owned_ids,
        )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": PROBE_PROMPT.format(query=query)}],
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    http = session or requests
    try:
        resp = http.post(DEEPSEEK_BASE, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return ProbeResult(
                query=query, platform="deepseek", status="error", cited=None,
                sentiment=None, context="DeepSeek 响应结构异常（非对象）", source="api",
                degraded=True,
                error=f"unexpected_json_type:{type(data).__name__}",
                meta={"note": "响应应为 JSON 对象（含 choices 数组），保留原始类型便于排查"},
                mine_ids=mine_ids,
                owned_ids=owned_ids,
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return ProbeResult(
                query=query, platform="deepseek", status="error", cited=None,
                sentiment=None, context="DeepSeek 响应结构异常（choices 缺失或元素非对象）",
                source="api", degraded=True,
                error="unexpected_choices_shape",
                meta={"note": "choices 应为非空列表且首个元素为对象，保留原始响应便于排查"},
                mine_ids=mine_ids,
                owned_ids=owned_ids,
            )
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return ProbeResult(
                query=query, platform="deepseek", status="error", cited=None,
                sentiment=None, context="DeepSeek 响应结构异常（message 非对象）",
                source="api", degraded=True,
                error="unexpected_message_shape",
                meta={"note": "message 应为对象（含 content），保留原始响应便于排查"},
                mine_ids=mine_ids,
                owned_ids=owned_ids,
            )
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            return ProbeResult(
                query=query, platform="deepseek", status="error", cited=None,
                sentiment=None, context="DeepSeek 响应结构异常（content 非字符串）",
                source="api", degraded=True,
                error="unexpected_content_type",
                meta={"note": "content 应为字符串或空，保留原始响应便于排查"},
                mine_ids=mine_ids,
                owned_ids=owned_ids,
            )
        answer = content or ""
    except requests.exceptions.RequestException as exc:
        return ProbeResult(
            query=query, platform="deepseek", status="error", cited=None,
            sentiment=None, context="DeepSeek API 调用失败", source="api", degraded=True,
            error=str(exc), meta={"note": "网络或服务异常，未写入有效探测"},
            mine_ids=mine_ids,
            owned_ids=owned_ids,
        )
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        return ProbeResult(
            query=query, platform="deepseek", status="error", cited=None,
            sentiment=None, context="DeepSeek 响应解析失败", source="api", degraded=True,
            error=str(exc), meta={"note": "响应结构与预期不符，保留原始响应便于排查"},
            mine_ids=mine_ids,
            owned_ids=owned_ids,
        )
    cited = query.lower() in answer.lower()
    mine_matched = _detect_mine(answer, mine_ids)
    competitor_matched = bool(_detect_mine(answer, competitor_ids)) if competitor_ids else None
    fact_risks = _extract_fact_risks(answer) if cited else []
    return ProbeResult(
        query=query, platform="deepseek", status="ok", cited=cited,
        sentiment=classify_sentiment(answer), context=_truncate(answer, 300),
        source="api", degraded=False, confidence="confirmed",
        meta={
            "answer": _truncate(answer, 1500),
            "model": DEEPSEEK_MODEL,
            "match": "exact_substring",
            "note": "被提及 = 回答正文出现品牌名（精确匹配），原始回答见 answer 字段供人工复核",
            "mine_checked": mine_ids,
            "mine_matched": mine_matched,
            "owned_ids": owned_ids,
            "competitor_matched": competitor_matched,
            "fact_risks": fact_risks,
        },
        mine_cited=bool(mine_matched) if mine_ids else None,
        mine_ids=mine_ids,
        cited_type=_classify_cited_type(mine_matched, owned_ids),
        owned_ids=owned_ids,
        competitor_matched=competitor_matched,
        fact_risks=fact_risks,
    )


# --------------------------------------------------------------------------
# 搜索引擎推断（Kimi / 豆包 / 元宝 的降级信号）
# --------------------------------------------------------------------------


BING_URL = "https://www.bing.com/search"
BING_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _parse_bing(html_text: str, limit: int = 10) -> list[dict]:
    """从 Bing 结果页 HTML 提取 (title, url, snippet)，解析失败时返回空列表"""
    results: list[dict] = []
    for block in re.findall(r'<li class="[^"]*b_algo[^"]*".*?</li>', html_text, re.DOTALL):
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not link:
            continue
        url = link.group(1)
        title = html_module.unescape(re.sub(r"<[^>]+>", "", link.group(2))).strip()
        snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
        snippet = (
            html_module.unescape(re.sub(r"<[^>]+>", "", snip.group(1))).strip()
            if snip else ""
        )
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def probe_search_inference(
    query: str,
    platform: str,
    timeout: int = 30,
    session: requests.Session | None = None,
    mine_ids: list[str] | None = None,
    owned_ids: list[str] | None = None,
    competitor_ids: list[str] | None = None,
) -> ProbeResult:
    """用 Bing 搜索结果推断平台检索库中的话题存在信号，并检查我的内容/竞品是否在其中"""
    mine_ids = mine_ids or []
    owned_ids = owned_ids or []
    competitor_ids = competitor_ids or []
    http = session or requests
    try:
        resp = http.get(
            BING_URL,
            params={"q": query, "setlang": "zh-hans", "count": "10"},
            headers={"User-Agent": BING_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=timeout,
        )
        resp.raise_for_status()
        html_text = resp.text
    except requests.exceptions.RequestException as exc:
        return ProbeResult(
            query=query, platform=platform, status="error", cited=None,
            sentiment=None, context="Bing 搜索请求失败", source="search_inference",
            degraded=True, error=str(exc),
            meta={"note": "网络或反爬拦截，未写入有效推断"},
            mine_ids=mine_ids,
            owned_ids=owned_ids,
        )

    results = _parse_bing(html_text)
    if not results:
        return ProbeResult(
            query=query, platform=platform, status="error", cited=None,
            sentiment=None, context="未解析到搜索结果（页面结构变化或触发反爬）",
            source="search_inference", degraded=True,
            error="no_results_parsed", meta={"html_len": len(html_text)},
            mine_ids=mine_ids,
            owned_ids=owned_ids,
        )

    # cited 只看标题+摘要：URL 常含关键词（如 github.com/openai/codex），拼入会误判「被提及」
    cited = False
    context = ""
    text_blobs = [f"{item.get('title', '')} {item.get('snippet', '')}" for item in results]
    for text_blob in text_blobs:
        if query.lower() in text_blob.lower():
            cited = True
            context = _truncate(text_blob, 300)
            break
    if not context:
        top = results[0]
        context = _truncate(f"{top.get('title', '')} {top.get('snippet', '')}", 300)
    # 我的内容标识匹配：URL 类标识扫 title+url+snippet（作者常以链接被收录），
    # 非 URL 类标识（标题/作者名/年份等）只扫 title+snippet，避免在 URL 里误命中
    url_mine_ids = [m for m in mine_ids if m.lower().startswith(("http://", "https://"))]
    text_mine_ids = [m for m in mine_ids if m not in url_mine_ids]
    url_matched = list(dict.fromkeys(
        matched for item in results
        for matched in _detect_mine(
            f"{item.get('title', '')} {item.get('url', '')} {item.get('snippet', '')}",
            url_mine_ids,
        )
    ))
    text_matched = list(dict.fromkeys(
        matched for blob in text_blobs for matched in _detect_mine(blob, text_mine_ids)
    ))
    mine_matched = list(dict.fromkeys(url_matched + text_matched))
    competitor_matched = None
    if competitor_ids:
        url_comp = [m for m in competitor_ids if m.lower().startswith(("http://", "https://"))]
        text_comp = [m for m in competitor_ids if m not in url_comp]
        comp_matched = list(dict.fromkeys(
            matched
            for item in results
            for matched in _detect_mine(
                f"{item.get('title', '')} {item.get('url', '')} {item.get('snippet', '')}",
                url_comp,
            )
        ))
        comp_matched += list(dict.fromkeys(
            matched for blob in text_blobs for matched in _detect_mine(blob, text_comp)
        ))
        competitor_matched = bool(comp_matched)
    return ProbeResult(
        query=query, platform=platform, status="ok", cited=cited,
        sentiment=None, context=context, source="search_inference", degraded=True,
        confidence="likely",
        meta={
            "results": results,
            "note": "搜索引擎存在信号，不等同于该平台真实引用；品牌名出现在标题/摘要即视为存在信号",
            "mine_checked": mine_ids,
            "mine_matched": mine_matched,
            "owned_ids": owned_ids,
            "competitor_matched": competitor_matched,
        },
        mine_cited=bool(mine_matched) if mine_ids else None,
        mine_ids=mine_ids,
        cited_type=_classify_cited_type(mine_matched, owned_ids),
        owned_ids=owned_ids,
        competitor_matched=competitor_matched,
    )


PLATFORMS = {
    "deepseek": {
        "label": "DeepSeek",
        "probe": lambda q, mine_ids=None, owned_ids=None, competitor_ids=None: probe_deepseek(
            q, mine_ids=mine_ids, owned_ids=owned_ids, competitor_ids=competitor_ids
        ),
        "note": "真实 API 探测（OpenAI 兼容接口）",
    },
    "kimi": {
        "label": "Kimi（月之暗面）",
        "probe": lambda q, mine_ids=None, owned_ids=None, competitor_ids=None: probe_search_inference(
            q, "kimi", mine_ids=mine_ids, owned_ids=owned_ids, competitor_ids=competitor_ids
        ),
        "note": "无公开 API，使用搜索引擎存在信号推断",
    },
    "doubao": {
        "label": "豆包（字节跳动）",
        "probe": lambda q, mine_ids=None, owned_ids=None, competitor_ids=None: probe_search_inference(
            q, "doubao", mine_ids=mine_ids, owned_ids=owned_ids, competitor_ids=competitor_ids
        ),
        "note": "无公开 API，使用搜索引擎存在信号推断",
    },
    "yuanbao": {
        "label": "元宝（腾讯）",
        "probe": lambda q, mine_ids=None, owned_ids=None, competitor_ids=None: probe_search_inference(
            q, "yuanbao", mine_ids=mine_ids, owned_ids=owned_ids, competitor_ids=competitor_ids
        ),
        "note": "无公开 API，使用搜索引擎存在信号推断",
    },
}


# --------------------------------------------------------------------------
# SQLite 存储
# --------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    platform TEXT NOT NULL,
    run_at TEXT NOT NULL,
    status TEXT NOT NULL,
    cited INTEGER,
    sentiment TEXT,
    context TEXT,
    source TEXT NOT NULL,
    degraded INTEGER NOT NULL DEFAULT 0,
    error TEXT,
      meta TEXT,
      mine_cited INTEGER,
      mine_ids TEXT,
      confidence TEXT,
      cited_type TEXT,
      owned_ids TEXT,
      competitor_matched INTEGER,
      fact_risks TEXT
);
CREATE INDEX IF NOT EXISTS idx_probes_query_platform_run
    ON probes(query, platform, run_at);
"""


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库补列：mine_cited / mine_ids / confidence / B3 引用质量字段（ALTER TABLE ADD COLUMN）"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(probes)").fetchall()}
    with conn:
        if "mine_cited" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN mine_cited INTEGER")
        if "mine_ids" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN mine_ids TEXT")
        if "confidence" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN confidence TEXT")
        if "cited_type" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN cited_type TEXT")
        if "owned_ids" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN owned_ids TEXT")
        if "competitor_matched" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN competitor_matched INTEGER")
        if "fact_risks" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN fact_risks TEXT")


def store_results(
    rows: list[ProbeResult],
    db_path: Path = DEFAULT_DB,
    run_at: str | None = None,
) -> str:
    """写入一次探测快照，返回本次 run_at"""
    # 微秒精度：避免同一秒内重跑两次时 run_at 相同，趋势/概览把两次运行合并
    run_at = run_at or datetime.now().astimezone().isoformat(timespec="microseconds")
    conn = connect(db_path)
    try:
        with conn:
            for r in rows:
                cited = 1 if r.cited is True else (0 if r.cited is False else None)
                mine_cited = 1 if r.mine_cited is True else (0 if r.mine_cited is False else None)
                competitor_matched = (
                    1 if r.competitor_matched is True
                    else (0 if r.competitor_matched is False else None)
                )
                conn.execute(
                    "INSERT INTO probes(query, platform, run_at, status, cited, sentiment,"
                    " context, source, degraded, error, meta, mine_cited, mine_ids, confidence,"
                    " cited_type, owned_ids, competitor_matched, fact_risks)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        r.query, r.platform, run_at, r.status, cited, r.sentiment,
                        r.context, r.source, 1 if r.degraded else 0, r.error,
                        json.dumps(r.meta if r.meta else {}, ensure_ascii=False),
                        mine_cited,
                        json.dumps(r.mine_ids or [], ensure_ascii=False),
                        r.confidence,
                        r.cited_type,
                        json.dumps(r.owned_ids or [], ensure_ascii=False),
                        competitor_matched,
                        json.dumps(r.fact_risks or [], ensure_ascii=False),
                    ),
                )
    finally:
        conn.close()
    return run_at


def load_history(
    query: str,
    platform: str | None = None,
    db_path: Path = DEFAULT_DB,
    limit: int = 200,
) -> list[dict]:
    """按时间倒序读取历史探测（新 -> 旧）"""
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if platform:
            cur = conn.execute(
                "SELECT * FROM probes WHERE query=? AND platform=? ORDER BY run_at DESC LIMIT ?",
                (query, platform, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM probes WHERE query=? ORDER BY run_at DESC LIMIT ?",
                (query, limit),
            )
        rows = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
    return rows


def build_trend(query: str, db_path: Path = DEFAULT_DB) -> dict:
    """按平台聚合历史快照，生成时间序列并找出引用状态的变化点"""
    rows = load_history(query, db_path=db_path, limit=1000)
    by_platform: dict[str, list[dict]] = {}
    for row in rows:
        by_platform.setdefault(row["platform"], []).append(row)

    series: dict[str, list[dict]] = {}
    changes: list[dict] = []
    for platform, items in by_platform.items():
        ordered = sorted(items, key=lambda r: r["run_at"])
        series[platform] = [
            {
                "run_at": r["run_at"],
                "cited": bool(r["cited"]) if r["cited"] is not None else None,
                "status": r["status"],
                "sentiment": r["sentiment"],
                "mine_cited": bool(r["mine_cited"]) if r["mine_cited"] is not None else None,
                "mine_checked": bool(_parse_mine_ids(r["mine_ids"])),
                "mine_ids": _parse_mine_ids(r["mine_ids"]),
                "cited_type": r["cited_type"],
                "owned_ids": _parse_mine_ids(r["owned_ids"]),
                "competitor_matched": (
                    bool(r["competitor_matched"])
                    if r["competitor_matched"] is not None else None
                ),
                "fact_risks": _parse_mine_ids(r["fact_risks"]),
            }
            for r in ordered
        ]
        prev: bool | None = None
        for item in ordered:
            cur_val = bool(item["cited"]) if item["cited"] is not None else None
            if cur_val is None:
                # 无效探测（未配置密钥/失败）不参与变化点，也不重置基线，
                # 这样隔次的有效对比仍能检出引用状态变化
                continue
            if prev is not None and prev != cur_val:
                changes.append({
                    "platform": platform,
                    "from": prev,
                    "to": cur_val,
                    "run_at": item["run_at"],
                })
            prev = cur_val
    return {
        "query": query,
        "series": series,
        "changes": changes,
        "total_runs": len({r["run_at"] for r in rows}),
    }


def build_delta(
    query: str,
    db_path: Path = DEFAULT_DB,
    trend: dict | None = None,
    platforms: list[str] | None = None,
) -> dict:
    """本次与上次有效快照的对比基线：引用新增/丢失、情感反转、我的内容变化"""
    trend = trend or build_trend(query, db_path=db_path)
    series = trend["series"]
    if platforms is not None:
        requested = set(platforms)
        series = {p: pts for p, pts in series.items() if p in requested}
    delta_platforms: dict[str, dict] = {}
    for platform, points in series.items():
        if not points:
            continue
        # 不依赖外部 trend 的构造顺序，显式按 run_at 升序取最新点
        points = sorted(points, key=lambda p: p["run_at"])
        latest = points[-1]
        if latest["status"] != "ok":
            # 本次探测无有效数据：不把历史两次快照的对比误报成「本次 vs 上次」
            delta_platforms[platform] = {
                "run_at": latest["run_at"],
                "status": latest["status"],
                "note": "本次探测无有效数据，未参与与上次对比",
            }
            continue
        valid = [p for p in points if p["status"] == "ok"]
        if len(valid) < 2:
            continue
        last, prev = valid[-1], valid[-2]
        item: dict = {"run_at": last["run_at"], "previous_run_at": prev["run_at"]}
        if last["cited"] is not None and prev["cited"] is not None:
            item["cited"] = last["cited"]
            item["cited_prev"] = prev["cited"]
            if last["cited"] and not prev["cited"]:
                item["cited_change"] = "added"
            elif not last["cited"] and prev["cited"]:
                item["cited_change"] = "lost"
            else:
                item["cited_change"] = "same"
        if last["sentiment"] and prev["sentiment"] and last["sentiment"] != prev["sentiment"]:
            item["sentiment_flip"] = f"{prev['sentiment']}→{last['sentiment']}"
        if last["mine_checked"] and prev["mine_checked"] and last["mine_cited"] is not None and prev["mine_cited"] is not None:
            if last["mine_cited"] and not prev["mine_cited"]:
                item["mine_change"] = "gained"
            elif not last["mine_cited"] and prev["mine_cited"]:
                item["mine_change"] = "lost"
        # lostprompt：上次被引用、本次未被引用但话题仍被提及、本次检出竞品内容出现
        # 上一轮竞品已命中（competitor_matched=True）时不判「夺走」——竞品一直在场，
        # 本轮丢失引用不是被替换；上一轮未查（None）按未命中处理，避免旧数据漏报
        if (
            last["mine_checked"] and prev["mine_checked"]
            and prev["mine_cited"] is True
            and last["mine_cited"] is False
            and last["cited"] is True
            and last["competitor_matched"] is True
            and prev["competitor_matched"] is not True
        ):
            item["competitor_replaced"] = True
            item["competitor_replaced_at"] = last["run_at"]
            # 上次明确未命中竞品（False）才算已确认夺走；
            # 上次未检查（None，首次启用 --competitor 的历史）按推断处理，待人工确认
            item["competitor_replaced_confirmed"] = prev["competitor_matched"] is False
        # 两个有效快照均无可对比数据（cited/sentiment/mine 全缺）时不写入空壳条目，
        # 避免 has_history=False 时渲染出全「—」的对比行
        if any(
            k in item
            for k in ("cited_change", "sentiment_flip", "mine_change", "competitor_replaced")
        ):
            delta_platforms[platform] = item
    # has_history 仅表示存在真实对比（引用/情感/我的内容任一变化或一致判定），
    # 不含「本次无有效数据」的 note 条目，避免 JSON 消费方误读
    compared = [
        item
        for item in delta_platforms.values()
        if any(
            k in item
            for k in ("cited_change", "sentiment_flip", "mine_change", "competitor_replaced")
        )
    ]
    return {"query": query, "platforms": delta_platforms, "has_history": bool(compared)}


# --------------------------------------------------------------------------
# 行动建议（每条带 falsifiability check）
# --------------------------------------------------------------------------


def build_recommendations(
    query: str,
    results: list[ProbeResult],
    delta: dict | None = None,
) -> list[Recommendation]:
    """根据探测结果生成带验证方式的 P0/P1/P2 行动清单"""
    recs: list[Recommendation] = []

    deepseek = next((r for r in results if r.platform == "deepseek"), None)
    if deepseek and deepseek.status == "ok" and deepseek.cited is False:
        recs.append(Recommendation(
            priority="P0",
            dimension="AI 引用",
            action=f"「{query}」在 DeepSeek 回答中未被提及：在内容里补充一段 130-170 字的自包含品牌段落"
                   "（结论前置 + 具体数据/案例支撑）",
            expected_impact="提升 DeepSeek 检索命中",
            falsifiability_check=f"重跑 /pulse track --query {_shell_quote(query)}，DeepSeek 被提及变为「是」",
        ))
    if deepseek and deepseek.status == "no_key":
        recs.append(Recommendation(
            priority="P1",
            dimension="数据可用性",
            action="在 .env 配置 DEEPSEEK_API_KEY 后重跑，才能拿到 DeepSeek 真实引用判断",
            expected_impact="补齐真实 API 探测",
            falsifiability_check="重跑后 DeepSeek 状态不再是「未配置密钥」",
        ))

    mine_checked = any(r.mine_ids for r in results)
    if mine_checked:
        if deepseek and deepseek.status == "ok" and deepseek.cited is True and deepseek.mine_cited is False:
            mine_txt = "、".join(deepseek.mine_ids[:3])
            recs.append(Recommendation(
                priority="P0",
                dimension="内容引用归属",
                action=f"「{query}」在 DeepSeek 回答中被提及，但你的内容（{mine_txt}）不在其中："
                       "围绕该话题发布/优化一篇自包含教程（每个 H2 一个问答对，首段 130-170 字直接给答案，"
                       "带具体数据/案例），确保标题覆盖话题关键词",
                expected_impact="让 AI 回答该话题时引用你的内容而不是别人的",
                falsifiability_check=f"重跑 /pulse track --query {_shell_quote(query)} "
                                     f"--mine {_shell_quote(deepseek.mine_ids[0])}，"
                                     "DeepSeek 我的内容变为「是」",
            ))
        inference_missing = [
            r for r in results
            if r.source == "search_inference" and r.status == "ok"
            and r.cited is True and r.mine_cited is False
        ]
        if inference_missing:
            names = "、".join(PLATFORMS[r.platform]["label"] for r in inference_missing[:3])
            mine_example = next((r.mine_ids[0] for r in inference_missing if r.mine_ids), "")
            recs.append(Recommendation(
                priority="P1",
                dimension="内容收录",
                action=f"{names} 对应话题在搜索生态中有内容，但你的内容不在其中："
                       "确保文章已在知乎等平台发布并被搜索收录（标题 + 首段覆盖话题关键词，正文带自包含答案块）",
                expected_impact="让话题搜索结果里出现你的内容",
                falsifiability_check=f"重跑 /pulse track --query {_shell_quote(query)} "
                                     f"--mine {_shell_quote(mine_example)}，"
                                     "搜索推断平台我的内容至少一个变为「是」",
            ))

    # B3 引用质量：lostprompt（竞品夺走）与 factcheck（未核实断言）
    delta = delta or {"platforms": {}}
    replaced = [
        (platform, item)
        for platform, item in delta["platforms"].items()
        if item.get("competitor_replaced")
    ]
    if replaced:
        names = "、".join(PLATFORMS.get(p, {}).get("label", p) for p, _ in replaced)
        recs.append(Recommendation(
            priority="P0",
            dimension="竞品夺走",
            action=f"在 {names} 上，你的内容上次被引用、本次被竞品替换："
                   "围绕差异化优势补充独家数据/实测/案例，并在标题与首段强化品牌锚定，"
                   "让 AI 能明确区分你与竞品",
            expected_impact="把 AI 引用从竞品拉回你的内容",
            falsifiability_check=f"重跑 /pulse track --query {_shell_quote(query)} "
                                 "--mine <你的内容URL> --competitor <竞品标识>，"
                                 "对应平台「竞品夺走」风险消失、我的内容变为「是」",
        ))
    risk_results = [
        r for r in results
        if r.status == "ok" and r.fact_risks
    ]
    if risk_results:
        risks = "、".join(
            f"{PLATFORMS[r.platform]['label']}：{'、'.join(r.fact_risks[:3])}"
            for r in risk_results[:3]
        )
        recs.append(Recommendation(
            priority="P1",
            dimension="信息风险",
            action=f"AI 回答中出现未核实断言（{risks}）：人工复核数字/版本真实性，"
                   "若与事实不符，准备纠偏内容或联系平台反馈",
            expected_impact="防止错误信息随 AI 回答扩散",
            falsifiability_check="重跑 /pulse track 后回答中的断言经人工核实一致，或已确认平台修正",
        ))

    failed = [r for r in results if r.status == "error"]
    if failed:
        names = "、".join(PLATFORMS[r.platform]["label"] for r in failed)
        recs.append(Recommendation(
            priority="P1",
            dimension="数据可用性",
            action=f"{names} 探测失败：检查网络或反爬拦截后重跑",
            expected_impact="补齐缺失平台的数据",
            falsifiability_check="重跑后失败平台状态恢复为「正常」",
        ))

    negative = [r for r in results if r.status == "ok" and r.sentiment == "negative"]
    if negative:
        names = "、".join(PLATFORMS[r.platform]["label"] for r in negative)
        recs.append(Recommendation(
            priority="P0",
            dimension="舆情",
            action=f"在 {names} 检测到负面提及：定位并核查负面内容来源，准备回应或补充正面材料",
            expected_impact="控制负面信号扩散",
            falsifiability_check="重跑 /pulse track，负面情感平台转为中性或正面",
        ))

    cited_platforms = [r for r in results if r.status == "ok" and r.cited is True]
    if cited_platforms and not any(r.sentiment == "negative" for r in cited_platforms):
        recs.append(Recommendation(
            priority="P2",
            dimension="持续监测",
            action="保持现有内容更新频率，两周后重跑对比引用变化",
            expected_impact="确认引用趋势稳定",
            falsifiability_check="两周后重跑，被提及平台数量不下降",
        ))

    recs = sort_recommendations(dedupe_recommendations(recs))
    for a, b, note in detect_conflicts(recs):
        print(f"  [冲突提示] {note}（矛盾建议保留，发布前人工复核取舍）", file=sys.stderr)
    return recs


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


STATUS_LABEL = {"ok": "正常", "no_key": "未配置密钥", "error": "失败"}
SENTIMENT_LABEL = {"positive": "正面", "neutral": "中性", "negative": "负面"}
CONFIDENCE_LABEL = {"confirmed": "Confirmed", "likely": "Likely", "hypothesis": "Hypothesis"}


def render_markdown(
    query: str,
    results: list[ProbeResult],
    trend: dict,
    recommendations: list[Recommendation],
    delta: dict | None = None,
) -> str:
    lines = ["# AI 平台引用跟踪报告", ""]
    lines.append(f"- **监测对象**：{query}")
    lines.append(f"- **运行时间**：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}")
    requested = [r.platform for r in results]
    lines.append(f"- **平台**：{'、'.join(PLATFORMS[p]['label'] for p in requested)}")
    lines.append("")

    lines.append("## 本次快照")
    lines.append("")
    with_mine = any(r.mine_ids for r in results)
    if with_mine:
        lines.append("| 平台 | 状态 | 置信度 | 被提及 | 我的内容 | 情感 | 上下文 / 说明 |")
        lines.append("|---|---|---|---|---|---|---|")
    else:
        lines.append("| 平台 | 状态 | 置信度 | 被提及 | 情感 | 上下文 / 说明 |")
        lines.append("|---|---|---|---|---|---|")
    for r in results:
        cited_txt = {True: "是", False: "否", None: "未知"}.get(r.cited, "未知")
        mine_txt = {True: "是", False: "否", None: "—"}.get(r.mine_cited, "—")
        if r.mine_cited is True:
            if r.cited_type == "earned":
                mine_txt += "（原创）"
            elif r.cited_type == "owned":
                mine_txt += "（转载）"
            else:
                mine_txt += "（未知）"
        sentiment = SENTIMENT_LABEL.get(r.sentiment or "", "—")
        conf = CONFIDENCE_LABEL.get(r.confidence or "", "—")
        if r.error:
            context = _md_cell(f"{r.context}（{r.error}）")
        else:
            context = _md_cell(r.context)
        row = (
            f"| {PLATFORMS[r.platform]['label']} | {STATUS_LABEL.get(r.status, r.status)} "
            f"| {conf} | {cited_txt} "
        )
        if with_mine:
            row += f"| {mine_txt} "
        row += f"| {sentiment} | {context} |"
        lines.append(row)

    lines.append("")
    delta = delta or {"platforms": {}}
    if delta.get("platforms"):
        # 仅渲染有 note 或任一对比键的行，避免外部传入空壳条目时出现全「—」行
        rows = [
            (platform, item)
            for platform, item in delta["platforms"].items()
            if item.get("note")
            or any(
                k in item
                for k in ("cited_change", "sentiment_flip", "mine_change", "competitor_replaced")
            )
        ]
        if rows:
            lines.append("## 与上次对比")
            lines.append("")
            lines.append("| 平台 | 引用变化 | 情感变化 | 我的内容 |")
            lines.append("|---|---|---|---|")
            cited_label = {"added": "新增被提及", "lost": "丢失被提及", "same": "无变化"}
            mine_label = {"gained": "新增被引用", "lost": "丢失被引用"}
            for platform, item in rows:
                label = PLATFORMS.get(platform, {}).get("label", platform)
                if item.get("note"):
                    lines.append(f"| {label} | {item['note']} | — | — |")
                    continue
                cited = cited_label.get(item.get("cited_change"), "—")
                flip = item.get("sentiment_flip", "—")
                mine = mine_label.get(item.get("mine_change"), "—")
                if item.get("competitor_replaced"):
                    mine = "丢失被引用（竞品夺走）"
                lines.append(f"| {label} | {cited} | {flip} | {mine} |")
            lines.append("")

    lines.append("## 趋势对比")
    lines.append("")
    if not trend["series"]:
        lines.append("暂无历史数据，本次为首个快照。")
    else:
        for platform in requested:
            points = trend["series"].get(platform, [])
            label = PLATFORMS[platform]["label"]
            if len(points) < 2:
                lines.append(f"- **{label}**：{len(points)} 次快照，重跑一次后生成趋势。")
                continue
            states = " → ".join(
                "是" if p["cited"] else "否" if p["cited"] is False else "未知"
                for p in points
            )
            line = f"- **{label}**（{len(points)} 次）：{states}"
            # 只要历史里有任何一次检查过 mine，就按 mine_ids 分组展示：
            # 不同次用不同标识时不会显示成假回归；未检查的运行显式标次数
            if any(p.get("mine_checked") for p in points):
                groups: dict[tuple, list] = {}
                for idx, p in enumerate(points, 1):
                    key = tuple(sorted(p.get("mine_ids") or []))
                    groups.setdefault(key, []).append((idx, p))
                multi_group = len(groups) > 1
                for key, group in groups.items():
                    states = " → ".join(
                        ("是" if p["mine_cited"] else "否" if p["mine_cited"] is False else "未知")
                        + (f"（第{idx}次）" if multi_group else "")
                        for idx, p in group
                    )
                    if key:
                        line += f"；我的内容({'、'.join(key)})：{states}"
                    else:
                        positions = "、".join(str(idx) for idx, _ in group)
                        line += f"；未检查 {len(group)} 次" + (f"（第{positions}次）" if multi_group else "")
            lines.append(line)
        if trend["changes"]:
            lines.append("")
            lines.append("**引用状态变化点**：")
            for ch in trend["changes"]:
                if ch["platform"] not in requested:
                    # 只展示本次实际运行平台的变化点，避免混入未运行平台的旧历史
                    continue
                label = PLATFORMS[ch["platform"]]["label"]
                lines.append(
                    f"- {label} 在 {ch['run_at']} 由「{'是' if ch['from'] else '否'}」"
                    f"变为「{'是' if ch['to'] else '否'}」"
                )
    lines.append("")

    # B3 风险提示：lostprompt（竞品夺走）与未核实断言（factcheck）
    risk_lines: list[str] = []
    for platform, item in (delta or {"platforms": {}})["platforms"].items():
        if item.get("competitor_replaced"):
            label = PLATFORMS.get(platform, {}).get("label", platform)
            suffix = (
                ""
                if item.get("competitor_replaced_confirmed")
                else "（推断：上次未检查竞品，待人工确认）"
            )
            risk_lines.append(
                f"- ⚠ **{label}**：上次被引用，本次被竞品替换{suffix}"
                f"（{item.get('competitor_replaced_at', '')}），建议补充差异化内容强化品牌锚定"
            )
    for r in results:
        if r.fact_risks:
            label = PLATFORMS[r.platform]["label"]
            risk_lines.append(
                f"- ⚠ **{label}** 回答中出现未核实断言：{'、'.join(r.fact_risks)}，"
                "建议人工复核后准备纠偏内容"
            )
    if risk_lines:
        lines.append("## 风险提示")
        lines.append("")
        lines.extend(risk_lines)
        lines.append("")

    lines.append("## 行动清单（每条都带验证方式）")
    lines.append("")
    if not recommendations:
        lines.append("当前没有需要优先处理的事项。")
    for priority in ("P0", "P1", "P2"):
        bucket = [r for r in recommendations if r.priority == priority]
        if not bucket:
            continue
        label = {"P0": "立即处理", "P1": "优先处理", "P2": "顺手优化"}[priority]
        lines.append(f"### {priority} · {label}")
        lines.append("")
        for i, rec in enumerate(bucket, 1):
            lines.append(f"{i}. **[ {_md_cell(rec.dimension)} ]** {_md_cell(rec.action)}")
            lines.append(f"   - 预期效果：{_md_cell(rec.expected_impact)}")
            lines.append(f"   - 验证方式：{_md_cell(rec.falsifiability_check)}")
        lines.append("")

    lines.append("## 数据说明")
    lines.append("")
    lines.append("- DeepSeek：真实 API 探测，被提及 = 回答正文出现品牌名（精确匹配），原始回答可在 JSON 快照的 meta.answer 复核。")
    lines.append("- Kimi / 豆包 / 元宝：无公开 API，使用 Bing 搜索结果推断检索库中的存在信号，**不等同于该平台真实引用**。")
    lines.append("- Kimi / 豆包 / 元宝 各自用 Bing 对同一查询词做搜索推断（结果通常相同），是检索库存在信号，不代表各平台各自的真实引用。")
    lines.append("- 传 --mine <你的内容标识>（URL/标题/作者名，可重复传多次，一次一个）时，额外判断 AI 回答/Bing 结果里是否出现你的内容；"
                 "URL 在搜索推断里更有效，标题/作者名在 AI 回答里更常见。")
    lines.append("- --mine-owned 传转载/自有渠道标识，命中时引用类型记为「转载（owned）」；"
                 "--mine 默认视为原创内容，命中记为「原创（earned）」。")
    lines.append("- --competitor 传竞品标识，用于 lostprompt（竞品夺走）分析："
                 "上次被引用、本次被竞品替换且话题仍被提及时会标出风险。")
    lines.append("- 「风险提示」中的未核实断言来自 AI 回答原文的数字/版本提取，只做风险提示不做事实判定，需人工复核。")
    lines.append("- URL 标识匹配规则：域名大小写不敏感、路径大小写敏感（URL 路径区分大小写）；标题/作者名不区分大小写。")
    lines.append("- 行动清单里的重跑命令为 POSIX shell 风格；PowerShell 可直接使用，但标识含英文单引号时"
                 "（shlex 会转义为 '\\''），需在 PowerShell 手动调整或改用 bash。")
    lines.append("- 每次运行写入 data/monitor.db，趋势来自同品牌的历史快照对比。")
    lines.append("")
    return "\n".join(lines)


def render_json(
    query: str,
    results: list[ProbeResult],
    trend: dict,
    recommendations: list[Recommendation],
    delta: dict | None = None,
) -> dict:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "query": query,
        "results": [r.__dict__ for r in results],
        "trend": trend,
        "delta": delta or {"platforms": {}, "has_history": False},
        "recommendations": [r.__dict__ for r in recommendations],
        "source_note": (
            "DeepSeek 为真实 API 探测；Kimi/豆包/元宝 为搜索引擎存在信号推断，不等同于真实引用"
        ),
    }


def save_report(
    query: str,
    results: list[ProbeResult],
    trend: dict,
    recommendations: list[Recommendation],
    out_dir: Path | None = None,
    delta: dict | None = None,
) -> list[Path]:
    out_dir = out_dir or SNAPSHOT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    slug = re_slug(query)
    md_path = out_dir / f"track-{slug}-{ts}.md"
    json_path = out_dir / f"track-{slug}-{ts}.json"
    md_path.write_text(render_markdown(query, results, trend, recommendations, delta), encoding="utf-8")
    json_path.write_text(
        json.dumps(render_json(query, results, trend, recommendations, delta), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return [md_path, json_path]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parse_platforms(raw: str | None) -> list[str]:
    if not raw:
        return list(PLATFORMS)
    parts = [s.strip().lower() for s in raw.split(",") if s.strip()]
    bad = [p for p in parts if p not in PLATFORMS]
    if bad:
        raise argparse.ArgumentTypeError(
            f"未知平台: {'、'.join(bad)}（可用: {'、'.join(PLATFORMS)}）"
        )
    return list(dict.fromkeys(parts))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse-track",
        description="Pulse AI 平台引用监控 —— 探测品牌在 DeepSeek/Kimi/豆包/元宝的被提及情况，"
                    "写入 monitor.db 并对比历史快照生成趋势",
    )
    parser.add_argument("--query", required=True, type=_non_empty_query, help="品牌名或关键词")
    parser.add_argument(
        "--mine",
        action="append",
        default=[],
        help="你的内容标识（URL/标题/作者名），可重复传多次（--mine <URL> --mine <昵称>），一次一个；"
             "传了才会额外判断 AI 回答/搜索结果里是否出现你的内容",
    )
    parser.add_argument(
        "--mine-owned",
        action="append",
        default=[],
        help="转载/自有渠道内容标识（区别于 --mine 原创内容），可重复传；"
             "命中时引用类型记为「转载（owned）」而非「原创（earned）」",
    )
    parser.add_argument(
        "--competitor",
        action="append",
        default=[],
        help="竞品内容标识（URL/标题/作者名），可重复传；"
             "传了会检测 AI 回答/搜索结果里是否出现竞品，用于 lostprompt（竞品夺走）分析",
    )
    parser.add_argument(
        "--platforms",
        type=_parse_platforms,
        help="逗号分隔的平台列表，默认全部（deepseek,kimi,doubao,yuanbao）",
    )
    parser.add_argument("--output", help="输出目录（默认 data/snapshots/）")
    parser.add_argument("--db", help="monitor.db 路径（默认 data/monitor.db，测试可指定临时库）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    platforms = args.platforms or list(PLATFORMS)
    earned_ids = list(dict.fromkeys(m.strip() for m in args.mine if m.strip()))
    owned_ids = list(dict.fromkeys(m.strip() for m in args.mine_owned if m.strip()))
    competitor_ids = list(dict.fromkeys(m.strip() for m in args.competitor if m.strip()))
    mine_ids = list(dict.fromkeys(earned_ids + owned_ids))
    db_path = Path(args.db) if args.db else DEFAULT_DB
    out_dir = Path(args.output) if args.output else None

    try:
        results = [
            PLATFORMS[p]["probe"](
                args.query,
                mine_ids=mine_ids,
                owned_ids=owned_ids,
                competitor_ids=competitor_ids,
            )
            for p in platforms
        ]
        store_results(results, db_path=db_path)
        trend = build_trend(args.query, db_path=db_path)
        delta = build_delta(args.query, db_path=db_path, trend=trend, platforms=platforms)
        recs = build_recommendations(args.query, results, delta=delta)
        paths = save_report(args.query, results, trend, recs, out_dir, delta)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"数据/配置异常：{exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    except OSError as exc:
        print(f"本地读写失败：{exc}", file=sys.stderr)
        return 1

    print(f"监测对象：{args.query}")
    for r in results:
        label = PLATFORMS[r.platform]["label"]
        mine = ""
        if r.mine_ids:
            mine_txt = {True: "是", False: "否", None: "—"}.get(r.mine_cited, "—")
            if r.mine_cited is True:
                if r.cited_type == "earned":
                    mine_txt += "（原创）"
                elif r.cited_type == "owned":
                    mine_txt += "（转载）"
                else:
                    mine_txt += "（未知）"
            mine = f" · 我的内容 {mine_txt}"
        if r.status == "ok":
            cited = "是" if r.cited else "否"
            extra = f" · 情感 {SENTIMENT_LABEL.get(r.sentiment or '', '—')}" if r.sentiment else ""
            conf = f" · 置信度 {CONFIDENCE_LABEL.get(r.confidence or '', '—')}"
            print(f"  {label}：{STATUS_LABEL[r.status]} · 被提及 {cited}{extra}{conf}{mine}")
        else:
            print(f"  {label}：{STATUS_LABEL.get(r.status, r.status)} · {_md_cell(r.context)}{mine}")
        if r.fact_risks:
            print(f"  [未核实断言] {label} 回答出现：{'、'.join(r.fact_risks)}（建议人工复核）")
    print(f"趋势对比：{trend['total_runs']} 次快照 · {len(trend['changes'])} 处引用状态变化")
    if delta["platforms"]:
        cited_label = {"added": "新增被提及", "lost": "丢失被提及", "same": "无变化"}
        for platform, item in delta["platforms"].items():
            label = PLATFORMS.get(platform, {}).get("label", platform)
            if item.get("note"):
                print(f"  {label}：{item['note']}")
                continue
            cited = cited_label.get(item.get("cited_change"))
            flip = item.get("sentiment_flip")
            mine = {"gained": "我的内容新增被引用", "lost": "我的内容丢失被引用"}.get(item.get("mine_change"), "")
            parts = [p for p in (cited, flip) if p]
            if item.get("competitor_replaced"):
                suffix = "" if item.get("competitor_replaced_confirmed") else "（推断）"
                parts.append(
                    f"竞品夺走{suffix}（{item.get('competitor_replaced_at', '')}）"
                )
            if parts or mine:
                body = "、".join(parts)
                if mine:
                    body = f"{body} · {mine}" if body else mine
                print(f"  与上次对比 · {label}：{body}")
    print(f"行动建议：{len(recs)} 条（P0={sum(1 for r in recs if r.priority == 'P0')}）")
    for p in paths:
        print(f"已保存：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
