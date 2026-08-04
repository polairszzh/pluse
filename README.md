<div align="center">

**<kbd>&nbsp;EN&nbsp;</kbd> · <kbd>&nbsp;中文&nbsp;</kbd>**

</div>

---

## EN

# Pulse — AI Visibility Engine

**Pulse is an open-source AI visibility plugin for [Claude Code](https://claude.ai/claude-code) and [Codex](https://github.com/openai/codex).** It monitors how your brand and content perform across Chinese AI platforms (DeepSeek, Kimi, Doubao, Yuanbao) and social search (Zhihu, Xiaohongshu, Bilibili, Douyin). Every audit surfaces actionable recommendations with falsifiability checks.

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
- **Freelance writers and consultants** who need to benchmark their personal brand visibility against peers without paying for an enterprise GEO subscription.

## Installation

```bash
git clone --depth 1 https://github.com/polairszzh/pluse.git
bash pluse/install.sh
```

```bash
/pulse doctor
```

## Quick Start

```bash
/pulse audit https://zhuanlan.zhihu.com/p/123456
/pulse track <brand-name>
/pulse adapt <topic> --source article.md --platforms zhihu,xiaohongshu,ai
/pulse compare <brandA> <brandB>
```

## Commands

| Command | Description |
|---------|-------------|
| `/pulse setup` | Install Python dependencies and initialize the data directory |
| `/pulse doctor` | Check runtime readiness without changing anything |
| `/pulse audit <url>` | Deep single-article analysis: content quality, AI citability, keyword coverage, competitor comparison |
| `/pulse track <brand>` | Monitor brand mentions across AI platforms (DeepSeek, Kimi, Doubao, Yuanbao) |
| `/pulse adapt <topic>` | Generate platform-adapted content from one source |
| `/pulse compare <a> <b>` | Side-by-side visibility comparison |
| `/pulse dashboard` | Start the local web dashboard on port 8766 |

## Features

**AI Search Monitoring** — Track whether your brand is cited on DeepSeek, Kimi, Doubao, and Yuanbao. Each snapshot records: cited or not, exact context, sentiment, and which competitors were mentioned instead.

**Passage Citability Scoring** — AI platforms cite content in 130-170 word self-contained blocks. Pulse scores every paragraph: too short lacks context, too long gets skipped. The report tells you exactly which paragraphs to fix.

**Content Quality (E-E-A-T adapted)** — Experience: first-hand cases, original data. Expertise: depth and accuracy. Authority: citations and platform weight. Trustworthiness: source verifiability (heaviest weight).

**Platform Content Adapter** — One article → three versions. Zhihu: 2000-4000 words, H2/H3 hierarchy. Xiaohongshu: 300-800 words, short sentences, emoji. AI-optimized: 130-170 word passages, Q&A structure.

**Competitor Gap Analysis** — Not just scores. Pulse shows exactly which topics competitors cover that you don't.

**Local Dashboard** — `localhost:8766`. Trend lines, radar charts, action queue. All data stays local.

## Compared to Enterprise GEO Tools

|  | Enterprise GEO SaaS | **Pulse** |
|---|---|---|
| **Target user** | Companies with marketing teams | Individual creators and developers |
| **Pricing** | ¥5k-50k/year subscription | **Free. MIT license.** |
| **Setup** | Sales call → onboarding → training | `git clone` + `bash install.sh` |
| **Data location** | Vendor cloud | **Your machine. Fully local.** |
| **Lock-in** | High | **None. Your files, your data.** |
| **Platform depth** | Generic | Zhihu/Xiaohongshu/Bilibili-specific rules |
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
│  fetch_page.py / fetch_zhihu.py / search_ai.py
│  content_adapter.py / scorer.py
└──────────────────────────┘
```

Platform knowledge lives in `references/` markdown files, not hardcoded in Python. Community members can contribute new platform rules without touching the engine.

## Limitations

**Zhihu data access.** Public API has rate limits. Pulse uses public API + page fetching; aggressive use may trigger CAPTCHAs.

**AI platform coverage.** DeepSeek has a public API. Kimi and Doubao do not — Pulse uses search-engine inference as fallback.

**Xiaohongshu and Douyin.** Minimal public APIs. Full support planned for Phase 3 (Playwright-based).

## Roadmap

| Phase | What | Status |
|-------|------|--------|
| **Phase 1** | `/pulse audit` — Zhihu article analysis, scoring, competitor comparison | In development |
| **Phase 2** | `/pulse track` — AI platform monitoring + local dashboard MVP | Planned |
| **Phase 3** | `/pulse adapt` — Multi-platform content adaptation | Planned |

## Contributing

Maintained by [@polairszzh](https://github.com/polairszzh). Issues, PRs, and feature requests welcome.

## License

MIT License. See [LICENSE](LICENSE).

---

## 中文

# Pulse — AI 可见度引擎

**Pulse 是一个面向 [Claude Code](https://claude.ai/claude-code) 和 [Codex](https://github.com/openai/codex) 的开源 AI 可见度插件。** 监控你的品牌和内容在中文 AI 平台（DeepSeek、Kimi、豆包、元宝）和社交搜索（知乎、小红书、B站、抖音）中的表现。每次审计都给出可验证的行动建议——不仅告诉你改什么，还告诉你改完之后怎么验证效果。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-blue)](https://claude.ai/claude-code)

> **双引擎，一套系统。** 同时提供 Claude Code（SKILL.md）和 Codex（TOML agent）入口。同一套脚本、同一套知识库、同一份数据——喜欢哪个运行时就用哪个。

### 为什么选 Pulse

- **AI 搜索优先，中文平台原生。** 市面上大多数 SEO/GEO 工具只针对 Google 和英文内容。Pulse 专攻中文 AI 生态：DeepSeek、Kimi、豆包、元宝，以及这些模型引用最多的社交平台。
- **为个人创作者设计，不是为企业。** 没有后台登录、没有销售电话。数据留在你的机器上，在终端里跑，和其他开发工具一样。
- **每条建议都可验证。** 每个发现都带有"怎么知道这条建议没效果？"的检查项。不说空话，只说能被检验的事。

### 谁适合用

- **独立开发者和内容创作者** — 在知乎、小红书、B站上发内容，想知道自己的文章有没有被 AI 引用
- **开源项目维护者** — 想知道 DeepSeek 和 Kimi 在推荐工具时有没有提到自己的项目
- **自由职业写手和顾问** — 想知道自己个人品牌在 AI 上的曝光情况，对比同行差多少

## 安装

```bash
git clone --depth 1 https://github.com/polairszzh/pluse.git
bash pluse/install.sh
```

安装脚本配置 Python 依赖并创建本地数据目录，不在系统全局安装任何东西。

```bash
/pulse doctor
```

## 快速开始

```bash
/pulse audit https://zhuanlan.zhihu.com/p/123456
/pulse track <品牌名>
/pulse adapt <主题> --source article.md --platforms zhihu,xiaohongshu,ai
/pulse compare <品牌A> <品牌B>
```

## 命令一览

| 命令 | 说明 |
|------|------|
| `/pulse setup` | 安装 Python 依赖，初始化数据目录 |
| `/pulse doctor` | 检查运行环境（不做任何修改） |
| `/pulse audit <url>` | 单篇文章深度分析：内容质量、AI 可引用性、关键词覆盖、竞品对比 |
| `/pulse track <品牌>` | 监控品牌在 AI 平台上的引用情况 |
| `/pulse adapt <主题>` | 生成多平台适配内容：知乎长文、小红书笔记、AI 优化版 |
| `/pulse compare <A> <B>` | 品牌可见度横向对比 |
| `/pulse dashboard` | 启动本地 Web 仪表盘（端口 8766） |

## 功能亮点

**AI 搜索引用监控** — 在 DeepSeek、Kimi、豆包、元宝上用核心关键词提问，追踪品牌是否被引用。每次记录：是否被引用、具体上下文、情感倾向、替代你被提到的是谁。

**Passage Citability 评分** — AI 偏好引用 130-170 字的自包含内容块。Pulse 逐段评分：太短缺上下文，太长 AI 跳过。报告精确指出哪几段要改、怎么改。

**内容质量评估（E-E-A-T 中文改编）** — 经验：第一手案例、原创数据。专业度：话题深度、术语准确性。权威性：被引次数、平台权重。可信度：来源可验证（权重最高）。

**多平台内容适配** — 一篇原稿 → 三个版本。知乎：2000-4000 字，H2/H3 层级。小红书：300-800 字，短句 emoji。AI 优化版：130-170 字自包含段落，问答结构。

**竞品差距分析** — 不只比分数。Pulse 精确指出竞品覆盖了哪些话题而你没有——让你知道下一篇该写什么。

**本地仪表盘** — `localhost:8766`。趋势折线图、雷达图、行动队列。所有数据留在本地。

## 与企业 GEO 工具对比

|  | 企业 GEO SaaS | **Pulse** |
|---|---|---|
| **目标用户** | 有营销团队的公司 | 个人创作者和开发者 |
| **价格** | 每年 ¥5k-50k 订阅 | **免费，MIT 协议** |
| **上手** | 销售电话 → 部署培训 → 使用 | `git clone` + `bash install.sh` |
| **数据位置** | 厂商云端 | **你的机器，完全本地** |
| **锁定风险** | 高 | **零。你的文件，你的数据。** |
| **平台深度** | 各平台通用 | 知乎/小红书/B站 专属规则 |
| **可验证性** | 很少 | **每条建议都有** |

## 架构

```
/pulse <命令>
       │
       ▼
┌─────────────────────────┐
│  SKILL.md (Claude Code)  │
│  agents/*.toml (Codex)   │  ← 指令层
└──────────┬──────────────┘
           │
┌──────────▼──────────────┐
│  编排 Agent（并行调度）   │  ← 编排层
└──────────┬──────────────┘
           │
┌──────────▼──────────────┐
│  scripts/                │  ← 执行层
│  fetch_page.py / fetch_zhihu.py / search_ai.py
│  content_adapter.py / scorer.py
└──────────────────────────┘
```

平台专属知识（排名因素、内容格式、AI 引用模式）编码在 `references/` 的 Markdown 文件中，不写在代码里。社区可以贡献新平台规则，不用动引擎。

## 局限性

**知乎数据获取** — 公开 API 有频率限制。Pulse 使用公开 API 加页面抓取的组合方案；高频使用可能触发验证码。

**AI 平台覆盖** — DeepSeek 有公开 API。Kimi 和豆包目前没有，Pulse 使用搜索引擎推断作为降级方案。

**小红书和抖音** — 几乎没有公开 API，完整支持计划在 Phase 3 实现（需要 Playwright 浏览器渲染）。

## 路线图

| 阶段 | 内容 | 状态 |
|------|------|------|
| **Phase 1** | `/pulse audit` — 知乎文章分析、内容评分、竞品对比 | 开发中 |
| **Phase 2** | `/pulse track` — AI 平台品牌监控 + 本地仪表盘 MVP | 计划中 |
| **Phase 3** | `/pulse adapt` — 多平台内容适配 | 计划中 |

## 参与贡献

项目由 [@polairszzh](https://github.com/polairszzh) 维护。欢迎提 Issue、PR 和功能建议。

## 开源协议

MIT License。详见 [LICENSE](LICENSE)。
