"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .domain import ItemState, PlanState
from .models import (
    CompensationItem,
    CompensationPlan,
    ImpactEntry,
    Interruption,
    Settlement,
)


def get_interruption(db: Session, interruption_id: str) -> Interruption | None:
    return db.get(Interruption, interruption_id)


def insert_interruption(
    db: Session,
    *,
    interruption_id: str,
    plan_version: str,
    kind: str,
    occurred_at: datetime,
    note: str,
) -> Interruption | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Interruption).values(
        interruption_id=interruption_id,
        plan_version=plan_version,
        kind=kind,
        status="open",
        occurred_at=occurred_at,
        note=note,
        version=1,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["interruption_id"]
    ).returning(Interruption.interruption_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Interruption, interruption_id)
    return None


def insert_impacts(
    db: Session,
    *,
    interruption_id: str,
    impacts: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for impact in impacts:
        stmt = sqlite_insert(ImpactEntry).values(
            interruption_id=interruption_id,
            student_id=impact["student_id"],
            activity_id=impact["activity_id"],
            capabilities=impact["capabilities"],
            completed=impact["completed"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["interruption_id", "student_id"]
        ).returning(ImpactEntry.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(impact["student_id"])
        else:
            duplicates.append(impact["student_id"])
    db.commit()
    return accepted, duplicates


def list_impacts(db: Session, interruption_id: str) -> list[ImpactEntry]:
    stmt = (
        select(ImpactEntry)
        .where(ImpactEntry.interruption_id == interruption_id)
        .order_by(ImpactEntry.student_id)
    )
    return list(db.execute(stmt).scalars().all())


def get_impact(
    db: Session, interruption_id: str, student_id: str
) -> ImpactEntry | None:
    stmt = select(ImpactEntry).where(
        ImpactEntry.interruption_id == interruption_id,
        ImpactEntry.student_id == student_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def get_plan(db: Session, plan_id: str) -> CompensationPlan | None:
    return db.get(CompensationPlan, plan_id)


def list_plans(db: Session, interruption_id: str) -> list[CompensationPlan]:
    stmt = (
        select(CompensationPlan)
        .where(CompensationPlan.interruption_id == interruption_id)
        .order_by(CompensationPlan.student_id)
    )
    return list(db.execute(stmt).scalars().all())


def insert_plan(
    db: Session,
    *,
    plan_id: str,
    interruption_id: str,
    student_id: str,
    activity_id: str,
    gaps: dict[str, int],
    unfilled: dict[str, int],
    audit: list[dict[str, Any]],
) -> CompensationPlan | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(CompensationPlan).values(
        plan_id=plan_id,
        interruption_id=interruption_id,
        student_id=student_id,
        activity_id=activity_id,
        status=PlanState.OFFERED.value,
        gaps=gaps,
        unfilled=unfilled,
        audit=audit,
        version=1,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_id"]
    ).returning(CompensationPlan.plan_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is None:
        return None
    return db.get(CompensationPlan, plan_id)


def insert_items(db: Session, items: list[dict[str, Any]]) -> None:
    for item in items:
        db.add(CompensationItem(**item))
    db.flush()


def list_items(db: Session, plan_id: str) -> list[CompensationItem]:
    stmt = (
        select(CompensationItem)
        .where(CompensationItem.plan_id == plan_id)
        .order_by(CompensationItem.start_at, CompensationItem.item_id)
    )
    return list(db.execute(stmt).scalars().all())


def confirm_plan_atomic(
    db: Session,
    *,
    plan_id: str,
    student_id: str,
    expected_version: int,
    audit: list[dict[str, Any]],
    now: datetime,
) -> bool:
    """条件更新确认：只有仍处于 offered 且版本一致的方案能被锁定。"""
    stmt = (
        update(CompensationPlan)
        .where(CompensationPlan.plan_id == plan_id)
        .where(CompensationPlan.status == PlanState.OFFERED.value)
        .where(CompensationPlan.version == expected_version)
        .values(
            status=PlanState.CONFIRMED.value,
            confirmed_by=student_id,
            version=CompensationPlan.version + 1,
            audit=audit,
            updated_at=now,
        )
    )
    result = db.execute(stmt)
    return result.rowcount == 1


def settle_plan_atomic(
    db: Session,
    *,
    plan_id: str,
    expected_version: int,
    audit: list[dict[str, Any]],
    now: datetime,
) -> bool:
    """执行确定性的业务处理。"""
    stmt = (
        update(CompensationPlan)
        .where(CompensationPlan.plan_id == plan_id)
        .where(CompensationPlan.status == PlanState.CONFIRMED.value)
        .where(CompensationPlan.version == expected_version)
        .values(
            status=PlanState.SETTLED.value,
            version=CompensationPlan.version + 1,
            audit=audit,
            updated_at=now,
        )
    )
    result = db.execute(stmt)
    return result.rowcount == 1


def get_settlement(db: Session, plan_id: str) -> Settlement | None:
    return db.get(Settlement, plan_id)


def insert_settlement(
    db: Session, *, plan_id: str, result: dict[str, Any]
) -> Settlement | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Settlement).values(plan_id=plan_id, result=result)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_id"]
    ).returning(Settlement.plan_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is None:
        return None
    return db.get(Settlement, plan_id)


def scheduled_items(db: Session, plan_id: str) -> list[CompensationItem]:
    stmt = (
        select(CompensationItem)
        .where(CompensationItem.plan_id == plan_id)
        .where(CompensationItem.status == ItemState.SCHEDULED.value)
        .order_by(CompensationItem.start_at, CompensationItem.item_id)
    )
    return list(db.execute(stmt).scalars().all())
