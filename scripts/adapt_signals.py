"""adapt 素材缺口与营销信号检测（从 content_adapter.py 拆分，行为不变）"""
from __future__ import annotations

import re
import urllib.parse

from adapt_draft import (
    QUERY_COVERAGE_TERMS,
    DraftDoc,
    _strip_frontmatter,
    _text_without_code,
)

# 常见占位图服务（发布前必须替换为真实截图）
PLACEHOLDER_IMAGE_HOSTS = {
    "picsum.photos", "placehold.co", "via.placeholder.com", "placeholder.com",
    "dummyimage.com", "loremflickr.com", "placekitten.com", "fakeimg.pl",
}

# 泛化/空 alt 文本（检测图片可访问性缺口）
GENERIC_ALT = {"图", "图片", "截图", "效果图", "示意图", "img", "image", "pic", "screenshot"}

# 营销转化/投毒信号：高危 = 明确的转化引导（必须人工处理）；中危 = 营销话术（提示核实）
PROMOTIONAL_HIGH_PATTERNS = [
    "闭眼入", "无脑入", "必买", "人手一份", "手慢无", "库存告急",
    "限时秒杀", "限时抢购", "错过等一年", "赶紧下单", "立即下单",
    "扫码购买", "扫码下单", "扫码领取", "扫码添加", "扫码加",
    "扫码报名", "扫码进群",
    "私信领取", "私信报名", "私信购买", "私信获取",
    "私信我",
    "优惠码", "优惠券", "付款码", "立即购买",
    "加微信", "加vx", "加VX",
]
PROMOTIONAL_MEDIUM_PATTERNS = [
    "强烈推荐", "一定要买", "一定要入手", "必入", "全网第一", "史上第一",
    "唯一", "100%", "免费领取", "限时", "报名通道", "课程报名",
]

# 词边界：命中后若紧跟这些字，视为子串误报（如「私信我们」「唯一的办法」）
PATTERN_SUFFIX_BLOCKS = {
    "私信我": {"们"},
    "唯一": {"的"},
}


def _links_in_text(text: str) -> list[str]:
    """提取正文中的所有 http(s) 链接，去掉尾部标点"""
    # URL 字符集近似 ASCII：排除空白/括号/引号/中文字符与中文标点，避免吞掉紧贴的文本
    found = re.findall(r"https?://[^\s)\]>\"'\u4e00-\u9fff，。；：！？、]+", text)
    return [u.rstrip(".,;:!?。，；：！？") for u in found]


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _image_alt(line: str) -> str:
    m = re.match(r"!\[([^\]]*)\]\([^)]+\)", line.strip())
    return m.group(1).strip() if m else ""


def detect_promotional_signals(text: str) -> list[dict]:
    """检测营销转化/投毒信号（强烈推荐课程/网站/购买引导），返回按严重度排序的清单。

    高危：明确的转化动作（扫码/私信/优惠码/限时抢购等）——发布前必须处理；
    中危：营销话术（强烈推荐/绝对化用语）——提示核实利益关系。
    """
    signals: list[dict] = []
    low = (text or "").lower()
    high_spans: list[tuple[int, int]] = []

    def scan(patterns: list[str], severity: str, skip: list[tuple[int, int]]) -> None:
        for pat in patterns:
            needle = pat.lower()
            start = 0
            while True:
                idx = low.find(needle, start)
                if idx == -1:
                    break
                end = idx + len(needle)
                # 跳过已被高危覆盖的区间，以及同位置重复命中（如「加vx」与「加VX」）
                if any(s <= idx < e or s < end <= e for s, e in skip):
                    start = end
                    continue
                if severity == "high" and any(s <= idx < e or s < end <= e for s, e in high_spans):
                    start = end
                    continue
                follow = low[end:end + 1]
                if follow in PATTERN_SUFFIX_BLOCKS.get(pat, set()):
                    # 子串误报（「私信我们」「唯一的办法」），跳过
                    start = end
                    continue
                ctx_start = max(0, idx - 12)
                ctx_end = min(len(text), end + 12)
                signals.append({
                    "severity": severity,
                    "pattern": pat,
                    "context": text[ctx_start:ctx_end].replace("\n", " "),
                })
                if severity == "high":
                    high_spans.append((idx, end))
                start = end

    scan(PROMOTIONAL_HIGH_PATTERNS, "high", [])
    scan(PROMOTIONAL_MEDIUM_PATTERNS, "medium", high_spans)
    severity_order = {"high": 0, "medium": 1}
    signals.sort(key=lambda s: severity_order.get(s["severity"], 9))
    return signals


