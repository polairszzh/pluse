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

import requests
from audit import Recommendation

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


def _truncate(text: str, limit: int) -> str:
    """折叠空白并截断到 limit 字符"""
    compact = " ".join(str(text or "").split())
    return compact[:limit]


def _detect_mine(text: str, mine_ids: list[str]) -> list[str]:
    """在文本中查找我的内容标识（URL/标题/作者名），不区分大小写"""
    if not mine_ids:
        return []
    lower_text = str(text or "").lower()
    stripped = [mid.strip() for mid in mine_ids if mid.strip()]
    return [mid for mid in stripped if mid.lower() in lower_text]


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
) -> ProbeResult:
    """调用 DeepSeek API 探测话题是否被提及、我的内容标识是否出现在回答中"""
    mine_ids = mine_ids or []
    key = _load_key()
    if key is None:
        return ProbeResult(
            query=query, platform="deepseek", status="no_key", cited=None,
            sentiment=None, context="未配置 DEEPSEEK_API_KEY / LLM_API_KEY，跳过真实调用",
            source="api", degraded=False,
            meta={"note": "在 .env 中配置 DEEPSEEK_API_KEY 后重跑可拿到真实引用判断"},
            mine_ids=mine_ids,
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
            )
        answer = content or ""
    except requests.exceptions.RequestException as exc:
        return ProbeResult(
            query=query, platform="deepseek", status="error", cited=None,
            sentiment=None, context="DeepSeek API 调用失败", source="api", degraded=True,
            error=str(exc), meta={"note": "网络或服务异常，未写入有效探测"},
            mine_ids=mine_ids,
        )
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        return ProbeResult(
            query=query, platform="deepseek", status="error", cited=None,
            sentiment=None, context="DeepSeek 响应解析失败", source="api", degraded=True,
            error=str(exc), meta={"note": "响应结构与预期不符，保留原始响应便于排查"},
            mine_ids=mine_ids,
        )
    cited = query.lower() in answer.lower()
    mine_matched = _detect_mine(answer, mine_ids)
    return ProbeResult(
        query=query, platform="deepseek", status="ok", cited=cited,
        sentiment=classify_sentiment(answer), context=_truncate(answer, 300),
        source="api", degraded=False,
        meta={
            "answer": _truncate(answer, 1500),
            "model": DEEPSEEK_MODEL,
            "match": "exact_substring",
            "note": "被提及 = 回答正文出现品牌名（精确匹配），原始回答见 answer 字段供人工复核",
            "mine_checked": mine_ids,
            "mine_matched": mine_matched,
        },
        mine_cited=bool(mine_matched) if mine_ids else None,
        mine_ids=mine_ids,
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
) -> ProbeResult:
    """用 Bing 搜索结果推断平台检索库中的话题存在信号，并检查我的内容是否在其中"""
    mine_ids = mine_ids or []
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
        )

    results = _parse_bing(html_text)
    if not results:
        return ProbeResult(
            query=query, platform=platform, status="error", cited=None,
            sentiment=None, context="未解析到搜索结果（页面结构变化或触发反爬）",
            source="search_inference", degraded=True,
            error="no_results_parsed", meta={"html_len": len(html_text)},
            mine_ids=mine_ids,
        )

    text_blobs = [f"{item['title']} {item['snippet']}" for item in results]
    mine_blobs = [f"{item['title']} {item['url']} {item['snippet']}" for item in results]

    # cited 只看标题+摘要：URL 常含关键词（如 github.com/openai/codex），拼入会误判「被提及」
    cited = False
    context = ""
    for text_blob in text_blobs:
        if query.lower() in text_blob.lower():
            cited = True
            context = _truncate(text_blob, 300)
            break
    if not context:
        top = results[0]
        context = _truncate(f"{top['title']} {top['snippet']}", 300)
    # 我的内容标识匹配允许查 URL（作者常以文章链接被收录），扫全部结果
    mine_matched = list(dict.fromkeys(
        matched for blob in mine_blobs for matched in _detect_mine(blob, mine_ids)
    ))
    return ProbeResult(
        query=query, platform=platform, status="ok", cited=cited,
        sentiment=None, context=context, source="search_inference", degraded=True,
        meta={
            "results": results,
            "note": "搜索引擎存在信号，不等同于该平台真实引用；品牌名出现在标题/摘要即视为存在信号",
            "mine_checked": mine_ids,
            "mine_matched": mine_matched,
        },
        mine_cited=bool(mine_matched) if mine_ids else None,
        mine_ids=mine_ids,
    )


