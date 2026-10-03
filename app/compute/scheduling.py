"""可解释的仪式服务排班策略。

评分由若干有界组件构成，避免单一“高优先级”标签永久挤压普通订单：

- 临近开场：场次开始时间越近得分越高，开场后按满分计；
- 服务紧急度：1~5 级线性映射；
- 等待时长：短期等待线性给分；
- 长期等待补偿：超过宽限期后按固定时间步逐步加分（老化补偿）；
- 基础优先级：人工标签只占较小权重，封顶 15 分；
- 技能匹配：领取者对该单所需技能的等级越高越靠前；
- 临时加权：人工临时提权，有分值上限和有效期，到期自动失效。

评分全部在此模块用纯函数完成（SQLite 只负责资格过滤），保证解释
文案与实际排序来自同一套公式，进程重启后只要数据库状态与时钟一致，
排序就一致。
"""

from __future__ import annotations

from typing import Any

# 临近开场：开场前 180 分钟进入窗口，开场时刻（及超时后）拿满分。
START_WINDOW_MINUTES = 180.0
START_MAX = 30.0

# 服务紧急度（任务 urgency 取值 1~5）。
URGENCY_MAX = 20.0

# 短期等待：前 30 分钟线性增长到 10 分。
WAIT_BASE_SPAN_MINUTES = 30.0
WAIT_BASE_MAX = 10.0

# 老化补偿：排队超过宽限期后，每经过一个时间步增加固定分值，直至封顶。
AGING_GRACE_MINUTES = 15.0
AGING_STEP_MINUTES = 10.0
AGING_PER_STEP = 2.0
AGING_CAP = 25.0

# 人工优先级标签只占总权重中的较小部分。
PRIORITY_MAX = 15.0

# 技能等级（1~5）映射到 0~5 分；只满足领取门槛（1 级）不加分。
SKILL_MAX = 5.0

# 临时提权的分值上限与有效期边界（秒）。
OVERRIDE_BONUS_MAX = 40.0
OVERRIDE_TTL_MIN_SECONDS = 60
OVERRIDE_TTL_MAX_SECONDS = 24 * 3600


def aging_steps(wait_minutes: float) -> int:
    """超过宽限期后已经跨过的老化档位。"""
    if wait_minutes < AGING_GRACE_MINUTES:
        return 0
    return int((wait_minutes - AGING_GRACE_MINUTES) // AGING_STEP_MINUTES)


def score_components(
    *,
    has_start: bool,
    minutes_to_start: float,
    urgency: int,
    wait_minutes: float,
    priority: int,
    skill_level: int,
    override_bonus: float,
) -> dict[str, float]:
    """计算各组件分值，所有输入均已归一化到各自定义域。"""
    if has_start:
        start_ratio = max(0.0, min(1.0, 1.0 - minutes_to_start / START_WINDOW_MINUTES))
        score_start = round(START_MAX * start_ratio, 4)
    else:
        score_start = 0.0
    score_urgency = round(URGENCY_MAX * (urgency - 1) / 4.0, 4)
    score_wait_base = round(
        WAIT_BASE_MAX * min(max(wait_minutes, 0.0), WAIT_BASE_SPAN_MINUTES) / WAIT_BASE_SPAN_MINUTES,
        4,
    )
    steps = aging_steps(wait_minutes)
    score_aging = round(min(AGING_CAP, steps * AGING_PER_STEP), 4)
    score_priority = round(PRIORITY_MAX * priority / 100.0, 4)
    score_skill = round(SKILL_MAX * (skill_level - 1) / 4.0, 4)
    score_override = round(max(0.0, min(OVERRIDE_BONUS_MAX, override_bonus)), 4)
    total = round(
        score_start
        + score_urgency
        + score_wait_base
        + score_aging
        + score_priority
        + score_skill
        + score_override,
        4,
    )
    return {
        "start": score_start,
        "urgency": score_urgency,
        "wait_base": score_wait_base,
        "aging": score_aging,
        "priority": score_priority,
        "skill": score_skill,
        "override": score_override,
        "total": total,
    }


def explain(components: dict[str, float], *, aging_step_count: int, has_override: bool) -> dict[str, Any]:
    """把分值翻译成现场负责人可以阅读的中文说明。"""
    reasons = [
        f"临近开场 {components['start']:.2f} 分",
        f"服务紧急度 {components['urgency']:.2f} 分",
        f"等待时长 {components['wait_base']:.2f} 分",
        f"长期等待补偿 {components['aging']:.2f} 分（已累计 {aging_step_count} 档）",
        f"优先级标签 {components['priority']:.2f} 分",
        f"技能匹配 {components['skill']:.2f} 分",
    ]
    if has_override:
        reasons.append(f"临时加权 {components['override']:.2f} 分（带有效期，到期自动失效）")
    reasons.append(f"合计 {components['total']:.2f} 分")
    return {"score": components["total"], "breakdown": components, "reasons": reasons}


def ranking_key(row: dict[str, Any], components: dict[str, float]) -> tuple[Any, ...]:
    """稳定平局打破：总分降序 → 更早开场 → 更早入队 → 更小 id。

    未设置开场时间的订单排在同分时设有开场时间的订单之后。
    """
    start_at = row.get("start_at") or ""
    queued_since = row.get("queued_since") or row.get("created_at") or ""
    return (
        -components["total"],
        1 if not start_at else 0,
        start_at,
        queued_since,
        int(row["id"]),
    )
