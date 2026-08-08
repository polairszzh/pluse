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
# 主域子域匹配时排除的 UGC 服务子域（百度知道/贴吧/文库、QQ 空间等）
UGC_HOST_SUFFIXES = (
    "zhidao.baidu.com", "tieba.baidu.com", "wenku.baidu.com", "baijiahao.baidu.com",
    "jingyan.baidu.com", "user.qzone.qq.com", "mp.weixin.qq.com",
)

_SEVERITY_ORDER = {"high": 2, "medium": 1, "low": 0}

# 明确否定词：作为 reject 的独立条件（含强否定；弱词见 WEAK_REJECT_RE）
REJECT_SIGNAL_RE = re.compile(
    r"(不存在|并未|没有|未提供|不提供|未推出|否认|未发放|不发放|未发布|未给|"
    r"不含|不包含|未包含|不包括|假的|虚假|错误信息|不实|并非|不是)"
)
# 单字「无」：仅与断言数字相邻时是否定（「无条件/无风险领取」不命中）
NO_ADJACENT_RE = re.compile(r"无\s*\d")
# 弱否定词：仅当无任何支持词时才算否定（「X 是谣言」= 否定；「此前有谣言称 X，现已证实」= 支持）
WEAK_REJECT_RE = re.compile(r"(谣言|辟谣|假消息)")
# 肯定表述排除：并非/不是 + 否定词 = 肯定（「该活动并非谣言」不是否定信号）
# 始终中性的表达：疑问句（有没有/是否存在）、双重否定（并非没有）、未公布（尚未公布）、
# 肯定前缀（并非谣言/不是问题）——豁免不解除
NEUTRAL_ALWAYS_RE = re.compile(
    r"(并非(?:谣言|虚假|不实|错误|假消息|问题|坏事|难事|骗局)|"
    r"不是(?:谣言|虚假|不实|错误|假消息|问题|坏事|难事|骗局)|不是假的|"
    r"尚未(?:公布|发布|披露)|并非没有|不是没有|有没有|是否存在|是否有|"
    r"并未否认|没有否认|不否认|未否认|不是不|并非不|"
    r"无法核实|无法确认|无可奉告|"
    r"不是(?:新用户|会员|所有人|所有用户)|并非所有|"
    r"未(?:经)?证实|未确认|尚未证实|"
    r"未被证实|无法证实|"
    r"未公布|未公开|未披露|"
    r"网传|传闻|传称|据悉|有消息称)"
)
# 支持信号：明确的肯定词——句含断言数字时视为支持。
# 「已辟谣」是否定信号（在 REJECT_SIGNAL_RE）；「已澄清」REJECT 优先保护（已澄清：X 不存在 → conflict）
SUPPORT_SIGNAL_RE = re.compile(r"(属实|确认|证实|已澄清)")
# 「没有 X」类固定短语：后跟数字断言时豁免解除（「没有相关 5000 积分活动」是明确否定）
NEUTRAL_NO_X_RE = re.compile(r"没有(?:问题|证据|找到|查到|相关|记录|信息|发现)")
# 第一手经验信号：个人体验类表述（官方「我们提供」不算个人经验）
FIRST_PERSON_RE = re.compile(
    r"(我(?:上周|昨天|最近|实测|亲测|用了|试了|体验|处理了|整理了|测试了|跑了|花了|"
    r"遇到了|发现了|写了|做了|改了|装了)|本人(?:实测|亲测|体验))"
)
# 省略主语的第一手经验延续：子句以体验动词开头 + 数字（「用了 5 分钟」；
# 「该工具用了 3 分钟」以名词开头，不匹配）
NO_SUBJECT_EXPERIENCE_RE = re.compile(
    r"^(?:然后|接着|之后|随后|又)?(?:用了|试了|花了|跑了|遇到了|发现了)\s*\d"
)

UNIT_FACT_RE = re.compile(
    r"(\d+(?:[, \t\u3000\xa0]\d{3})*(?:\.\d+)?[ \t\u3000\xa0]*(?:多[ \t\u3000\xa0]*)?"
    r"(?:万|亿)?[ \t\u3000\xa0]*(?:积分|元|分钟|小时|天|秒|GB|MB|%|万|亿|字|行|人|用户|粉丝))"
)
VERSION_RE = re.compile(
    r"(?:版本|发布|更新|升级|v\.?|ver\.?|Version|version)\s*(\d+\.\d+(?:\.\d+)?)"
)