def detect_material_gaps(doc: DraftDoc, query: str | None = None) -> list[dict]:
    """识别草稿里的素材缺口，返回按严重度排序的清单（发布前处理）。

    类型：
      - placeholder_image: 占位图服务链接（high）
      - unverified_links: 外部链接未验证（medium）
      - query_coverage_missing: 目标话题要求的内容章节缺失（high）
      - image_alt_missing: 图片 alt 缺失或过泛（low）
    """
    gaps: list[dict] = []
    # 用剥 frontmatter、去代码块的正文扫描，避免元数据 URL/关键词误报，同时保留 H2 标题
    scan_text = _text_without_code(_strip_frontmatter(doc.raw))

    # 1) 占位图
    for img in doc.images:
        host = _host_of(img)
        if host in PLACEHOLDER_IMAGE_HOSTS:
            gaps.append({
                "type": "placeholder_image",
                "severity": "high",
                "detail": f"占位图 {img} 需替换为真实截图",
                "suggestion": "替换为真实截图或官方图片",
            })

    # 2) 外部链接待核实（排除占位图 URL，避免重复报告）
    # 只排除占位图服务的 host（已报高危缺口），非占位图图片链接仍计入待核实
    placeholder_hosts = {
        _host_of(i) for i in doc.images if _host_of(i) in PLACEHOLDER_IMAGE_HOSTS
    }
    urls = sorted({
        u for u in _links_in_text(scan_text)
        if _host_of(u) and _host_of(u) not in placeholder_hosts
    })
    if urls:
        shown = "、".join(urls[:5]) + ("…" if len(urls) > 5 else "")
        gaps.append({
            "type": "unverified_links",
            "severity": "medium",
            "detail": f"正文 {len(urls)} 个外部链接未验证：{shown}",
            "suggestion": "发布前逐条核实链接域名与内容一致，尤其官网/下载链接",
        })

    # 3) query 要求的话题覆盖
    q = (query or "").strip()
    if q:
        low = scan_text.lower()
        missing = [
            term for term, words in QUERY_COVERAGE_TERMS.items()
            if term in q and not any(w in low for w in words)
        ]
        if missing:
            gaps.append({
                "type": "query_coverage_missing",
                "severity": "high",
                "detail": f"目标话题要求覆盖「{'、'.join(missing)}」，正文未见相关内容",
                "suggestion": "补充对应章节后再发布",
            })

    # 4) 图片 alt
    for line in re.findall(r"!\[[^\]]*\]\([^)]+\)", scan_text):
        alt = _image_alt(line)
        if not alt or alt.lower() in GENERIC_ALT:
            gaps.append({
                "type": "image_alt_missing",
                "severity": "low",
                "detail": f"图片 alt 缺失或过泛：{line[:60]}",
                "suggestion": "为图片补充描述性 alt 文本",
            })

    # 5) 营销转化/投毒信号
    for sig in detect_promotional_signals(scan_text):
        gaps.append({
            "type": "promotional_signal",
            "severity": sig["severity"],
            "detail": f"疑似营销转化话术「{sig['pattern']}」：…{sig['context']}…",
            "suggestion": (
                "移除营销转化引导（扫码/私信/优惠码/绝对化推荐），或补充明确的利益关系声明后再发布"
                if sig["severity"] == "high"
                else "核实是否夹带付费推广，建议补充利益关系声明"
            ),
        })

    # 去重 + 按严重度排序
    seen: set[tuple[str, str]] = set()
    dedup: list[dict] = []
    for gap in gaps:
        key = (gap["type"], gap["detail"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(gap)
    severity_order = {"high": 0, "medium": 1, "low": 2}
    dedup.sort(key=lambda g: severity_order.get(g["severity"], 9))
    return dedup


def _gap_block(gaps: list[dict]) -> str:
    """生成发布前不可见的 HTML 注释块（编辑器可见，发布页无影响）"""
    if not gaps:
        return ""
    lines = ["<!-- 素材缺口（发布前处理，发布时删除本块） -->"]
    for gap in gaps:
        lines.append(
            f"<!-- [{gap['severity'].upper()}] {gap['detail']} → {gap['suggestion']} -->"
        )
    return "\n".join(lines) + "\n"


def _scan_facts(text: str) -> list[str]:
    """提取正文中带单位的具体数字断言，供人工核对（LLM 不验证事实）"""
    pattern = r"(\d+(?:\.\d+)?\s*(?:多\s*)?(?:积分|元|分钟|小时|天|秒|GB|MB|%|万|亿|字|行))"
    seen: set[str] = set()
    facts: list[str] = []
    for m in re.findall(pattern, text):
        norm = re.sub(r"\s+", "", m)
        if norm not in seen:
            seen.add(norm)
            facts.append(norm)
    return facts
