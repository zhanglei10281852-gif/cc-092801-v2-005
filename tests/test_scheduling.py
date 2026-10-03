from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.compute import scheduling
from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock
from app.database import close_connection, get_connection


WEDDING = {
    "code": "wedding-ceremony",
    "name": "婚礼现场服务模板",
    "algorithm": "wedding-ceremony",
    "parameter_schema": {"guests": {"type": "integer", "required": True, "minimum": 1, "maximum": 5000}},
    "default_parameters": {},
    "max_runtime_seconds": 600,
    "max_attempts": 3,
}
MEMORIAL = {
    "code": "memorial-ceremony",
    "name": "追思仪式现场服务模板",
    "algorithm": "memorial-ceremony",
    "parameter_schema": {"hall": {"type": "string", "required": True}},
    "default_parameters": {},
    "max_runtime_seconds": 600,
    "max_attempts": 3,
}

ALL_CAPABILITIES = ["wedding-ceremony", "memorial-ceremony"]
START = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)  # 周日清晨


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TOWNSHIP_DATABASE_PATH", str(tmp_path / "schedule.db"))
    close_connection()
    from app.database import init_db

    init_db()
    clock = FrozenClock(START)
    ops = ComputeOperationsService(get_connection(), clock)
    ops.create_template(WEDDING, "dispatcher")
    ops.create_template(MEMORIAL, "dispatcher")
    return ops


def iso_at(**delta) -> str:
    return (START + timedelta(**delta)).isoformat()


def submit(service, key, *, template="wedding-ceremony", urgency=3, priority=50, start_in=None,
           skill="", min_level=1, at=None):
    payload = {
        "template_code": template,
        "project_code": "weekend",
        "requested_by": "family-1",
        "parameters": {"guests": 100} if template == "wedding-ceremony" else {"hall": "念慈厅"},
        "priority": priority,
        "idempotency_key": key,
        "urgency": urgency,
        "required_skill": skill,
        "min_skill_level": min_level,
    }
    if start_in is not None:
        payload["start_at"] = iso_at(minutes=start_in)
    if at is not None:
        service.clock.current = at  # type: ignore[attr-defined]
    return service.submit(payload)


# ---------- 纯策略公式 ----------

def test_score_components_bounds_and_aging():
    fresh = scheduling.score_components(has_start=False, minutes_to_start=0, urgency=1, wait_minutes=0,
                                        priority=0, skill_level=1, override_bonus=0)
    assert fresh["total"] == 0
    aged = scheduling.score_components(has_start=False, minutes_to_start=0, urgency=5, wait_minutes=150,
                                       priority=100, skill_level=5, override_bonus=40)
    assert aged["urgency"] == 20
    assert aged["aging"] == 25  # (150-15)//10 = 13 档 * 2，已到封顶
    assert aged["wait_base"] == 10
    assert aged["priority"] == 15
    assert aged["skill"] == 5
    assert aged["override"] == 40
    assert aged["total"] == 115


def test_start_window_and_aging_steps():
    at_window_start = scheduling.score_components(has_start=True, minutes_to_start=180, urgency=3,
                                                  wait_minutes=0, priority=50, skill_level=1, override_bonus=0)
    assert at_window_start["start"] == 0
    one_hour_before = scheduling.score_components(has_start=True, minutes_to_start=60, urgency=3,
                                                  wait_minutes=0, priority=50, skill_level=1, override_bonus=0)
    assert one_hour_before["start"] == 20
    already_started = scheduling.score_components(has_start=True, minutes_to_start=-30, urgency=3,
                                                  wait_minutes=0, priority=50, skill_level=1, override_bonus=0)
    assert already_started["start"] == 30
    assert scheduling.aging_steps(14) == 0
    assert scheduling.aging_steps(25) == 1
    assert scheduling.aging_steps(10**6) == 99998


# ---------- 领取顺序：开场时间、紧急度、等待、技能 ----------

def test_claim_order_combines_start_urgency_wait_and_skills(service):
    # A：60 分钟后开场，普通紧急度
    a = submit(service, "order-a", start_in=60)
    # B：120 分钟后开场，但特急
    b = submit(service, "order-b", template="memorial-ceremony", urgency=5, start_in=120)
    # C：无开场时间，20 分钟前就已入队
    service.clock.current = START - timedelta(minutes=20)  # type: ignore[attr-defined]
    c = submit(service, "order-c")
    service.clock.current = START  # type: ignore[attr-defined]

    preview = service.preview_queue(ALL_CAPABILITIES)
    assert [item["id"] for item in preview] == [a["id"], b["id"], c["id"]]
    breakdown = preview[0]["schedule"]["breakdown"]
    assert breakdown["start"] == 20
    assert breakdown["urgency"] == 10
    # A 与 B 同分（30），开场更早的 A 胜出：平局次序稳定
    assert preview[0]["schedule"]["score"] == preview[1]["schedule"]["score"]

    claimed = service.claim("worker-1", ALL_CAPABILITIES, 60)
    assert claimed["id"] == a["id"]
    assert claimed["schedule"]["score"] == breakdown["total"]
    assert service.claim("worker-2", ALL_CAPABILITIES, 60)["id"] == b["id"]
    assert service.claim("worker-3", ALL_CAPABILITIES, 60)["id"] == c["id"]
    assert service.claim("worker-4", ALL_CAPABILITIES, 60) is None


