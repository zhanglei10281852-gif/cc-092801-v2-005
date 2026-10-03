from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.compute import scheduling
from app.compute.repository import ComputeRepository
from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class ComputeOperationsService:
    """管理计算模板、配额、任务租约、结果版本和人工干预。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = ComputeRepository(self.connection)

    def list_templates(self) -> list[dict[str, Any]]:
        return self.repository.active_templates()

    def create_template(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        self._validate_schema(payload["parameter_schema"], payload["default_parameters"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            if repository.template_by_code(payload["code"]):
                raise ConflictError("参数模板编码已存在")
            return repository.create_template(
                code=payload["code"], name=payload["name"], algorithm=payload["algorithm"],
                parameter_schema=payload["parameter_schema"], defaults=payload["default_parameters"],
                max_runtime_seconds=payload["max_runtime_seconds"], max_attempts=payload["max_attempts"],
                created_by=actor, now=now,
            )

    def set_quota(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            return ComputeRepository(connection).upsert_quota(actor=actor, now=now, **payload)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template = repository.template_by_code(payload["template_code"])
            if template is None or not template["active"]:
                raise NotFoundError("参数模板不存在或已经停用")
            parameters = self._validate_parameters(template, payload["parameters"])
            existing = repository.task_by_idempotency(payload["requested_by"], payload["idempotency_key"])
            parameter_digest = digest(parameters)
            if existing is not None:
                if existing["parameter_digest"] != parameter_digest:
                    raise ConflictError("同一幂等键对应了不同的计算参数")
                return dict(repository.task_by_id(existing["id"]))
            self._check_quota(repository, payload["requested_by"], now_value)
            start_at_text = self._normalize_start_at(payload.get("start_at"))
            required_skill = (payload.get("required_skill") or "").strip()
            min_skill_level = int(payload.get("min_skill_level", 1))
            return repository.create_task(
                template_id=template["id"], project_code=payload["project_code"],
                requested_by=payload["requested_by"], parameters=parameters,
                parameter_digest=parameter_digest, priority=payload["priority"],
                idempotency_key=payload["idempotency_key"], max_attempts=template["max_attempts"],
                urgency=int(payload.get("urgency", 3)), start_at=start_at_text,
                required_skill=required_skill, min_skill_level=min_skill_level, now=now,
            )

    def list_tasks(self, *, status: str | None = None, project_code: str | None = None, requested_by: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_tasks(status=status, project_code=project_code, requested_by=requested_by, limit=max(1, min(limit, 500)))

    def get_task(self, task_id: int) -> dict[str, Any]:
        row = self.repository.task_by_id(task_id)
        if row is None:
            raise NotFoundError("计算任务不存在")
        result = dict(row)
        result["results"] = self.repository.result_versions(task_id)
        result["interventions"] = self.repository.interventions(task_id)
        result["schedule_events"] = self.repository.list_schedule_events(task_id=task_id, limit=50)
        return result

    def claim(self, worker_id: str, capabilities: list[str], lease_seconds: int, skills: dict[str, int] | None = None) -> dict[str, Any] | None:
        now_value = self.clock.now()
        now = to_storage(now_value)
        lease_until = to_storage(now_value + timedelta(seconds=lease_seconds))
        skill_levels = self._normalize_skills(skills)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            repository.expire_overrides(now)
            rows = repository.eligible_candidates(capabilities, skill_levels, now)
            chosen: sqlite3.Row | None = None
            chosen_components: dict[str, float] | None = None
            chosen_key: tuple[Any, ...] | None = None
            for row in rows:
                candidate_skill_level = self._candidate_skill_level(row, skill_levels)
                if candidate_skill_level is None:
                    continue  # 技能等级未达到该单门槛
                components = self._score_row(row, candidate_skill_level, now_value)
                key = scheduling.ranking_key(dict(row), components)
                if chosen_key is None or key < chosen_key:
                    chosen, chosen_components, chosen_key = row, components, key
            if chosen is None:
                return None
            cursor = connection.execute(
                "UPDATE compute_tasks SET status='running',attempt_count=attempt_count+1,lease_owner=?,lease_expires_at=?,started_at=COALESCE(started_at,?),schedule_score=?,updated_at=?,version=version+1 WHERE id=? AND status='queued'",
                (worker_id, lease_until, now, chosen_components["total"], now, chosen["id"]),
            )
            if cursor.rowcount != 1:
                # 并发下被其他领取者抢先：条件更新失败，不产生重复领取。
                return None
            task = dict(repository.task_by_id(chosen["id"]))
            explanation = scheduling.explain(
                chosen_components,
                aging_step_count=scheduling.aging_steps(self._wait_minutes(chosen, now_value)),
                has_override=chosen["override_bonus"] is not None,
            )
            task["schedule"] = explanation
            repository.add_schedule_event(
                task_id=chosen["id"], worker_id=worker_id, action="claim",
                score=chosen_components["total"], breakdown=chosen_components,
                metadata={"worker_capabilities": sorted(set(capabilities)), "worker_skills": skill_levels,
                          "override_reason": chosen["override_reason"], "reasons": explanation["reasons"]},
                now=now,
            )
            return task

    def heartbeat(self, task_id: int, worker_id: str, lease_seconds: int) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        expires = to_storage(now_value + timedelta(seconds=lease_seconds))
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE compute_tasks SET lease_expires_at=?,updated_at=?,version=version+1 WHERE id=? AND status='running' AND lease_owner=?",
                (expires, now, task_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("任务未由当前工作者持有")
            return dict(ComputeRepository(connection).task_by_id(task_id))

    def complete(self, task_id: int, worker_id: str, result: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            if task["status"] != "running" or task["lease_owner"] != worker_id:
                raise ConflictError("任务未由当前工作者持有")
            version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM compute_results WHERE task_id=?", (task_id,)).fetchone()[0])
            connection.execute(
                "INSERT INTO compute_results(task_id,version,result_json,metrics_json,result_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, version, json.dumps(result, ensure_ascii=False, sort_keys=True), json.dumps(metrics, ensure_ascii=False, sort_keys=True), digest({"result": result, "metrics": metrics}), worker_id, now),
            )
            connection.execute(
                "UPDATE compute_tasks SET status='succeeded',current_result_version=?,lease_owner='',lease_expires_at='',finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                (version, now, now, task_id),
            )
            return dict(repository.task_by_id(task_id))

    def fail(self, task_id: int, worker_id: str, error_code: str, message: str, retryable: bool) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            if task["status"] != "running" or task["lease_owner"] != worker_id:
                raise ConflictError("任务未由当前工作者持有")
            can_retry = retryable and int(task["attempt_count"]) < int(task["max_attempts"])
            status = "queued" if can_retry else "failed"
            delay = min(300, 2 ** max(0, int(task["attempt_count"]) - 1)) if can_retry else 0
            available = to_storage(now_value + timedelta(seconds=delay))
            connection.execute(
                "UPDATE compute_tasks SET status=?,available_at=?,queued_since=?,lease_owner='',lease_expires_at='',last_error_code=?,last_error_message=?,finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                (status, available, available if can_retry else task["queued_since"], error_code, message[:2000], None if can_retry else now, now, task_id),
            )
            ComputeRepository(connection).deactivate_overrides(task_id)
            if can_retry:
                ComputeRepository(connection).add_schedule_event(
                    task_id=task_id, worker_id=worker_id, action="requeue",
                    score=0.0, breakdown={}, metadata={"cause": "failure_retry", "delay_seconds": delay,
                                                        "error_code": error_code}, now=now,
                )
            return dict(repository.task_by_id(task_id))

    def cancel(self, task_id: int, actor: str, reason: str, batch_key: str = "") -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> None:
            if task["status"] not in {"queued", "running"}:
                raise ConflictError("当前任务状态不允许取消")
            status = "cancel_requested" if task["status"] == "running" else "cancelled"
            connection.execute("UPDATE compute_tasks SET status=?,finished_at=?,updated_at=?,version=version+1 WHERE id=?", (status, None if status == "cancel_requested" else now, now, task["id"]))
            ComputeRepository(connection).deactivate_overrides(task_id)
        return self._intervene(task_id, actor, reason, "cancel", batch_key, mutate)

    def boost(self, task_id: int, actor: str, reason: str, bonus: float, ttl_seconds: int) -> dict[str, Any]:
        """临时提高排队分值：必须给出原因，且设有有效期，到期自动失效。"""
        if not reason.strip():
            raise ValidationError("临时提权必须填写原因")
        if not 0 < bonus <= scheduling.OVERRIDE_BONUS_MAX:
            raise ValidationError(f"临时加权分值必须在 0 与 {scheduling.OVERRIDE_BONUS_MAX:g} 之间")
        if not scheduling.OVERRIDE_TTL_MIN_SECONDS <= ttl_seconds <= scheduling.OVERRIDE_TTL_MAX_SECONDS:
            raise ValidationError("临时加权有效期超出允许范围")
        now_value = self.clock.now()
        now = to_storage(now_value)
        expires = to_storage(now_value + timedelta(seconds=ttl_seconds))
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            if task["status"] != "queued":
                raise ConflictError("只有排队中的服务单可以临时提权")
            repository.deactivate_overrides(task_id)
            override = repository.add_override(task_id=task_id, bonus=float(bonus), reason=reason, actor=actor, expires_at=expires, now=now)
            after = dict(repository.task_by_id(task_id))
            repository.add_intervention(
                task_id=task_id, actor=actor, action="priority_boost",
                reason=reason, before=dict(task), after=after, batch_key="", now=now,
            )
            repository.add_schedule_event(
                task_id=task_id, worker_id="", action="boost", score=0.0,
                breakdown={"override": float(bonus)},
                metadata={"actor": actor, "reason": reason, "ttl_seconds": ttl_seconds,
                          "expires_at": expires, "override_id": override["id"]},
                now=now,
            )
            return {"task": after, "override": override}

    def preview_queue(self, capabilities: list[str], skills: dict[str, int] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """按当前时钟模拟领取顺序，不改变任何状态，用于解释排班结果。"""
        now_value = self.clock.now()
        now = to_storage(now_value)
        skill_levels = self._normalize_skills(skills)
        repository = self.repository
        rows = repository.eligible_candidates(capabilities, skill_levels, now)
        ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        for row in rows:
            candidate_skill_level = self._candidate_skill_level(row, skill_levels)
            if candidate_skill_level is None:
                continue
            components = self._score_row(row, candidate_skill_level, now_value)
            item = dict(row)
            item["schedule"] = scheduling.explain(
                components,
                aging_step_count=scheduling.aging_steps(self._wait_minutes(row, now_value)),
                has_override=row["override_bonus"] is not None,
            )
            ranked.append((scheduling.ranking_key(dict(row), components), item))
        ranked.sort(key=lambda pair: pair[0])
        return [item for _, item in ranked[: max(1, min(limit, 100))]]

    def list_schedule_events(self, task_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_schedule_events(task_id=task_id, limit=max(1, min(limit, 500)))

    def retry(self, task_id: int, actor: str, reason: str, priority: int | None = None, batch_key: str = "") -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> None:
            if task["status"] not in {"failed", "cancelled"}:
                raise ConflictError("只有失败或已取消任务可以人工重试")
            chosen = task["priority"] if priority is None else priority
            connection.execute("UPDATE compute_tasks SET status='queued',priority=?,available_at=?,queued_since=?,lease_owner='',lease_expires_at='',finished_at=NULL,schedule_score=0,updated_at=?,version=version+1 WHERE id=?", (chosen, now, now, now, task["id"]))
            repository = ComputeRepository(connection)
            repository.deactivate_overrides(task_id)
            repository.add_schedule_event(
                task_id=task_id, worker_id="", action="requeue", score=0.0, breakdown={},
                metadata={"cause": "manual_retry", "actor": actor, "reason": reason}, now=now,
            )
        return self._intervene(task_id, actor, reason, "retry", batch_key, mutate)

    def set_priority(self, task_id: int, actor: str, reason: str, priority: int, batch_key: str = "") -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> None:
            if task["status"] not in {"queued", "running"}:
                raise ConflictError("只有排队或运行中的任务可以调整优先级")
            connection.execute("UPDATE compute_tasks SET priority=?,updated_at=?,version=version+1 WHERE id=?", (priority, now, task["id"]))
            repository = ComputeRepository(connection)
            repository.add_schedule_event(
                task_id=task_id, worker_id="", action="priority_label", score=0.0,
                breakdown={"priority_old": int(task["priority"]), "priority_new": int(priority)},
                metadata={"actor": actor, "reason": reason}, now=now,
            )
        return self._intervene(task_id, actor, reason, "priority", batch_key, mutate)

    def batch_operation(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch_key = digest({"actor": payload["actor"], "task_ids": payload["task_ids"], "operation": payload["operation"], "reason": payload["reason"]})
        succeeded: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for task_id in list(dict.fromkeys(payload["task_ids"])):
            try:
                if payload["operation"] == "cancel":
                    value = self.cancel(task_id, payload["actor"], payload["reason"], batch_key)
                elif payload["operation"] == "retry":
                    value = self.retry(task_id, payload["actor"], payload["reason"], payload.get("priority"), batch_key)
                else:
                    value = self.set_priority(task_id, payload["actor"], payload["reason"], int(payload["priority"]), batch_key)
                succeeded.append({"task_id": task_id, "status": value["status"], "version": value["version"]})
            except (ConflictError, NotFoundError) as exc:
                failed.append({"task_id": task_id, "code": exc.code, "message": exc.message})
        return {"batch_key": batch_key, "succeeded": succeeded, "failed": failed}

    def recover_expired(self, actor: str = "recovery-worker") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        recovered: list[int] = []
        exhausted: list[int] = []
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            rows = connection.execute("SELECT * FROM compute_tasks WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<? ORDER BY id", (now,)).fetchall()
            for task in rows:
                before = dict(task)
                if int(task["attempt_count"]) < int(task["max_attempts"]):
                    status, finished_at = "queued", None
                    recovered.append(int(task["id"]))
                else:
                    status, finished_at = "failed", now
                    exhausted.append(int(task["id"]))
                connection.execute(
                    "UPDATE compute_tasks SET status=?,lease_owner='',lease_expires_at='',available_at=?,queued_since=?,last_error_code='lease_expired',last_error_message='工作者租约已过期',finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                    (status, now, now, finished_at, now, task["id"]),
                )
                if status == "queued":
                    repository.deactivate_overrides(task["id"])
                after = dict(repository.task_by_id(task["id"]))
                repository.add_intervention(task_id=task["id"], actor=actor, action="lease_recovery", reason="租约过期自动恢复", before=before, after=after, batch_key="", now=now)
                repository.add_schedule_event(
                    task_id=task["id"], worker_id="", action="requeue", score=0.0, breakdown={},
                    metadata={"cause": "lease_expired", "actor": actor, "back_to_queue": status == "queued"}, now=now,
                )
        return {"recovered": recovered, "exhausted": exhausted}

    def summary(self) -> dict[str, Any]:
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM compute_tasks GROUP BY status ORDER BY status").fetchall()
        oldest = self.connection.execute("SELECT MIN(created_at) FROM compute_tasks WHERE status='queued'").fetchone()[0]
        return {"states": {row["status"]: row["amount"] for row in rows}, "oldest_queued_at": oldest, "templates": len(self.repository.active_templates())}

    def _intervene(self, task_id: int, actor: str, reason: str, action: str, batch_key: str, mutation: Callable[[sqlite3.Connection, sqlite3.Row, str], None]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            before = dict(task)
            mutation(connection, task, now)
            after = dict(repository.task_by_id(task_id))
            repository.add_intervention(task_id=task_id, actor=actor, action=action, reason=reason, before=before, after=after, batch_key=batch_key, now=now)
            return after

    def _check_quota(self, repository: ComputeRepository, requested_by: str, now: datetime) -> None:
        quota = repository.quota("user", requested_by)
        if quota is None:
            return
        states = repository.count_user_states(requested_by)
        if states.get("queued", 0) >= int(quota["max_queued"]):
            raise ConflictError("用户排队任务配额已用尽")
        if states.get("running", 0) >= int(quota["max_running"]):
            raise ConflictError("用户运行任务配额已用尽")
        day_start = to_storage(now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0))
        if repository.count_user_submissions_since(requested_by, day_start) >= int(quota["daily_submissions"]):
            raise ConflictError("用户当日提交配额已用尽")

    def _normalize_start_at(self, value: Any) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, datetime):
            moment = value if value.tzinfo else value.replace(tzinfo=UTC)
            return to_storage(moment)
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValidationError("场次开始时间格式不正确") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return to_storage(parsed)

    @staticmethod
    def _normalize_skills(skills: dict[str, int] | None) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for name, level in (skills or {}).items():
            name = str(name).strip()
            if not name:
                continue
            level = int(level)
            if not 1 <= level <= 5:
                raise ValidationError(f"技能 {name} 的等级必须在 1 到 5 之间")
            normalized[name] = level
        return normalized

    @staticmethod
    def _candidate_skill_level(row: sqlite3.Row, skills: dict[str, int]) -> int | None:
        """返回领取者对该单所需技能的等级；达不到门槛返回 None。"""
        required = row["required_skill"]
        if not required:
            return 1
        level = skills.get(required)
        if level is None or level < int(row["min_skill_level"]):
            return None
        return level

    @staticmethod
    def _wait_minutes(row: sqlite3.Row, now_value: datetime) -> float:
        queued = from_storage(row["queued_since"] or row["created_at"])
        if queued is None:
            return 0.0
        return max(0.0, (now_value - queued).total_seconds() / 60.0)

    def _score_row(self, row: sqlite3.Row, skill_level: int, now_value: datetime) -> dict[str, float]:
        start_at = from_storage(row["start_at"]) if row["start_at"] else None
        minutes_to_start = (start_at - now_value).total_seconds() / 60.0 if start_at else 0.0
        return scheduling.score_components(
            has_start=start_at is not None,
            minutes_to_start=minutes_to_start,
            urgency=int(row["urgency"]),
            wait_minutes=self._wait_minutes(row, now_value),
            priority=int(row["priority"]),
            skill_level=skill_level,
            override_bonus=float(row["override_bonus"] or 0.0),
        )

    @staticmethod
    def _validate_schema(schema: dict[str, dict[str, Any]], defaults: dict[str, Any]) -> None:
        if not schema:
            raise ValidationError("参数模板至少包含一个参数")
        allowed = {"integer", "number", "string", "boolean"}
        for name, rule in schema.items():
            if not name or not isinstance(rule, dict) or rule.get("type") not in allowed:
                raise ValidationError(f"参数 {name or '<empty>'} 的规则不合法")
        if set(defaults) - set(schema):
            raise ValidationError("默认值包含未声明参数")

    def _validate_parameters(self, template: sqlite3.Row, supplied: dict[str, Any]) -> dict[str, Any]:
        schema = json.loads(template["parameter_schema_json"])
        values = {**json.loads(template["default_parameters_json"]), **supplied}
        unknown = set(values) - set(schema)
        if unknown:
            raise ValidationError("包含模板未声明的参数", context={"parameters": sorted(unknown)})
        normalized: dict[str, Any] = {}
        for name, rule in schema.items():
            if name not in values:
                if rule.get("required"):
                    raise ValidationError(f"缺少必填参数：{name}")
                continue
            value = values[name]
            kind = rule["type"]
            valid = {"integer": isinstance(value, int) and not isinstance(value, bool), "number": isinstance(value, (int, float)) and not isinstance(value, bool), "string": isinstance(value, str), "boolean": isinstance(value, bool)}[kind]
            if not valid:
                raise ValidationError(f"参数 {name} 类型不正确")
            if rule.get("minimum") is not None and value < rule["minimum"]:
                raise ValidationError(f"参数 {name} 小于允许的最小值")
            if rule.get("maximum") is not None and value > rule["maximum"]:
                raise ValidationError(f"参数 {name} 大于允许的最大值")
            if rule.get("choices") and value not in rule["choices"]:
                raise ValidationError(f"参数 {name} 不在允许的选项中")
            normalized[name] = value
        return normalized
