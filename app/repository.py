"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import (
    AlternativeActivity,
    AlternativeAvailability,
    CompensationPlan,
    CompensationSlot,
    Event as EventModel,
    Freeze,
    Interruption,
    InterruptionImpact,
    Plan,
)


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


# ---------------------------------------------------------------------------
# 中断事件与影响名单
# ---------------------------------------------------------------------------


def insert_interruption(
    db: Session,
    *,
    interruption_id: str,
    plan_version: str,
    reason: str,
    interrupt_start: datetime,
    interrupt_end: datetime,
) -> Interruption | None:
    """登记中断事件；已存在同号事件时返回 None（幂等）。"""

    stmt = sqlite_insert(Interruption).values(
        interruption_id=interruption_id,
        plan_version=plan_version,
        reason=reason,
        interrupt_start=interrupt_start,
        interrupt_end=interrupt_end,
        status="ACTIVE",
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["interruption_id", "plan_version"]
    ).returning(Interruption.id)
    inserted_id = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted_id is None:
        return None
    return db.get(Interruption, inserted_id)


def get_interruption_by_code(
    db: Session, plan_version: str, interruption_id: str
) -> Interruption | None:
    stmt = select(Interruption).where(
        Interruption.plan_version == plan_version,
        Interruption.interruption_id == interruption_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def get_interruption(db: Session, interruption_pk: int) -> Interruption | None:
    return db.get(Interruption, interruption_pk)


def list_interruptions(db: Session, plan_version: str) -> list[Interruption]:
    stmt = (
        select(Interruption)
        .where(Interruption.plan_version == plan_version)
        .order_by(Interruption.id)
    )
    return list(db.execute(stmt).scalars().all())


def mark_interruption_status(
    db: Session, interruption_pk: int, status: str
) -> None:
    db.execute(
        update(Interruption)
        .where(Interruption.id == interruption_pk)
        .values(status=status)
    )
    db.commit()


def insert_impacts(
    db: Session,
    *,
    interruption_pk: int,
    impacts: list[dict[str, Any]],
) -> tuple[int, int]:
    """批量写入影响名单，按 (中断, 学生, 原活动) 幂等去重。"""

    accepted = 0
    duplicates = 0
    for item in impacts:
        stmt = sqlite_insert(InterruptionImpact).values(
            interruption_pk=interruption_pk,
            student_id=item["student_id"],
            activity_id=item["activity_id"],
            activity_type=item.get("activity_type", "regular"),
            required_skill_codes=list(item.get("required_skill_codes", [])),
            lost_start=item["lost_start"],
            lost_end=item["lost_end"],
            lost_seconds=item["lost_seconds"],
            skill_gap_seconds=dict(item.get("skill_gap_seconds", {})),
            already_completed_seconds=item.get("already_completed_seconds", 0),
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["interruption_pk", "student_id", "activity_id"]
        ).returning(InterruptionImpact.id)
        if db.execute(stmt).scalar_one_or_none() is not None:
            accepted += 1
        else:
            duplicates += 1
    db.commit()
    return accepted, duplicates


def list_impacts(
    db: Session, interruption_pk: int
) -> list[InterruptionImpact]:
    stmt = (
        select(InterruptionImpact)
        .where(InterruptionImpact.interruption_pk == interruption_pk)
        .order_by(InterruptionImpact.student_id, InterruptionImpact.activity_id)
    )
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# 替代活动目录与可用时段
# ---------------------------------------------------------------------------


def upsert_alternative(
    db: Session,
    *,
    plan_version: str,
    alternative_id: str,
    title: str,
    skill_code: str,
    weight: float,
) -> None:
    stmt = sqlite_insert(AlternativeActivity).values(
        plan_version=plan_version,
        alternative_id=alternative_id,
        title=title,
        skill_code=skill_code,
        weight=weight,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version", "alternative_id"],
        set_={
            "title": title,
            "skill_code": skill_code,
            "weight": weight,
        },
    )
    db.execute(stmt)
    db.commit()


def list_alternatives(
    db: Session, plan_version: str
) -> list[AlternativeActivity]:
    stmt = select(AlternativeActivity).where(
        AlternativeActivity.plan_version == plan_version
    )
    return list(db.execute(stmt).scalars().all())


def upsert_availability(
    db: Session,
    *,
    plan_version: str,
    alternative_id: str,
    start_at: datetime,
    end_at: datetime,
    capacity: int,
) -> None:
    stmt = sqlite_insert(AlternativeAvailability).values(
        plan_version=plan_version,
        alternative_id=alternative_id,
        start_at=start_at,
        end_at=end_at,
        capacity=capacity,
        used=0,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version", "alternative_id", "start_at", "end_at"],
        set_={"capacity": capacity},
    )
    db.execute(stmt)
    db.commit()


def list_availabilities(
    db: Session, plan_version: str
) -> list[AlternativeAvailability]:
    stmt = select(AlternativeAvailability).where(
        AlternativeAvailability.plan_version == plan_version
    )
    return list(db.execute(stmt).scalars().all())


def try_book_availability(db: Session, availability_pk: int, seats: int = 1) -> bool:
    """条件更新占用名额：仅当仍有容量时成功（并发安全）。"""

    stmt = (
        update(AlternativeAvailability)
        .where(
            AlternativeAvailability.id == availability_pk,
            AlternativeAvailability.used + seats <= AlternativeAvailability.capacity,
        )
        .values(used=AlternativeAvailability.used + seats)
    )
    result = db.execute(stmt)
    db.commit()
    return (result.rowcount or 0) == 1


def release_availability(db: Session, availability_pk: int, seats: int = 1) -> None:
    stmt = (
        update(AlternativeAvailability)
        .where(AlternativeAvailability.id == availability_pk)
        .values(used=AlternativeAvailability.used - seats)
    )
    db.execute(stmt)
    db.commit()


def find_availability_pk(
    db: Session,
    plan_version: str,
    alternative_id: str,
    start_at: datetime,
    end_at: datetime,
) -> int | None:
    stmt = select(AlternativeAvailability.id).where(
        AlternativeAvailability.plan_version == plan_version,
        AlternativeAvailability.alternative_id == alternative_id,
        AlternativeAvailability.start_at == start_at,
        AlternativeAvailability.end_at == end_at,
    )
    return db.execute(stmt).scalar_one_or_none()


# ---------------------------------------------------------------------------
# 补偿方案与替代安排
# ---------------------------------------------------------------------------


def insert_compensation_plan(
    db: Session,
    *,
    plan_code: str,
    plan_version: str,
    interruption_pk: int,
    student_id: str,
    gap_seconds: int,
    gap_by_skill: dict[str, Any],
) -> CompensationPlan | None:
    stmt = sqlite_insert(CompensationPlan).values(
        plan_code=plan_code,
        plan_version=plan_version,
        interruption_pk=interruption_pk,
        student_id=student_id,
        status="PROPOSED",
        gap_seconds=gap_seconds,
        gap_by_skill=gap_by_skill,
        completed_seconds=0,
        settled_seconds=0,
        source_credits=[],
        locked_version=1,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["interruption_pk", "student_id"]
    ).returning(CompensationPlan.id)
    plan_pk = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if plan_pk is None:
        return None
    return db.get(CompensationPlan, plan_pk)


def get_compensation_plan_by_code(
    db: Session, plan_version: str, plan_code: str
) -> CompensationPlan | None:
    stmt = select(CompensationPlan).where(
        CompensationPlan.plan_version == plan_version,
        CompensationPlan.plan_code == plan_code,
    )
    return db.execute(stmt).scalar_one_or_none()


def get_compensation_plan(db: Session, plan_pk: int) -> CompensationPlan | None:
    return db.get(CompensationPlan, plan_pk)


def list_compensation_plans(
    db: Session,
    *,
    plan_version: str,
    interruption_pk: int | None = None,
    student_id: str | None = None,
) -> list[CompensationPlan]:
    stmt = select(CompensationPlan).where(
        CompensationPlan.plan_version == plan_version
    )
    if interruption_pk is not None:
        stmt = stmt.where(CompensationPlan.interruption_pk == interruption_pk)
    if student_id is not None:
        stmt = stmt.where(CompensationPlan.student_id == student_id)
    stmt = stmt.order_by(CompensationPlan.id)
    return list(db.execute(stmt).scalars().all())


def insert_compensation_slot(
    db: Session,
    *,
    comp_plan_pk: int,
    alternative_id: str,
    title: str,
    skill_code: str,
    start_at: datetime,
    end_at: datetime,
    scheduled_seconds: int,
    credited_seconds: int,
    weight: float,
    source_slot_pk: int | None = None,
) -> CompensationSlot:
    row = CompensationSlot(
        comp_plan_pk=comp_plan_pk,
        alternative_id=alternative_id,
        title=title,
        skill_code=skill_code,
        start_at=start_at,
        end_at=end_at,
        scheduled_seconds=scheduled_seconds,
        credited_seconds=credited_seconds,
        weight=weight,
        status="SCHEDULED",
        source_slot_pk=source_slot_pk,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_compensation_slots(
    db: Session, comp_plan_pk: int
) -> list[CompensationSlot]:
    stmt = (
        select(CompensationSlot)
        .where(CompensationSlot.comp_plan_pk == comp_plan_pk)
        .order_by(CompensationSlot.start_at, CompensationSlot.id)
    )
    return list(db.execute(stmt).scalars().all())


def get_compensation_slot(db: Session, slot_pk: int) -> CompensationSlot | None:
    return db.get(CompensationSlot, slot_pk)


def update_compensation_slot(
    db: Session, slot_pk: int, **values: Any
) -> None:
    db.execute(
        update(CompensationSlot)
        .where(CompensationSlot.id == slot_pk)
        .values(**values)
    )
    db.commit()


def update_compensation_plan(
    db: Session, plan_pk: int, **values: Any
) -> None:
    db.execute(
        update(CompensationPlan)
        .where(CompensationPlan.id == plan_pk)
        .values(**values)
    )
    db.commit()


def try_confirm_compensation_plan(
    db: Session, plan_pk: int, *, expected_version: int, confirmed_at: datetime
) -> bool:
    """乐观锁确认：仅 PROPOSED 且版本匹配时把方案锁定为 CONFIRMED。"""

    stmt = (
        update(CompensationPlan)
        .where(
            CompensationPlan.id == plan_pk,
            CompensationPlan.status == "PROPOSED",
            CompensationPlan.locked_version == expected_version,
        )
        .values(
            status="CONFIRMED",
            confirmed_at=confirmed_at,
            locked_version=CompensationPlan.locked_version + 1,
        )
    )
    result = db.execute(stmt)
    db.commit()
    return (result.rowcount or 0) == 1
