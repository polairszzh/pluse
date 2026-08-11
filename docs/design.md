# AI Visibility Engine — 开发设计文档

> 状态：设计阶段 | 2026-08-04

---

## 1. 产品定义

### 1.1 一句话

监控和优化品牌/内容在**中文 AI 平台**（DeepSeek、Kimi、豆包、元宝）和**社交搜索**（知乎、小红书、B站、抖音、微信搜一搜）里的可见度。

### 1.2 目标用户

**个人创作者和开发者是第一优先级。**

- 你在知乎/小红书/B站上有自己的账号，想自己的内容被 AI 引用更多
- 你在做 side project / 开源项目，想知道项目名在 DeepSeek/Kimi 里的口碑
- 你是独立开发者/博主，没有预算去买企业 GEO 工具的年度订阅
- 不需要企业认证、不需要付费账号 — 装上就能跑

企业用户是后期扩展，但 MVP 阶段全部围绕个人使用场景设计。

### 1.3 产品形态

**混合架构：双引擎 Skill（Claude Code + Codex） + 本地 Web Dashboard（可视化）**

- Claude Code Skill（`skills/visibility/SKILL.md`）+ Codex Skill（`agents/visibility-*.md` TOML 格式）
- 两个引擎共享同一套 `scripts/` 和 `references/`，只是入口指令格式不同
- 从第一天就维护 `CLAUDE.md` 和 `AGENTS.md` 两份项目指令，像 Claude SEO 生态里 Claude SEO + Codex SEO 的关系
- 本地 Dashboard 负责可视化：品牌趋势图、AI 引用率变化、竞品对比
- 两者共享同一套脚本和数据文件，本地运行，零服务器成本
- 后期可选：托管云服务（定时自动跑 + 推送报告）

### 1.3 与竞品的差异

| 维度 | 国内 GEO 产品 | Claude SEO | **本产品** |
|------|-------------|-----------|----------|
| 用户 | 中大企业 CMO | 英文 SEO 从业者 | 中文开发者/独立创作者 |
| 平台覆盖 | AI 平台监控 | Google 搜索全链路 | AI 平台 + 社交搜索 + 内容分发 |
| 分发 | SaaS 销售驱动 | Claude Code Skill | Skill + 本地 Dashboard |
| 开源 | 闭源 | MIT | MIT |
| 差异化功能 | — | — | 一篇文章 → 多平台适配 + AI 引用追踪 + 竞品对比 |

---

## 2. MVP 范围与分阶段计划

### Phase 1 — 知乎单平台审计（预计 2-3 周）

**目标**：能跑通"输入 URL → 分析 → 出报告"的完整链路。

**命令**：
- `/visibility audit <知乎文章URL>` — 单篇文章深度分析
- `/visibility brand <品牌名>` — 品牌在知乎上的整体可见度

**不做**：
- 多平台内容适配
- AI 平台引用监控
- Dashboard

### Phase 2 — AI 搜索监控（预计 2 周）

**新增命令**：
- `/visibility monitor <品牌名/关键词>` — 在 DeepSeek、Kimi、豆包里搜索，追踪品牌是否被引用
- `/visibility compare <品牌A> <品牌B>` — 两个品牌在 AI 平台上的引用对比

**新增组件**：
- 本地 Dashboard MVP：一个 HTML 页面展示引用率趋势

### Phase 3 — 多平台内容适配（预计 2 周）

**新增命令**：
- `/visibility content <主题> <原文>` — 一篇文章 → 知乎版 + 小红书版 + AI 搜索优化版
- `/visibility platform <平台名> <URL>` — 支持小红书、B站的文章/视频分析

---

## 3. 架构设计

### 3.1 目录结构