PLATFORMS = {
    "deepseek": {
        "label": "DeepSeek",
        "probe": lambda q, mine_ids=None: probe_deepseek(q, mine_ids=mine_ids),
        "note": "真实 API 探测（OpenAI 兼容接口）",
    },
    "kimi": {
        "label": "Kimi（月之暗面）",
        "probe": lambda q, mine_ids=None: probe_search_inference(q, "kimi", mine_ids=mine_ids),
        "note": "无公开 API，使用搜索引擎存在信号推断",
    },
    "doubao": {
        "label": "豆包（字节跳动）",
        "probe": lambda q, mine_ids=None: probe_search_inference(q, "doubao", mine_ids=mine_ids),
        "note": "无公开 API，使用搜索引擎存在信号推断",
    },
    "yuanbao": {
        "label": "元宝（腾讯）",
        "probe": lambda q, mine_ids=None: probe_search_inference(q, "yuanbao", mine_ids=mine_ids),
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
    mine_ids TEXT
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
    """旧库补列：mine_cited / mine_ids（ALTER TABLE ADD COLUMN）"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(probes)").fetchall()}
    with conn:
        if "mine_cited" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN mine_cited INTEGER")
        if "mine_ids" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN mine_ids TEXT")


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
                conn.execute(
                    "INSERT INTO probes(query, platform, run_at, status, cited, sentiment,"
                    " context, source, degraded, error, meta, mine_cited, mine_ids)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        r.query, r.platform, run_at, r.status, cited, r.sentiment,
                        r.context, r.source, 1 if r.degraded else 0, r.error,
                        json.dumps(r.meta if r.meta else {}, ensure_ascii=False),
                        mine_cited,
                        json.dumps(r.mine_ids or [], ensure_ascii=False),
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
                "mine_checked": bool(json.loads(r["mine_ids"])) if r["mine_ids"] else False,
                "mine_ids": json.loads(r["mine_ids"]) if r["mine_ids"] else [],
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


# --------------------------------------------------------------------------
# 行动建议（每条带 falsifiability check）
# --------------------------------------------------------------------------


def build_recommendations(
    query: str,
    results: list[ProbeResult],
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

    recs.sort(key=lambda r: PRIORITY_ORDER.get(r.priority, 9))
    return recs


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


STATUS_LABEL = {"ok": "正常", "no_key": "未配置密钥", "error": "失败"}
SENTIMENT_LABEL = {"positive": "正面", "neutral": "中性", "negative": "负面"}


def render_markdown(
    query: str,
    results: list[ProbeResult],
    trend: dict,
    recommendations: list[Recommendation],
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
        lines.append("| 平台 | 状态 | 被提及 | 我的内容 | 情感 | 上下文 / 说明 |")
        lines.append("|---|---|---|---|---|---|")
    else:
        lines.append("| 平台 | 状态 | 被提及 | 情感 | 上下文 / 说明 |")
        lines.append("|---|---|---|---|---|")
    for r in results:
        cited_txt = {True: "是", False: "否", None: "未知"}.get(r.cited, "未知")
        mine_txt = {True: "是", False: "否", None: "—"}.get(r.mine_cited, "—")
        sentiment = SENTIMENT_LABEL.get(r.sentiment or "", "—")
        if r.error:
            context = _md_cell(f"{r.context}（{r.error}）")
        else:
            context = _md_cell(r.context)
        row = (
            f"| {PLATFORMS[r.platform]['label']} | {STATUS_LABEL.get(r.status, r.status)} "
            f"| {cited_txt} "
        )
        if with_mine:
            row += f"| {mine_txt} "
        row += f"| {sentiment} | {context} |"
        lines.append(row)
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
                for p in points:
                    key = tuple(sorted(p.get("mine_ids") or []))
                    groups.setdefault(key, []).append(p)
                for key, group in groups.items():
                    states = " → ".join(
                        "是" if p["mine_cited"] else "否" if p["mine_cited"] is False else "未知"
                        for p in group
                    )
                    if key:
                        line += f"；我的内容({'、'.join(key)})：{states}"
                    else:
                        line += f"；未检查 {len(group)} 次"
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
    lines.append("- 每次运行写入 data/monitor.db，趋势来自同品牌的历史快照对比。")
    lines.append("")
    return "\n".join(lines)


def render_json(
    query: str,
    results: list[ProbeResult],
    trend: dict,
    recommendations: list[Recommendation],
) -> dict:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "query": query,
        "results": [r.__dict__ for r in results],
        "trend": trend,
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
) -> list[Path]:
    out_dir = out_dir or SNAPSHOT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    slug = re_slug(query)
    md_path = out_dir / f"track-{slug}-{ts}.md"
    json_path = out_dir / f"track-{slug}-{ts}.json"
    md_path.write_text(render_markdown(query, results, trend, recommendations), encoding="utf-8")
    json_path.write_text(
        json.dumps(render_json(query, results, trend, recommendations), ensure_ascii=False, indent=2),
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
    mine_ids = [m.strip() for m in args.mine if m.strip()]
    db_path = Path(args.db) if args.db else DEFAULT_DB
    out_dir = Path(args.output) if args.output else None

    try:
        results = [PLATFORMS[p]["probe"](args.query, mine_ids=mine_ids) for p in platforms]
        store_results(results, db_path=db_path)
        trend = build_trend(args.query, db_path=db_path)
        recs = build_recommendations(args.query, results)
        paths = save_report(args.query, results, trend, recs, out_dir)
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
        if r.status == "ok":
            cited = "是" if r.cited else "否"
            extra = f" · 情感 {SENTIMENT_LABEL.get(r.sentiment or '', '—')}" if r.sentiment else ""
            degraded = " · Bing 推断" if r.degraded else ""
            mine = ""
            if r.mine_ids:
                mine = " · 我的内容 " + {True: "是", False: "否", None: "—"}.get(r.mine_cited, "—")
            print(f"  {label}：{STATUS_LABEL[r.status]} · 被提及 {cited}{extra}{degraded}{mine}")
        else:
            print(f"  {label}：{STATUS_LABEL.get(r.status, r.status)} · {_md_cell(r.context)}")
    print(f"趋势对比：{trend['total_runs']} 次快照 · {len(trend['changes'])} 处引用状态变化")
    print(f"行动建议：{len(recs)} 条（P0={sum(1 for r in recs if r.priority == 'P0')}）")
    for p in paths:
        print(f"已保存：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
