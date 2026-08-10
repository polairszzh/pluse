# Pulse Project Instructions

Pulse is an AI visibility engine for the Chinese internet. It monitors brand/content presence across Chinese AI platforms (DeepSeek, Kimi, Doubao, Yuanbao) and social search (Zhihu, Xiaohongshu, Bilibili, Douyin).

## Project Memory

This project is connected to a Loci brain. On session start, read `.loci/memory.md` for current state and active context.

## Architecture

- `skills/visibility/SKILL.md` — main orchestrator for the `/pulse` command
- `agents/` — Claude Code agents (`*.md`) and Codex agents (`*.toml`)
- `scripts/` — Python execution layer (fetching, scoring, content adaptation)
- `references/` — platform knowledge encoded as markdown (Zhihu algorithm, AI platform patterns, content format rules)
- `dashboard/` — local web dashboard (Express + Chart.js, port 8766)
- `data/` — local storage (audit snapshots, monitoring DB)

## Key Design Principles

1. **Platform knowledge lives in `references/`, not in code.** Update a markdown file when a platform changes its algorithm.
2. **Every recommendation carries a falsifiability check.** No vague advice.
3. **Dual-engine from day one.** Claude Code (`SKILL.md`) and Codex (TOML agents) share the same scripts and references.
4. **Personal creator first, enterprise later.** The default experience is for a single individual tracking their own content.

## Development Practices

- Python scripts use standard library + requests + trafilatura + htmldate. No heavy frameworks.
- Dashboard is vanilla HTML/CSS/JS + Chart.js. No React, no build step.
- Tests live in `tests/`. Run with `pytest`.
- Keep the install footprint small — `pip install -r requirements.txt` should finish in under 30 seconds.

## Review 与提交规范（防止反复修改）

1. **提交/推送前必须自查本次改动**，尤其是新写逻辑的边界情况：子串匹配要测前缀/边界、
   URL 匹配要测主机/路径边界、空值/None、重复输入。自查 ≠ 跑通测试——要主动想
   「这个新逻辑在什么输入下会出错」并补上对应测试。
2. **AI review 是参考，不是命令**：处理每条建议前先写结论「接受 / 拒绝 + 理由」再动手——接受的须确认问题真实存在且改动必要（评估兼容/成本），拒绝的须附证据回帖；禁止把 review 当 to-do list 逐条照做。误读/幻觉 → 回帖附证据，不改代码。
3. **修复也可能引入新 bug**：每次修复后，对被改动的逻辑重新做一次边界自查
   （教训：曾因「修 URL 大小写」引入前缀误匹配的 critical）。
4. **停止边界（suggestions 不是 todo list）**：
   - critical + 数据正确性问题 → 必须处理；
   - 风格/可读性/锦上添花类 suggestion → 记入项目 `.loci/todo.json`，不逐轮纠缠；
   - 合入条件：测试全绿 + critical 清零 + 数据正确性问题清零。LGTM 是参考闸门，不是唯一闸门。
5. **收敛信号**：连续两轮只剩风格类建议时，按「边际收益趋零」停止并合入，不再追下一轮。
