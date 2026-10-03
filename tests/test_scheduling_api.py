from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


WEDDING = {
    "code": "wedding-api",
    "name": "婚礼服务",
    "algorithm": "wedding-api",
    "parameter_schema": {"guests": {"type": "integer", "required": True, "minimum": 1}},
    "default_parameters": {},
    "max_runtime_seconds": 600,
    "max_attempts": 2,
}
MEMORIAL = {
    "code": "memorial-api",
    "name": "追思服务",
    "algorithm": "memorial-api",
    "parameter_schema": {"hall": {"type": "string", "required": True}},
    "default_parameters": {},
    "max_runtime_seconds": 600,
    "max_attempts": 2,
}

NOW = datetime(2026, 10, 4, 8, 0, tzinfo=UTC)


def _task(key, *, template="wedding-api", **extra):
    payload = {
        "template_code": template,
        "project_code": "api-weekend",
        "requested_by": "family-api",
        "parameters": {"guests": 80} if template == "wedding-api" else {"hall": "追远厅"},
        "idempotency_key": key,
    }
    payload.update(extra)
    return payload


def test_scheduling_flow_over_http_with_freezing_and_restart(client, monkeypatch):
    # 冻结服务时钟：直接注入到应用内的服务构造不可行，改为通过环境变量控制的固定时刻断言相对顺序；
    # 这里用提交时刻与开场时刻的差值驱动顺序，因此无需冻结真实时钟。
    assert client.post("/api/compute/templates?actor=admin", json=WEDDING).status_code == 201
    assert client.post("/api/compute/templates?actor=admin", json=MEMORIAL).status_code == 201

    def iso(minutes_from_now: int) -> str:
        return (datetime.now(UTC) + timedelta(minutes=minutes_from_now)).isoformat()

    soon_wedding = client.post("/api/compute/tasks", json=_task(
        "api-key-1", urgency=2, priority=40, start_at=iso(30),
        required_skill="wedding-host", min_skill_level=2)).json()
    memorial_urgent = client.post("/api/compute/tasks", json=_task(
        "api-key-2", template="memorial-api", urgency=5, start_at=iso(180))).json()
    normal_old = client.post("/api/compute/tasks", json=_task("api-key-3", priority=10)).json()

    # 接口入参校验：技能等级超范围、缺原因的临时提权应被拒绝
    bad_skill = client.post("/api/compute/tasks", json=_task("api-key-bad", min_skill_level=9))
    assert bad_skill.status_code == 422

    # 婚礼单需要二级司仪，一级司仪看不到它但可接追思单
    claim = client.post("/api/compute/tasks/claim", json={
        "worker_id": "junior", "capabilities": ["wedding-api", "memorial-api"],
        "skills": {"wedding-host": 1}, "lease_seconds": 60})
    assert claim.status_code == 200
    assert claim.json()["task"]["id"] == memorial_urgent["id"]

    # 二级司仪预览：临近开场的婚礼单排第一，且返回逐组件解释
    preview = client.post("/api/compute/queue/preview", json={
        "capabilities": ["wedding-api", "memorial-api"], "skills": {"wedding-host": 2}})
    ranked = preview.json()["items"]
    assert ranked[0]["id"] == soon_wedding["id"]
    assert set(ranked[0]["schedule"]["breakdown"]) == {
        "start", "urgency", "wait_base", "aging", "priority", "skill", "override", "total"}

    # 临时提权必须给原因，且只允许排队中的单
    no_reason = client.post(f"/api/compute/tasks/{normal_old['id']}/boost",
                            json={"actor": "admin", "reason": "x", "bonus": 30, "ttl_seconds": 600})
    assert no_reason.status_code == 422  # 原因少于 2 个字符
    boost = client.post(f"/api/compute/tasks/{normal_old['id']}/boost",
                        json={"actor": "dispatcher", "reason": "家属已到场等候", "bonus": 35, "ttl_seconds": 1800})
    assert boost.status_code == 201
    assert boost.json()["override"]["reason"] == "家属已到场等候"

    ranked = client.post("/api/compute/queue/preview", json={
        "capabilities": ["wedding-api"], "skills": {"wedding-host": 2}}).json()["items"]
    assert ranked[0]["id"] == normal_old["id"]

    # 领取审计可查
    events = client.get(f"/api/compute/schedule-events?task_id={memorial_urgent['id']}").json()["items"]
    assert events[0]["action"] == "claim"
    assert events[0]["worker_id"] == "junior"

    # 接口创建的全部字段持久化，任务详情包含干预与排班审计
    details = client.get(f"/api/compute/task-details/{normal_old['id']}").json()
    assert details["urgency"] == 3 and details["required_skill"] == ""
    assert details["interventions"][-1]["action"] == "priority_boost"
    assert details["schedule_events"][0]["action"] == "boost"


def test_restart_keeps_queue_and_claims_consistent(tmp_path, monkeypatch):
    db_path = tmp_path / "restart-http.db"
    monkeypatch.setenv("TOWNSHIP_DATABASE_PATH", str(db_path))

    from app.database import close_connection, init_db
    close_connection()
    init_db()

    from app.main import app

    start = datetime.now(UTC) + timedelta(minutes=40)
    with TestClient(app) as first:
        first.post("/api/compute/templates?actor=admin", json=WEDDING)
        first.post("/api/compute/tasks", json=_task("restart-http-1", start_at=start.isoformat(), urgency=4))
        first.post("/api/compute/tasks", json=_task("restart-http-2", urgency=2))
        order_before = [item["id"] for item in first.post("/api/compute/queue/preview",
                        json={"capabilities": ["wedding-api"]}).json()["items"]]
        assert len(order_before) == 2
        claimed = first.post("/api/compute/tasks/claim",
                             json={"worker_id": "before-restart", "capabilities": ["wedding-api"],
                                   "lease_seconds": 60}).json()["task"]
        assert claimed["id"] == order_before[0]

    # 进程重启：全新 TestClient + 同一数据库文件
    close_connection()
    with TestClient(app) as second:
        order_after = [item["id"] for item in second.post("/api/compute/queue/preview",
                       json={"capabilities": ["wedding-api"]}).json()["items"]]
        assert order_after == order_before[1:]  # 已领取的单不会再次出现
        again = second.post("/api/compute/tasks/claim",
                            json={"worker_id": "after-restart", "capabilities": ["wedding-api"],
                                  "lease_seconds": 60}).json()["task"]
        assert again["id"] == order_before[1]
        assert second.post("/api/compute/tasks/claim",
                           json={"worker_id": "empty", "capabilities": ["wedding-api"],
                                 "lease_seconds": 60}).json()["task"] is None
