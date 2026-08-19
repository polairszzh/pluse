"""track 数据模型（从 search_ai.py 拆分，行为不变）"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProbeResult:
    """单个平台的一次探测结果"""

    query: str
    platform: str
    status: str                  # ok | no_key | error
    cited: bool | None           # 是否被提及；未知时为 None
    sentiment: str | None        # positive | neutral | negative | None
    context: str                 # 上下文/说明摘要
    source: str                  # api | search_inference
    degraded: bool               # 是否为降级信号（非真实引用）
    error: str | None = None
    meta: dict = field(default_factory=dict)
    mine_cited: bool | None = None      # 我的内容标识是否出现在探测结果中
    mine_ids: list[str] = field(default_factory=list)  # 本次检查的我的内容标识
    confidence: str | None = None      # confirmed(真实API) | likely(搜索推断) | hypothesis(启发式)
    cited_type: str | None = None       # mine 命中时的引用类型：earned(原创被引) | owned(转载/自有渠道被引)
    owned_ids: list[str] = field(default_factory=list)  # 本次检查的转载/自有渠道标识
    competitor_matched: bool | None = None  # 本次探测是否检测到竞品内容出现
    competitor_ids: list[str] = field(default_factory=list)  # 本次检查的竞品标识
    fact_risks: list[str] = field(default_factory=list)  # 回答中关于品牌的未核实数字断言
    sample_idx: int = 0             # 多采样编号（同一 run_at 内 0..N-1）
    prob: float | None = None       # 聚合后：被提及概率（命中数 / 样本数）
    ci_low: float | None = None     # 聚合后：Wilson 置信区间下界
    ci_high: float | None = None    # 聚合后：Wilson 置信区间上界
    sample_count: int = 1           # 聚合后：样本数