```
visibility-engine/
├── CLAUDE.md                    # Claude Code 项目指令
├── AGENTS.md                    # Codex 项目指令
├── README.md                    # 项目说明 + 安装指南
├── LICENSE                      # MIT
├── package.json                 # Dashboard 依赖
├── install.sh                   # 安装脚本
│
├── skills/
│   └── visibility/
│       ├── SKILL.md             # 主 orchestrator（/visibility 命令入口）
│       └── references/
│           ├── zhihu-guide.md   # 知乎算法机制、内容格式、搜索排名因素
│           ├── xiaohongshu-guide.md  # 小红书搜索机制
│           ├── ai-search-guide.md    # DeepSeek/Kimi/豆包/元宝 引用机制分析
│           └── content-format.md     # 各平台内容格式对照表
│
├── agents/
│   │   ├── visibility-audit.md      # Claude Code agent (Markdown)
│   │   ├── visibility-audit.toml    # Codex agent (TOML)
│   │   ├── visibility-brand.md
│   │   ├── visibility-brand.toml
│   │   ├── visibility-content.md
│   │   ├── visibility-content.toml
│   │   ├── visibility-monitor.md
│   │   ├── visibility-monitor.toml
│   │   ├── visibility-compare.md
│   │   └── visibility-compare.toml
│
├── scripts/
│   ├── fetch_page.py            # 通用页面抓取（trafilatura + htmldate）
│   ├── fetch_zhihu.py           # 知乎专用抓取（文章内容、赞同数、评论）
│   ├── search_ai.py             # AI 平台搜索（DeepSeek/Kimi/豆包 API）
│   ├── search_platform.py       # 社交平台搜索（知乎/小红书/B站）
│   ├── content_adapter.py       # 内容格式转换引擎
│   └── scorer.py                # 0-100 评分引擎
│
├── dashboard/
│   ├── server.js                # Express 本地服务器（端口 8766）
│   ├── public/
│   │   ├── index.html           # 主仪表盘
│   │   └── app.js               # 前端逻辑（Chart.js 图表）
│   └── views/
│       └── report.ejs           # 报告模板
│
├── data/                        # 本地数据存储（gitignore）
│   ├── snapshots/               # 历史审计快照
│   └── monitor.db               # SQLite 监控数据
│
├── tests/
│   ├── test_audit.py
│   ├── test_monitor.py
│   └── test_content.py
│
└── docs/
    ├── ARCHITECTURE.md
    ├── COMMANDS.md
    └── API.md
```

### 3.2 三层架构（参照 Claude SEO）

```
┌──────────────────────────────────────┐
│  Directive Layer (SKILL.md)          │
│  ─ /visibility audit|brand|monitor   │
│  ─ 路由分发、参数解析                │
└──────────────┬───────────────────────┘
               │
┌──────────────▼───────────────────────┐
│  Orchestration Layer (agents/)       │
│  ─ 多 agent 并行调度                 │
│  ─ 结果聚合 + 优先级排序             │
│  ─ 0-100 评分                        │
└──────────────┬───────────────────────┘
               │
┌──────────────▼───────────────────────┐
│  Execution Layer (scripts/)          │
│  ─ fetch_*.py：数据采集              │
│  ─ scorer.py：量化评分               │
│  ─ content_adapter.py：内容转换      │
│  ─ search_*.py：多平台搜索           │
└──────────────────────────────────────┘
```

### 3.3 双引擎设计（Claude Code + Codex）

参照 Claude SEO 生态的做法——同一个 SEO 系统同时发布 `claude-seo` 和 `codex-seo`。我们从第一天就按双引擎设计：

```
                    ┌─────────────────────┐
                    │  references/        │
                    │  skills/visibility/ │  ← 共享知识库（平台规则、内容格式）
                    │  scripts/           │  ← 共享执行层（Python 脚本）
                    │  data/              │  ← 共享数据层（快照、监控DB）
                    │  dashboard/         │  ← 共享可视化层
                    └─────────┬───────────┘
                              │
              ┌───────────────┴───────────────┐
              │                               │
    ┌─────────▼──────────┐      ┌─────────────▼──────────┐
    │ Claude Code Engine │      │    Codex Engine        │
    │                    │      │                        │
    │ skills/visibility/ │      │ agents/*.toml          │
    │   SKILL.md         │      │ (TOML agent 定义)       │
    │ agents/*.md        │      │                        │
    │ (Markdown agent)   │      │ 入口: /visibility ...  │
    │                    │      │ (Codex 命令格式)        │
    │ 入口: /visibility  │      │                        │
    └────────────────────┘      └────────────────────────┘
```

**差异点**：
- Claude Code agent 用 Markdown 格式（`agents/*.md`），指令写在 YAML frontmatter + markdown body
- Codex agent 用 TOML 格式（`agents/*.toml`），支持 `[agent]` `[tool]` `[prompt]` 段
- 两套 agent 文件共享同一个 `scripts/` 和 `references/`，只有格式不同
- `CLAUDE.md` 和 `AGENTS.md` 分别在各自的 session 启动时被加载，内容对称但语法适配

### 3.4 平台知识编码策略

核心原则：平台规则不是写在 Python 代码里，而是编码在 `references/` 的 markdown 文件中。这样：
- 社区可以贡献新平台规则，不需要改代码
- 平台算法变化时，更新一个 markdown 文件即可
- Agent 在运行时会读取这些 reference 文件作为上下文