def test_skill_gate_level_and_skill_score_ordering(service):
    # 需要三级婚礼司仪
    wedding = submit(service, "skill-wedding", start_in=90, skill="wedding-host", min_level=3)
    # 需要花艺技能（另一模板，避免与上一单除技能外完全相同）
    floral = submit(service, "skill-floral", template="memorial-ceremony", urgency=3, start_in=90, skill="floral", min_level=1)

    # 二级司仪不能领取三级要求的单
    assert service.claim("junior-host", ALL_CAPABILITIES, 60, {"wedding-host": 2, "floral": 1})["id"] == floral["id"]
    # 没有任何相关技能者看不到这两单
    assert service.claim("nobody", ALL_CAPABILITIES, 60) is None
    # 三级司仪可以领取
    senior = service.claim("senior-host", ALL_CAPABILITIES, 60, {"wedding-host": 3})
    assert senior["id"] == wedding["id"]
    assert senior["schedule"]["breakdown"]["skill"] == 2.5


def test_aging_compensation_lifts_long_waiting_normal_order_past_priority_label(service):
    # 普通订单：低优先级标签，但已经等待很久
    service.clock.current = START - timedelta(minutes=200)  # type: ignore[attr-defined]
    normal = submit(service, "old-normal", priority=20)
    # 高优先级标签的新订单，无开场时间
    service.clock.current = START  # type: ignore[attr-defined]
    labelled = submit(service, "new-high-label", priority=100)

    preview = service.preview_queue(ALL_CAPABILITIES)
    assert preview[0]["id"] == normal["id"]
    normal_breakdown = preview[0]["schedule"]["breakdown"]
    assert normal_breakdown["aging"] == 25  # 补偿已到封顶
    assert normal_breakdown["priority"] == 3
    labelled_score = next(item["schedule"]["score"] for item in preview if item["id"] == labelled["id"])
    assert normal_breakdown["total"] > labelled_score


def test_aging_grows_gradually_with_clock(service):
    service.clock.current = START  # type: ignore[attr-defined]
    task = submit(service, "aging-watch", urgency=3)

    def aging_of():
        item = service.preview_queue(ALL_CAPABILITIES)[0]
        return item["schedule"]["breakdown"]["aging"]

    assert aging_of() == 0
    service.clock.advance(minutes=25)
    assert aging_of() == 2  # 跨过 15 分钟宽限后的第一档
    service.clock.advance(minutes=10)
    assert aging_of() == 4
    service.clock.advance(minutes=120)
    assert aging_of() == 25  # 封顶后不再无限挤压别人


def test_equal_conditions_have_stable_order(service):
    first = submit(service, "tie-1")
    second = submit(service, "tie-2")
    third = submit(service, "tie-3")
    expected = [first["id"], second["id"], third["id"]]
    for _ in range(3):
        assert [item["id"] for item in service.preview_queue(ALL_CAPABILITIES)] == expected
    ids = [service.claim(f"worker-{i}", ALL_CAPABILITIES, 60)["id"] for i in range(3)]
    assert ids == expected


# ---------- 临时提权 ----------

def test_temporary_boost_requires_reason_has_ttl_and_audits(service):
    soon = submit(service, "boost-soon", start_in=120, urgency=2)
    later = submit(service, "boost-later", start_in=30, urgency=1)

    # later 开场更近，本来排第一
    assert service.preview_queue(ALL_CAPABILITIES)[0]["id"] == later["id"]

    with pytest.raises(Exception) as exc:
        service.boost(soon["id"], "dispatcher", "   ", 30, 1800)
    assert exc.value.code == "validation_error"

    result = service.boost(soon["id"], "dispatcher", "家属提前到场，需要优先布置", 30, 1800)
    assert result["override"]["bonus"] == 30
    assert result["override"]["expires_at"] > result["task"]["updated_at"]

    preview = service.preview_queue(ALL_CAPABILITIES)
    assert preview[0]["id"] == soon["id"]
    assert any("临时加权" in reason for reason in preview[0]["schedule"]["reasons"])

    details = service.get_task(soon["id"])
    assert details["interventions"][-1]["action"] == "priority_boost"
    assert details["interventions"][-1]["reason"] == "家属提前到场，需要优先布置"
    events = [event for event in details["schedule_events"] if event["action"] == "boost"]
    assert events and events[0]["breakdown_json"]["override"] == 30
    assert events[0]["metadata_json"]["reason"] == "家属提前到场，需要优先布置"

    # 到期后加权自动失效，恢复原始顺序
    service.clock.advance(minutes=31)
    assert service.preview_queue(ALL_CAPABILITIES)[0]["id"] == later["id"]


