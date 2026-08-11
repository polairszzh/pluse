"""知识库 freshness 检查（D3）

扫描平台规则知识文件（skills/visibility/references/*.md 与 docs/国内收录三路径.md），
检查每个文件是否带 `Updated: YYYY-MM-DD` 标记、是否超过 90 天未更新。
防止「平台规则会过期」导致的知识腐烂。

用法：
  python scripts/reference_freshness.py           # 报告模式，打印结果
  python scripts/reference_freshness.py --ci      # CI 模式，有告警 exit 1
"""
from __future__ import annotations

import argparse
import re
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = (
    PROJECT_ROOT / "skills" / "visibility" / "references",
    PROJECT_ROOT / "docs" / "国内收录三路径.md",
)
MAX_AGE_DAYS = 90

_UPDATED_RE = re.compile(r"Updated:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def _parse_updated_detail(text: str) -> tuple[date | None, str | None]:
    """解析 Updated 标记：返回 (日期, 错误)。区分「无标记」与「日期无效」"""
    m = _UPDATED_RE.search(text)
    if not m:
        return None, "缺少 Updated: YYYY-MM-DD 标记"
    raw = m.group(1)
    try:
        return date.fromisoformat(raw), None
    except ValueError:
        return None, f"Updated 日期无效：{raw}（应为 YYYY-MM-DD）"


def parse_updated(text: str) -> date | None:
    """解析文件中的 Updated: YYYY-MM-DD 标记，缺失/格式错误返回 None"""
    parsed, _ = _parse_updated_detail(text)
    return parsed


def check_file(path: Path, today: date | None = None) -> list[str]:
    """检查单个知识文件的 freshness，返回告警列表（空 = 正常）"""
    today = today or datetime.now().astimezone().date()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"无法读取：{exc}"]
    updated, err = _parse_updated_detail(text)
    if err:
        return [err]
    age = (today - updated).days
    if age < 0:
        return [f"Updated 是未来日期（{updated}）"]
    if age > MAX_AGE_DAYS:
        return [f"超过 {MAX_AGE_DAYS} 天未更新（{updated}，距今 {age} 天）"]
    return []


def scan(targets, today: date | None = None) -> dict[str, list[str]]:
    """扫描目标（目录或文件），返回 {路径: 告警列表}"""
    result: dict[str, list[str]] = {}
    for target in targets:
        files = sorted(target.rglob("*.md")) if target.is_dir() else [target]
        for f in files:
            warnings = check_file(f, today=today)
            if warnings:
                try:
                    key = str(f.relative_to(PROJECT_ROOT))
                except ValueError:
                    key = str(f)
                result[key] = warnings
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知识库 freshness 检查")
    parser.add_argument("--ci", action="store_true", help="CI 模式：有告警退出码 1")
    args = parser.parse_args(argv)

    problems = scan(DEFAULT_TARGETS)
    if not problems:
        print("知识库 freshness：全部文件正常（90 天内更新）。")
        return 0
    for path, warnings in problems.items():
        for w in warnings:
            print(f"[freshness] {path}：{w}")
    return 1 if args.ci else 0


if __name__ == "__main__":
    raise SystemExit(main())
