"""fact_checker.py 单元测试 —— mock 所有网络调用，不触网"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import requests
from fact_checker import (
    _host_authority,
    extract_fact_candidates,
    risk_flag,
    risk_severity,
    verify_fact,
    verify_facts,
)


class FakeResponse:
    def __init__(self, text=""):
        self.text = text

    def raise_for_status(self):
        pass


def bing_html(items: list[tuple[str, str, str]]) -> str:
    """构造最小 Bing 结果页 HTML：(title, url, snippet)"""
    rows = []
    for title, url, snippet in items:
        rows.append(
            f'<li class="b_algo"><h2><a href="{url}">{title}</a></h2>'
            f'<p>{snippet}</p></li>'
        )
    return "<html><body>" + "".join(rows) + "</body></html>"


class TestExtract:
    def test_extracts_fact_and_context(self):
        cands = extract_fact_candidates("WorkBuddy 新用户领 5000 积分，每天签到 100 积分。")
        facts = {c["fact"] for c in cands}
        assert "5000积分" in facts
        assert "100积分" in facts
        assert any("新用户领 5000 积分" in c["context"] for c in cands)

    def test_version_requires_context(self):
        # 版本号只认带版本/发布等上下文的数字，「3.5 元」不当版本
        cands = extract_fact_candidates("价格 3.5 元。版本 2.3.1 已发布。")
        facts = {c["fact"] for c in cands}
        assert "2.3.1" in facts
        assert "3.5" not in facts

    def test_skips_first_person_experience(self):
        cands = extract_fact_candidates("我上周处理了 3000 行数据，用了 5 分钟。")
        assert cands == []  # 第一手经验不做外部验证

    def test_dedup(self):
        cands = extract_fact_candidates("5000 积分。再次提到 5000积分。")
        assert sum(1 for c in cands if c["fact"] == "5000积分") == 1

    def test_thousands_separator(self):
        # 千分位逗号：「5,000 积分」应提取为 5,000积分，而非 000积分
        cands = extract_fact_candidates("新用户领 5,000 积分。")
        facts = {c["fact"] for c in cands}
        assert "5,000积分" in facts
        assert "000积分" not in facts

    def test_keeps_higher_risk_context(self):
        # 同一断言首次低危句、后高危句（医疗）时，保留高危上下文
        cands = extract_fact_candidates("售价 5000 积分。该偏方 5000 积分可治愈。")
        item = next(c for c in cands if c["fact"] == "5000积分")
        assert risk_flag(item["context"]) == "医学/健康"

    def test_official_we_not_first_person(self):
        # 官方口径「我们提供」不是第一手经验，应进入验证
        cands = extract_fact_candidates("我们提供 5000 积分给新用户。")
        assert any(c["fact"] == "5000积分" for c in cands)

    def test_third_party_ce_not_first_person(self):
        # 「第三方实测」不是第一手经验，应进入验证
        cands = extract_fact_candidates("第三方实测处理 5000 行数据。")
        assert any(c["fact"] == "5000行" for c in cands)

    def test_wo_chuli_first_person(self):
        # 「我处理了 3000 行」是第一手经验，跳过外部验证
        cands = extract_fact_candidates("我处理了 3000 行数据。")
        assert cands == []

    def test_tool_processed_not_first_person(self):
        # 「该工具处理了 3000 行」是公开陈述，不是第一手经验
        cands = extract_fact_candidates("该工具处理了 3000 行数据。")
        assert any(c["fact"] == "3000行" for c in cands)

    def test_tool_used_not_first_person(self):
        # 「该工具用了 3 分钟」以名词开头，不是第一手经验延续
        cands = extract_fact_candidates("该工具用了 3 分钟完成处理。")
        assert any(c["fact"] == "3分钟" for c in cands)

    def test_no_subject_with_conjunction(self):
        # 带连接词的省略主语体验句（然后用了 5 分钟）仍跳过
        cands = extract_fact_candidates("我处理完数据，然后用了 5 分钟。")
        assert all(c["fact"] != "5分钟" for c in cands)

    def test_sentence_boundary_exclamation(self):
        # 感叹号是句子边界：context 不跨感叹句
        cands = extract_fact_candidates("新用户领 5000 积分！这个偏方很好。")
        item = next(c for c in cands if c["fact"] == "5000积分")
        assert "偏方" not in item["context"]

    def test_comma_clause_first_person_only(self):
        # 逗号后的公开断言不被第一人称子句误伤
        cands = extract_fact_candidates("我上周处理了 3000 行，新用户领 5000 积分。")
        facts = {c["fact"] for c in cands}
        assert "5000积分" in facts  # 公开断言仍验证
        assert "3000行" not in facts  # 第一手经验跳过


class TestRisk:
    def test_medical_risk(self):
        assert risk_flag("这个偏方 3 天治愈") == "医学/健康"
        assert risk_severity("医学/健康") == "high"

    def test_exam_risk(self):
        assert risk_flag("2026 年高考分数线 550 分") == "教育招考"
        assert risk_severity("教育招考") == "high"

    def test_version_risk(self):
        assert risk_flag("WorkBuddy 最新版本 2.3.1") == "软件版本"
        assert risk_severity("软件版本") == "medium"

    def test_version_risk_requires_context(self):
        # 无版本上下文的任意小数不标软件版本
        assert risk_flag("成本 3.5 元") is None
        assert risk_flag("售价 3.5 元") == "价格/政策"

    def test_price_policy_medium(self):
        assert risk_flag("每天签到 100 积分") == "价格/政策"
        assert risk_severity("价格/政策") == "medium"

    def test_price_words(self):
        # 售价/定价/费用 等词也命中价格风险
        assert risk_flag("售价 5000 元") == "价格/政策"
        assert risk_flag("官方定价 299 元") == "价格/政策"

    def test_no_risk(self):
        assert risk_flag("本文介绍了 WorkBuddy 的使用体验") is None

    def test_no_risk_low_severity(self):
        assert risk_severity(None) == "low"  # 无风险领域不默认中危

    def test_ugc_platform_not_authority(self):
        # UGC 平台（知乎/小红书等）不视为权威来源
        assert _host_authority("zhihu.com") == 1
        assert _host_authority("xiaohongshu.com") == 1
        assert _host_authority("douyin.com") == 1
        # 公司官方门户仍权威
        assert _host_authority("tencent.com") == 2
        assert _host_authority("codebuddy.cn") == 2
        # 维基语言子域权威
        assert _host_authority("en.wikipedia.org") == 2
        assert _host_authority("ja.wikipedia.org") == 2


class TestVerifyFact:
    def test_confirmed(self, monkeypatch):
        html = bing_html([
            ("WorkBuddy 新用户福利", "https://www.codebuddy.cn/work/", "新用户注册可领 5000 积分"),
            ("其他", "https://blog.example.com/1", "无关内容"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "confirmed"
        assert r["authoritative"] is True  # codebuddy.cn 在权威白名单

    def test_confirmed_with_authority(self, monkeypatch):
        html = bing_html([
            ("百度百科 WorkBuddy", "https://baike.baidu.com/item/WorkBuddy", "新用户 5000 积分"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "confirmed"
        assert r["authoritative"] is True

    def test_conflict(self, monkeypatch):
        html = bing_html([
            ("辟谣", "https://baike.baidu.com/item/x", "该说法不存在，官方并未推出 5000 积分活动"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "conflict"
        assert r["reject_snippets"]

    def test_plain_support_not_confirmed(self, monkeypatch):
        # 普通来源可能是投毒/灌水源，即使含数字也不能确认
        html = bing_html([
            ("普通博客", "https://blog.example.com/1", "新用户领 5000 积分"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "untrusted"
        assert "可信度" in r["reason"] or "投毒" in r["reason"]

    def test_plain_reject_untrusted(self, monkeypatch):
        # 普通否定同样不可信 → untrusted（不是 conflict）
        html = bing_html([
            ("普通博客", "https://blog.example.com/r", "该说法不存在，没有 5000 积分"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "untrusted"

    def test_authority_reject_beats_support(self, monkeypatch):
        html = bing_html([
            ("辟谣页", "https://baike.baidu.com/item/x", "该说法不存在，官方并未推出 5000 积分"),
            ("普通页", "https://blog.example.com/1", "新用户领 5000 积分"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "conflict"  # 权威辟谣优先于普通网页支持

    def test_digit_boundary_no_false_confirm(self, monkeypatch):
        html = bing_html([
            ("活动页", "https://www.example.com/x", "新用户领 15000 积分"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "unverified"  # 15000 不证实 5000

    def test_version_suffix_boundary(self, monkeypatch):
        # 版本号后边界：2.3.1 不因 2.3.10 误判
        html = bing_html([
            ("发布页", "https://www.example.com/x", "最新版本 2.3.10 已发布"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "2.3.1")
        assert r["status"] == "unverified"  # 2.3.10 不证实 2.3.1

    def test_version_dot_boundary(self, monkeypatch):
        # 1.0 不应被 1.0.2 证实（版本号延续点）
        html = bing_html([
            ("发布页", "https://www.example.com/x", "最新版本 1.0.2 已发布"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "1.0")
        assert r["status"] == "unverified"

    def test_version_prefix_dot_boundary(self, monkeypatch):
        # 3.1 不应被 2.3.1 证实（版本号前边界延续点）
        html = bing_html([
            ("发布页", "https://www.example.com/x", "最新版本 2.3.1 已发布"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "3.1")
        assert r["status"] == "unverified"

    def test_unverifiable_word_not_conflict(self, monkeypatch):
        html = bing_html([
            ("报道", "https://www.example.com/x", "该说法目前无法核实"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "unverified"  # 「无法核实」不是否定信号，不判冲突

    def test_double_negation_not_conflict(self, monkeypatch):
        # 权威页「该活动并非谣言」是肯定表述，不应判 conflict
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "该活动并非谣言，新用户领 5000 积分"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "confirmed"  # 双重否定 = 肯定，且含数字支持

    def test_wei_mei_reject(self, monkeypatch):
        # 「并未」等常用否定词也要识别：权威页否定断言 → conflict
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "官方并未推出 5000 积分"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "conflict"

    def test_negation_bound_to_same_sentence(self, monkeypatch):
        # 否定词在别的句（不含断言数字）不算针对该断言：仍可被支持确认
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "官方没有相关活动。新用户可领 5000 积分。"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "confirmed"  # 否定不绑定该断言，支持句含数字

    def test_except_fake_news(self, monkeypatch):
        # 「并非假消息」是肯定表述，不判否定
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "该活动并非假消息，新用户领 5000 积分"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "confirmed"

    def test_not_problem_not_conflict(self, monkeypatch):
        # 「这不是问题」是否定中性词，不是否定断言
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "这不是问题，5000 积分照常发放"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "confirmed"

    def test_no_problem_not_conflict(self, monkeypatch):
        # 「没有问题」含「没有」但不是否定断言
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "没有问题，5000 积分照常发放"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] != "conflict"  # 中性语境不判否定（也不强行确认）

    def test_not_yet_announced_not_conflict(self, monkeypatch):
        # 「尚未公布」是中性（未公布 ≠ 断言为假），不判 conflict
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "官方尚未公布 5000 积分详情"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "unverified"  # 中性结果不参与判定，无法确认

    def test_double_negation_wei_mei_not_conflict(self, monkeypatch):
        # 「并非没有推出」= 确实推出了（双重否定为肯定）
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "官方并非没有推出 5000 积分"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] != "conflict"

    def test_question_not_conflict(self, monkeypatch):
        # 「有没有 5000 积分？」是疑问句，不是否定断言
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "新用户有没有 5000 积分？"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] != "conflict"

    def test_no_related_with_digit_is_reject(self, monkeypatch):
        # 「没有相关 5000 积分活动」是明确否定：豁免短语后紧跟数字时不豁免
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "官方没有相关 5000 积分活动"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "conflict"

    def test_rumor_cleared_not_conflict(self, monkeypatch):
        # 「谣言已澄清，活动属实」是肯定（澄清后属实），不判 conflict
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "关于 5000 积分的谣言已澄清，活动属实"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "confirmed"  # 澄清后属实 → 权威支持

    def test_shuoshi_support(self, monkeypatch):
        # 「活动属实」是支持信号，权威源应判 confirmed（不是中性跳过）
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "5000 积分活动属实"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "confirmed"

    def test_yi_piyao_reject(self, monkeypatch):
        # 「官方已辟谣：5000 积分不存在」是明确否定，REJECT 优先于 SUPPORT
        html = bing_html([
            ("官方说明", "https://baike.baidu.com/item/x", "官方已辟谣：5000 积分不存在"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "conflict"

    def test_unverified(self, monkeypatch):
        html = bing_html([("无关文章", "https://www.example.com/x", "普通内容没有提到数字")])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "unverified"

    def test_search_failure_unverified(self, monkeypatch):
        def boom(*a, **kw):
            raise requests.exceptions.ConnectionError("down")

        monkeypatch.setattr(requests, "get", boom)
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "unverified"
        assert "搜索失败" in r["reason"]


class TestVerifyFacts:
    def test_pipeline_with_risk_flag(self, monkeypatch):
        html = bing_html([("无关", "https://www.example.com/x", "没有提到")])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        out = verify_facts("WorkBuddy 最新版本 2.3.1 已发布。", "workbuddy")
        assert out
        assert all("risk" in r for r in out)
        assert any(r["risk"] == "软件版本" for r in out)
        assert all(r["status"] == "unverified" for r in out)

    def test_single_failure_degrades_not_crash(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr("fact_checker.verify_fact", boom)
        out = verify_facts("售价 5000 元。", "workbuddy")
        assert out and out[0]["status"] == "unverified"
        assert "验证异常" in out[0]["reason"]
