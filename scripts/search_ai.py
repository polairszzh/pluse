"""Pulse AI 平台引用监控执行层 —— /pulse track 的脚本端。

输入品牌/关键词，探测其在 DeepSeek、Kimi、豆包、元宝里的被提及情况：
  - DeepSeek：有公开 API（OpenAI 兼容），直接发固定探测问题，
    判断回答里是否出现品牌、情感倾向，并截取上下文。
  - Kimi / 豆包 / 元宝：无公开 API，用 Bing 搜索结果推断其检索库中
    是否存在品牌内容（存在信号，不是该平台的真实引用）。

每次探测结果写入 data/monitor.db（SQLite）；重跑同一品牌时与历史快照
对比，输出趋势。报告落盘 data/snapshots/track-*.md + track-*.json。

Phase 2 边界（诚实标注，不夸大）：
  - DeepSeek 被提及 = 回答正文出现品牌名（精确匹配），原始回答存入 meta 供人工复核；
  - Kimi/豆包/元宝 是搜索引擎存在信号推断，报告中必须保留这条局限说明。

本文件为 CLI 门面：逻辑已拆到 track_config / track_models / track_utils /
track_probe / track_db / track_report，这里保留入口与全部公共接口再导出
（测试与外部脚本按 search_ai.<name> 引用，行为不变）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 以下为公共接口再导出：tests / fact_checker / content_adapter / bottleneck_diag
# 仍按 `from search_ai import ...` 与 `search_ai.<name>` 引用，行为不变。
# F401 豁免见 pyproject.toml per-file-ignores（门面文件以再导出为主）。
from track_config import (
    _NEG_1CHAR,
    _NEG_2CHAR,
    _NEG_3CHAR,
    DEEPSEEK_BASE,
    DEEPSEEK_MODEL,
    DEFAULT_DB,
    NEGATIVE_WORDS,
    POSITIVE_WORDS,
    PRIORITY_ORDER,
    PROBE_PROMPT,
    PROJECT_ROOT,
    SNAPSHOT_DIR,
)
from track_db import (
    _migrate,
    _recent_run_rows,
    build_delta,
    build_trend,
    connect,
    load_history,
    store_results,
)
from track_models import ProbeResult
from track_probe import (
    BAIDU_URL,
    BING_UA,
    BING_URL,
    PLATFORMS,
    _baidu_target_url,
    _classify_empty_page,
    _count_mentions,
    _is_negated,
    _parse_baidu,
    _parse_bing,
    _site_query,
    check_index,
    classify_sentiment,
    probe_deepseek,
    probe_search_inference,
)
from track_report import (
    CONFIDENCE_LABEL,
    SENTIMENT_LABEL,
    STATUS_LABEL,
    build_recommendations,
    render_json,
    render_markdown,
    save_report,
)
from track_utils import (
    _aggregate_binary,
    _aggregate_bool_tristate,
    _aggregate_samples,
    _classify_cited_type,
    _detect_mine,
    _extract_fact_risks,
    _majority,
    _md_cell,
    _non_empty_query,
    _parse_mine_ids,
    _positive_int,
    _shell_quote,
    _truncate,
    _union_strings,
    _url_identity,
    _url_present,
    _wilson_interval,
    re_slug,
)

# --------------------------------------------------------------------------
# 配置加载
# --------------------------------------------------------------------------


def _load_key() -> str | None:
    """按顺序从环境变量和 .env 读取 DEEPSEEK_API_KEY / LLM_API_KEY"""
    candidates = ("DEEPSEEK_API_KEY", "LLM_API_KEY")
    for key in candidates:
        value = os.environ.get(key, "").strip().strip('"').strip("'")
        if value and value != "your_api_key_here":
            return value
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() in candidates:
                    value = v.strip().strip('"').strip("'")
                    if value and value != "your_api_key_here":
                        return value
    return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parse_platforms(raw: str | None) -> list[str]:
    if not raw:
        return list(PLATFORMS)
    parts = [s.strip().lower() for s in raw.split(",") if s.strip()]
    bad = [p for p in parts if p not in PLATFORMS]
    if bad:
        raise argparse.ArgumentTypeError(
            f"未知平台: {'、'.join(bad)}（可用: {'、'.join(PLATFORMS)}）"
        )
    return list(dict.fromkeys(parts))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse-track",
        description="Pulse AI 平台引用监控 —— 探测品牌在 DeepSeek/Kimi/豆包/元宝的被提及情况，"
                    "写入 monitor.db 并对比历史快照生成趋势",
    )
    entry = parser.add_mutually_exclusive_group(required=True)
    entry.add_argument("--query", type=_non_empty_query, help="品牌名或关键词（与 --index-check 二选一）")
    entry.add_argument(
        "--index-check",
        type=_non_empty_query,
        help="检查单篇内容 URL 在主流检索源（Bing/百度）的收录状态（与 --query 二选一）",
    )
    parser.add_argument(
        "--mine",
        action="append",
        default=[],
        help="你的内容标识（URL/标题/作者名），可重复传多次（--mine <URL> --mine <昵称>），一次一个；"
             "传了才会额外判断 AI 回答/搜索结果里是否出现你的内容",
    )
    parser.add_argument(
        "--mine-owned",
        action="append",
        default=[],
        help="转载/自有渠道内容标识（区别于 --mine 原创内容），可重复传；"
             "仅命中 owned 且未命中任何原创标识时，引用类型才记为「转载（owned）」",
    )
    parser.add_argument(
        "--competitor",
        action="append",
        default=[],
        help="竞品内容标识（URL/标题/作者名），可重复传；"
             "传了会检测 AI 回答/搜索结果里是否出现竞品，用于 lostprompt（竞品夺走）分析",
    )
    parser.add_argument(
        "--samples",
        type=_positive_int,
        default=argparse.SUPPRESS,
        help="每平台采样次数（未指定时取 5）：多次探测计算被提及概率与置信区间；1 为单次判定",
    )
    parser.add_argument(
        "--platforms",
        type=_parse_platforms,
        help="逗号分隔的平台列表，默认全部（deepseek,kimi,doubao,yuanbao）",
    )
    parser.add_argument("--output", help="输出目录（默认 data/snapshots/）")
    parser.add_argument("--db", help="monitor.db 路径（默认 data/monitor.db，测试可指定临时库）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.index_check:
        conflicting = [
            name
            for name, val in (
                ("--mine", args.mine),
                ("--mine-owned", args.mine_owned),
                ("--competitor", args.competitor),
                ("--platforms", args.platforms),
                ("--samples", getattr(args, "samples", None)),
                ("--output", args.output),
                ("--db", args.db),
            )
            if val
        ]
        if conflicting:
            print(
                f"--index-check 与 {'、'.join(conflicting)} 互斥，请勿同时传入",
                file=sys.stderr,
            )
            return 2
        result = check_index(args.index_check)
        label = {"bing": "Bing", "baidu": "百度"}
        status_txt = {
            "indexed": "已收录",
            "likely_indexed": "疑似收录",
            "not_indexed": "未收录",
            "error": "探测失败",
        }
        print(f"收录检查：{result['url']}")
        print(f"查询：{result['query']}")
        for name, src in result["sources"].items():
            txt = status_txt.get(src["status"], src["status"])
            extra = f"（{src['error']}）" if src["status"] == "error" else ""
            print(f"  {label.get(name, name)}：{txt}{extra}")
        ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        out_path = SNAPSHOT_DIR / f"index-check-{re_slug(args.index_check)}-{ts}.json"
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"已保存：{out_path}")
        return 0
    platforms = args.platforms or list(PLATFORMS)
    earned_ids = list(dict.fromkeys(m.strip() for m in args.mine if m.strip()))
    owned_ids = list(dict.fromkeys(m.strip() for m in args.mine_owned if m.strip()))
    competitor_ids = list(dict.fromkeys(m.strip() for m in args.competitor if m.strip()))
    overlap = [m for m in owned_ids if m in earned_ids]
    if overlap:
        print(
            f"  [提示] 以下标识同时传入 --mine 与 --mine-owned，按原创（earned）处理：{'、'.join(overlap)}",
            file=sys.stderr,
        )
        owned_ids = [m for m in owned_ids if m not in earned_ids]
    mine_ids = list(dict.fromkeys(earned_ids + owned_ids))
    db_path = Path(args.db) if args.db else DEFAULT_DB
    out_dir = Path(args.output) if args.output else None
    # --samples 用 SUPPRESS 默认：未传时 args.samples 属性不存在，须 getattr 兜底
    samples = getattr(args, "samples", None) or 5

    try:
        raw_samples: list[ProbeResult] = []
        for sample_idx in range(samples):
            for p in platforms:
                r = PLATFORMS[p]["probe"](
                    args.query,
                    mine_ids=mine_ids,
                    owned_ids=owned_ids,
                    competitor_ids=competitor_ids,
                )
                r.sample_idx = sample_idx
                raw_samples.append(r)
        store_results(raw_samples, db_path=db_path)
        # 多采样聚合：每平台多数派判定 + 被提及概率/置信区间
        results = [
            _aggregate_samples([r for r in raw_samples if r.platform == p])
            for p in platforms
        ]
        trend = build_trend(args.query, db_path=db_path)
        delta = build_delta(args.query, db_path=db_path, trend=trend, platforms=platforms)
        recs = build_recommendations(args.query, results, delta=delta)
        paths = save_report(args.query, results, trend, recs, out_dir, delta)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"数据/配置异常：{exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    except OSError as exc:
        print(f"本地读写失败：{exc}", file=sys.stderr)
        return 1

    print(f"监测对象：{args.query}")
    for r in results:
        label = PLATFORMS[r.platform]["label"]
        mine = ""
        if r.mine_ids:
            mine_txt = {True: "是", False: "否", None: "—"}.get(r.mine_cited, "—")
            if r.mine_cited is True:
                if r.cited_type == "earned":
                    mine_txt += "（原创）"
                elif r.cited_type == "owned":
                    mine_txt += "（转载）"
                else:
                    mine_txt += "（未知）"
            mine = f" · 我的内容 {mine_txt}"
        if r.status == "ok":
            if r.sample_count > 1 and r.prob is not None:
                hits = r.meta.get("sample_hits", 0)
                invalid = r.meta.get("sample_invalid", 0)
                invalid_note = f"，{invalid} 次无效" if invalid else ""
                ci_note = (
                    f"，CI {r.ci_low:.0%}-{r.ci_high:.0%}"
                    if r.ci_low is not None and r.ci_high is not None else ""
                )
                cited = (
                    f"{'是' if r.cited else '否'} "
                    f"({r.prob:.0%}, {hits}/{r.sample_count}{invalid_note}{ci_note})"
                )
            else:
                cited = {True: "是", False: "否", None: "未知"}.get(r.cited, "未知")
            extra = f" · 情感 {SENTIMENT_LABEL.get(r.sentiment or '', '—')}" if r.sentiment else ""
            conf = f" · 置信度 {CONFIDENCE_LABEL.get(r.confidence or '', '—')}"
            print(f"  {label}：{STATUS_LABEL[r.status]} · 被提及 {cited}{extra}{conf}{mine}")
        else:
            print(f"  {label}：{STATUS_LABEL.get(r.status, r.status)} · {_md_cell(r.context)}{mine}")
        if r.fact_risks:
            print(f"  [未核实断言] {label} 回答出现：{'、'.join(r.fact_risks)}（建议人工复核）")
    print(f"趋势对比：{trend['total_runs']} 次快照 · {len(trend['changes'])} 处引用状态变化")
    if delta["platforms"]:
        cited_label = {"added": "新增被提及", "lost": "丢失被提及", "same": "无变化"}
        for platform, item in delta["platforms"].items():
            label = PLATFORMS.get(platform, {}).get("label", platform)
            if item.get("note"):
                print(f"  {label}：{item['note']}")
                continue
            cited = cited_label.get(item.get("cited_change"))
            flip = item.get("sentiment_flip")
            mine = {"gained": "我的内容新增被引用", "lost": "我的内容丢失被引用"}.get(item.get("mine_change"), "")
            parts = [p for p in (cited, flip) if p]
            if item.get("competitor_replaced"):
                suffix = "" if item.get("competitor_replaced_confirmed") else "（推断）"
                parts.append(
                    f"竞品夺走{suffix}（{item.get('competitor_replaced_at', '')}）"
                )
            if parts or mine:
                body = "、".join(parts)
                if mine:
                    body = f"{body} · {mine}" if body else mine
                print(f"  与上次对比 · {label}：{body}")
    print(f"行动建议：{len(recs)} 条（P0={sum(1 for r in recs if r.priority == 'P0')}）")
    for p in paths:
        print(f"已保存：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
