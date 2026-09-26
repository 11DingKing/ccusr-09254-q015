"""服务端业务模块。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..repository import get_plan as get_plan_row
from ..services import PlanNotFoundError
from . import repository as repo
from .domain import (
    ItemState,
    ItemView,
    PlanState,
    SlotPool,
    allocate_gaps,
    aware_from_storage,
    compute_gaps,
    compute_settlement,
    credit_for,
    ensure_aware,
    iso_utc,
    make_audit_entry,
    split_item_at_resume,
)


class InterruptionNotFoundError(Exception):
    pass


class CompensationPlanNotFoundError(Exception):
    pass


class InvalidStateError(Exception):
    pass


class ConfirmConflictError(Exception):
    pass


class ForbiddenStudentError(Exception):
    pass


class DomainValidationError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _interruption_view(row) -> dict[str, Any]:
    return {
        "interruption_id": row.interruption_id,
        "plan_version": row.plan_version,
        "kind": row.kind,
        "status": row.status,
        "occurred_at": iso_utc(aware_from_storage(row.occurred_at)),
        "resumed_at": (
            iso_utc(aware_from_storage(row.resumed_at))
            if row.resumed_at is not None
            else None
        ),
        "note": row.note,
        "version": row.version,
    }


def _impact_view(row) -> dict[str, Any]:
    capabilities = {k: int(v) for k, v in dict(row.capabilities).items()}
    completed = {k: int(v) for k, v in dict(row.completed).items()}
    return {
        "student_id": row.student_id,
        "activity_id": row.activity_id,
        "capabilities": capabilities,
        "completed": completed,
        "gaps": compute_gaps(capabilities, completed),
    }


def _item_view(row) -> dict[str, Any]:
    raw = int(row.raw_seconds)
    retained = int(row.retained_seconds)
    return {
        "item_id": row.item_id,
        "capability": row.capability,
        "slot_id": row.slot_id,
        "source_plan_version": row.source_plan_version,
        "start_at": iso_utc(aware_from_storage(row.start_at)),
        "end_at": iso_utc(aware_from_storage(row.end_at)),
        "raw_seconds": raw,
        "retained_seconds": retained,
        "released_seconds": max(0, raw - retained),
        "conversion_ratio": float(row.conversion_ratio),
        "credited_seconds": int(row.credited_seconds),
        "status": row.status,
    }


def _plan_view(db: Session, row) -> dict[str, Any]:
    items = [_item_view(item) for item in repo.list_items(db, row.plan_id)]
    return {
        "plan_id": row.plan_id,
        "interruption_id": row.interruption_id,
        "student_id": row.student_id,
        "activity_id": row.activity_id,
        "status": row.status,
        "version": int(row.version),
        "confirmed_by": row.confirmed_by,
        "gaps": {k: int(v) for k, v in dict(row.gaps).items()},
        "unfilled": {k: int(v) for k, v in dict(row.unfilled).items()},
        "items": items,
        "audit": list(row.audit),
    }


def _require_interruption(db: Session, interruption_id: str):
    row = repo.get_interruption(db, interruption_id)
    if row is None:
        raise InterruptionNotFoundError(
            f"interruption '{interruption_id}' is not registered"
        )
    return row


def _require_plan(db: Session, plan_id: str):
    row = repo.get_plan(db, plan_id)
    if row is None:
        raise CompensationPlanNotFoundError(
            f"compensation plan '{plan_id}' does not exist"
        )
    return row


def register_interruption(
    db: Session,
    *,
    interruption_id: str,
    plan_version: str,
    kind: str,
    occurred_at: datetime,
    note: str = "",
) -> tuple[dict[str, Any], bool]:
    """中断登记：幂等，重复登记返回已存在的记录。"""
    if get_plan_row(db, plan_version) is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    occurred = ensure_aware(occurred_at)
    existing = repo.get_interruption(db, interruption_id)
    if existing is not None:
        return _interruption_view(existing), False
    row = repo.insert_interruption(
        db,
        interruption_id=interruption_id,
        plan_version=plan_version,
        kind=kind,
        occurred_at=occurred,
        note=note,
    )
    if row is None:
        existing = repo.get_interruption(db, interruption_id)
        assert existing is not None
        return _interruption_view(existing), False
    return _interruption_view(row), True


def get_interruption_view(db: Session, interruption_id: str) -> dict[str, Any]:
    return _interruption_view(_require_interruption(db, interruption_id))


def register_impacts(
    db: Session, interruption_id: str, impacts: list[dict[str, Any]]
) -> dict[str, Any]:
    """影响名单登记：批量导入，重复学生记为 duplicates。"""
    interruption = _require_interruption(db, interruption_id)
    if interruption.status != "open":
        raise InvalidStateError("中断已恢复，无法继续登记影响名单")
    for impact in impacts:
        for mapping, label in (
            (impact["capabilities"], "capabilities"),
            (impact["completed"], "completed"),
        ):
            for key, value in mapping.items():
                if not str(key).strip():
                    raise DomainValidationError(f"{label} 的能力项名称不能为空")
                if int(value) < 0:
                    raise DomainValidationError(f"{label} 的秒数不能为负")
    accepted, duplicates = repo.insert_impacts(
        db, interruption_id=interruption_id, impacts=impacts
    )
    return {"accepted": len(accepted), "duplicates": duplicates}


def list_impact_views(db: Session, interruption_id: str) -> list[dict[str, Any]]:
    _require_interruption(db, interruption_id)
    return [_impact_view(row) for row in repo.list_impacts(db, interruption_id)]


def generate_plans(
    db: Session,
    interruption_id: str,
    *,
    slots: list[dict[str, Any]],
    student_ids: list[str] | None = None,
) -> dict[str, Any]:
    """方案生成：按能力要求与可用时段为受影响学生切出补偿项。"""
    interruption = _require_interruption(db, interruption_id)
    if interruption.status != "open":
        raise InvalidStateError("中断已恢复，无法生成补偿方案")

    slot_ids = [s["slot_id"] for s in slots]
    if len(slot_ids) != len(set(slot_ids)):
        raise DomainValidationError("可用时段的 slot_id 不能重复")
    pools = [
        SlotPool.from_spec(
            slot_id=s["slot_id"],
            capability=s["capability"],
            source_plan_version=s.get("source_plan_version"),
            conversion_ratio=float(s.get("conversion_ratio", 1.0)),
            start_at=s["start_at"],
            end_at=s["end_at"],
        )
        for s in slots
    ]

    impacts = repo.list_impacts(db, interruption_id)
    if student_ids is not None:
        wanted = set(student_ids)
        known = {impact.student_id for impact in impacts}
        missing = sorted(wanted - known)
        if missing:
            raise DomainValidationError(
                f"以下学生不在影响名单中: {', '.join(missing)}"
            )
        impacts = [impact for impact in impacts if impact.student_id in wanted]

    now = _now()
    created: list[dict[str, Any]] = []
    existing: list[str] = []
    for impact in impacts:
        plan_id = f"{interruption_id}:{impact.student_id}"
        if repo.get_plan(db, plan_id) is not None:
            existing.append(plan_id)
            continue
        capabilities = {k: int(v) for k, v in dict(impact.capabilities).items()}
        completed = {k: int(v) for k, v in dict(impact.completed).items()}
        gaps = compute_gaps(capabilities, completed)
        specs, unfilled = allocate_gaps(gaps, pools)
        audit_entry = make_audit_entry(
            identifier=plan_id,
            sequence=1,
            version=1,
            action="generate",
            actor_id="system",
            occurred_at=now,
            before="-",
            after=PlanState.OFFERED.value,
            reason="中断补偿方案生成",
        )
        row = repo.insert_plan(
            db,
            plan_id=plan_id,
            interruption_id=interruption_id,
            student_id=impact.student_id,
            activity_id=impact.activity_id,
            gaps=gaps,
            unfilled=unfilled,
            audit=[audit_entry],
        )
        if row is None:
            existing.append(plan_id)
            continue
        repo.insert_items(
            db,
            [
                {
                    "item_id": f"{plan_id}#{seq:02d}",
                    "plan_id": plan_id,
                    "capability": spec.capability,
                    "slot_id": spec.slot_id,
                    "source_plan_version": spec.source_plan_version,
                    "start_at": spec.start_at,
                    "end_at": spec.end_at,
                    "raw_seconds": spec.raw_seconds,
                    "retained_seconds": spec.raw_seconds,
                    "conversion_ratio": spec.conversion_ratio,
                    "credited_seconds": spec.credited_seconds,
                    "status": ItemState.SCHEDULED.value,
                }
                for seq, spec in enumerate(specs, start=1)
            ],
        )
        created.append(_plan_view(db, row))
    db.commit()
    return {"plans": created, "existing": existing}


def list_plan_views(db: Session, interruption_id: str) -> list[dict[str, Any]]:
    _require_interruption(db, interruption_id)
    return [_plan_view(db, row) for row in repo.list_plans(db, interruption_id)]


def get_plan_view(db: Session, plan_id: str) -> dict[str, Any]:
    return _plan_view(db, _require_plan(db, plan_id))


def confirm_plan(
    db: Session, plan_id: str, *, student_id: str
) -> dict[str, Any]:
    """学生确认：原子条件更新，并发下只有第一个确认生效，其余幂等返回。"""
    plan = _require_plan(db, plan_id)
    if plan.student_id != student_id:
        raise ForbiddenStudentError("只能确认本人的补偿方案")
    if plan.status == PlanState.CONFIRMED.value:
        return _plan_view(db, plan)
    if plan.status != PlanState.OFFERED.value:
        raise InvalidStateError(f"方案状态为 {plan.status}，无法确认")

    now = _now()
    entry = make_audit_entry(
        identifier=plan_id,
        sequence=len(plan.audit) + 1,
        version=int(plan.version) + 1,
        action="confirm",
        actor_id=student_id,
        occurred_at=now,
        before=PlanState.OFFERED.value,
        after=PlanState.CONFIRMED.value,
        reason="学生确认锁定替代方案",
    )
    updated = repo.confirm_plan_atomic(
        db,
        plan_id=plan_id,
        student_id=student_id,
        expected_version=int(plan.version),
        audit=list(plan.audit) + [entry],
        now=now,
    )
    if not updated:
        db.rollback()
        plan = _require_plan(db, plan_id)
        if (
            plan.status == PlanState.CONFIRMED.value
            and plan.confirmed_by == student_id
        ):
            return _plan_view(db, plan)
        raise ConfirmConflictError("方案已被其他操作变更，请刷新后重试")
    db.commit()
    return _plan_view(db, _require_plan(db, plan_id))


def adjust_plan(
    db: Session,
    plan_id: str,
    *,
    actor_id: str,
    reason: str,
    item_updates: list[dict[str, Any]],
    expected_version: int | None = None,
) -> dict[str, Any]:
    """方案调整：仅 offered/confirmed 状态可调整，保留审计轨迹。"""
    plan = _require_plan(db, plan_id)
    if plan.status not in (PlanState.OFFERED.value, PlanState.CONFIRMED.value):
        raise InvalidStateError(f"方案状态为 {plan.status}，无法调整")
    if expected_version is not None and expected_version != int(plan.version):
        raise ConfirmConflictError(
            f"方案版本已变化（当前 {plan.version}），请刷新后重试"
        )

    items = {item.item_id: item for item in repo.list_items(db, plan_id)}
    gaps = {k: int(v) for k, v in dict(plan.gaps).items()}
    before_summary: list[str] = []
    after_summary: list[str] = []
    now = _now()

    for update_spec in item_updates:
        item = items.get(update_spec["item_id"])
        if item is None:
            raise DomainValidationError(
                f"补偿项 {update_spec['item_id']} 不属于方案 {plan_id}"
            )
        if item.status != ItemState.SCHEDULED.value:
            raise InvalidStateError(
                f"补偿项 {item.item_id} 已{item.status}，不可调整"
            )
        before_summary.append(
            f"{item.item_id}={item.raw_seconds}s@{item.conversion_ratio}"
        )
        if update_spec.get("start_at") is not None:
            item.start_at = ensure_aware(update_spec["start_at"])
        if update_spec.get("end_at") is not None:
            item.end_at = ensure_aware(update_spec["end_at"])
        start = aware_from_storage(item.start_at)
        end = aware_from_storage(item.end_at)
        if end <= start:
            raise DomainValidationError("补偿项的结束时间必须晚于开始时间")
        item.start_at = start
        item.end_at = end
        if update_spec.get("conversion_ratio") is not None:
            ratio = float(update_spec["conversion_ratio"])
            if ratio <= 0:
                raise DomainValidationError("折算比例必须大于零")
            item.conversion_ratio = ratio
        item.raw_seconds = int((end - start).total_seconds())
        item.retained_seconds = item.raw_seconds
        gap = gaps.get(item.capability, 0)
        others = sum(
            int(other.credited_seconds)
            for other in items.values()
            if other.capability == item.capability
            and other.item_id != item.item_id
            and other.status != ItemState.RELEASED.value
        )
        item.credited_seconds = credit_for(
            item.raw_seconds, float(item.conversion_ratio), cap=max(gap - others, 0)
        )
        item.updated_at = now
        after_summary.append(
            f"{item.item_id}={item.raw_seconds}s@{item.conversion_ratio}"
        )

    entry = make_audit_entry(
        identifier=plan_id,
        sequence=len(plan.audit) + 1,
        version=int(plan.version) + 1,
        action="adjust",
        actor_id=actor_id,
        occurred_at=now,
        before=";".join(before_summary) or "-",
        after=";".join(after_summary) or "-",
        reason=reason,
    )
    plan.audit = list(plan.audit) + [entry]
    plan.version = int(plan.version) + 1
    plan.updated_at = now
    db.commit()
    return _plan_view(db, plan)


def resume_interruption(
    db: Session, interruption_id: str, *, resumed_at: datetime
) -> dict[str, Any]:
    """原活动恢复：只释放尚未开始的补偿，已完成部分保留来源。"""
    interruption = _require_interruption(db, interruption_id)
    if interruption.status != "open":
        raise InvalidStateError("中断已处于恢复状态，不能重复恢复")
    moment = ensure_aware(resumed_at)
    occurred = aware_from_storage(interruption.occurred_at)
    if moment < occurred:
        raise DomainValidationError("恢复时间不能早于中断发生时间")

    now = _now()
    interruption.status = "resumed"
    interruption.resumed_at = moment
    interruption.version = int(interruption.version) + 1
    interruption.updated_at = now

    plans_affected = 0
    items_retained = 0
    items_released = 0
    retained_seconds = 0
    released_seconds = 0

    for plan in repo.list_plans(db, interruption_id):
        if plan.status == PlanState.SETTLED.value:
            continue
        retained_count = 0
        released_count = 0
        for item in repo.scheduled_items(db, plan.plan_id):
            new_status, kept = split_item_at_resume(
                start_at=aware_from_storage(item.start_at),
                end_at=aware_from_storage(item.end_at),
                raw_seconds=int(item.raw_seconds),
                resumed_at=moment,
            )
            item.status = new_status.value
            item.retained_seconds = kept
            item.updated_at = now
            if new_status == ItemState.RELEASED:
                released_count += 1
                released_seconds += int(item.raw_seconds)
            else:
                retained_count += 1
                retained_seconds += kept
                released_seconds += int(item.raw_seconds) - kept
        if retained_count or released_count:
            plans_affected += 1
            items_retained += retained_count
            items_released += released_count
            entry = make_audit_entry(
                identifier=plan.plan_id,
                sequence=len(plan.audit) + 1,
                version=int(plan.version) + 1,
                action="resume_applied",
                actor_id="system",
                occurred_at=now,
                before="scheduled",
                after=f"retained:{retained_count},released:{released_count}",
                reason="原活动恢复，释放尚未开始的补偿",
            )
            plan.audit = list(plan.audit) + [entry]
            plan.version = int(plan.version) + 1
            plan.updated_at = now

    db.commit()
    return {
        "interruption_id": interruption_id,
        "status": interruption.status,
        "resumed_at": iso_utc(moment),
        "plans_affected": plans_affected,
        "items_retained": items_retained,
        "items_released": items_released,
        "retained_seconds": retained_seconds,
        "released_seconds": released_seconds,
    }


def settle_plan(db: Session, plan_id: str) -> tuple[dict[str, Any], bool]:
    """结算：汇总原活动已完成与保留的补偿部分，结果持久化且幂等。"""
    plan = _require_plan(db, plan_id)
    existing = repo.get_settlement(db, plan_id)
    if existing is not None:
        return dict(existing.result), False
    if plan.status == PlanState.OFFERED.value:
        raise InvalidStateError("学生确认锁定后才能结算")
    if plan.status != PlanState.CONFIRMED.value:
        raise InvalidStateError(f"方案状态为 {plan.status}，无法结算")

    impact = repo.get_impact(db, plan.interruption_id, plan.student_id)
    if impact is None:
        raise InvalidStateError("缺少影响名单记录，无法结算")

    items = [
        ItemView(
            item_id=item.item_id,
            capability=item.capability,
            slot_id=item.slot_id,
            source_plan_version=item.source_plan_version,
            status=ItemState(item.status),
            raw_seconds=int(item.raw_seconds),
            retained_seconds=int(item.retained_seconds),
            conversion_ratio=float(item.conversion_ratio),
            credited_seconds=int(item.credited_seconds),
        )
        for item in repo.list_items(db, plan_id)
    ]
    now = _now()
    result = compute_settlement(
        plan_id=plan_id,
        interruption_id=plan.interruption_id,
        student_id=plan.student_id,
        activity_id=impact.activity_id,
        capabilities={k: int(v) for k, v in dict(impact.capabilities).items()},
        completed={k: int(v) for k, v in dict(impact.completed).items()},
        items=items,
        settled_at=now,
    )
    row = repo.insert_settlement(db, plan_id=plan_id, result=result)
    if row is None:
        db.rollback()
        existing = repo.get_settlement(db, plan_id)
        assert existing is not None
        return dict(existing.result), False

    entry = make_audit_entry(
        identifier=plan_id,
        sequence=len(plan.audit) + 1,
        version=int(plan.version) + 1,
        action="settle",
        actor_id="system",
        occurred_at=now,
        before=PlanState.CONFIRMED.value,
        after=PlanState.SETTLED.value,
        reason="补偿结算",
    )
    settled = repo.settle_plan_atomic(
        db,
        plan_id=plan_id,
        expected_version=int(plan.version),
        audit=list(plan.audit) + [entry],
        now=now,
    )
    if not settled:
        db.rollback()
        raise ConfirmConflictError("方案已被其他操作变更，请重试结算")
    db.commit()
    return result, True


def get_settlement_view(db: Session, plan_id: str) -> dict[str, Any]:
    _require_plan(db, plan_id)
    row = repo.get_settlement(db, plan_id)
    if row is None:
        raise InvalidStateError(f"方案 {plan_id} 尚未结算")
    return dict(row.result)
