"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class Interruption(Base):
    """自然灾害、停产等导致一批学生实训中断的事件。"""

    __tablename__ = "interruptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interruption_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    interrupt_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    interrupt_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "interruption_id", "plan_version", name="uq_interruptions_id_plan"
        ),
        CheckConstraint(
            "interrupt_end > interrupt_start", name="ck_interruptions_window_order"
        ),
    )


class InterruptionImpact(Base):
    """中断事件影响名单中的单个学生及其缺口明细。"""

    __tablename__ = "interruption_impacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interruption_pk: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True
    )
    student_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    required_skill_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    lost_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lost_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lost_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    skill_gap_seconds: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    already_completed_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "interruption_pk",
            "student_id",
            "activity_id",
            name="uq_impact_interruption_student_activity",
        ),
        CheckConstraint("lost_seconds >= 0", name="ck_impact_lost_nonneg"),
        CheckConstraint(
            "already_completed_seconds >= 0", name="ck_impact_completed_nonneg"
        ),
    )


class CompensationPlan(Base):
    """针对单个受影响学生生成的补偿方案。"""

    __tablename__ = "compensation_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_code: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    interruption_pk: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PROPOSED")
    gap_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    gap_by_skill: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    completed_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    settled_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_credits: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    locked_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "interruption_pk", "student_id", name="uq_comp_interruption_student"
        ),
        UniqueConstraint(
            "plan_code", "plan_version", name="uq_comp_code_plan"
        ),
        CheckConstraint("gap_seconds >= 0", name="ck_comp_gap_nonneg"),
        CheckConstraint("completed_seconds >= 0", name="ck_comp_completed_nonneg"),
        CheckConstraint("settled_seconds >= 0", name="ck_comp_settled_nonneg"),
        CheckConstraint("locked_version >= 1", name="ck_comp_version_positive"),
    )


class CompensationSlot(Base):
    """补偿方案中的一条替代安排（替代活动的某个可用时段）。"""

    __tablename__ = "compensation_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    comp_plan_pk: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    alternative_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    skill_code: Mapped[str] = mapped_column(String(64), nullable=False)
    start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scheduled_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    credited_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="SCHEDULED")
    source_slot_pk: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "comp_plan_pk", "alternative_id", "start_at",
            name="uq_slot_plan_alt_start",
        ),
        CheckConstraint("scheduled_seconds > 0", name="ck_slot_scheduled_positive"),
        CheckConstraint("credited_seconds >= 0", name="ck_slot_credited_nonneg"),
        CheckConstraint("weight > 0", name="ck_slot_weight_positive"),
    )


class AlternativeActivity(Base):
    """可用于补偿的替代活动及其能力（技能）目录。"""

    __tablename__ = "alternative_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    alternative_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    skill_code: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    __table_args__ = (
        UniqueConstraint(
            "plan_version", "alternative_id", name="uq_alt_plan_alternative"
        ),
    )


class AlternativeAvailability(Base):
    """替代活动的可预约时段（按培养方案维护的共享资源）。"""

    __tablename__ = "alternative_availabilities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    alternative_id: Mapped[str] = mapped_column(String(128), nullable=False)
    start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "plan_version",
            "alternative_id",
            "start_at",
            "end_at",
            name="uq_avail_plan_alt_window",
        ),
        CheckConstraint("capacity > 0", name="ck_avail_capacity_positive"),
        CheckConstraint("used >= 0", name="ck_avail_used_nonneg"),
        CheckConstraint("used <= capacity", name="ck_avail_used_within_capacity"),
    )
