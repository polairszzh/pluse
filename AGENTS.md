# Pulse Project Instructions (Codex)

Pulse is an AI visibility engine for the Chinese internet. It monitors brand/content presence across Chinese AI platforms (DeepSeek, Kimi, Doubao, Yuanbao) and social search (Zhihu, Xiaohongshu, Bilibili, Douyin).

## Project Memory

This project is connected to a Loci brain. On session start, read `.loci/memory.md` for current state and active context.

## Architecture

- `skills/visibility/SKILL.md` — main orchestrator for Claude Code
- `agents/` — TOML agents for Codex (`*.toml`) and Markdown agents for Claude Code (`*.md`)
- `scripts/` — Python execution layer (fetching, scoring, content adaptation)
- `references/` — platform knowledge encoded as markdown (Zhihu algorithm, AI platform patterns, content format rules)
- `dashboard/` — local web dashboard (Express + Chart.js, port 8766)
- `data/` — local storage (audit snapshots, monitoring DB)

## Key Design Principles

1. **Platform knowledge lives in `references/`, not in code.**
2. **Every recommendation carries a falsifiability check.**
3. **Dual-engine from day one.** Claude Code (SKILL.md) and Codex (TOML agents) share scripts and references.
4. **Personal creator first, enterprise later.**

## Development Practices

- Python scripts use standard library + requests + trafilatura + htmldate.
- Dashboard is vanilla HTML/CSS/JS + Chart.js.
- Tests in `tests/`, run with `pytest`.