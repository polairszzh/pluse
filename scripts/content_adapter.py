"""Pulse 多平台内容适配执行层 —— /pulse adapt 的脚本端。

输入本地 Markdown 草稿（--source），输出：
  - 知乎版：结构化改写 + 适度扩写（H2/H3 层级、段落 120-180 字、保留图片引用与代码块）
  - AI 优化版：每个 H2 一个问答对，首段 130-170 字自包含答案（写给 AI 摘取，不用图）
每个生成版本都带 falsifiability check（发布后如何验证被引用）。

生成策略（混合）：
  - 规则脚手架：结构/长度/关键词/平台禁则来自 skills/visibility/references/content-format.md，
    确定、可测、无 LLM 也可跑（回退路径）。
  - LLM 改写：DeepSeek 可用时对语气/措辞做自然化改写；失败自动回退脚手架。

Phase 3 边界（诚实标注）：
  - 输出是「待发布的草稿」，不是「保证被引用的成品」；发布后 track --mine 才是裁判。
  - 草稿审计只含四个文本维度（AI 可引用性/内容质量/关键词覆盖/结构），互动维度标「未发布」。

本文件为 CLI 门面：逻辑已拆到 adapt_draft / adapt_signals / adapt_rewrite /
adapt_output，这里保留入口与全部公共接口再导出（测试按 content_adapter.<name>
引用，行为不变）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import bottleneck_diag

# 以下为公共接口再导出：tests 仍按 `from content_adapter import ...` 引用，行为不变。
# F401 豁免见 pyproject.toml per-file-ignores（门面文件以再导出为主）。
from adapt_draft import (
    CONTENT_FORMAT_PATH,
    DRAFT_WEIGHTS,
    PRIORITY_ORDER,
    PROJECT_ROOT,
    QUERY_COVERAGE_TERMS,
    DraftDoc,
    _content_format_updated,
    _draft_recommendations,
    _load_content_format,
    _query_keywords,
    _round_half_up,
    _slug,
    _strip_frontmatter,
    _text_without_code,
    parse_markdown,
    score_draft,
)
from adapt_output import (
    AI_PURPOSE_NOTE,
    _collapse_blank_lines,
    _postprocess_llm_output,
    build_manifest,
    build_review_checklist,
    save_manifest,
)
from adapt_rewrite import (
    AI_SYSTEM,
    ZHIHU_SYSTEM,
    _ai_system,
    _clean_ai_text,
    _ensure_blank,
    _fallback_ai_version,
    _fallback_zhihu_version,
    _first_150,
    _format_zhihu_lines,
    _llm_rewrite,
    _rewrite_instructions,
    _rewrite_triggers,
    _split_paragraph,
    _zhihu_system,
    generate_ai_version,
    generate_zhihu_version,
)
from adapt_signals import (
    GENERIC_ALT,
    PATTERN_SUFFIX_BLOCKS,
    PLACEHOLDER_IMAGE_HOSTS,
    PROMOTIONAL_HIGH_PATTERNS,
    PROMOTIONAL_MEDIUM_PATTERNS,
    _gap_block,
    _host_of,
    _image_alt,
    _links_in_text,
    _scan_facts,
    detect_material_gaps,
    detect_promotional_signals,
)
from fact_checker import risk_severity, verify_facts

OUTPUT_DIR = PROJECT_ROOT / "data" / "output"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _existing_file(value: str) -> str:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"文件不存在或不是文件：{value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse-adapt",
        description="Pulse 多平台内容适配 —— 本地 Markdown 草稿 → 知乎版 + AI 优化版",
    )
    parser.add_argument("--source", required=True, type=_existing_file, help="本地 Markdown 草稿路径")
    parser.add_argument("--query", help="目标话题关键词（用于打分与验证命令，默认取草稿标题）")
    parser.add_argument(
        "--platforms",
        help="逗号分隔的版本列表，默认 zhihu,ai（当前支持 zhihu/ai）",
    )
    parser.add_argument("--no-llm", action="store_true", help="强制走规则脚手架，不调用 LLM")
    parser.add_argument("--output", help="输出目录（默认 data/output/）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    platforms = [p.strip().lower() for p in (args.platforms or "zhihu,ai").split(",") if p.strip()]
    platforms = list(dict.fromkeys(platforms))
    supported = {"zhihu", "ai"}
    bad = [p for p in platforms if p not in supported]
    if bad:
        print(f"暂不支持的版本：{'、'.join(bad)}（可用：{'、'.join(sorted(supported))}）", file=sys.stderr)
        return 2
    out_dir = Path(args.output) if args.output else OUTPUT_DIR

    try:
        doc = parse_markdown(Path(args.source).read_text(encoding="utf-8"))
        if not doc.title or doc.title == "untitled":
            print("草稿缺少一级标题（# 标题）。", file=sys.stderr)
            return 2
        query = args.query or doc.title
        keywords = _query_keywords(query)
        draft_score = score_draft(doc.title, doc.body_text or doc.raw, keywords)
        gaps = detect_material_gaps(doc, query)
        # A2 瓶颈定位：改写前先看话题在 AI 检索库里的状态（尽力而为，失败降级不阻断）
        try:
            bottleneck_result = bottleneck_diag.diagnose(query)
        except Exception:  # noqa: BLE001 — 诊断失败不影响生成
            bottleneck_result = None

        high_promo = [
            g for g in gaps
            if g["type"] == "promotional_signal" and g["severity"] == "high"
        ]
        if high_promo:
            print("拒绝生成：检测到明确的营销转化信号，Pulse 不帮这类内容做适配。", file=sys.stderr)
            for gap in high_promo:
                print(f"  [拒绝] {gap['detail']}", file=sys.stderr)
            print(
                "请移除营销转化引导（扫码/私信/优惠码/必买/限时抢购等），"
                "或补充明确的利益关系声明后重试。",
                file=sys.stderr,
            )
            return 3

        # 置信度防火墙：对草稿中的公开声明型数字断言做多源交叉验证（尽力而为，失败降级不阻断）
        try:
            scan_text = _text_without_code(_strip_frontmatter(doc.raw))
            fact_results = verify_facts(scan_text, query)
        except Exception:  # noqa: BLE001 — 防火墙尽力而为：任何异常都降级，不阻断生成
            fact_results = []
        conflicts = [r for r in fact_results if r["status"] == "conflict"]
        if conflicts:
            print("拒绝生成：检测到数据断言与检索结果冲突，Pulse 不帮无法核实的数据做适配。", file=sys.stderr)
            for r in conflicts:
                snippet = "；".join(r["reject_snippets"][:2])
                print(
                    f"  [拒绝] 断言「{r['fact']}」（{r['context'][:30]}…）与来源矛盾：{snippet}",
                    file=sys.stderr,
                )
            print("请核实数据来源或移除该断言后重试。", file=sys.stderr)
            return 3
        for r in fact_results:
            if r["status"] in ("unverified", "untrusted"):
                risk_note = f"（{r['risk']}领域）" if r["risk"] else ""
                if r["status"] == "untrusted":
                    detail = f"数据断言「{r['fact']}」仅检索到普通来源，可信度不足{risk_note}（普通网页可能为投毒/灌水来源）"
                    suggestion = "发布前通过权威渠道核验，或移除该断言"
                else:
                    detail = f"数据断言「{r['fact']}」无法核实{risk_note}：{r.get('reason', '未检索到来源')}"
                    suggestion = "发布前通过官方渠道核验，或在正文标注不确定性"
                gaps.append({
                    "type": "fact_unverified",
                    "severity": risk_severity(r["risk"]),
                    "detail": detail,
                    "suggestion": suggestion,
                })
        severity_order = {"high": 0, "medium": 1, "low": 2}
        gaps.sort(key=lambda g: severity_order.get(g["severity"], 9))

        out_dir.mkdir(parents=True, exist_ok=True)

        versions: dict[str, tuple[str, bool]] = {}
        if "ai" in platforms:
            versions["ai"] = generate_ai_version(
                doc, draft_score["dimensions"], query, llm=not args.no_llm
            )
        if "zhihu" in platforms:
            versions["zhihu"] = generate_zhihu_version(
                doc, draft_score["dimensions"], query, llm=not args.no_llm
            )
        if not versions:
            print("未生成任何版本。", file=sys.stderr)
            return 2

        postprocess_warnings: list[str] = []
        for name in list(versions):
            content, used = versions[name]
            cleaned, warns = _postprocess_llm_output(content, name)
            versions[name] = (cleaned, used)
            postprocess_warnings.extend(warns)
            # LLM 改写产物补扫营销信号：改写引入转化话术同样拒绝（exit 3）
            high_sigs = [
                s for s in detect_promotional_signals(cleaned)
                if s["severity"] == "high"
            ]
            if high_sigs:
                print("拒绝生成：LLM 改写输出含营销转化信号，不产出版本。", file=sys.stderr)
                for sig in high_sigs:
                    print(
                        f"  [拒绝] 疑似营销转化话术「{sig['pattern']}」：…{sig['context']}…",
                        file=sys.stderr,
                    )
                return 3

        checklist_path = build_review_checklist(
            doc, query, gaps, draft_score, versions, out_dir
        )
        manifest = build_manifest(
            doc, query, versions, draft_score, out_dir, gaps, checklist_path,
            postprocess_warnings, bottleneck_result,
        )
        manifest_path = save_manifest(manifest, out_dir)
    except OSError as exc:
        print(f"本地读写失败：{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"数据/配置异常：{exc}", file=sys.stderr)
        return 1

    print(f"草稿：{doc.title}（{len(doc.raw)} 字）")
    bn = manifest.get("bottleneck")
    if bn:
        print(f"瓶颈定位：{bn['layer_label']} —— {bn['reason']}")
        print(f"  方向：{bn['direction']}")
    print(f"草稿得分：{draft_score['overall']}/100 · {draft_score['grade']}（互动维度未发布）")
    for b in draft_score.get("blockers", []):
        print(f"  [阻断] {b}（总分已封顶）")
    for name in ("AI 可引用性", "内容质量 (E-E-A-T)", "关键词覆盖", "结构与格式"):
        print(f"  {name}: {draft_score['dimensions'][name]}/100")
    for name in versions:
        used = "LLM" if versions[name][1] else "规则脚手架"
        print(f"  {name} 版：{manifest['versions'][name]['file']}（{used}）")
    for warn in manifest["llm_postprocess_warnings"]:
        print(f"  [后处理] {warn}")
    print(f"行动建议：{len(draft_score['recommendations'])} 条（P0={sum(1 for r in draft_score['recommendations'] if r['priority'] == 'P0')}）")
    if gaps:
        high = sum(1 for g in gaps if g["severity"] == "high")
        print(f"素材缺口：{len(gaps)} 项（高危={high}）")
        for gap in gaps:
            print(f"  [{gap['severity']}] {gap['detail']}")
    print(f"发布前检查清单：{manifest['human_review']['checklist']}")
    print(f"已保存清单：{manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