例子（`references/zhihu-guide.md` 结构）：
```markdown
# 知乎搜索排名机制

## 排序因子（按权重）
1. 赞同数（尤其是前 24 小时的赞同速度）
2. 收藏数（高权重信号，代表内容价值）
3. 账号权重（盐值分、领域垂直度、历史表现）
4. 内容质量（原创度、篇幅、多媒体丰富度）
5. 关键词匹配（标题 > 首段 > H2 > 正文）

## AI 引用特征
- DeepSeek 高频引用知乎来源，尤其是技术/产品类问题
- Kimi 倾向于引用有数据支撑的回答
- 豆包引用知乎频率较低，更偏好头条/抖音生态内容

## 内容格式最佳实践
- 标题：15-30字，含核心关键词，问题式或对比式最佳
- 正文：2000-4000字，H2/H3 层级清晰
- 首段：100-150字，直接回答或抛出核心观点
- 每段 120-180字，之间有逻辑连接词
```

### 3.5 数据流

```
/visibility audit <知乎URL>
        │
        ▼
  SKILL.md 解析参数
        │
        ├──→ fetch_zhihu.py ──→ 文章元数据（标题、正文、赞同、评论、发布时间）
        ├──→ fetch_page.py  ──→ 页面技术数据（加载速度、结构化数据）
        └──→ search_ai.py   ──→ 相关关键词在 AI 平台的表现
        │
        ▼
  visibility-audit agent
        │
        ├── 内容质量评分（E-E-A-T 改编）
        ├── AI 可引用性评分（passage citability）
        ├── 关键词分析
        └── 竞品对比（同话题下知乎高赞文章）
        │
        ▼
  scorer.py ──→ 0-100 综合评分
        │
        ▼
  输出：Markdown 报告 + JSON + 可选 PDF
        │
        ▼
  data/snapshots/ ──→ 历史记录（供 Dashboard 展示趋势）
```

---

## 4. 输入/输出规格

### 4.0 统一 output contract（D2，2026-08-11）

audit / track / brand 的 JSON 快照推荐项统一结构（adapt 的 `draft_score.recommendations`
为草稿建议 dict，不含推荐数组，不在本 contract 范围）：

- **顶层**：`generated_at`（ISO 时间）、`query`（目标话题）、`recommendations`（统一六元数组）、各命令专属字段（article/scores、results/trend、benchmark 等）。
- **推荐项六元**（`rec_as_contract`，audit.py 定义，全命令共用）：
  | 键 | 含义 | 来源 |
  |----|------|------|
  | `finding` | 发现/问题维度 | Recommendation.dimension |
  | `evidence` | 支撑证据 | Recommendation.evidence |
  | `impact` | 预期影响 | Recommendation.expected_impact |
  | `fix` | 具体动作 | Recommendation.action |
  | `confidence` | 置信度（confirmed/likely/hypothesis） | Recommendation.confidence |
  | `falsifiability` | 怎么知道没效果 | Recommendation.falsifiability_check |

- 测试断言六元键存在（audit/track 的 render_json 输出）。

### 4.1 `/visibility audit`

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 知乎文章完整 URL，如 `https://zhuanlan.zhihu.com/p/123456` |
| `--keywords` | string[] | 否 | 目标关键词列表，逗号分隔。不提供则自动从文章中提取 |
| `--compare` | string | 否 | 竞品文章 URL，对比分析 |
| `--format` | enum | 否 | `markdown`（默认）\| `json` \| `html` |

**输出**：`AUDIT-REPORT.md`

