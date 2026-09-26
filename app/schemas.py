"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


# ---------------------------------------------------------------------------
# 实训中断补偿
# ---------------------------------------------------------------------------


class InterruptionIn(BaseModel):
    interruption_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field("", max_length=256)
    interrupt_start: datetime
    interrupt_end: datetime

    @model_validator(mode="after")
    def _check_window(self) -> "InterruptionIn":
        if self.interrupt_end <= self.interrupt_start:
            raise ValueError("interrupt_end must be after interrupt_start")
        return self

    @field_validator("interrupt_start", "interrupt_end")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class ImpactEntryIn(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=128)
    activity_id: str = Field(..., min_length=1, max_length=128)
    activity_type: str = Field("regular", max_length=64)
    required_skill_codes: list[str] = Field(default_factory=list)
    # 原活动排期（可跨多段），与中断窗口求交得到损失学时。
    scheduled_segments: list[list[datetime]] = Field(default_factory=list)
    # 中断前/期间已完成的签到区间，用于避免重复计入。
    completed_segments: list[list[datetime]] = Field(default_factory=list)

    @field_validator("scheduled_segments", "completed_segments")
    @classmethod
    def _check_segments(
        cls, segments: list[list[datetime]]
    ) -> list[list[datetime]]:
        for pair in segments:
            if len(pair) != 2 or pair[0].tzinfo is None or pair[1].tzinfo is None:
                raise ValueError("each segment must be [aware-start, aware-end]")
            if pair[1] <= pair[0]:
                raise ValueError("segment end must be after start")
        return segments


class ImpactBatchIn(BaseModel):
    impacts: list[ImpactEntryIn]


class ImpactOut(BaseModel):
    student_id: str
    activity_id: str
    activity_type: str
    required_skill_codes: list[str]
    lost_start: datetime | None
    lost_end: datetime | None
    lost_seconds: int
    gap_seconds: int
    skill_gap_seconds: dict[str, int]
    already_completed_seconds: int


class InterruptionOut(BaseModel):
    interruption_id: str
    plan_version: str
    reason: str
    interrupt_start: datetime
    interrupt_end: datetime
    status: str
    impact_count: int
    impacts: list[ImpactOut] = []
    accepted: int | None = None
    duplicates: int | None = None
    resume_at: str | None = None
    released_slot_count: int | None = None
    released_seconds: int | None = None


class AlternativeIn(BaseModel):
    alternative_id: str = Field(..., min_length=1, max_length=128)
    title: str = Field("", max_length=256)
    skill_code: str = Field(..., min_length=1, max_length=64)
    weight: float = Field(1.0, gt=0)


class AvailabilityIn(BaseModel):
    alternative_id: str = Field(..., min_length=1, max_length=128)
    start_at: datetime
    end_at: datetime
    capacity: int = Field(1, ge=1)

    @model_validator(mode="after")
    def _check_window(self) -> "AvailabilityIn":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class AlternativeCatalogIn(BaseModel):
    alternatives: list[AlternativeIn] = Field(default_factory=list)
    availabilities: list[AvailabilityIn] = Field(default_factory=list)


class CompensationSlotOut(BaseModel):
    slot_id: int | None = None
    alternative_id: str
    title: str
    skill_code: str
    start_at: datetime
    end_at: datetime
    scheduled_seconds: int
    credited_seconds: int
    weight: float
    status: str = "SCHEDULED"


class SourceCreditOut(BaseModel):
    source: str
    student_id: str
    activity_id: str
    seconds: int


class CompensationPlanOut(BaseModel):
    plan_code: str
    plan_version: str
    interruption_id: str
    student_id: str
    status: str
    gap_seconds: int
    gap_by_skill: dict[str, int]
    completed_seconds: int
    settled_seconds: int
    locked_version: int
    fully_covered: bool
    slots: list[CompensationSlotOut] = []
    source_credits: list[dict[str, Any]] = []
    confirmed_at: datetime | None = None
    settled_at: datetime | None = None


class CompensationGenerateIn(BaseModel):
    interruption_id: str = Field(..., min_length=1, max_length=128)
    student_ids: list[str] = Field(default_factory=list)
    code_prefix: str = Field("CP", max_length=64)
    # 生成时即把方案锁定（学生已线下确认）的可选项；默认 PROPOSED。
    confirm: bool = False


class CompensationConfirmIn(BaseModel):
    expected_version: int = Field(..., ge=1)


class SlotAdjustmentIn(BaseModel):
    start_at: datetime | None = None
    end_at: datetime | None = None
    credited_seconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_window(self) -> "SlotAdjustmentIn":
        if (
            self.start_at is not None
            and self.end_at is not None
            and self.end_at <= self.start_at
        ):
            raise ValueError("end_at must be after start_at")
        return self


class SlotCompleteIn(BaseModel):
    credited_seconds: int | None = Field(default=None, ge=0)


class ResumeIn(BaseModel):
    resume_at: datetime | None = None


class SettleResultOut(BaseModel):
    interruption_id: str
    plan_version: str
    status: str
    plans: list[CompensationPlanOut]
    total_gap_seconds: int
    total_completed_seconds: int
    total_credited_seconds: int
    retained_source_seconds: int
    released_slot_count: int
    released_seconds: int
