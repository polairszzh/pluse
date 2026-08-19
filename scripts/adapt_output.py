"""adapt 输出后处理、发布清单与 manifest（从 content_adapter.py 拆分，行为不变）"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import search_ai
from adapt_draft import (
    CONTENT_FORMAT_PATH,
    DraftDoc,
    _content_format_updated,
    _load_content_format,
    _slug,
    _strip_frontmatter,
    _text_without_code,
)
from adapt_rewrite import _rewrite_triggers
from adapt_signals import _gap_block, _scan_facts

# AI 优化版用途标注：内部参考，非直接发布物
AI_PURPOSE_NOTE = (
    "<!-- AI 优化版 · 内部参考，非直接发布物：用于校验 AI 可引用性，"
    "及未来网站/多平台分发的可引用骨架。发布请用知乎版或自行加工。 -->\n\n"
)


def _postprocess_llm_output(content: str, version: str) -> tuple[str, list[str]]:
    """LLM 输出确定性后处理：AI 版去图、规整连续空行；返回 (content, warnings)"""
    warnings: list[str] = []
    text = _strip_frontmatter(content or "")  # LLM 若复制 frontmatter，剥掉
    if version == "ai":
        imgs = re.findall(r"!\[[^\]]*\]\([^)]+\)", text)
        if imgs:
            text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
            warnings.append(f"AI 版移除了 {len(imgs)} 处图片引用")
    text = _collapse_blank_lines(text)  # 跳过代码块压缩空行，避免破坏代码排版
    return text.strip(), warnings


def _collapse_blank_lines(text: str) -> str:
    """代码块外连续空行压缩为至多一个空行，代码块内原样保留"""
    out: list[str] = []
    in_code = False
    blank_run = 0
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if blank_run:
                out.append("")
            blank_run = 0
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        if not line.strip():
            blank_run += 1
            continue
        if blank_run:
            out.append("")
        blank_run = 0
        out.append(line)
    if blank_run:
        out.append("")
    return "\n".join(out)


def build_manifest(
    doc: DraftDoc,
    query: str | None,
    versions: dict[str, tuple[str, bool]],
    draft_score: dict,
    out_dir: Path,
    gaps: list[dict] | None = None,
    checklist_path: Path | None = None,
    postprocess_warnings: list[str] | None = None,
    bottleneck: dict | None = None,
) -> dict:
    """生成输出清单：每个版本带 falsifiability check"""
    gaps = gaps or []
    slug = _slug(doc.title)
    paths: dict[str, str] = {}
    for name, (content, used_llm) in versions.items():
        path = out_dir / f"{name}-{slug}.md"
        body = content.rstrip() + "\n\n" + _gap_block(gaps)
        if name == "ai":
            body = AI_PURPOSE_NOTE + body
        path.write_text(body, encoding="utf-8")
        paths[name] = str(path)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_title": doc.title,
        "source_chars": len(doc.raw),
        "query": query,
        "bottleneck": bottleneck,
        "draft_score": draft_score,
        "material_gaps": gaps,
        "llm_postprocess_warnings": postprocess_warnings or [],
        "content_format": {
            "file": str(CONTENT_FORMAT_PATH),
            "loaded": bool(_load_content_format()),
            "updated": _content_format_updated(),
        },
        "rewrite_triggers": _rewrite_triggers(draft_score["dimensions"]),
        "human_review": {
            "status": "pending",
            "checklist": str(checklist_path) if checklist_path else None,
            "note": "LLM 改写输出为待人工审阅的草稿；发布前按检查清单逐项确认。",
        },
        "versions": {
            name: {
                "file": paths[name],
                "used_llm": used_llm,
                "purpose": (
                    "publish（处理素材缺口后可直接发布）"
                    if name == "zhihu"
                    else "internal_reference（内部参考，非直接发布物）"
                ),
                "falsifiability_check": (
                    f"发布对应版本后，重跑 /pulse track --query {search_ai._shell_quote(query or doc.title)} "
                    f"--mine <文章URL>，AI 回答该话题时引用来源里出现你的内容"
                ),
            }
            for name, (_, used_llm) in versions.items()
        },
        "note": "输出为待发布的草稿，不是保证被引用的成品；互动维度需发布后重跑 audit 补全。",
    }


def build_review_checklist(
    doc: DraftDoc,
    query: str | None,
    gaps: list[dict],
    draft_score: dict,
    versions: dict[str, tuple[str, bool]],
    out_dir: Path,
) -> Path:
    """生成发布前人工审阅清单（LLM 改写版的人工介入入口）"""
    lines = [
        f"# 发布前检查清单 · {doc.title}",
        "",
        "> 本清单由 /pulse adapt 自动生成。LLM 改写输出是「待人工审阅的草稿」，发布前请逐项确认。",
        "",
        f"**目标话题**：{query or doc.title}",
        f"**草稿评分**：{draft_score['overall']}/100 · {draft_score['grade']}",
        "",
        "## 1. 素材缺口（发布前必须处理）",
        "",
    ]
    if draft_score.get("blockers"):
        idx = lines.index("## 1. 素材缺口（发布前必须处理）")
        lines[idx:idx] = (
            ["**阻断原因（一票封顶）**："]
            + [f"- {b}" for b in draft_score["blockers"]]
            + [""]
        )
    if gaps:
        lines.extend(
            f"- [ ] [{g['severity'].upper()}] {g['detail']} → {g['suggestion']}"
            for g in gaps
        )
    else:
        lines.append("- [x] 无自动检测到的素材缺口（仍建议通读全文）")
    lines += ["", "## 2. 事实核对（LLM 只改文风、不验证事实，请人工核实）", ""]
    facts = _scan_facts(_text_without_code(doc.body_text or doc.raw))
    if facts:
        lines.append("以下带单位的具体数字断言来自草稿，请与官网/官方文档核对：")
        lines.extend(f"- [ ] {fact}" for fact in facts)
    else:
        lines.append("- [ ] 未检测到带单位的数字断言，仍建议核对正文事实")
    lines += ["", "## 3. 版本用途", ""]
    for name, (_, used_llm) in versions.items():
        purpose = (
            "可发布物（处理完素材缺口后）"
            if name == "zhihu"
            else "内部参考，非直接发布物（用于校验与未来多平台骨架）"
        )
        engine = "LLM 改写" if used_llm else "规则脚手架"
        lines.append(f"- **{name} 版**（{engine}）：{purpose}")
    lines += [
        "",
        "## 4. 发布确认",
        "",
        "- [ ] 已处理全部素材缺口（替换占位图/核实链接/补齐缺失章节）",
        "- [ ] 已核对正文数字、链接与官方信息一致",
        "- [ ] 已通读 LLM 改写内容，确认无事实偏差与风格问题",
        "",
    ]
    path = out_dir / f"review-checklist-{_slug(doc.title)}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def save_manifest(manifest: dict, out_dir: Path) -> Path:
    ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    path = out_dir / f"adapt-{_slug(manifest['source_title'])}-{ts}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