```markdown
# 可见度审计报告：<文章标题>

**URL**: https://zhuanlan.zhihu.com/p/123456
**日期**: 2026-08-04
**作者**: <作者名>
**平台**: 知乎

---

## 总览

| 指标 | 值 |
|------|-----|
| **可见度评分** | 72/100 (B+) |
| **AI 可引用性** | 68/100 |
| **内容质量** | 80/100 |
| **关键词覆盖** | 65/100 |
| **互动数据** | 赞同 234 · 收藏 89 · 评论 45 |

---

## 1. AI 可引用性分析

### Passage Citability 评分：68/100

| 段落 | 字数 | 可引用性 | 问题 |
|------|------|---------|------|
| 第3段 | 156字 | ✅ 高 | 自包含答案块，有数据支撑 |
| 第5段 | 89字 | ⚠️ 中 | 太短，缺乏上下文 |
| 第7段 | 312字 | ❌ 低 | 太长，AI 难以摘取 |

### 建议
- 将第7段拆分为 2-3 个 130-160 字的自包含段落
- 第5段补充具体数据或案例

---

## 2. 内容质量分析 (E-E-A-T 改编)

| 维度 | 得分 | 说明 |
|------|------|------|
| 经验信号 | 60/100 | 缺少第一手案例或原创数据 |
| 专业深度 | 85/100 | 技术细节扎实，行业术语使用准确 |
| 权威背书 | 40/100 | 未被其他高权重来源引用 |
| 可信度 | 75/100 | 信息可验证，但缺少引用来源 |

### 建议
1. 🔴 添加 2-3 个第一手案例或原创数据（提升经验信号）
2. 🟡 文中提到的数据点标注来源链接（提升可信度）
3. 🟡 在文末添加作者背景简介（提升权威背书）

---

## 3. 关键词覆盖

| 目标关键词 | 出现频次 | 位置 | 状态 |
|-----------|---------|------|------|
| AI 搜索优化 | 8次 | 标题、H2、正文 | ✅ 已覆盖 |
| GEO | 3次 | H3、正文 | ⚠️ 未在首段出现 |
| 内容策略 | 2次 | 正文 | ❌ 频次不足 |

### 竞品对比
同话题下知乎 Top 3 文章的共同关键词：
- "AI 引用率"（你的文章未覆盖）
- "品牌监测"（你的文章已覆盖）
- "多平台分发"（你的文章未覆盖）

---

## 4. 结构化与格式

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 标题含目标关键词 | ✅ | |
| H2/H3 层级合理 | ✅ | |
| 段落长度（120-180字） | ⚠️ | 3个段落超 250 字 |
| 有列表/表格 | ✅ | |
| 有数据/图表 | ❌ | 建议添加 |
| 文末有 CTA/引导 | ❌ | 建议添加互动引导 |

---

## 5. 优先行动计划

| 优先级 | 行动 | 预期影响 | 可证伪性检查 |
|--------|------|---------|------------|
| 🔴 P0 | 补充"AI 引用率"相关关键词段落 | +8 AI可引用性 | 两周后在 DeepSeek 搜索该话题，文章是否被引用 |
| 🔴 P0 | 添加第一手案例数据 | +15 内容质量 | 赞同/收藏增长率是否提升 |
| 🟡 P1 | 第7段拆分为 3 个短段落 | +5 AI可引用性 | DeepSeek 引用片段是否来自新拆分段落 |
| 🟡 P1 | 首段加入核心关键词 | +5 关键词覆盖 | 知乎搜索排名是否上升 |
| 🟢 P2 | 添加数据可视化图表 | +3 互动率 | 收藏数是否增长 |

---

## 附录

### A. 方法论
本报告基于以下框架：
- Google AI Optimization Guide (May 2026)
- E-E-A-T 评分改编（针对中文内容生态）
- Passage Citability 评分（134-167 字最优窗口）

### B. 原始数据
见 `data/snapshots/audit-20260804-zhihu-123456.json`
```

### 4.2 `/visibility brand`

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 品牌名称 |
| `--platforms` | string[] | 否 | 监测平台，默认全选：`zhihu,xiaohongshu,bilibili,douyin,weixin` |
| `--period` | enum | 否 | `snapshot`（默认，当前快照）\| `3m` \| `6m` \| `1y` |

**输出**：`BRAND-REPORT.md`

```markdown
# 品牌可见度报告：<品牌名>

**日期**: 2026-08-04
**监测平台**: 知乎、小红书、B站

---

## 总览

| 维度 | 得分 | 变化 |
|------|------|------|
| **综合可见度** | 65/100 | ↑3 |
| 知乎 | 72/100 | ↑5 |
| 小红书 | 58/100 | ↓2 |
| B站 | 60/100 | — |
| AI 平台引用率 | 12% | ↑4% |

---

## 1. 平台可见度详情

### 知乎
| 指标 | 当前值 | 较上月 |
|------|--------|--------|
| 品牌相关文章数 | 47篇 | +3 |
| 高赞文章（>100赞同） | 12篇 | +2 |
| 品牌话题关注者 | 3,240 | +156 |
| 负面内容数 | 2篇 | — |

### AI 平台引用率
| 平台 | 引用率 | 情感 |
|------|--------|------|
| DeepSeek | 18% | 🟢 正面 |
| Kimi | 9% | 🟡 中性 |
| 豆包 | 12% | 🟢 正面 |
| 元宝 | 5% | 🟡 中性 |

### 对比竞品
| 竞品 | 综合可见度 | AI引用率 | 差距 |
|------|----------|---------|------|
| 竞品A | 78/100 | 24% | +13 |
| 竞品B | 55/100 | 8% | -10 |

---

## 2. 优先行动计划

（同 audit 格式，按 P0/P1/P2 排列）
```

### 4.3 `/visibility monitor`

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 监控关键词或品牌名 |
| `--platforms` | string[] | 否 | AI 平台：`deepseek,kimi,doubao,yuanbao`（默认全选） |
| `--schedule` | enum | 否 | `once`（默认）\| `daily` \| `weekly` |

**输出**：`MONITOR-REPORT.md`

