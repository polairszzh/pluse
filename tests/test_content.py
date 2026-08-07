"""content_adapter.py 单元测试 —— LLM 调用全部 mock，不触网"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import requests
import search_ai
from content_adapter import (
    _fallback_ai_version,
    _fallback_zhihu_version,
    _first_150,
    _query_keywords,
    _rewrite_instructions,
    _rewrite_triggers,
    _scan_facts,
    _split_paragraph,
    _text_without_code,
    detect_material_gaps,
    detect_promotional_signals,
    generate_ai_version,
    generate_zhihu_version,
    main,
    parse_markdown,
    score_draft,
)

SAMPLE_DRAFT = """---
title: WorkBuddy 入门
---
# WorkBuddy 是什么？有什么用

WorkBuddy 是一款 AI 工作流自动化工具，可以连接多种 AI 服务，把重复任务自动化。
它主要解决手动切换多个 AI 工具、复制粘贴上下文的问题。

## WorkBuddy 有哪些优势

优势一：节省时间，重复任务一键跑完。优势二：配置简单，几分钟就能上手。

## 怎么安装和配置 WorkBuddy

先下载安装包，然后运行安装命令：

```
pip install workbuddy
```

![架构图](images/arch.png)

配置完成后即可使用，支持导入导出工作流。
"""


class FakeResponse:
    def __init__(self, data=None):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class TestParse:
    def test_basic(self):
        doc = parse_markdown(SAMPLE_DRAFT)
        assert doc.title == "WorkBuddy 是什么？有什么用"
        assert doc.topics == ["WorkBuddy 有哪些优势", "怎么安装和配置 WorkBuddy"]
        assert len(doc.sections) == 2
        assert doc.intro and "WorkBuddy" in doc.intro[0]
        assert doc.images == ["images/arch.png"]
        assert doc.code_blocks == ["pip install workbuddy"]
        assert "WorkBuddy" in doc.body_text

    def test_empty(self):
        doc = parse_markdown("")
        assert doc.title == "untitled"
        assert doc.topics == []
        assert doc.sections == []

    def test_intro_keeps_image_and_code_before_first_h2(self):
        doc = parse_markdown(
            "# 标题\n\n"
            "![架构图](images/arch.png)\n\n"
            "```\n"
            "pip install workbuddy\n"
            "```\n\n"
            "引言正文。\n\n"
            "## 章节\n\n"
            "内容。\n"
        )
        assert doc.images == ["images/arch.png"]
        assert doc.code_blocks == ["pip install workbuddy"]
        assert any("![架构图]" in ln for ln in doc.intro)
        assert any("pip install workbuddy" in ln for ln in doc.intro)
        # 脚手架回退不丢引言区图片/代码
        md = _fallback_zhihu_version(doc)
        assert "![架构图](images/arch.png)" in md
        assert "pip install workbuddy" in md
        # 引言区代码块独立成块，不被空格拼成行内文本
        assert "```\npip install workbuddy\n```" in md
        assert "``` pip install" not in md

    def test_h1_after_h2_starts_new_section(self):
        doc = parse_markdown("# 标题\n\n## 优势\n\n内容A。\n\n# 新标题\n\n内容B。\n")
        assert doc.title == "标题"
        assert doc.topics == ["优势"]
        assert doc.sections == [("优势", ["内容A。"]), ("新标题", ["内容B。"])]


class TestScore:
    def test_draft_score_shape(self):
        s = score_draft("WorkBuddy 是什么", SAMPLE_DRAFT, ["workbuddy"])
        assert s["engagement"]["status"] == "未发布"
        assert set(s["dimensions"]) == {
            "AI 可引用性", "内容质量 (E-E-A-T)", "关键词覆盖", "结构与格式",
        }
        assert 0 <= s["overall"] <= 100
        assert all(r["falsifiability_check"] for r in s["recommendations"])
        assert all(r["priority"] in ("P0", "P1", "P2") for r in s["recommendations"])

    def test_query_keywords_splits_terms(self):
        assert _query_keywords("workbuddy 安装配置教程") == [
            "workbuddy 安装配置教程", "安装", "配置", "教程",
        ]
        assert _query_keywords("") == []
        assert _query_keywords("workbuddy") == ["workbuddy"]

    def test_keyword_score_improves_with_tokenized_query(self):
        draft = (
            "# WorkBuddy 是什么\n\n"
            "WorkBuddy 是腾讯云桌面 AI 智能体。\n\n"
            "## 怎么安装配置 WorkBuddy\n\n"
            "第一步下载安装包，第二步配置模型。\n"
        )
        whole = score_draft("WorkBuddy 是什么", draft, ["workbuddy 安装配置教程"])
        tokenized = score_draft("WorkBuddy 是什么", draft, _query_keywords("workbuddy 安装配置教程"))
        assert tokenized["dimensions"]["关键词覆盖"] > whole["dimensions"]["关键词覆盖"]
        assert tokenized["dimensions"]["关键词覆盖"] > 40

    def test_keyword_in_code_not_counted(self):
        draft = "# X\n\n正文没有目标词。\n\n```\nworkbuddy secret\n```\n"
        s = score_draft("X", draft, ["workbuddy"])
        assert s["dimensions"]["关键词覆盖"] == 10


class TestMaterialGaps:
    def test_bad_draft_all_gap_types(self):
        doc = parse_markdown(
            "# 摸鱼神器 WorkBuddy\n\n"
            "![图](https://picsum.photos/800/400)\n\n"
            "官网 [workbuddy.ai](https://www.workbuddy.ai/) 下载。\n"
        )
        gaps = detect_material_gaps(doc, "workbuddy 安装配置教程")
        types = {g["type"] for g in gaps}
        assert "placeholder_image" in types
        assert "unverified_links" in types
        assert "query_coverage_missing" in types
        assert "image_alt_missing" in types
        # 严重度排序：high 在前
        assert gaps[0]["severity"] == "high"
        by_type = {g["type"]: g for g in gaps}
        assert "picsum.photos" in by_type["placeholder_image"]["detail"]
        assert "workbuddy.ai" in by_type["unverified_links"]["detail"]
        assert "安装" in by_type["query_coverage_missing"]["detail"]

    def test_query_coverage_ok(self):
        doc = parse_markdown(
            "# WorkBuddy 教程\n\n"
            "## 安装步骤\n\n"
            "第一步下载安装包，然后配置。\n\n"
            "![架构图](images/arch.png)\n"
        )
        gaps = detect_material_gaps(doc, "workbuddy 安装配置教程")
        assert all(g["type"] != "query_coverage_missing" for g in gaps)
        assert all(g["type"] != "placeholder_image" for g in gaps)
        assert all(g["type"] != "image_alt_missing" for g in gaps)

    def test_no_duplicates(self):
        doc = parse_markdown(
            "# 测试\n\n![图](https://picsum.photos/a)\n![图2](https://picsum.photos/b)\n"
        )
        gaps = detect_material_gaps(doc)
        placeholder = [g for g in gaps if g["type"] == "placeholder_image"]
        assert len(placeholder) == 2  # 不同 URL 不合并
        assert len(gaps) == len({(g["type"], g["detail"]) for g in gaps})

    def test_code_blocks_ignored(self):
        doc = parse_markdown(
            "# WorkBuddy\n\n"
            "正文没有链接、数字和关键词。\n\n"
            "```\n"
            "curl https://example.com/x -o out && echo '100% done in 5 minutes'\n"
            "install --setup\n"
            "```\n"
        )
        gaps = detect_material_gaps(doc, "workbuddy 安装配置教程")
        # 代码里的 URL 不报待核实链接
        assert all(g["type"] != "unverified_links" for g in gaps)
        # 代码里的 install 不算「安装」章节覆盖 → 「安装」仍报缺失
        assert any(g["type"] == "query_coverage_missing" for g in gaps)
        assert any("安装" in g["detail"] for g in gaps if g["type"] == "query_coverage_missing")
        # 代码里的数字不进入事实核对清单
        facts = _scan_facts(_text_without_code(doc.raw))
        assert facts == []

    def test_promotional_high_detected(self):
        sigs = detect_promotional_signals(
            "这门课扫码购买就能领，必买！手慢无，限时抢购。"
        )
        assert any(s["severity"] == "high" for s in sigs)
        patterns = {s["pattern"] for s in sigs}
        assert "扫码购买" in patterns or "扫码" in patterns
        assert "必买" in patterns
        assert any("手慢无" == s["pattern"] for s in sigs)

    def test_promotional_medium_detected(self):
        sigs = detect_promotional_signals("强烈推荐这个网站，全网第一的教程。")
        assert all(s["severity"] == "medium" for s in sigs)
        assert any("强烈推荐" == s["pattern"] for s in sigs)
        assert any("全网第一" == s["pattern"] for s in sigs)

    def test_normal_recommendation_no_false_positive(self):
        text = (
            "WorkBuddy 是我用过比较好用的桌面 AI 工具。"
            "它支持本地文件操作和定时任务，下载地址 codebuddy.cn。"
            "以上是个人使用体验分享，不构成任何购买建议。"
        )
        assert detect_promotional_signals(text) == []

    def test_promotional_signals_enter_gaps(self):
        doc = parse_markdown(
            "# 推荐一个课程\n\n"
            "这个课程必买，扫码下单立减 50！\n"
        )
        gaps = detect_material_gaps(doc)
        promo = [g for g in gaps if g["type"] == "promotional_signal"]
        assert promo
        assert any(g["severity"] == "high" for g in promo)
        assert any("扫码" in g["detail"] for g in promo)


class TestReviewChecklist:
    def test_scan_facts(self):
        facts = _scan_facts(
            "新用户领 5000 积分，每天签到 100 积分，10 分钟出初稿，"
            "建议 8GB 内存，支持 Windows 10/11 和 macOS 10.15+。"
        )
        assert "5000积分" in facts
        assert "100积分" in facts
        assert "10分钟" in facts
        assert "8GB" in facts
        # 无单位的版本号不列入
        assert all("10/11" not in f and "10.15" not in f for f in facts)

    def test_text_without_code_unclosed(self):
        assert _text_without_code("正文 ok\n```\ncurl https://x\n") == "正文 ok"
        assert _text_without_code("正文 ok\n```\ncurl https://x\n```\n正文尾") == "正文 ok\n正文尾"


class TestRewriteInstructions:
    def test_low_keyword_forces_title_rule(self):
        ins = _rewrite_instructions(
            {"关键词覆盖": 10, "AI 可引用性": 78, "内容质量 (E-E-A-T)": 58, "结构与格式": 80},
            "workbuddy 安装教程",
        )
        assert "标题必须包含目标关键词" in ins
        assert "workbuddy 安装教程" in ins

    def test_low_citability_forces_answer_block(self):
        ins = _rewrite_instructions(
            {"关键词覆盖": 90, "AI 可引用性": 40, "内容质量 (E-E-A-T)": 90, "结构与格式": 90},
        )
        assert "自包含答案块" in ins

    def test_high_scores_light_edit(self):
        ins = _rewrite_instructions(
            {"关键词覆盖": 90, "AI 可引用性": 90, "内容质量 (E-E-A-T)": 90, "结构与格式": 90},
        )
        assert "轻度润色" in ins
        assert "【强制" not in ins

    def test_none_dims_empty(self):
        assert _rewrite_instructions(None) == ""

    def test_low_quality_mentions_html_comment_not_visible_section(self):
        ins = _rewrite_instructions(
            {"关键词覆盖": 90, "AI 可引用性": 90, "内容质量 (E-E-A-T)": 40, "结构与格式": 90},
        )
        assert "HTML 注释" in ins
        assert "【素材缺口】" not in ins

    def test_low_quality_forces_third_person(self):
        ins = _rewrite_instructions(
            {"关键词覆盖": 90, "AI 可引用性": 90, "内容质量 (E-E-A-T)": 40, "结构与格式": 90},
        )
        assert "第三人称" in ins
        assert "不使用第一人称" in ins

    def test_rewrite_triggers_ai_citability_70(self):
        dims = {"关键词覆盖": 90, "AI 可引用性": 65, "内容质量 (E-E-A-T)": 90, "结构与格式": 90}
        assert _rewrite_triggers(dims) == ["AI 可引用性"]
        assert "自包含答案块" in _rewrite_instructions(dims)  # 触发条件与 manifest 记录一致
        dims_ok = {"关键词覆盖": 90, "AI 可引用性": 72, "内容质量 (E-E-A-T)": 90, "结构与格式": 90}
        assert _rewrite_triggers(dims_ok) == []
        assert "自包含答案块" not in _rewrite_instructions(dims_ok)

    def test_first_150_breaks_at_sentence(self):
        short = "只有一句。"
        assert _first_150(short) == (short, "")
        long_text = "这是第一句。" + "这是第二句很长。" * 30
        first, rest = _first_150(long_text)
        assert len(first) <= 150
        assert first.endswith("。")  # 不在句中截断
        assert rest  # 剩余保留
        assert first + rest == long_text


class TestFallback:
    def test_ai_version_structure(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        doc = parse_markdown(SAMPLE_DRAFT)
        md, used = generate_ai_version(doc)
        assert used is False
        assert md.startswith("# ")
        assert "## " in md
        assert "![" not in md  # AI 优化版不用图
        # 话题 H2 转为问答句
        assert "WorkBuddy 有哪些优势？" in md

    def test_zhihu_version_keeps_code_and_images(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        doc = parse_markdown(SAMPLE_DRAFT)
        md, used = generate_zhihu_version(doc)
        assert used is False
        assert "pip install workbuddy" in md
        assert "![架构图](images/arch.png)" in md
        assert "## WorkBuddy 有哪些优势" in md
        assert "写在最后" in md

    def test_zhihu_fallback_keeps_code_block_intact(self):
        doc = parse_markdown(
            "# 标题\n\n"
            "## 安装步骤\n\n"
            "先运行下面的命令：\n\n"
            "```\n"
            "pip install workbuddy --upgrade\n"
            "workbuddy config --init\n"
            "```\n\n"
            "然后重启。\n"
        )
        md = _fallback_zhihu_version(doc)
        block = md.split("## 安装步骤")[1]
        # 代码围栏连续：代码行不被切分、不被空行隔开
        assert "```\npip install workbuddy --upgrade\nworkbuddy config --init\n```" in block

    def test_split_paragraph(self):
        parts = _split_paragraph("第一句。第二句很长。" * 40, 180)
        assert len(parts) > 1
        assert all(len(p) <= 180 for p in parts)

    def test_fallback_ai_uses_topics(self):
        doc = parse_markdown(SAMPLE_DRAFT)
        md = _fallback_ai_version(doc)
        assert "它是什么？" in md
        assert "WorkBuddy 是一款 AI 工作流自动化工具" in md
        assert "WorkBuddy 有哪些优势？" in md
        assert "怎么安装和配置 WorkBuddy？" in md

    def test_fallback_ai_merges_duplicate_heading(self):
        doc = parse_markdown(
            "# 标题\n\n"
            "## 优势\n\n"
            "优势内容一。\n\n"
            "## 优势\n\n"
            "优势内容二。\n"
        )
        md = _fallback_ai_version(doc)
        assert "优势内容一。" in md
        assert "优势内容二。" in md
        assert md.count("## 优势？") == 1  # 同一标题只输出一个问答块，内容合并

    def test_fallback_ai_no_h2_uses_full_intro(self):
        long_intro = "WorkBuddy 是腾讯云推出的桌面 AI 智能体工作台。" * 20
        doc = parse_markdown(f"# 无 H2 草稿\n\n{long_intro}\n")
        md = _fallback_ai_version(doc)
        assert "它是什么？" in md
        assert long_intro in md  # 引言全文保留，不截断
        assert "核心内容：" not in md  # 无默认话题占位句
        assert "有哪些优势" not in md

    def test_fallback_ai_intro_keeps_rest_and_strips_images(self):
        tail = "这是很长的补充内容，用于验证引言后半段不会被丢弃。"
        doc = parse_markdown(
            "# 标题\n\n"
            "![示意图](https://x.com/a.png)\n\n"
            f"第一句。{tail * 8}\n\n"
            "## WorkBuddy 有哪些优势\n\n"
            "优势一。\n"
        )
        md = _fallback_ai_version(doc)
        assert "![示意图](https://x.com/a.png)" not in md  # 引言图片被去掉
        assert tail in md  # 引言 150 字之后的内容仍保留

    def test_fallback_zhihu_intro_has_no_code(self):
        doc = parse_markdown(SAMPLE_DRAFT)
        md = _fallback_zhihu_version(doc)
        intro_para = md.split("## ")[0]
        assert "pip install" not in intro_para


class TestLLM:
    def test_llm_used_when_available(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        payload = {"choices": [{"message": {"content": "# AI 优化版\n\n## WorkBuddy 是什么？\n\n内容"}}]}
        monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResponse(payload))
        doc = parse_markdown(SAMPLE_DRAFT)
        md, used = generate_ai_version(doc)
        assert used is True
        assert md.startswith("# AI 优化版")

    def test_llm_failure_falls_back(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")

        def boom(*a, **kw):
            raise requests.exceptions.ConnectionError("down")

        monkeypatch.setattr(requests, "post", boom)
        doc = parse_markdown(SAMPLE_DRAFT)
        md, used = generate_zhihu_version(doc)
        assert used is False
        assert "写在最后" in md

    def test_llm_attribute_error_falls_back(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")

        def bad_shape(*a, **kw):
            # choices 为字符串时 data.get 抛 AttributeError，必须回退而非崩溃
            return FakeResponse({"choices": "notalist"})

        monkeypatch.setattr(requests, "post", bad_shape)
        doc = parse_markdown(SAMPLE_DRAFT)
        md, used = generate_zhihu_version(doc)
        assert used is False
        assert "写在最后" in md

    def test_zhihu_llm_gets_score_instructions(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        captured: dict = {}

        def fake_post(*a, **kw):
            captured["payload"] = kw["json"]
            return FakeResponse({"choices": [{"message": {"content": "# 改"}}]})

        monkeypatch.setattr(requests, "post", fake_post)
        doc = parse_markdown(SAMPLE_DRAFT)
        generate_zhihu_version(
            doc,
            {"关键词覆盖": 10, "AI 可引用性": 78, "内容质量 (E-E-A-T)": 58, "结构与格式": 80},
            "workbuddy 安装教程",
        )
        system = captured["payload"]["messages"][0]["content"]
        assert "改写强度较高" in system
        assert "标题必须包含目标关键词" in system
        assert "HTML 注释" in system
        assert "【素材缺口】" not in system

    def test_ai_llm_does_not_force_visible_gap_section(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        captured: dict = {}

        def fake_post(*a, **kw):
            captured["payload"] = kw["json"]
            return FakeResponse({"choices": [{"message": {"content": "# 改"}}]})

        monkeypatch.setattr(requests, "post", fake_post)
        doc = parse_markdown(SAMPLE_DRAFT)
        generate_ai_version(
            doc,
            {"关键词覆盖": 10, "AI 可引用性": 78, "内容质量 (E-E-A-T)": 58, "结构与格式": 80},
            "workbuddy 安装教程",
        )
        system = captured["payload"]["messages"][0]["content"]
        assert "【素材缺口】" not in system
        assert "HTML 注释" in system

    def test_llm_system_injects_content_format_rules(self, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        captured: dict = {}

        def fake_post(*a, **kw):
            captured["payload"] = kw["json"]
            return FakeResponse({"choices": [{"message": {"content": "# 改"}}]})

        monkeypatch.setattr(requests, "post", fake_post)
        doc = parse_markdown(SAMPLE_DRAFT)
        generate_zhihu_version(doc)
        system = captured["payload"]["messages"][0]["content"]
        assert "多平台内容格式对照表" in system
        assert "content-format.md" in system


class TestCLI:
    def test_cli_end_to_end(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        src = tmp_path / "draft.md"
        src.write_text(SAMPLE_DRAFT, encoding="utf-8")
        out = tmp_path / "out"
        code = main(["--source", str(src), "--no-llm", "--output", str(out)])
        assert code == 0
        ai_files = list(out.glob("ai-*.md"))
        zhihu_files = list(out.glob("zhihu-*.md"))
        manifests = list(out.glob("adapt-*.json"))
        assert len(ai_files) == 1
        assert len(zhihu_files) == 1
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        assert isinstance(manifest["source_chars"], int)
        assert manifest["content_format"]["loaded"] is True
        assert len(manifest["content_format"]["updated"]) == 10
        assert manifest["llm_postprocess_warnings"] == []
        assert manifest["draft_score"]["engagement"]["status"] == "未发布"
        assert manifest["material_gaps"] == []
        assert manifest["human_review"]["status"] == "pending"
        assert manifest["human_review"]["checklist"]
        assert manifest["versions"]["ai"]["purpose"].startswith("internal_reference")
        assert manifest["versions"]["zhihu"]["purpose"].startswith("publish")
        for name in ("ai", "zhihu"):
            assert "falsifiability_check" in manifest["versions"][name]
            assert "track" in manifest["versions"][name]["falsifiability_check"]
        # 人工介入入口：检查清单 + AI 版用途标注
        checklist = next(iter(out.glob("review-checklist-*.md"))).read_text(encoding="utf-8")
        assert "发布前检查清单" in checklist
        ai_md = next(iter(out.glob("ai-*.md"))).read_text(encoding="utf-8")
        zhihu_md = next(iter(out.glob("zhihu-*.md"))).read_text(encoding="utf-8")
        assert "内部参考，非直接发布物" in ai_md
        assert "内部参考" not in zhihu_md

    def test_cli_reports_gaps_on_bad_draft(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        bad = (
            "# 摸鱼神器 WorkBuddy\n\n"
            "![截图](https://picsum.photos/1)\n\n"
            "官网 [workbuddy.ai](https://www.workbuddy.ai/) 下载。\n"
        )
        src = tmp_path / "bad.md"
        src.write_text(bad, encoding="utf-8")
        out = tmp_path / "out2"
        code = main(
            ["--source", str(src), "--no-llm", "--query", "workbuddy 安装配置教程", "--output", str(out)]
        )
        assert code == 0
        manifest = json.loads(next(iter(out.glob("adapt-*.json"))).read_text(encoding="utf-8"))
        gap_types = {g["type"] for g in manifest["material_gaps"]}
        assert "placeholder_image" in gap_types
        assert "query_coverage_missing" in gap_types
        assert "unverified_links" in gap_types
        assert "关键词覆盖" in manifest["rewrite_triggers"]
        assert manifest["human_review"]["checklist"]
        for name in ("ai", "zhihu"):
            md = next(iter(out.glob(f"{name}-*.md"))).read_text(encoding="utf-8")
            assert "素材缺口" in md  # 输出尾部带发布前注释块

    def test_checklist_lists_facts_for_manual_verify(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        draft = (
            "# WorkBuddy 教程\n\n"
            "新用户注册可领 5000 积分，每天签到得 100 积分，装完 10 分钟上手。\n"
        )
        src = tmp_path / "facts.md"
        src.write_text(draft, encoding="utf-8")
        out = tmp_path / "out3"
        assert main(["--source", str(src), "--no-llm", "--output", str(out)]) == 0
        checklist = next(iter(out.glob("review-checklist-*.md"))).read_text(encoding="utf-8")
        assert "5000积分" in checklist
        assert "100积分" in checklist
        assert "10分钟" in checklist
        assert "- [ ] 已核对正文数字" in checklist

    def test_no_llm_preserves_search_ai_load_key(self, tmp_path, monkeypatch):
        sentinel = lambda: "sentinel-key"
        monkeypatch.setattr("search_ai._load_key", sentinel)
        src = tmp_path / "draft.md"
        src.write_text(SAMPLE_DRAFT, encoding="utf-8")
        out = tmp_path / "out4"
        assert main(["--source", str(src), "--no-llm", "--output", str(out)]) == 0
        assert search_ai._load_key is sentinel  # 全局未被覆盖

    def test_llm_output_postprocessed_strips_ai_images(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        payload = {
            "choices": [{"message": {"content": "# 改\n\n![图](https://x.com/a.png)\n\n正文。\n\n\n多余空行。"}}]
        }
        monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResponse(payload))
        src = tmp_path / "draft.md"
        src.write_text(SAMPLE_DRAFT, encoding="utf-8")
        out = tmp_path / "out5"
        assert main(["--source", str(src), "--output", str(out)]) == 0
        manifest = json.loads(next(iter(out.glob("adapt-*.json"))).read_text(encoding="utf-8"))
        assert any("图片引用" in w for w in manifest["llm_postprocess_warnings"])
        ai_md = next(iter(out.glob("ai-*.md"))).read_text(encoding="utf-8")
        assert "![图](https://x.com/a.png)" not in ai_md
        assert "\n\n\n" not in ai_md  # 连续空行被规整

    def test_cli_rejects_high_promotional(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: None)
        bad = (
            "# 推荐一个课程\n\n"
            "这门课必买，扫码下单立减 50！\n"
        )
        src = tmp_path / "promo.md"
        src.write_text(bad, encoding="utf-8")
        out = tmp_path / "out6"
        code = main(["--source", str(src), "--no-llm", "--output", str(out)])
        assert code == 3  # 直接拒绝服务
        assert not list(out.glob("*.md"))  # 不生成任何版本
        assert not list(out.glob("adapt-*.json"))

    def test_generate_uses_llm_after_no_llm_main(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search_ai._load_key", lambda: "sk-test")
        payload = {"choices": [{"message": {"content": "# LLM 版"}}]}
        monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResponse(payload))
        src = tmp_path / "d.md"
        src.write_text(SAMPLE_DRAFT, encoding="utf-8")
        assert main(["--source", str(src), "--no-llm", "--output", str(tmp_path / "o1")]) == 0
        # main --no-llm 后直接调用 generate_*：默认启用 LLM，无模块级状态泄漏
        doc = parse_markdown(SAMPLE_DRAFT)
        md, used = generate_zhihu_version(doc)
        assert used is True
        assert md.startswith("# LLM 版")

    def test_missing_source(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            main(["--source", str(tmp_path / "nope.md")])
        assert exc.value.code == 2

    def test_directory_source_rejected(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            main(["--source", str(tmp_path)])
        assert exc.value.code == 2

    def test_invalid_platform(self, tmp_path):
        src = tmp_path / "draft.md"
        src.write_text(SAMPLE_DRAFT, encoding="utf-8")
        assert main(["--source", str(src), "--platforms", "xhs"]) == 2
