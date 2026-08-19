"""track SQLite 存储与趋势/变化点（从 search_ai.py 拆分，行为不变）"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from track_config import DEFAULT_DB
from track_models import ProbeResult
from track_utils import (
    _aggregate_binary,
    _aggregate_bool_tristate,
    _majority,
    _parse_mine_ids,
    _union_strings,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    platform TEXT NOT NULL,
    run_at TEXT NOT NULL,
    status TEXT NOT NULL,
    cited INTEGER,
    sentiment TEXT,
    context TEXT,
    source TEXT NOT NULL,
    degraded INTEGER NOT NULL DEFAULT 0,
    error TEXT,
      meta TEXT,
      mine_cited INTEGER,
      mine_ids TEXT,
      confidence TEXT,
      cited_type TEXT,
      owned_ids TEXT,
      competitor_matched INTEGER,
      competitor_ids TEXT,
      fact_risks TEXT,
      sample_idx INTEGER NOT NULL DEFAULT 0,
      run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_probes_query_platform_run
    ON probes(query, platform, run_at);
"""


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库补列：mine_cited / mine_ids / confidence / B3 引用质量字段（ALTER TABLE ADD COLUMN）"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(probes)").fetchall()}
    with conn:
        if "mine_cited" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN mine_cited INTEGER")
        if "mine_ids" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN mine_ids TEXT")
        if "confidence" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN confidence TEXT")
        if "cited_type" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN cited_type TEXT")
        if "owned_ids" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN owned_ids TEXT")
        if "competitor_matched" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN competitor_matched INTEGER")
        if "competitor_ids" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN competitor_ids TEXT")
        if "fact_risks" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN fact_risks TEXT")
        if "sample_idx" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN sample_idx INTEGER NOT NULL DEFAULT 0")
        if "run_id" not in cols:
            conn.execute("ALTER TABLE probes ADD COLUMN run_id TEXT")
        # run_id 索引依赖补列完成：旧库在 _migrate 之后才可建
        conn.execute("CREATE INDEX IF NOT EXISTS idx_probes_run_id ON probes(run_id)")


def store_results(
    rows: list[ProbeResult],
    db_path: Path = DEFAULT_DB,
    run_at: str | None = None,
) -> str:
    """写入一次探测快照，返回本次 run_at"""
    # 微秒精度：避免同一秒内重跑两次时 run_at 相同，趋势/概览把两次运行合并
    run_at = run_at or datetime.now().astimezone().isoformat(timespec="microseconds")
    # run_id：同一次 store 调用内的所有样本共享；硬区分同 run_at 的不同独立运行
    run_id = uuid.uuid4().hex[:12]
    conn = connect(db_path)
    try:
        with conn:
            for r in rows:
                cited = 1 if r.cited is True else (0 if r.cited is False else None)
                mine_cited = 1 if r.mine_cited is True else (0 if r.mine_cited is False else None)
                competitor_matched = (
                    1 if r.competitor_matched is True
                    else (0 if r.competitor_matched is False else None)
                )
                conn.execute(
                    "INSERT INTO probes(query, platform, run_at, status, cited, sentiment,"
                    " context, source, degraded, error, meta, mine_cited, mine_ids, confidence,"
                    " cited_type, owned_ids, competitor_matched, competitor_ids, fact_risks,"
                    " sample_idx, run_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        r.query, r.platform, run_at, r.status, cited, r.sentiment,
                        r.context, r.source, 1 if r.degraded else 0, r.error,
                        json.dumps(r.meta if r.meta else {}, ensure_ascii=False),
                        mine_cited,
                        json.dumps(r.mine_ids or [], ensure_ascii=False),
                        r.confidence,
                        r.cited_type,
                        json.dumps(r.owned_ids or [], ensure_ascii=False),
                        competitor_matched,
                        json.dumps(r.competitor_ids or [], ensure_ascii=False),
                        json.dumps(r.fact_risks or [], ensure_ascii=False),
                        r.sample_idx,
                        run_id,
                    ),
                )
    finally:
        conn.close()
    return run_at