```markdown
# AI 搜索监控报告：<关键词>

**日期**: 2026-08-04
**监测平台**: DeepSeek、Kimi、豆包

---

## 1. 引用概览

| 平台 | 是否被引用 | 引用位置 | 情感 | 上下文 |
|------|-----------|---------|------|--------|
| DeepSeek | ✅ 是 | 回答第3段 | 🟢 正面 | "XX 是该领域最活跃的开源项目之一" |
| Kimi | ✅ 是 | 回答第5段 | 🟡 中性 | "可以参考 XX 和 YY 两个方案" |
| 豆包 | ❌ 否 | — | — | 提到了竞品 A 和 B，未提及本品牌 |

---

## 2. 引用趋势（近30天）

[Chart: 折线图 — 三个平台引用率变化]

---

## 3. 竞品引用对比

| 品牌 | DeepSeek | Kimi | 豆包 | 综合 |
|------|---------|------|------|------|
| 本品牌 | 18% | 9% | 0% | 9% |
| 竞品A | 24% | 15% | 8% | 16% |
| 竞品B | 8% | 5% | 3% | 5% |

---

## 4. 建议

1. 豆包完全未引用 → 检查品牌在豆包的数据源（知乎？头条？）是否有存在感
2. Kimi 引用偏中性 → 提升内容的数据量和原创性，增加正面引用概率
```

### 4.4 `/visibility content`

**输入**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `topic` | string | 是 | 内容主题 |
| `--source` | string | 否 | 原始文章路径或 URL（如果有现成文章） |
| `--platforms` | string[] | 否 | 目标平台：`zhihu,xiaohongshu,douyin,ai`（默认全选） |
| `--tone` | enum | 否 | `professional`（默认）\| `casual` \| `storytelling` |

**输出**：每个平台一个 `.md` 文件

```
data/output/
├── zhihu-<slug>.md          # 知乎长文版
├── xiaohongshu-<slug>.md    # 小红书图文版
├── douyin-<slug>.md         # 抖音脚本版
└── ai-optimized-<slug>.md   # AI 搜索优化版
```

**内容适配规则表**（`references/content-format.md`）：

| 特征 | 知乎 | 小红书 | 抖音/B站 | AI 优化版 |
|------|------|--------|---------|----------|
| 字数 | 2000-4000字 | 300-800字 | 脚本 500-1000字 | 1200-2000字 |
| 段落长度 | 120-180字 | 30-60字 | — | 134-167字 |
| 标题风格 | 深度+关键词 | 口语化+emoji | 前三秒钩子 | 问题导向 |
| 结构 | H2/H3 层级 | 短句 + 分点 + emoji | 画面描述 + 旁白 | Q&A 块 |
| 关键词密度 | 2-3% | 1-2%（自然融入） | 口语提及 | 2-3% + 相关问题 |
| 链接 | 可放外链 | 不能放外链 | 评论区放链 | 不依赖链接 |
| 互动引导 | 文末提问 | 文末互动 | 口播引导 | N/A |

---

## 5. 评分引擎规格

### 5.1 0-100 评分体系

```
综合可见度评分 = 0.35 × AI可引用性 + 0.25 × 内容质量
                + 0.20 × 关键词覆盖 + 0.10 × 结构化 + 0.10 × 互动数据
```

### 5.2 AI 可引用性评分（6 维度，每项 0-100）

| 维度 | 权重 | 评分依据 |
|------|------|---------|
| Passage Citability | 30% | 是否有 134-167 字的自包含答案块 |
| 问题-答案结构 | 20% | H2/H3 是否为问题形式，正文是否直接回答 |
| 引用密度 | 20% | 文章是否引用/被引用自权威来源 |
| 实体存在感 | 15% | 品牌/人名/产品名在 Wikipedia、Reddit、知乎等平台的出现情况 |
| 数据可引用性 | 10% | 是否包含可被 AI 直接引用的统计数据 |
| 时效性 | 5% | 发布时间、是否有更新日期 |

### 5.3 内容质量评分（E-E-A-T 中文改编版）

| 维度 | 权重 | 评分依据 |
|------|------|---------|
| 可信度（Trust） | 30% | 信息来源是否可验证、是否有利益冲突声明 |
| 经验（Experience） | 25% | 第一手案例、原创数据、实际操作截图 |
| 专业度（Expertise） | 25% | 行业术语准确性、分析深度、逻辑严谨性 |
| 权威性（Authority） | 20% | 作者背景、平台权重、被引用次数 |

---

## 6. 数据模型

### 6.1 审计快照（`data/snapshots/audit-<timestamp>-<slug>.json`）

