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
    BrandResult,
    Dimension,
    _own_key,
    build_own_index,
    build_recommendations,
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

    def test_engagement_flat(self):
        flat = ArticleItem(title="t", url="u", content_type="Article", content_text="x",
                           vote_count=20, comment_count=0, favorite_count=0,
                           author_name="", author_badge="", updated_at=int(time.time()))
        dim = score_engagement([flat], 20)
        assert dim.score == 70
        assert "持平" in dim.detail

    def test_raw_values(self):
        assert score_share(2, 10).raw == 20.0
        assert score_share(1, 10).raw == 10.0
        assert score_coverage(3, 4).raw == 75.0
        assert score_coverage(0, 0).raw == 50.0
        assert score_presence(1).raw == 100.0


class TestRunBrand:
    def test_full_flow_with_gap(self, monkeypatch, own_item, other_item):
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, {"我的名字"}, []))

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
            lambda: (set(), set(), ["本人账号暂无创作内容"]),
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
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, {"我的名字"}, []))

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
        monkeypatch.setattr("brand.build_own_index", lambda: (set(), {"我的名字"}, []))
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


class TestBuildOwnIndex:
    def test_returns_sets(self, monkeypatch, own_item):
        monkeypatch.setattr(
            "brand.zhihu_api.get_my_contents",
            lambda **_: SimpleNamespace(items=[own_item]),
        )
        url_ids, authors, notes = build_own_index()
        assert url_ids == {"1001"}
        assert authors == {"我的名字"}
        assert notes == []

    def test_api_error_falls_back(self, monkeypatch):
        def boom(**_: object):
            raise zhihu_api.AuthError(20001, "invalid secret")

        monkeypatch.setattr("brand.zhihu_api.get_my_contents", boom)
        url_ids, authors, notes = build_own_index()
        assert url_ids == set()
        assert authors == set()
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
            "我的品牌", dims, 1, 10, [], [own_item], 20.0, has_topics=False
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
            "我的品牌", dims, 1, 10, [], [own_item], 20.0, has_topics=False
        )
        assert any(r.dimension == "份额占比" and r.priority == "P1" for r in recs)

    def test_coverage_threshold_uses_raw(self, own_item):
        dims = {
            "搜索存在率": score_presence(1),
            "份额占比": score_share(1, 10),
            "话题覆盖": Dimension("话题覆盖", 80, "4/5", raw=79.5),
            "互动基准": score_engagement([own_item], 20),
        }
        recs = build_recommendations(
            "我的品牌", dims, 1, 10, [], [own_item], 20.0, has_topics=True
        )
        assert any(r.dimension == "话题覆盖" and r.priority == "P1" for r in recs)


class TestReport:
    def test_save_report_writes_files(self, tmp_path, monkeypatch, own_item):
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, set(), []))
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


class TestMain:
    def test_requires_brand(self):
        with pytest.raises(SystemExit):
            main([])

    def test_happy_path(self, tmp_path, monkeypatch, own_item):
        monkeypatch.setattr("brand.build_own_index", lambda: ({"1001"}, set(), []))
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

    def test_auth_error_returns_1(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise zhihu_api.AuthError(20001, "invalid secret")

        monkeypatch.setattr("brand.run_brand", boom)
        assert main(["--brand", "x"]) == 1
