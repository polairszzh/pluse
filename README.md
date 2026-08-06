# Pulse — AI 可见度引擎

**Pulse 是一个面向 [Claude Code](https://claude.ai/claude-code) 和 [Codex](https://github.com/openai/codex) 的开源 AI 可见度插件。** 监控你的品牌和内容在中文 AI 平台（DeepSeek、Kimi、豆包、元宝）和社交搜索（知乎、小红书、B站、抖音）中的表现。每次审计都给出可验证的行动建议——不仅告诉你改什么，还告诉你改完之后怎么验证效果。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-blue)](https://claude.ai/claude-code)

> **双引擎，一套系统。** 同时提供 Claude Code（SKILL.md）和 Codex（TOML agent）入口。同一套脚本、同一套知识库、同一份数据——喜欢哪个运行时就用哪个。

## 为什么选 Pulse

- **AI 搜索优先，中文平台原生。** 市面上大多数 SEO/GEO 工具只针对 Google 和英文内容。Pulse 专攻中文 AI 生态：DeepSeek、Kimi、豆包、元宝，以及这些模型引用最多的社交平台。
- **为个人创作者设计，不是为企业。** 没有后台登录、没有销售电话。数据留在你的机器上，在终端里跑，和其他开发工具一样。
- **每条建议都可验证。** 每个发现都带有"怎么知道这条建议没效果？"的检查项。不说空话，只说能被检验的事。

## 谁适合用

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
/pulse brand --brand <品牌名> --topics 话题1,话题2 --competitors 竞品A,竞品B
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
| `/pulse brand --brand <品牌名>` | 品牌在知乎的整体可见度：搜索存在率、份额占比、话题覆盖缺口、互动基准 |
| `/pulse track <品牌/话题>` | 监控品牌/话题在 AI 平台上的被提及情况；`--mine <URL/标题/作者名>`（可重复）额外判断你的内容是否被引用 |
| `/pulse adapt <主题>` | 生成多平台适配内容：知乎长文、小红书笔记、AI 优化版 |
| `/pulse compare <A> <B>` | 品牌可见度横向对比 |
| `/pulse dashboard` | 启动本地 Web 仪表盘（端口 8766） |

## 功能亮点

**AI 搜索引用监控（Phase 2 · 已上线 MVP）** — 在 DeepSeek、Kimi、豆包、元宝上追踪品牌/话题是否被提及。DeepSeek 用真实 API 探测（回答正文是否出现关键词，含情感倾向与上下文）；Kimi/豆包/元宝无公开 API，用搜索引擎存在信号推断（报告中如实标注，不等同于真实引用）。传 `--mine <你的内容标识>` 可进一步判断 AI 回答/搜索结果里是否出现你的文章——即「话题被提到 ≠ 你的内容被引用」，这是个人创作者的核心度量。每次运行写入 `data/monitor.db`，同品牌重跑自动生成趋势对比。

**AI 可引用性评分（Phase 1 · 已上线：整篇粒度）** — AI 偏好引用 130-170 字的自包含内容块。基于 API 摘要对整篇打分（段落信息量、问答结构、引用密度、实体覆盖、数据、时效）；逐段「精确指出哪几段要改」依赖全文抓取，全文能力排在 Phase 3（多平台适配阶段），届时升级为逐段分析。

**内容质量评估（Phase 1 · 已上线，E-E-A-T 中文改编）** — 经验：第一手案例、原创数据。专业度：话题深度、术语准确性。权威性：被引次数、平台权重。可信度：来源可验证（权重最高）。

**多平台内容适配（Phase 3 · 规划中）** — 一篇原稿 → 三个版本。知乎：2000-4000 字，H2/H3 层级。小红书：300-800 字，短句 emoji。AI 优化版：130-170 字自包含段落，问答结构。

**竞品差距分析（Phase 1 · 已上线，/pulse brand）** — 不只比分数。Pulse 指出竞品覆盖了哪些话题而你没有——让你知道下一篇该写什么。

**本地仪表盘（Phase 2 · 已上线 MVP）** — `node dashboard/server.js` 启动（需 Node ≥ 22.5，Node ≥ 24 开箱即用，22.5–23.x 需 `--experimental-sqlite` flag），`localhost:8766`。概览卡片、各平台被提及趋势折线图、最近快照表。所有数据留在本地。

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
│  audit.py / brand.py / search_ai.py / zhihu_api.py / scorer.py
└──────────────────────────┘
```

平台专属知识（排名因素、内容格式、AI 引用模式）编码在 `references/` 的 Markdown 文件中，不写在代码里。社区可以贡献新平台规则，不用动引擎。

## 局限性

**知乎数据获取** — 使用知乎开放平台 API（搜索、用户内容、用户关注）。API 返回摘要而非全文，Phase 1 的 AI 可引用性评分为整篇粒度（非逐段）。每日配额约 1000-5000 次，个人使用完全够。知乎页面有严格的反爬机制，全文抓取计划在 Phase 3 通过用户登录态 + 浏览器渲染实现。

**AI 平台覆盖** — DeepSeek 有公开 API，直接探测真实回答。Kimi、豆包、元宝目前没有公开 API，Pulse 用 Bing 搜索结果推断其检索库中的存在信号（已实现，报告中如实标注为推断而非真实引用）。

**小红书和抖音** — 几乎没有公开 API，完整支持计划在 Phase 3 实现（需要 Playwright 浏览器渲染）。

## 路线图

| 阶段 | 内容 | 状态 |
|------|------|------|
| **Phase 1** | `/pulse audit` — 知乎文章分析、内容评分、竞品对比 | 开发中 |
| **Phase 2** | `/pulse track` — AI 平台品牌监控 + 本地仪表盘 MVP | 已上线（MVP） |
| **Phase 3** | `/pulse adapt` — 多平台内容适配 | 计划中 |

## 参与贡献

项目由 [@polairszzh](https://github.com/polairszzh) 维护。欢迎提 Issue、PR 和功能建议。

## 开源协议

MIT License。详见 [LICENSE](LICENSE)。
