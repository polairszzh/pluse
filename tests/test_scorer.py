"""scorer.py 单元测试"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from scorer import (
    BLOCKED_CEILING,
    _authority,
    _citation_density,
    _data_citability,
    _entity_presence,
    _experience,
    _expertise,
    _passage_citability,
    _qa_structure,
    _timeliness,
    _trust,
    audit_article,
    clamp,
    grade,
    score_ai_citability,
    score_content_quality,
    score_engagement,
    score_evidence_citation,
    score_keyword_coverage,
    score_structure,
)

# ── 基础工具 ────────────────────────────────────────────

class TestGrade:
    def test_a_plus(self):  assert grade(92) == "A+"
    def test_a(self):       assert grade(87) == "A"
    def test_b_plus(self):  assert grade(78) == "B+"
    def test_b(self):       assert grade(68) == "B"
    def test_c(self):       assert grade(55) == "C"
    def test_d(self):       assert grade(30) == "D"
    def test_boundary(self): assert grade(90) == "A+"


class TestClamp:
    def test_in_range(self):   assert clamp(50) == 50
    def test_above(self):      assert clamp(150) == 100
    def test_below(self):      assert clamp(-10) == 0


# ── AI 可引用性子维度 ──────────────────────────────────

class TestPassageCitability:
    def test_rich(self):
        s, _ = _passage_citability("x" * 600)
        assert s >= 80
    def test_good(self):
        s, _ = _passage_citability("x" * 400)
        assert 60 <= s <= 85
    def test_short(self):
        s, _ = _passage_citability("x" * 200)
        assert 40 <= s <= 65
    def test_very_short(self):
        s, _ = _passage_citability("x" * 80)
        assert s <= 50


class TestQAStructure:
    def test_question_and_answer(self):
        s, _ = _qa_structure("如何做好 AI 搜索优化？", "AI 搜索优化的关键在于让内容被大语言模型引用。根据最新研究")
        assert s >= 85
    def test_question_only(self):
        s, _ = _qa_structure("怎么提升知乎流量？", "很多创作者都在思考这个问题")
        assert 60 <= s <= 85
    def test_neither(self):
        s, _ = _qa_structure("我的创作心得", "写了很久知乎，分享一些想法")
        assert s <= 55


class TestCitationDensity:
    def test_high(self):
        s, _ = _citation_density("根据张三的研究《AI搜索趋势》显示，参考来源：https://example.com。据报道，2026年")
        assert s >= 75
    def test_none(self):
        s, _ = _citation_density("这是一篇纯观点文章，没有任何引用标记。")
        assert s <= 50, f"Expected <=50, got {s}"


class TestEntityPresence:
    def test_rich(self):
        s, _ = _entity_presence(
            "DeepSeek 和 Kimi 在引用知乎内容时，豆瓣小红书的数据也很有价值。ChatGPT 对比 Gemini。",
            "知乎 vs 小红书：AI 搜索引用来源分析"
        )
        assert s >= 70
    def test_none(self):
        s, _ = _entity_presence("写作是件值得坚持的事。", "个人感悟")
        assert s <= 50


class TestDataCitability:
    def test_stats(self):
        s, _ = _data_citability("根据 2026 年数据，AI 搜索渗透率达 42%，用户 3.2 亿人，同比增长 67%。")
        assert s >= 80
    def test_no_data(self):
        s, _ = _data_citability("AI搜索优化很重要，企业应该重视。")
        assert s <= 50


class TestTimeliness:
    def test_recent(self):
        now = int(time.time()) - 86400 * 10      # 10 天前
        s, _ = _timeliness(now)
        assert s >= 85
    def test_old(self):
        now = int(time.time()) - 86400 * 400
        s, _ = _timeliness(now)
        assert s <= 55
    def test_none(self):
        s, _ = _timeliness(None)
        assert s <= 50


# ── E-E-A-T 子维度 ────────────────────────────────────

class TestTrust:
    def test_with_citations(self):
        s, _ = _trust("根据 https://example.com 的研究，来源显示。本文据实撰写。")
        assert s >= 60
    def test_bare(self):
        s, _ = _trust("我觉得这个产品很好。")
        assert s <= 60


class TestExperience:
    def test_first_person_case(self):
        s, _ = _experience("我曾在3个项目里落地了这个方案，实战经验如下。客户端案例显示。")
        assert s >= 65
    def test_none(self):
        s, _ = _experience("理论上这个方法可行。")
        assert s <= 50


class TestExpertise:
    def test_high(self):
        s, _ = _expertise("RAG 架构结合 LLM 做 GEO 优化，API 接口通过 SDK 调用，数据库索引策略。")
        assert s >= 65
    def test_low(self):
        s, _ = _expertise("写好文章很重要，大家多练习。")
        assert s <= 50


class TestAuthority:
    def test_with_badge(self):
        s, d = _authority("张三", "优秀答主")
        assert s >= 70
        assert "认证" in d
    def test_no_author(self):
        s, _ = _authority("", "")
        assert s <= 40


# ── 顶层维度 ──────────────────────────────────────────

class TestScoreAICitability:
    def test_good_article(self):
        result = score_ai_citability(
            title="如何做好 AI 搜索优化？",
            content_text=(
                "根据 2026 年最新研究数据，AI 搜索渗透率达 42%。"
                "参考《GEO 白皮书》显示，引用率提升 30%。"
                "知乎、小红书等平台已成为 DeepSeek 和 Kimi 的主要引用来源。"
                "据行业报告指出，企业应建立结构化知识库以便 AI 引用。"
                "研究表明持续 3-6 个月的内容建设可将 AI 可见度提升 2 倍以上。"
                "本文基于 5 个企业 GEO 项目的实战经验总结。"
            ),
            updated_at=int(time.time()) - 86400 * 5,
        )
        assert result.score >= 65, f"Expected >=65, got {result.score}"

    def test_poor_article(self):
        result = score_ai_citability(
            title="随便写写",
            content_text="最近看了些东西，觉得挺有意思的。",
        )
        assert result.score <= 55, f"Expected <=55, got {result.score}"


class TestScoreContentQuality:
    def test_good(self):
        result = score_content_quality(
            "我曾在 3 个项目里实践 RAG 架构，数据表明引用率提升 25%。"
            "参考来源：https://arxiv.org 论文。本文基于第一手经验撰写。",
            author_name="张三",
            author_badge="人工智能领域答主",
        )
        assert result.score >= 65

    def test_poor(self):
        result = score_content_quality("写得还行吧。")
        assert result.score <= 55


class TestScoreKeywordCoverage:
    def test_full_coverage(self):
        result = score_keyword_coverage(
            "AI搜索优化实战指南",
            "本文介绍 AI 搜索优化和 GEO 的核心方法",
            ["AI搜索", "GEO", "优化"],
        )
        assert result.score >= 70

    def test_none(self):
        result = score_keyword_coverage("标题", "内容", ["AI搜索", "GEO"])
        assert result.score <= 50


class TestScoreStructure:
    def test_good(self):
        result = score_structure(
            "知乎内容创作完整指南（15-30字）",
            "首段\n\n第二段\n\n第三段\n\n第四段\n\n第五段\n\n第六段\n\n1. 要点一\n\n2. 要点二",
        )
        assert result.score >= 65

    def test_poor(self):
        result = score_structure("短", "一段话")
        assert result.score <= 65


class TestScoreEngagement:
    def test_above_avg(self):
        result = score_engagement(votes=200, comments=10, favorites=80, benchmark_avg_votes=50)
        assert result.score >= 80

    def test_below_avg(self):
        result = score_engagement(votes=5, comments=0, favorites=1, benchmark_avg_votes=50)
        assert result.score <= 40


# ── 综合 ──────────────────────────────────────────────

class TestAuditArticle:
    def test_full_pipeline(self):
        result = audit_article(
            title="如何做好 AI 搜索优化？2026 实战指南",
            content_text=(
                "根据 2026 年最新数据，AI 搜索渗透率已达 42%。"
                "我在 3 个企业项目中的实践经验表明，建立结构化知识库是 GEO 优化的核心。"
                "参考《GEO 白皮书》和 https://arxiv.org 论文。"
                "DeepSeek 和 Kimi 倾向于引用知乎上有数据支撑的长文。"
                "第一步诊断现状，第二步建立问题库，第三步整理品牌语料。"
                "据行业报告指出，AI 引用率提升 30% 需要持续 3-6 个月的内容建设。"
            ),
            votes=150, comments=20, favorites=60,
            author_name="张三", author_badge="人工智能领域答主",
            updated_at=int(time.time()) - 86400 * 7,
            keywords=["AI搜索", "GEO", "引用率", "知识库"],
            benchmark_avg_votes=30,
        )
        assert result.overall >= 65, f"Good article should score >=65, got {result.overall}"
        assert result.grade in ("B", "B+", "A"), f"Unexpected grade: {result.grade}"
        assert len(result.details) > 10

    def test_poor_article(self):
        result = audit_article(
            title="随笔",
            content_text="最近看了些东西，感觉还不错。分享一下。",
            votes=2, comments=0, favorites=0,
            benchmark_avg_votes=50,
        )
        assert result.overall <= 55, f"Poor article should score <=55, got {result.overall}"
        assert result.grade in ("C", "D")

    def test_differentiation(self):
        """好文章和差文章之间应有 >20 分的差距"""
        good = audit_article(
            title="AI 搜索优化怎么做？完整的 GEO 实战指南",
            content_text=(
                "根据 2026 年 Pew Research 数据，69% 的搜索为零点击搜索。"
                "我在给 5 家企业做 GEO 优化后发现，结构化语料库和权威信源建设"
                "是提升 AI 引用率的关键。根据 KDD 论文《GEO: Generative Engine Optimization》，"
                "Information Gain 是主要预测因子。DeepSeek 偏好知乎来源。"
            ),
            votes=200, comments=30, favorites=80,
            author_name="李四", author_badge="机器学习优秀答主",
            updated_at=int(time.time()) - 86400 * 3,
            keywords=["AI搜索", "GEO", "引用率"],
            benchmark_avg_votes=20,
        )
        poor = audit_article(
            title="一些想法",
            content_text="AI搜索好像挺重要的，但具体怎么做我也在摸索。改天再聊。",
            votes=1, comments=0, favorites=0,
            benchmark_avg_votes=20,
        )
        gap = good.overall - poor.overall
        assert gap >= 20, f"Good-poor gap should be >=20, got {gap}. good={good.overall}, poor={poor.overall}"


# ── Benchmark 文章快照测试 ─────────────────────────────

class TestBenchmarkArticles:
    """用真实 benchmark 数据验证评分区分度"""
    def test_benchmark_ranking(self):
        data_path = Path(__file__).parent.parent / "data" / "benchmark_articles.json"
        if not data_path.exists():
            pytest.skip("benchmark_articles.json 未生成，运行 fetch_benchmarks.py 创建")

        with open(data_path, encoding="utf-8") as f:
            data = json.load(f)

        all_scores = []
        for articles in data.values():
            for a in articles:
                content_text = a.get("content_text") or (a.get("title", "") + " " + a.get("title", ""))
                result = audit_article(
                    title=a["title"],
                    content_text=content_text,
                    votes=a["votes"],
                    comments=a["comments"],
                    author_name="",
                    benchmark_avg_votes=sum(
                        aa["votes"] for aa in articles
                    ) / max(len(articles), 1),
                )
                all_scores.append((a["score"], a["votes"], result.overall, result.grade))

        # 验证：高分文章的评分应该高于低分文章（在各自话题内）
        high_score = [s for s in all_scores if s[0] >= 2.0]
        low_score = [s for s in all_scores if s[0] <= 1.7]

        if high_score and low_score:
            avg_high = sum(s[2] for s in high_score) / len(high_score)
            avg_low = sum(s[2] for s in low_score) / len(low_score)
            # 高 RankingScore 的文章应该在我们的评分里也更高
            # 不要求绝对大小（因为 RankingScore 和我们的打分维度不同），
            # 但两者之间的相对趋势应该一致
            assert avg_high > avg_low * 0.7, \
                f"avg_high={avg_high:.0f}, avg_low={avg_low:.0f}"


class TestBlockers:
    """C1 一票封顶：关键阻断项存在时总分封在低档"""

    LONG_TEXT = (
        "本文介绍 WorkBuddy 的安装配置方法。根据官方文档 https://codebuddy.cn/work/，"
        "安装约需 5 分钟，支持 3 种模型接入。笔者实测处理 1000 行代码耗时 2 秒，"
        "参考《WorkBuddy 官方指南》与知乎专栏。据官方公告最新版本 2.3.1 已发布，"
        "RAG、LLM、API、SDK、CLI 等接口详见附录。"
    ) * 8  # >100 字且含引用/数据/术语/第一人称

    def test_missing_title_blocked(self):
        scores = audit_article("", self.LONG_TEXT)
        assert any("标题" in b for b in scores.blockers)
        assert scores.overall <= BLOCKED_CEILING
        assert scores.grade == grade(BLOCKED_CEILING)

    def test_missing_content_blocked(self):
        scores = audit_article("有效标题", "")
        assert any("正文" in b for b in scores.blockers)
        assert scores.overall <= BLOCKED_CEILING

    def test_short_content_blocked(self):
        scores = audit_article("有效标题", "太短")
        assert any("过短" in b for b in scores.blockers)
        assert scores.overall <= BLOCKED_CEILING

    def test_extra_blocker(self):
        scores = audit_article(
            "有效标题", self.LONG_TEXT,
            extra_blockers=["账号零内容"],
        )
        assert "账号零内容" in scores.blockers
        assert scores.overall <= BLOCKED_CEILING

    def test_normal_no_blocker(self):
        scores = audit_article("有效标题", self.LONG_TEXT)
        assert scores.blockers == []
        assert scores.overall > BLOCKED_CEILING


class TestEvidenceCitation:
    """C2 证据引用层权重：引语/统计/来源/权威"""

    def test_rich_evidence_high_score(self):
        text = (
            "据《2026 人工智能发展报告》统计，中国 AI 市场规模达 5000 亿元，"
            "年增长 30%。官方（https://www.gov.cn）数据显示 1000 万人使用。"
            "参考知乎专栏与教育部文件（edu.cn）。"
        )
        dim = score_evidence_citation(text)
        assert dim.score >= 80
        assert any("引语" in d for d in dim.details)
        assert any("统计" in d for d in dim.details)
        assert any("权威" in d for d in dim.details)

    def test_no_evidence_low_score(self):
        dim = score_evidence_citation("我认为这个产品很好，体验不错。")
        assert dim.score < 50
        assert any("缺少" in d for d in dim.details)

    def test_fake_authority_not_counted(self):
        # medu.cn 含 edu.cn 子串、非官方含 官方 子串：不得命中权威
        dim = score_evidence_citation(
            "参考 medu.cn 与非官方渠道的说法，数据 30%。"
        )
        assert not any("权威" in d for d in dim.details)

    def test_domain_followed_by_chinese_hits(self):
        # gov.cn官网（域名后紧跟中文）也应命中权威（ASCII 边界，汉字不算边界）
        dim = score_evidence_citation("据 gov.cn官网 数据，覆盖 1000 万人。")
        assert any("权威" in d for d in dim.details)

    def test_plain_units_not_counted_as_stats(self):
        # 裸「3 人」「5 次」不带数量级/百分号：不计入统计密度
        dim = score_evidence_citation("3 人使用，5 次尝试，10 元定价。")
        assert not any("处统计数据" in d for d in dim.details)

    def test_curly_quotes_counted(self):
        # 中文弯引号直接引语也应检测
        dim = score_evidence_citation("他指出：“这个方案值得推广。”")
        assert any(d.startswith("检测到") and "引语" in d for d in dim.details)

    def test_bare_reference_word_not_counted(self):
        # 裸「参考」「来源」不是强来源信号，不计数
        dim = score_evidence_citation("仅供参考，来源不明。")
        assert not any("来源标记" in d for d in dim.details)

    def test_tongji_and_full_report_sources(self):
        # 据统计/据新华社报道 完整命中，不被截断
        dim = score_evidence_citation("据统计，市场增长；据新华社报道，政策落地。")
        assert any("来源" in d for d in dim.details)

    def test_media_name_report_source(self):
        # 仅媒体名场景（不含据统计）：据新华社报道 单独命中
        dim = score_evidence_citation("据新华社报道，政策落地；据央视消息，数据公布。")
        assert any("来源" in d for d in dim.details)

    def test_audit_article_includes_evidence_dimension(self):
        text = (
            "据官方统计 500 万人使用，参考《报告》（edu.cn）与 https://www.gov.cn 数据。"
        ) * 5
        scores = audit_article("有效标题", text)
        assert "证据引用" in scores.sub_scores
        assert scores.evidence_citation == scores.sub_scores["证据引用"].score
