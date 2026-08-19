"""track 共享常量与路径（从 search_ai.py 拆分，行为不变）"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "snapshots"
DEFAULT_DB = PROJECT_ROOT / "data" / "monitor.db"

DEEPSEEK_BASE = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

PROBE_PROMPT = (
    "请搜索并介绍「{query}」：它是什么、有什么特点、有哪些值得注意的信息。"
    "如果可获取的信息有限，请如实说明。"
)

POSITIVE_WORDS = (
    "推荐", "优秀", "领先", "好用", "强大", "认可", "称赞",
    "好评", "值得", "高效", "出色", "首选",
)
NEGATIVE_WORDS = (
    "失望", "投诉", "诈骗", "骗局", "糟糕", "劣质", "后悔", "差评", "坑人", "翻车", "踩坑",
)

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# 否定前缀（正面词/负面词共用）：1 字、2 字、3 字三档
_NEG_1CHAR = ("不", "没", "无", "未")
_NEG_2CHAR = ("不太", "并不", "并非", "没有", "并无")
_NEG_3CHAR = ("谈不上", "说不上", "算不上")
