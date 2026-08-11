"""知乎全文抓取模块测试（不启动真实浏览器）"""

import fetch_zhihu_full as fzf


class TestIsZhihuUrl:
    def test_zhihu_hosts(self):
        assert fzf._is_zhihu_url("https://zhuanlan.zhihu.com/p/123") is True
        assert fzf._is_zhihu_url("https://www.zhihu.com/question/1") is True

    def test_non_zhihu(self):
        assert fzf._is_zhihu_url("https://example.com/p/1") is False
        assert fzf._is_zhihu_url("not-a-url") is False


class TestIsArticleUrl:
    def test_article_and_answer(self):
        assert fzf._is_article_url("https://zhuanlan.zhihu.com/p/123") is True
        assert fzf._is_article_url("https://www.zhihu.com/answer/123") is True

    def test_non_article_rejected(self):
        assert fzf._is_article_url("https://www.zhihu.com/question/1") is False
        assert fzf._is_article_url("https://www.zhihu.com/zvideo/1") is False
        assert fzf._is_article_url("https://www.zhihu.com/pin/1") is False


class TestFetchFullContent:
    def test_non_zhihu_url_rejected_without_playwright(self):
        # 非知乎 URL 直接返回错误，不依赖 Playwright 安装
        result = fzf.fetch_full_content("https://example.com/p/1")
        assert "error" in result
        assert "仅支持知乎链接" in result["error"]

    def test_question_url_rejected_without_playwright(self):
        # 问题页含多个回答，非单篇内容，直接拒绝（不启动 Playwright）
        result = fzf.fetch_full_content("https://www.zhihu.com/question/123")
        assert "error" in result
        assert "文章/回答" in result["error"]


class TestExtractContent:
    def test_content_selectors_include_answer_anchor(self):
        selectors = fzf._content_selectors("https://www.zhihu.com/answer/123")
        assert selectors[0] == "#answer-123 .RichText"
        assert fzf._content_selectors("https://zhuanlan.zhihu.com/p/1") == list(
            fzf._CONTENT_SELECTORS
        )

    def test_answer_url_prefers_target_anchor(self):
        calls = []

        class FakePage:
            def eval_on_selector(self, selector, _expr):
                calls.append(selector)
                if selector.startswith("#answer-123"):
                    return "目标回答正文。" * 20
                return ""

        assert fzf._extract_content(
            FakePage(), url="https://www.zhihu.com/answer/123"
        ) == "目标回答正文。" * 20
        assert calls[0] == "#answer-123 .RichText"

    def test_prefers_specific_selector_with_content(self):
        class FakePage:
            def eval_on_selector(self, selector, _expr):
                if selector == ".Post-RichText":
                    return ""
                if selector == ".RichText":
                    return "正文内容足够长。" * 20
                raise RuntimeError("not found")

        assert fzf._extract_content(FakePage()) == "正文内容足够长。" * 20

    def test_short_or_empty_skipped(self):
        class FakePage:
            def eval_on_selector(self, selector, _expr):
                if selector == ".Post-RichText":
                    return "太短"
                raise RuntimeError("not found")

        assert fzf._extract_content(FakePage()) is None

    def test_all_selectors_fail_returns_none(self):
        class FakePage:
            def eval_on_selector(self, selector, _expr):
                raise RuntimeError("not found")

        assert fzf._extract_content(FakePage()) is None