```json
{
  "id": "audit-20260804-zhihu-123456",
  "timestamp": "2026-08-04T14:00:00+08:00",
  "type": "article",
  "platform": "zhihu",
  "url": "https://zhuanlan.zhihu.com/p/123456",
  "metadata": {
    "title": "...",
    "author": "...",
    "published": "2026-07-15",
    "word_count": 3200,
    "upvotes": 234,
    "saves": 89,
    "comments": 45
  },
  "scores": {
    "overall": 72,
    "ai_citability": 68,
    "content_quality": 80,
    "keyword_coverage": 65,
    "structure": 75,
    "engagement": 70
  },
  "recommendations": [
    {
      "priority": "P0",
      "action": "...",
      "expected_impact": "+8 ai_citability",
      "falsifiability_check": "..."
    }
  ]
}
```

### 6.2 监控记录（`data/monitor.db` SQLite）

```sql
CREATE TABLE snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT NOT NULL,
  platform TEXT NOT NULL,  -- deepseek | kimi | doubao | yuanbao
  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
  cited BOOLEAN,
  sentiment TEXT,           -- positive | neutral | negative
  context TEXT,
  position INTEGER,         -- 在第几段被引用
  competitor_cited TEXT,    -- JSON array of competitor names
  raw_response TEXT
);

CREATE INDEX idx_query_platform ON snapshots(query, platform);
CREATE INDEX idx_timestamp ON snapshots(timestamp);
```

---

## 7. 脚本接口规格

### 7.1 `fetch_zhihu.py`

```
输入：
  --url <知乎文章URL>
  --output <输出JSON路径>
  --render [auto|never|always]  默认 auto（检测是否需 JS 渲染）

输出 JSON：
{
  "success": true,
  "url": "...",
  "title": "...",
  "author": {
    "name": "...",
    "bio": "...",
    "followers": 1234
  },
  "content": {
    "raw_html": "...",
    "plain_text": "...",
    "word_count": 3200,
    "paragraphs": [...]
  },
  "stats": {
    "upvotes": 234,
    "saves": 89,
    "comments": 45,
    "shares": 12
  },
  "topics": ["AI", "SEO"],
  "published": "2026-07-15T10:30:00+08:00",
  "updated": "2026-07-20T14:00:00+08:00"
}
```

### 7.2 `search_ai.py`

```
输入：
  --query <搜索关键词>
  --platforms <deepseek,kimi,doubao,yuanbao>
  --output <输出JSON路径>

输出 JSON：
{
  "query": "AI搜索优化工具推荐",
  "timestamp": "2026-08-04T14:00:00+08:00",
  "results": [
    {
      "platform": "deepseek",
      "cited": true,
      "sentiment": "positive",
      "context": "在AI搜索优化领域，目前有XX等工具...",
      "position": 3,
      "competitors_mentioned": ["Claude SEO", "SheepGeo"],
      "raw_answer": "..."
    },
    {
      "platform": "doubao",
      "cited": false,
      "competitors_mentioned": ["SheepGeo", "Geowise"]
    }
  ]
}
```

### 7.3 `content_adapter.py`

```
输入：
  --topic <主题>
  --source <原始文章路径>    可选
  --platforms <zhihu,xiaohongshu,douyin,ai>
  --tone <professional|casual|storytelling>
  --output-dir <输出目录>

行为：
  1. 如果提供 --source，读取原文作为素材
  2. 如果仅提供 --topic，先做话题研究（搜索各平台相关内容）
  3. 根据每个平台的格式规则（references/content-format.md）生成适配版本
  4. 每个平台输出一个 .md 文件

输出文件：
  data/output/<platform>-<slug>.md
```

---

## 8. Dashboard 规格（Phase 2）

### 8.1 定位

这是**个人创作者视角**的可视化面板，不是企业后台。信息架构围绕一个核心问题组织：
**"我的内容在 AI 和社交平台上的表现怎么样，下一步该做什么？"**

### 8.2 技术栈

- 后端：Node.js + Express
- 前端：纯 HTML/CSS/JS + Chart.js（图表）
- 数据：读本地 JSON 快照 + SQLite 监控数据
- 端口：8766

### 8.3 页面布局

