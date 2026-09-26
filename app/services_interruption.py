"""实训中断补偿的应用服务层。

串联仓储与 ``app.core.compensation`` 的纯领域逻辑，对外提供：

- 中断事件登记与影响名单批量导入（按能力要求计算缺口，已完成部分不重复计入）；
- 替代活动目录/可用时段维护；
- 按能力要求与可用时段批量生成补偿方案；
- 学生确认（乐观锁，并发安全）后锁定方案；
- 方案调整（确认前改期、确认后登记完成情况）；
- 原活动恢复：只释放尚未开始的补偿，已完成部分保留来源；
- 中断结算汇总。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.clock import to_utc
from .core.compensation import (
    Alternative,
    AvailabilityWindow,
    BusyWindow,
    build_gap,
    merge_gaps,
    plan_compensation,
)
from .core.replay import replay
from . import repository as repo
from .models import (
    AlternativeAvailability,
    CompensationPlan,
    CompensationSlot,
    Interruption,
    InterruptionImpact,
)

STATUS_PROPOSED = "PROPOSED"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_SETTLED = "SETTLED"

SLOT_SCHEDULED = "SCHEDULED"
SLOT_COMPLETED = "COMPLETED"
SLOT_RELEASED = "RELEASED"

INTERRUPTION_ACTIVE = "ACTIVE"
INTERRUPTION_RECOVERED = "RECOVERED"
INTERRUPTION_SETTLED = "SETTLED"


class PlanNotFoundError(Exception):
    pass


class InterruptionNotFoundError(Exception):
    pass


class InterruptionConflictError(Exception):
    pass


class CompensationConflictError(Exception):
    pass


class CapacityExhaustedError(Exception):
    pass


class CompensationNotFoundError(Exception):
    pass


class SlotNotFoundError(Exception):
    pass


class ValidationError(Exception):
    pass


def _require_plan(db: Session, plan_version: str):
    plan = repo.get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def _db_utc(value: datetime) -> datetime:
    """SQLite 读回的时间不带时区，写入时已统一为 UTC，这里补回 tzinfo。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_interruption(
    db: Session, plan_version: str, interruption_id: str
) -> Interruption:
    row = repo.get_interruption_by_code(db, plan_version, interruption_id)
    if row is None:
        raise InterruptionNotFoundError(
            f"interruption '{interruption_id}' is not registered"
        )
    return row


# ---------------------------------------------------------------------------
# 中断登记与影响名单
# ---------------------------------------------------------------------------


