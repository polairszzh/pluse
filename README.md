# Pulse — AI Visibility Engine

**Pulse is an open-source AI visibility plugin for [Claude Code](https://claude.ai/claude-code) and [Codex](https://github.com/openai/codex).** It monitors how your brand and content perform across Chinese AI platforms (DeepSeek, Kimi, Doubao, Yuanbao) and social search (Zhihu, Xiaohongshu, Bilibili, Douyin). Every audit surfaces actionable recommendations with falsifiability checks — so you know not just what to fix, but how to verify it worked.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-blue)](https://claude.ai/claude-code)

> **Two engines, one system.** Pulse ships with both Claude Code (`SKILL.md`) and Codex (TOML agents) entry points. Same scripts, same references, same data — pick the runtime you prefer.

### Why Pulse

- **AI-search first, Chinese platform native.** Most SEO/GEO tools target Google and English content. Pulse targets the Chinese AI ecosystem: DeepSeek, Kimi, Doubao, and Yuanbao, plus the social platforms these models cite most (Zhihu, Xiaohongshu).
- **Built for individual creators, not enterprises.** No dashboard you have to log into. No sales call. Your data stays on your machine. You run it in your terminal, same as the rest of your dev workflow.
- **Falsifiable recommendations.** Every finding carries a "how would we know this failed?" check and a leading indicator. No vague advice — testable claims only.

### Who this is for

- **Independent developers and content creators** who publish on Zhihu, Xiaohongshu, or Bilibili and want their content cited by AI platforms.
- **Open-source project maintainers** who want to know whether DeepSeek and Kimi mention their project when users ask for tool recommendations.
- **Freelance writers and consultants** who need to benchmark their personal brand visibility against peers — without paying for an enterprise GEO subscription.

## Installation

```bash
git clone --depth 1 https://github.com/polairszzh/pluse.git
bash pluse/install.sh
```

The installer sets up Python dependencies and creates the local data directory. Nothing is installed globally.

```bash
# Check that everything is ready
/pulse doctor
```

## Quick Start

```bash
# Full audit of a Zhihu article
/pulse audit https://zhuanlan.zhihu.com/p/123456

# Track brand mentions across AI platforms
/pulse track <brand-name>

# Generate platform-adapted content from one source
/pulse adapt <topic> --source article.md --platforms zhihu,xiaohongshu,ai

# Compare your visibility against competitors
/pulse compare <brandA> <brandB>
```

## Commands

| Command | Description |
|---------|-------------|
| `/pulse setup` | Install Python dependencies and initialize the data directory |
| `/pulse doctor` | Check runtime readiness without changing anything |
| `/pulse audit <url>` | Deep single-article analysis: content quality, AI citability, keyword coverage, competitor comparison |
| `/pulse track <brand>` | Monitor brand mentions across AI platforms (DeepSeek, Kimi, Doubao, Yuanbao) |
| `/pulse adapt <topic>` | Generate platform-adapted content: Zhihu long-form, Xiaohongshu notes, AI-optimized passages |
| `/pulse compare <a> <b>` | Side-by-side visibility comparison: platform presence, AI citation rate, content gaps |
| `/pulse dashboard` | Start the local web dashboard on port 8766 |

## Features

### AI Search Monitoring

Track whether your brand is cited when users ask relevant questions on DeepSeek, Kimi, Doubao, and Yuanbao. Each monitoring snapshot records: whether you were cited, the exact context, sentiment (positive/neutral/negative), and which competitors were mentioned instead.

### Passage Citability Scoring

AI platforms cite content in self-contained, 130-170 word blocks. Pulse scores every paragraph in your article for citability — too short and it lacks context, too long and the AI skips it. The audit tells you exactly which paragraphs to rewrite and how.

### Content Quality Assessment (E-E-A-T adapted for Chinese platforms)

Experience signals: first-hand case studies, original screenshots, personal data. Expertise: topical depth and terminology accuracy. Authority: citation count and platform weight. Trustworthiness: source verifiability and disclosure. Weighted with Trust as the heaviest factor.

### Platform Content Adapter

One source article → three platform-optimized versions. Zhihu: 2000-4000 words, H2/H3 hierarchy, keyword-dense titles. Xiaohongshu: 300-800 words, short sentences, emoji, no external links. AI-optimized: 130-170 word self-contained passages, Q&A structure, high attribution density.

### Competitor Gap Analysis

Not just "you score 72, competitor scores 78." Pulse tells you exactly which topics your competitor covers that you don't, so you know where to direct your next piece of content.

### Local Web Dashboard

A private dashboard running on `localhost:8766`. Trend lines for AI citation rates over time, radar chart for platform presence, and a prioritized action queue sourced from your audit reports. All data stays on your machine.

## Compared to Enterprise GEO Tools

|  | Enterprise GEO SaaS | **Pulse** |
|---|---|---|
| **Target user** | Companies with marketing teams | Individual creators and developers |
| **Pricing** | ¥5k-50k/year subscription | **Free. MIT license.** |
| **Setup** | Sales call → onboarding → training | `git clone` + `bash install.sh` |
| **Data location** | Vendor cloud | **Your machine. Fully local.** |
| **AI platforms** | Enterprise dashboard view | Terminal-first, with optional local dashboard |
| **Lock-in** | High (data export friction) | **None. Your files, your data.** |
| **Chinese platform depth** | Generic across platforms | Zhihu/Xiaohongshu/Bilibili-specific rules encoded |
| **Falsifiability** | Rarely | **Every recommendation.** |

## Architecture

```
/pulse <command>
       │
       ▼
┌─────────────────────────┐
│  SKILL.md (Claude Code)  │
│  agents/*.toml (Codex)   │  ← Directive Layer
└──────────┬──────────────┘
           │
┌──────────▼──────────────┐
│  Orchestration Agents    │  ← Parallel dispatch, scoring, synthesis
└──────────┬──────────────┘
           │
┌──────────▼──────────────┐
│  scripts/                │  ← Execution Layer
│  fetch_page.py           │     Page fetching (trafilatura + htmldate)
│  fetch_zhihu.py          │     Zhihu-specific extraction
│  search_ai.py            │     AI platform search
│  content_adapter.py      │     Multi-platform content conversion
│  scorer.py               │     0-100 scoring engine
└──────────────────────────┘
```

Platform-specific knowledge (ranking factors, content formats, AI citation patterns) is encoded in `references/` markdown files — not hardcoded in Python. Community members can contribute new platform rules without touching the engine.

## Limitations

**Zhihu data access.** Zhihu's public API has rate limits and may require authentication for certain endpoints. Pulse uses a combination of public API and page fetching; aggressive use may trigger CAPTCHAs.

**AI platform coverage.** DeepSeek has a public API. Kimi and Doubao currently do not — Pulse uses search-engine inference as a fallback, which is less precise than direct API access.

**Xiaohongshu and Douyin.** These platforms have minimal public APIs. Full support is planned for Phase 3 and will require Playwright-based rendering.

## Roadmap

| Phase | What | When |
|-------|------|------|
| **Phase 1** | `/pulse audit` — Zhihu article analysis, content scoring, competitor comparison | In development |
| **Phase 2** | `/pulse track` — AI platform brand monitoring + local dashboard MVP | Planned |
| **Phase 3** | `/pulse adapt` — Multi-platform content adaptation (Zhihu, Xiaohongshu, AI-optimized) | Planned |

## Contributing

This project is maintained by [@polairszzh](https://github.com/polairszzh). Contributions, issues, and feature requests are welcome.

## License

MIT License. See [LICENSE](LICENSE) for details.
