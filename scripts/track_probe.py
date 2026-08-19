"""track 平台探测（从 search_ai.py 拆分，行为不变）

包含 DeepSeek 真实 API 探测、Bing/百度 搜索推断与收录检查、情感分类、PLATFORMS 注册表。

注意：probe_deepseek / probe_search_inference / check_index 内部对 `_load_key`、
`_parse_bing` 的调用统一经 `search_ai` 模块名空间在调用时解析（函数内懒加载），
因为既有测试 monkeypatch 目标是 `search_ai._load_key` / `search_ai._parse_bing`；
拆模块后必须保持这两个补丁接缝不变，测试才能不改一行继续全绿。
"""
from __future__ import annotations

import html as html_module
import re
from urllib.parse import parse_qs, urlsplit

import requests
from track_config import (
    _NEG_1CHAR,
    _NEG_2CHAR,
    _NEG_3CHAR,
    DEEPSEEK_BASE,
    DEEPSEEK_MODEL,
    NEGATIVE_WORDS,
    POSITIVE_WORDS,
    PROBE_PROMPT,
)
from track_models import ProbeResult
from track_utils import (
    _classify_cited_type,
    _detect_mine,
    _extract_fact_risks,
    _truncate,
    _url_present,
)

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
    import search_ai  # 懒加载：测试 monkeypatch 目标是 search_ai._load_key
    key = search_ai._load_key()
    if key is None:
        return ProbeResult(
            query=query, platform="deepseek", status="no_key", cited=None,
            sentiment=None, context="未配置 DEEPSEEK_API_KEY / LLM_API_KEY，跳过真实调用",
            source="api", degraded=False,
            meta={"note": "在 .env 中配置 DEEPSEEK_API_KEY 后重跑可拿到真实引用判断"},
            mine_ids=mine_ids,
            owned_ids=owned_ids,
            competitor_ids=competitor_ids,
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
                competitor_ids=competitor_ids,
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
                competitor_ids=competitor_ids,
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
                competitor_ids=competitor_ids,
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
                competitor_ids=competitor_ids,
            )
        answer = content or ""
    except requests.exceptions.RequestException as exc:
        return ProbeResult(
            query=query, platform="deepseek", status="error", cited=None,
            sentiment=None, context="DeepSeek API 调用失败", source="api", degraded=True,
            error=str(exc), meta={"note": "网络或服务异常，未写入有效探测"},
            mine_ids=mine_ids,
            owned_ids=owned_ids,
            competitor_ids=competitor_ids,
        )
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        return ProbeResult(
            query=query, platform="deepseek", status="error", cited=None,
            sentiment=None, context="DeepSeek 响应解析失败", source="api", degraded=True,
            error=str(exc), meta={"note": "响应结构与预期不符，保留原始响应便于排查"},
            mine_ids=mine_ids,
            owned_ids=owned_ids,
            competitor_ids=competitor_ids,
        )
    cited = query.lower() in answer.lower()
    mine_matched = _detect_mine(answer, mine_ids)
    competitor_matched = bool(_detect_mine(answer, competitor_ids)) if competitor_ids else None
    fact_risks = _extract_fact_risks(answer, query) if cited else []
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
        competitor_ids=competitor_ids,
        fact_risks=fact_risks,
    )


# --------------------------------------------------------------------------
# 搜索引擎推断（Kimi / 豆包 / 元宝 的降级信号）
# --------------------------------------------------------------------------


BING_URL = "https://www.bing.com/search"
BAIDU_URL = "https://www.baidu.com/s"
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


