"""B6 平台信源推荐测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import recommend


class TestPlatformOf:
    def test_known_hosts(self):
        assert recommend._platform_of("https://zhuanlan.zhihu.com/p/1") == "知乎"
        assert recommend._platform_of("https://blog.csdn.net/x/article/1") == "CSDN"
        assert recommend._platform_of("https://mp.weixin.qq.com/s/abc") == "公众号"
        assert recommend._platform_of("https://www.toutiao.com/article/1") == "今日头条"

    def test_unknown_host(self):
        assert recommend._platform_of("https://example.com/p/1") is None


class TestRecommendEngine:
    def test_deepseek_ranking(self):
        lines = recommend.recommend_engine("deepseek")
        assert lines[0] == "推荐发布平台（目标引擎：DeepSeek）："
        assert lines[1] == "  1. CSDN（24.6%）"
        assert lines[2] == "  2. 知乎（19.8%）"
        assert any("内容策略" in line for line in lines)
        assert any("16800" in line for line in lines)

    def test_yuanbao_marks_calibration_pending(self):
        lines = recommend.recommend_engine("yuanbao")
        assert any("待真实数据校准" in line for line in lines)


class TestRecommendUrl:
    def test_zhihu_article_ranking(self):
        lines = recommend.recommend_url("https://zhuanlan.zhihu.com/p/2068807215738369970")
        assert lines[0] == "文章所在平台：知乎"
        assert any("DeepSeek 19.8%（第 2 位）" in line for line in lines)
        assert any("豆包 21.8%（第 2 位）" in line for line in lines)

    def test_unknown_url(self):
        lines = recommend.recommend_url("https://example.com/p/1")
        assert lines[0] == "文章所在平台：未识别"
        assert any("未识别" in line for line in lines)


class TestMain:
    def test_engine(self, capsys):
        assert recommend.main(["--engine", "deepseek"]) == 0
        out = capsys.readouterr().out
        assert "推荐发布平台（目标引擎：DeepSeek）" in out
        assert "CSDN（24.6%）" in out

    def test_url(self, capsys):
        assert recommend.main(["--url", "https://zhuanlan.zhihu.com/p/1"]) == 0
        out = capsys.readouterr().out
        assert "文章所在平台：知乎" in out
        assert "DeepSeek 19.8%（第 2 位）" in out

    def test_all(self, capsys):
        assert recommend.main(["--engine", "all"]) == 0
        out = capsys.readouterr().out
        assert "== DeepSeek ==" in out
        assert "== 元宝 ==" in out

    def test_missing_required(self):
        import pytest

        with pytest.raises(SystemExit) as exc:
            recommend.main([])
        assert exc.value.code == 2
