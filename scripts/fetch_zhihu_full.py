"""知乎文章全文抓取（可选：Playwright + 本机 Edge/Chrome）

背景：知乎开放平台 API 只返回 300-800 字摘要，评分粒度受限。
本模块用系统自带浏览器（关闭自动化特征 + 正常 UA）抓取完整正文，
供 `audit --full` 使用；失败/未安装 Playwright 时由调用方降级 API 摘要。

实测结论（2026-08-06）：Edge + --disable-blink-features=AutomationControlled
+ 删除 navigator.webdriver + 正常 UA，headful/headless 均能抓取知乎文章完整正文，
无需登录。边界说明：为读取公开页面做了最小自动化特征隐藏（禁用自动化标记、
移除 webdriver、UA 去掉 HeadlessChrome 标记——UA 版本为浏览器内置，不伪造
特定版本/冒充用户）；只读 + 低频，批量监测不依赖此通道。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_CONTENT_SELECTORS = (".Post-RichText", ".RichText", ".post-content")
_MIN_CONTENT_LEN = 100


def _is_zhihu_url(url: str) -> bool:
    """知乎域名校验"""
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return False
    return host == "zhihu.com" or host.endswith(".zhihu.com")


def _is_article_url(url: str) -> bool:
    """校验是否为单篇知乎内容（文章 /p/ 或回答 /answer/）

    问题页（/question/）含多个回答，视频/想法等正文容器不同，均拒绝，
    避免 --full 抓到非文章正文导致评分语义错误。
    """
    try:
        path = urlsplit(url).path or ""
    except ValueError:
        return False
    return bool(re.search(r"/(?:p|answer)/\d+", path))


def _content_selectors(url: str | None = None) -> list[str]:
    """构建正文选择器列表：/answer/ 多回答页优先目标回答锚点，其余为通用选择器"""
    selectors = list(_CONTENT_SELECTORS)
    if url:
        m = re.search(r"/answer/(\d+)", url)
        if m:
            selectors.insert(0, f"#answer-{m.group(1)} .RichText")
    return selectors


def _extract_content(page, url: str | None = None) -> str | None:
    """按选择器优先级提取正文：精确选择器优先，取所有匹配中最长文本，
    空/过短结果跳过，全部失败返回 None"""
    selectors = _content_selectors(url)
    for selector in selectors:
        try:
            texts = page.eval_on_selector_all(
                selector, "els => els.map(e => e.innerText)"
            )
        except Exception:  # noqa: BLE001, S112 — 选择器不存在/异常，尝试下一个
            continue
        if texts:
            candidates = [
                t.strip() for t in texts if t and len(t.strip()) >= _MIN_CONTENT_LEN
            ]
            if candidates:
                return max(candidates, key=len)
    return None


def fetch_full_content(url: str, timeout: int = 30) -> dict:
    """抓取知乎文章完整正文

    返回 {"title": str, "content": str}（成功）或 {"error": str}（失败/不可用）。
    """
    if not _is_zhihu_url(url):
        return {"error": f"仅支持知乎链接：{url}"}
    if not _is_article_url(url):
        return {
            "error": (
                "仅支持知乎文章/回答（/p/ 或 /answer/ 链接），"
                "问题页/视频/想法等非单篇内容不支持"
            )
        }
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "error": (
                "Playwright 未安装：pip install playwright && "
                "playwright install msedge（本机需有 Edge）"
            )
        }
    try:
        with sync_playwright() as p:
            browser = None
            launch_errors = []
            for channel in ("msedge", "chrome"):
                try:
                    browser = p.chromium.launch(
                        channel=channel,
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    break
                except Exception as exc:  # noqa: BLE001 — 尝试下一个 channel
                    launch_errors.append(f"{channel}: {exc}")
            if browser is None:
                raise RuntimeError(
                    "本机未找到 Edge/Chrome："
                    + "; ".join(launch_errors)
                    + "（可用 playwright install msedge 安装）"
                )
            try:
                # 用浏览器内置桌面 UA（去掉 Headless 标记），不硬编码具体版本：
                # headless 默认 UA 含 HeadlessChrome 易被识别，移除标记后走正常桌面形态
                ctx = browser.new_context()
                page = ctx.new_page()
                ua = page.evaluate("navigator.userAgent")
                if "HeadlessChrome" in ua:
                    ua = ua.replace("HeadlessChrome", "Chrome")
                    ctx.close()
                    ctx = browser.new_context(user_agent=ua)
                    page = ctx.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => false})"
                )
                page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                # 先滚动触发懒加载（长文/回答页正文可能初始不在 DOM），再等正文容器
                for _ in range(5):
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(500)
                page.mouse.wheel(0, -20000)
                try:
                    # 导航后用最终 URL（可能被重定向）重新计算锚点选择器
                    final_url = page.url
                    page.wait_for_selector(
                        ", ".join(_content_selectors(final_url)),
                        timeout=timeout * 1000,
                    )
                except Exception:  # noqa: BLE001 — 超时转可读错误
                    return {
                        "error": (
                            "正文加载超时（可能需登录/付费、页面结构变化或网络问题）"
                        )
                    }
                content = _extract_content(page, final_url)
                title = page.title()
                if content is None:
                    return {"error": "正文提取为空（页面结构变化或内容需登录/付费）"}
                return {"title": title, "content": content}
            finally:
                # 无论提取成功/失败都显式关闭浏览器，避免依赖上下文隐式清理
                try:
                    browser.close()
                except Exception:  # noqa: BLE001, S110 — 关闭失败不影响返回结果
                    pass
    except Exception as exc:  # noqa: BLE001 — 浏览器/网络/选择器异常统一降级
        return {"error": str(exc)}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法：python scripts/fetch_zhihu_full.py <知乎文章URL>")
        raise SystemExit(2)
    result = fetch_full_content(sys.argv[1])
    if "error" in result:
        print(f"抓取失败：{result['error']}")
        raise SystemExit(1)
    print(f"标题：{result['title']}")
    print(f"正文长度：{len(result['content'])} 字")