def _parse_baidu(html_text: str, limit: int = 10) -> list[dict]:
    """从百度结果页 HTML 提取 (title, url, snippet)，解析失败返回空列表

    百度链接多为跳转（www.baidu.com/link?url=...），收录判定主要靠标题/摘要，
    url 字段保留原始跳转链接供参考。
    """
    results: list[dict] = []
    # 块边界到下一个结果块或结尾：避免 .*?</div> 在嵌套 div 处提前截断
    block_class = r'class="(?=[^"]*result)(?=[^"]*c-container)[^"]*"'
    for block in re.findall(
        r"<div[^>]*" + block_class + r"[^>]*>"
        r"(.*?)(?=<div[^>]*" + block_class + r"|$)",
        html_text,
        re.DOTALL,
    ):
        link = re.search(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not link:
            continue
        # href 先做 HTML 实体解码（&amp; → &），否则 parse_qs 无法正确切分跳转参数
        url = html_module.unescape(link.group(1))
        title = html_module.unescape(re.sub(r"<[^>]+>", "", link.group(2))).strip()
        if not title:
            continue
        # 摘要取 h3 之后整段可见文本（去标签后拼接）：嵌套 span/a 不截断，
        # 保证「疑似收录」判定能覆盖摘要中的 URL；截断到 300 字符，
        # 避免最后一个结果块混入页脚噪音造成误判
        after = block[link.end() :]
        snippet = " ".join(
            html_module.unescape(re.sub(r"<[^>]+>", " ", after)).split()
        )[:300]
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def _site_query(url: str) -> str:
    """构造 site: 收录探测查询（去协议，保留域名与路径）

    query/fragment 不参与：跟踪参数/锚点是噪音，与 _url_present 的
    「query 不参与比较」设计一致；site: 用 path 匹配范围更宽更稳。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"site:{url}"
    if not parts.netloc:
        # 无协议输入（如 zhuanlan.zhihu.com/p/123）：netloc 为空、path 为整串，
        # 直接按原样构造，避免 host+path 重复；query/fragment 同归一化剥离
        path_only = url.split("?", 1)[0].split("#", 1)[0]
        return f"site:{path_only}"
    host = parts.netloc or url
    path = parts.path or ""
    return f"site:{host}{path}"


def _baidu_target_url(item_url: str) -> str:
    """从百度跳转链接（www.baidu.com/link?url=...）还原真实目标 URL"""
    try:
        parts = urlsplit(item_url)
    except ValueError:
        return item_url
    host = parts.netloc or ""
    if host != "baidu.com" and not host.endswith(".baidu.com"):
        return item_url
    # parse_qs 已做一次 percent 解码，不得再 unquote（避免 %25 双重解码）
    target = parse_qs(parts.query).get("url", [""])[0]
    return target if target else item_url


_NO_RESULT_MARKERS = (
    "没有找到",
    "抱歉，没有找到",
    "未找到相关",
    "no results",
    "there are no results",
)
_BLOCK_MARKERS = (
    "安全验证",
    "wappass",
    "verify",
    "captcha",
    "网络不给力",
    "访问过于频繁",
)


def _classify_empty_page(html_text: str) -> str:
    """区分空解析结果页的语义：not_indexed（有效无结果）vs error（反爬/解析失败）

    反爬/拦截标记 → 探测失败；明确的无结果标记 → 未收录；
    无明确特征时不猜测（避免把大体积反爬页误判为未收录），一律按探测失败处理。
    """
    lower = html_text.lower()
    if any(m in lower for m in _BLOCK_MARKERS):
        return "error"
    if any(m in lower for m in _NO_RESULT_MARKERS):
        return "not_indexed"
    return "error"


def check_index(
    url: str,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> dict:
    """检查单篇内容在主流检索源（Bing/百度）的收录状态

    对每个源发 site: 查询：命中该 URL（精确匹配，百度跳转链接还原后比较）→ 已收录；
    查询成功但无命中 → 未收录；请求/解析失败 → 探测失败。
    摘要文本含 URL 作为疑似收录的辅助信号（重定向/参数变体时 URL 可能不完全一致）。
    """
    http = session or requests
    headers = {
        "User-Agent": BING_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    q = _site_query(url)
    sources: dict[str, dict] = {}

    for name, base, params in (
        ("bing", BING_URL, {"q": q, "count": "10", "setlang": "zh-hans"}),
        ("baidu", BAIDU_URL, {"wd": q}),
    ):
        try:
            resp = http.get(base, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            html_text = resp.text
        except requests.exceptions.RequestException as exc:
            sources[name] = {"status": "error", "error": str(exc), "found": None}
            continue
        # 反爬/验证拦截优先于解析：拦截页即使解析出少量结果也不得误判收录
        if any(m in html_text.lower() for m in _BLOCK_MARKERS):
            sources[name] = {
                "status": "error",
                "error": "blocked（反爬/验证拦截）",
                "found": None,
            }
            continue
        import search_ai  # 懒加载：测试 monkeypatch 目标是 search_ai._parse_bing
        items = search_ai._parse_bing(html_text) if name == "bing" else _parse_baidu(html_text)
        if not items:
            if _classify_empty_page(html_text) == "not_indexed":
                sources[name] = {"status": "not_indexed", "found": False, "results": []}
                continue
            sources[name] = {
                "status": "error",
                "error": "no_results_parsed（页面结构变化或触发反爬）",
                "found": None,
            }
            continue
        url_hit = any(
            _url_present(url, item.get("url", ""))
            or _url_present(url, _baidu_target_url(item.get("url", "")))
            for item in items
        )
        # 疑似收录：摘要文本里出现该 URL（百度跳转链接/参数变体场景）
        snippet_hit = any(
            _url_present(url, item.get("snippet", "")) for item in items
        )
        if url_hit:
            status = "indexed"
        elif snippet_hit:
            status = "likely_indexed"
        else:
            status = "not_indexed"
        sources[name] = {
            "status": status,
            "found": url_hit,
            "results": items[:3],
        }
    return {"url": url, "query": q, "sources": sources}


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
            competitor_ids=competitor_ids,
        )

    import search_ai  # 懒加载：测试 monkeypatch 目标是 search_ai._parse_bing
    results = search_ai._parse_bing(html_text)
    if not results:
        return ProbeResult(
            query=query, platform=platform, status="error", cited=None,
            sentiment=None, context="未解析到搜索结果（页面结构变化或触发反爬）",
            source="search_inference", degraded=True,
            error="no_results_parsed", meta={"html_len": len(html_text)},
            mine_ids=mine_ids,
            owned_ids=owned_ids,
            competitor_ids=competitor_ids,
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
        competitor_ids=competitor_ids,
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
