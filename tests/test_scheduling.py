"""可解释排班策略的回归测试。

覆盖：开场时间/紧急度/等待补偿/技能匹配的综合顺序、长期等待补偿反超、
相同条件的稳定次序、临时加权的原因与到期、取消与重新排队不产生重复领取、
打分明细落审计，以及进程重启后的队列一致性。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock, to_storage
from app.database import close_connection, get_connection, init_db

BASE = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)


def make_template(code: str, algorithm: str) -> dict:
    return {
        "code": code,
        "name": f"模板-{code}",
        "algorithm": algorithm,
        "parameter_schema": {"seat": {"type": "integer", "minimum": 1}},
        "default_parameters": {},
        "max_runtime_seconds": 300,
        "max_attempts": 2,
    }


@pytest.fixture()
def service_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TOWNSHIP_DATABASE_PATH", str(tmp_path / "scheduling.db"))
    close_connection()
    init_db()
    clock = FrozenClock(BASE)
    service = ComputeOperationsService(get_connection(), clock)
    for code, algorithm in [("wedding", "wedding-host"), ("memorial", "memorial-rite"), ("setup", "setup-crew")]:
        service.create_template(make_template(code, algorithm), "planner")
    yield service, clock
    close_connection()


def submit(service, key, *, priority=50, skill="", starts_in=None, at=None):
    payload = {
        "template_code": "wedding",
        "project_code": "weekend",
        "requested_by": "planner",
        "parameters": {"seat": 10},
        "priority": priority,
        "idempotency_key": key,
        "required_skill": skill,
    }
    if starts_in is not None:
        payload["event_starts_at"] = (at or service.clock.now()) + timedelta(minutes=starts_in)
    return service.submit(payload)


# ---------------------------------------------------------------------------
# 1. 综合顺序：开场临近 + 等待补偿可以反超静态高优先级
# ---------------------------------------------------------------------------

def test_urgency_and_wait_compensation_overtake_static_priority(service_env):
    service, clock = service_env
    # 高优先级普通婚礼：开场很远（窗口外），无紧急加分。
    high = submit(service, "sched-high", priority=90, starts_in=300)
    # 普通追思订单：60 分钟后开场 → 临近开场分 60，立即就已 110 > 90。
    memorial = service.submit({
        "template_code": "memorial", "project_code": "weekend", "requested_by": "planner",
        "parameters": {"seat": 4}, "priority": 50, "idempotency_key": "sched-soon",
        "required_skill": "memorial-rite",
        "event_starts_at": clock.now() + timedelta(minutes=60),
    })
    # 普通婚礼：开场 90 分钟后（窗口内 30 分），再等 61 分钟，等待补偿 61 分。
    patient = submit(service, "sched-wait", priority=50, starts_in=90)

    order_now = [item["id"] for item in service.queue_preview(["wedding-host", "memorial-rite", "setup-crew"])]
    assert order_now[0] == memorial["id"]

    clock.advance(minutes=61)
    claimed = service.claim("worker-1", ["wedding-host", "memorial-rite", "setup-crew"], 60)
    # 追思（50 + 61 等待 + 超开场 1 分钟 122 = 233）先领；
    # 随后普通等待单（50 + 61 等待 + 距开场 29 分钟 → 91 = 202）
    # 高于静态高优先级单（90 + 0 + 0）。
    assert claimed["id"] == memorial["id"]
    claimed_two = service.claim("worker-1", ["wedding-host", "memorial-rite", "setup-crew"], 60)
    assert claimed_two["id"] == patient["id"]
    assert claimed_two["score"]["wait_compensation"] == 61
    assert claimed_two["score"]["urgency"] == 91
    last = service.claim("worker-1", ["wedding-host", "memorial-rite", "setup-crew"], 60)
    assert last["id"] == high["id"]


def test_overdue_start_accumulates_faster(service_env):
    service, clock = service_env
    task = submit(service, "sched-overdue", priority=10, starts_in=30)
    clock.advance(minutes=45)  # 已过开场 15 分钟
    detail = service.queue_preview([])[0]["score"]
    # 窗口满分 120 + 超开场 15 分钟 × 2 = 150。
    assert detail["urgency"] == 120 + 15 * 2


# ---------------------------------------------------------------------------
# 2. 技能匹配：不具备技能不可领取；显式技能匹配有加分
# ---------------------------------------------------------------------------

def test_skill_gating_and_match_bonus(service_env):
    service, clock = service_env
    skilled = submit(service, "sched-skilled", priority=50, skill="memorial-rite", starts_in=100)
    unskilled = submit(service, "sched-unskilled", priority=55)
    # 仅具婚礼技能的工作者看不到追思技能单。
    only = service.queue_preview(["wedding-host"])
    assert [item["id"] for item in only] == [unskilled["id"]]
    assert service.claim("host-only", ["wedding-host"], 60)["id"] == unskilled["id"]
    # 追思工作者领取技能单，带 +15 匹配分。
    claimed = service.claim("rite-worker", ["memorial-rite"], 60)
    assert claimed["id"] == skilled["id"]
    assert claimed["score"]["skill_bonus"] == 15
    assert claimed["score"]["skill_matched"] is True
    # 无任何技能的工作者（空能力）按原语义可承接算法匹配外的任意订单；队列已空。
    assert service.claim("generic", [], 60) is None


# ---------------------------------------------------------------------------
# 3. 相同条件稳定次序（跨重启一致）
# ---------------------------------------------------------------------------

def test_tie_break_is_stable_and_survives_restart(service_env, monkeypatch):
    service, clock = service_env
    ids = [submit(service, f"sched-tie-{index:02d}", priority=50)["id"] for index in range(4)]
    preview = service.queue_preview([])
    assert [item["id"] for item in preview] == ids
    assert [item["score"]["total_score"] for item in preview] == [50] * 4

    # 模拟进程重启：关闭线程连接后重新打开，打分完全由数据库中的字段推导。
    close_connection()
    restarted = ComputeOperationsService(get_connection(), clock)
    assert [item["id"] for item in restarted.queue_preview([])] == ids

    claimed_ids = []
    for _ in range(4):
        task = restarted.claim("worker-restart", [], 60)
        claimed_ids.append(task["id"])
    assert claimed_ids == ids


# ---------------------------------------------------------------------------
# 4. 临时加权必须有原因、到期自动失效，且写入审计
# ---------------------------------------------------------------------------

def test_boost_requires_reason_expires_and_is_audited(service_env):
    service, clock = service_env
    low = submit(service, "sched-boost-low", priority=10)
    high = submit(service, "sched-boost-high", priority=30)
    with pytest.raises(Exception):
        service.boost(low["id"], "dispatcher", "   ", 30, 1800)
    record = service.boost(low["id"], "dispatcher", "家属已到场需要提前布置", 30, 1800)
    assert record["points"] == 30 and record["reason"] == "家属已到场需要提前布置"

    preview = service.queue_preview([])
    assert preview[0]["id"] == low["id"]
    assert preview[0]["score"]["manual_boost"] == 30
    claimed = service.claim("worker-b", [], 60)
    assert claimed["id"] == low["id"]

    details = service.get_task(low["id"])
    intervention = details["interventions"][-1]
    assert intervention["action"] == "boost" and "家属已到场" in intervention["reason"]
    assert details["claim_events"][-1]["score_breakdown"]["manual_boost"] == 30

    # 加权只影响排队顺序，不改变永久优先级。
    assert details["priority"] == 10
    # 到期后新的加权查询不再计入。
    clock.advance(seconds=1801)
    high_requeue = service.get_task(high["id"])
    assert high_requeue["current_score"]["manual_boost"] == 0
    assert service.queue_preview([])[0]["id"] == high["id"]


def test_boost_is_capped_and_ttl_bounded(service_env):
    service, _ = service_env
    task = submit(service, "sched-boost-cap", priority=0)
    record = service.boost(task["id"], "dispatcher", "临时救场", 999, 99999)
    assert record["points"] == 40
    from app.core.clock import from_storage
    ttl = (from_storage(record["expires_at"]) - from_storage(record["created_at"])).total_seconds()
    assert ttl == 7200


# ---------------------------------------------------------------------------
# 5. 取消 / 重新排队不会造成重复领取
# ---------------------------------------------------------------------------

def test_cancel_requested_never_reenters_queue(service_env):
    service, _ = service_env
    task = submit(service, "sched-cancel-run", priority=80)
    assert service.claim("worker-a", [], 60)["id"] == task["id"]
    # 运行中取消只是请求取消：订单绝不会重新进入队列被他人重复领取。
    cancelled = service.cancel(task["id"], "planner", "家属改期")
    assert cancelled["status"] == "cancel_requested"
    assert service.queue_preview([]) == []
    assert service.claim("worker-b", [], 60) is None
    from app.core.errors import ConflictError
    with pytest.raises(ConflictError):
        service.retry(task["id"], "planner", "想重新排队")


def test_failure_requeue_and_manual_retry_do_not_double_claim(service_env):
    service, clock = service_env
    task = submit(service, "sched-once", priority=80)
    assert service.claim("worker-a", [], 60)["id"] == task["id"]

    # 可重试失败后回到队列（带回避延迟），且只能被领取一次。
    failed = service.fail(task["id"], "worker-a", "transient", "现场停电", True)
    assert failed["status"] == "queued"
    clock.advance(seconds=5)
    first = service.claim("worker-c", [], 60)
    assert first["id"] == task["id"] and first["attempt_count"] == 2
    assert service.claim("worker-d", [], 60) is None

    # 终态失败后人工重试：同一订单在队列中只出现一次。
    service.fail(task["id"], "worker-c", "fatal", "无法执行", False)
    assert service.get_task(task["id"])["status"] == "failed"
    retried = service.retry(task["id"], "planner", "问题已解决重新安排")
    assert retried["status"] == "queued"
    queued_ids = [item["id"] for item in service.queue_preview([])]
    assert queued_ids.count(task["id"]) == 1
    assert service.claim("worker-e", [], 60)["id"] == task["id"]
    assert service.claim("worker-f", [], 60) is None


def test_cancel_queued_task_removes_it_from_queue(service_env):
    service, _ = service_env
    task = submit(service, "sched-cancel-q", priority=80)
    result = service.cancel(task["id"], "planner", "场次取消")
    assert result["status"] == "cancelled"
    assert service.queue_preview([]) == []
    assert service.claim("worker-a", [], 60) is None


def test_lease_recovery_requeues_once_with_rank_reset(service_env):
    service, clock = service_env
    task = submit(service, "sched-lease", priority=70)
    service.claim("lost-worker", [], 10)
    clock.advance(seconds=11)
    result = service.recover_expired()
    assert result["recovered"] == [task["id"]]
    details = service.get_task(task["id"])
    # 恢复后重新排队，排队基准被重置为恢复时刻（不会带着陈旧补偿无限靠前）。
    assert details["queue_rank_at"] == to_storage(clock.now())
    assert service.claim("new-worker", [], 60)["id"] == task["id"]
    assert service.claim("other-worker", [], 60) is None
    assert details["interventions"][-1]["action"] == "lease_recovery"


# ---------------------------------------------------------------------------
# 6. 领取审计包含完整打分明细，SQL 与 Python 打分逐单一致
# ---------------------------------------------------------------------------

def test_claim_audit_breakdown_and_sql_python_parity(service_env):
    service, clock = service_env
    submit(service, "sched-audit-1", priority=70, skill="wedding-host", starts_in=45)
    submit(service, "sched-audit-2", priority=30, starts_in=200)
    clock.advance(minutes=30)
    items = service.queue_preview(["wedding-host"])
    for item in items:
        # SQLite 总分与 Python 打分明细必须完全相等。
        assert item["total_score"] == item["score"]["total_score"]

    claimed = service.claim("host", ["wedding-host"], 60)
    events = service.get_task(claimed["id"])["claim_events"]
    assert len(events) == 1
    event = events[0]
    assert event["worker_id"] == "host"
    assert event["score_total"] == claimed["score"]["total_score"]
    breakdown = event["score_breakdown"]
    assert set(breakdown) >= {
        "total_score", "base_priority", "wait_compensation", "waited_minutes",
        "urgency", "skill_bonus", "manual_boost", "event_starts_at", "required_skill",
    }


# ---------------------------------------------------------------------------
# 7. 接口层：创建带场次与技能的订单、临时加权与队列预览
# ---------------------------------------------------------------------------

def test_api_scheduling_fields_and_preview(client):
    tpl = {
        "code": "rite-api",
        "name": "追思礼仪模板",
        "algorithm": "memorial-rite",
        "parameter_schema": {"hall": {"type": "string", "required": True}},
        "default_parameters": {},
        "max_runtime_seconds": 300,
        "max_attempts": 2,
    }
    created = client.post("/api/compute/templates?actor=planner", json=tpl)
    assert created.status_code == 201, created.text
    starts = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    payload = {
        "template_code": "rite-api", "project_code": "ceremony-周日",
        "requested_by": "planner-王", "parameters": {"hall": "明德厅"},
        "priority": 40, "idempotency_key": "api-ceremony-01",
        "event_starts_at": starts, "required_skill": "memorial-rite",
    }
    response = client.post("/api/compute/tasks", json=payload)
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    assert response.json()["required_skill"] == "memorial-rite"

    boost = client.post(
        f"/api/compute/tasks/{task_id}/boost",
        json={"actor": "现场负责人", "reason": "家属提前到达", "points": 25, "ttl_seconds": 900},
    )
    assert boost.status_code == 200, boost.text
    assert boost.json()["points"] == 25

    preview = client.get("/api/compute/queue/preview", params={"capabilities": "memorial-rite", "limit": 5})
    assert preview.status_code == 200
    item = preview.json()["items"][0]
    assert item["id"] == task_id
    assert item["score"]["manual_boost"] == 25
    assert item["score"]["urgency"] >= 60  # 距开场约 1 小时

    # 原因是必填审计字段。
    bad = client.post(
        f"/api/compute/tasks/{task_id}/boost",
        json={"actor": "现场负责人", "reason": "x", "points": 25},
    )
    assert bad.status_code == 422
