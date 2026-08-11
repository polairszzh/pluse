"""知乎全文抓取模块测试（不启动真实浏览器）"""

import fetch_zhihu_full as fzf


class TestIsZhihuUrl:
    def test_zhihu_hosts(self):
        assert fzf._is_zhihu_url("https://zhuanlan.zhihu.com/p/123") is True
        assert fzf._is_zhihu_url("https://www.zhihu.com/question/1") is True

    def test_non_zhihu(self):
        assert fzf._is_zhihu_url("https://example.com/p/1") is False
        assert fzf._is_zhihu_url("not-a-url") is False


class TestFetchFullContent:
    def test_non_zhihu_url_rejected_without_playwright(self):
        # 非知乎 URL 直接返回错误，不依赖 Playwright 安装
        result = fzf.fetch_full_content("https://example.com/p/1")
        assert "error" in result
        assert "仅支持知乎链接" in result["error"]
