from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TemplateCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    algorithm: str = Field(min_length=2, max_length=120)
    parameter_schema: dict[str, dict[str, Any]]
    default_parameters: dict[str, Any] = Field(default_factory=dict)
    max_runtime_seconds: int = Field(default=600, ge=1, le=86400)
    max_attempts: int = Field(default=3, ge=1, le=20)


class QuotaSet(BaseModel):
    subject_type: Literal["user", "role", "project"]
    subject_key: str = Field(min_length=1, max_length=120)
    max_queued: int = Field(default=20, ge=0, le=100000)
    max_running: int = Field(default=4, ge=0, le=10000)
    daily_submissions: int = Field(default=200, ge=0, le=1000000)


class TaskSubmit(BaseModel):
    template_code: str = Field(min_length=2, max_length=64)
    project_code: str = Field(min_length=1, max_length=80)
    requested_by: str = Field(min_length=1, max_length=80)
    parameters: dict[str, Any]
    priority: int = Field(default=50, ge=0, le=100)
    idempotency_key: str = Field(min_length=6, max_length=160)
    urgency: int = Field(default=3, ge=1, le=5, description="服务紧急度：1 普通 ~ 5 特急")
    start_at: str | None = Field(default=None, description="场次开始时间，ISO 8601；缺省表示不按开场时间加权")
    required_skill: str = Field(default="", max_length=80, description="该服务单要求的技能编码，空串表示无技能要求")
    min_skill_level: int = Field(default=1, ge=1, le=5, description="所需技能最低等级")

    @model_validator(mode="after")
    def validate_skill_level(self) -> "TaskSubmit":
        if self.required_skill.strip() == "" and self.min_skill_level != 1:
            raise ValueError("未指定所需技能时不能设置技能等级要求")
        return self


class TaskClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    skills: dict[str, int] = Field(default_factory=dict, description="领取者技能等级映射，等级 1~5")
    lease_seconds: int = Field(default=60, ge=5, le=3600)

    @model_validator(mode="after")
    def validate_skill_levels(self) -> "TaskClaim":
        bad = [name for name, level in self.skills.items() if not 1 <= level <= 5]
        if bad:
            raise ValueError("技能等级必须在 1 到 5 之间")
        return self


class QueuePreview(BaseModel):
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    skills: dict[str, int] = Field(default_factory=dict)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_skill_levels(self) -> "QueuePreview":
        bad = [name for name, level in self.skills.items() if not 1 <= level <= 5]
        if bad:
            raise ValueError("技能等级必须在 1 到 5 之间")
        return self


class BoostRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000, description="临时提权原因（必填，会写入审计）")
    bonus: float = Field(gt=0, le=40, description="临时加权分值，最多 40 分")
    ttl_seconds: int = Field(default=1800, ge=60, le=86400, description="临时加权有效期（秒），到期自动失效")


class TaskResult(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    result: dict[str, Any]
    metrics: dict[str, Any] = Field(default_factory=dict)


class TaskFailure(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    error_code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = True


class CancelRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class RetryRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    priority: int | None = Field(default=None, ge=0, le=100)


class PriorityRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    priority: int = Field(ge=0, le=100)


class BatchOperation(BaseModel):
    task_ids: list[int] = Field(min_length=1, max_length=200)
    operation: Literal["cancel", "retry", "priority"]
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    priority: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_priority(self) -> "BatchOperation":
        if self.operation == "priority" and self.priority is None:
            raise ValueError("批量调整优先级时必须提供 priority")
        return self
