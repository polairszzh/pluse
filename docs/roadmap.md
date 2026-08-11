# Pulse 后续开发计划（Roadmap）

> 状态：规划中 | 2026-08-06
> 本文档吸收以下开源仓库的设计，转化为 Pulse 的后续迭代方向。每条标注来源与设计要点，便于回溯。

## 来源仓库

| 仓库 | 吸收的核心设计 |
|------|--------------|
| [HeiGe-GEO-SEO](https://github.com/HeiGeAi/HeiGe-GEO-SEO) | 国内 GEO 核心判断（求收录+占平台）、八层瓶颈定位、评分驱动改写指令编译器、监测=概率闭环、引用质量分层、诚实边界 |
| [claude-seo](https://github.com/AgriciDaniel/claude-seo) | Falsifiability 四件套（依据/依赖/失败判据/先行指标）、段落级可引用性、E-E-A-T 信任要素、drift 基线、分级凭证 |
| [Agentic-SEO-Skill](https://github.com/Bhanunamikaze/Agentic-SEO-Skill) | 置信度标签（Confirmed/Likely/Hypothesis）、证据先行工作流、报告前 verifier、知识库 freshness、统一 rubric |
| [seo-audit-skill](https://github.com/JeffLi1993/seo-audit-skill) | Script + LLM 两层架构：确定性检查交给脚本，LLM 只处理 `llm_review_required` 的语义判断 |
| [geo-seo-claude](https://github.com/zubair-trabzada/geo-seo-claude) | 并行子 agent 审计、平台差异化优化、六维权重评分法 |
| [Seo-Prompt-Master](https://github.com/umutxyp/Seo-Promt-Master) | 确定性 0-100 评分卡 + blocker 封顶、五阶段工作流、规则全部带来源引用 |
| [awesome-seo](https://github.com/bmpi-dev/awesome-seo) | 资源索引：可作为 `references/` 的入口清单（GEO KDD 2024、AutoGEO 等） |

## 顶层原则（吸收后固化为开发准则）

1. **确定性检查交给脚本，LLM 只做语义判断**（seo-audit-skill / claude-seo）。引用判定、URL 匹配、评分算分必须可复现；LLM 只处理「这段 H1 是否覆盖了搜索意图」这类语义问题。Pulse 已基本如此，后续新增功能保持这条边界。
2. **每个发现带置信度标签 + falsifiability check**（Agentic-SEO-Skill / claude-seo）。标签：`Confirmed`（真实 API 探测）/ `Likely`（搜索推断）/ `Hypothesis`（启发式）。每个推荐带「怎么知道它失败了」的判据。
3. **国内 GEO = 求收录 + 占平台**（HeiGe）。国内 AI 没有公开爬虫，靠博查/搜狗/百度索引和自家生态；robots.txt 基本失效。Pulse 的优化建议应围绕「被索引 + 占住平台生态位」，而不是海外那套 robots/llms.txt 打法。
4. **不编造数据，数字是方向参数**（HeiGe / Seo-Prompt-Master）。引语、统计、引用一律不虚构；权重和结论标注来源与时间，失效时如实说明。
5. **报告前过 verifier**（Agentic-SEO-Skill）：去重、矛盾抑制、按影响排序后再输出，从源头减少 review 返工。
6. **监测 = 概率闭环**（HeiGe）：单次探测只是样本，多次采样取均值才是「被引用概率」。

---

## 可信 GEO 与反投毒护栏（2026-08-07）

**原则**：正规 GEO 与投毒的本质区别是「教 AI 说真话」而不是「操控 AI」。真正有效的 GEO 靠结构化内容和权威信源，不是批量灌水。Pulse 明确站在可信侧，护栏内置在工具里（开源工具没有使用者准入，不能指望使用者自律）。

**五条不可越界的红线**（2026-08-07 写入 README 与本文档，作为项目公开承诺）：
1. **不编造**——数据/来源/引用必须有出处，LLM 只改文风不造事实
2. **不批量灌水**——输出均为「人写 + 人审」，不提供一键批量生产
3. **利益披露**——推荐付费课程/网站/商品必须声明利益关系
4. **不碰 AI 呈现**——不做隐藏提示词、不针对检索漏洞动手脚
5. **守平台规则**——不刷量、不诱导、不绕限流

**现有护栏**（已实现）：
- 不编造数据：LLM 只改文风不造事实，素材缺口清单列出待核实链接与数字断言
- 发布前人工检查清单 + AI 版「内部参考」标注
- 每条建议带 falsifiability check；Bing 推断 ≠ 真实引用，报告中明示

**营销转化/投毒信号检测**（2026-08-07 实现）：
- 高危（扫码购买/私信领取/优惠码/必买/限时抢购/手慢无/加微信等）：**直接拒绝生成（exit 3）**，不产出任何版本，CLI 列出命中的信号与解除方式
- 中危（强烈推荐/全网第一/唯一/100%/免费领取等）：进检查清单提示核实利益关系
- 正常推荐（体验分享 + 不构成购买建议）不误报，个人创作者写推荐文不受影响

**数据可验证性 · 置信度防火墙**（MVP 已实现，2026-08-07，提前于排期）：
- **验证对象**：草稿中「可公开核实的声明型数据」——品牌/产品 + 数字（用户数、价格、日期、百分比、版本号、政策）。第一手经验数据（「我上周处理了 3000 行」）不做外部验证。
- **验证手段**：确定性多源交叉比对（复用 Bing 搜索），**LLM 不做事实裁判**——不能让幻觉验证幻觉。
- **来源权威度分级**：官方域名 > 权威百科/政府/主流媒体 > 普通网页/二手来源；冲突时优先权威源。
- **四级判定**（可信度模型：仅权威来源可确认/否决，普通来源可能为投毒/灌水源）：
  - confirmed（权威来源重现断言）→ 放行；
  - conflict（权威来源否定断言）→ **拒绝生成（exit 3，同营销信号机制）**，列出冲突证据；
  - untrusted（仅普通来源支持或否定）→ 不阻断，标注「来源可信度不足，普通网页可能为投毒/灌水来源」，进检查清单；
  - unverified（未检索到相关来源）→ 不阻断，标注「信息可能存在延迟或不确定性，建议通过官方渠道核验」，进检查清单。
- **风险领域标记**：医学偏方、软件版本号、价格/计费政策等易传播错误信息的领域，即使无法核实也强制标注不确定性。
- **引用原则**：只做事实核查，不做「搬运工」——验证结论只用于放行/标注/拒绝，不把搜索结果搬进生成内容。
- **依赖与前置**：复用 Bing 搜索通道与 track 置信度标签基建（Phase 4 第一批已落地）；防火墙实现期间通过多轮 review 收敛否定/中性/权威判定的语义边界。
  **实现**（scripts/fact_checker.py + adapt 集成）：数字断言+上下文提取（跳过第一手经验、同断言保留最高风险上下文）、Bing 多源交叉、显式权威白名单（政府/教育/主流媒体/著名百科/著名公司官方）、四级判定（confirmed/conflict/untrusted/unverified）、风险领域分级（医学/招考 high、价格政策/版本 medium、无风险 low）、失败降级不阻断。真实验证：WorkBuddy 草稿「5000积分」曾被普通来源证实（可信度模型修正后改为仅权威可确认）、「100积分（价格/政策）」等标无法核实。

**后续增强**：
- C2 证据引用层权重：权威引语/统计/可验证来源 ≈ 43% 权重，把「可验证性」做成评分硬指标
- 数据可验证性（置信度防火墙）：详见上文独立小节——管「据是不是真的」，多源交叉验证，疑似冲突即拒绝（exit 3），无法核实则显式标注不确定性
- 真实信息密度检查：缺第一人称经历/踩坑记录/缺点与质疑/具体细节的内容按缺口标记——证据权重管「内容有没有据」，这条管「内容有没有作者」，防止改写把内容润色成营销号卖点清单
- 反批量灌水：明确不提供一键批量生成；如出现需求，先加内容真实性校验再谈
- 参考 HeiGe GEU 合规护栏：防止「改写」沦为「投毒」，人工 checkpoint 不可跳过

---

## 已验证：本地浏览器全文获取通道（2026-08-06 实测通过）

**结论**：用系统自带 Edge（真实浏览器二进制）+ `--disable-blink-features=AutomationControlled` + 删除 `navigator.webdriver` + 正常浏览器 UA，headful 与 headless 均能抓取知乎文章完整正文。实测 3 篇（1.3k / 5.9k / 11k 字）全部完整，无需登录、无需第三方服务。

**对 Phase 1 旧结论的修正**：此前判定「headless 抓知乎 403」不准确——真正的钥匙是关闭自动化特征并复用本机浏览器，而不是浏览器模式本身。

**设计要点**：
- 新增 `scripts/fetch_zhihu_full.py`（可选依赖：Playwright + 本机 Edge/Chrome），`audit --full <url>` 才启用，默认仍走 API 摘要。
- 失败/风控（40362）自动降级到 API 摘要，报告标注「浏览器采集」与「摘要级」；只读 + 低频 + 请求间隔，不做 stealth 指纹伪装；批量监测不依赖此通道。
- 处理「展开全文/登录墙」（盐选、付费内容）与长文懒加载。
- 第三方托管 reader（Jina Reader 等）实测本机连不通，海外服务在国内网络不可靠，不作依赖。

**优先级**：P1（已验证、成本低）；归属 Phase 5 audit 升级，todo 已登记。
  **实现**（2026-08-11，PR #18）：新增 `scripts/fetch_zhihu_full.py`（Playwright + 本机 Edge/Chrome，关闭自动化特征 + 正常 UA，懒加载滚动后提取正文）；`audit --url <url> --full` 启用——抓取成功用完整正文评分，失败/未装 Playwright 自动降级 API 摘要并标注；报告/JSON/CLI 标注 content_source（browser / api_summary_fallback / api_summary）。真实端到端验证：知乎文章全文抓取成功（AI 可引用性 78 分）。

---

## Phase 3（进行中，未提交）：`/pulse adapt` 内容适配

已有骨架：本地 Markdown 草稿解析 → 四维草稿评分 → 知乎版 + AI 版（脚手架 + DeepSeek 重写，失败回退）。以下吸收点可直接并入：

### A1. 评分驱动改写指令（HeiGe rewrite compiler）
- **设计**：不吃固定 prompt，而是「吃打分 → 生成精确改写指令包」。指令按目标平台分叉（知乎版/AI 版），带反 AI 味约束（少空话、不 keyword stuffing），可喂给任意 LLM，不锁死 DeepSeek。
- **做法**：`content_adapter.py` 的四维评分结果序列化为 `rewrite_instructions.json`，LLM 只负责按指令改写，失败仍回退脚手架。
- **优先级**：P1（Phase 3 核心升级）。

### A2. 改写前先定位瓶颈（HeiGe 八层机制）
- **设计**：八层瓶颈 = 记忆→索引→查询→检索→重排→装配→引用→治理。一个问句没被引用，先判断卡在哪层（没收录 / 查询没匹配 / 检索没召回 / 内容没被选中），再决定是改内容还是换渠道，别上来就改文。
- **做法**：adapt 前若已有该话题的 track 数据，先输出「上次没被引用的原因假设」（未收录 vs 内容质量问题），再决定改写方向。
- **优先级**：P1。

### A3. 素材缺口清单 + 人工 checkpoint（HeiGe 诚实边界）
- **设计**：Brief/草稿输出里显式列出「缺什么素材」（缺权威引语、缺统计数据、缺来源链接），绝不编造；发布前保留人工终审标记。
- **做法**：adapt 输出尾部增加 `## 素材缺口` 小节；无 LLM 时脚手架版本同样生成。
- **优先级**：P0（诚实性红线，MVP 就要有）。

### A4. 段落级可引用性（claude-seo / geo-seo-claude）
- **设计**：AI 最容易引用的段落是 134-167 词、自包含、直接回答问题、事实密度高的答案块。整篇打分升级为逐段打分，给出「哪段最可能被引用」。
- **做法**：草稿评分在四维之上增加段落级 citability 明细；改写指令据此指定目标段落。
- **优先级**：P2（Phase 3 收尾或 Phase 5）。

---

## Phase 4：`/pulse track` 测量升级

### B1. 多采样概率闭环（HeiGe measure）★ 此前建议第 1 条
- **设计**：每问句每平台采样 N 次（默认 5），取「被提及/被引用」均值 = 被引用概率，附置信区间与各次样本原文。人工或宿主 agent 采集后喂回，形成度量闭环。
- **做法**：`track --samples 5`；DB 增加 sample 表或 runs 编号；趋势图切换为概率曲线。
- **优先级**：P1。依赖：B2 置信度标签先落地（采样数据更可信）。
  **实现**（2026-08-10，PR #13）：`track --samples N`（默认 5，1 为单次）；probes 表加 sample_idx 列（同一 run_at 的 N 行样本）；`_aggregate_samples` 多数派聚合 + Wilson 95% 置信区间；build_trend 按 run_at 聚合出概率序列（n/hits/prob/ci），变化点基于多数派判定；CLI/报告显示「是 (80%, 4/5)」；风险信号（竞品/未核实断言）保守合并任一命中；样本原文存 meta.sample_answers。

### B2. 置信度标签（Agentic-SEO-Skill）★ 此前建议第 3 条
- **设计**：每个平台结果标注 `Confirmed`（DeepSeek 真实 API）/ `Likely`（Bing 推断）/ `Hypothesis`（启发式匹配）。报告和仪表板都显示标签，避免把推断当事实。
- **做法**：track 输出 schema 增加 `confidence` 字段；CLI 摘要、snapshot、dashboard 三处同步展示。
- **优先级**：P0（成本低，直接解决现有 Bing 推断的诚实性问题，且为 B1/B3 打底）。

### B3. 引用质量分层（HeiGe v1.5）
- **设计**：从「测被不被引」升到「测引用质量 + 管被引风险」：
  - 情感分层（已有 sentiment，扩展到「被引语境」）；
  - earned / owned 拆分（原创内容被引 vs 转载/自有渠道被引）；
  - lostprompt（上次提到你、这次被竞品替换——竞品夺走分析）；
  - factcheck（AI 回答里出现关于你的错误信息）。
- **做法**：`--mine` 结果之上加 `cited_type`、`competitor_replaced` 字段；报告新增「风险」小节。
- **优先级**：P1。
  **实现**（2026-08-10，PR #12）：`--mine-owned`（转载/自有渠道）+ `--competitor`（竞品标识）参数；ProbeResult 新增 cited_type/competitor_matched/fact_risks/owned_ids 并落库；build_delta 计算 competitor_replaced（lostprompt）；DeepSeek 回答数字/版本断言提取为未核实风险（只提示不判定）；报告/CLI/JSON 新增「风险提示」（竞品夺走 + 未核实断言）。dashboard 展示待后续排期。

### B4. 变化点基线（claude-seo drift）
- **设计**：每次 run 与上次快照对比，输出「本周变化」摘要：引用新增/丢失、情感反转、首次被提及，而不是只给原始快照列表。
- **做法**：`track` 报告头部增加 `## 与上次对比` 小节；dashboard 增加变化点徽标。
- **优先级**：P1（对已有趋势数据是低成本增量）。

### B5. 国内收录/占平台检查（HeiGe 抓取逻辑）
- **设计**：国内 AI 靠百度/搜狗/博查索引喂数据。对个人创作者，检查「内容是否被这些索引收录」比检查 robots.txt 有用得多。
- **做法**：新增 `track --index-check`（百度收录自查，site: 探测）；知识库补充「国内收录三路径 + 各平台生态位」。
- **优先级**：P2（依赖网络探测策略定型）。
  **实现**（2026-08-10，PR #14）：`track --index-check <URL>`（与 --query 互斥）；Bing `site:` 探测可用（已收录/未收录）；百度探测接入但当前 UA 被反爬拦截（如实标注探测失败，解析器单测通过）；搜狗/博查待接入；知识库 docs/国内收录三路径.md（三路径 + 各平台生态位权重表）。

### B6. 平台信源推荐（HeiGe recommend）
- **设计**：把「想被豆包引用该发哪」从静态知识变成推荐：初期用 references 静态权重表（平台×信源偏好），后期用真实 track 数据校准。
- **做法**：新增 `pulse recommend --engine 豆包` 类命令，输出平台权重排序 + 来源说明。
- **优先级**：P2。
  **实现**（2026-08-11，PR #15）：`python scripts/recommend.py --engine <deepseek|doubao|tongyi|wenxin|yuanbao|all>` 输出引用源权重排序 + 内容策略 + 数据来源说明；`--url <文章URL>` 识别文章平台并输出该平台在各引擎的权重排名（可用任意文章验证）；权重数据来自 2026 年 3-5 月 16800 次查询实测（元宝为生态位估计，标注待校准）。后期用真实 track 数据校准。

---

## Phase 5：audit / adapt 评分升级

### C1. 评分卡 blocker 封顶（Seo-Prompt-Master）
- **设计**：关键阻断项一票封顶——如全站不可索引、账号零内容，总分封在低档；没有完整覆盖率不出最终分。
- **做法**：scorer 增加 `blockers[]`；有 blocker 时总分封顶并明确提示。Pulse 现有「no_key 平台不参与分母」「首次快照不显示 delta」是同一精神的延续，统一成规则。
- **优先级**：P1。
  **实现**（2026-08-11，PR #17）：scorer 增加 blockers[]（缺标题/缺正文/正文<100 字）+ extra_blockers 参数（站点级）；有 blocker 时 overall 封顶 40（C 档）并标注「已封顶」；audit 报告/JSON/CLI 与 adapt 报告/CLI 同步展示阻断原因。

### C2. 证据引用层权重（HeiGe cescore）
- **设计**：权威原文引语 + 统计数据 + 可引用性合计约 43%，是被引用第一杠杆。现有五维权重（AI可引用性 35 / 内容质量 25 / 关键词 20 / 结构 10 / 互动 10）据此校准，尤其补「证据引用」维度。
- **做法**：scorer 增加「引语/统计/来源」检测项；基准集重打分后人工校验。
- **优先级**：P1。依赖：benchmark 数据集扩充。

### C3. E-E-A-T 信任要素（claude-seo）
- **设计**：Trustworthiness 权重最高：可验证来源、日期戳、纠错透明、联系方式。先跑 Who/How/Why 启发（这篇文章是谁写的、怎么写的、为什么可信），再评子项。
- **做法**：audit 内容质量维度增加「信任要素」检查项。
- **优先级**：P2。

### C4. 并行审计（geo-seo-claude / claude-seo）
- **设计**：全站审计 5 路并行子 agent（内容/技术/平台/引用/结构），分钟级出报告。
- **做法**：Pulse 先做脚本级并行（Python concurrent 跑多个检查脚本），agent 级并行留到命令收口后。
- **优先级**：P2。

---

## Phase 6：工程与报告质量

### D1. 报告前 verifier（Agentic-SEO-Skill finding_verifier）
- **设计**：最终报告生成前过一遍 verifier：发现去重、矛盾抑制（两条建议互相打架时合并或降级）、按影响排序。
- **做法**：新增 `scripts/verifier.py`，audit/track/adapt 三处输出前统一调用；测试断言「重复发现不出现两次」。
- **优先级**：P0（直接降低 review 轮次——过去 17 轮 review 的痛点）。

### D2. 统一 rubric + output contract（Agentic-SEO-Skill llm-audit-rubric）
- **设计**：所有命令的输出结构统一为 `Finding / Evidence / Impact / Fix / Confidence / Falsifiability`，测试可断言结构。
- **做法**：`docs/design.md` 输出规格统一；snapshot JSON schema 对齐。
- **优先级**：P1。

### D3. 知识库 freshness（Agentic-SEO-Skill reference_freshness）
- **设计**：`references/` 每个文件带 `Updated: YYYY-MM-DD` 标记，CI 检查超过 90 天未更新的文件并告警。平台规则会过期，这是防止「知识腐烂」的机制。
- **做法**：`reference_freshness.py` + CI job。
- **优先级**：P1。
  **实现**（2026-08-11，PR #16）：知识文件统一 `Updated: YYYY-MM-DD` 标记（references/ 3 个 + docs/国内收录三路径.md）；`scripts/reference_freshness.py` 检查缺失/超过 90 天/未来日期，`--ci` 模式告警退出 1；接入 CI（ci.yml Knowledge freshness 步骤）；content_adapter 兼容新旧标记格式。

### D4. 双引擎同步校验（Agentic-SEO-Skill validate_skill_inventory）
- **设计**：Claude Code（SKILL.md + agents/*.md）与 Codex（agents/*.toml）命令清单、脚本引用一致性校验，防双引擎漂移。
- **做法**：`validate_skill_inventory.py` 对比 md/toml 中引用的 scripts/ 与 references/ 是否存在、命令是否对称。
- **优先级**：P2。

### D5. 可分享 HTML 报告（seo-audit-skill report-template / geo-seo-claude）
- **设计**：markdown 快照 → 零依赖自包含 HTML（可打印、可分享），企业版再上 PDF。
- **优先级**：P3。

---

## Phase 7：网站侧（面向有独立站点的创作者，可选）

### E1. agent 友好检查（HeiGe agentready）
- 检测内容是否对 AI agent 可读：登录墙、CAPTCHA、文件墙。AI agent 打不开的内容等于没发布。

### E2. Schema 检测/生成（claude-seo / geo-seo-claude / HeiGe）
- JSON-LD 校验 + deprecated 类型清单；FAQPage 实测带来 2.7 倍引用率（HeiGe 案例）。

### E3. llms.txt / robots 生成（HeiGe files）
- **诚实边界**：llms.txt 是 B2A 基础设施，不是排名杠杆（实测 97% 零请求）；只对开发者工具类内容值得做。

---

## 明确不吸收 / 缓吸收

- **CRM-lite、客户提案、月度报告**（geo-seo-claude）：企业代理向，与「个人创作者优先」矛盾，留企业版。
- **Google GSC / PageSpeed / hreflang / programmatic SEO 全套**（claude-seo）：Google 向 + 站点规模化，Pulse 是中国优先的个人创作者工具。
- **24 行业覆盖层**（Seo-Prompt-Master）：等核心闭环跑通，再挑 2-3 个高频行业做 vertical。
- **全站爬虫式技术 SEO 套件**：Pulse 定位是内容可见度，不是 Ahrefs/Semrush 替代品（HeiGe 自己的定位说明同样如此）。

---

## 推荐落地顺序

1. **现在（Phase 3 内，低成本高回报）**：B2 置信度标签、A3 素材缺口清单、D1 报告 verifier。
2. **Phase 4 主打**：B1 多采样概率、B3 引用质量分层、B4 变化点基线、A1 评分驱动改写。
3. **中期**：C2 证据引用层权重、A4 段落级可引用性、D3 知识库 freshness、C1 blocker 封顶。
4. **后期/可选**：B5 国内收录检查、B6 平台信源推荐、C4 并行审计、D4 双引擎校验、Phase 7 网站侧。
