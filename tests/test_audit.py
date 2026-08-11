"""audit.py 单元测试 — mock 知乎 API，不触网"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import zhihu_api
from audit import (
    CITABILITY_SUB_RULES,
    QUALITY_SUB_RULES,
    audit_one,
    main,
    make_slug,
    render_json,
    render_markdown,
    resolve_article,
    save_report,
)
from zhihu_api import ArticleItem


@pytest.fixture
def item():
    """一篇互动不错、内容充实的示例文章"""
    return ArticleItem(
        title="如何做好 AI 搜索优化？",
        url="https://zhuanlan.zhihu.com/p/123456",
        content_type="Article",
        content_text=(
            "AI 搜索优化的关键在于让内容被大语言模型引用。"
            "根据 2026 年的一线实测，DeepSeek 与 Kimi 的引用频率"
            "与内容结构和数据密度直接相关，参考官方文档与公开数据。" * 20
        ),
        vote_count=80,
        comment_count=12,
        favorite_count=30,
        author_name="测试作者",
        author_badge="优秀答主",
        updated_at=int(time.time()),
    )


class TestMakeSlug:
    def test_chinese_title(self):
        assert make_slug("如何做好 AI 搜索优化？") == "如何做好-AI-搜索优化"

    def test_empty_title(self):
        assert make_slug("") == "untitled"

    def test_truncated(self):
        assert len(make_slug("长" * 100)) <= 40


class TestResolveArticle:
    def test_matches_own_contents_first(self, item, monkeypatch):
        monkeypatch.setattr(
            "audit.zhihu_api.get_my_contents",
            lambda **_: SimpleNamespace(items=[item]),
        )

        def fail(*_args, **_kwargs):
            raise AssertionError("不应走搜索")

        monkeypatch.setattr("audit.zhihu_api.find_article_by_url", fail)
        found = resolve_article(item.url)
        assert found is item

    def test_falls_back_to_search(self, item, monkeypatch):
        monkeypatch.setattr(
            "audit.zhihu_api.get_my_contents",
            lambda **_: SimpleNamespace(items=[]),
        )
        monkeypatch.setattr(
            "audit.zhihu_api.find_article_by_url",
            lambda q, u: item if q == "AI搜索" and u == item.url else None,
        )
        assert resolve_article(item.url, "AI搜索") is item

    def test_no_query_returns_none(self, item, monkeypatch):
        monkeypatch.setattr(
            "audit.zhihu_api.get_my_contents",
            lambda **_: SimpleNamespace(items=[]),
        )
        assert resolve_article(item.url) is None


class TestAuditOne:
    def test_uses_topic_benchmark(self, item, monkeypatch):
        monkeypatch.setattr(
            "audit.zhihu_api.topic_benchmark",
            lambda q, count=10: {"query": q, "count": 10, "avg_votes": 40.0},
        )
        scores, benchmark, recs = audit_one(item, "AI搜索优化")
        assert benchmark["avg_votes"] == 40.0
        assert 0 <= scores.overall <= 100
        assert scores.grade
        assert isinstance(recs, list)

    def test_without_query_skips_benchmark(self, item, monkeypatch):
        def fail(*_args, **_kwargs):
            raise AssertionError("没有 query 时不应拉基准")

        monkeypatch.setattr("audit.zhihu_api.topic_benchmark", fail)
        scores, benchmark, _ = audit_one(item)
        assert benchmark == {}
        assert 0 <= scores.overall <= 100


class TestBuildRecommendations:
    def test_missing_keyword_gives_p0(self, item):
        _, _, recs = audit_one(item, keywords=["AI搜索优化", "不存在的关键词"])
        p0 = [r for r in recs if r.priority == "P0"]
        assert p0, "缺少关键词时应给出 P0 建议"
        assert "不存在的关键词" in p0[0].action
        assert p0[0].falsifiability_check

    def test_all_recs_have_verification(self, item):
        _, _, recs = audit_one(item, keywords=["AI搜索优化"])
        assert all(r.falsifiability_check for r in recs)
        assert all(r.priority in ("P0", "P1", "P2") for r in recs)

    def test_sorted_by_priority(self, item):
        _, _, recs = audit_one(item, keywords=["AI搜索优化", "缺词"])
        order = [r.priority for r in recs]
        assert order == sorted(order, key={"P0": 0, "P1": 1, "P2": 2}.get)

    def test_markdown_bullet_list_not_flagged(self):
        item = ArticleItem(
            title="随便写写",
            url="https://zhuanlan.zhihu.com/p/998",
            content_type="Article",
            content_text="- 第一点\n- 第二点\n- 第三点\n- 第四点\n- 第五点",
            vote_count=0,
            comment_count=0,
            favorite_count=0,
            author_name="",
            author_badge="",
            updated_at=int(time.time()),
        )
        _, _, recs = audit_one(item)
        assert not any("没有列表/分点结构" in r.action for r in recs)

    def test_plain_text_gets_list_recommendation(self):
        item = ArticleItem(
            title="随便写写",
            url="https://zhuanlan.zhihu.com/p/997",
            content_type="Article",
            content_text="随便写写。随便聊聊。没有更多。",
            vote_count=0,
            comment_count=0,
            favorite_count=0,
            author_name="",
            author_badge="",
            updated_at=int(time.time()),
        )
        _, _, recs = audit_one(item)
        assert any("没有列表/分点结构" in r.action for r in recs)


class TestReport:
    def test_markdown_contains_key_sections(self, item):
        scores, benchmark, recs = audit_one(item, "AI搜索优化")
        md = render_markdown(item, scores, benchmark, recs, "AI搜索优化")
        assert "可见度审计报告" in md
        assert item.title in md
        assert item.url in md
        assert "综合得分" in md
        assert "行动清单" in md
        assert "验证方式" in md

    def test_json_roundtrip(self, item, tmp_path):
        scores, benchmark, recs = audit_one(item)
        payload = render_json(item, scores, benchmark, recs)
        assert payload["scores"]["overall"] == scores.overall
        assert payload["scores"]["grade"] == scores.grade
        assert isinstance(payload["recommendations"], list)
        # D2 output contract：推荐项统一六元结构
        if payload["recommendations"]:
            keys = set(payload["recommendations"][0])
            assert {
                "finding", "evidence", "impact", "fix", "confidence", "falsifiability",
            } <= keys

    def test_blocked_article_shows_cap_reason(self, item):
        from scorer import audit_article

        blocked = audit_article(title="", content_text="正文内容足够长。" * 30)
        md = render_markdown(item, blocked, {}, [], "AI搜索优化")
        assert "已封顶" in md
        assert "阻断原因" in md
        assert "缺少标题" in md

    def test_save_report_writes_md_and_json(self, item, tmp_path):
        scores, benchmark, recs = audit_one(item)
        paths = save_report(item, scores, benchmark, recs, out_dir=tmp_path)
        assert len(paths) == 2
        assert all(p.exists() for p in paths)
        data = json.loads(paths[1].read_text(encoding="utf-8"))
        assert data["scores"]["overall"] == scores.overall


class TestDimensionCoverage:
    def test_markdown_renders_all_five_dimensions(self, item):
        scores, benchmark, recs = audit_one(item)
        md = render_markdown(item, scores, benchmark, recs)
        for label in ("AI 可引用性", "内容质量", "关键词覆盖", "结构与格式", "互动数据"):
            assert label in md

    def test_authority_rule_fires_on_weak_content(self):
        weak = ArticleItem(
            title="随便写写",
            url="https://zhuanlan.zhihu.com/p/999",
            content_type="Article",
            content_text="随便写写。随便聊聊。没有更多。",
            vote_count=0,
            comment_count=0,
            favorite_count=0,
            author_name="",
            author_badge="",
            updated_at=int(time.time()),
        )
        _, _, recs = audit_one(weak)
        assert any("完善知乎作者主页" in r.action for r in recs)

    def test_sub_rule_keys_match_scorer(self, item):
        scores, _, _ = audit_one(item)
        citability_subs = scores.sub_scores["AI 可引用性"].sub_scores
        quality_subs = scores.sub_scores["内容质量"].sub_scores
        assert set(CITABILITY_SUB_RULES) <= set(citability_subs)
        assert set(QUALITY_SUB_RULES) <= set(quality_subs)


class TestMain:
    def test_audit_url_ok(self, item, tmp_path, monkeypatch):
        monkeypatch.setattr("audit.resolve_article", lambda u, q: item)
        monkeypatch.setattr(
            "audit.zhihu_api.topic_benchmark",
            lambda q, count=10: {"avg_votes": 40.0},
        )
        code = main(["--url", item.url, "--query", "AI搜索", "--output", str(tmp_path)])
        assert code == 0
        assert list(tmp_path.glob("*.md"))

    def test_audit_url_full_browser(self, item, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("audit.resolve_article", lambda u, q: item)
        monkeypatch.setattr(
            "audit.zhihu_api.topic_benchmark",
            lambda q, count=10: {},
        )
        monkeypatch.setattr(
            "audit.fetch_full_content",
            lambda url: {"title": "标题", "content": "完整正文内容。" * 50},
        )
        code = main(["--url", item.url, "--full", "--output", str(tmp_path)])
        assert code == 0
        assert item.content_text.startswith("完整正文内容")
        captured = capsys.readouterr().out
        assert "浏览器全文" in captured
        scores, _, _ = audit_one(item)
        payload = render_json(item, scores, {}, [], content_source="browser")
        assert payload["content_source"] == "browser"
        assert "整篇粒度评分，非逐段" in payload["source_note"]

    def test_audit_url_full_fallback(self, item, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("audit.resolve_article", lambda u, q: item)
        monkeypatch.setattr(
            "audit.zhihu_api.topic_benchmark",
            lambda q, count=10: {},
        )
        monkeypatch.setattr(
            "audit.fetch_full_content",
            lambda url: {"error": "Playwright 未安装"},
        )
        code = main(["--url", item.url, "--full", "--output", str(tmp_path)])
        assert code == 0
        captured = capsys.readouterr().out
        assert "降级" in captured
        md = list(tmp_path.glob("*.md"))[-1].read_text(encoding="utf-8")
        assert "降级" in md
        payload = json.loads(
            list(tmp_path.glob("audit-*.json"))[-1].read_text(encoding="utf-8")
        )
        assert payload["content_source"] == "api_summary_fallback"
        assert "失败" in payload["fetch_note"]

    def test_audit_url_full_url_error_hint(self, item, tmp_path, monkeypatch, capsys):
        # URL 类错误（非知乎/非文章）不提示检查 Playwright，避免误导
        monkeypatch.setattr("audit.resolve_article", lambda u, q: item)
        monkeypatch.setattr(
            "audit.zhihu_api.topic_benchmark",
            lambda q, count=10: {},
        )
        monkeypatch.setattr(
            "audit.fetch_full_content",
            lambda url: {"error": "仅支持知乎文章/回答（/p/ 或 /answer/ 链接）"},
        )
        code = main(["--url", item.url, "--full", "--output", str(tmp_path)])
        assert code == 0
        err = capsys.readouterr().err
        assert "仅支持知乎文章/回答" in err
        assert "浏览器全文抓取失败" not in err
        payload = json.loads(
            list(tmp_path.glob("audit-*.json"))[-1].read_text(encoding="utf-8")
        )
        # URL 类错误：标为 api_summary（等同跳过）而非 fallback，原因写入 JSON
        assert payload["content_source"] == "api_summary"
        assert "已跳过" in payload["fetch_note"]

    def test_render_markdown_content_source_browser(self, item):
        scores, benchmark, recs = audit_one(item, "AI搜索优化")
        md = render_markdown(
            item, scores, benchmark, recs, "AI搜索优化", content_source="browser"
        )
        assert "本机浏览器采集的完整正文" in md
        assert "整篇正文打分" in md
        assert "摘要打分" not in md

    def test_render_markdown_fallback_not_suggesting_full(self, item):
        scores, benchmark, recs = audit_one(item, "AI搜索优化")
        md = render_markdown(
            item, scores, benchmark, recs, "AI搜索优化",
            content_source="api_summary_fallback",
        )
        assert "已降级" in md
        assert "可用 audit --url" not in md
        assert "Playwright" not in md

    def test_render_markdown_api_summary_suggests_full(self, item):
        scores, benchmark, recs = audit_one(item, "AI搜索优化")
        md = render_markdown(
            item, scores, benchmark, recs, "AI搜索优化",
            content_source="api_summary",
        )
        assert "可用 `audit --url <url> --full`" in md

    def test_render_markdown_skipped_full_no_suggestion(self, item):
        # 已尝试 --full 但 URL 被跳过：不再提示「可用 --full」
        scores, benchmark, recs = audit_one(item, "AI搜索优化")
        md = render_markdown(
            item, scores, benchmark, recs, "AI搜索优化",
            content_source="api_summary",
            fetch_note="--full 已跳过：仅支持知乎文章/回答（/p/ 或 /answer/ 链接）",
        )
        assert "可用 `audit --url" not in md
        assert "全文抓取说明：--full 已跳过" in md
        # 合并后无重复的「本次已尝试 --full」独立行
        assert "本次已尝试" not in md

    def test_render_unknown_content_source_no_keyerror(self, item):
        # 新增来源未同步字典时用默认值，不抛 KeyError
        scores, benchmark, recs = audit_one(item, "AI搜索优化")
        md = render_markdown(
            item, scores, benchmark, recs, "AI搜索优化",
            content_source="unknown_future_source",
        )
        assert "数据来源" in md
        payload = render_json(
            item, scores, {}, [], content_source="unknown_future_source"
        )
        assert payload["content_source"] == "unknown_future_source"

    def test_url_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("audit.resolve_article", lambda u, q: None)
        assert main(["--url", "https://zhuanlan.zhihu.com/p/1", "--query", "q"]) == 2

    def test_me_with_index(self, item, tmp_path, monkeypatch):
        monkeypatch.setattr("audit.load_my_articles", lambda limit: [item])
        code = main(["--me", "--index", "0", "--output", str(tmp_path)])
        assert code == 0
        assert list(tmp_path.glob("*.md"))

    def test_auth_error_returns_1(self, item, tmp_path, monkeypatch):
        def boom(*_args, **_kwargs):
            raise zhihu_api.AuthError(20001, "invalid secret")

        monkeypatch.setattr("audit.resolve_article", boom)
        assert main(["--url", item.url, "--query", "q"]) == 1

    def test_missing_source_arg_exits(self):
        with pytest.raises(SystemExit):
            main(["--query", "q"])

    def test_full_requires_url(self):
        # --full 仅支持 --url 模式，与 --me/--topic 连用显式报错
        assert main(["--me", "--full"]) == 2
        assert main(["--topic", "x", "--full"]) == 2

    def test_topic_top_zero_rejected(self):
        with pytest.raises(SystemExit):
            main(["--topic", "x", "--top", "0"])

    def test_oserror_reports_local_write(self, item, monkeypatch, capsys):
        monkeypatch.setattr("audit.resolve_article", lambda u, q: item)
        monkeypatch.setattr("audit.audit_one", lambda it, q=None, k=None: (None, {}, []))

        def boom(*_args, **_kwargs):
            raise OSError("磁盘空间不足")

        monkeypatch.setattr("audit.save_report", boom)
        code = main(["--url", item.url, "--query", "q"])
        err = capsys.readouterr().err
        assert code == 1
        assert "本地读写失败" in err
