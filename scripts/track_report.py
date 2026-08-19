"""track 行动建议与报告渲染（从 search_ai.py 拆分，行为不变）"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from audit import Recommendation, rec_as_contract
from track_config import SNAPSHOT_DIR
from track_models import ProbeResult
from track_probe import PLATFORMS
from track_utils import _md_cell, _shell_quote, re_slug
from verifier import dedupe_recommendations, detect_conflicts, sort_recommendations

# --------------------------------------------------------------------------
# 行动建议（每条带 falsifiability check）
# --------------------------------------------------------------------------


def build_recommendations(
    query: str,
    results: list[ProbeResult],
    delta: dict | None = None,
) -> list[Recommendation]:
    """根据探测结果生成带验证方式的 P0/P1/P2 行动清单"""
    recs: list[Recommendation] = []

    deepseek = next((r for r in results if r.platform == "deepseek"), None)
    if deepseek and deepseek.status == "ok" and deepseek.cited is False:
        recs.append(Recommendation(
            priority="P0",
            dimension="AI 引用",
            action=f"「{query}」在 DeepSeek 回答中未被提及：在内容里补充一段 130-170 字的自包含品牌段落"
                   "（结论前置 + 具体数据/案例支撑）",
            expected_impact="提升 DeepSeek 检索命中",
            falsifiability_check=f"重跑 /pulse track --query {_shell_quote(query)}，DeepSeek 被提及变为「是」",
        ))
    if deepseek and deepseek.status == "no_key":
        recs.append(Recommendation(
            priority="P1",
            dimension="数据可用性",
            action="在 .env 配置 DEEPSEEK_API_KEY 后重跑，才能拿到 DeepSeek 真实引用判断",
            expected_impact="补齐真实 API 探测",
            falsifiability_check="重跑后 DeepSeek 状态不再是「未配置密钥」",
        ))

    mine_checked = any(r.mine_ids for r in results)
    if mine_checked:
        if deepseek and deepseek.status == "ok" and deepseek.cited is True and deepseek.mine_cited is False:
            mine_txt = "、".join(deepseek.mine_ids[:3])
            recs.append(Recommendation(
                priority="P0",
                dimension="内容引用归属",
                action=f"「{query}」在 DeepSeek 回答中被提及，但你的内容（{mine_txt}）不在其中："
                       "围绕该话题发布/优化一篇自包含教程（每个 H2 一个问答对，首段 130-170 字直接给答案，"
                       "带具体数据/案例），确保标题覆盖话题关键词",
                expected_impact="让 AI 回答该话题时引用你的内容而不是别人的",
                falsifiability_check=f"重跑 /pulse track --query {_shell_quote(query)} "
                                     f"--mine {_shell_quote(deepseek.mine_ids[0])}，"
                                     "DeepSeek 我的内容变为「是」",
            ))
        inference_missing = [
            r for r in results
            if r.source == "search_inference" and r.status == "ok"
            and r.cited is True and r.mine_cited is False
        ]
        if inference_missing:
            names = "、".join(PLATFORMS[r.platform]["label"] for r in inference_missing[:3])
            mine_example = next((r.mine_ids[0] for r in inference_missing if r.mine_ids), "")
            recs.append(Recommendation(
                priority="P1",
                dimension="内容收录",
                action=f"{names} 对应话题在搜索生态中有内容，但你的内容不在其中："
                       "确保文章已在知乎等平台发布并被搜索收录（标题 + 首段覆盖话题关键词，正文带自包含答案块）",
                expected_impact="让话题搜索结果里出现你的内容",
                falsifiability_check=f"重跑 /pulse track --query {_shell_quote(query)} "
                                     f"--mine {_shell_quote(mine_example)}，"
                                     "搜索推断平台我的内容至少一个变为「是」",
            ))

    # B3 引用质量：lostprompt（竞品夺走）与 factcheck（未核实断言）
    delta = delta or {"platforms": {}}
    replaced = [
        (platform, item)
        for platform, item in delta["platforms"].items()
        if item.get("competitor_replaced")
    ]
    confirmed_replaced = [
        (p, item) for p, item in replaced if item.get("competitor_replaced_confirmed")
    ]
    inferred_replaced = [
        (p, item) for p, item in replaced if not item.get("competitor_replaced_confirmed")
    ]
    if confirmed_replaced:
        names = "、".join(PLATFORMS.get(p, {}).get("label", p) for p, _ in confirmed_replaced)
        mine_args = next(
            (" ".join(f"--mine {_shell_quote(m)}" for m in r.mine_ids) for r in results if r.mine_ids),
            "--mine <你的内容URL>",
        )
        comp_args = next(
            (
                " ".join(f"--competitor {_shell_quote(c)}" for c in r.competitor_ids)
                for r in results if r.competitor_ids
            ),
            "--competitor <竞品标识>",
        )
        recs.append(Recommendation(
            priority="P1",
            dimension="竞品夺走",
            action=f"在 {names} 上，你的内容上次被引用、本次被竞品替换："
                   "该判定基于单次对比样本（AI 回答有随机性），建议先重跑一次确认；"
                   "确属夺走则围绕差异化优势补充独家数据/实测/案例，并在标题与首段强化品牌锚定",
            expected_impact="确认后把 AI 引用从竞品拉回你的内容",
            falsifiability_check=f"重跑 /pulse track --query {_shell_quote(query)} "
                                 f"{mine_args} {comp_args}，"
                                 "对应平台「竞品夺走」风险消失、我的内容变为「是」",
        ))
    if inferred_replaced:
        names = "、".join(PLATFORMS.get(p, {}).get("label", p) for p, _ in inferred_replaced)
        mine_args = next(
            (" ".join(f"--mine {_shell_quote(m)}" for m in r.mine_ids) for r in results if r.mine_ids),
            "--mine <你的内容URL>",
        )
        comp_args = next(
            (
                " ".join(f"--competitor {_shell_quote(c)}" for c in r.competitor_ids)
                for r in results if r.competitor_ids
            ),
            "--competitor <竞品标识>",
        )
        recs.append(Recommendation(
            priority="P1",
            dimension="竞品夺走（推断）",
            action=f"在 {names} 上，你的内容上次被引用、本次未被引用且检出竞品——"
                   "因上次未检查竞品，该判定为推断，请先人工确认竞品是否新出现："
                   "确属夺走则补充差异化内容强化品牌锚定",
            expected_impact="确认是否为真实竞品夺走，避免误判后浪费优化动作",
            falsifiability_check=f"重跑 /pulse track --query {_shell_quote(query)} "
                                 f"{mine_args} {comp_args}，"
                                 "连续两次检查后「竞品夺走」转为已确认或消失",
        ))
    risk_results = [
        r for r in results
        if r.status == "ok" and r.fact_risks
    ]
    if risk_results:
        risks = "、".join(
            f"{PLATFORMS[r.platform]['label']}：{'、'.join(r.fact_risks[:3])}"
            for r in risk_results[:3]
        )
        recs.append(Recommendation(
            priority="P1",
            dimension="信息风险",
            action=f"AI 回答中出现未核实断言（{risks}）：人工复核数字/版本真实性，"
                   "若与事实不符，准备纠偏内容或联系平台反馈",
            expected_impact="防止错误信息随 AI 回答扩散",
            falsifiability_check="重跑 /pulse track 后回答中的断言经人工核实一致，或已确认平台修正",
        ))

    failed = [r for r in results if r.status == "error"]
    if failed:
        names = "、".join(PLATFORMS[r.platform]["label"] for r in failed)
        recs.append(Recommendation(
            priority="P1",
            dimension="数据可用性",
            action=f"{names} 探测失败：检查网络或反爬拦截后重跑",
            expected_impact="补齐缺失平台的数据",
            falsifiability_check="重跑后失败平台状态恢复为「正常」",
        ))

    negative = [r for r in results if r.status == "ok" and r.sentiment == "negative"]
    if negative:
        names = "、".join(PLATFORMS[r.platform]["label"] for r in negative)
        recs.append(Recommendation(
            priority="P0",
            dimension="舆情",
            action=f"在 {names} 检测到负面提及：定位并核查负面内容来源，准备回应或补充正面材料",
            expected_impact="控制负面信号扩散",
            falsifiability_check="重跑 /pulse track，负面情感平台转为中性或正面",
        ))

    cited_platforms = [r for r in results if r.status == "ok" and r.cited is True]
    if cited_platforms and not any(r.sentiment == "negative" for r in cited_platforms):
        recs.append(Recommendation(
            priority="P2",
            dimension="持续监测",
            action="保持现有内容更新频率，两周后重跑对比引用变化",
            expected_impact="确认引用趋势稳定",
            falsifiability_check="两周后重跑，被提及平台数量不下降",
        ))

    recs = sort_recommendations(dedupe_recommendations(recs))
    for a, b, note in detect_conflicts(recs):
        print(f"  [冲突提示] {note}（矛盾建议保留，发布前人工复核取舍）", file=sys.stderr)
    return recs


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


STATUS_LABEL = {"ok": "正常", "no_key": "未配置密钥", "error": "失败"}
SENTIMENT_LABEL = {"positive": "正面", "neutral": "中性", "negative": "负面"}
CONFIDENCE_LABEL = {"confirmed": "Confirmed", "likely": "Likely", "hypothesis": "Hypothesis"}


def render_markdown(
    query: str,
    results: list[ProbeResult],
    trend: dict,
    recommendations: list[Recommendation],
    delta: dict | None = None,
) -> str:
    lines = ["# AI 平台引用跟踪报告", ""]
    lines.append(f"- **监测对象**：{query}")
    lines.append(f"- **运行时间**：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}")
    requested = [r.platform for r in results]
    lines.append(f"- **平台**：{'、'.join(PLATFORMS[p]['label'] for p in requested)}")
    lines.append("")

    lines.append("## 本次快照")
    lines.append("")
    with_mine = any(r.mine_ids for r in results)
    if with_mine:
        lines.append("| 平台 | 状态 | 置信度 | 被提及 | 我的内容 | 情感 | 上下文 / 说明 |")
        lines.append("|---|---|---|---|---|---|---|")
    else:
        lines.append("| 平台 | 状态 | 置信度 | 被提及 | 情感 | 上下文 / 说明 |")
        lines.append("|---|---|---|---|---|---|")
    for r in results:
        if r.status != "ok":
            # 整体失败/未配置：不展示部分样本的命中结果，避免「失败 + 被提及 是」矛盾
            cited_txt = "未知"
        elif r.sample_count > 1 and r.prob is not None:
            hits = r.meta.get("sample_hits", 0)
            invalid = r.meta.get("sample_invalid", 0)
            invalid_note = f"，{invalid} 次无效" if invalid else ""
            ci_note = (
                f"，CI {r.ci_low:.0%}-{r.ci_high:.0%}"
                if r.ci_low is not None and r.ci_high is not None else ""
            )
            cited_txt = (
                f"{'是' if r.cited else '否'} "
                f"({r.prob:.0%}, {hits}/{r.sample_count}{invalid_note}{ci_note})"
            )
        else:
            cited_txt = {True: "是", False: "否", None: "未知"}.get(r.cited, "未知")
        mine_txt = {True: "是", False: "否", None: "—"}.get(r.mine_cited, "—")
        if r.mine_cited is True:
            if r.cited_type == "earned":
                mine_txt += "（原创）"
            elif r.cited_type == "owned":
                mine_txt += "（转载）"
            else:
                mine_txt += "（未知）"
        sentiment = SENTIMENT_LABEL.get(r.sentiment or "", "—")
        conf = CONFIDENCE_LABEL.get(r.confidence or "", "—")
        if r.error:
            context = _md_cell(f"{r.context}（{r.error}）")
        else:
            context = _md_cell(r.context)
        row = (
            f"| {PLATFORMS[r.platform]['label']} | {STATUS_LABEL.get(r.status, r.status)} "
            f"| {conf} | {cited_txt} "
        )
        if with_mine:
            row += f"| {mine_txt} "
        row += f"| {sentiment} | {context} |"
        lines.append(row)

    lines.append("")
    delta = delta or {"platforms": {}}
    if delta.get("platforms"):
        # 仅渲染有 note 或任一对比键的行，避免外部传入空壳条目时出现全「—」行
        rows = [
            (platform, item)
            for platform, item in delta["platforms"].items()
            if item.get("note")
            or any(
                k in item
                for k in ("cited_change", "sentiment_flip", "mine_change", "competitor_replaced")
            )
        ]
        if rows:
            lines.append("## 与上次对比")
            lines.append("")
            lines.append("| 平台 | 引用变化 | 情感变化 | 我的内容 |")
            lines.append("|---|---|---|---|")
            cited_label = {"added": "新增被提及", "lost": "丢失被提及", "same": "无变化"}
            mine_label = {"gained": "新增被引用", "lost": "丢失被引用"}
            for platform, item in rows:
                label = PLATFORMS.get(platform, {}).get("label", platform)
                if item.get("note"):
                    lines.append(f"| {label} | {item['note']} | — | — |")
                    continue
                cited = cited_label.get(item.get("cited_change"), "—")
                flip = item.get("sentiment_flip", "—")
                mine = mine_label.get(item.get("mine_change"), "—")
                if item.get("competitor_replaced"):
                    mine = "丢失被引用（竞品夺走）"
                lines.append(f"| {label} | {cited} | {flip} | {mine} |")
            lines.append("")

    lines.append("## 趋势对比")
    lines.append("")
    if not trend["series"]:
        lines.append("暂无历史数据，本次为首个快照。")
    else:
        for platform in requested:
            points = trend["series"].get(platform, [])
            label = PLATFORMS[platform]["label"]
            if len(points) < 2:
                lines.append(f"- **{label}**：{len(points)} 次快照，重跑一次后生成趋势。")
                continue
            def point_txt(p: dict) -> str:
                invalid_note = f"（{p['invalid']} 次无效）" if p.get("invalid") else ""
                if p.get("status") != "ok":
                    # 整体失败/未配置：显示未知，不按部分有效样本的概率渲染是/否
                    return "未知" + invalid_note
                if p.get("n", 1) > 1 and p.get("prob") is not None:
                    return (
                        f"{'是' if p['cited'] else '否'} "
                        f"({p['prob']:.0%}, {p['hits']}/{p['n']}){invalid_note}"
                    )
                base = "是" if p["cited"] else "否" if p["cited"] is False else "未知"
                return base + invalid_note

            states = " → ".join(point_txt(p) for p in points)
            line = f"- **{label}**（{len(points)} 次）：{states}"
            # 只要历史里有任何一次检查过 mine，就按 mine_ids 分组展示：
            # 不同次用不同标识时不会显示成假回归；未检查的运行显式标次数
            if any(p.get("mine_checked") for p in points):
                groups: dict[tuple, list] = {}
                for idx, p in enumerate(points, 1):
                    key = tuple(sorted(p.get("mine_ids") or []))
                    groups.setdefault(key, []).append((idx, p))
                multi_group = len(groups) > 1
                for key, group in groups.items():
                    states = " → ".join(
                        ("是" if p["mine_cited"] else "否" if p["mine_cited"] is False else "未知")
                        + (f"（第{idx}次）" if multi_group else "")
                        for idx, p in group
                    )
                    if key:
                        line += f"；我的内容({'、'.join(key)})：{states}"
                    else:
                        positions = "、".join(str(idx) for idx, _ in group)
                        line += f"；未检查 {len(group)} 次" + (f"（第{positions}次）" if multi_group else "")
            lines.append(line)
        if trend["changes"]:
            lines.append("")
            lines.append("**引用状态变化点**：")
            for ch in trend["changes"]:
                if ch["platform"] not in requested:
                    # 只展示本次实际运行平台的变化点，避免混入未运行平台的旧历史
                    continue
                label = PLATFORMS[ch["platform"]]["label"]
                lines.append(
                    f"- {label} 在 {ch['run_at']} 由「{'是' if ch['from'] else '否'}」"
                    f"变为「{'是' if ch['to'] else '否'}」"
                )
    lines.append("")

    # B3 风险提示：lostprompt（竞品夺走）与未核实断言（factcheck）
    risk_lines: list[str] = []
    for platform, item in (delta or {"platforms": {}})["platforms"].items():
        if item.get("competitor_replaced"):
            label = PLATFORMS.get(platform, {}).get("label", platform)
            suffix = (
                ""
                if item.get("competitor_replaced_confirmed")
                else "（推断：上次未检查竞品或前后竞品标识不一致，待人工确认）"
            )
            risk_lines.append(
                f"- ⚠ **{label}**：上次被引用，本次被竞品替换{suffix}"
                f"（{item.get('competitor_replaced_at', '')}），建议补充差异化内容强化品牌锚定"
            )
    for r in results:
        if r.fact_risks:
            label = PLATFORMS[r.platform]["label"]
            risk_lines.append(
                f"- ⚠ **{label}** 回答中出现未核实断言：{'、'.join(r.fact_risks)}，"
                "建议人工复核后准备纠偏内容"
            )
    if risk_lines:
        lines.append("## 风险提示")
        lines.append("")
        lines.extend(risk_lines)
        lines.append("")

    lines.append("## 行动清单（每条都带验证方式）")
    lines.append("")
    if not recommendations:
        lines.append("当前没有需要优先处理的事项。")
    for priority in ("P0", "P1", "P2"):
        bucket = [r for r in recommendations if r.priority == priority]
        if not bucket:
            continue
        label = {"P0": "立即处理", "P1": "优先处理", "P2": "顺手优化"}[priority]
        lines.append(f"### {priority} · {label}")
        lines.append("")
        for i, rec in enumerate(bucket, 1):
            lines.append(f"{i}. **[ {_md_cell(rec.dimension)} ]** {_md_cell(rec.action)}")
            lines.append(f"   - 预期效果：{_md_cell(rec.expected_impact)}")
            lines.append(f"   - 验证方式：{_md_cell(rec.falsifiability_check)}")
        lines.append("")

    lines.append("## 数据说明")
    lines.append("")
    lines.append("- DeepSeek：真实 API 探测，被提及 = 回答正文出现品牌名（精确匹配），原始回答可在 JSON 快照的 meta.answer 复核。")
    lines.append("- 默认每平台采样 5 次（--samples 可调，1 为单次判定）：被提及 = 多数样本命中，概率 = 命中数/样本数，"
                 "置信区间为 Wilson 95% 区间；单次采样时显示是/否。")
    lines.append("- Kimi / 豆包 / 元宝：无公开 API，使用 Bing 搜索结果推断检索库中的存在信号，**不等同于该平台真实引用**。")
    lines.append("- Kimi / 豆包 / 元宝 各自用 Bing 对同一查询词做搜索推断（结果通常相同），是检索库存在信号，不代表各平台各自的真实引用。")
    lines.append("- 传 --mine <你的内容标识>（URL/标题/作者名，可重复传多次，一次一个）时，额外判断 AI 回答/Bing 结果里是否出现你的内容；"
                 "URL 在搜索推断里更有效，标题/作者名在 AI 回答里更常见。")
    lines.append("- --mine-owned 传转载/自有渠道标识：仅命中 owned 且未命中任何原创标识时记为「转载（owned）」；"
                 "命中任一原创标识（--mine）即按更高价值口径记为「原创（earned）」。")
    lines.append("- --competitor 传竞品标识，用于 lostprompt（竞品夺走）分析："
                 "上次被引用、本次被竞品替换且话题仍被提及时会标出风险。")
    lines.append("- 「风险提示」中的未核实断言来自 AI 回答原文的数字/版本提取，只做风险提示不做事实判定，需人工复核。")
    lines.append("- URL 标识匹配规则：域名大小写不敏感、路径大小写敏感（URL 路径区分大小写）；标题/作者名不区分大小写。")
    lines.append("- 行动清单里的重跑命令为 POSIX shell 风格；PowerShell 可直接使用，但标识含英文单引号时"
                 "（shlex 会转义为 '\\''），需在 PowerShell 手动调整或改用 bash。")
    lines.append("- 每次运行写入 data/monitor.db，趋势来自同品牌的历史快照对比。")
    lines.append("")
    return "\n".join(lines)


def render_json(
    query: str,
    results: list[ProbeResult],
    trend: dict,
    recommendations: list[Recommendation],
    delta: dict | None = None,
) -> dict:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "query": query,
        "results": [r.__dict__ for r in results],
        "trend": trend,
        "delta": delta or {"platforms": {}, "has_history": False},
        "recommendations": [rec_as_contract(r) for r in recommendations],
        "source_note": (
            "DeepSeek 为真实 API 探测；Kimi/豆包/元宝 为搜索引擎存在信号推断，不等同于真实引用"
        ),
    }


def save_report(
    query: str,
    results: list[ProbeResult],
    trend: dict,
    recommendations: list[Recommendation],
    out_dir: Path | None = None,
    delta: dict | None = None,
) -> list[Path]:
    out_dir = out_dir or SNAPSHOT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    slug = re_slug(query)
    md_path = out_dir / f"track-{slug}-{ts}.md"
    json_path = out_dir / f"track-{slug}-{ts}.json"
    md_path.write_text(render_markdown(query, results, trend, recommendations, delta), encoding="utf-8")
    json_path.write_text(
        json.dumps(render_json(query, results, trend, recommendations, delta), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return [md_path, json_path]