```
╔══════════════════════════════════════════════════════════════════╗
║  Visibility Engine                         最后更新: 2 分钟前    ║
║  Hi 老板 · 个人创作者版                                          ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  ┌─ 总览卡片 ───────────────────────────────────────────────┐   ║
║  │                                                           │   ║
║  │  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────┐ │   ║
║  │  │ 综合可见度 │  │ AI 引用率  │  │ 知乎互动  │  │ 内容  │ │   ║
║  │  │           │  │           │  │           │  │ 总量  │ │   ║
║  │  │   72/100  │  │   12%     │  │  +234 赞  │  │ 47篇  │ │   ║
║  │  │   ↑3     │  │   ↑4%     │  │  ↑ 本月   │  │ +3篇  │ │   ║
║  │  └───────────┘  └───────────┘  └───────────┘  └───────┘ │   ║
║  └───────────────────────────────────────────────────────────┘   ║
║                                                                  ║
║  ┌─ AI 平台表现 ─────────────────────────────────────────────┐   ║
║  │                                    [30天 ▼] [导出]        │   ║
║  │  ┌─────────────────────────────────────┐  ┌───────────┐  │   ║
║  │  │ 引用率趋势 (折线图)                   │  │ 最新引用快照│  │   ║
║  │  │ 25%│        .DeepSeek               │  │           │  │   ║
║  │  │ 20%│   .--''`--.                    │  │ DeepSeek  │  │   ║
║  │  │ 15%│.-'  Kimi   `--.__             │  │ "XX 是该领│  │   ║
║  │  │ 10%│/   豆包          `--.          │  │ 域最活跃的│  │   ║
║  │  │  5%│ 元宝                  `-.      │  │ 开源项目"  │  │   ║
║  │  │  0%└─────────────────────┘           │  │ 🟢 正面   │  │   ║
║  │  │    7/1  7/8  7/15  7/22  8/1       │  │ 3 小时前  │  │   ║
║  │  └─────────────────────────────────────┘  │           │  │   ║
║  │                                           │ Kimi       │  │   ║
║  │                                           │ "可参考 XX │  │   ║
║  │                                           │ 和 YY"     │  │   ║
║  │                                           │ 🟡 中性    │  │   ║
║  │                                           └───────────┘  │   ║
║  └───────────────────────────────────────────────────────────┘   ║
║                                                                  ║
║  ┌─ 平台内容表现 ──┐  ┌─ 竞品差距 ──────────────────────────┐   ║
║  │                  │  │                                      │   ║
║  │ [知乎] [小红书]   │  │  竞品        可见度  AI引用  差距   │   ║
║  │  [B站] [抖音]    │  │  ────────────────────────────────   │   ║
║  │                  │  │  本品牌(你)   ████████░░ 72   12%    │   ║
║  │  ┌── 文章表现 ─┐ │  │  竞品A       ██████████ 78   24% +6 │   ║
║  │  │ 标题    互动 │ │  │  竞品B       ████░░░░░░ 55    8% -17│   ║
║  │  │ AI SEO  234赞│ │  │                                      │   ║
║  │  │ GEO入门  89赞│ │  │  🔍 你在以下话题中缺失，竞品A在吃红利:│   ║
║  │  │ 多平台    56赞│ │  │  · "AI引用率优化"   竞品A 有3篇     │   ║
║  │  │ 内容策略  45赞│ │  │  · "品牌监测工具对比" 仅竞品A 覆盖  │   ║
║  │  │ ...更多       │ │  │  · "小红书SEO"       你已有2篇 ✅  │   ║
║  │  └──────────────┘ │  │                                      │   ║
║  └──────────────────┘  └──────────────────────────────────────┘   ║
║                                                                  ║
║  ┌─ 待办行动清单 ──────────────────────────────────────────┐   ║
║  │  优先级  行动                     预期效果    截止    状态│   ║
║  │  ──────────────────────────────────────────────────────  │   ║
║  │  🔴 P0   补充"AI引用率"段落       +8 可引用性  8/8    ○ │   ║
║  │  🔴 P0   在知乎发一篇"品牌监测对比"  填补话题空白 8/10   ○ │   ║
║  │  🟡 P1   拆分段7为三个短段落       +5 可引用性  8/15   ○ │   ║
║  │  🟢 P2   添加数据可视化图表        +3 互动      —      ○ │   ║
║  └──────────────────────────────────────────────────────────┘   ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

### 8.4 页面布局逻辑

**信息层级（自上而下）**：
1. **总览卡片** — 4 个核心指标，一眼看懂当前状态。用箭头标注变化方向
2. **AI 平台表现** — 左侧趋势线（30天引用率变化），右侧实时引用快照（最近一次各 AI 平台的引用内容原文），让人既看到趋势又看到细节
3. **双栏 — 平台内容表现 + 竞品差距** — 左侧展示知乎/小红书/B站/抖音四个 tab 切换的内容列表（按赞同/收藏排序），右侧展示竞品对比条 + **话题空缺提示**（这是关键差异化功能——不是简单说"你比竞品差 6 分"，而是说"竞品A 在以下话题上有内容而你没有"）
4. **待办行动清单** — 从每次 audit/monitor 的报告中提取 P0/P1/P2 建议，变成可勾选的行动项，有截止日期

### 8.5 个人创作者 vs 企业版差异

