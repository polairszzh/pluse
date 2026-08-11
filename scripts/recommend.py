"""Pulse 平台信源推荐 —— /pulse recommend

把「想被某 AI 引擎引用该发哪」从静态知识变成推荐：
  --engine <引擎>   输出该引擎的引用源权重排序 + 内容策略；
  --url <文章URL>   识别文章所在平台，输出该平台在各引擎的引用权重排名
                    （可用任意文章验证，不限于自己的内容）。

权重数据来源：2026 年 3-5 月 16800 次真实查询实测（14 平台 × 4 引擎 × 100 关键词 × 3 重复），
见 docs/roadmap.md 与 docs/国内收录三路径.md。元宝无公开实测，为生态位估计（标注待校准）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "recommend_sources.json"


def _load_engine_data() -> tuple[dict, dict, dict]:
    """加载外置权重数据（data/recommend_sources.json），便于真实 track 数据校准更新"""
    with open(_DATA_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    engines = raw["engines"]
    return (
        {k: v["label"] for k, v in engines.items()},
        {k: v["strategy"] for k, v in engines.items()},
        {k: v["sources"] for k, v in engines.items()},
    )


ENGINE_LABELS, ENGINE_STRATEGY, ENGINE_SOURCES = _load_engine_data()

# 平台 → 域名后缀（用于 --url 识别文章所在平台）
PLATFORM_HOSTS: dict[str, tuple[str, ...]] = {
    "知乎": ("zhihu.com",),
    "CSDN": ("csdn.net",),
    "博客园": ("cnblogs.com",),
    "公众号": ("mp.weixin.qq.com",),
    "今日头条": ("toutiao.com",),
    "百家号": ("baijiahao.baidu.com",),
    "百度知道": ("zhidao.baidu.com",),
    "百度百科": ("baike.baidu.com",),
    "搜狐号": ("sohu.com",),
    "网易号": ("163.com",),
    "小红书": ("xiaohongshu.com",),
    "腾讯新闻": ("news.qq.com",),
    "搜狗号": ("weixin.sogou.com",),
    "抖音生态": ("douyin.com",),
}


def _platform_of(url: str) -> str | None:
    """按域名识别文章所在平台，无法识别返回 None"""
    try:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        path = parts.path or ""
    except ValueError:
        return None
    for platform, hosts in PLATFORM_HOSTS.items():
        for h in hosts:
            if not (host == h or host.endswith("." + h)):
                continue
            # 搜狐号/网易号限定 www/mp 子域 + 路径：news.sohu.com/a/ 等频道不误判
            if platform == "搜狐号" and not (
                host == "mp.sohu.com"
                or (host == "www.sohu.com" and path.startswith("/a/"))
            ):
                continue
            if platform == "网易号" and not (
                host == "www.163.com" and path.startswith("/dy/")
            ):
                continue
            return platform
    return None


def _ranked(engine: str) -> list[tuple[str, float]]:
    return sorted(ENGINE_SOURCES[engine].items(), key=lambda item: item[1], reverse=True)


def recommend_engine(engine: str) -> list[str]:
    """生成单个引擎的推荐文本行"""
    lines = [f"推荐发布平台（目标引擎：{ENGINE_LABELS[engine]}）："]
    for i, (platform, weight) in enumerate(_ranked(engine), 1):
        lines.append(f"  {i}. {platform}（{weight:.1%}）")
    lines.append(f"内容策略：{ENGINE_STRATEGY[engine]}")
    if engine == "yuanbao":
        lines.append("数据说明：元宝无公开实测，权重为生态位估计，待真实数据校准")
    else:
        lines.append("数据说明：2026 年 3-5 月实测（16800 次查询），见 docs/国内收录三路径.md")
    lines.append("权重为相对值：各平台权重之和可能不足 100%，未列平台占剩余份额")
    return lines


def recommend_url(url: str) -> list[str]:
    """生成给定文章 URL 的平台推荐文本（可用于验证任意文章）"""
    platform = _platform_of(url)
    lines = [f"文章所在平台：{platform or '未识别'}"]
    if platform is None:
        lines.append("  （无法从域名识别平台，可用 --engine 查看各引擎推荐）")
        return lines
    lines.append("该平台在各引擎的引用权重：")
    for engine in ("deepseek", "doubao", "tongyi", "wenxin", "yuanbao"):
        ranked = _ranked(engine)
        pos = next((i for i, (p, _) in enumerate(ranked, 1) if p == platform), None)
        weight = ENGINE_SOURCES[engine].get(platform)
        if pos is None:
            lines.append(f"  {ENGINE_LABELS[engine]}：未进入前 9（权重 0 或未收录）")
        else:
            note = "（待校准）" if engine == "yuanbao" else ""
            lines.append(
                f"  {ENGINE_LABELS[engine]} {weight:.1%}（第 {pos} 位）{note}"
            )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pulse-recommend",
        description="Pulse 平台信源推荐 —— 输出 AI 引擎的引用源权重排序与内容策略",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--engine",
        choices=["deepseek", "doubao", "tongyi", "wenxin", "yuanbao", "all"],
        help="目标 AI 引擎（all 输出全部引擎前三）",
    )
    group.add_argument("--url", help="文章 URL：识别平台并输出该平台在各引擎的权重排名")
    args = parser.parse_args(argv)

    if args.url is not None:
        print("\n".join(recommend_url(args.url)))
        return 0
    if args.engine == "all":
        for engine in ("deepseek", "doubao", "tongyi", "wenxin", "yuanbao"):
            print(f"== {ENGINE_LABELS[engine]} ==")
            for line in recommend_engine(engine)[1:4]:
                print(line)
            print()
        print("完整排序与策略：python scripts/recommend.py --engine <引擎名>")
        return 0
    print("\n".join(recommend_engine(args.engine)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
