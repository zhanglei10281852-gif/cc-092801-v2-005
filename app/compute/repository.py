from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from app.compute.scheduling import DEFAULT_WEIGHTS, SchedulerWeights, score_sql_expression, score_sql_params


class ComputeRepository:
    """封装计算任务运营领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection, weights: SchedulerWeights = DEFAULT_WEIGHTS) -> None:
        self.connection = connection
        self.weights = weights

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

    def create_task(
        self,
        *,
        template_id: int,
        project_code: str,
        requested_by: str,
        parameters: dict[str, Any],
        parameter_digest: str,
        priority: int,
        idempotency_key: str,
        max_attempts: int,
        now: str,
        event_starts_at: str = "",
        required_skill: str = "",
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_tasks(template_id,project_code,requested_by,parameters_json,parameter_digest,priority,idempotency_key,status,attempt_count,max_attempts,available_at,event_starts_at,required_skill,queue_rank_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',0,?,?,?,?,?,?,?)",
            (template_id, project_code, requested_by, json.dumps(parameters, ensure_ascii=False, sort_keys=True), parameter_digest, priority, idempotency_key, max_attempts, now, event_starts_at, required_skill, now, now, now),
        )
        return dict(self.task_by_id(cursor.lastrowid))

    def queued_candidate(self, capabilities: Iterable[str], now: str) -> sqlite3.Row | None:
        """按排班总分挑选一个当前可领取的订单。

        技能匹配规则：订单显式声明 required_skill 时工作者必须具备该技能；
        未声明时沿用模板 algorithm 匹配；工作者技能列表为空表示可承接任意订单。
        总分相同时按排队基准时间、任务 id 两级稳定排序。
        """
        capability_list = sorted(set(capabilities))
        score_expr = score_sql_expression(self.weights)
        sql = (
            "SELECT t.*,tpl.algorithm AS template_algorithm," + score_expr + " AS total_score,"
            " COALESCE(b.boost_points,0) AS boost_points"
            " FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id"
            " LEFT JOIN (SELECT task_id,SUM(points) AS boost_points FROM compute_boosts WHERE expires_at>? GROUP BY task_id) b ON b.task_id=t.id"
            " WHERE t.status='queued' AND t.available_at<=? AND " + self._skill_filter(capability_list)
            + " ORDER BY total_score DESC, t.queue_rank_at ASC, t.id ASC LIMIT 1"
        )
        # 占位符按 SQL 文本顺序：打分表达式（6×now + 技能加分）、boost 子查询的 now、
        # WHERE 中的 available_at now，最后是技能过滤参数。
        params: list[Any] = score_sql_params(now, self.weights.skill_match_bonus) + [now, now]
        if capability_list:
            params += [json.dumps(capability_list)] * 2
        row = self.connection.execute(sql, params).fetchone()
        return row

    @staticmethod
    def _skill_filter(capability_list: list[str]) -> str:
        if not capability_list:
            return "1=1"
        return (
            "((t.required_skill<>'' AND EXISTS (SELECT 1 FROM json_each(?) WHERE value=t.required_skill))"
            " OR (t.required_skill='' AND EXISTS (SELECT 1 FROM json_each(?) WHERE value=tpl.algorithm)))"
        )

    def result_versions(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_results WHERE task_id=? ORDER BY version", (task_id,)).fetchall()]

    def interventions(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_interventions WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]

    def add_intervention(self, *, task_id: int, actor: str, action: str, reason: str, before: dict[str, Any], after: dict[str, Any], batch_key: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO compute_interventions(task_id,actor,action,reason,before_json,after_json,batch_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, actor, action, reason, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), batch_key, now),
        )

    def add_boost(self, *, task_id: int, points: int, reason: str, expires_at: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_boosts(task_id,points,reason,expires_at,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (task_id, points, reason, expires_at, created_by, now),
        )
        return dict(self.connection.execute("SELECT * FROM compute_boosts WHERE id=?", (cursor.lastrowid,)).fetchone())

    def active_boosts(self, task_id: int, now: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM compute_boosts WHERE task_id=? AND expires_at>? ORDER BY id",
            (task_id, now),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_claim_event(self, *, task_id: int, worker_id: str, attempt_no: int, score_total: int, breakdown: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO compute_claim_events(task_id,worker_id,attempt_no,score_total,score_breakdown_json,created_at) VALUES(?,?,?,?,?,?)",
            (task_id, worker_id, attempt_no, score_total, json.dumps(breakdown, ensure_ascii=False, sort_keys=True), now),
        )

    def claim_events(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_claim_events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]

    def queued_preview(self, capabilities: Iterable[str], now: str, limit: int) -> list[sqlite3.Row]:
        """按与领取完全相同的规则返回排队候选（含总分），不落任何状态。"""
        capability_list = sorted(set(capabilities))
        score_expr = score_sql_expression(self.weights)
        sql = (
            "SELECT t.*,tpl.algorithm AS template_algorithm," + score_expr + " AS total_score,"
            " COALESCE(b.boost_points,0) AS boost_points"
            " FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id"
            " LEFT JOIN (SELECT task_id,SUM(points) AS boost_points FROM compute_boosts WHERE expires_at>? GROUP BY task_id) b ON b.task_id=t.id"
            " WHERE t.status='queued' AND t.available_at<=? AND " + self._skill_filter(capability_list)
            + " ORDER BY total_score DESC, t.queue_rank_at ASC, t.id ASC LIMIT ?"
        )
        params: list[Any] = score_sql_params(now, self.weights.skill_match_bonus) + [now, now]
        if capability_list:
            params += [json.dumps(capability_list)] * 2
        params.append(limit)
        return list(self.connection.execute(sql, params).fetchall())

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