def load_history(
    query: str,
    platform: str | None = None,
    db_path: Path = DEFAULT_DB,
    limit: int = 200,
) -> list[dict]:
    """按时间倒序读取历史探测（新 -> 旧）"""
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if platform:
            cur = conn.execute(
                "SELECT * FROM probes WHERE query=? AND platform=? ORDER BY run_at DESC LIMIT ?",
                (query, platform, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM probes WHERE query=? ORDER BY run_at DESC LIMIT ?",
                (query, limit),
            )
        rows = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
    return rows


def _recent_run_rows(
    query: str,
    db_path: Path = DEFAULT_DB,
    max_runs: int = 1000,
) -> list[dict]:
    """按 run 完整加载最近 max_runs 次运行的全部行

    多采样后同一 run 有 N 行：按行数限流会把样本组截断成不完整组，
    导致概率/多数派偏差。这里先按 run_id 取最近 max_runs 个 run，
    再加载这些 run 的全部行（组完整）；旧数据（run_id 为空）按行取
    最近 max_runs 行（一行一 run，单采样时代语义）。
    """
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows: list[dict] = []
        cur = conn.execute(
            "SELECT run_id FROM probes WHERE query=? AND run_id IS NOT NULL "
            "GROUP BY run_id ORDER BY MAX(id) DESC LIMIT ?",
            (query, max_runs),
        )
        run_ids = [r["run_id"] for r in cur.fetchall()]
        if run_ids:
            # 分批 IN 查询：避免超出 SQLite 绑定变量上限（旧版默认 999）
            for start in range(0, len(run_ids), 500):
                chunk = run_ids[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                cur = conn.execute(
                    f"SELECT * FROM probes WHERE query=? AND run_id IN ({placeholders}) "
                    "ORDER BY run_at, id",
                    [query, *chunk],
                )
                rows.extend(dict(r) for r in cur.fetchall())
        # 旧数据（无 run_id）：单采样时代，一行一 run，按行取最近 max_runs 行
        cur = conn.execute(
            "SELECT * FROM probes WHERE query=? AND run_id IS NULL "
            "ORDER BY run_at DESC, id DESC LIMIT ?",
            (query, max_runs),
        )
        rows.extend(dict(r) for r in cur.fetchall())
        return rows
    finally:
        conn.close()


def build_trend(query: str, db_path: Path = DEFAULT_DB) -> dict:
    """按平台/run_at 聚合历史快照，生成带概率的时间序列并找出引用状态的变化点"""
    # 按 run 完整加载最近 1000 次运行（多采样组不截断），避免样本组被行数限流切半
    import search_ai  # 懒加载：测试 monkeypatch 目标是 search_ai._recent_run_rows
    rows = search_ai._recent_run_rows(query, db_path=db_path, max_runs=1000)
    by_platform: dict[str, list[dict]] = {}
    for row in rows:
        by_platform.setdefault(row["platform"], []).append(row)

    series: dict[str, list[dict]] = {}
    changes: list[dict] = []
    for platform, items in by_platform.items():
        ordered = sorted(items, key=lambda r: r["run_at"])
        # 多采样：同一 run_at 的 N 行聚合成一个带概率的点
        # 多采样：同一 run 的 N 行按 run_id 聚合成一个带概率的点；
        # run_id 硬区分同一时间戳的不同独立运行；旧数据（run_id 为空）回退按 run_at 合并
        by_run_id: dict[str, list[dict]] = {}
        for r in ordered:
            rid = r["run_id"] or f"legacy:{r['run_at']}"
            by_run_id.setdefault(rid, []).append(r)
        # 批次顺序：先按 run_at（补录历史时 run_at 与插入顺序不一致），
        # 同 run_at 再按最小行 id（插入顺序）
        batches = sorted(
            by_run_id.items(),
            key=lambda item: (
                item[1][0]["run_at"],
                min(row["id"] for row in item[1]),
            ),
        )
        points: list[dict] = []
        for _rid, group in batches:
            run_at = group[0]["run_at"]
            # cited 为 NULL 的失败/未配置样本不计入概率分母
            agg = _aggregate_binary([
                r["cited"] for r in group if r["cited"] is not None
            ])
            invalid = len(group) - agg["n"]
            n, hits = agg["n"], agg["hits"]
            prob, ci_low, ci_high = agg["prob"], agg["ci_low"], agg["ci_high"]
            cited = agg["cited"]

            # 取首个「解析后非空」的 mine_ids：空 JSON 字符串 "[]" 不得提前停止
            mine_ids = next(
                (
                    parsed
                    for r in group
                    if (parsed := _parse_mine_ids(r["mine_ids"]))
                ),
                [],
            )
            competitor_matched = _aggregate_bool_tristate([
                bool(r["competitor_matched"])
                for r in group
                if r["competitor_matched"] is not None
            ])
            mine_cited = _aggregate_binary([
                r["mine_cited"] for r in group if r["mine_cited"] is not None
            ])["cited"]
            status = _majority([r["status"] for r in group]) or "error"
            if status != "ok":
                # 整体失败/未配置：cited/mine_cited 置 None，
                # 变化点检测跳过（现有 None 逻辑）、趋势显示「未知」，与表格口径一致
                cited = None
                mine_cited = None
            points.append({
                "run_at": run_at,
                "n": n,
                "hits": hits,
                "invalid": invalid,
                "prob": prob,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "cited": cited,
                "status": status,
                "sentiment": _majority([r["sentiment"] for r in group]),
                "mine_cited": mine_cited,
                "mine_checked": bool(mine_ids),
                "mine_ids": mine_ids,
                "cited_type": _majority([r["cited_type"] for r in group]),
                "owned_ids": next(
                    (
                        parsed
                        for r in group
                        if (parsed := _parse_mine_ids(r["owned_ids"]))
                    ),
                    [],
                ),
                "competitor_matched": competitor_matched,
                "competitor_ids": next(
                    (
                        parsed
                        for r in group
                        if (parsed := _parse_mine_ids(r["competitor_ids"]))
                    ),
                    [],
                ),
                "fact_risks": _union_strings([
                    _parse_mine_ids(r["fact_risks"]) for r in group
                ]),
            })
        # 按 run 截断：每平台保留最近 1000 次运行（与单采样时代 limit=1000 行的
        # 可回溯范围一致，多采样后按 run 计数而不是按行计数）；
        # 变化点检测也基于截断后的序列，避免引用展示范围外的 run_at
        points = points[-1000:]
        series[platform] = points
        prev: bool | None = None
        for item in points:
            cur_val = bool(item["cited"]) if item["cited"] is not None else None
            if cur_val is None:
                # 无效探测（未配置密钥/失败）不参与变化点，也不重置基线，
                # 这样隔次的有效对比仍能检出引用状态变化
                continue
            if prev is not None and prev != cur_val:
                changes.append({
                    "platform": platform,
                    "from": prev,
                    "to": cur_val,
                    "run_at": item["run_at"],
                })
            prev = cur_val
    return {
        "query": query,
        "series": series,
        "changes": changes,
        "total_runs": len({r["run_at"] for r in rows}),
    }


def build_delta(
    query: str,
    db_path: Path = DEFAULT_DB,
    trend: dict | None = None,
    platforms: list[str] | None = None,
) -> dict:
    """本次与上次有效快照的对比基线：引用新增/丢失、情感反转、我的内容变化"""
    trend = trend or build_trend(query, db_path=db_path)
    series = trend["series"]
    if platforms is not None:
        requested = set(platforms)
        series = {p: pts for p, pts in series.items() if p in requested}
    delta_platforms: dict[str, dict] = {}
    for platform, points in series.items():
        if not points:
            continue
        # 不依赖外部 trend 的构造顺序，显式按 run_at 升序取最新点
        points = sorted(points, key=lambda p: p["run_at"])
        latest = points[-1]
        if latest["status"] != "ok":
            # 本次探测无有效数据：不把历史两次快照的对比误报成「本次 vs 上次」
            delta_platforms[platform] = {
                "run_at": latest["run_at"],
                "status": latest["status"],
                "note": "本次探测无有效数据，未参与与上次对比",
            }
            continue
        valid = [p for p in points if p["status"] == "ok"]
        if len(valid) < 2:
            continue
        last, prev = valid[-1], valid[-2]
        item: dict = {"run_at": last["run_at"], "previous_run_at": prev["run_at"]}
        if last["cited"] is not None and prev["cited"] is not None:
            item["cited"] = last["cited"]
            item["cited_prev"] = prev["cited"]
            if last["cited"] and not prev["cited"]:
                item["cited_change"] = "added"
            elif not last["cited"] and prev["cited"]:
                item["cited_change"] = "lost"
            else:
                item["cited_change"] = "same"
        if last["sentiment"] and prev["sentiment"] and last["sentiment"] != prev["sentiment"]:
            item["sentiment_flip"] = f"{prev['sentiment']}→{last['sentiment']}"
        if last["mine_checked"] and prev["mine_checked"] and last["mine_cited"] is not None and prev["mine_cited"] is not None:
            if last["mine_cited"] and not prev["mine_cited"]:
                item["mine_change"] = "gained"
            elif not last["mine_cited"] and prev["mine_cited"]:
                item["mine_change"] = "lost"
        # lostprompt：上次被引用、本次未被引用但话题仍被提及、本次检出竞品内容出现
        # 上一轮竞品已命中（competitor_matched=True）时不判「夺走」——竞品一直在场，
        # 本轮丢失引用不是被替换；上一轮未查（None）按未命中处理，避免旧数据漏报
        # 前后两次 mine_ids 集合不一致时也不判——用户更换 --mine 标识会导致假回归
        if (
            last["mine_checked"] and prev["mine_checked"]
            and last["mine_ids"] and prev["mine_ids"]
            and set(last["mine_ids"]) == set(prev["mine_ids"])
            and prev["mine_cited"] is True
            and last["mine_cited"] is False
            and last["cited"] is True
            and last["competitor_matched"] is True
            and prev["competitor_matched"] is not True
        ):
            item["competitor_replaced"] = True
            item["competitor_replaced_at"] = last["run_at"]
            # 已确认需要：上次明确未命中竞品（False）且前后竞品标识集合一致；
            # 上次未检查（None）或换了竞品标识 → 推断，待人工确认
            same_competitors = (
                bool(last["competitor_ids"])
                and bool(prev["competitor_ids"])
                and set(last["competitor_ids"]) == set(prev["competitor_ids"])
            )
            item["competitor_replaced_confirmed"] = (
                prev["competitor_matched"] is False and same_competitors
            )
        # 两个有效快照均无可对比数据（cited/sentiment/mine 全缺）时不写入空壳条目，
        # 避免 has_history=False 时渲染出全「—」的对比行
        if any(
            k in item
            for k in ("cited_change", "sentiment_flip", "mine_change", "competitor_replaced")
        ):
            delta_platforms[platform] = item
    # has_history 仅表示存在真实对比（引用/情感/我的内容任一变化或一致判定），
    # 不含「本次无有效数据」的 note 条目，避免 JSON 消费方误读
    compared = [
        item
        for item in delta_platforms.values()
        if any(
            k in item
            for k in ("cited_change", "sentiment_flip", "mine_change", "competitor_replaced")
        )
    ]
    return {"query": query, "platforms": delta_platforms, "has_history": bool(compared)}
