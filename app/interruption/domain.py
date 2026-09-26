"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from math import ceil
from typing import Any, Mapping

_EPSILON = 1e-9


class InterruptionState(StrEnum):
    OPEN = "open"
    RESUMED = "resumed"


class PlanState(StrEnum):
    OFFERED = "offered"
    CONFIRMED = "confirmed"
    SETTLED = "settled"


class ItemState(StrEnum):
    SCHEDULED = "scheduled"
    RETAINED = "retained"
    RELEASED = "released"


ALLOWED_PLAN_TRANSITIONS: Mapping[PlanState, frozenset[PlanState]] = {
    PlanState.OFFERED: frozenset({PlanState.CONFIRMED}),
    PlanState.CONFIRMED: frozenset({PlanState.SETTLED}),
    PlanState.SETTLED: frozenset(),
}


class DomainError(ValueError):
    """封装领域状态与业务约束。"""


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise DomainError("时间必须包含时区")
    return value.astimezone(UTC)


def aware_from_storage(value: datetime) -> datetime:
    """SQLite 不保存时区，读取时按 UTC 还原。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    return ensure_aware(value).isoformat().replace("+00:00", "Z")


def compute_gaps(
    capabilities: Mapping[str, int], completed: Mapping[str, int]
) -> dict[str, int]:
    """缺口 = 能力要求 - 原活动已完成部分，不为负，避免重复计入。"""
    gaps: dict[str, int] = {}
    for capability, required in capabilities.items():
        gap = int(required) - int(completed.get(capability, 0))
        if gap > 0:
            gaps[capability] = gap
    return gaps


def credit_for(raw_seconds: int, ratio: float, cap: int | None = None) -> int:
    """按折算比例换算秒数，可选上限（不超过剩余缺口）。"""
    credited = int(raw_seconds * ratio + _EPSILON)
    if cap is not None:
        credited = min(credited, cap)
    return max(0, credited)


@dataclass
class SlotPool:
    """可用时段池：同一时段的剩余秒数在多名学生之间按顺序消耗。"""

    slot_id: str
    capability: str
    source_plan_version: str | None
    conversion_ratio: float
    start_at: datetime
    end_at: datetime
    cursor: datetime

    @classmethod
    def from_spec(
        cls,
        *,
        slot_id: str,
        capability: str,
        source_plan_version: str | None,
        conversion_ratio: float,
        start_at: datetime,
        end_at: datetime,
    ) -> "SlotPool":
        start = ensure_aware(start_at)
        end = ensure_aware(end_at)
        if end <= start:
            raise DomainError("可用时段的结束时间必须晚于开始时间")
        if conversion_ratio <= 0:
            raise DomainError("折算比例必须大于零")
        return cls(
            slot_id=slot_id,
            capability=capability,
            source_plan_version=source_plan_version,
            conversion_ratio=conversion_ratio,
            start_at=start,
            end_at=end,
            cursor=start,
        )

    @property
    def remaining_seconds(self) -> int:
        return max(0, int((self.end_at - self.cursor).total_seconds()))


@dataclass(frozen=True)
class ItemSpec:
    capability: str
    slot_id: str
    source_plan_version: str | None
    start_at: datetime
    end_at: datetime
    raw_seconds: int
    conversion_ratio: float
    credited_seconds: int


def allocate_gaps(
    gaps: Mapping[str, int], pools: list[SlotPool]
) -> tuple[list[ItemSpec], dict[str, int]]:
    """按能力缺口从可用时段中确定性地切出补偿项，返回 (补偿项, 未覆盖缺口)。"""
    items: list[ItemSpec] = []
    unfilled: dict[str, int] = {}
    for capability in sorted(gaps):
        remaining = gaps[capability]
        if remaining <= 0:
            continue
        candidates = sorted(
            (p for p in pools if p.capability == capability),
            key=lambda p: (p.start_at, p.slot_id),
        )
        for pool in candidates:
            if remaining <= 0:
                break
            available = pool.remaining_seconds
            if available <= 0:
                continue
            need = ceil(remaining / pool.conversion_ratio - _EPSILON)
            take = min(need, available)
            credited = credit_for(take, pool.conversion_ratio, cap=remaining)
            if take <= 0 or credited <= 0:
                continue
            start = pool.cursor
            end = start + timedelta(seconds=take)
            items.append(
                ItemSpec(
                    capability=capability,
                    slot_id=pool.slot_id,
                    source_plan_version=pool.source_plan_version,
                    start_at=start,
                    end_at=end,
                    raw_seconds=take,
                    conversion_ratio=pool.conversion_ratio,
                    credited_seconds=credited,
                )
            )
            pool.cursor = end
            remaining -= credited
        if remaining > 0:
            unfilled[capability] = remaining
    return items, unfilled


def split_item_at_resume(
    *,
    start_at: datetime,
    end_at: datetime,
    raw_seconds: int,
    resumed_at: datetime,
) -> tuple[ItemState, int]:
    """原活动恢复时拆分补偿项：尚未开始的部分释放，已完成部分保留。

    返回 (新状态, 保留的原始秒数)；释放秒数 = raw_seconds - 保留秒数。
    """
    moment = ensure_aware(resumed_at)
    start = ensure_aware(start_at)
    end = ensure_aware(end_at)
    if start >= moment:
        return ItemState.RELEASED, 0
    if end <= moment:
        return ItemState.RETAINED, raw_seconds
    retained = int((moment - start).total_seconds())
    return ItemState.RETAINED, max(0, min(retained, raw_seconds))


def retained_credit(
    status: ItemState,
    retained_seconds: int,
    ratio: float,
    credited_seconds: int,
) -> int:
    """结算时补偿项实际计入的秒数：已释放为零，否则按保留部分折算。"""
    if status == ItemState.RELEASED or retained_seconds <= 0:
        return 0
    return credit_for(retained_seconds, ratio, cap=credited_seconds)


@dataclass(frozen=True)
class ItemView:
    item_id: str
    capability: str
    slot_id: str
    source_plan_version: str | None
    status: ItemState
    raw_seconds: int
    retained_seconds: int
    conversion_ratio: float
    credited_seconds: int


def compute_settlement(
    *,
    plan_id: str,
    interruption_id: str,
    student_id: str,
    activity_id: str,
    capabilities: Mapping[str, int],
    completed: Mapping[str, int],
    items: list[ItemView],
    settled_at: datetime,
) -> dict[str, Any]:
    """汇总原活动已完成部分与保留的补偿部分，保留各自的来源。"""
    capability_names = sorted(
        set(capabilities) | set(completed) | {i.capability for i in items}
    )
    per_capability: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    original_total = 0
    substitute_total = 0
    released_total = 0

    for name in capability_names:
        required = int(capabilities.get(name, 0))
        original = int(completed.get(name, 0))
        substitute = 0
        released = 0
        for item in items:
            if item.capability != name:
                continue
            if item.status == ItemState.RELEASED:
                released += item.credited_seconds
                continue
            kept = retained_credit(
                item.status,
                item.retained_seconds,
                item.conversion_ratio,
                item.credited_seconds,
            )
            substitute += kept
            released += item.credited_seconds - kept
        total = original + substitute
        per_capability[name] = {
            "required_seconds": required,
            "original_seconds": original,
            "substitute_seconds": substitute,
            "released_seconds": released,
            "total_seconds": total,
            "fulfilled": total >= required,
        }
        original_total += original
        substitute_total += substitute
        released_total += released

    if original_total > 0:
        sources.append(
            {
                "source": "original",
                "activity_id": activity_id,
                "seconds": original_total,
            }
        )
    for item in items:
        if item.status == ItemState.RELEASED:
            continue
        credited = retained_credit(
            item.status,
            item.retained_seconds,
            item.conversion_ratio,
            item.credited_seconds,
        )
        if credited <= 0:
            continue
        sources.append(
            {
                "source": "substitute",
                "item_id": item.item_id,
                "slot_id": item.slot_id,
                "source_plan_version": item.source_plan_version,
                "capability": item.capability,
                "seconds": credited,
            }
        )

    return {
        "plan_id": plan_id,
        "interruption_id": interruption_id,
        "student_id": student_id,
        "capabilities": per_capability,
        "sources": sources,
        "original_seconds": original_total,
        "substitute_seconds": substitute_total,
        "released_seconds": released_total,
        "total_seconds": original_total + substitute_total,
        "settled_at": iso_utc(settled_at),
    }


def make_audit_entry(
    *,
    identifier: str,
    sequence: int,
    version: int,
    action: str,
    actor_id: str,
    occurred_at: datetime,
    before: str,
    after: str,
    reason: str,
) -> dict[str, Any]:
    raw = f"{identifier}|{version}|{action}|{actor_id}|{reason}".encode("utf-8")
    return {
        "sequence": sequence,
        "action": action,
        "actor_id": actor_id,
        "occurred_at": iso_utc(occurred_at),
        "before": before,
        "after": after,
        "reason": reason,
        "fingerprint": sha256(raw).hexdigest(),
    }
