"""zhihu_api.py 单元测试"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from zhihu_api import (
    ArticleItem,
    AuthError,
    QuotaExceeded,
    ZhihuAPIError,
    _parse_article,
    extract_article_id,
    get_my_contents,
    get_my_followees,
    search,
    topic_benchmark,
)

# ── 环境检查 ────────────────────────────────────────────

_needs_env = pytest.mark.skipif(
    not (Path(__file__).parent.parent / ".env").exists(),
    reason="需要 .env 文件（含 ZHIHU_ACCESS_SECRET）",
)


# ── 纯函数测试（不需要 API） ─────────────────────────────

class TestExtractArticleId:
    def test_zhuanlan_url(self):
        assert extract_article_id("https://zhuanlan.zhihu.com/p/1992754233318077903") == "1992754233318077903"

    def test_answer_url(self):
        assert extract_article_id("https://www.zhihu.com/answer/123456789") == "123456789"

    def test_question_url(self):
        assert extract_article_id("https://www.zhihu.com/question/12345/answer/67890") == "67890"

    def test_trailing_slash(self):
        assert extract_article_id("https://zhuanlan.zhihu.com/p/abc123/") == "abc123"

    def test_no_path(self):
        assert extract_article_id("https://www.zhihu.com") is None


class TestParseArticle:
    def test_full_article(self):
        raw = {
            "Title": "测试文章",
            "Url": "https://zhuanlan.zhihu.com/p/123",
            "ContentType": "Article",
            "ContentText": "这是一篇测试文章的内容摘要",
            "VoteUpCount": 42,
            "CommentCount": 10,
            "FavoriteCount": 15,
            "AuthorName": "测试作者",
            "AuthorAvatar": "https://pic.zhimg.com/avatar.jpg",
            "AuthorBadgeText": "优秀答主",
            "RankingScore": 2.1,
            "EditTime": 1710000000,
        }
        item = _parse_article(raw)
        assert item.title == "测试文章"
        assert item.vote_count == 42
        assert item.comment_count == 10
        assert item.favorite_count == 15
        assert item.ranking_score == 2.1
        assert item.content_type == "Article"
        assert item.author_name == "测试作者"

    def test_answer_uses_like_count(self):
        raw = {
            "Title": "回答",
            "ContentType": "Answer",
            "LikeCount": 99,
            "CommentCount": 5,
        }
        item = _parse_article(raw)
        assert item.vote_count == 99

    def test_missing_fields_default(self):
        item = _parse_article({})
        assert item.title == ""
        assert item.vote_count == 0
        assert item.ranking_score == 0.0

    def test_null_content_text(self):
        item = _parse_article({"Title": "X", "ContentText": None})
        assert item.content_text == ""


# ── API 测试（需要 .env） ────────────────────────────────

@_needs_env
class TestSearchAPI:
    def test_basic_search(self):
        result = search("AI", count=3)
        assert len(result.items) <= 3
        assert len(result.items) > 0
        assert result.search_hash_id
        for item in result.items:
            assert isinstance(item, ArticleItem)
            assert item.title
            assert item.url
            assert item.content_text

    def test_empty_query_raises(self):
        with pytest.raises(ValueError, match="query 不能为空"):
            search("")
        with pytest.raises(ValueError, match="query 不能为空"):
            search("   ")

    def test_count_clamped(self):
        result = search("测试", count=100)
        assert len(result.items) <= 10


@_needs_env
class TestUserContentsAPI:
    def test_get_contents(self):
        result = get_my_contents(content_type="all", limit=5)
        assert hasattr(result, 'paging')
        assert hasattr(result.paging, 'totals')
        for item in result.items:
            assert isinstance(item, ArticleItem)

    def test_invalid_content_type(self):
        with pytest.raises(ValueError, match="content_type"):
            get_my_contents(content_type="invalid")


@_needs_env
class TestUserFolloweesAPI:
    def test_get_followees(self):
        result = get_my_followees(limit=5)
        assert hasattr(result, 'paging')
        assert hasattr(result.paging, 'totals')


@_needs_env
class TestTopicBenchmark:
    def test_returns_stats(self):
        result = topic_benchmark("AI搜索优化", count=5)
        assert result["query"] == "AI搜索优化"
        assert "avg_ranking_score" in result
        assert "max_ranking_score" in result
        assert "top3_urls" in result
        assert result["count"] > 0

    def test_any_query_returns_valid_scores(self):
        result = topic_benchmark("zzzzzzz_not_a_real_query_99999")
        assert "avg_ranking_score" in result


class TestErrorClasses:
    def test_quota_exceeded_is_api_error(self):
        err = QuotaExceeded(30001, "频率限制")
        assert isinstance(err, ZhihuAPIError)
        assert err.code == 30001

    def test_auth_error_is_api_error(self):
        err = AuthError(20001, "鉴权失败")
        assert isinstance(err, ZhihuAPIError)
        assert err.code == 20001
