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
)
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Interruption(Base):
    __tablename__ = "interruptions"

    interruption_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ImpactEntry(Base):
    __tablename__ = "interruption_impacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interruption_id: Mapped[str] = mapped_column(String(128), nullable=False)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capabilities: Mapped[dict] = mapped_column(JSON, nullable=False)
    completed: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "interruption_id", "student_id", name="uq_impacts_interruption_student"
        ),
        Index("ix_impacts_interruption", "interruption_id"),
    )


class CompensationPlan(Base):
    __tablename__ = "compensation_plans"

    plan_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    interruption_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    student_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="offered")
    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gaps: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    unfilled: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    audit: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "interruption_id", "student_id", name="uq_comp_plans_interruption_student"
        ),
    )


class CompensationItem(Base):
    __tablename__ = "compensation_items"

    item_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    slot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_plan_version: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    retained_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    conversion_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    credited_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="scheduled"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint("raw_seconds >= 0", name="ck_comp_items_raw_nonneg"),
        CheckConstraint("retained_seconds >= 0", name="ck_comp_items_retained_nonneg"),
        CheckConstraint("conversion_ratio > 0", name="ck_comp_items_ratio_pos"),
        Index("ix_comp_items_plan", "plan_id"),
    )


class Settlement(Base):
    __tablename__ = "compensation_settlements"

    plan_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