| 元素 | 个人创作者版（默认） | 企业版（后期可选） |
|------|-------------------|------------------|
| 品牌数量 | 1 个（就是你） | 多品牌切换 |
| 竞品追踪 | 手动添加 2-3 个竞品 | 自动发现 + 批量追踪 |
| 报告频率 | 手动跑，出报告 | 定时自动跑，推送通知 |
| 数据导出 | Markdown + JSON | + PDF 精美报告 |
| 协作 | 无 | 团队成员可见 |
| 定价 | 免费开源 | 托管付费 |

### 8.6 API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/overview` | GET | 总览卡片数据（4 个指标 + 变化值） |
| `/api/ai-trends` | GET | AI 引用率趋势（`?days=30`） |
| `/api/ai-latest` | GET | 最新 AI 引用快照（各平台最新一条） |
| `/api/platform/:name` | GET | 指定平台的内容列表（知乎/小红书/B站/抖音） |
| `/api/competitors` | GET | 竞品列表 + 对比数据 |
| `/api/gaps` | GET | 话题空缺分析（竞品覆盖了但你没覆盖的话题） |
| `/api/actions` | GET | 待办行动清单 |
| `/api/actions/:id/toggle` | POST | 切换行动项完成状态 |
| `/api/snapshots` | GET | 审计快照历史列表 |
| `/api/snapshots/:id` | GET | 单个快照详情 |
| `/api/compare` | POST | 自定义竞品对比 `{"query":"...", "competitors":["A","B"]}`

---

## 9. 开发顺序

```
Phase 1（3周）
  Week 1: scripts/fetch_zhihu.py + scripts/scorer.py
  Week 2: skills/visibility/SKILL.md + agents/visibility-audit.md
  Week 3: /visibility audit 端到端跑通 + 测试

Phase 2（2周）
  Week 4: scripts/search_ai.py + agents/visibility-monitor.md
  Week 5: /visibility monitor + dashboard MVP

Phase 3（2周）
  Week 6: scripts/content_adapter.py + agents/visibility-content.md
  Week 7: /visibility content + 小红书/B站支持
```

---

## 10. 数据策略（已验证）

> 验证日期：2026-08-04

### 10.1 知乎数据采集

**API 已调通**（使用知乎开放平台 Access Secret）：

| API | 状态 | 给 Pulse 提供什么 |
|-----|------|------------------|
| 搜索 API `/content/zhihu_search` | ✅ Code: 0 | 话题文章列表、标题、摘要（300-800字）、赞同数、评论数、排名分、作者信息 |
| 用户内容 API `/user/contents` | ✅ Code: 0 | 本人所有文章/回答的点赞、评论、收藏数据 |
| 用户关注 API `/user/followees` | ✅ Code: 0 | 关注列表 + 每个用户的粉丝数 |
| 直答 API `/chat/completions` | 未测试 | 可用于品牌情感分析（"XX 这个产品怎么样？"），Phase 2 探索 |

**页面全文抓取不可行**（已验证）：

| 方案 | 结果 |
|------|------|
| `requests` 直接抓 | ❌ HTTP 403 — 知乎自研反爬 `zh-zse-ck` |
| `cloudscraper` | ❌ 403 — 只能绕 Cloudflare，绕不过 zh-zse-ck |
| Playwright + stealth + webdriver 伪装 | ❌ 403 — 知乎识别 headless 浏览器 |

**结论**：Phase 1 使用 API 数据做审计。API 的 `ContentText` 摘要（300-800字）+ 结构化数据（赞同/收藏/评论/排名分）足够支撑粗粒度的审计报告。Passage citability 从"逐段打分"降级为"整篇打分"——这是有意识的取舍，换取零安装门槛（不需要 Chromium）。

全文抓取留给 Phase 3，届时需要用户提供知乎登录 cookie + 非 headless 模式。

### 10.2 AI 平台搜索

**DeepSeek** 有公开 API，可直接查询。
**Kimi、豆包、元宝** 目前无公开 API。Phase 2 使用搜索引擎推断（Bing/Google `site:` 搜索）作为降级方案。

### 10.3 未解决的问题

1. ~~知乎数据采集策略~~ → 已解决：Phase 1 用 API，Phase 3 考虑全文抓取
2. **AI 平台搜索方式**：DeepSeek 有公开 API，Kimi 和豆包目前没有
3. **小红书数据采集**：几乎没有公开 API，Phase 3 可能需要 Playwright
4. **额度确认**：API 文档写 5000/天，实测入口显示 1000/天。按 1000/天规划
5. **其他用户数据**：用户 API 需要 OAuth 授权才能看别人的数据，竞品作者分析受限
6. **测试策略**：需要一组固定测试文章，跑 audit 后人工打分作为 ground truth，调 scorer 权重
