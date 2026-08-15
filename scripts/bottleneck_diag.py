"""A2 八层瓶颈定位 —— 改写前先判断卡在哪一层

一个问句没被引用，先判断卡在哪层（没收录 / 查询没匹配 / 检索没召回 /
内容没被选中），再决定是改内容还是换渠道，别上来就改文。

把 HeiGe 八层（记忆→索引→查询→检索→重排→装配→引用→治理）映射为
个人创作者可诊断的四类：
  - 记忆/索引层：话题从未被 AI 提及 → 没收录/没进入检索（改内容无效，先解决收录）
  - 检索/选择层：话题被提及但你的内容未被选中 → 内容适配（A 系列）
  - 引用层：内容已被引用 → 保持/加固
  - 风险/治理层：被引用但有负面/竞品夺走/未核实断言 → 治理

用法：
  python scripts/bottleneck_diag.py --query <话题> [--mine <内容标识>] [--index-status <indexed|not_indexed|unknown>]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import search_ai

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "monitor.db"


LAYER_LABELS = {
    "memory_index": "记忆/索引层",
    "retrieval_selection": "检索/选择层",
    "citation": "引用层",
    "governance": "风险/治理层",
    "no_data": "无数据",
    "unknown": "无法判定",
}


def diagnose(
    query: str,
    db_path: Path = DEFAULT_DB,
    mine_ids: list[str] | None = None,
    index_status: str | None = None,
) -> dict:
    """基于 track 历史 + 可选收录状态判定瓶颈层"""
    trend = search_ai.build_trend(query, db_path=db_path)
    series = trend["series"]
    has_track_data = bool(series)
    has_ok_data = False

    # 平台汇总：被提及 / 我的内容被引用 / 风险信号
    platform_summary: dict[str, dict] = {}
    any_cited = False
    any_mine_cited = False
    any_risk = False
    for platform, points in series.items():
        ok_points = [p for p in points if p.get("status") == "ok"]
        if not ok_points:
            continue
        has_ok_data = True
        cited = any(p.get("cited") is True for p in ok_points)
        mine_cited = any(p.get("mine_cited") is True for p in ok_points)
        risk = any(p.get("fact_risks") or p.get("competitor_matched") for p in ok_points)
        platform_summary[platform] = {
            "mentioned": cited,
            "mine_cited": mine_cited,
            "risk": risk,
        }
        any_cited = any_cited or cited
        any_mine_cited = any_mine_cited or mine_cited
        any_risk = any_risk or risk

    mine_checked = bool(mine_ids)

    # 判定层
    layer = "unknown"
    reason = ""
    direction = ""
    if not has_track_data:
        layer = "no_data"
        reason = "该话题没有 track 历史，先跑 /pulse track 建立基线"
        direction = "先建立基线（track --samples 5）"
    elif not has_ok_data:
        layer = "unknown"
        reason = "话题有 track 历史但均为失败/未配置，无有效探测数据"
        direction = "重跑 track（检查网络/密钥）后再诊断"
    elif index_status == "not_indexed":
        layer = "memory_index"
        reason = "内容未被搜索引擎收录（site: 无命中）——话题就算被检索也召回不到你的内容"
        direction = "先解决收录（渠道/平台收录机制），改内容无效"
    elif not any_cited:
        layer = "memory_index"
        reason = "话题从未被任何平台提及——AI 检索库里没有该话题的存在信号"
        direction = "检查收录 + 是否发布在目标平台生态位（B5/B6）"
    elif any_mine_cited:
        layer = "citation"
        reason = "内容已被引用"
        direction = "保持/加固；如有风险信号再进治理层"
    elif mine_checked:
        layer = "retrieval_selection"
        reason = "话题被提及但你的内容未被选中——检索召回但内容没被引用"
        direction = "内容适配（评分驱动改写 + 强化品牌锚定）"
    else:
        layer = "retrieval_selection"
        reason = "话题被提及；未检查你的内容是否被引用（未传 --mine）"
        direction = "传 --mine 检查内容归属，再决定是否内容适配"

    if any_risk and layer in ("citation", "retrieval_selection"):
        layer = "governance"
        reason += "；且存在风险信号（未核实断言/竞品夺走）"
        direction = "先治理风险（复核断言、竞品分析），再谈优化"

    return {
        "query": query,
        "has_track_data": has_track_data,
        "layer": layer,
        "layer_label": LAYER_LABELS[layer],
        "reason": reason,
        "direction": direction,
        "platform_summary": platform_summary,
        "index_status": index_status,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pulse-bottleneck", description="A2 八层瓶颈定位")
    parser.add_argument("--query", required=True, type=str, help="目标话题")
    parser.add_argument(
        "--mine", action="append", default=[],
        help="你的内容标识（URL/标题/作者名），可重复传；传了才判定「内容未被选中」",
    )
    parser.add_argument(
        "--index-status",
        choices=["indexed", "not_indexed", "unknown"],
        default=None,
        help="收录状态（来自 index-check），提供后优先判定收录层",
    )
    parser.add_argument("--db", help="monitor.db 路径（默认 data/monitor.db）")
    args = parser.parse_args(argv)

    result = diagnose(
        args.query,
        db_path=Path(args.db) if args.db else DEFAULT_DB,
        mine_ids=list(dict.fromkeys(args.mine)),
        index_status=args.index_status,
    )
    print(f"瓶颈定位：{result['layer_label']}")
    print(f"依据：{result['reason']}")
    print(f"方向：{result['direction']}")
    if result["platform_summary"]:
        print("平台摘要：")
        for platform, info in result["platform_summary"].items():
            label = search_ai.PLATFORMS.get(platform, {}).get("label", platform)
            print(
                f"  {label}：被提及 {'是' if info['mentioned'] else '否'}"
                f" · 我的内容 {'是' if info['mine_cited'] else '否'}"
                + (" · 有风险信号" if info["risk"] else "")
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
