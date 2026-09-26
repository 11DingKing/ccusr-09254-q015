"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import PlanNotFoundError
from . import services
from .domain import DomainError
from .schemas import (
    AdjustIn,
    CompensationPlanOut,
    ConfirmIn,
    GenerateIn,
    GenerateResult,
    ImpactBatchIn,
    ImpactImportResult,
    ImpactListOut,
    InterruptionIn,
    InterruptionOut,
    InterruptionRegisterResult,
    PlanListOut,
    ResumeIn,
    ResumeResult,
    SettlementOut,
    SettlementResult,
)

router = APIRouter(prefix="/api", tags=["interruption"])


def _raise_mapped(exc: Exception) -> None:
    if isinstance(exc, (services.InterruptionNotFoundError, services.CompensationPlanNotFoundError, PlanNotFoundError)):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, services.ForbiddenStudentError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, (services.InvalidStateError, services.ConfirmConflictError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (services.DomainValidationError, DomainError)):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.post(
    "/interruptions",
    response_model=InterruptionRegisterResult,
    status_code=status.HTTP_201_CREATED,
)
def register_interruption(body: InterruptionIn, db: Session = Depends(get_db)) -> Any:
    """中断登记：记录自然灾害或企业停产导致的实训中断事件。"""
    try:
        interruption, created = services.register_interruption(
            db,
            interruption_id=body.interruption_id,
            plan_version=body.plan_version,
            kind=body.kind,
            occurred_at=body.occurred_at,
            note=body.note,
        )
        return {"created": created, "interruption": interruption}
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.get("/interruptions/{interruption_id}", response_model=InterruptionOut)
def read_interruption(interruption_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.get_interruption_view(db, interruption_id)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post(
    "/interruptions/{interruption_id}/impacts",
    response_model=ImpactImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_impacts(
    interruption_id: str, body: ImpactBatchIn, db: Session = Depends(get_db)
) -> Any:
    """影响名单登记：批量导入受影响学生及其能力要求与已完成部分。"""
    try:
        return services.register_impacts(
            db,
            interruption_id,
            [impact.model_dump() for impact in body.impacts],
        )
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.get("/interruptions/{interruption_id}/impacts", response_model=ImpactListOut)
def list_impacts(interruption_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return {"impacts": services.list_impact_views(db, interruption_id)}
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post(
    "/interruptions/{interruption_id}/plans/generate",
    response_model=GenerateResult,
    status_code=status.HTTP_201_CREATED,
)
def generate_plans(
    interruption_id: str, body: GenerateIn, db: Session = Depends(get_db)
) -> Any:
    """方案生成：按能力要求与可用时段为受影响学生生成补偿缺口方案。"""
    try:
        return services.generate_plans(
            db,
            interruption_id,
            slots=[slot.model_dump() for slot in body.slots],
            student_ids=body.student_ids,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.get("/interruptions/{interruption_id}/plans", response_model=PlanListOut)
def list_plans(interruption_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return {"plans": services.list_plan_views(db, interruption_id)}
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post("/interruptions/{interruption_id}/resume", response_model=ResumeResult)
def resume_interruption(
    interruption_id: str, body: ResumeIn, db: Session = Depends(get_db)
) -> Any:
    """原活动恢复：只释放尚未开始的补偿，已完成部分保留来源。"""
    try:
        return services.resume_interruption(
            db, interruption_id, resumed_at=body.resumed_at
        )
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.get("/compensation-plans/{plan_id}", response_model=CompensationPlanOut)
def read_plan(plan_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.get_plan_view(db, plan_id)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post("/compensation-plans/{plan_id}/confirm", response_model=CompensationPlanOut)
def confirm_plan(plan_id: str, body: ConfirmIn, db: Session = Depends(get_db)) -> Any:
    """学生确认：确认后替代方案锁定。"""
    try:
        return services.confirm_plan(db, plan_id, student_id=body.student_id)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post("/compensation-plans/{plan_id}/adjust", response_model=CompensationPlanOut)
def adjust_plan(plan_id: str, body: AdjustIn, db: Session = Depends(get_db)) -> Any:
    """方案调整：调整补偿项时段或折算比例，保留审计轨迹。"""
    try:
        return services.adjust_plan(
            db,
            plan_id,
            actor_id=body.actor_id,
            reason=body.reason,
            item_updates=[u.model_dump() for u in body.item_updates],
            expected_version=body.expected_version,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post(
    "/compensation-plans/{plan_id}/settle",
    response_model=SettlementResult,
    status_code=status.HTTP_201_CREATED,
)
def settle_plan(plan_id: str, db: Session = Depends(get_db)) -> Any:
    """结算：汇总原活动已完成部分与保留的补偿部分，避免重复计入。"""
    try:
        settlement, created = services.settle_plan(db, plan_id)
        return {"created": created, "settlement": settlement}
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.get("/compensation-plans/{plan_id}/settlement", response_model=SettlementOut)
def read_settlement(plan_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.get_settlement_view(db, plan_id)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)
