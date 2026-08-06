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
    build_recommendations,
    build_trend,
    classify_sentiment,
    connect,
    load_history,
    main,
    probe_deepseek,
    probe_search_inference,
    re_slug,
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

    def test_empty_inputs(self):
        assert _detect_mine("", ["a"]) == []
        assert _detect_mine("任意文本", []) == []


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
        recs = build_recommendations("品牌A", results)
        assert any(r.priority == "P0" and r.dimension == "内容引用归属" for r in recs)
        assert all(r.falsifiability_check for r in recs)

    def test_inference_mine_missing_p1(self):
        results = [ProbeResult(
            "品牌A", "kimi", "ok", True, None, "c", "search_inference", True,
            mine_cited=False, mine_ids=["https://a.com/1"],
        )]
        recs = build_recommendations("品牌A", results)
        assert any(r.priority == "P1" and r.dimension == "内容收录" for r in recs)

    def test_mine_cited_no_p0(self):
        results = [ProbeResult(
            "品牌A", "deepseek", "ok", True, "positive", "c", "api", False,
            mine_cited=True, mine_ids=["https://a.com/1"],
        )]
        recs = build_recommendations("品牌A", results)
        assert not any(r.dimension == "内容引用归属" for r in recs)


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

    def test_platform_registry(self):
        assert set(PLATFORMS) == {"deepseek", "kimi", "doubao", "yuanbao"}
        assert PLATFORMS["deepseek"]["probe"] is probe_deepseek
