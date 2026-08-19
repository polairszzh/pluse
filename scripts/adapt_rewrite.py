"""adapt 评分驱动改写指令与版本生成（从 content_adapter.py 拆分，行为不变）"""
from __future__ import annotations

import re

import requests
import search_ai
from adapt_draft import DraftDoc, _load_content_format, _strip_frontmatter

# --------------------------------------------------------------------------
# 评分驱动改写指令（A1：低分维度强制修正，全高分才轻度润色）
# --------------------------------------------------------------------------


def _rewrite_triggers(dims: dict[str, int] | None) -> list[str]:
    """与 _rewrite_instructions 同阈值的触发维度清单（供 manifest 记录，保证一致）"""
    if not dims:
        return []
    out: list[str] = []
    if dims.get("关键词覆盖", 100) < 60:
        out.append("关键词覆盖")
    if dims.get("AI 可引用性", 100) < 70:
        out.append("AI 可引用性")
    if dims.get("内容质量 (E-E-A-T)", 100) < 60:
        out.append("内容质量 (E-E-A-T)")
    if dims.get("结构与格式", 100) < 60:
        out.append("结构与格式")
    return out


def _rewrite_instructions(
    dims: dict[str, int] | None,
    query: str | None = None,
) -> str:
    """按四维得分生成改写强度指令；dims 为 None 时返回空（贴近原稿）"""
    if not dims:
        return ""
    parts: list[str] = []
    kw = dims.get("关键词覆盖", 100)
    cit = dims.get("AI 可引用性", 100)
    qual = dims.get("内容质量 (E-E-A-T)", 100)
    struct = dims.get("结构与格式", 100)

    if kw < 60:
        q = (query or "").strip()
        if q:
            parts.append(f"标题必须包含目标关键词「{q}」或其自然变体，首段前 50 字内自然出现一次；禁止堆砌关键词")
        else:
            parts.append("标题必须包含核心关键词，首段前 50 字内自然出现一次；禁止堆砌关键词")
    if cit < 70:
        parts.append("每个 H2 下第一段写成 130-170 字自包含答案块：结论前置、直接回答、带具体信息")
    if qual < 60:
        parts.append(
            "补充具体数字、案例或第一手经验增强可信度；不得编造数据/链接/引用，"
            "缺失素材在文末用 HTML 注释列明，不要额外生成可见的素材缺口章节；"
            "第一手经验以第三人称转述（如「作者实测…」），不使用第一人称"
        )
    if struct < 60:
        parts.append("按「是什么 → 为什么用 → 怎么安装配置」重排 H2/H3，段落 120-180 字")

    if not parts:
        return "本次贴近原稿做轻度润色：修正语病、统一语气，不改变结构与篇幅。"
    return "改写强度较高，以下为必须逐条落实的规则：" + "".join(f"\n- {p}" for p in parts)


# --------------------------------------------------------------------------
# LLM 改写（DeepSeek；失败回退规则脚手架）
# --------------------------------------------------------------------------


