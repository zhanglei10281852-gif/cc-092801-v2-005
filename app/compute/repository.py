from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


class ComputeRepository:
    """封装计算任务运营领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def template_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_templates WHERE code=?", (code,)).fetchone()

    def template_by_id(self, template_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_templates WHERE id=?", (template_id,)).fetchone()

    def active_templates(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM compute_templates WHERE active=1 ORDER BY code,version").fetchall()
        return [dict(row) for row in rows]

    def create_template(self, *, code: str, name: str, algorithm: str, parameter_schema: dict[str, Any], defaults: dict[str, Any], max_runtime_seconds: int, max_attempts: int, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_templates(code,name,algorithm,version,parameter_schema_json,default_parameters_json,max_runtime_seconds,max_attempts,active,created_by,created_at,updated_at) VALUES(?,?,?,1,?,?,?,?,1,?,?,?)",
            (code, name, algorithm, json.dumps(parameter_schema, ensure_ascii=False, sort_keys=True), json.dumps(defaults, ensure_ascii=False, sort_keys=True), max_runtime_seconds, max_attempts, created_by, now, now),
        )
        return dict(self.template_by_id(cursor.lastrowid))

    def quota(self, subject_type: str, subject_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_quotas WHERE subject_type=? AND subject_key=?", (subject_type, subject_key)).fetchone()

    def upsert_quota(self, *, subject_type: str, subject_key: str, max_queued: int, max_running: int, daily_submissions: int, actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO compute_quotas(subject_type,subject_key,max_queued,max_running,daily_submissions,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(subject_type,subject_key) DO UPDATE SET max_queued=excluded.max_queued,max_running=excluded.max_running,daily_submissions=excluded.daily_submissions,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (subject_type, subject_key, max_queued, max_running, daily_submissions, actor, now, now),
        )
        return dict(self.quota(subject_type, subject_key))

    def count_user_states(self, requested_by: str) -> dict[str, int]:
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM compute_tasks WHERE requested_by=? GROUP BY status", (requested_by,)).fetchall()
        return {str(row["status"]): int(row["amount"]) for row in rows}

    def count_user_submissions_since(self, requested_by: str, since: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM compute_tasks WHERE requested_by=? AND created_at>=?", (requested_by, since)).fetchone()[0])

    def task_by_id(self, task_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT t.*,tpl.code AS template_code,tpl.algorithm AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id WHERE t.id=?", (task_id,)).fetchone()

    def task_by_idempotency(self, requested_by: str, key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_tasks WHERE requested_by=? AND idempotency_key=?", (requested_by, key)).fetchone()

    def create_task(self, *, template_id: int, project_code: str, requested_by: str, parameters: dict[str, Any], parameter_digest: str, priority: int, idempotency_key: str, max_attempts: int, urgency: int, start_at: str, required_skill: str, min_skill_level: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_tasks(template_id,project_code,requested_by,parameters_json,parameter_digest,priority,idempotency_key,status,attempt_count,max_attempts,available_at,urgency,start_at,required_skill,min_skill_level,queued_since,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',0,?,?,?,?,?,?,?,?,?)",
            (template_id, project_code, requested_by, json.dumps(parameters, ensure_ascii=False, sort_keys=True), parameter_digest, priority, idempotency_key, max_attempts, now, urgency, start_at, required_skill, min_skill_level, now, now, now),
        )
        return dict(self.task_by_id(cursor.lastrowid))

    def eligible_candidates(self, capabilities: Iterable[str], skills: dict[str, int], now: str) -> list[sqlite3.Row]:
        """取出所有已到领取时间且领取者资格达标的排队单（含临时加权）。

        评分与排序全部在服务层完成，保证解释与实际顺序一致；现场调度
        规模为单容器离线队列，全量取数可接受，避免按 id 截断导致高分单
        永远落在窗口之外。

        - 旧任务（``required_skill=''``）沿用模板 algorithm 能力匹配；
        - 新任务按 ``required_skill`` 是否在领取者技能集合内过滤，
          具体技能等级是否达标在服务层用 skills 映射判定。
        """
        clauses: list[str] = []
        values: list[Any] = [now]
        capability_list = sorted(set(capabilities))
        if capability_list:
            placeholders = ",".join("?" for _ in capability_list)
            clauses.append(f"(t.required_skill='' AND tpl.algorithm IN ({placeholders}))")
            values.extend(capability_list)
        skill_names = sorted(skills)
        if skill_names:
            placeholders = ",".join("?" for _ in skill_names)
            clauses.append(f"(t.required_skill<>'' AND t.required_skill IN ({placeholders}))")
            values.extend(skill_names)
        if not clauses:
            # 与历史行为保持一致：既不声明能力也不声明技能时，可领取任意无技能要求的旧单。
            clauses.append("t.required_skill=''")
        sql = (
            "SELECT t.*,tpl.algorithm AS template_algorithm,o.id AS override_id,o.bonus AS override_bonus,"
            "o.reason AS override_reason,o.expires_at AS override_expires_at "
            "FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id "
            "LEFT JOIN compute_priority_overrides o ON o.task_id=t.id AND o.active=1 AND o.expires_at>? "
            "WHERE t.status='queued' AND t.available_at<=? AND (" + " OR ".join(clauses) + ") "
            "ORDER BY t.id"
        )
        return list(self.connection.execute(sql, [now, *values]).fetchall())

    def active_override(self, task_id: int, now: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_priority_overrides WHERE task_id=? AND active=1 AND expires_at>? ORDER BY id DESC LIMIT 1",
            (task_id, now),
        ).fetchone()

    def deactivate_overrides(self, task_id: int) -> int:
        cursor = self.connection.execute("UPDATE compute_priority_overrides SET active=0 WHERE task_id=? AND active=1", (task_id,))
        return cursor.rowcount

    def add_override(self, *, task_id: int, bonus: float, reason: str, actor: str, expires_at: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_priority_overrides(task_id,bonus,reason,actor,active,expires_at,created_at) VALUES(?,?,?,?,'1',?,?)",
            (task_id, bonus, reason, actor, expires_at, now),
        )
        return dict(self.connection.execute("SELECT * FROM compute_priority_overrides WHERE id=?", (cursor.lastrowid,)).fetchone())

    def expire_overrides(self, now: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM compute_priority_overrides WHERE active=1 AND expires_at<=? ORDER BY id", (now,)
        ).fetchall()
        expired = [dict(row) for row in rows]
        if expired:
            self.connection.execute("UPDATE compute_priority_overrides SET active=0 WHERE active=1 AND expires_at<=?", (now,))
        return expired

    def add_schedule_event(self, *, task_id: int | None, worker_id: str, action: str, score: float, breakdown: dict[str, Any], metadata: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO compute_schedule_events(task_id,worker_id,action,score,breakdown_json,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (task_id, worker_id, action, score, json.dumps(breakdown, ensure_ascii=False, sort_keys=True), json.dumps(metadata, ensure_ascii=False, sort_keys=True), now),
        )

    def list_schedule_events(self, *, task_id: int | None = None, limit: int) -> list[dict[str, Any]]:
        if task_id is not None:
            rows = self.connection.execute("SELECT * FROM compute_schedule_events WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM compute_schedule_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["breakdown_json"] = json.loads(item["breakdown_json"])
            item["metadata_json"] = json.loads(item["metadata_json"])
            items.append(item)
        return items

    def result_versions(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_results WHERE task_id=? ORDER BY version", (task_id,)).fetchall()]

    def interventions(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_interventions WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]

    def add_intervention(self, *, task_id: int, actor: str, action: str, reason: str, before: dict[str, Any], after: dict[str, Any], batch_key: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO compute_interventions(task_id,actor,action,reason,before_json,after_json,batch_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, actor, action, reason, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), batch_key, now),
        )

    def list_tasks(self, *, status: str | None, project_code: str | None, requested_by: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("t.status=?")
            values.append(status)
        if project_code:
            clauses.append("t.project_code=?")
            values.append(project_code)
        if requested_by:
            clauses.append("t.requested_by=?")
            values.append(requested_by)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT t.*,tpl.code AS template_code,tpl.algorithm AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id" + where + " ORDER BY t.priority DESC,t.created_at DESC,t.id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]
