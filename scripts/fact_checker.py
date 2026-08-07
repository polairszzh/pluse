"""置信度防火墙：对草稿中的可公开核实的声明型数据做多源交叉验证。

四级判定（可信度模型：仅权威来源可确认/否决，普通来源可能为投毒/灌水源）：
  - confirmed：权威来源（政府/教育/主流媒体/著名百科/著名公司官方）重现断言 → 放行
  - conflict：权威来源否定断言 → 拒绝（由调用方 exit 3）
  - untrusted：仅普通来源（支持或否定）→ 来源可信度不足，标注「可能投毒/灌水」
  - unverified：未检索到任何相关来源 → 标注不确定性，不阻断

原则（roadmap 数据可验证性 · 置信度防火墙）：
  - 确定性多源交叉比对（复用 Bing 搜索），LLM 不做事实裁判；
  - 只验证「品牌/产品 + 数字」类公开声明，第一手经验数据（「我上周处理了 3000 行」）不验证；
  - 只做事实核查，不把搜索结果搬进生成内容。
"""
from __future__ import annotations

import concurrent.futures
import re
import urllib.parse

import requests
from search_ai import BING_UA, BING_URL, _parse_bing

AUTHORITY_SUFFIXES = (".gov.cn", ".gov", ".edu.cn", ".edu")
AUTHORITY_HOSTS = {
    # 著名百科
    "baike.baidu.com", "baike.sogou.com", "wikipedia.org", "zh.wikipedia.org",
    # 主流媒体
    "xinhuanet.com", "people.com.cn", "cctv.com", "gmw.cn", "cnr.cn",
    "chinanews.com.cn", "chinadaily.com.cn", "thepaper.cn",
    # 著名公司官方门户（品牌/产品验证场景；UGC 平台不在此列——用户内容不能确认断言）
    "codebuddy.cn", "tencent.com", "baidu.com", "alibaba.com", "bytedance.com",
}

_SEVERITY_ORDER = {"high": 2, "medium": 1, "low": 0}

REJECT_SIGNAL_RE = re.compile(r"(不存在|并非|假的|谣言|辟谣|错误信息|不实)")
# 肯定表述排除：并非/不是 + 否定词 = 肯定（「该活动并非谣言」不是否定信号）
REJECT_EXCEPT_RE = re.compile(
    r"(并非(?:谣言|虚假|不实|错误)|不是(?:谣言|虚假|不实|错误)|不是假的)"
)
# 第一手经验信号：个人体验类表述（官方「我们提供」不算个人经验）
FIRST_PERSON_RE = re.compile(
    r"(我(?:上周|昨天|最近|实测|亲测|用了|试了|体验)|本人(?:实测|亲测|体验)|实测|亲测)"
)

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
        sentence = _sentence_around(text, start, end)
        if FIRST_PERSON_RE.search(sentence):
            return  # 第一手经验不做外部验证
        if fact not in seen:
            seen[fact] = sentence
            return
        # 同一断言多次出现：保留风险等级更高的上下文（防高危句被低危首现覆盖）
        cur = _SEVERITY_ORDER.get(risk_severity(risk_flag(seen[fact])), 0)
        new = _SEVERITY_ORDER.get(risk_severity(risk_flag(sentence)), 0)
        if new > cur:
            seen[fact] = sentence

    for m in UNIT_FACT_RE.finditer(text or ""):
        add(re.sub(r"\s+", "", m.group(1)), m.start(), m.end())
    for m in VERSION_RE.finditer(text or ""):
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
    """断言数字是否重现于文本：要求数字前后均无数字边界（15000 不证实 5000、2.3.10 不证实 2.3.1）"""
    idx = blob_norm.find(fact_norm)
    while idx != -1:
        prev = blob_norm[idx - 1] if idx > 0 else ""
        end = idx + len(fact_norm)
        nxt = blob_norm[end] if end < len(blob_norm) else ""
        if not prev.isdigit() and not nxt.isdigit():
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
    """对单个数字断言做多源交叉验证，返回四级判定（confirmed / conflict / untrusted / unverified）"""
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
        is_reject = bool(REJECT_SIGNAL_RE.search(r["snippet"])) and not bool(
            REJECT_EXCEPT_RE.search(r["snippet"])
        )
        if is_reject:
            rejects.append(r)
        elif _fact_present(fact_norm, blob_norm):
            supports.append(r)

    # 只有可信来源（权威）才能确认或否决；普通来源可能是投毒/灌水源，
    # 无论支持还是否定都不能单独裁决 → 一律无法核实
    authority_rejects = [
        r for r in rejects if _host_authority(_host_of(r.get("url", ""))) >= 2
    ]
    authority_supports = [
        r for r in supports if _host_authority(_host_of(r.get("url", ""))) >= 2
    ]
    if authority_rejects:
        return {
            "fact": fact,
            "status": "conflict",
            "reject_snippets": [r["snippet"][:100] for r in authority_rejects[:3]],
        }
    if authority_supports:
        return {
            "fact": fact,
            "status": "confirmed",
            "support_count": len(authority_supports),
            "authoritative": True,
            "top_support": {
                "title": authority_supports[0]["title"],
                "url": authority_supports[0].get("url", ""),
            },
        }
    if supports or rejects:
        return {
            "fact": fact,
            "status": "untrusted",
            "reason": "仅检索到普通来源（支持或否定），来源可信度不足无法确认——普通网页可能是投毒/灌水来源",
        }
    return {"fact": fact, "status": "unverified", "reason": "未检索到重现该断言的来源"}


def verify_facts(
    text: str,
    query: str,
) -> list[dict]:
    """提取草稿数字断言并并发验证，返回判定清单（含风险领域标记）。

    并发搜索（默认 4 线程）避免多断言串行叠加耗时，使用 requests 模块级默认连接。
    """
    cands = extract_fact_candidates(text)
    if not cands:
        return []

    def run(cand: dict) -> dict:
        result = verify_fact(query, cand["fact"])  # 并发场景不共享 session，用模块级 requests
        result["context"] = cand["context"]
        result["risk"] = risk_flag(cand["context"])
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        return list(executor.map(run, cands))
