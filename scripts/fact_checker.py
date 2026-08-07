"""置信度防火墙：对草稿中的可公开核实的声明型数据做多源交叉验证。

三级判定：
  - confirmed：至少一个来源重现断言数字 → 放行（权威源加分）
  - conflict：搜索到矛盾信号（否定/辟谣且未重现断言）→ 拒绝（由调用方 exit 3）
  - unverified：无可靠来源重现 → 标注不确定性，不阻断

原则（roadmap 数据可验证性 · 置信度防火墙）：
  - 确定性多源交叉比对（复用 Bing 搜索），LLM 不做事实裁判；
  - 只验证「品牌/产品 + 数字」类公开声明，第一手经验数据（「我上周处理了 3000 行」）不验证；
  - 只做事实核查，不把搜索结果搬进生成内容。
"""
from __future__ import annotations

import re
import urllib.parse

import requests
from search_ai import BING_UA, BING_URL, _parse_bing

AUTHORITY_SUFFIXES = (".gov.cn", ".gov", ".edu.cn", ".edu")
AUTHORITY_HOSTS = {
    "zhihu.com", "baike.baidu.com", "baike.sogou.com",
    "wikipedia.org", "zh.wikipedia.org", "xinhuanet.com", "people.com.cn",
    "codebuddy.cn",
}

REJECT_SIGNAL_RE = re.compile(r"(不存在|并非|假的|谣言|辟谣|错误信息|不实)")
FIRST_PERSON_RE = re.compile(r"(我|我们|本人|实测|亲测)")

UNIT_FACT_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:多\s*)?(?:积分|元|分钟|小时|天|秒|GB|MB|%|万|亿|字|行))"
)
VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")

# 高危风险领域：误导会造成实际伤害（医疗健康、教育招考），无法核实强制高危
HIGH_RISK_PATTERNS = {
    "医学/健康": r"(药|治疗|偏方|疫苗|治愈|降糖|降压|养生)",
    "教育招考": r"(高考|中考|考研|招考|分数线|招生简章|录取)",
}

# 中危风险领域：信息经常变动（价格政策、软件版本），无法核实标 medium 提示即可
MEDIUM_RISK_PATTERNS = {
    "价格/政策": r"(价格|收费|优惠|积分|政策|计费)",
    "软件版本": r"\d+\.\d+(?:\.\d+)?",
}


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _host_authority(host: str) -> int:
    """来源权威度：2=官方/权威（政府/教育/百科/权威媒体），1=普通网页"""
    if host.endswith(AUTHORITY_SUFFIXES) or host in AUTHORITY_HOSTS:
        return 2
    return 1


def extract_fact_candidates(text: str) -> list[dict]:
    """提取 (fact, context)：数字断言 + 所在句上下文；第一手经验断言跳过，同断言去重"""
    seen: dict[str, str] = {}

    def add(fact: str, start: int, end: int) -> None:
        if FIRST_PERSON_RE.search(sentence):
            return  # 第一手经验不做外部验证
        if fact not in seen:
            seen[fact] = sentence

    for m in UNIT_FACT_RE.finditer(text or ""):
        sentence = _sentence_around(text, m.start(), m.end())
        add(re.sub(r"\s+", "", m.group(1)), m.start(), m.end())
    for m in VERSION_RE.finditer(text or ""):
        sentence = _sentence_around(text, m.start(), m.end())
        add(m.group(1), m.start(), m.end())
    return [{"fact": f, "context": c} for f, c in seen.items()]


def _sentence_around(text: str, start: int, end: int) -> str:
    """返回数字断言所在句（按中文句号/分号/换行切分）"""
    left = max(
        text.rfind("。", 0, start), text.rfind("；", 0, start),
        text.rfind("\n", 0, start),
    ) + 1
    right_cands = [
        i for i in (text.find("。", end), text.find("；", end), text.find("\n", end))
        if i != -1
    ]
    right = min(right_cands) if right_cands else len(text)
    return text[left:right].strip()