# 高危风险领域：误导会造成实际伤害（医疗健康、教育招考），无法核实强制高危
HIGH_RISK_PATTERNS = {
    "医学/健康": r"(药|治疗|偏方|疫苗|治愈|降糖|降压|养生)",
    "教育招考": r"(高考|中考|考研|招考|分数线|招生简章|录取)",
}

# 中危风险领域：信息经常变动（价格政策、软件版本），无法核实标 medium 提示即可
MEDIUM_RISK_PATTERNS = {
    "价格/政策": r"(价格|售价|定价|费用|收费|资费|报价|优惠|积分|政策|计费)",
    "软件版本": r"(?:版本|发布|更新|升级|v\.?|ver\.?|Version|version)\s*\d+\.\d+(?:\.\d+)?",
}


def _host_of(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return host.split(":")[0]  # 剥离端口（codebuddy.cn:443 → codebuddy.cn）
    except ValueError:
        return ""


def _host_authority(host: str) -> int:
    """来源权威度：2=官方/权威（政府/教育/百科/权威媒体），1=普通网页"""
    if host.endswith(AUTHORITY_SUFFIXES):
        return 2
    # 维基语言子域（en/ja/de.wikipedia.org 等）同为权威百科
    if host in AUTHORITY_HOSTS or host.endswith(".wikipedia.org"):
        return 2
    # 官方/权威主域的子域（cloud.tencent.com → tencent.com），排除 UGC 服务
    if any(host.endswith("." + d) for d in AUTHORITY_HOSTS) and not any(
        host == u or host.endswith("." + u) for u in UGC_HOST_SUFFIXES
    ):
        return 2
    return 1


def extract_fact_candidates(text: str) -> list[dict]:
    """提取 (fact, context)：数字断言 + 所在句上下文；第一手经验断言跳过，同断言去重"""
    seen: dict[tuple[str, str], str] = {}

    def add(fact: str, start: int, end: int) -> None:
        sentence = _sentence_around(text, start, end)
        # 只检查断言数字所在子句（按逗号切分）是否为第一手经验：
        # 「我上周处理了 3000 行，新用户领 5000 积分」只跳过 3000行，5000积分 仍验证
        # 子句定位：ASCII 逗号仅作千分位时不切（5,000 行），比较时去逗号归一
        clause = next(
            (c for c in re.split(r"[，]|,(?!\d)", sentence) if fact in re.sub(r"[\s,]", "", c)),
            sentence,
        )
        if FIRST_PERSON_RE.search(clause) or NO_SUBJECT_EXPERIENCE_RE.search(clause.strip()):
            return  # 第一手经验不做外部验证
        key = (fact, _subject_key(sentence))
        if key not in seen:
            seen[key] = sentence
            return
        # 同一断言多次出现：保留风险等级更高的上下文（防高危句被低危首现覆盖）
        cur = _SEVERITY_ORDER.get(risk_severity(risk_flag(seen[key])), 0)
        new = _SEVERITY_ORDER.get(risk_severity(risk_flag(sentence)), 0)
        if new > cur:
            seen[key] = sentence

    for m in UNIT_FACT_RE.finditer(text or ""):
        add(re.sub(r"\s+", "", m.group(1)).replace(",", ""), m.start(), m.end())
    for m in VERSION_RE.finditer(text or ""):
        add(m.group(1), m.start(1), m.end(1))
    return [{"fact": f, "context": c} for (f, _), c in seen.items()]


def _subject_key(context: str) -> str:
    """断言主体键：上下文去数字/标点后取前 15 字（区分跨主体断言，同主体重复合并）"""
    return re.sub(r"[\d\s，。；！？、,:：]+", "", context or "")[:15]


def _sentence_around(text: str, start: int, end: int) -> str:
    """返回数字断言所在句（按中文句号/分号/换行切分）"""
    left = max(
        text.rfind("。", 0, start), text.rfind("；", 0, start),
        text.rfind("！", 0, start), text.rfind("？", 0, start),
        text.rfind("\n", 0, start),
    ) + 1
    right_cands = [
        i for i in (
            text.find("。", end), text.find("；", end),
            text.find("！", end), text.find("？", end), text.find("\n", end),
        )
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
    fact_norm = fact_norm.replace(",", "")
    blob_norm = blob_norm.replace(",", "")
    idx = blob_norm.find(fact_norm)
    while idx != -1:
        prev = blob_norm[idx - 1] if idx > 0 else ""
        end = idx + len(fact_norm)
        nxt = blob_norm[end] if end < len(blob_norm) else ""
        # 前后边界均排除数字与版本号延续点（3.1 不因 2.3.1 证实、1.0 不证实 1.0.2）
        if not prev.isdigit() and prev != "." and not nxt.isdigit() and nxt != ".":
            return True
        idx = blob_norm.find(fact_norm, idx + 1)
    return False


def _classify_snippet(snippet: str, fact_norm: str) -> str:
    """逐句分类 snippet：reject（否定+断言同句）/ support（断言重现）/ none。

    中性表达（尚未公布/没有问题/并非谣言等）所在句跳过，不参与判定——
    既不算否定也不算支持；逗号也分句，避免「并非谣言，新用户领 5000 积分」
    的肯定前缀吞掉支持内容。
    """
    has_reject = False
    has_support = False
    weak_reject = False
    snippet_has_digit = False
    support_seen = False
    for sent in re.split(r"[。；！？\n，]|,(?!\d)", snippet or ""):
        if _is_neutral(sent, fact_norm):
            continue  # 中性/肯定语境句跳过
        sent_norm = re.sub(r"\s+", "", sent)
        has_digit = _fact_present(fact_norm, sent_norm)
        if has_digit:
            snippet_has_digit = True
        if (REJECT_SIGNAL_RE.search(sent) or NO_ADJACENT_RE.search(sent)) and has_digit:
            has_reject = True  # 明确否定词优先
        elif SUPPORT_SIGNAL_RE.search(sent):
            if re.search(r"(不属实|非属实)", sent):
                # 「不属实/非属实」= 明确否定
                if has_digit:
                    has_reject = True
            elif WEAK_REJECT_RE.search(sent) and re.search(r"(证实|确认|澄清(?:为|是))", sent):
                # 「已被证实/确认为谣言/假消息」= 证实否定 → 归弱否定
                if has_digit:
                    weak_reject = True
            else:
                support_seen = True  # 支持词跨子句回看（「现已证实」），须 snippet 内出现过断言数字
        elif WEAK_REJECT_RE.search(sent) and has_digit:
            weak_reject = True  # 弱否定（谣言/辟谣）：无支持词时才判否定
        elif has_digit:
            has_support = True
    if support_seen and snippet_has_digit:
        has_support = True
    if weak_reject and not has_support:
        has_reject = True
    if has_reject:
        return "reject"
    if has_support:
        return "support"
    return "none"


def _is_neutral(sent: str, fact_norm: str) -> bool:
    """中性豁免判断：始终中性表达直接豁免；「没有 X」类仅当后不紧跟断言数字时豁免"""
    if NEUTRAL_ALWAYS_RE.search(sent or ""):
        return True
    m = NEUTRAL_NO_X_RE.search(sent or "")
    if not m:
        return False
    after = sent[m.end():]
    # 允许修饰词（的/了/过/些）在数字前：「没有相关的 5000 积分活动」仍是明确否定
    stripped = re.sub(r"^[\s的了过些]+", "", after)
    return not bool(re.match(r"\d", stripped))


def _search(query: str, session: requests.Session | None = None, timeout: int = 20) -> list[dict]:
    http = session or requests
    resp = http.get(
        BING_URL,
        params={"q": query, "setlang": "zh-hans", "count": "10"},
        headers={"User-Agent": BING_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        timeout=timeout,
    )
    resp.raise_for_status()
    try:
        return _parse_bing(resp.text)
    except Exception:  # noqa: BLE001 — 解析异常不吞掉整个搜索，按无结果处理
        return []


def verify_fact(
    query: str,
    fact: str,
    session: requests.Session | None = None,
    context: str = "",
) -> dict:
    """对单个数字断言做多源交叉验证，返回四级判定（confirmed / conflict / untrusted / unverified）"""
    fact_norm = re.sub(r"\s+", "", fact)
    search_q = f"{query} {fact}"
    if context:
        search_q += f" {_subject_key(context)}"  # 断言主体进搜索词，降低张冠李戴
    try:
        results = _search(search_q, session=session)
    except requests.exceptions.RequestException as exc:
        return {"fact": fact, "status": "unverified", "reason": f"搜索失败：{exc}"}
    if not results:
        return {"fact": fact, "status": "unverified", "reason": "未解析到搜索结果"}

    supports: list[dict] = []
    rejects: list[dict] = []
    for r in results:
        cls = _classify_snippet(r["snippet"], fact_norm)
        if cls == "reject":
            rejects.append(r)
        elif cls == "support":
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
        try:
            result = verify_fact(query, cand["fact"], context=cand["context"])
        except Exception:  # noqa: BLE001 — 单个断言验证异常不拖垮其他断言
            result = {"fact": cand["fact"], "status": "unverified", "reason": "验证异常"}
        result["context"] = cand["context"]
        result["risk"] = risk_flag(cand["context"])
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        return list(executor.map(run, cands))