def _llm_rewrite(
    system: str, user: str, timeout: int = 90, enabled: bool = True
) -> str | None:
    """调用 DeepSeek 生成；enabled=False 或任何失败返回 None（调用方回退脚手架）"""
    if not enabled:
        return None
    key = search_ai._load_key()
    if key is None:
        return None
    payload = {
        "model": search_ai.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(
            search_ai.DEEPSEEK_BASE, json=payload, headers=headers, timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        return content if isinstance(content, str) and content.strip() else None
    except (
        requests.exceptions.RequestException, ValueError, KeyError,
        IndexError, TypeError, AttributeError,
    ):
        return None


# --------------------------------------------------------------------------
# AI 优化版生成
# --------------------------------------------------------------------------


AI_SYSTEM = (
    "你是 AI 可见度优化编辑。把用户草稿改写成「AI 搜索优化版」：写给 AI 在合成答案时能准确摘取，"
    "不是写给人类通读。规则：每个 H2 是一个完整问答对；H2 下第一段 130-170 字、自包含、"
    "直接给答案并带具体信息；不使用第一人称；不使用表格和复杂列表；不依赖链接；"
    "总长 1200-2000 字。只输出 Markdown，不要额外说明。"
)


def _clean_ai_text(text: str) -> str:
    """AI 版文本清理：去图片引用 + 压缩空白（AI 版不用图）"""
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _ai_system(dims: dict[str, int] | None = None, query: str | None = None) -> str:
    extra = _rewrite_instructions(dims, query)
    parts = [AI_SYSTEM]
    if extra:
        parts.append(extra)
    rules = _load_content_format()
    if rules:
        parts.append(
            "平台格式规则（来自 references/content-format.md，AI 优化版请遵循表格「AI 优化版」列）：\n" + rules
        )
    return "\n\n".join(parts)


def _first_150(text: str) -> tuple[str, str]:
    """取 ≤150 字内最后一个完整句作为首段，避免硬截断断句；返回 (first, rest)"""
    if len(text) <= 150:
        return text, ""
    cut = text[:150]
    idx_cn = max(
        cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("；")
    )
    if idx_cn != -1:
        return cut[: idx_cn + 1], text[idx_cn + 1:]
    # 无中文句读时才考虑英文句点，且要求句点后是空白/句尾（避免版本号/小数点在句中截断）
    idx_en = -1
    for i in range(len(cut) - 1, -1, -1):
        if cut[i] == "." and (i == len(cut) - 1 or cut[i + 1].isspace()):
            idx_en = i
            break
    if idx_en == -1:
        return cut, text[150:]
    return cut[: idx_en + 1], text[idx_en + 1:]


def _fallback_ai_version(doc: DraftDoc) -> str:
    """无 LLM 时的规则脚手架：把每个 H2 话题重组为问答对 + 自包含首段"""
    lines = [f"# {doc.title}：快速了解与使用指南", ""]
    intro_text = _clean_ai_text(" ".join(doc.intro))
    intro_target: str | None = None
    if not doc.sections:
        # 无任何小节：整篇引言即「它是什么」自包含答案，不生成默认话题占位
        if intro_text:
            lines.append("## 它是什么？")
            lines.append("")
            lines.append(intro_text)
            lines.append("")
        lines.append("## 总结")
        lines.append("")
        lines.append(
            f"{doc.title}的核心要点已整理为自包含问答块，"
            "发布后可观察 AI 平台对该话题的回答是否引用本文。"
        )
        lines.append("")
        return "\n".join(lines)
    if intro_text:
        target = next(
            (h for h, _ in doc.sections if ("是什么" in h or "介绍" in h)),
            None,
        )
        if target:
            # 已有「是什么/介绍」H2：引言合并进该节回答开头，不单独成块也不丢弃
            intro_target = target
        else:
            # 引言回答「它是什么」，是 AI 引用最可能摘取的内容，先补一个问答块
            first, rest = _first_150(intro_text)
            lines.append("## 它是什么？")
            lines.append("")
            lines.append(first)
            lines.append("")
            if rest:
                lines.append(rest)
                lines.append("")
    used: list[str] = []
    for heading, _ in doc.sections:
        topic = heading
        if topic in used:
            continue
        used.append(topic)
        q = topic if re.search(r"[？?]$", topic) else f"{topic}？"
        # 重复 H2 标题的内容合并进同一个问答块，不丢弃
        body = "\n".join(
            "\n".join(sec_body) for h, sec_body in doc.sections if h == topic
        )
        if heading == intro_target and intro_text:
            body = intro_text + "\n" + body
        body = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", body)  # AI 优化版去图
        answer = re.sub(r"\s+", " ", body).strip()
        if heading not in doc.topics:
            # 后续 H1 分节标题：不转问答，保留为子节直接输出
            lines.append(f"## {heading}")
            lines.append("")
            lines.append(answer or "（本节内容待补充）")
            lines.append("")
            continue
        if len(answer) < 60:
            answer = f"{topic}是本文介绍的核心内容：{answer or '具体信息以官方文档为准。'}"
        first, rest = _first_150(answer)
        lines.append(f"## {q}")
        lines.append("")
        lines.append(first)
        lines.append("")
        if rest:
            lines.append(rest)
            lines.append("")
    if not any("总结" in h for h, _ in doc.sections):
        lines.append("## 总结")
        lines.append("")
        lines.append(
            f"{doc.title}的核心要点已按「是什么、优势、安装配置」整理为自包含问答块，"
            "发布后可观察 AI 平台对该话题的回答是否引用本文。"
        )
        lines.append("")
    return "\n".join(lines)


def generate_ai_version(
    doc: DraftDoc,
    dims: dict[str, int] | None = None,
    query: str | None = None,
    llm: bool = True,
) -> tuple[str, bool]:
    """生成 AI 优化版；返回 (markdown, 是否走了 LLM)"""
    out = _llm_rewrite(_ai_system(dims, query), _strip_frontmatter(doc.raw), enabled=llm)
    if out:
        return out, True
    return _fallback_ai_version(doc), False


# --------------------------------------------------------------------------
# 知乎版生成
# --------------------------------------------------------------------------


ZHIHU_SYSTEM = (
    "你是知乎长文编辑。把用户草稿改写成知乎版：2000-4000 字为上限，本次贴近原稿做结构化改写与适度扩写，"
    "不硬凑字数。规则：H2/H3 层级清晰；段落 120-180 字；标题含核心关键词；"
    "正文可有数据、引用、案例；文末可以提问引导互动但不要诱导点赞；不写营销号话术。"
    "保留原稿中的代码块与图片引用（图片引用保持 ![alt](path) 形式）。只输出 Markdown。"
)


def _zhihu_system(dims: dict[str, int] | None = None, query: str | None = None) -> str:
    extra = _rewrite_instructions(dims, query)
    parts = [ZHIHU_SYSTEM]
    if extra:
        parts.append(extra)
    rules = _load_content_format()
    if rules:
        parts.append(
            "平台格式规则（来自 references/content-format.md，知乎版请遵循「知乎」列与注意事项）：\n" + rules
        )
    return "\n\n".join(parts)


def _format_zhihu_lines(lines_in: list[str]) -> list[str]:
    """把引言/节内行格式化为知乎版：代码块独立成块、表格连续、列表/引用/图片/标题原样、普通文本拼段"""
    out: list[str] = []
    in_code = False
    para: list[str] = []
    table_mode = False

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append(re.sub(r"\s+", " ", " ".join(para)).strip())
            out.append("")
            para = []

    for line in lines_in:
        s = line.strip()
        if s.startswith("```"):
            flush_para()
            table_mode = False
            in_code = not in_code
            out.append(line)
            if not in_code:
                _ensure_blank(out)
            continue
        if in_code:
            out.append(line)
            continue
        if not s:
            flush_para()  # 空行作为段落分隔，不并入段落
            continue
        if s.startswith("|"):
            # 表格行连续输出，行间不加空行（避免拆散表格）
            flush_para()
            if not table_mode:
                _ensure_blank(out)
            out.append(line)
            table_mode = True
            continue
        table_mode = False
        if s == "---" or s.startswith(("![", "#", "- ", "* ", "+ ", ">")) or re.match(
            r"^\d+[.、)]\s", s
        ):
            # 列表/引用/图片/标题：原样输出，前后空行分隔
            flush_para()
            out.append(line)
            out.append("")
            continue
        para.append(line)
    flush_para()
    return out


def _ensure_blank(lines_out: list[str]) -> None:
    """仅当末行非空时才追加空行，避免连续空行"""
    if lines_out and lines_out[-1] != "":
        lines_out.append("")


def _fallback_zhihu_version(doc: DraftDoc) -> str:
    """无 LLM 时的规则脚手架：结构化重排 + 段落切分 + 保留代码/图片"""
    lines = [f"# {doc.title}", ""]
    # 引言：首段
    if doc.intro:
        lines.extend(_format_zhihu_lines(doc.intro))
    for heading, body in doc.sections:
        lines.append(f"## {heading}")
        lines.append("")
        in_code = False
        table_mode = False
        for line in body:
            if line.strip().startswith("```"):
                # 代码块围栏：原样保留，结束围栏后补空行分隔，代码行不切分不加空行
                in_code = not in_code
                lines.append(line)
                if not in_code:
                    _ensure_blank(lines)
                continue
            if in_code:
                lines.append(line)
                continue
            if not line.strip():
                continue  # 正文空行跳过，避免连续空行（段落间由行尾空行分隔）
            if line.startswith("|"):
                # 表格行连续输出，行间不加空行
                if not table_mode:
                    _ensure_blank(lines)
                lines.append(line)
                table_mode = True
                continue
            table_mode = False
            if line.strip() == "---" or line.startswith(
                ("![", "#", "- ", "* ", "+ ", ">")
            ) or re.match(r"^\d+[.、)]\s", line):
                # 列表/引用/图片/标题：原样输出，不切分，保留前缀
                lines.append(line)
                lines.append("")
                continue
            if len(line) > 180:
                # 超长段落按句切分到 ≤180 字
                chunks = _split_paragraph(line, 180)
                for chunk in chunks:
                    lines.append(chunk)
                    lines.append("")
            else:
                lines.append(line)
                lines.append("")
    lines.append("## 写在最后")
    lines.append("")
    lines.append("你在使用过程中遇到过什么问题？欢迎在评论区交流。")
    lines.append("")
    return "\n".join(lines)


def _split_paragraph(text: str, limit: int) -> list[str]:
    """按句号/分号切分长段落到 ≤limit 字符"""
    sentences = re.split(r"(?<=[。；!?！？])", text)
    chunks: list[str] = []
    buf = ""
    for sent in sentences:
        if not sent:
            continue
        if len(buf) + len(sent) <= limit:
            buf += sent
        else:
            if buf:
                chunks.append(buf.strip())
            if len(sent) > limit:
                # 超长单句直接硬切
                while len(sent) > limit:
                    chunks.append(sent[:limit].strip())
                    sent = sent[limit:]
                buf = sent
            else:
                buf = sent
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [text[:limit]]


def generate_zhihu_version(
    doc: DraftDoc,
    dims: dict[str, int] | None = None,
    query: str | None = None,
    llm: bool = True,
) -> tuple[str, bool]:
    """生成知乎版；返回 (markdown, 是否走了 LLM)"""
    out = _llm_rewrite(_zhihu_system(dims, query), _strip_frontmatter(doc.raw), enabled=llm)
    if out:
        return out, True
    return _fallback_zhihu_version(doc), False
