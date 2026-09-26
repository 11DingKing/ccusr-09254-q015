"""实训中断补偿相关 HTTP 路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services_interruption as si
from .db import get_db
from .schemas import (
    AlternativeCatalogIn,
    CompensationConfirmIn,
    CompensationGenerateIn,
    CompensationPlanOut,
    ImpactBatchIn,
    InterruptionIn,
    InterruptionOut,
    ResumeIn,
    SettleResultOut,
    SlotAdjustmentIn,
    SlotCompleteIn,
)

router = APIRouter(prefix="/api/plans/{plan_version}")

_NOT_FOUND = (
    si.PlanNotFoundError,
    si.InterruptionNotFoundError,
    si.CompensationNotFoundError,
    si.SlotNotFoundError,
)
_CONFLICT = (
    si.InterruptionConflictError,
    si.CompensationConflictError,
    si.CapacityExhaustedError,
)


def _raise(exc: Exception) -> None:
    if isinstance(exc, _NOT_FOUND):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, _CONFLICT):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, si.ValidationError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.post(
    "/interruptions",
    response_model=InterruptionOut,
    status_code=status.HTTP_201_CREATED,
)
def post_interruption(
    plan_version: str, body: InterruptionIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return si.register_interruption(
            db,
            plan_version=plan_version,
            interruption_id=body.interruption_id,
            reason=body.reason,
            interrupt_start=body.interrupt_start,
            interrupt_end=body.interrupt_end,
        )
    except Exception as exc:  # noqa: BLE001 - 统一映射为 HTTP 错误
        _raise(exc)


@router.post(
    "/interruptions/{interruption_id}/impacts",
    response_model=InterruptionOut,
    status_code=status.HTTP_201_CREATED,
)
def post_impacts(
    plan_version: str,
    interruption_id: str,
    body: ImpactBatchIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return si.register_impacts(
            db,
            plan_version=plan_version,
            interruption_id=interruption_id,
            entries=[e.model_dump() for e in body.impacts],
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/interruptions/{interruption_id}", response_model=InterruptionOut)
def read_interruption(
    plan_version: str, interruption_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return si.get_interruption_detail(db, plan_version, interruption_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.put("/catalog")
def put_catalog(
    plan_version: str, body: AlternativeCatalogIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return si.upsert_catalog(
            db,
            plan_version=plan_version,
            alternatives=[a.model_dump() for a in body.alternatives],
            availabilities=[w.model_dump() for w in body.availabilities],
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post(
    "/interruptions/{interruption_id}/compensations/generate",
    response_model=list[CompensationPlanOut],
    status_code=status.HTTP_201_CREATED,
)
def post_generate(
    plan_version: str,
    interruption_id: str,
    body: CompensationGenerateIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return si.generate_compensation(
            db,
            plan_version=plan_version,
            interruption_id=interruption_id,
            student_ids=body.student_ids or None,
            code_prefix=body.code_prefix,
            confirm=body.confirm,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/compensations", response_model=list[CompensationPlanOut])
def list_compensations(
    plan_version: str,
    interruption_id: str | None = None,
    student_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return si.list_plans(
            db,
            plan_version=plan_version,
            interruption_id=interruption_id,
            student_id=student_id,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/compensations/{plan_code}", response_model=CompensationPlanOut)
def read_compensation(
    plan_version: str, plan_code: str, db: Session = Depends(get_db)
) -> Any:
    try:
        rows = si.list_plans(db, plan_version=plan_version)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    for row in rows:
        if row["plan_code"] == plan_code:
            return row
    raise HTTPException(status_code=404, detail=f"compensation plan '{plan_code}' not found")


@router.post("/compensations/{plan_code}/confirm", response_model=CompensationPlanOut)
def post_confirm(
    plan_version: str,
    plan_code: str,
    body: CompensationConfirmIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return si.confirm_compensation(
            db,
            plan_version=plan_version,
            plan_code=plan_code,
            expected_version=body.expected_version,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch(
    "/compensations/{plan_code}/slots/{slot_id}", response_model=CompensationPlanOut
)
def patch_slot(
    plan_version: str,
    plan_code: str,
    slot_id: int,
    body: SlotAdjustmentIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return si.adjust_slot(
            db,
            plan_version=plan_version,
            plan_code=plan_code,
            slot_id=slot_id,
            start_at=body.start_at,
            end_at=body.end_at,
            credited_seconds=body.credited_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post(
    "/compensations/{plan_code}/slots/{slot_id}/complete",
    response_model=CompensationPlanOut,
)
def post_complete_slot(
    plan_version: str,
    plan_code: str,
    slot_id: int,
    body: SlotCompleteIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return si.complete_slot(
            db,
            plan_version=plan_version,
            plan_code=plan_code,
            slot_id=slot_id,
            credited_seconds=body.credited_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/interruptions/{interruption_id}/resume", response_model=InterruptionOut)
def post_resume(
    plan_version: str,
    interruption_id: str,
    body: ResumeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return si.resume_interruption(
            db,
            plan_version=plan_version,
            interruption_id=interruption_id,
            resume_at=body.resume_at,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post(
    "/interruptions/{interruption_id}/settle", response_model=SettleResultOut
)
def post_settle(
    plan_version: str,
    interruption_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return si.settle_interruption(
            db, plan_version=plan_version, interruption_id=interruption_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