def register_interruption(
    db: Session,
    *,
    plan_version: str,
    interruption_id: str,
    reason: str,
    interrupt_start: datetime,
    interrupt_end: datetime,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    if repo.get_interruption_by_code(db, plan_version, interruption_id) is not None:
        raise InterruptionConflictError(
            f"interruption '{interruption_id}' already exists"
        )
    row = repo.insert_interruption(
        db,
        interruption_id=interruption_id,
        plan_version=plan_version,
        reason=reason,
        interrupt_start=to_utc(interrupt_start),
        interrupt_end=to_utc(interrupt_end),
    )
    assert row is not None
    return serialize_interruption(row, [])


def register_impacts(
    db: Session,
    *,
    plan_version: str,
    interruption_id: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    interruption = _require_interruption(db, plan_version, interruption_id)

    payload: list[dict[str, Any]] = []
    for entry in entries:
        skill_codes = entry.get("required_skill_codes") or []
        if not skill_codes:
            raise ValidationError(
                f"student '{entry.get('student_id')}' impact requires "
                "required_skill_codes"
            )
        gap = build_gap(
            student_id=entry["student_id"],
            activity_id=entry["activity_id"],
            activity_type=entry.get("activity_type", "regular"),
            skill_codes=skill_codes,
            interrupt_start=_db_utc(interruption.interrupt_start),
            interrupt_end=_db_utc(interruption.interrupt_end),
            scheduled_segments=[
                (to_utc(pair[0]), to_utc(pair[1]))
                for pair in entry.get("scheduled_segments", [])
            ],
            completed_segments=[
                (to_utc(pair[0]), to_utc(pair[1]))
                for pair in entry.get("completed_segments", [])
            ],
        )
        payload.append(
            {
                "student_id": gap.student_id,
                "activity_id": gap.activity_id,
                "activity_type": gap.activity_type,
                "required_skill_codes": gap.skill_codes,
                "lost_start": gap.lost_start,
                "lost_end": gap.lost_end,
                "lost_seconds": gap.gap_seconds + gap.completed_seconds,
                "skill_gap_seconds": gap.gap_by_skill,
                "already_completed_seconds": gap.completed_seconds,
            }
        )

    accepted, duplicates = repo.insert_impacts(
        db, interruption_pk=interruption.id, impacts=payload
    )
    impacts = repo.list_impacts(db, interruption.id)
    result = serialize_interruption(interruption, impacts)
    result["accepted"] = accepted
    result["duplicates"] = duplicates
    return result


def get_interruption_detail(
    db: Session, plan_version: str, interruption_id: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    interruption = _require_interruption(db, plan_version, interruption_id)
    impacts = repo.list_impacts(db, interruption.id)
    return serialize_interruption(interruption, impacts)


# ---------------------------------------------------------------------------
# 替代活动目录与可用时段
# ---------------------------------------------------------------------------


def upsert_catalog(
    db: Session,
    *,
    plan_version: str,
    alternatives: list[dict[str, Any]],
    availabilities: list[dict[str, Any]],
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    known = {a.alternative_id for a in repo.list_alternatives(db, plan_version)}
    for alt in alternatives:
        repo.upsert_alternative(
            db,
            plan_version=plan_version,
            alternative_id=alt["alternative_id"],
            title=alt.get("title", ""),
            skill_code=alt["skill_code"],
            weight=float(alt.get("weight", 1.0)),
        )
        known.add(alt["alternative_id"])
    for window in availabilities:
        if window["alternative_id"] not in known:
            # 允许目录条目与时段在同一请求中提交，故不拒绝未知活动的历史数据。
            continue
        repo.upsert_availability(
            db,
            plan_version=plan_version,
            alternative_id=window["alternative_id"],
            start_at=to_utc(window["start_at"]),
            end_at=to_utc(window["end_at"]),
            capacity=int(window.get("capacity", 1)),
        )
    return {
        "alternatives": len(alternatives),
        "availabilities": len(
            [w for w in availabilities if w["alternative_id"] in known]
        ),
    }


# ---------------------------------------------------------------------------
# 方案生成
# ---------------------------------------------------------------------------


def _student_busy(db: Session, plan_version: str, student_id: str):
    """从既有签到事件重放学生忙碌时段，避免与替代安排冲突。"""

    events = repo.load_events(db, plan_version)
    state = replay(
        events,
        plan_version=plan_version,
        timezone_name="UTC",
        required_seconds=0,
    )
    progress = state.students.get(student_id)
    if progress is None:
        return []
    return [
        BusyWindow(start_utc=r.start_utc, end_utc=r.end_utc)
        for r in progress.checkins
    ]


def _live_reserved_slots(
    db: Session, plan_version: str
) -> list[CompensationSlot]:
    """所有仍占用共享容量的替代安排（已释放的不计）。"""

    plans = {p.id: p for p in repo.list_compensation_plans(db, plan_version=plan_version)}
    slots: list[CompensationSlot] = []
    for plan in plans.values():
        for slot in repo.list_compensation_slots(db, plan.id):
            if slot.status in (SLOT_SCHEDULED, SLOT_COMPLETED):
                slots.append(slot)
    return slots


def _window_for(
    windows: list[AlternativeAvailability],
    alternative_id: str,
    start_at: datetime,
    end_at: datetime,
) -> AlternativeAvailability | None:
    start_at, end_at = to_utc(start_at), to_utc(end_at)
    for window in windows:
        w_start, w_end = _db_utc(window.start_at), _db_utc(window.end_at)
        if (
            window.alternative_id == alternative_id
            and w_start <= start_at
            and w_end >= end_at
        ):
            return window
    return None


def generate_compensation(
    db: Session,
    *,
    plan_version: str,
    interruption_id: str,
    student_ids: list[str] | None = None,
    code_prefix: str = "CP",
    confirm: bool = False,
) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    interruption = _require_interruption(db, plan_version, interruption_id)
    if interruption.status != INTERRUPTION_ACTIVE:
        raise CompensationConflictError(
            f"interruption is {interruption.status}; no new plan can be generated"
        )

    impacts = repo.list_impacts(db, interruption.id)
    wanted = set(student_ids) if student_ids else None
    by_student: dict[str, list[InterruptionImpact]] = {}
    for impact in impacts:
        if wanted is not None and impact.student_id not in wanted:
            continue
        by_student.setdefault(impact.student_id, []).append(impact)

    alternatives = [
        Alternative(
            alternative_id=a.alternative_id,
            title=a.title,
            skill_code=a.skill_code,
            weight=a.weight,
        )
        for a in repo.list_alternatives(db, plan_version)
    ]
    windows = repo.list_availabilities(db, plan_version)
    availabilities = [
        AvailabilityWindow(
            alternative_id=w.alternative_id,
            start_utc=_db_utc(w.start_at),
            end_utc=_db_utc(w.end_at),
            capacity=w.capacity,
        )
        for w in windows
    ]
    reserved_rows = _live_reserved_slots(db, plan_version)
    reserved: list[tuple[str, str, datetime, datetime]] = [
        (
            f"plan-{s.comp_plan_pk}",
            s.alternative_id,
            _db_utc(s.start_at),
            _db_utc(s.end_at),
        )
        for s in reserved_rows
    ]

    results: list[dict[str, Any]] = []
    for student_id in sorted(by_student):
        existing = repo.list_compensation_plans(
            db,
            plan_version=plan_version,
            interruption_pk=interruption.id,
            student_id=student_id,
        )
        if existing:
            # 重复生成保持幂等：直接返回既有方案。
            results.append(
                serialize_plan(db, plan_version, interruption, existing[0])
            )
            continue

        student_impacts = by_student[student_id]
        gaps = [
            _gap_from_impact(impact)
            for impact in student_impacts
            if (impact.lost_seconds - impact.already_completed_seconds) > 0
        ]
        merged = merge_gaps(gaps)
        if merged is None or merged.gap_seconds <= 0:
            continue

        busy = _student_busy(db, plan_version, student_id)
        proposal = plan_compensation(
            merged,
            alternatives=alternatives,
            availabilities=availabilities,
            busy=busy,
            reserved=reserved,
        )

        plan_row = repo.insert_compensation_plan(
            db,
            plan_code=f"{code_prefix}-{interruption.id:04d}-{student_id}",
            plan_version=plan_version,
            interruption_pk=interruption.id,
            student_id=student_id,
            gap_seconds=merged.gap_seconds,
            gap_by_skill=merged.gap_by_skill,
        )
        assert plan_row is not None

        # 容量按“方案占窗”计数：同一方案在同一窗口的多个分段总共只占一个
        # 名额，故每个窗口只占用一次。
        booked_window_ids: set[int] = set()
        for slot in proposal.slots:
            window = _window_for(
                windows, slot.alternative_id, slot.start_utc, slot.end_utc
            )
            if window is None:
                raise CapacityExhaustedError(
                    f"no availability window covers alternative "
                    f"'{slot.alternative_id}'"
                )
            if window.id not in booked_window_ids:
                if not repo.try_book_availability(db, window.id):
                    # 容量在生成过程中被并发占满：当前无法锁定该窗口。
                    raise CapacityExhaustedError(
                        f"capacity exhausted for alternative "
                        f"'{slot.alternative_id}'"
                    )
                booked_window_ids.add(window.id)
            repo.insert_compensation_slot(
                db,
                comp_plan_pk=plan_row.id,
                alternative_id=slot.alternative_id,
                title=slot.title,
                skill_code=slot.skill_code,
                start_at=slot.start_utc,
                end_at=slot.end_utc,
                scheduled_seconds=slot.scheduled_seconds,
                credited_seconds=slot.credited_seconds,
                weight=slot.weight,
            )
            reserved.append(
                (
                    f"plan-{plan_row.id}",
                    slot.alternative_id,
                    slot.start_utc,
                    slot.end_utc,
                )
            )

        if confirm:
            _lock_plan(db, plan_row, expected_version=1)
        results.append(serialize_plan(db, plan_version, interruption, plan_row))

    return results


def _gap_from_impact(impact: InterruptionImpact):
    from .core.compensation import SkillGap

    gap_seconds = max(
        impact.lost_seconds - impact.already_completed_seconds, 0
    )
    return SkillGap(
        student_id=impact.student_id,
        activity_id=impact.activity_id,
        activity_type=impact.activity_type,
        skill_codes=list(impact.required_skill_codes),
        gap_seconds=gap_seconds,
        gap_by_skill=dict(impact.skill_gap_seconds),
        completed_seconds=impact.already_completed_seconds,
        lost_start=impact.lost_start,
        lost_end=impact.lost_end,
    )


# ---------------------------------------------------------------------------
# 确认（锁定）
# ---------------------------------------------------------------------------


def _lock_plan(db: Session, plan_row: CompensationPlan, *, expected_version: int):
    ok = repo.try_confirm_compensation_plan(
        db,
        plan_row.id,
        expected_version=expected_version,
        confirmed_at=datetime.now(timezone.utc),
    )
    if not ok:
        raise CompensationConflictError(
            "plan is not PROPOSED or expected_version is stale"
        )


def confirm_compensation(
    db: Session,
    *,
    plan_version: str,
    plan_code: str,
    expected_version: int,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    plan_row = _require_plan_row(db, plan_version, plan_code)
    _lock_plan(db, plan_row, expected_version=expected_version)
    db.refresh(plan_row)
    interruption = repo.get_interruption(db, plan_row.interruption_pk)
    assert interruption is not None
    return serialize_plan(db, plan_version, interruption, plan_row)


# ---------------------------------------------------------------------------
# 调整
# ---------------------------------------------------------------------------


def _require_plan_row(
    db: Session, plan_version: str, plan_code: str
) -> CompensationPlan:
    row = repo.get_compensation_plan_by_code(db, plan_version, plan_code)
    if row is None:
        raise CompensationNotFoundError(
            f"compensation plan '{plan_code}' does not exist"
        )
    return row


def _require_slot(db: Session, plan_row: CompensationPlan, slot_id: int):
    slot = repo.get_compensation_slot(db, slot_id)
    if slot is None or slot.comp_plan_pk != plan_row.id:
        raise SlotNotFoundError(f"slot {slot_id} does not belong to {plan_row.plan_code}")
    return slot


def adjust_slot(
    db: Session,
    *,
    plan_version: str,
    plan_code: str,
    slot_id: int,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    credited_seconds: int | None = None,
) -> dict[str, Any]:
    plan_row = _require_plan_row(db, plan_version, plan_code)
    interruption = repo.get_interruption(db, plan_row.interruption_pk)
    assert interruption is not None
    if plan_row.status != STATUS_PROPOSED:
        raise CompensationConflictError(
            "plan is locked by student confirmation; rescheduling is rejected"
        )
    slot = _require_slot(db, plan_row, slot_id)

    new_start = to_utc(start_at) if start_at is not None else _db_utc(slot.start_at)
    new_end = to_utc(end_at) if end_at is not None else _db_utc(slot.end_at)
    if new_end <= new_start:
        raise ValidationError("end_at must be after start_at")

    moved = new_start != _db_utc(slot.start_at) or new_end != _db_utc(slot.end_at)
    if moved:
        windows = repo.list_availabilities(db, plan_version)
        target = _window_for(windows, slot.alternative_id, new_start, new_end)
        if target is None:
            raise ValidationError(
                "requested time is outside any availability window"
            )

        def _occupying_plan_pks(window_pk: int) -> set[int]:
            owners: set[int] = set()
            for other in _live_reserved_slots(db, plan_version):
                other_window = _window_for(
                    windows,
                    other.alternative_id,
                    _db_utc(other.start_at),
                    _db_utc(other.end_at),
                )
                if other_window is not None and other_window.id == window_pk:
                    owners.add(other.comp_plan_pk)
            return owners

        # 目标窗口的名额按不同方案计数；排除当前方案自身（同方案迁段不重复占名额）。
        target_owners = _occupying_plan_pks(target.id) - {plan_row.id}
        if len(target_owners) >= target.capacity:
            raise CapacityExhaustedError("target window has no remaining capacity")

        old_window = _window_for(
            windows,
            slot.alternative_id,
            _db_utc(slot.start_at),
            _db_utc(slot.end_at),
        )
        same_window = old_window is not None and old_window.id == target.id
        plan_slots = [
            s
            for s in _live_reserved_slots(db, plan_version)
            if s.comp_plan_pk == plan_row.id and s.id != slot.id
        ]
        plan_still_in_old = any(
            (
                w := _window_for(
                    windows,
                    s.alternative_id,
                    _db_utc(s.start_at),
                    _db_utc(s.end_at),
                )
            )
            is not None
            and old_window is not None
            and w.id == old_window.id
            for s in plan_slots
        )
        plan_already_in_target = any(
            (
                w := _window_for(
                    windows,
                    s.alternative_id,
                    _db_utc(s.start_at),
                    _db_utc(s.end_at),
                )
            )
            is not None
            and w.id == target.id
            for s in plan_slots
        )
        if old_window is not None and not same_window and not plan_still_in_old:
            repo.release_availability(db, old_window.id)
        need_new_seat = not same_window and not plan_already_in_target
        if need_new_seat and not repo.try_book_availability(db, target.id):
            # 释放后重新占回原窗口，保持状态一致。
            if old_window is not None and not plan_still_in_old:
                repo.try_book_availability(db, old_window.id)
            raise CapacityExhaustedError("target window has no remaining capacity")

    values: dict[str, Any] = {}
    if moved:
        values.update(start_at=new_start, end_at=new_end)
        values["scheduled_seconds"] = int((new_end - new_start).total_seconds())
    if credited_seconds is not None:
        values["credited_seconds"] = credited_seconds
    if values:
        repo.update_compensation_slot(db, slot.id, **values)
    return serialize_plan(
        db, plan_version, interruption, _require_plan_row(db, plan_version, plan_code)
    )


def complete_slot(
    db: Session,
    *,
    plan_version: str,
    plan_code: str,
    slot_id: int,
    credited_seconds: int | None = None,
) -> dict[str, Any]:
    plan_row = _require_plan_row(db, plan_version, plan_code)
    interruption = repo.get_interruption(db, plan_row.interruption_pk)
    assert interruption is not None
    if plan_row.status != STATUS_CONFIRMED:
        raise CompensationConflictError(
            "only CONFIRMED plans can register completed slots"
        )
    slot = _require_slot(db, plan_row, slot_id)
    if slot.status != SLOT_SCHEDULED:
        raise CompensationConflictError(
            f"slot is already {slot.status}"
        )
    credited = slot.credited_seconds if credited_seconds is None else credited_seconds
    repo.update_compensation_slot(
        db,
        slot.id,
        status=SLOT_COMPLETED,
        credited_seconds=credited,
    )
    slots = repo.list_compensation_slots(db, plan_row.id)
    repo.update_compensation_plan(
        db,
        plan_row.id,
        completed_seconds=sum(
            s.credited_seconds for s in slots if s.status == SLOT_COMPLETED
        ),
    )
    return serialize_plan(
        db, plan_version, interruption, _require_plan_row(db, plan_version, plan_code)
    )


# ---------------------------------------------------------------------------
# 原活动恢复：只释放尚未开始的补偿
# ---------------------------------------------------------------------------


def resume_interruption(
    db: Session,
    *,
    plan_version: str,
    interruption_id: str,
    resume_at: datetime | None = None,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    interruption = _require_interruption(db, plan_version, interruption_id)
    if interruption.status == INTERRUPTION_SETTLED:
        raise CompensationConflictError("interruption is already settled")
    moment = to_utc(resume_at) if resume_at is not None else datetime.now(timezone.utc)

    released_count = 0
    released_seconds = 0
    plans = repo.list_compensation_plans(
        db, plan_version=plan_version, interruption_pk=interruption.id
    )
    windows = repo.list_availabilities(db, plan_version)
    for plan_row in plans:
        slots = repo.list_compensation_slots(db, plan_row.id)
        to_release = [
            s
            for s in slots
            if s.status == SLOT_SCHEDULED and _db_utc(s.start_at) >= moment
        ]
        # 恢复时刻尚未开始的补偿才释放；已开始/完成的保留。
        for slot in to_release:
            repo.update_compensation_slot(
                db,
                slot.id,
                status=SLOT_RELEASED,
                credited_seconds=0,
            )
            released_count += 1
            released_seconds += slot.scheduled_seconds

        # 名额按方案-窗口计数：仅当该方案在窗口内不再保留任何已开始/完成
        # 时段时，才归还这个窗口的一个名额。
        release_ids = {s.id for s in to_release}
        kept_window_ids: set[int] = set()
        for s in slots:
            if s.id in release_ids:
                continue
            w = _window_for(
                windows,
                s.alternative_id,
                _db_utc(s.start_at),
                _db_utc(s.end_at),
            )
            if w is not None:
                kept_window_ids.add(w.id)
        freed_window_ids: set[int] = set()
        for s in to_release:
            w = _window_for(
                windows,
                s.alternative_id,
                _db_utc(s.start_at),
                _db_utc(s.end_at),
            )
            if (
                w is not None
                and w.id not in kept_window_ids
                and w.id not in freed_window_ids
            ):
                repo.release_availability(db, w.id)
                freed_window_ids.add(w.id)

    repo.mark_interruption_status(db, interruption.id, INTERRUPTION_RECOVERED)
    db.refresh(interruption)
    result = serialize_interruption(interruption, repo.list_impacts(db, interruption.id))
    result["released_slot_count"] = released_count
    result["released_seconds"] = released_seconds
    result["resume_at"] = moment.isoformat().replace("+00:00", "Z")
    return result


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------


def settle_interruption(
    db: Session, *, plan_version: str, interruption_id: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    interruption = _require_interruption(db, plan_version, interruption_id)
    if interruption.status == INTERRUPTION_SETTLED:
        raise CompensationConflictError("interruption is already settled")
    impacts = repo.list_impacts(db, interruption.id)
    retained_by_student: dict[str, int] = {}
    for impact in impacts:
        retained_by_student[impact.student_id] = (
            retained_by_student.get(impact.student_id, 0)
            + impact.already_completed_seconds
        )

    plans = repo.list_compensation_plans(
        db, plan_version=plan_version, interruption_pk=interruption.id
    )
    serialized: list[dict[str, Any]] = []
    total_gap = 0
    total_completed = 0
    windows = repo.list_availabilities(db, plan_version)
    for plan_row in plans:
        slots = repo.list_compensation_slots(db, plan_row.id)
        # 结算时仍未完成（含未开始与已开始但未登记完成）的时段一律不再产生
        # 学时，标记为 RELEASED 并按方案-窗口归还仍占用的名额。
        pending = [s for s in slots if s.status == SLOT_SCHEDULED]
        kept_window_ids: set[int] = set()
        for s in slots:
            if s.status == SLOT_COMPLETED:
                w = _window_for(
                    windows,
                    s.alternative_id,
                    _db_utc(s.start_at),
                    _db_utc(s.end_at),
                )
                if w is not None:
                    kept_window_ids.add(w.id)
        freed_window_ids: set[int] = set()
        for s in pending:
            repo.update_compensation_slot(
                db, s.id, status=SLOT_RELEASED, credited_seconds=0
            )
            w = _window_for(
                windows,
                s.alternative_id,
                _db_utc(s.start_at),
                _db_utc(s.end_at),
            )
            if (
                w is not None
                and w.id not in kept_window_ids
                and w.id not in freed_window_ids
            ):
                repo.release_availability(db, w.id)
                freed_window_ids.add(w.id)
        slots = repo.list_compensation_slots(db, plan_row.id)
        if plan_row.status == STATUS_CONFIRMED:
            completed = sum(
                s.credited_seconds
                for s in slots
                if s.status == SLOT_COMPLETED
            )
            source_credits: list[dict[str, Any]] = [
                {
                    "source": "original_activity",
                    "student_id": plan_row.student_id,
                    "seconds": retained_by_student.get(plan_row.student_id, 0),
                }
            ]
            for slot in slots:
                if slot.status == SLOT_COMPLETED:
                    source_credits.append(
                        {
                            "source": "alternative",
                            "alternative_id": slot.alternative_id,
                            "slot_id": slot.id,
                            "seconds": slot.credited_seconds,
                        }
                    )
            repo.update_compensation_plan(
                db,
                plan_row.id,
                status=STATUS_SETTLED,
                completed_seconds=completed,
                settled_seconds=completed,
                settled_at=datetime.now(timezone.utc),
                source_credits=source_credits,
            )
            total_completed += completed
        elif plan_row.status == STATUS_PROPOSED:
            # 未确认的方案不产生任何补偿学时。
            repo.update_compensation_plan(
                db,
                plan_row.id,
                status=STATUS_SETTLED,
                settled_at=datetime.now(timezone.utc),
                source_credits=[
                    {
                        "source": "original_activity",
                        "student_id": plan_row.student_id,
                        "seconds": retained_by_student.get(plan_row.student_id, 0),
                    }
                ],
            )
        total_gap += plan_row.gap_seconds
        db.refresh(plan_row)
        serialized.append(serialize_plan(db, plan_version, interruption, plan_row))

    repo.mark_interruption_status(db, interruption.id, INTERRUPTION_SETTLED)
    retained_total = sum(retained_by_student.values())
    return {
        "interruption_id": interruption.interruption_id,
        "plan_version": plan_version,
        "status": INTERRUPTION_SETTLED,
        "plans": serialized,
        "total_gap_seconds": total_gap,
        "total_completed_seconds": total_completed,
        "total_credited_seconds": total_completed + retained_total,
        "retained_source_seconds": retained_total,
        "released_slot_count": sum(
            1
            for plan_row in plans
            for s in repo.list_compensation_slots(db, plan_row.id)
            if s.status == SLOT_RELEASED
        ),
        "released_seconds": sum(
            s.scheduled_seconds
            for plan_row in plans
            for s in repo.list_compensation_slots(db, plan_row.id)
            if s.status == SLOT_RELEASED
        ),
    }


def list_plans(
    db: Session,
    *,
    plan_version: str,
    interruption_id: str | None = None,
    student_id: str | None = None,
) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    interruption_pk: int | None = None
    interruption: Interruption | None = None
    if interruption_id is not None:
        interruption = _require_interruption(db, plan_version, interruption_id)
        interruption_pk = interruption.id
    rows = repo.list_compensation_plans(
        db,
        plan_version=plan_version,
        interruption_pk=interruption_pk,
        student_id=student_id,
    )
    interruption_cache = {
        i.id: i for i in repo.list_interruptions(db, plan_version)
    }
    return [
        serialize_plan(
            db,
            plan_version,
            interruption_cache[p.interruption_pk],
            p,
        )
        for p in rows
    ]


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def serialize_impact(impact: InterruptionImpact) -> dict[str, Any]:
    gap_seconds = max(impact.lost_seconds - impact.already_completed_seconds, 0)
    return {
        "student_id": impact.student_id,
        "activity_id": impact.activity_id,
        "activity_type": impact.activity_type,
        "required_skill_codes": list(impact.required_skill_codes),
        "lost_start": _db_utc(impact.lost_start) if impact.lost_start else None,
        "lost_end": _db_utc(impact.lost_end) if impact.lost_end else None,
        "lost_seconds": impact.lost_seconds,
        "gap_seconds": gap_seconds,
        "skill_gap_seconds": dict(impact.skill_gap_seconds),
        "already_completed_seconds": impact.already_completed_seconds,
    }


def serialize_interruption(
    interruption: Interruption, impacts: list[InterruptionImpact]
) -> dict[str, Any]:
    return {
        "interruption_id": interruption.interruption_id,
        "plan_version": interruption.plan_version,
        "reason": interruption.reason,
        "interrupt_start": _db_utc(interruption.interrupt_start),
        "interrupt_end": _db_utc(interruption.interrupt_end),
        "status": interruption.status,
        "impact_count": len(impacts),
        "impacts": [serialize_impact(i) for i in impacts],
    }


def serialize_plan(
    db: Session,
    plan_version: str,
    interruption: Interruption,
    plan_row: CompensationPlan,
) -> dict[str, Any]:
    slots = repo.list_compensation_slots(db, plan_row.id)
    credited = sum(s.credited_seconds for s in slots if s.status != SLOT_RELEASED)
    return {
        "plan_code": plan_row.plan_code,
        "plan_version": plan_version,
        "interruption_id": interruption.interruption_id,
        "student_id": plan_row.student_id,
        "status": plan_row.status,
        "gap_seconds": plan_row.gap_seconds,
        "gap_by_skill": dict(plan_row.gap_by_skill),
        "completed_seconds": plan_row.completed_seconds,
        "settled_seconds": plan_row.settled_seconds,
        "locked_version": plan_row.locked_version,
        "fully_covered": credited >= plan_row.gap_seconds,
        "slots": [
            {
                "slot_id": s.id,
                "alternative_id": s.alternative_id,
                "title": s.title,
                "skill_code": s.skill_code,
                "start_at": _db_utc(s.start_at),
                "end_at": _db_utc(s.end_at),
                "scheduled_seconds": s.scheduled_seconds,
                "credited_seconds": s.credited_seconds if s.status != SLOT_RELEASED else 0,
                "weight": s.weight,
                "status": s.status,
            }
            for s in slots
        ],
        "source_credits": list(plan_row.source_credits or []),
        "confirmed_at": _db_utc(plan_row.confirmed_at) if plan_row.confirmed_at else None,
        "settled_at": _db_utc(plan_row.settled_at) if plan_row.settled_at else None,
    }
