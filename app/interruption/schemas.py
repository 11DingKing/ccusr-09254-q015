"""服务端业务模块。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    NonNegativeInt,
    PositiveFloat,
    model_validator,
)


class InterruptionIn(BaseModel):
    interruption_id: str = Field(..., min_length=1, max_length=128)
    plan_version: str = Field(..., min_length=1, max_length=128)
    kind: Literal["natural_disaster", "enterprise_shutdown", "other"]
    occurred_at: AwareDatetime
    note: str = Field("", max_length=512)


class InterruptionOut(BaseModel):
    interruption_id: str
    plan_version: str
    kind: str
    status: str
    occurred_at: str
    resumed_at: str | None
    note: str
    version: int


class InterruptionRegisterResult(BaseModel):
    created: bool
    interruption: InterruptionOut


class ImpactIn(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=128)
    activity_id: str = Field(..., min_length=1, max_length=128)
    capabilities: dict[str, NonNegativeInt]
    completed: dict[str, NonNegativeInt] = {}


class ImpactBatchIn(BaseModel):
    impacts: list[ImpactIn] = Field(..., min_length=1)


class ImpactImportResult(BaseModel):
    accepted: int
    duplicates: list[str]


class ImpactOut(BaseModel):
    student_id: str
    activity_id: str
    capabilities: dict[str, int]
    completed: dict[str, int]
    gaps: dict[str, int]


class ImpactListOut(BaseModel):
    impacts: list[ImpactOut]


class SlotIn(BaseModel):
    slot_id: str = Field(..., min_length=1, max_length=128)
    capability: str = Field(..., min_length=1, max_length=128)
    start_at: AwareDatetime
    end_at: AwareDatetime
    conversion_ratio: PositiveFloat = 1.0
    source_plan_version: str | None = Field(None, max_length=128)

    @model_validator(mode="after")
    def _check_order(self) -> "SlotIn":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class GenerateIn(BaseModel):
    slots: list[SlotIn] = []
    student_ids: list[str] | None = None


class CompensationItemOut(BaseModel):
    item_id: str
    capability: str
    slot_id: str
    source_plan_version: str | None
    start_at: str
    end_at: str
    raw_seconds: int
    retained_seconds: int
    released_seconds: int
    conversion_ratio: float
    credited_seconds: int
    status: str


class CompensationPlanOut(BaseModel):
    plan_id: str
    interruption_id: str
    student_id: str
    activity_id: str
    status: str
    version: int
    confirmed_by: str | None
    gaps: dict[str, int]
    unfilled: dict[str, int]
    items: list[CompensationItemOut]
    audit: list[dict[str, Any]]


class GenerateResult(BaseModel):
    plans: list[CompensationPlanOut]
    existing: list[str]


class PlanListOut(BaseModel):
    plans: list[CompensationPlanOut]


class ConfirmIn(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=128)


class ItemAdjustIn(BaseModel):
    item_id: str = Field(..., min_length=1, max_length=256)
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    conversion_ratio: PositiveFloat | None = None


class AdjustIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)
    expected_version: int | None = None
    item_updates: list[ItemAdjustIn] = []


class ResumeIn(BaseModel):
    resumed_at: AwareDatetime


class ResumeResult(BaseModel):
    interruption_id: str
    status: str
    resumed_at: str
    plans_affected: int
    items_retained: int
    items_released: int
    retained_seconds: int
    released_seconds: int


class CapabilitySettlement(BaseModel):
    required_seconds: int
    original_seconds: int
    substitute_seconds: int
    released_seconds: int
    total_seconds: int
    fulfilled: bool


class SettlementOut(BaseModel):
    plan_id: str
    interruption_id: str
    student_id: str
    capabilities: dict[str, CapabilitySettlement]
    sources: list[dict[str, Any]]
    original_seconds: int
    substitute_seconds: int
    released_seconds: int
    total_seconds: int
    settled_at: str


class SettlementResult(BaseModel):
    created: bool
    settlement: SettlementOut