def test_boost_only_allowed_on_queued_and_bounded(service):
    task = submit(service, "boost-bound")
    service.claim("worker-x", ALL_CAPABILITIES, 60)
    with pytest.raises(Exception) as exc:
        service.boost(task["id"], "dispatcher", "运行中想提权", 10, 600)
    assert exc.value.code == "conflict"
    with pytest.raises(Exception):
        service.boost(task["id"] + 999, "dispatcher", "不存在的单", 10, 600)


# ---------- 取消 / 重新排队不产生重复领取 ----------

def test_cancel_requeue_and_no_double_claim(service):
    one = submit(service, "dup-one", start_in=30)
    two = submit(service, "dup-two", start_in=60)

    first_winner = service.claim("w1", ALL_CAPABILITIES, 60)
    assert first_winner["id"] == one["id"]
    # 立即再领只能领到下一张，绝不会重复拿到 one
    second_winner = service.claim("w2", ALL_CAPABILITIES, 60)
    assert second_winner["id"] == two["id"]
    assert second_winner["lease_owner"] != first_winner["lease_owner"]

    cancelled = service.cancel(two["id"], "dispatcher", "家属改期")
    assert cancelled["status"] == "cancel_requested"  # 运行中取消只登记请求，单不会回到队列
    assert service.claim("w3", ALL_CAPABILITIES, 60) is None

    # 失败可重试：退避结束后重新入队，只能被一个领取者拿到
    service.fail(one["id"], "w1", "transient", "设备临时故障", True)
    assert service.claim("w-too-early", ALL_CAPABILITIES, 60) is None  # 仍在退避窗口
    service.clock.advance(seconds=1)
    requeued = service.claim("w4", ALL_CAPABILITIES, 60)
    assert requeued["id"] == one["id"]
    assert service.claim("w5", ALL_CAPABILITIES, 60) is None
    events = [event["action"] for event in service.get_task(one["id"])["schedule_events"]]
    assert events.count("claim") == 2
    assert "requeue" in events


def test_requeue_resets_aging_baseline(service):
    task = submit(service, "reset-aging")
    service.clock.advance(minutes=150)
    assert service.preview_queue(ALL_CAPABILITIES)[0]["schedule"]["breakdown"]["aging"] == 25
    service.claim("w1", ALL_CAPABILITIES, 60)
    service.fail(task["id"], "w1", "transient", "临时故障", True)
    service.clock.advance(seconds=1)  # 越过退避窗口
    # 刚重新入队，老化补偿从零开始
    assert service.preview_queue(ALL_CAPABILITIES)[0]["schedule"]["breakdown"]["aging"] == 0


# ---------- 审计内容 ----------

def test_claim_writes_explainable_schedule_audit(service):
    submit(service, "audit-1", start_in=45, urgency=4)
    claimed = service.claim("audited-worker", ALL_CAPABILITIES, 60, {"wedding-host": 4})
    events = service.list_schedule_events(task_id=claimed["id"])
    event = events[0]
    assert event["action"] == "claim"
    assert event["worker_id"] == "audited-worker"
    assert event["score"] == claimed["schedule"]["score"]
    assert set(event["breakdown_json"]) >= {"start", "urgency", "wait_base", "aging", "priority", "skill", "override", "total"}
    assert any("临近开场" in reason for reason in event["metadata_json"]["reasons"])


# ---------- 进程重启后的队列一致性 ----------

def test_queue_order_consistent_across_restart(service, tmp_path, monkeypatch):
    a = submit(service, "restart-a", start_in=90)
    b = submit(service, "restart-b", start_in=30, urgency=2)
    c = submit(service, "restart-c", start_in=30, urgency=2)  # 与 b 同条件，靠 id 稳定排序
    service.boost(a["id"], "dispatcher", "设备需要提前装车", 25, 3600)

    before = [item["id"] for item in service.preview_queue(ALL_CAPABILITIES)]

    # 模拟进程重启：关闭线程连接，以同一数据库文件与同一冻结时钟重建服务
    close_connection()
    restarted = ComputeOperationsService(get_connection(), FrozenClock(START))
    after = [item["id"] for item in restarted.preview_queue(ALL_CAPABILITIES)]
    assert before == after
    assert len(before) == 3

    claimed = restarted.claim("post-restart-worker", ALL_CAPABILITIES, 60)
    assert claimed["id"] == after[0]
    # 重启前后各领一次不会拿到同一张单
    assert restarted.claim("another-worker", ALL_CAPABILITIES, 60)["id"] == after[1]
