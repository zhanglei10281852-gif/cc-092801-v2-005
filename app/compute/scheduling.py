"""可解释的排班打分。

领取顺序由四个可叠加的分项决定，全部为整数分值，便于向现场负责人解释：

1. 基础紧急度 ``priority``：订单提交时给出，范围 0-100。
2. 等待补偿：排队每多一分钟增加固定分值，长期未被领取的订单会逐步追上高优先级订单。
3. 临近开场：开场时间进入关注窗口后线性加分；一旦超过开场时间仍未领取，按更高的速率累计。
4. 技能匹配：工作者具备该订单所需技能时给一次性加分；不具备时该订单不会出现在候选集中。

临时加权（人工 boost）独立累计并带过期时间，避免高优先级标签永久挤压普通订单。
所有分项相同时，按入队时间、任务 id 两级稳定排序，保证相同条件下次序恒定。

SQL（repository 中的排队查询）与本文件的 :func:`score_components` 使用同一套常量与
整数取整规则，变更公式时两处必须同步修改，并由回归测试锁定一致性。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from app.core.clock import from_storage


@dataclass(frozen=True, slots=True)
class SchedulerWeights:
    wait_comp_per_minute: int = 1
    """排队等待每分钟补偿的分值。"""

    overdue_per_minute: int = 2
    """超过开场时间后每分钟累计的紧急分值。"""

    start_window_minutes: int = 120
    """距离开场多少分钟内开始计入临近开场加分。"""

    skill_match_bonus: int = 15
    """工作者技能与订单要求匹配时的一次性加分。"""

    max_boost_points: int = 40
    """单次临时加权允许的最大分值。"""

    boost_cap_seconds: int = 7200
    """临时加权最长有效期（秒），防止变相永久提级。"""


DEFAULT_WEIGHTS = SchedulerWeights()


def _whole_minutes(delta_seconds: float) -> int:
    """向零取整的分钟数；SQL 中 CAST(... AS INTEGER) 对正值等价于向下取整。"""
    return int(delta_seconds // 60)


def urgency_component(event_starts_at: str | None, now: datetime, weights: SchedulerWeights) -> int:
    starts = from_storage(event_starts_at) if event_starts_at else None
    if starts is None:
        return 0
    delta = (starts - now).total_seconds()
    if delta < 0:
        # 已过开场时间：窗口满分 + 每分钟额外累计，保证过开场后次序只升不降。
        return weights.start_window_minutes + _whole_minutes(-delta) * weights.overdue_per_minute
    minutes_to_start = _whole_minutes(delta)
    if minutes_to_start >= weights.start_window_minutes:
        return 0
    return weights.start_window_minutes - minutes_to_start


def score_components(
    task: Any,
    now: datetime,
    capabilities: Iterable[str] | None,
    weights: SchedulerWeights = DEFAULT_WEIGHTS,
) -> dict[str, Any]:
    """计算单个订单的打分明细，供领取响应与队列预览做解释。

    ``task`` 可以是 sqlite3.Row/dict；boost_points 由调用方从排队查询结果注入。
    """
    get = task.__getitem__ if hasattr(task, "__getitem__") else lambda key: getattr(task, key)
    priority = int(get("priority"))
    ranked_at = from_storage(get("queue_rank_at"))
    waited = _whole_minutes((now - ranked_at).total_seconds()) if ranked_at else 0
    waited = max(0, waited)
    wait_bonus = waited * weights.wait_comp_per_minute
    starts_value = get("event_starts_at")
    urgency = urgency_component(starts_value if starts_value else None, now, weights)
    required = get("required_skill") or None
    # capabilities=None 表示没有具体工作者上下文（如订单详情的信息性打分），不计技能分；
    # [] 表示“不限技能的工作者”，与 SQL 的空能力过滤一致，技能单照常拿匹配分。
    if capabilities is None:
        skill_matched = False
        skill_bonus = 0
    else:
        capability_set = set(capabilities)
        skill_matched = bool(required) and (not capability_set or required in capability_set)
        skill_bonus = weights.skill_match_bonus if skill_matched else 0
    try:
        boost_points = int(get("boost_points") or 0)
    except (KeyError, IndexError):
        boost_points = 0
    total = priority + wait_bonus + urgency + skill_bonus + boost_points
    return {
        "total_score": total,
        "base_priority": priority,
        "waited_minutes": waited,
        "wait_compensation": wait_bonus,
        "urgency": urgency,
        "event_starts_at": starts_value or None,
        "required_skill": required or None,
        "skill_matched": skill_matched,
        "skill_bonus": skill_bonus,
        "manual_boost": boost_points,
    }


def score_sql_expression(weights: SchedulerWeights = DEFAULT_WEIGHTS) -> str:
    """与 :func:`score_components` 等价的 SQLite 打分表达式。

    占位符全部为 qmark：等待项的当前时间出现 2 次（WHEN 与 THEN 各一次），
    开场紧急度出现 4 次，随后 1 个技能加分。参数用 :func:`score_sql_params` 绑定。
    表达式引用了 ``t`` 与聚合临时表别名 ``b``（boost_points）。
    """
    w = weights
    # 存储值为 UTC ISO 文本（带 +00:00 后缀）；substr 去掉偏移后用 strftime('%s')
    # 得到整秒，再做整数分钟运算，与 Python 端 datetime 相减取整完全一致，
    # 避免 julianday 浮点误差在整分钟边界造成一分偏差（不同版本 SQLite 对时区偏移解析不一致）。
    epoch_now = "strftime('%s',substr(?,1,19))"
    epoch_rank = "strftime('%s',substr(t.queue_rank_at,1,19))"
    epoch_start = "strftime('%s',substr(t.event_starts_at,1,19))"
    minutes_since_rank = f"CAST(({epoch_now}-{epoch_rank})/60 AS INTEGER)"
    wait = f"(CASE WHEN {minutes_since_rank} > 0 THEN {minutes_since_rank} ELSE 0 END)*{w.wait_comp_per_minute}"
    seconds_to_start = f"({epoch_start}-{epoch_now})"
    seconds_overdue = f"({epoch_now}-{epoch_start})"
    urgency = (
        "CASE WHEN t.event_starts_at<>'' THEN "
        f"CASE WHEN {seconds_to_start} < 0 THEN {w.start_window_minutes}+CAST({seconds_overdue}/60.0 AS INTEGER)*{w.overdue_per_minute} "
        f"WHEN CAST({seconds_to_start}/60.0 AS INTEGER) >= {w.start_window_minutes} THEN 0 "
        f"ELSE {w.start_window_minutes}-CAST({seconds_to_start}/60.0 AS INTEGER) END "
        "ELSE 0 END"
    )
    skill = "(CASE WHEN t.required_skill='' THEN 0 ELSE ? END)"
    return f"(t.priority + {wait} + ({urgency}) + {skill} + COALESCE(b.boost_points,0))"


def score_sql_params(now: str, skill_bonus: int) -> list[Any]:
    """按 :func:`score_sql_expression` 中占位符的文本顺序返回绑定参数。"""
    return [now] * 6 + [skill_bonus]
