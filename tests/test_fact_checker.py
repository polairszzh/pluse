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

    def test_skips_first_person_experience(self):
        cands = extract_fact_candidates("我上周处理了 3000 行数据，用了 5 分钟。")
        assert cands == []  # 第一手经验不做外部验证

    def test_dedup(self):
        cands = extract_fact_candidates("5000 积分。再次提到 5000积分。")
        assert sum(1 for c in cands if c["fact"] == "5000积分") == 1

    def test_keeps_higher_risk_context(self):
        # 同一断言首次低危句、后高危句（医疗）时，保留高危上下文
        cands = extract_fact_candidates("售价 5000 积分。该偏方 5000 积分可治愈。")
        item = next(c for c in cands if c["fact"] == "5000积分")
        assert risk_flag(item["context"]) == "医学/健康"

    def test_official_we_not_first_person(self):
        # 官方口径「我们提供」不是第一手经验，应进入验证
        cands = extract_fact_candidates("我们提供 5000 积分给新用户。")
        assert any(c["fact"] == "5000积分" for c in cands)


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

    def test_price_policy_medium(self):
        assert risk_flag("每天签到 100 积分") == "价格/政策"
        assert risk_severity("价格/政策") == "medium"

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
