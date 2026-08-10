"""search_ai.py 单元测试 —— mock 所有网络调用，不触网"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import requests
import search_ai
from audit import Recommendation
from search_ai import (
    PLATFORMS,
    ProbeResult,
    _detect_mine,
    _parse_bing,
    _parse_platforms,
    _shell_quote,
    build_delta,
    build_recommendations,
    build_trend,
    classify_sentiment,
    connect,
    load_history,
    main,
    probe_deepseek,
    probe_search_inference,
    re_slug,
    render_json,
    render_markdown,
    save_report,
    store_results,
)


class FakeResponse:
    def __init__(self, status=200, text="", data=None, exc=None, json_exc=None):
        self.status_code = status
        self._text = text
        self._data = data
        self._exc = exc
        self._json_exc = json_exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        if self._json_exc:
            raise self._json_exc
        return self._data if self._data is not None else {}

    @property
    def text(self):
        return self._text


def _deepseek_answer(mentioned: bool, sentiment_word: str = "", query: str = "测试品牌") -> dict:
    content = "关于该对象的信息有限。" if not mentioned else f"关于{query}：该品牌{sentiment_word}，值得关注。"
    return {"choices": [{"message": {"content": content}}]}


BING_HTML = """
<ol id="b_results">
  <li class="b_algo" data-idx="0">
    <h2><a href="https://example.com/1">AI 搜索优化入门</a></h2>
    <p>这篇讲 AI 搜索优化怎么做，<strong>AI搜索优化</strong> 关键词覆盖完整。</p>
  </li>
  <li class="b_algo">
    <h2><a href="https://example.com/2">另一个话题</a></h2>
    <p>与目标品牌无关的内容。</p>
  </li>
