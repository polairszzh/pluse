"""brand.py 单元测试 — mock 知乎 API，不触网"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import zhihu_api
from audit import Recommendation
from brand import (
    DIMENSION_WEIGHTS,
    BrandResult,
    Dimension,
    _fmt_pct,
    _md_cell,
    _own_key,
    _round_half_up,
    build_own_index,
    build_recommendations,
    combine,
    is_competitor,
    is_own,
    main,
    re_slug,
    render_markdown,
    run_brand,
    save_report,
    score_coverage,
    score_engagement,
    score_presence,
    score_share,
)
from zhihu_api import ArticleItem


@pytest.fixture
def own_item():
    return ArticleItem(
        title="我的品牌实战总结",
        url="https://zhuanlan.zhihu.com/p/1001",
        content_type="Article",
        content_text="x",
        vote_count=50,
        comment_count=5,
        favorite_count=3,
        author_name="我的名字",
        author_badge="",
        updated_at=int(time.time()),
    )


@pytest.fixture
def other_item():
    return ArticleItem(
        title="竞品分析",
        url="https://zhuanlan.zhihu.com/p/2002",
        content_type="Article",
        content_text="x",
        vote_count=80,
        comment_count=10,
        favorite_count=5,
        author_name="Kimi官方",
        author_badge="",
        updated_at=int(time.time()),
    )


class TestSlug:
    def test_basic(self):
        assert re_slug("DeepSeek AI") == "DeepSeek-AI"

    def test_empty(self):
        assert re_slug("") == "untitled"


class TestIdentify:
    def test_url_match_first(self, own_item):
        assert is_own(own_item, {"1001"}, set())

    def test_author_fallback(self, own_item):
        assert is_own(own_item, set(), {"我的名字"})

    def test_no_match(self, own_item, other_item):
        assert not is_own(other_item, {"1001"}, {"我的名字"})

    def test_competitor_author(self, other_item):
        assert is_competitor(other_item, ["Kimi", "豆包"]) == "Kimi"

    def test_competitor_case_insensitive(self, other_item):
        other_item.author_name = "kimi官方"
        assert is_competitor(other_item, ["Kimi"]) == "Kimi"

    def test_no_competitor(self, own_item):
        assert is_competitor(own_item, ["Kimi"]) is None

    def test_competitor_short_word_boundary(self):
        def item(author):
            return ArticleItem(
                title="t", url="u", content_type="Article", content_text="x",
                vote_count=0, comment_count=0, favorite_count=0,
                author_name=author, author_badge="", updated_at=int(time.time()),
            )

        assert is_competitor(item("OpenAI 官方"), ["AI"]) is None
        assert is_competitor(item("AI 搜索助手"), ["AI"]) == "AI"
        assert is_competitor(item("新智元团队"), ["新智元"]) == "新智元"

    def test_is_own_without_url(self, own_item):
        own_item.url = None
        assert is_own(own_item, set(), {"我的名字"})
        assert not is_own(own_item, {"1001"}, set())

    def test_own_key_without_url(self, own_item):
        own_item.url = None
        assert _own_key(own_item) == "我的名字|我的品牌实战总结"


class TestScores:
    def test_presence_bands(self):
        assert score_presence(None).score == 0
        assert score_presence(1).score == 100
        assert score_presence(2).score == 85
        assert score_presence(4).score == 70
        assert score_presence(8).score == 55

    def test_share(self):
        assert score_share(2, 10).score == 20
        assert score_share(0, 10).score == 0
        assert score_share(0, 0).score == 0

    def test_coverage(self):
        assert score_coverage(2, 4).score == 50
        assert score_coverage(0, 3).score == 0
        assert score_coverage(0, 0).score == 50

    def test_engagement(self):
        assert score_engagement([], 10).score == 10
        rich = ArticleItem(title="t", url="u", content_type="Article", content_text="x",
                           vote_count=100, comment_count=0, favorite_count=0,
                           author_name="", author_badge="", updated_at=int(time.time()))
        assert score_engagement([rich], 10).score == 90
        assert score_engagement([rich], 10).raw == 1000.0

    def test_engagement_flat(self):
        flat = ArticleItem(title="t", url="u", content_type="Article", content_text="x",
                           vote_count=20, comment_count=0, favorite_count=0,
                           author_name="", author_badge="", updated_at=int(time.time()))
        dim = score_engagement([flat], 20)
        assert dim.score == 70
        assert "持平" in dim.detail
        assert dim.raw == 100.0
        assert score_engagement([], 20).raw == 0.0

    def test_engagement_tolerance(self):
        item = ArticleItem(title="t", url="u", content_type="Article", content_text="x",
                           vote_count=10, comment_count=0, favorite_count=0,
                           author_name="", author_badge="", updated_at=int(time.time()))
        dim = score_engagement([item], 10.000000001)
        assert dim.score == 70
        assert "持平" in dim.detail

    def test_raw_values(self):
        assert score_share(2, 10).raw == 20.0
        assert score_share(1, 10).raw == 10.0
        assert score_coverage(3, 4).raw == 75.0
        assert score_coverage(0, 0).raw == 50.0
        assert score_presence(1).raw == 100.0

    def test_fmt_pct(self):
        assert _fmt_pct(20.0) == "20"
        assert _fmt_pct(19.5) == "19.5"
        assert _fmt_pct(0.0) == "0"
        assert _md_cell("A|B\nC") == "A\\|B C"

    def test_round_half_up(self):
        assert _round_half_up(2.5) == 3
        assert _round_half_up(3.5) == 4
        assert _round_half_up(19.5) == 20
        assert score_coverage(1, 40).score == 3

    def test_combine_uses_single_weight_table(self):
        assert abs(sum(DIMENSION_WEIGHTS.values()) - 1.0) < 1e-9
        dims = {
            "搜索存在率": score_presence(1),
            "份额占比": score_share(1, 10),
            "话题覆盖": score_coverage(1, 1),
            "互动基准": score_engagement([], 20),
        }
        overall, _ = combine(dims)
        expected = int(100 * 0.30 + 10 * 0.20 + 100 * 0.30 + 10 * 0.20)
        assert overall == expected

    def test_combine_rounds_half_up(self):
        low = ArticleItem(title="t", url="u", content_type="Article", content_text="x",
                          vote_count=0, comment_count=0, favorite_count=0,
                          author_name="", author_badge="", updated_at=int(time.time()))
        dims = {
            "搜索存在率": score_presence(2),
            "份额占比": score_share(5, 10),
            "话题覆盖": score_coverage(2, 3),
            "互动基准": score_engagement([low], 20),
        }
        overall, _ = combine(dims)
        assert overall == 62


class TestRunBrand:
    def test_full_flow_with_gap(self, monkeypatch, own_item, other_item):
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, {"我的名字"}, True, []))

        def fake_search(query, count=10):
            if query == "我的品牌":
                return SimpleNamespace(items=[own_item, other_item])
            return SimpleNamespace(items=[other_item])

        monkeypatch.setattr("brand.zhihu_api.search", fake_search)
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 20.0},
        )
        result = run_brand("我的品牌", topics=["AI工具"], competitors=["Kimi"])
        assert result.dimensions["搜索存在率"].score == 100
        assert result.dimensions["份额占比"].score == 50
        assert result.dimensions["话题覆盖"].score == 0
        assert result.topic_coverage[0]["competitors"] == {"Kimi": 1}
        assert any("AI工具" in r.action for r in result.recommendations)
        assert result.brand_search[0]["mine"] is True

    def test_no_own_content(self, monkeypatch, other_item):
        monkeypatch.setattr(
            "brand.build_own_index",
            lambda: (set(), set(), True, ["本人账号暂无创作内容"]),
        )
        monkeypatch.setattr(
            "brand.zhihu_api.search",
            lambda q, count=10: SimpleNamespace(items=[other_item]),
        )
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 10.0},
        )
        result = run_brand("我的品牌", topics=["AI工具"], competitors=["Kimi"])
        assert result.dimensions["搜索存在率"].score == 0
        assert any(r.priority == "P0" for r in result.recommendations)
        assert result.notes

    def test_own_deduplicated_across_searches(self, monkeypatch):
        own_a = ArticleItem(
            title="重复文章", url="https://zhuanlan.zhihu.com/p/1001",
            content_type="Article", content_text="x", vote_count=50,
            comment_count=5, favorite_count=3, author_name="我的名字",
            author_badge="", updated_at=int(time.time()),
        )
        own_b = ArticleItem(
            title="重复文章", url="https://zhuanlan.zhihu.com/p/1001",
            content_type="Article", content_text="x", vote_count=80,
            comment_count=8, favorite_count=4, author_name="我的名字",
            author_badge="", updated_at=int(time.time()),
        )
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, {"我的名字"}, True, []))

        def fake_search(query, count=10):
            if query == "我的品牌":
                return SimpleNamespace(items=[own_a])
            return SimpleNamespace(items=[own_b])

        monkeypatch.setattr("brand.zhihu_api.search", fake_search)
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 20.0},
        )
        result = run_brand("我的品牌", topics=["AI工具"], competitors=[])
        detail = result.dimensions["互动基准"].detail
        assert "平均赞同（50）" in detail
        assert "平均赞同（65）" not in detail

    def test_run_brand_with_missing_url(self, monkeypatch):
        item = ArticleItem(
            title="无链接文章", url=None, content_type="Article", content_text="x",
            vote_count=5, comment_count=0, favorite_count=0, author_name="我的名字",
            author_badge="", updated_at=int(time.time()),
        )
        monkeypatch.setattr("brand.build_own_index", lambda: (set(), {"我的名字"}, True, []))
        monkeypatch.setattr(
            "brand.zhihu_api.search",
            lambda q, count=10: SimpleNamespace(items=[item]),
        )
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 10.0},
        )
        result = run_brand("我的品牌")
        assert result.dimensions["搜索存在率"].score == 100

    def test_engagement_uses_brand_results_only(self, monkeypatch):
        no_url = ArticleItem(
            title="同一篇", url=None, content_type="Article", content_text="x",
            vote_count=50, comment_count=0, favorite_count=0, author_name="我的名字",
            author_badge="", updated_at=int(time.time()),
        )
        with_url = ArticleItem(
            title="同一篇", url="https://zhuanlan.zhihu.com/p/1001",
            content_type="Article", content_text="x", vote_count=80,
            comment_count=0, favorite_count=0, author_name="我的名字",
            author_badge="", updated_at=int(time.time()),
        )
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, {"我的名字"}, True, []))

        def fake_search(query, count=10):
            if query == "我的品牌":
                return SimpleNamespace(items=[no_url])
            return SimpleNamespace(items=[with_url])

        monkeypatch.setattr("brand.zhihu_api.search", fake_search)
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 20.0},
        )
        result = run_brand("我的品牌", topics=["AI工具"], competitors=[])
        detail = result.dimensions["互动基准"].detail
        assert "平均赞同（50）" in detail
        assert "平均赞同（80）" not in detail

    def test_topic_search_failure_degraded(self, monkeypatch, other_item):
        monkeypatch.setattr("brand.build_own_index", lambda: (set(), set(), True, []))

        def fake_search(query, count=10):
            if query == "我的品牌":
                return SimpleNamespace(items=[other_item])
            if query == "坏话题":
                raise zhihu_api.QuotaExceeded(30001, "rate limited")
            return SimpleNamespace(items=[other_item])

        monkeypatch.setattr("brand.zhihu_api.search", fake_search)
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 10.0},
        )
        result = run_brand("我的品牌", topics=["坏话题", "好话题"], competitors=["Kimi"])
        assert result.dimensions["话题覆盖"].score == 0
        assert result.topic_coverage[0]["error"]
        assert any("坏话题" in n for n in result.notes)
        assert any("好话题" in r.action for r in result.recommendations)

    def test_all_topics_failed_no_misleading_coverage(self, monkeypatch, other_item):
        monkeypatch.setattr("brand.build_own_index", lambda: (set(), set(), True, []))

        def fake_search(query, count=10):
            if query == "我的品牌":
                return SimpleNamespace(items=[other_item])
            raise zhihu_api.QuotaExceeded(30001, "rate limited")

        monkeypatch.setattr("brand.zhihu_api.search", fake_search)
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 10.0},
        )
        result = run_brand("我的品牌", topics=["坏1", "坏2"], competitors=["Kimi"])
        cov = result.dimensions["话题覆盖"]
        assert cov.score == 50
        assert "全部搜索失败" in cov.detail
        assert not any(r.dimension == "话题覆盖" and r.priority == "P1" for r in result.recommendations)
        assert any("全部搜索失败" in r.action for r in result.recommendations)

    def test_share_deduplicated_in_brand_results(self, monkeypatch):
        own_a = ArticleItem(
            title="重复文章", url="https://zhuanlan.zhihu.com/p/1001",
            content_type="Article", content_text="x", vote_count=50,
            comment_count=0, favorite_count=0, author_name="我的名字",
            author_badge="", updated_at=int(time.time()),
        )
        own_b = ArticleItem(
            title="重复文章", url="https://zhuanlan.zhihu.com/p/1001",
            content_type="Article", content_text="x", vote_count=80,
            comment_count=0, favorite_count=0, author_name="我的名字",
            author_badge="", updated_at=int(time.time()),
        )
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, {"我的名字"}, True, []))
        monkeypatch.setattr(
            "brand.zhihu_api.search",
            lambda q, count=10: SimpleNamespace(items=[own_a, own_b]),
        )
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 10.0},
        )
        result = run_brand("我的品牌")
        assert result.dimensions["份额占比"].score == 50
        assert "1 条" in result.dimensions["份额占比"].detail

    def test_share_dedupe_url_and_no_url_forms(self, monkeypatch):
        no_url = ArticleItem(
            title="同一篇", url=None, content_type="Article", content_text="x",
            vote_count=50, comment_count=0, favorite_count=0, author_name="我的名字",
            author_badge="", updated_at=int(time.time()),
        )
        with_url = ArticleItem(
            title="同一篇", url="https://zhuanlan.zhihu.com/p/1001",
            content_type="Article", content_text="x", vote_count=80,
            comment_count=0, favorite_count=0, author_name="我的名字",
            author_badge="", updated_at=int(time.time()),
        )
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, {"我的名字"}, True, []))
        monkeypatch.setattr(
            "brand.zhihu_api.search",
            lambda q, count=10: SimpleNamespace(items=[no_url, with_url]),
        )
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 10.0},
        )
        result = run_brand("我的品牌")
        assert result.dimensions["份额占比"].score == 50
        assert "1 条" in result.dimensions["份额占比"].detail
        assert "平均赞同（80）" in result.dimensions["互动基准"].detail

    def test_brand_search_failure_degraded(self, monkeypatch, other_item):
        monkeypatch.setattr("brand.build_own_index", lambda: (set(), set(), True, []))

        def fake_search(query, count=10):
            if query == "我的品牌":
                raise zhihu_api.QuotaExceeded(30001, "rate limited")
            return SimpleNamespace(items=[other_item])

        monkeypatch.setattr("brand.zhihu_api.search", fake_search)
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 10.0},
        )
        result = run_brand("我的品牌", topics=["好话题"], competitors=["Kimi"])
        assert result.brand_search_error
        assert result.dimensions["搜索存在率"].score == 50
        assert "无法判断" in result.dimensions["搜索存在率"].detail
        assert not any(r.dimension == "搜索存在率" for r in result.recommendations)
        assert any("好话题" in r.action for r in result.recommendations)

    def test_own_index_failure_degraded(self, monkeypatch, other_item):
        monkeypatch.setattr(
            "brand.build_own_index",
            lambda: (set(), set(), False, ["本人内容拉取失败，「自己」识别不可用：boom"]),
        )
        monkeypatch.setattr(
            "brand.zhihu_api.search",
            lambda q, count=10: SimpleNamespace(items=[other_item]),
        )
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 10.0},
        )
        result = run_brand("我的品牌")
        assert result.dimensions["搜索存在率"].score == 50
        assert "识别不可用" in result.dimensions["搜索存在率"].detail
        assert not any(r.dimension == "搜索存在率" for r in result.recommendations)


class TestBuildOwnIndex:
    def test_returns_sets(self, monkeypatch, own_item):
        monkeypatch.setattr(
            "brand.zhihu_api.get_my_contents",
            lambda **_: SimpleNamespace(items=[own_item]),
        )
        url_ids, authors, ok, notes = build_own_index()
        assert url_ids == {"1001"}
        assert authors == {"我的名字"}
        assert ok is True
        assert notes == []

    def test_api_error_falls_back(self, monkeypatch):
        def boom(**_: object):
            raise zhihu_api.AuthError(20001, "invalid secret")

        monkeypatch.setattr("brand.zhihu_api.get_my_contents", boom)
        url_ids, authors, ok, notes = build_own_index()
        assert url_ids == set()
        assert authors == set()
        assert ok is False
        assert any("拉取失败" in n for n in notes)


class TestRecommendations:
    def test_no_topics_hint(self, own_item):
        dims = {
            "搜索存在率": score_presence(1),
            "份额占比": score_share(1, 10),
            "话题覆盖": score_coverage(0, 0),
            "互动基准": score_engagement([own_item], 20),
        }
        recs = build_recommendations(
            "我的品牌", dims, 1, 10, [], [own_item], 20.0,
            topics_requested=False, coverage_analyzed=False,
        )
        assert any("--topics" in r.action for r in recs)

    def test_recommendation_fields_match_audit(self):
        fields = set(Recommendation.__dataclass_fields__)
        assert {"priority", "dimension", "action", "expected_impact", "falsifiability_check"} <= fields

    def test_share_threshold_uses_raw(self, own_item):
        dims = {
            "搜索存在率": score_presence(1),
            "份额占比": Dimension("份额占比", 20, "20%", raw=19.5),
            "话题覆盖": score_coverage(0, 0),
            "互动基准": score_engagement([own_item], 20),
        }
        recs = build_recommendations(
            "我的品牌", dims, 1, 10, [], [own_item], 20.0,
            topics_requested=False, coverage_analyzed=False,
        )
        share_recs = [r for r in recs if r.dimension == "份额占比" and r.priority == "P1"]
        assert share_recs
        assert "19.5%" in share_recs[0].action

    def test_coverage_threshold_uses_raw(self, own_item):
        dims = {
            "搜索存在率": score_presence(1),
            "份额占比": score_share(1, 10),
            "话题覆盖": Dimension("话题覆盖", 80, "4/5", raw=79.5),
            "互动基准": score_engagement([own_item], 20),
        }
        recs = build_recommendations(
            "我的品牌", dims, 1, 10, [], [own_item], 20.0,
            topics_requested=True, coverage_analyzed=True,
        )
        coverage_recs = [r for r in recs if r.dimension == "话题覆盖" and r.priority == "P1"]
        assert coverage_recs
        assert "79.5%" in coverage_recs[0].action

    def test_all_topics_failed_hint(self, own_item):
        dims = {
            "搜索存在率": score_presence(1),
            "份额占比": score_share(1, 10),
            "话题覆盖": score_coverage(0, 0, "指定的话题全部搜索失败，覆盖维度按中性处理"),
            "互动基准": score_engagement([own_item], 20),
        }
        recs = build_recommendations(
            "我的品牌", dims, 1, 10, [], [own_item], 20.0,
            topics_requested=True, coverage_analyzed=False,
        )
        assert not any(r.dimension == "话题覆盖" and r.priority == "P1" for r in recs)
        assert any("全部搜索失败" in r.action for r in recs)


class TestReport:
    def test_save_report_writes_files(self, tmp_path, monkeypatch, own_item):
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, set(), True, []))
        monkeypatch.setattr(
            "brand.zhihu_api.search",
            lambda q, count=10: SimpleNamespace(items=[own_item]),
        )
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 20.0},
        )
        result = run_brand("我的品牌", topics=["AI工具"], competitors=["Kimi"])
        paths = save_report(result, ["Kimi"], ["AI工具"], tmp_path)
        assert len(paths) == 2
        assert all(p.exists() for p in paths)
        data = json.loads(paths[1].read_text(encoding="utf-8"))
        assert data["brand"] == "我的品牌"
        assert data["overall"] == result.overall

    def test_markdown_no_topics_has_blank_line(self):
        dims = {
            "搜索存在率": score_presence(1),
            "份额占比": score_share(1, 10),
            "话题覆盖": score_coverage(0, 0),
            "互动基准": score_engagement([], 20),
        }
        result = BrandResult(
            brand="x", overall=60, grade="C", dimensions=dims,
            brand_search=[], topic_coverage=[], recommendations=[],
        )
        md = render_markdown(result, [], [])
        assert "未指定话题（加 --topics 做覆盖分析）。\n\n## 行动清单" in md

    def test_markdown_escapes_pipe(self):
        dims = {
            "搜索存在率": score_presence(1),
            "份额占比": score_share(1, 10),
            "话题覆盖": score_coverage(0, 0),
            "互动基准": score_engagement([], 20),
        }
        result = BrandResult(
            brand="x", overall=60, grade="C", dimensions=dims,
            brand_search=[{
                "rank": 1, "title": "A|B", "url": "u",
                "author": "作者|名", "mine": True, "competitor": "Kimi|X",
                "votes": 1, "ranking_score": 1.0,
            }],
            topic_coverage=[], recommendations=[],
        )
        md = render_markdown(result, [], [])
        assert "A\\|B" in md
        assert "作者\\|名" in md
        assert "Kimi\\|X" in md

    def test_markdown_normalizes_topic_and_rec(self):
        dims = {
            "搜索存在率": score_presence(1),
            "份额占比": score_share(1, 10),
            "话题覆盖": score_coverage(1, 1),
            "互动基准": score_engagement([], 20),
        }
        result = BrandResult(
            brand="x", overall=60, grade="C", dimensions=dims,
            brand_search=[],
            topic_coverage=[{"topic": "A|B\nC", "own_count": 1, "competitors": {}, "avg_votes": 5}],
            recommendations=[Recommendation(
                priority="P0", dimension="话题覆盖",
                action="写一篇关于 A|B\nC 的内容", expected_impact="+10",
                falsifiability_check="重跑验证",
            )],
        )
        md = render_markdown(result, [], [])
        assert "### A\\|B C" in md
        assert "写一篇关于 A\\|B C 的内容" in md


class TestMain:
    def test_requires_brand(self):
        with pytest.raises(SystemExit):
            main([])

    def test_happy_path(self, tmp_path, monkeypatch, own_item):
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, set(), True, []))
        monkeypatch.setattr(
            "brand.zhihu_api.search",
            lambda q, count=10: SimpleNamespace(items=[own_item]),
        )
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 20.0},
        )
        code = main(["--brand", "我的品牌", "--output", str(tmp_path)])
        assert code == 0
        assert list(tmp_path.glob("brand-*.md"))

    def test_save_oserror_reports_local_write(self, monkeypatch, own_item, capsys):
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, {"我的名字"}, True, []))
        monkeypatch.setattr(
            "brand.zhihu_api.search",
            lambda q, count=10: SimpleNamespace(items=[own_item]),
        )
        monkeypatch.setattr(
            "brand.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 10.0},
        )

        def boom(*_args, **_kwargs):
            raise OSError("磁盘空间不足")

        monkeypatch.setattr("brand.save_report", boom)
        code = main(["--brand", "我的品牌"])
        err = capsys.readouterr().err
        assert code == 1
        assert "本地读写失败" in err
