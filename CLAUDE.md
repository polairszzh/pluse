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