</ol>
"""


class TestSentiment:
    def test_positive(self):
        assert classify_sentiment("该产品很优秀，值得推荐") == "positive"

    def test_negative(self):
        assert classify_sentiment("用户反馈差评，不推荐购买") == "negative"

    def test_neutral(self):
        assert classify_sentiment("简单介绍，没有倾向") == "neutral"

    def test_not_recommended_is_negative(self):
        assert classify_sentiment("不推荐") == "negative"

    def test_not_recommended_with_detail(self):
        assert classify_sentiment("该产品不推荐，存在差评") == "negative"

    def test_slightly_not_recommended_is_negative(self):
        assert classify_sentiment("不太推荐") == "negative"
        assert classify_sentiment("并不推荐") == "negative"

    def test_three_char_negation(self):
        assert classify_sentiment("谈不上推荐") == "negative"
        assert classify_sentiment("说不上推荐") == "negative"

    def test_negated_negative_word_is_not_negative(self):
        assert classify_sentiment("没有投诉") == "neutral"
        assert classify_sentiment("并无差评") == "neutral"

    def test_negated_positive_word_is_negative(self):
        assert classify_sentiment("并不优秀") == "negative"
        assert classify_sentiment("并非差评") == "neutral"

    def test_conflict_positive_wins(self):
        assert classify_sentiment("整体好评，但有个别差评") == "positive"


class TestParseBing:
    def test_extracts_results(self):
        results = _parse_bing(BING_HTML)
        assert len(results) == 2
        assert results[0]["title"] == "AI 搜索优化入门"
        assert results[0]["url"] == "https://example.com/1"
        assert "AI搜索优化" in results[0]["snippet"]

    def test_empty_on_unparseable(self):
        assert _parse_bing("<html>no results</html>") == []


class TestDetectMine:
    def test_url_title_author(self):
        text = "可参考 https://zhuanlan.zhihu.com/p/123 和「我的昵称」写的教程"
        assert _detect_mine(text, ["https://zhuanlan.zhihu.com/p/123"]) == ["https://zhuanlan.zhihu.com/p/123"]
        assert _detect_mine(text, ["我的昵称"]) == ["我的昵称"]
        assert _detect_mine(text, ["https://other.com/x"]) == []


class TestB3Quality:
    """B3 引用质量分层：earned/owned 拆分、lostprompt、未核实断言"""

    def test_classify_cited_type(self):
        assert search_ai._classify_cited_type(["https://a.com/1"], []) == "earned"
        assert search_ai._classify_cited_type(
            ["https://a.com/1"], ["https://a.com/1"]
        ) == "owned"
        # 命中任一非 owned 标识即记为 earned（更高价值口径）
        assert search_ai._classify_cited_type(
            ["https://a.com/1", "https://a.com/2"], ["https://a.com/1"]
        ) == "earned"
        assert search_ai._classify_cited_type([], ["https://a.com/1"]) is None

    def test_extract_fact_risks(self):
        answer = "WorkBuddy 最新版本 2.3.1，注册送 5000 积分，已有 10000 用户使用。"
        risks = search_ai._extract_fact_risks(answer, "WorkBuddy")
        assert any("版本 2.3.1" in r for r in risks)
        assert any("5000积分" in r for r in risks)
        assert any("10000用户" in r for r in risks)

    def test_extract_fact_risks_dedupe_and_limit(self):
        answer = "该品牌版本 1.0 与版本 1.0 重复；另有 3 天、4 天、5 天、6 天、7 天、8 天"
        risks = search_ai._extract_fact_risks(answer, "该品牌", limit=5)
        assert len(risks) <= 5
        assert sum(1 for r in risks if "版本 1.0" in r) == 1

    def test_extract_fact_risks_skips_date_units(self):
        # 年/月/天属于日期表述，不提取为风险噪音
        answer = "该产品 2026 年发布，3 天前更新，当前版本 2.3.1"
        risks = search_ai._extract_fact_risks(answer, "该产品")
        assert not any(r.startswith(("2026", "3 天")) for r in risks)
        assert any("版本 2.3.1" in r for r in risks)

    def test_extract_fact_risks_skips_relative_time(self):
        # 相对时间表达（X 小时前/X 分钟后）不作为未核实断言
        answer = "该功能 3 小时前上线，5 分钟后可用，升级需 2 分钟"
        risks = search_ai._extract_fact_risks(answer, "该功能")
        assert risks == []

    def test_extract_fact_risks_includes_context(self):
        answer = "WorkBuddy 最新版本 2.3.1，注册送 5000 积分。"
        risks = search_ai._extract_fact_risks(answer, "WorkBuddy")
        assert any("版本 2.3.1" in r and "最新" in r for r in risks)
        assert any("5000积分" in r and "注册送" in r for r in risks)

    def test_extract_fact_risks_version_with_colon(self):
        # 版本与数字之间允许中英文冒号
        risks = search_ai._extract_fact_risks(
            "WorkBuddy 版本：2.3.1 和 version: 1.0.2", "WorkBuddy"
        )
        assert any("版本 2.3.1" in r for r in risks)
        assert any("版本 1.0.2" in r for r in risks)

    def test_extract_fact_risks_compound_units(self):
        # 复合数量单位不得被截断：1.8万亿 不能提取成 1.8万
        risks = search_ai._extract_fact_risks(
            "WorkBuddy 估值 1.8万亿，年营收 5000万元，补贴 3亿元，累计 1000万用户，"
            "接待 1.8万人次，日活 1.8亿用户，调用 3亿次，服务 100亿次请求，覆盖 5000万人",
            "WorkBuddy",
            limit=10,
        )
        assert any("1.8万亿" in r for r in risks)
        assert any("5000万元" in r for r in risks)
        assert any("3亿元" in r for r in risks)
        assert any("1000万用户" in r for r in risks)
        assert any("1.8万人次" in r for r in risks)
        assert any("1.8亿用户" in r for r in risks)
        assert any("3亿次" in r for r in risks)
        # 数量级/对象单位解耦后，未枚举的新组合（百亿次/千万人）也能完整提取
        assert any("100亿次" in r for r in risks)
        assert any("5000万人" in r for r in risks)
        # 人次 必须作为完整单位提取，不得截成「1.8万人」
        assert any(r.startswith("1.8万人次（") for r in risks)
        # 不得出现被截断的「1.8万（…）」标签
        assert not any(r.startswith("1.8万（") for r in risks)

    def test_extract_fact_risks_only_near_brand(self):
        # 远离品牌词的数字（系统要求 16GB 内存）不作为品牌断言提取
        answer = (
            "WorkBuddy 提供稳定的云服务，功能丰富。"
            + "。" * 100
            + "系统要求 16GB 内存，升级耗时 2 小时。"
        )
        risks = search_ai._extract_fact_risks(answer, "WorkBuddy")
        assert all("16GB" not in r and "2 小时" not in r for r in risks)

    def test_probe_deepseek_cited_type_earned_and_owned(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        answer = "可参考 https://zhuanlan.zhihu.com/p/123 的教程"
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(
                data={"choices": [{"message": {"content": answer}}]}
            ),
        )
        earned = probe_deepseek(
            "codex 安装", mine_ids=["https://zhuanlan.zhihu.com/p/123"]
        )
        assert earned.cited_type == "earned"
        owned = probe_deepseek(
            "codex 安装",
            mine_ids=["https://zhuanlan.zhihu.com/p/123"],
            owned_ids=["https://zhuanlan.zhihu.com/p/123"],
        )
        assert owned.cited_type == "owned"

    def test_probe_deepseek_competitor_and_fact_risks(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        answer = "WorkBuddy 最新版本 2.3.1，可参考竞品 https://comp.example.com 的文档"
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(
                data={"choices": [{"message": {"content": answer}}]}
            ),
        )
        hit = probe_deepseek("WorkBuddy", competitor_ids=["https://comp.example.com"])
        assert hit.competitor_matched is True
        assert any("版本 2.3.1" in r for r in hit.fact_risks)
        miss = probe_deepseek("WorkBuddy", competitor_ids=["https://nope.example/x"])
        assert miss.competitor_matched is False
        not_checked = probe_deepseek("WorkBuddy")
        assert not_checked.competitor_matched is None

    def test_probe_search_inference_cited_type_owned(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=BING_HTML))
        result = probe_search_inference(
            "AI搜索优化", "kimi",
            mine_ids=["https://example.com/1"],
            owned_ids=["https://example.com/1"],
        )
        assert result.mine_cited is True
        assert result.cited_type == "owned"

    def test_probe_search_inference_competitor_matched(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=BING_HTML))
        hit = probe_search_inference(
            "AI搜索优化", "kimi", competitor_ids=["https://example.com/1"]
        )
        assert hit.competitor_matched is True
        miss = probe_search_inference(
            "AI搜索优化", "kimi", competitor_ids=["https://nope.example/x"]
        )
        assert miss.competitor_matched is False


class TestB1Sampling:
    """B1 多采样概率闭环：Wilson 区间、样本聚合、概率趋势与展示"""

    def test_wilson_interval(self):
        lo, hi = search_ai._wilson_interval(4, 5)
        assert lo <= 0.8 <= hi
        assert 0.0 <= lo <= hi <= 1.0
        assert search_ai._wilson_interval(0, 5)[0] == 0.0
        assert search_ai._wilson_interval(5, 5)[1] == 1.0
        assert search_ai._wilson_interval(3, 0) == (0.0, 0.0)

    def test_aggregate_samples_majority_and_prob(self):
        s1 = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c1", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"], cited_type="earned",
        )
        s2 = ProbeResult(
            "品牌A", "deepseek", "ok", True, "negative", "c2", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"], cited_type="earned",
        )
        s3 = ProbeResult(
            "品牌A", "deepseek", "ok", False, None, "c3", "api", False,
            mine_cited=False, mine_ids=["https://a.com/1"],
        )
        agg = search_ai._aggregate_samples([s1, s2, s3])
        assert agg.cited is True  # 2/3 多数
        assert agg.prob == 2 / 3
        assert agg.sample_count == 3
        assert agg.sentiment == "positive"  # 多数派
        assert agg.mine_cited is True
        assert agg.ci_low <= agg.prob <= agg.ci_high

    def test_aggregate_samples_risk_conservative(self):
        # competitor/fact_risks 保守：任一样本命中即保留，不因多数而漏报
        s1 = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            competitor_matched=False,
        )
        s2 = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            competitor_matched=True, fact_risks=["版本 2.3.1"],
        )
        agg = search_ai._aggregate_samples([s1, s2])
        assert agg.competitor_matched is True
        assert "版本 2.3.1" in agg.fact_risks

    def test_aggregate_samples_excludes_invalid(self):
        # cited=None 的失败样本不计入概率分母
        s1 = ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)
        s2 = ProbeResult(
            "品牌A", "deepseek", "error", None, None, "boom", "api", True, error="x"
        )
        s3 = ProbeResult(
            "品牌A", "deepseek", "error", None, None, "boom", "api", True, error="x"
        )
        agg = search_ai._aggregate_samples([s1, s2, s3])
        assert agg.prob == 1.0
        assert agg.sample_count == 1
        assert agg.meta["sample_invalid"] == 2
        assert agg.cited is True

    def test_aggregate_samples_tie_votes_not_cited(self):
        # 偶数采样平局（1/2 命中）按严格多数应为「否」，与 build_trend 一致
        s1 = ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)
        s2 = ProbeResult("品牌A", "deepseek", "ok", False, "neutral", "c", "api", False)
        agg = search_ai._aggregate_samples([s1, s2])
        assert agg.prob == 0.5
        assert agg.cited is False

    def test_aggregate_samples_context_matches_final_verdict(self):
        # 最终判定为「否」时，上下文不得取自命中样本（避免「否 (40%)」却展示命中内容）
        hit = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "命中内容", "api", False
        )
        hit2 = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "命中内容2", "api", False
        )
        miss1 = ProbeResult(
            "品牌A", "deepseek", "ok", False, "neutral", "未命中A", "api", False
        )
        miss2 = ProbeResult(
            "品牌A", "deepseek", "ok", False, "neutral", "未命中B", "api", False
        )
        miss3 = ProbeResult(
            "品牌A", "deepseek", "ok", False, "neutral", "未命中C", "api", False
        )
        # 2/5 命中 → 多数为否，上下文应来自未命中样本
        agg = search_ai._aggregate_samples([hit, hit2, miss1, miss2, miss3])
        assert agg.cited is False
        assert agg.prob == 0.4
        assert agg.context == "未命中A"

    def test_aggregate_samples_context_fallback_not_mismatched(self):
        # 判定为否且未命中样本 context 全空时，不得回退到命中样本的 context
        hit = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "命中内容", "api", False
        )
        miss = ProbeResult(
            "品牌A", "deepseek", "ok", False, "neutral", "", "api", False
        )
        agg = search_ai._aggregate_samples([hit, miss, miss, miss, miss])
        assert agg.cited is False
        assert agg.context == ""

    def test_aggregate_samples_keeps_all_answers(self):
        # sample_answers 不截断：--samples > 5 时全部样本原文可复核
        samples = [
            ProbeResult(
                "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
                meta={"answer": f"原始回答 {i}"},
            )
            for i in range(7)
        ]
        agg = search_ai._aggregate_samples(samples)
        assert len(agg.meta["sample_answers"]) == 7
        assert all(
            isinstance(a, dict) and "answer" in a and "cited" in a
            for a in agg.meta["sample_answers"]
        )

    def test_aggregate_samples_mine_cited_tie_not_true(self):
        # mine_cited 与 cited 统一严格多数：1/2 平局为否
        s1 = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
        )
        s2 = ProbeResult(
            "品牌A", "deepseek", "ok", False, "neutral", "c", "api", False,
            mine_cited=False, mine_ids=["https://a.com/1"],
        )
        agg = search_ai._aggregate_samples([s1, s2])
        assert agg.mine_cited is False

    def test_aggregate_samples_competitor_none_stays_none(self):
        # 未传 --competitor（全 None）时聚合结果应为 None（未知），与 build_trend 一致
        samples = [
            ProbeResult(
                "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
                competitor_matched=None,
            )
            for _ in range(3)
        ]
        agg = search_ai._aggregate_samples(samples)
        assert agg.competitor_matched is None

    def test_aggregate_samples_error_detail_from_majority(self):
        # 多数样本失败但首个正常：status=error 且保留错误详情
        ok = ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)
        bad1 = ProbeResult(
            "品牌A", "deepseek", "error", None, None, "boom", "api", True, error="network down"
        )
        bad2 = ProbeResult(
            "品牌A", "deepseek", "error", None, None, "boom", "api", True, error="network down"
        )
        agg = search_ai._aggregate_samples([ok, bad1, bad2])
        assert agg.status == "error"
        assert agg.error == "network down"
        assert agg.degraded is True

    def test_aggregate_samples_error_not_leaked_from_minority(self):
        # 多数派 status=ok 时，少数失败样本的错误不得混入聚合结果
        ok = ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)
        ok2 = ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)
        bad = ProbeResult(
            "品牌A", "deepseek", "error", None, None, "boom", "api", True, error="network"
        )
        agg = search_ai._aggregate_samples([ok, ok2, bad])
        assert agg.status == "ok"
        assert agg.error is None

    def test_aggregate_samples_all_invalid_prob_none(self):
        samples = [
            ProbeResult(
                "品牌A", "deepseek", "error", None, None, "boom", "api", True, error="x"
            )
            for _ in range(3)
        ]
        agg = search_ai._aggregate_samples(samples)
        assert agg.cited is None
        assert agg.prob is None
        assert agg.sample_count == 0

    def test_build_trend_excludes_invalid_samples(self, tmp_path):
        db = tmp_path / "monitor.db"
        ok = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            sample_idx=0,
        )
        bad = ProbeResult(
            "品牌A", "deepseek", "error", None, None, "boom", "api", True,
            error="x", sample_idx=1,
        )
        store_results(
            [ok, bad, bad],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        p = build_trend("品牌A", db_path=db)["series"]["deepseek"][0]
        assert p["n"] == 1 and p["hits"] == 1 and p["prob"] == 1.0
        assert p["invalid"] == 2
        assert p["cited"] is True

    def test_build_trend_aggregates_samples(self, tmp_path):
        db = tmp_path / "monitor.db"

        def mk(cited, sample_idx):
            return ProbeResult(
                "品牌A", "deepseek", "ok", cited, "positive", "c", "api", False,
                sample_idx=sample_idx,
            )

        store_results(
            [mk(True, 0), mk(True, 1), mk(False, 2)],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        store_results(
            [mk(False, 0), mk(True, 1), mk(False, 2)],
            db_path=db, run_at="2026-08-02T10:00:00+08:00",
        )
        trend = build_trend("品牌A", db_path=db)
        points = trend["series"]["deepseek"]
        assert len(points) == 2
        p1, p2 = points[0], points[1]
        assert p1["n"] == 3 and p1["hits"] == 2 and p1["prob"] == 2 / 3
        assert p2["n"] == 3 and p2["hits"] == 1 and p2["prob"] == 1 / 3
        assert p1["cited"] is True and p2["cited"] is False  # 多数派
        assert trend["changes"] == [{
            "platform": "deepseek",
            "from": True,
            "to": False,
            "run_at": "2026-08-02T10:00:00+08:00",
        }]
        delta = build_delta("品牌A", db_path=db, trend=trend)
        assert delta["platforms"]["deepseek"]["cited_change"] == "lost"

    def test_build_trend_tie_votes_not_cited(self, tmp_path):
        # 2 样本 1 命中：严格多数为否，与 _aggregate_samples 一致（不依赖插入顺序）
        db = tmp_path / "monitor.db"
        store_results([
            ProbeResult("品牌A", "deepseek", "ok", False, "neutral", "c", "api", False, sample_idx=0),
            ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False, sample_idx=1),
        ], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        p = build_trend("品牌A", db_path=db)["series"]["deepseek"][0]
        assert p["hits"] == 1 and p["n"] == 2
        assert p["cited"] is False

    def test_build_trend_separates_same_run_at_by_run_id(self, tmp_path):
        # 两次独立运行（两次 store 调用）即使 run_at 相同也按 run_id 分开
        db = tmp_path / "monitor.db"
        store_results([
            ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False),
        ], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([
            ProbeResult("品牌A", "deepseek", "ok", False, "neutral", "c", "api", False),
        ], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        points = build_trend("品牌A", db_path=db)["series"]["deepseek"]
        assert len(points) == 2
        assert {p["hits"] for p in points} == {0, 1}

    def test_build_trend_same_run_at_ordered_by_insertion(self, tmp_path):
        # 同 run_at 的两次独立运行按插入顺序排序，而非随机 run_id
        db = tmp_path / "monitor.db"
        store_results([
            ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False),
        ], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([
            ProbeResult("品牌A", "deepseek", "ok", False, "neutral", "c", "api", False),
        ], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        points = build_trend("品牌A", db_path=db)["series"]["deepseek"]
        assert [p["hits"] for p in points] == [1, 0]

    def test_build_trend_changes_stay_within_truncated_series(self, monkeypatch):
        # 截断到最近 1000 次运行后，变化点不得引用序列范围外的 run_at
        rows = []
        for i in range(1005):
            rows.append({
                "id": i + 1,
                "platform": "deepseek",
                "run_at": f"run-{i:04d}",
                "cited": 1 if i % 2 == 0 else 0,
                "status": "ok",
                "sentiment": "positive",
                "mine_cited": None,
                "mine_ids": None,
                "cited_type": None,
                "owned_ids": None,
                "competitor_matched": None,
                "competitor_ids": None,
                "fact_risks": None,
                "sample_idx": 0,
                "run_id": f"r{i}",
            })
        monkeypatch.setattr(
            search_ai, "load_history", lambda *a, **kw: rows
        )
        trend = search_ai.build_trend("品牌A", db_path=Path("unused.db"))
        shown = {p["run_at"] for p in trend["series"]["deepseek"]}
        assert len(shown) == 1000
        assert all(ch["run_at"] in shown for ch in trend["changes"])

    def test_render_markdown_trend_probability_format(self):
        # 趋势点概率格式与表格/CLI 一致：是 (80%, 4/5)
        trend = {
            "series": {
                "deepseek": [
                    {"run_at": "t1", "n": 5, "hits": 4, "prob": 0.8,
                     "ci_low": 0.38, "ci_high": 0.96, "cited": True,
                     "status": "ok", "sentiment": "positive",
                     "mine_cited": None, "mine_checked": False, "mine_ids": [],
                     "cited_type": None, "owned_ids": [],
                     "competitor_matched": None, "competitor_ids": [], "fact_risks": []},
                    {"run_at": "t2", "n": 5, "hits": 2, "prob": 0.4,
                     "ci_low": 0.12, "ci_high": 0.77, "cited": False,
                     "status": "ok", "sentiment": "neutral",
                     "mine_cited": None, "mine_checked": False, "mine_ids": [],
                     "cited_type": None, "owned_ids": [],
                     "competitor_matched": None, "competitor_ids": [], "fact_risks": []},
                ],
            },
            "changes": [],
            "total_runs": 2,
        }
        results = [ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)]
        md = render_markdown("品牌A", results, trend, [])
        assert "是 (80%, 4/5) → 否 (40%, 2/5)" in md

    def test_main_samples_loop_and_prob_display(self, tmp_path, monkeypatch, capsys):
        calls = {"n": 0}

        def fake_probe(query, mine_ids=None, owned_ids=None, competitor_ids=None):
            calls["n"] += 1
            return ProbeResult(
                query, "deepseek", "ok", calls["n"] != 3,
                "positive", "c", "api", False,
            )

        monkeypatch.setitem(search_ai.PLATFORMS["deepseek"], "probe", fake_probe)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        code = main([
            "--query", "codex", "--platforms", "deepseek", "--samples", "5",
            "--db", str(db), "--output", str(out),
        ])
        assert code == 0
        assert calls["n"] == 5
        captured = capsys.readouterr().out
        assert "被提及 是 (80%, 4/5)" in captured
        history = load_history("codex", db_path=db)
        assert len(history) == 5
        assert {r["sample_idx"] for r in history} == {0, 1, 2, 3, 4}

    def test_render_markdown_probability(self):
        r = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            prob=0.8, ci_low=0.38, ci_high=0.96, sample_count=5,
        )
        r.meta = {"sample_hits": 4}
        md = render_markdown("品牌A", [r], {"series": {}, "changes": []}, [])
        assert "是 (80%, 4/5)" in md

    def test_render_markdown_probability_with_invalid_note(self):
        # 存在无效样本时附注无效数，避免「4/5」被误读为总采样 5 次
        r = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            prob=1.0, ci_low=0.6, ci_high=1.0, sample_count=4,
        )
        r.meta = {"sample_hits": 4, "sample_invalid": 1}
        md = render_markdown("品牌A", [r], {"series": {}, "changes": []}, [])
        assert "是 (100%, 4/4，1 次无效)" in md

    def test_render_markdown_unknown_when_all_samples_invalid(self):
        # 全部样本无效（失败/未配置）时显示「未知」，不得显示「否 (0%, 0/5)」
        r = ProbeResult(
            "品牌A", "deepseek", "error", None, None, "boom", "api", True,
            prob=None, sample_count=0, error="x",
        )
        r.meta = {"sample_invalid": 5}
        md = render_markdown("品牌A", [r], {"series": {}, "changes": []}, [])
        assert "未知" in md
        assert "(0%, 0/" not in md

    def test_empty_inputs(self):
        assert _detect_mine("", ["a"]) == []
        assert _detect_mine("任意文本", []) == []

    def test_surrounding_spaces_stripped(self):
        assert _detect_mine("参考我的昵称 写的文章", [" 我的昵称 "]) == ["我的昵称"]

    def test_deduplicates_matched_ids(self):
        assert _detect_mine("文本 A 和 B", ["A", "A", "B"]) == ["A", "B"]

    def test_url_host_case_insensitive_path_sensitive(self):
        # 域名大小写不敏感 → 命中
        assert _detect_mine(
            "来源 https://ZhuanLan.Zhihu.com/p/123",
            ["https://zhuanlan.zhihu.com/p/123"],
        ) == ["https://zhuanlan.zhihu.com/p/123"]
        # 路径大小写不同 → 不命中（避免误报）
        assert _detect_mine(
            "来源 https://zhuanlan.zhihu.com/P/123",
            ["https://zhuanlan.zhihu.com/p/123"],
        ) == []

    def test_url_prefix_boundary_no_false_positive(self):
        # 主机前缀：example.com 不命中 example.com.evil.com
        assert _detect_mine("https://example.com.evil.com", ["https://example.com"]) == []
        # 路径前缀：/p/123 不命中 /p/1234
        assert _detect_mine("https://a.com/p/1234", ["https://a.com/p/123"]) == []
        # critical：/p/123 不命中 /p/123/456（路径边界含 /）
        assert _detect_mine("https://a.com/p/123/456", ["https://a.com/p/123"]) == []
        # 根域标识不命中子路径
        assert _detect_mine("https://example.com/p", ["https://example.com"]) == []
        # 精确路径命中
        assert _detect_mine("https://a.com/p/123", ["https://a.com/p/123"]) == ["https://a.com/p/123"]

    def test_url_sentence_period_and_slash_equivalence(self):
        # URL 后跟英文句号（其后为空白/结尾）→ 句子标点，正常命中
        assert _detect_mine("见 https://a.com/p. 接着", ["https://a.com/p"]) == ["https://a.com/p"]
        # 字面点路径：/p.x 命中自身，但不命中 /p.x/y
        assert _detect_mine("https://a.com/p.x", ["https://a.com/p.x"]) == ["https://a.com/p.x"]
        assert _detect_mine("https://a.com/p.x/y", ["https://a.com/p.x"]) == []
        # 尾斜杠等价：/p 与 /p/ 互为匹配
        assert _detect_mine("https://a.com/p/", ["https://a.com/p"]) == ["https://a.com/p"]
        assert _detect_mine("https://a.com/p", ["https://a.com/p/"]) == ["https://a.com/p/"]
        # 根域精确匹配
        assert _detect_mine("https://example.com", ["https://example.com"]) == ["https://example.com"]

    def test_url_root_with_sentence_period(self):
        # 根域后英文句号（其后为空白/结尾）→ 句子标点，命中
        assert _detect_mine("见 https://example.com. 下一条", ["https://example.com"]) == ["https://example.com"]
        assert _detect_mine("https://example.com.", ["https://example.com"]) == ["https://example.com"]

    def test_url_paren_then_period(self):
        # (https://a.com/p). 中 ) 后接英文句号 → 命中
        assert _detect_mine("见 (https://a.com/p). 接着", ["https://a.com/p"]) == ["https://a.com/p"]

    def test_url_query_params_ignored(self):
        # query 跟踪参数不参与比较：带 utm 的引用仍命中
        assert _detect_mine("https://a.com/p?utm_source=x", ["https://a.com/p"]) == ["https://a.com/p"]

    def test_url_identifier_trailing_punct_is_exact(self):
        # 配置标识保留原样：/p. 不误匹配 /p（互误判），精确匹配自身
        assert _detect_mine("https://a.com/p", ["https://a.com/p."]) == []
        # 根域尾斜杠等价：无斜杠标识命中带斜杠文本
        assert _detect_mine("https://example.com/", ["https://example.com"]) == ["https://example.com"]
        # 根域仍不命中子路径
        assert _detect_mine("https://example.com/p", ["https://example.com"]) == []


class TestDeepSeekProbe:
    def test_load_key_strips_quotes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setattr("search_ai.PROJECT_ROOT", tmp_path)
        (tmp_path / ".env").write_text('DEEPSEEK_API_KEY="sk-quoted"\n', encoding="utf-8")
        assert search_ai._load_key() == "sk-quoted"

    def test_no_key(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        result = probe_deepseek("测试品牌")
        assert result.status == "no_key"
        assert result.cited is None

    def test_cited_true(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(data=_deepseek_answer(True, "优秀", "测试品牌")),
        )
        result = probe_deepseek("测试品牌")
        assert result.status == "ok"
        assert result.cited is True
        assert result.sentiment == "positive"
        assert result.meta["answer"]
        assert result.confidence == "confirmed"  # 真实 API 探测

    def test_cited_false(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(data=_deepseek_answer(False)),
        )
        result = probe_deepseek("测试品牌")
        assert result.status == "ok"
        assert result.cited is False
        assert result.sentiment == "neutral"

    def test_mine_cited(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        answer = "可参考 https://zhuanlan.zhihu.com/p/123 的教程"
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(data={"choices": [{"message": {"content": answer}}]}),
        )
        result = probe_deepseek("codex 安装", mine_ids=["https://zhuanlan.zhihu.com/p/123"])
        assert result.mine_cited is True
        assert result.meta["mine_matched"] == ["https://zhuanlan.zhihu.com/p/123"]

    def test_mine_not_cited(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        answer = "可参考 https://zhuanlan.zhihu.com/p/123 的教程"
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(data={"choices": [{"message": {"content": answer}}]}),
        )
        result = probe_deepseek("codex 安装", mine_ids=["https://other.com/x"])
        assert result.mine_cited is False
        assert result.meta["mine_matched"] == []

    def test_no_mine_returns_none(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(data=_deepseek_answer(True, "优秀", "测试品牌")),
        )
        result = probe_deepseek("测试品牌", mine_ids=[])
        assert result.mine_cited is None
        assert result.mine_ids == []

    def test_request_error(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")

        def boom(*a, **kw):
            raise requests.exceptions.ConnectionError("network down")

        monkeypatch.setattr(requests, "post", boom)
        result = probe_deepseek("测试品牌")
        assert result.status == "error"
        assert result.cited is None
        assert "network down" in result.error

    def test_error_branch_carries_mine_ids(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")

        def boom(*a, **kw):
            raise requests.exceptions.ConnectionError("down")

        monkeypatch.setattr(requests, "post", boom)
        result = probe_deepseek("测试品牌", mine_ids=["https://a.com/1"])
        assert result.status == "error"
        assert result.mine_ids == ["https://a.com/1"]
        assert result.mine_cited is None

    def test_bad_json(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(json_exc=ValueError("bad json")),
        )
        result = probe_deepseek("测试品牌")
        assert result.status == "error"

    def test_non_object_json(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: FakeResponse(data=[1, 2, 3]),
        )
        result = probe_deepseek("测试品牌")
        assert result.status == "error"
        assert "unexpected_json_type" in result.error

    def test_choices_element_not_dict(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        for payload in ({"choices": [None]}, {"choices": ["oops"]}):
            def fake_post(*a, _payload=payload, **kw):
                return FakeResponse(data=_payload)

            monkeypatch.setattr(requests, "post", fake_post)
            result = probe_deepseek("测试品牌")
            assert result.status == "error"

    def test_content_not_string(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        payload = {"choices": [{"message": {"content": [1, 2]}}]}
        monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResponse(data=payload))
        result = probe_deepseek("测试品牌")
        assert result.status == "error"
        assert "unexpected_content_type" in result.error


class TestSearchInference:
    def test_cited_true(self, monkeypatch):
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **kw: FakeResponse(text=BING_HTML),
        )
        result = probe_search_inference("AI搜索优化", "kimi")
        assert result.status == "ok"
        assert result.cited is True
        assert result.source == "search_inference"
        assert result.degraded is True
        assert len(result.meta["results"]) == 2
        assert result.confidence == "likely"  # 搜索推断

    def test_cited_false(self, monkeypatch):
        html = BING_HTML.replace("AI搜索优化", "别的话题")
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        result = probe_search_inference("AI搜索优化", "doubao")
        assert result.status == "ok"
        assert result.cited is False
        assert result.context

    def test_mine_cited(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=BING_HTML))
        result = probe_search_inference("AI搜索优化", "kimi", mine_ids=["https://example.com/1"])
        assert result.mine_cited is True
        assert result.meta["mine_matched"] == ["https://example.com/1"]

    def test_mine_not_cited(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=BING_HTML))
        result = probe_search_inference("AI搜索优化", "kimi", mine_ids=["https://nope.example/x"])
        assert result.mine_cited is False
        assert result.meta["mine_matched"] == []

    def test_non_url_mine_does_not_match_url_field(self, monkeypatch):
        # 非 URL 标识（如年份 2024）只扫 title+snippet，不扫 URL
        html = """
        <ol id="b_results">
          <li class="b_algo">
            <h2><a href="https://example.com/2024-guide">安装与配置教程</a></h2>
            <p>一步步讲解安装步骤。</p>
          </li>
        </ol>
        """
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        result = probe_search_inference("codex", "kimi", mine_ids=["2024"])
        assert result.mine_cited is False
        assert result.meta["mine_matched"] == []

    def test_search_inference_tolerates_missing_snippet_key(self, monkeypatch):
        # 防御：_parse_bing 返回项缺 snippet 键时，竞品/我的内容检测不抛 KeyError
        items = [{"title": "AI搜索优化入门", "url": "https://example.com/comp"}]
        monkeypatch.setattr(search_ai, "_parse_bing", lambda html: items)
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text="<html>"))
        result = probe_search_inference(
            "AI搜索优化", "kimi",
            mine_ids=["https://example.com/comp"],
            competitor_ids=["https://example.com/comp"],
        )
        assert result.status == "ok"
        assert result.cited is True
        assert result.mine_cited is True
        assert result.competitor_matched is True

    def test_non_url_mine_matches_title(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=BING_HTML))
        result = probe_search_inference("AI搜索优化", "kimi", mine_ids=["AI 搜索优化入门"])
        assert result.mine_cited is True

    def test_url_keyword_does_not_trigger_cited(self, monkeypatch):
        # URL 含查询词（codex）但标题/摘要不含：cited 应为 False，mine 仍可匹配 URL
        html = """
        <ol id="b_results">
          <li class="b_algo">
            <h2><a href="https://example.com/codex-guide">安装与配置教程</a></h2>
            <p>一步步讲解安装步骤。</p>
          </li>
        </ol>
        """
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text=html))
        result = probe_search_inference("codex", "kimi", mine_ids=["https://example.com/codex-guide"])
        assert result.cited is False
        assert result.mine_cited is True

    def test_request_error(self, monkeypatch):
        def boom(*a, **kw):
            raise requests.exceptions.Timeout("slow")

        monkeypatch.setattr(requests, "get", boom)
        result = probe_search_inference("AI搜索优化", "yuanbao")
        assert result.status == "error"

    def test_inference_error_carries_mine_ids(self, monkeypatch):
        def boom(*a, **kw):
            raise requests.exceptions.Timeout("slow")

        monkeypatch.setattr(requests, "get", boom)
        result = probe_search_inference("AI搜索优化", "kimi", mine_ids=["https://a.com/1"])
        assert result.status == "error"
        assert result.mine_ids == ["https://a.com/1"]
        assert result.mine_cited is None

    def test_unparseable(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(text="<html>anti-bot</html>"))
        result = probe_search_inference("AI搜索优化", "kimi")
        assert result.status == "error"
        assert result.error == "no_results_parsed"


class TestDB:
    def test_migration_adds_mine_columns(self, tmp_path):
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE probes (id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL,"
            " platform TEXT NOT NULL, run_at TEXT NOT NULL, status TEXT NOT NULL, cited INTEGER,"
            " sentiment TEXT, context TEXT, source TEXT NOT NULL,"
            " degraded INTEGER NOT NULL DEFAULT 0, error TEXT, meta TEXT)"
        )
        conn.commit()
        conn.close()
        conn = connect(db)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(probes)").fetchall()}
        assert "mine_cited" in cols
        assert "mine_ids" in cols
        conn.close()

    def test_store_and_load(self, tmp_path):
        db = tmp_path / "monitor.db"
        rows = [
            ProbeResult("品牌A", "deepseek", "ok", True, "positive", "ctx1", "api", False),
            ProbeResult(
                "品牌A", "kimi", "ok", False, None, "ctx2", "search_inference", True,
                meta={"note": "搜索引擎存在信号"},
            ),
        ]
        run_at = store_results(rows, db_path=db, run_at="2026-08-06T10:00:00+08:00")
        assert run_at == "2026-08-06T10:00:00+08:00"
        history = load_history("品牌A", db_path=db)
        assert len(history) == 2
        by_platform = {h["platform"]: h for h in history}
        assert by_platform["deepseek"]["cited"] == 1
        assert by_platform["deepseek"]["sentiment"] == "positive"
        assert by_platform["kimi"]["degraded"] == 1
        meta = json.loads(by_platform["kimi"]["meta"])
        assert meta["note"] == "搜索引擎存在信号"
        assert json.loads(by_platform["deepseek"]["meta"]) == {}

    def test_store_and_load_mine_fields(self, tmp_path):
        db = tmp_path / "monitor.db"
        row = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1", "我的昵称"],
        )
        store_results([row], db_path=db, run_at="2026-08-06T10:00:00+08:00")
        history = load_history("品牌A", db_path=db)
        assert history[0]["mine_cited"] == 1
        assert json.loads(history[0]["mine_ids"]) == ["https://a.com/1", "我的昵称"]

    def test_schema_has_clean_b3_columns(self, tmp_path):
        # regression: quoted column names must not appear in CREATE TABLE
        db = tmp_path / "monitor.db"
        conn = search_ai.connect(db)
        cols = [c[1] for c in conn.execute("PRAGMA table_info(probes)").fetchall()]
        conn.close()
        assert cols == [
            "id", "query", "platform", "run_at", "status", "cited", "sentiment",
            "context", "source", "degraded", "error", "meta", "mine_cited",
            "mine_ids", "confidence", "cited_type", "owned_ids",
            "competitor_matched", "competitor_ids", "fact_risks", "sample_idx", "run_id",
        ]
        assert not any(chr(39) in c for c in cols)

    def test_store_and_load_confidence(self, tmp_path):
        db = tmp_path / "monitor.db"
        row = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            confidence="confirmed",
        )
        store_results([row], db_path=db, run_at="2026-08-06T10:00:00+08:00")
        history = load_history("品牌A", db_path=db)
        assert history[0]["confidence"] == "confirmed"

    def test_store_and_load_b3_quality_fields(self, tmp_path):
        db = tmp_path / "monitor.db"
        row = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            meta={"answer": "原始回答", "mine_checked": ["https://a.com/1"]},
            mine_cited=True, mine_ids=["https://a.com/1"],
            cited_type="owned", owned_ids=["https://a.com/1"],
            competitor_matched=True, competitor_ids=["https://comp.example.com"],
            fact_risks=["版本 2.3.1"],
        )
        store_results([row], db_path=db, run_at="2026-08-06T10:00:00+08:00")
        saved = load_history("品牌A", db_path=db)[0]
        assert json.loads(saved["meta"])["answer"] == "原始回答"
        assert saved["mine_cited"] == 1
        assert saved["cited_type"] == "owned"
        assert json.loads(saved["owned_ids"]) == ["https://a.com/1"]
        assert saved["competitor_matched"] == 1
        assert json.loads(saved["competitor_ids"]) == ["https://comp.example.com"]
        assert json.loads(saved["fact_risks"]) == ["版本 2.3.1"]

    def test_build_delta(self, tmp_path):
        db = tmp_path / "monitor.db"
        r1 = ProbeResult(
            "品牌A", "deepseek", "ok", False, "neutral", "c", "api",
            degraded=False, mine_cited=False, mine_ids=["https://a.com/1"],
        )
        r2 = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api",
            degraded=False, mine_cited=True, mine_ids=["https://a.com/1"],
        )
        store_results([r1], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([r2], db_path=db, run_at="2026-08-02T10:00:00+08:00")
        delta = build_delta("品牌A", db_path=db)
        item = delta["platforms"]["deepseek"]
        assert item["cited_change"] == "added"
        assert item["sentiment_flip"] == "neutral→positive"
        assert item["mine_change"] == "gained"
        assert delta["has_history"] is True

    def test_build_delta_single_run_no_history(self, tmp_path):
        db = tmp_path / "monitor.db"
        r = ProbeResult(
            "品牌A", "deepseek", "ok", False, "neutral", "c", "api",
            degraded=False,
        )
        store_results([r], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        delta = build_delta("品牌A", db_path=db)
        assert delta["has_history"] is False
        assert delta["platforms"] == {}

    def test_build_delta_marks_no_valid_data_for_latest_failure(self, tmp_path):
        # 本次探测失败时，不得把历史两次有效快照的对比误报成「本次 vs 上次」
        db = tmp_path / "monitor.db"
        ok_run = ProbeResult(
            "品牌A", "deepseek", "ok", False, "neutral", "c", "api",
            degraded=False,
        )
        failed_run = ProbeResult(
            "品牌A", "deepseek", "error", None, None, "网络异常", "api",
            degraded=True, error="boom",
        )
        store_results([ok_run], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([failed_run], db_path=db, run_at="2026-08-02T10:00:00+08:00")
        delta = build_delta("品牌A", db_path=db)
        item = delta["platforms"]["deepseek"]
        assert item["status"] == "error"
        assert "无有效数据" in item["note"]
        assert "cited_change" not in item
        # note 条目不算真实对比，has_history 不得为 True
        assert delta["has_history"] is False

    def test_build_delta_skips_shell_entries_without_comparison(self, tmp_path):
        # 两个有效快照均无可对比数据时，不写入空壳条目
        db = tmp_path / "monitor.db"
        r1 = ProbeResult(
            "品牌A", "deepseek", "ok", None, None, "c", "api",
            degraded=False,
        )
        r2 = ProbeResult(
            "品牌A", "deepseek", "ok", None, None, "c", "api",
            degraded=False,
        )
        store_results([r1], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([r2], db_path=db, run_at="2026-08-02T10:00:00+08:00")
        delta = build_delta("品牌A", db_path=db)
        assert delta["platforms"] == {}
        assert delta["has_history"] is False

    def test_build_delta_accepts_prebuilt_trend(self, tmp_path):
        # 复用 main 已构建的 trend，避免 build_delta 内部重复读库
        db = tmp_path / "monitor.db"
        r1 = ProbeResult(
            "品牌A", "deepseek", "ok", False, "neutral", "c", "api",
            degraded=False,
        )
        r2 = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api",
            degraded=False,
        )
        store_results([r1], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([r2], db_path=db, run_at="2026-08-02T10:00:00+08:00")
        trend = build_trend("品牌A", db_path=db)
        assert build_delta("品牌A", db_path=db, trend=trend) == build_delta(
            "品牌A", db_path=db
        )

    def test_build_delta_filters_by_requested_platforms(self, tmp_path):
        # 只对本次探测的平台生成对比，历史其他平台不混入
        db = tmp_path / "monitor.db"

        def mk(p, cited):
            return ProbeResult(
                "品牌A", p, "ok", cited, "positive", "c", "api",
                degraded=False,
            )

        store_results(
            [mk("deepseek", False), mk("kimi", True)],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        store_results(
            [mk("deepseek", True), mk("kimi", False)],
            db_path=db, run_at="2026-08-02T10:00:00+08:00",
        )
        delta = build_delta("品牌A", db_path=db, platforms=["deepseek"])
        assert set(delta["platforms"]) == {"deepseek"}
        assert delta["platforms"]["deepseek"]["cited_change"] == "added"
        assert delta["has_history"] is True

    def test_build_delta_sorts_trend_points_by_run_at(self, tmp_path):
        # 不依赖外部 trend 的排序，显式按 run_at 升序取最新点
        trend = {
            "series": {
                "deepseek": [
                    {
                        "run_at": "2026-08-02T10:00:00+08:00",
                        "status": "ok", "cited": True,
                        "sentiment": "positive",
                        "mine_cited": None, "mine_checked": False, "mine_ids": [],
                    },
                    {
                        "run_at": "2026-08-01T10:00:00+08:00",
                        "status": "ok", "cited": False,
                        "sentiment": "neutral",
                        "mine_cited": None, "mine_checked": False, "mine_ids": [],
                    },
                ],
            },
            "changes": [],
            "total_runs": 2,
        }
        delta = build_delta("品牌A", db_path=tmp_path / "monitor.db", trend=trend)
        item = delta["platforms"]["deepseek"]
        assert item["run_at"] == "2026-08-02T10:00:00+08:00"
        assert item["cited_change"] == "added"

    def test_build_delta_detects_competitor_replaced(self, tmp_path):
        # lostprompt：上次被引用、本次未被引用但话题仍被提及、本次检出竞品
        db = tmp_path / "monitor.db"
        prev = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
        )
        last = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=False, mine_ids=["https://a.com/1"],
            competitor_matched=True,
        )
        store_results([prev], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([last], db_path=db, run_at="2026-08-02T10:00:00+08:00")
        delta = build_delta("品牌A", db_path=db)
        item = delta["platforms"]["deepseek"]
        assert item["competitor_replaced"] is True
        # 上次未检查竞品（None）→ 推断待人工确认，而非已确认
        assert item["competitor_replaced_confirmed"] is False
        assert item["mine_change"] == "lost"
        assert delta["has_history"] is True

    def test_build_delta_competitor_replaced_confirmed_when_prev_missed(self, tmp_path):
        # 上次明确未命中竞品（False）→ 竞品夺走为已确认
        db = tmp_path / "monitor.db"
        prev = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
            competitor_matched=False, competitor_ids=["https://comp.example.com"],
        )
        last = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=False, mine_ids=["https://a.com/1"],
            competitor_matched=True, competitor_ids=["https://comp.example.com"],
        )
        store_results([prev], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([last], db_path=db, run_at="2026-08-02T10:00:00+08:00")
        item = build_delta("品牌A", db_path=db)["platforms"]["deepseek"]
        assert item["competitor_replaced"] is True
        assert item["competitor_replaced_confirmed"] is True

    def test_build_delta_no_competitor_replaced_when_mine_ids_changed(self, tmp_path):
        # 前后两次 mine_ids 不一致（用户更换标识）时不判竞品夺走，避免假回归
        db = tmp_path / "monitor.db"
        prev = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
            competitor_matched=False,
        )
        last = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=False, mine_ids=["https://a.com/2"],
            competitor_matched=True,
        )
        store_results([prev], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([last], db_path=db, run_at="2026-08-02T10:00:00+08:00")
        item = build_delta("品牌A", db_path=db)["platforms"]["deepseek"]
        assert item.get("competitor_replaced") is not True

    def test_build_delta_competitor_replaced_inferred_when_competitor_ids_changed(self, tmp_path):
        # 前后竞品标识不一致（用户更换 --competitor）时不得确认夺走，按推断处理
        db = tmp_path / "monitor.db"
        prev = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
            competitor_matched=False, competitor_ids=["https://comp-a.example"],
        )
        last = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=False, mine_ids=["https://a.com/1"],
            competitor_matched=True, competitor_ids=["https://comp-b.example"],
        )
        store_results([prev], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([last], db_path=db, run_at="2026-08-02T10:00:00+08:00")
        item = build_delta("品牌A", db_path=db)["platforms"]["deepseek"]
        assert item["competitor_replaced"] is True
        assert item["competitor_replaced_confirmed"] is False

    def test_build_delta_no_competitor_replaced_when_competitor_persists(self, tmp_path):
        # 上一轮竞品已命中时不判「夺走」：竞品一直在场，本轮丢失引用不是被替换
        db = tmp_path / "monitor.db"
        prev = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
            competitor_matched=True,
        )
        last = ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=False, mine_ids=["https://a.com/1"],
            competitor_matched=True,
        )
        store_results([prev], db_path=db, run_at="2026-08-01T10:00:00+08:00")
        store_results([last], db_path=db, run_at="2026-08-02T10:00:00+08:00")
        delta = build_delta("品牌A", db_path=db)
        item = delta["platforms"]["deepseek"]
        assert item.get("competitor_replaced") is not True
        assert item["mine_change"] == "lost"

    def test_default_run_at_has_microsecond_precision(self, tmp_path):
        db = tmp_path / "monitor.db"
        run_at = store_results(
            [ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)],
            db_path=db,
        )
        assert "." in run_at

    def test_trend_detects_change(self, tmp_path):
        db = tmp_path / "monitor.db"
        store_results(
            [ProbeResult("品牌A", "deepseek", "ok", False, "neutral", "c", "api", False)],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        store_results(
            [ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)],
            db_path=db, run_at="2026-08-06T10:00:00+08:00",
        )
        trend = build_trend("品牌A", db_path=db)
        assert trend["total_runs"] == 2
        assert len(trend["series"]["deepseek"]) == 2
        assert trend["changes"] == [
            {"platform": "deepseek", "from": False, "to": True, "run_at": "2026-08-06T10:00:00+08:00"}
        ]

    def test_trend_detects_change_across_invalid_probe(self, tmp_path):
        db = tmp_path / "monitor.db"
        store_results(
            [ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        store_results(
            [ProbeResult("品牌A", "deepseek", "no_key", None, None, "c", "api", False)],
            db_path=db, run_at="2026-08-03T10:00:00+08:00",
        )
        store_results(
            [ProbeResult("品牌A", "deepseek", "ok", False, "neutral", "c", "api", False)],
            db_path=db, run_at="2026-08-06T10:00:00+08:00",
        )
        trend = build_trend("品牌A", db_path=db)
        assert trend["total_runs"] == 3
        assert trend["changes"] == [
            {"platform": "deepseek", "from": True, "to": False, "run_at": "2026-08-06T10:00:00+08:00"}
        ]

    def test_trend_ignores_only_invalid_probe(self, tmp_path):
        db = tmp_path / "monitor.db"
        store_results(
            [ProbeResult("品牌A", "deepseek", "no_key", None, None, "c", "api", False)],
            db_path=db, run_at="2026-08-03T10:00:00+08:00",
        )
        trend = build_trend("品牌A", db_path=db)
        assert trend["total_runs"] == 1
        assert trend["changes"] == []

    def test_trend_marks_mine_checked(self, tmp_path):
        db = tmp_path / "monitor.db"
        store_results(
            [ProbeResult(
                "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
                mine_cited=True, mine_ids=["https://a.com/1"],
            )],
            db_path=db, run_at="2026-08-06T10:00:00+08:00",
        )
        store_results(
            [ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)],
            db_path=db, run_at="2026-08-06T11:00:00+08:00",
        )
        trend = build_trend("品牌A", db_path=db)
        points = trend["series"]["deepseek"]
        assert points[0]["mine_checked"] is True
        assert points[1]["mine_checked"] is False

    def test_trend_tolerates_malformed_mine_ids(self, tmp_path):
        db = tmp_path / "monitor.db"
        store_results(
            [ProbeResult(
                "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
                mine_cited=True, mine_ids=["https://a.com/1"],
            )],
            db_path=db, run_at="2026-08-06T10:00:00+08:00",
        )
        conn = connect(db)
        conn.execute("UPDATE probes SET mine_ids='{bad json' WHERE query='品牌A'")
        conn.commit()
        conn.close()
        trend = build_trend("品牌A", db_path=db)  # 坏数据不崩
        assert trend["series"]["deepseek"][0]["mine_checked"] is False

    def test_trend_filters_non_string_mine_ids(self, tmp_path):
        db = tmp_path / "monitor.db"
        store_results(
            [ProbeResult(
                "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
                mine_cited=True, mine_ids=["https://a.com/1"],
            )],
            db_path=db, run_at="2026-08-06T10:00:00+08:00",
        )
        conn = connect(db)
        conn.execute("UPDATE probes SET mine_ids='[1, 2]' WHERE query='品牌A'")
        conn.commit()
        conn.close()
        trend = build_trend("品牌A", db_path=db)  # 非字符串元素被过滤，不抛 TypeError
        point = trend["series"]["deepseek"][0]
        assert point["mine_ids"] == []
        assert point["mine_checked"] is False

    def test_no_history(self, tmp_path):
        db = tmp_path / "monitor.db"
        trend = build_trend("不存在", db_path=db)
        assert trend["total_runs"] == 0
        assert trend["series"] == {}


class TestRecommendations:
    def test_deepseek_not_cited_p0(self):
        results = [ProbeResult("品牌A", "deepseek", "ok", False, "neutral", "c", "api", False)]
        recs = build_recommendations("品牌A", results)
        assert any(r.priority == "P0" and r.dimension == "AI 引用" for r in recs)
        assert all(r.falsifiability_check for r in recs)

    def test_no_key_p1(self):
        results = [ProbeResult("品牌A", "deepseek", "no_key", None, None, "c", "api", False)]
        recs = build_recommendations("品牌A", results)
        assert any(r.priority == "P1" and "DEEPSEEK_API_KEY" in r.action for r in recs)

    def test_negative_p0(self):
        results = [ProbeResult("品牌A", "deepseek", "ok", True, "negative", "c", "api", False)]
        recs = build_recommendations("品牌A", results)
        assert any(r.priority == "P0" and r.dimension == "舆情" for r in recs)

    def test_error_p1(self):
        results = [ProbeResult("品牌A", "kimi", "error", None, None, "c", "search_inference", True, error="x")]
        recs = build_recommendations("品牌A", results)
        assert any(r.priority == "P1" and r.dimension == "数据可用性" for r in recs)

    def test_cited_positive_p2(self):
        results = [ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)]
        recs = build_recommendations("品牌A", results)
        assert any(r.priority == "P2" and r.dimension == "持续监测" for r in recs)

    def test_mine_not_cited_p0(self):
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=False, mine_ids=["https://a.com/1"],
        )]
        recs = build_recommendations("codex 如何安装", results)
        assert any(r.priority == "P0" and r.dimension == "内容引用归属" for r in recs)
        assert all(r.falsifiability_check for r in recs)
        mine_rec = next(r for r in recs if r.dimension == "内容引用归属")
        assert "--query 'codex 如何安装'" in mine_rec.falsifiability_check
        assert "--mine https://a.com/1" in mine_rec.falsifiability_check

    def test_shell_quote_handles_special_chars(self):
        assert _shell_quote("a$b`c\\d") == "'a$b`c\\d'"
        assert _shell_quote("https://a.com/1") == "https://a.com/1"

    def test_inference_mine_missing_p1(self):
        results = [ProbeResult(
            "品牌A", "kimi", "ok", True, None, "c", "search_inference", True,
            mine_cited=False, mine_ids=["https://a.com/1"],
        )]
        recs = build_recommendations("codex 如何安装", results)
        assert any(r.priority == "P1" and r.dimension == "内容收录" for r in recs)
        rec = next(r for r in recs if r.dimension == "内容收录")
        assert "--query 'codex 如何安装'" in rec.falsifiability_check
        assert "--mine https://a.com/1" in rec.falsifiability_check

    def test_mine_cited_no_p0(self):
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
        )]
        recs = build_recommendations("品牌A", results)
        assert not any(r.dimension == "内容引用归属" for r in recs)

    def test_competitor_replaced_confirmed_p1(self):
        # 单次对比样本（AI 回答有随机性）不直接给 P0，降为 P1 并建议重跑确认
        delta = {
            "platforms": {
                "deepseek": {
                    "competitor_replaced": True,
                    "competitor_replaced_confirmed": True,
                    "competitor_replaced_at": "2026-08-02T10:00:00+08:00",
                },
            }
        }
        recs = build_recommendations("品牌A", [], delta=delta)
        confirmed = [r for r in recs if r.dimension == "竞品夺走"]
        assert confirmed and confirmed[0].priority == "P1"
        assert "单次对比样本" in confirmed[0].action
        assert all(r.falsifiability_check for r in recs)

    def test_competitor_replaced_inferred_p1(self):
        # 推断（上次未检查竞品）降为 P1，并明确标注待人工确认
        delta = {
            "platforms": {
                "deepseek": {
                    "competitor_replaced": True,
                    "competitor_replaced_confirmed": False,
                    "competitor_replaced_at": "2026-08-02T10:00:00+08:00",
                },
            }
        }
        recs = build_recommendations("品牌A", [], delta=delta)
        inferred = [r for r in recs if r.dimension == "竞品夺走（推断）"]
        assert inferred and inferred[0].priority == "P1"
        assert "人工确认" in inferred[0].action
        assert not any(r.priority == "P0" and r.dimension == "竞品夺走" for r in recs)

    def test_competitor_replaced_falsifiability_uses_actual_mine(self):
        # 复跑命令必须使用实际 mine 标识（标题/作者名），而非硬编码 URL 占位
        delta = {
            "platforms": {
                "deepseek": {
                    "competitor_replaced": True,
                    "competitor_replaced_confirmed": True,
                },
            }
        }
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=False, mine_ids=["我的昵称"],
        )]
        recs = build_recommendations("品牌A", results, delta=delta)
        rec = next(r for r in recs if r.dimension == "竞品夺走")
        assert "--mine '我的昵称'" in rec.falsifiability_check

    def test_competitor_replaced_falsifiability_joins_all_mine_ids(self):
        # 多个 --mine 标识时复跑命令须全部拼接，不能只取第一个
        delta = {
            "platforms": {
                "deepseek": {
                    "competitor_replaced": True,
                    "competitor_replaced_confirmed": True,
                },
            }
        }
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=False, mine_ids=["https://a.com/1", "我的昵称"],
            competitor_ids=["https://comp.example.com"],
        )]
        recs = build_recommendations("品牌A", results, delta=delta)
        rec = next(r for r in recs if r.dimension == "竞品夺走")
        assert "--mine https://a.com/1 --mine '我的昵称'" in rec.falsifiability_check
        assert "--competitor https://comp.example.com" in rec.falsifiability_check
        assert "--competitor <竞品标识>" not in rec.falsifiability_check

    def test_fact_risks_p1(self):
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            fact_risks=["版本 2.3.1"],
        )]
        recs = build_recommendations("品牌A", results)
        assert any(r.priority == "P1" and r.dimension == "信息风险" for r in recs)


class TestReport:
    def test_render_markdown(self):
        results = [
            ProbeResult("品牌A", "deepseek", "ok", True, "positive", "很好的上下文", "api", False),
            ProbeResult("品牌A", "kimi", "ok", False, None, "没有命中", "search_inference", True),
        ]
        recs = [Recommendation("P0", "AI 引用", "补充自包含段落", "提升命中", "重跑后变为是")]
        md = render_markdown("品牌A", results, {"series": {"deepseek": [{"run_at": "x", "cited": True, "status": "ok", "sentiment": "positive"}]}, "changes": [], "total_runs": 1}, recs)
        assert "品牌A" in md
        assert "DeepSeek" in md
        assert "不等同于该平台真实引用" in md
        assert "验证方式" in md

    def test_render_markdown_shows_confidence(self):
        results = [
            ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
                        confidence="confirmed"),
            ProbeResult("品牌A", "kimi", "ok", False, None, "c2", "search_inference", True,
                        confidence="likely"),
        ]
        md = render_markdown("品牌A", results, {"series": {}, "changes": []}, [])
        assert "| Confirmed |" in md
        assert "| Likely |" in md

    def test_render_json_includes_confidence(self):
        results = [
            ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
                        confidence="confirmed"),
            ProbeResult("品牌A", "kimi", "ok", False, None, "c2", "search_inference", True,
                        confidence="likely"),
        ]
        data = render_json("品牌A", results, {"series": {}, "changes": []}, [])
        confs = {r["platform"]: r["confidence"] for r in data["results"]}
        assert confs == {"deepseek": "confirmed", "kimi": "likely"}

    def test_render_markdown_shows_delta(self):
        delta = {
            "platforms": {
                "deepseek": {"cited_change": "added", "sentiment_flip": "neutral→positive"},
            }
        }
        md = render_markdown("品牌A", [], {"series": {}, "changes": []}, [], delta)
        assert "与上次对比" in md
        assert "新增被提及" in md
        assert "neutral→positive" in md

    def test_render_markdown_table_rows_contiguous(self):
        # 回归：表格行之间不得插入空行（否则破坏 Markdown 表格渲染）
        results = [
            ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False),
            ProbeResult("品牌A", "kimi", "ok", False, None, "c2", "search_inference", True),
        ]
        md = render_markdown("品牌A", results, {"series": {}, "changes": []}, [])
        lines = md.splitlines()
        start = lines.index("## 本次快照")
        end = lines.index("## 趋势对比")
        for i in range(start, end):
            if (
                lines[i] == ""
                and i > start
                and lines[i - 1].startswith("|")
                and i + 1 < end
                and lines[i + 1].startswith("|")
            ):
                raise AssertionError("表格行之间出现空行")

    def test_render_markdown_delta_preceded_by_blank_when_no_results(self):
        # 回归：results 为空时，与上次对比章节前仍应有空行分隔
        delta = {"platforms": {"deepseek": {"cited_change": "added"}}}
        md = render_markdown("品牌A", [], {"series": {}, "changes": []}, [], delta)
        lines = md.splitlines()
        idx = lines.index("## 与上次对比")
        assert idx > 0 and lines[idx - 1] == ""

    def test_render_markdown_delta_shows_no_valid_data_note(self):
        delta = {
            "platforms": {
                "deepseek": {
                    "run_at": "t",
                    "status": "error",
                    "note": "本次探测无有效数据，未参与与上次对比",
                },
            }
        }
        md = render_markdown("品牌A", [], {"series": {}, "changes": []}, [], delta)
        assert "本次探测无有效数据，未参与与上次对比" in md

    def test_render_markdown_delta_skips_shell_rows(self):
        # 外部传入仅含 run_at 的空壳条目时，不渲染全「—」的对比表格
        delta = {
            "platforms": {
                "deepseek": {"run_at": "t", "previous_run_at": "t0"},
            }
        }
        md = render_markdown("品牌A", [], {"series": {}, "changes": []}, [], delta)
        assert "与上次对比" not in md

    def test_render_markdown_delta_same_shown_as_no_change(self):
        # cited_change == "same" 时显示「无变化」，与 CLI 一致，避免唯一对比也成全「—」行
        delta = {"platforms": {"deepseek": {"cited_change": "same"}}}
        md = render_markdown("品牌A", [], {"series": {}, "changes": []}, [], delta)
        assert "无变化" in md

    def test_render_markdown_b3_risk_section(self):
        delta = {
            "platforms": {
                "deepseek": {
                    "competitor_replaced": True,
                    "competitor_replaced_confirmed": False,
                    "competitor_replaced_at": "2026-08-02T10:00:00+08:00",
                },
            }
        }
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"], cited_type="earned",
            fact_risks=["版本 2.3.1"],
        )]
        md = render_markdown("品牌A", results, {"series": {}, "changes": []}, [], delta)
        assert "## 风险提示" in md
        assert "竞品夺走" in md
        assert "推断" in md
        assert "前后竞品标识不一致" in md
        assert "版本 2.3.1" in md
        assert "是（原创）" in md

    def test_render_markdown_no_risk_section_when_clean(self):
        results = [ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)]
        md = render_markdown("品牌A", results, {"series": {}, "changes": []}, [])
        assert "## 风险提示" not in md

    def test_render_markdown_mine_unknown_type_not_marked_owned(self):
        # mine_cited=True 但 cited_type 缺失（旧数据/手工构造）时不得误标「转载」
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
        )]
        md = render_markdown("品牌A", results, {"series": {}, "changes": []}, [])
        assert "是（未知）" in md
        assert "（转载）" not in md

    def test_render_markdown_filters_changes_by_requested_platforms(self):
        results = [ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)]
        trend = {
            "series": {"deepseek": [], "kimi": []},
            "changes": [
                {"platform": "kimi", "from": False, "to": True, "run_at": "2026-08-01T10:00:00+08:00"},
                {"platform": "deepseek", "from": False, "to": True, "run_at": "2026-08-06T10:00:00+08:00"},
            ],
            "total_runs": 3,
        }
        md = render_markdown("品牌A", results, trend, [])
        # 只保留本次运行平台（deepseek）的变化点，kimi 的旧变化不混入
        assert md.count("由「否」变为「是」") == 1

    def test_render_markdown_with_mine_column(self):
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
        )]
        md = render_markdown("品牌A", results, {"series": {}, "changes": []}, [])
        assert "我的内容" in md
        assert "--mine" in md

    def test_render_markdown_trend_shows_mine_unknown(self):
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
        )]
        trend = {
            "series": {
                "deepseek": [
                    {"run_at": "t1", "cited": True, "status": "ok", "sentiment": "positive",
                     "mine_cited": True, "mine_checked": True, "mine_ids": ["https://a.com/1"]},
                    {"run_at": "t2", "cited": True, "status": "error", "sentiment": None,
                     "mine_cited": None, "mine_checked": True, "mine_ids": ["https://a.com/1"]},
                    {"run_at": "t3", "cited": False, "status": "ok", "sentiment": "neutral",
                     "mine_cited": False, "mine_checked": True, "mine_ids": ["https://a.com/1"]},
                ]
            },
            "changes": [],
            "total_runs": 3,
        }
        md = render_markdown("品牌A", results, trend, [])
        assert "我的内容(https://a.com/1)：是 → 未知 → 否" in md

    def test_render_markdown_trend_marks_unchecked(self):
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
        )]
        trend = {
            "series": {
                "deepseek": [
                    {"run_at": "t1", "cited": True, "status": "ok", "sentiment": "positive",
                     "mine_cited": True, "mine_checked": True, "mine_ids": ["https://a.com/1"]},
                    {"run_at": "t2", "cited": True, "status": "ok", "sentiment": "positive",
                     "mine_cited": None, "mine_checked": False, "mine_ids": []},
                    {"run_at": "t3", "cited": False, "status": "ok", "sentiment": "neutral",
                     "mine_cited": False, "mine_checked": True, "mine_ids": ["https://a.com/1"]},
                ]
            },
            "changes": [],
            "total_runs": 3,
        }
        md = render_markdown("品牌A", results, trend, [])
        assert "我的内容(https://a.com/1)：是（第1次） → 否（第3次）" in md
        assert "未检查 1 次（第2次）" in md

    def test_save_report(self, tmp_path):
        results = [ProbeResult("品牌A", "deepseek", "ok", True, "positive", "c", "api", False)]
        paths = save_report("品牌A", results, {"series": {}}, [], out_dir=tmp_path)
        assert len(paths) == 2
        assert paths[0].suffix == ".md"
        assert paths[1].suffix == ".json"
        data = json.loads(paths[1].read_text(encoding="utf-8"))
        assert data["query"] == "品牌A"
        assert data["results"][0]["platform"] == "deepseek"


class TestCLI:
    def test_main_no_key_exit_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        code = main(["--query", "测试品牌", "--platforms", "deepseek", "--db", str(db), "--output", str(out)])
        assert code == 0
        assert out.exists()
        assert db.exists()
        assert len(list(out.glob("track-*.md"))) == 1
        history = load_history("测试品牌", db_path=db)
        assert history[0]["status"] == "no_key"

    def test_main_with_mine(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        code = main([
            "--query", "codex 安装", "--platforms", "deepseek",
            "--mine", "https://a.com/1", "--db", str(db), "--output", str(out),
        ])
        assert code == 0
        history = load_history("codex 安装", db_path=db)
        assert json.loads(history[0]["mine_ids"]) == ["https://a.com/1"]
        assert history[0]["mine_cited"] is None  # no_key → mine 未知

    def test_main_with_repeated_mine_keeps_comma_in_identifier(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        code = main([
            "--query", "codex 安装", "--platforms", "deepseek",
            "--mine", "https://a.com/1,2", "--mine", "我的昵称",
            "--db", str(db), "--output", str(out),
        ])
        assert code == 0
        history = load_history("codex 安装", db_path=db)
        assert json.loads(history[0]["mine_ids"]) == ["https://a.com/1,2", "我的昵称"]

    def test_main_dedupes_mine(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        code = main([
            "--query", "codex", "--platforms", "deepseek",
            "--mine", "https://a.com/1", "--mine", "https://a.com/1",
            "--db", str(db), "--output", str(out),
        ])
        assert code == 0
        history = load_history("codex", db_path=db)
        assert json.loads(history[0]["mine_ids"]) == ["https://a.com/1"]

    def test_main_parses_mine_owned_and_competitor(self, tmp_path, monkeypatch):
        captured = {}

        def fake_probe(query, mine_ids=None, owned_ids=None, competitor_ids=None):
            captured["mine_ids"] = mine_ids
            captured["owned_ids"] = owned_ids
            captured["competitor_ids"] = competitor_ids
            return ProbeResult(query, "deepseek", "ok", True, "positive", "c", "api", False)

        monkeypatch.setitem(search_ai.PLATFORMS["deepseek"], "probe", fake_probe)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        code = main([
            "--query", "codex", "--platforms", "deepseek",
            "--mine", "https://a.com/1",
            "--mine-owned", "https://a.com/2",
            "--competitor", "https://comp.example.com",
            "--db", str(db), "--output", str(out),
        ])
        assert code == 0
        assert captured["mine_ids"] == ["https://a.com/1", "https://a.com/2"]
        assert captured["owned_ids"] == ["https://a.com/2"]
        assert captured["competitor_ids"] == ["https://comp.example.com"]

    def test_main_owned_earned_overlap_prefers_earned(self, tmp_path, monkeypatch, capsys):
        # 同一标识同时传 --mine 与 --mine-owned：按原创处理并从 owned 移除，stderr 提示
        captured = {}

        def fake_probe(query, mine_ids=None, owned_ids=None, competitor_ids=None):
            captured["owned_ids"] = owned_ids
            return ProbeResult(query, "deepseek", "ok", True, "positive", "c", "api", False)

        monkeypatch.setitem(search_ai.PLATFORMS["deepseek"], "probe", fake_probe)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        code = main([
            "--query", "codex", "--platforms", "deepseek",
            "--mine", "https://a.com/1",
            "--mine-owned", "https://a.com/1",
            "--db", str(db), "--output", str(out),
        ])
        assert code == 0
        assert captured["owned_ids"] == []
        assert "按原创（earned）处理" in capsys.readouterr().err

    def test_main_prints_competitor_replaced(self, tmp_path, monkeypatch, capsys):
        calls = {"n": 0}

        def fake_probe(query, mine_ids=None, owned_ids=None, competitor_ids=None):
            calls["n"] += 1
            return ProbeResult(
                query, "deepseek", "ok", True, "positive", "c", "api", False,
                mine_cited=calls["n"] == 1,
                mine_ids=mine_ids or [],
                competitor_matched=calls["n"] > 1,
                competitor_ids=competitor_ids or [],
            )

        monkeypatch.setitem(search_ai.PLATFORMS["deepseek"], "probe", fake_probe)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        assert main([
            "--query", "codex", "--platforms", "deepseek",
            "--mine", "https://a.com/1",
            "--competitor", "https://comp.example.com",
            "--samples", "1",
            "--db", str(db), "--output", str(out),
        ]) == 0
        assert main([
            "--query", "codex", "--platforms", "deepseek",
            "--mine", "https://a.com/1",
            "--competitor", "https://comp.example.com",
            "--samples", "1",
            "--db", str(db), "--output", str(out),
        ]) == 0
        captured = capsys.readouterr().out
        # 竞品夺走与引用变化应同条输出，不跳过 cited/flip 变化
        assert "竞品夺走" in captured
        # prev 明确未命中竞品（False）→ 已确认夺走，不带「推断」标注
        assert "（推断）" not in captured
        assert "无变化" in captured

    def test_main_prints_mine_unknown_type_when_cited_type_missing(self, tmp_path, monkeypatch, capsys):
        # mine_cited=True 但 cited_type 缺失时显示「未知」，不得误标「转载」
        def fake_probe(query, mine_ids=None, owned_ids=None, competitor_ids=None):
            return ProbeResult(
                query, "deepseek", "ok", True, "positive", "c", "api", False,
                mine_cited=True, mine_ids=mine_ids or [],
            )

        monkeypatch.setitem(search_ai.PLATFORMS["deepseek"], "probe", fake_probe)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        assert main([
            "--query", "codex", "--platforms", "deepseek",
            "--mine", "https://a.com/1", "--db", str(db), "--output", str(out),
        ]) == 0
        captured = capsys.readouterr().out
        assert "我的内容 是（未知）" in captured

    def test_main_prints_mine_for_no_key(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        code = main([
            "--query", "codex", "--platforms", "deepseek",
            "--mine", "https://a.com/1", "--db", str(db), "--output", str(out),
        ])
        assert code == 0
        captured = capsys.readouterr().out
        assert "我的内容 —" in captured

    def test_main_prints_delta_mine_only_change(self, tmp_path, monkeypatch, capsys):
        # 仅「我的内容」变化（cited/flip 均无变化）时，控制台仍应打印对比条目
        calls = {"n": 0}

        def fake_probe(query, mine_ids=None, owned_ids=None, competitor_ids=None):
            calls["n"] += 1
            return ProbeResult(
                query, "deepseek", "ok", None, None, "ctx", "api",
                degraded=False,
                mine_cited=calls["n"] > 1,
                mine_ids=mine_ids or [],
            )

        monkeypatch.setitem(search_ai.PLATFORMS["deepseek"], "probe", fake_probe)
        db = tmp_path / "monitor.db"
        out = tmp_path / "snap"
        assert main([
            "--query", "codex", "--platforms", "deepseek",
            "--mine", "https://a.com/1", "--samples", "1",
            "--db", str(db), "--output", str(out),
        ]) == 0
        assert main([
            "--query", "codex", "--platforms", "deepseek",
            "--mine", "https://a.com/1", "--samples", "1",
            "--db", str(db), "--output", str(out),
        ]) == 0
        captured = capsys.readouterr().out
        # 仅 mine 变化时不得出现「： · 」的多余分隔符
        assert "与上次对比 · DeepSeek：我的内容新增被引用" in captured
        assert "： · " not in captured

    def test_invalid_platform_rejected(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            main(["--query", "x", "--platforms", "unknown"])
        assert exc.value.code == 2

    def test_platform_defaults(self):
        assert _parse_platforms(None) == ["deepseek", "kimi", "doubao", "yuanbao"]
        assert _parse_platforms(" deepseek, Kimi ") == ["deepseek", "kimi"]
        assert _parse_platforms("deepseek,kimi,deepseek") == ["deepseek", "kimi"]


class TestMisc:
    def test_re_slug(self):
        assert re_slug("AI 搜索优化") == "AI-搜索优化"
        assert re_slug("") == "untitled"

    def test_platform_registry(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        assert set(PLATFORMS) == {"deepseek", "kimi", "doubao", "yuanbao"}
        # 注册表 probe 统一签名：所有平台都能接收 mine_ids
        result = PLATFORMS["deepseek"]["probe"]("测试品牌", mine_ids=["https://a.com/1"])
        assert result.platform == "deepseek"
        assert result.mine_ids == ["https://a.com/1"]
