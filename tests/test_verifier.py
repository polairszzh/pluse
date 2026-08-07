"""verifier.py 单元测试 —— 报告前建议去重 / 矛盾检测 / 排序"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from audit import Recommendation
from verifier import dedupe_recommendations, detect_conflicts, sort_recommendations


def rec(priority: str, dimension: str, action: str) -> Recommendation:
    return Recommendation(
        priority=priority, dimension=dimension, action=action,
        expected_impact="impact", falsifiability_check="verify",
    )


class TestDedupe:
    def test_same_action_keeps_highest_priority(self):
        recs = [
            rec("P1", "内容", "补充一段自包含答案块"),
            rec("P0", "内容", "补充一段 自包含答案块。"),
        ]
        out = dedupe_recommendations(recs)
        assert len(out) == 1
        assert out[0].priority == "P0"

    def test_distinct_actions_kept(self):
        recs = [
            rec("P0", "内容", "补充自包含答案块"),
            rec("P1", "结构", "重排 H2 层级"),
        ]
        assert len(dedupe_recommendations(recs)) == 2

    def test_synonym_not_deduped(self):
        # 同义词（增加/补充）不做合并——归一化仅处理空白/标点差异，行为锁定
        recs = [
            rec("P0", "内容", "增加段落篇幅"),
            rec("P1", "内容", "补充段落篇幅"),
        ]
        assert len(dedupe_recommendations(recs)) == 2


class TestConflicts:
    def test_expand_vs_shrink_conflict(self):
        recs = [
            rec("P0", "内容", "增加段落篇幅，丰富细节"),
            rec("P1", "内容", "精简段落，删减冗余"),
        ]
        conflicts = detect_conflicts(recs)
        assert len(conflicts) == 1
        assert "方向矛盾" in conflicts[0][2]

    def test_different_dimension_no_conflict(self):
        recs = [
            rec("P0", "内容", "增加段落篇幅"),
            rec("P1", "结构", "精简标题"),
        ]
        assert detect_conflicts(recs) == []


class TestSort:
    def test_priority_order(self):
        recs = [rec("P2", "d", "x"), rec("P0", "d", "y"), rec("P1", "d", "z")]
        out = sort_recommendations(recs)
        assert [r.priority for r in out] == ["P0", "P1", "P2"]
