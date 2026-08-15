"""A2 八层瓶颈定位测试"""


import bottleneck_diag as bottleneck
import search_ai


def _mk(platform, cited, mine_cited=None, mine_ids=None, fact_risks=None,
        competitor=False, sample_idx=0):
    return search_ai.ProbeResult(
        "话题A", platform, "ok", cited, "positive", "c", "api", False,
        mine_cited=mine_cited, mine_ids=mine_ids or [],
        fact_risks=fact_risks or [], competitor_matched=competitor,
        sample_idx=sample_idx,
    )


class TestDiagnose:
    def test_no_data(self, tmp_path):
        result = bottleneck.diagnose("话题A", db_path=tmp_path / "m.db")
        assert result["layer"] == "no_data"
        assert "先建立基线" in result["direction"]

    def test_all_invalid_history_unknown(self, tmp_path):
        # 有历史但全部失败/未配置：不得误判「从未提及」
        db = tmp_path / "m.db"
        search_ai.store_results(
            [search_ai.ProbeResult(
                "话题A", "deepseek", "error", None, None, "boom", "api", True,
                error="x",
            )],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        result = bottleneck.diagnose("话题A", db_path=db)
        assert result["layer"] == "unknown"
        assert "无有效探测数据" in result["reason"]

    def test_never_mentioned_memory_index(self, tmp_path):
        db = tmp_path / "m.db"
        search_ai.store_results(
            [_mk("deepseek", False), _mk("kimi", False)],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        result = bottleneck.diagnose("话题A", db_path=db)
        assert result["layer"] == "memory_index"
        assert "从未被任何平台提及" in result["reason"]

    def test_index_status_not_indexed_priority(self, tmp_path):
        db = tmp_path / "m.db"
        search_ai.store_results(
            [_mk("deepseek", True)],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        result = bottleneck.diagnose(
            "话题A", db_path=db, index_status="not_indexed"
        )
        assert result["layer"] == "memory_index"
        assert "未被搜索引擎收录" in result["reason"]

    def test_index_status_priority_over_no_data(self, tmp_path):
        # 无 track 历史但显式提供未收录：优先判收录层，不抢 no_data
        result = bottleneck.diagnose(
            "话题A", db_path=tmp_path / "m.db", index_status="not_indexed"
        )
        assert result["layer"] == "memory_index"

    def test_index_status_priority_over_invalid_history(self, tmp_path):
        db = tmp_path / "m.db"
        search_ai.store_results(
            [search_ai.ProbeResult(
                "话题A", "deepseek", "error", None, None, "boom", "api", True,
                error="x",
            )],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        result = bottleneck.diagnose(
            "话题A", db_path=db, index_status="not_indexed"
        )
        assert result["layer"] == "memory_index"

    def test_mentioned_but_mine_not_checked(self, tmp_path):
        db = tmp_path / "m.db"
        search_ai.store_results(
            [_mk("deepseek", True)],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        result = bottleneck.diagnose("话题A", db_path=db)
        assert result["layer"] == "retrieval_selection"
        assert "未传 --mine" in result["reason"]

    def test_new_mine_unchecked_in_history_unknown(self, tmp_path):
        # 历史用旧 mine 检查过、本次传新 mine：不得沿用旧 mine_cited 误判
        db = tmp_path / "m.db"
        search_ai.store_results(
            [_mk("deepseek", True, mine_cited=True, mine_ids=["https://a.com/1"])],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        result = bottleneck.diagnose(
            "话题A", db_path=db, mine_ids=["https://b.com/2"]
        )
        assert result["layer"] == "unknown"
        assert "未用本次 --mine" in result["reason"]
        assert "重跑 track 带 --mine" in result["direction"]

    def test_mentioned_but_mine_not_cited(self, tmp_path):
        db = tmp_path / "m.db"
        search_ai.store_results(
            [_mk("deepseek", True, mine_cited=False, mine_ids=["https://a.com/1"])],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        result = bottleneck.diagnose(
            "话题A", db_path=db, mine_ids=["https://a.com/1"]
        )
        assert result["layer"] == "retrieval_selection"
        assert "未被选中" in result["reason"]

    def test_mine_cited_citation_layer(self, tmp_path):
        db = tmp_path / "m.db"
        search_ai.store_results(
            [_mk("deepseek", True, mine_cited=True, mine_ids=["https://a.com/1"])],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        result = bottleneck.diagnose(
            "话题A", db_path=db, mine_ids=["https://a.com/1"]
        )
        assert result["layer"] == "citation"

    def test_risk_promotes_to_governance(self, tmp_path):
        db = tmp_path / "m.db"
        search_ai.store_results(
            [_mk("deepseek", True, mine_cited=True, mine_ids=["https://a.com/1"],
                 fact_risks=["版本 2.3.1"])],
            db_path=db, run_at="2026-08-01T10:00:00+08:00",
        )
        result = bottleneck.diagnose(
            "话题A", db_path=db, mine_ids=["https://a.com/1"]
        )
        assert result["layer"] == "governance"


class TestMain:
    def test_cli_output(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(
            bottleneck, "DEFAULT_DB", tmp_path / "m.db"
        )
        assert bottleneck.main(["--query", "话题A"]) == 0
        out = capsys.readouterr().out
        assert "瓶颈定位" in out
        assert "无数据" in out
