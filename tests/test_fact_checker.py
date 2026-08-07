"""fact_checker.py 单元测试 —— mock 所有网络调用，不触网"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import requests
from fact_checker import (
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


class TestVerifyFact:
    def test_confirmed(self, monkeypatch):
        html = bing_html([
            ("WorkBuddy 新用户福利", "https://www.codebuddy.cn/work/", "新用户注册可领 5000 积分"),
            ("其他", "https://blog.example.com/1", "无关内容"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "confirmed"
        assert r["authoritative"] is True  # codebuddy.cn 不在权威白名单，但 support 存在

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
            ("辟谣", "https://www.example.com/rebuttal", "该说法不存在，官方并未推出 5000 积分活动"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "conflict"
        assert r["reject_snippets"]

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

    def test_unverifiable_word_not_conflict(self, monkeypatch):
        html = bing_html([
            ("报道", "https://www.example.com/x", "该说法目前无法核实"),
        ])
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        r = verify_fact("workbuddy", "5000积分")
        assert r["status"] == "unverified"  # 「无法核实」不是否定信号，不判冲突

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
