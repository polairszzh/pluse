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
        assert recommend._platform_of("https://zhidao.baidu.com/question/1") == "百度知道"
        assert recommend._platform_of("https://baike.baidu.com/item/x") == "百度百科"
        assert recommend._platform_of("https://news.qq.com/a/1") == "腾讯新闻"
        # 视频子域不得误判为腾讯新闻
        assert recommend._platform_of("https://v.qq.com/x/cover/1") is None

    def test_sohu_163_path_dependent(self):
        # 新闻频道（news.sohu.com / news.163.com）不误判为号
        assert recommend._platform_of("https://news.sohu.com/1.html") is None
        assert recommend._platform_of("https://www.sohu.com/a/123") == "搜狐号"
        assert recommend._platform_of("https://mp.sohu.com/profile?xpt=1") == "搜狐号"
        assert recommend._platform_of("https://news.163.com/1.html") is None
        assert recommend._platform_of("https://www.163.com/dy/article/abc.html") == "网易号"

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

    def test_baidu_zhidao_ranking(self):
        # 百度知道与文心权重表一致：不再输出未识别
        lines = recommend.recommend_url("https://zhidao.baidu.com/question/1")
        assert lines[0] == "文章所在平台：百度知道"
        assert any("文心一言 18.5%（第 2 位）" in line for line in lines)


class TestMain:
    def test_engine(self, capsys):
        assert recommend.main(["--engine", "deepseek"]) == 0
        out = capsys.readouterr().out
        assert "推荐发布平台（目标引擎：DeepSeek）" in out
        assert "CSDN（24.6%）" in out
        assert "相对值" in out

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

    def test_empty_url_no_keyerror(self, capsys):
        # --url "" 走 URL 分支（未识别），不落入 engine 分支触发 KeyError
        assert recommend.main(["--url", ""]) == 0
        out = capsys.readouterr().out
        assert "文章所在平台：未识别" in out