def risk_flag(context: str) -> str | None:
    """风险领域标记：先查高危（医学/招考），再查中危（价格政策/版本）；未命中返回 None"""
    for name, pattern in HIGH_RISK_PATTERNS.items():
        if re.search(pattern, context or ""):
            return name
    for name, pattern in MEDIUM_RISK_PATTERNS.items():
        if re.search(pattern, context or ""):
            return name
    return None


def risk_severity(risk: str | None) -> str:
    """风险领域严重度：高危领域（医学/招考）= high，中危（价格政策/版本）= medium，无风险 = low"""
    if risk in HIGH_RISK_PATTERNS:
        return "high"
    if risk in MEDIUM_RISK_PATTERNS:
        return "medium"
    return "low"


def _fact_present(fact_norm: str, blob_norm: str) -> bool:
    """断言数字是否重现于文本：要求数字前无数字边界（15000积分 不证实 5000积分）"""
    idx = blob_norm.find(fact_norm)
    while idx != -1:
        prev = blob_norm[idx - 1] if idx > 0 else ""
        if not prev.isdigit():
            return True
        idx = blob_norm.find(fact_norm, idx + 1)
    return False


def _search(query: str, session: requests.Session | None = None, timeout: int = 20) -> list[dict]:
    http = session or requests
    resp = http.get(
        BING_URL,
        params={"q": query, "setlang": "zh-hans", "count": "10"},
        headers={"User-Agent": BING_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return _parse_bing(resp.text)


def verify_fact(query: str, fact: str, session: requests.Session | None = None) -> dict:
    """对单个数字断言做多源交叉验证，返回三级判定（confirmed / conflict / unverified）"""
    fact_norm = fact.replace(" ", "")
    search_q = f"{query} {fact}"
    try:
        results = _search(search_q, session=session)
    except requests.exceptions.RequestException as exc:
        return {"fact": fact, "status": "unverified", "reason": f"搜索失败：{exc}"}
    if not results:
        return {"fact": fact, "status": "unverified", "reason": "未解析到搜索结果"}

    supports: list[dict] = []
    rejects: list[dict] = []
    for r in results:
        blob_norm = f"{r['title']} {r['snippet']}".replace(" ", "")
        is_reject = bool(REJECT_SIGNAL_RE.search(r["snippet"]))
        if is_reject:
            rejects.append(r)
        elif _fact_present(fact_norm, blob_norm):
            supports.append(r)

    # 否定/权威优先：权威辟谣 > 来源支持 > 普通否定 > 未核实
    authority_rejects = [
        r for r in rejects if _host_authority(_host_of(r.get("url", ""))) >= 2
    ]
    if authority_rejects:
        return {
            "fact": fact,
            "status": "conflict",
            "reject_snippets": [r["snippet"][:100] for r in authority_rejects[:3]],
        }
    if supports:
        authority_supports = [
            r for r in supports if _host_authority(_host_of(r.get("url", ""))) >= 2
        ]
        return {
            "fact": fact,
            "status": "confirmed",
            "support_count": len(supports),
            "authoritative": bool(authority_supports),
            "top_support": {"title": supports[0]["title"], "url": supports[0].get("url", "")},
        }
    if rejects:
        return {
            "fact": fact,
            "status": "conflict",
            "reject_snippets": [r["snippet"][:100] for r in rejects[:3]],
        }
    return {"fact": fact, "status": "unverified", "reason": "未检索到重现该断言的来源"}


def verify_facts(
    text: str,
    query: str,
    session: requests.Session | None = None,
) -> list[dict]:
    """提取草稿数字断言并逐一验证，返回判定清单（含风险领域标记）"""
    out: list[dict] = []
    for cand in extract_fact_candidates(text):
        result = verify_fact(query, cand["fact"], session=session)
        result["context"] = cand["context"]
        result["risk"] = risk_flag(cand["context"])
        out.append(result)
    return out
