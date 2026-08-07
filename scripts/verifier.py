"""报告前 verifier：对行动建议做去重、矛盾检测与排序。

audit / track / adapt 输出报告前统一调用，避免重复建议、互相矛盾的建议
混入最终清单（roadmap D1）。
"""
from __future__ import annotations

import re
from typing import Any

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

_EXPAND_RE = re.compile(r"(增加|补充|扩写|添加|丰富|加长|扩展)")
_SHRINK_RE = re.compile(r"(精简|删减|缩短|删除|削减|压缩)")


def _normalize_action(text: str) -> str:
    """归一化 action 文本：去空白/标点/全半角，用于相似度比较"""
    t = re.sub(r"[\s\u3000，。、；：！？（）()「」『』\"'`]+", "", text or "")
    return t.lower()


def dedupe_recommendations(recs: list[Any]) -> list[Any]:
    """按 action 归一化去重：同义建议只保留优先级最高的一条，保持原顺序"""
    seen: dict[str, Any] = {}
    for r in recs:
        key = _normalize_action(r.action)
        if key not in seen:
            seen[key] = r
            continue
        cur = PRIORITY_ORDER.get(r.priority, 9)
        prev = PRIORITY_ORDER.get(seen[key].priority, 9)
        if cur < prev:
            seen[key] = r
    return list(seen.values())


def detect_conflicts(recs: list[Any]) -> list[tuple[Any, Any, str]]:
    """检测同维度内方向矛盾的建议（如「增加篇幅」vs「精简篇幅」）。

    返回 [(建议A, 建议B, 说明)]。启发式匹配「增加/补充/扩写」与「精简/删减/缩短」类动作词。
    """
    conflicts: list[tuple[Any, Any, str]] = []
    by_dim: dict[str, list[Any]] = {}
    for r in recs:
        by_dim.setdefault(r.dimension, []).append(r)
    for items in by_dim.values():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if _EXPAND_RE.search(a.action) and _SHRINK_RE.search(b.action):
                    conflicts.append((a, b, f"「{a.action[:26]}…」与「{b.action[:26]}…」方向矛盾"))
                elif _SHRINK_RE.search(a.action) and _EXPAND_RE.search(b.action):
                    conflicts.append((b, a, f"「{b.action[:26]}…」与「{a.action[:26]}…」方向矛盾"))
    return conflicts


def sort_recommendations(recs: list[Any]) -> list[Any]:
    """按优先级排序（P0 → P1 → P2），稳定排序保留同级原顺序"""
    return sorted(recs, key=lambda r: PRIORITY_ORDER.get(r.priority, 9))
