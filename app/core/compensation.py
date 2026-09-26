"""实训中断补偿的纯领域逻辑。

本模块不触碰数据库与 HTTP，只负责：
- 按中断窗口与原活动排期计算每个学生的能力（技能）缺口；
- 依据替代活动目录与可用时段，按能力要求与学生可用时段生成补偿排期；
- 按折算权重把替代学时折算回原活动学时。

所有时间均为时区感知的 UTC ``datetime``，保证与 replay/snapshot 口径一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil
from typing import Any, Iterable

from .clock import elapsed_seconds, merge_intervals, to_utc


@dataclass(frozen=True)
class LostSegment:
    """原活动被中断吞掉的一段排期。"""

    student_id: str
    activity_id: str
    activity_type: str
    skill_codes: tuple[str, ...]
    start_utc: datetime
    end_utc: datetime

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)


@dataclass(frozen=True)
class CompletedSegment:
    """原活动在中断前已经完成、不可重复计入的部分。"""

    student_id: str
    activity_id: str
    start_utc: datetime
    end_utc: datetime

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)


@dataclass
class SkillGap:
    student_id: str
    activity_id: str
    activity_type: str
    skill_codes: list[str]
    gap_seconds: int
    gap_by_skill: dict[str, int] = field(default_factory=dict)
    completed_seconds: int = 0
    lost_start: datetime | None = None
    lost_end: datetime | None = None


@dataclass(frozen=True)
class Alternative:
    alternative_id: str
    title: str
    skill_code: str
    weight: float = 1.0


@dataclass(frozen=True)
class AvailabilityWindow:
    alternative_id: str
    start_utc: datetime
    end_utc: datetime
    capacity: int = 1


@dataclass(frozen=True)
class BusyWindow:
    start_utc: datetime
    end_utc: datetime


@dataclass
class ScheduledSlot:
    alternative_id: str
    title: str
    skill_code: str
    start_utc: datetime
    end_utc: datetime
    scheduled_seconds: int
    credited_seconds: int
    weight: float


@dataclass
class CompensationProposal:
    student_id: str
    gap_seconds: int
    gap_by_skill: dict[str, int]
    slots: list[ScheduledSlot] = field(default_factory=list)

    @property
    def credited_seconds(self) -> int:
        return sum(s.credited_seconds for s in self.slots)

    @property
    def covered(self) -> bool:
        return self.credited_seconds >= self.gap_seconds


def _intersect(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> tuple[datetime, datetime] | None:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return (start, end) if end > start else None


def overlap_seconds(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> int:
    overlap = _intersect(a_start, a_end, b_start, b_end)
    return elapsed_seconds(*overlap) if overlap else 0


def build_gap(
    *,
    student_id: str,
    activity_id: str,
    activity_type: str,
    skill_codes: Iterable[str],
    interrupt_start: datetime,
    interrupt_end: datetime,
    scheduled_segments: Iterable[tuple[datetime, datetime]],
    completed_segments: Iterable[tuple[datetime, datetime]] = (),
) -> SkillGap:
    """计算单个学生在单个原活动上的能力缺口。

    缺口 = 原活动排期与中断窗口的交集，再扣除中断前/中断期间已经完成的
    签到区间——已完成部分仍由原活动的签到事件计入，补偿只补真正缺失的
    学时，从而避免重复计入；扣除的已完成时长同时登记留痕（结算报告中
    保留来源）。
    """

    interrupt_start = to_utc(interrupt_start)
    interrupt_end = to_utc(interrupt_end)
    codes = sorted(dict.fromkeys(skill_codes))
    if not codes:
        raise ValueError("skill_codes must not be empty")

    lost_intervals: list[tuple[datetime, datetime]] = []
    for raw_start, raw_end in scheduled_segments:
        overlap = _intersect(
            to_utc(raw_start), to_utc(raw_end), interrupt_start, interrupt_end
        )
        if overlap is not None:
            lost_intervals.append(overlap)
    lost_merged = merge_intervals(lost_intervals)
    lost_total = sum(elapsed_seconds(s, e) for s, e in lost_merged)

    # 已完成且落在“被吞掉”区间内的部分：不能重复补偿，只登记来源。
    completed_clipped: list[tuple[datetime, datetime]] = []
    for raw_start, raw_end in completed_segments:
        for lost_start, lost_end in lost_merged:
            overlap = _intersect(
                to_utc(raw_start),
                to_utc(raw_end),
                lost_start,
                lost_end,
            )
            if overlap is not None:
                completed_clipped.append(overlap)
    completed_merged = merge_intervals(completed_clipped)
    completed_in_lost = sum(elapsed_seconds(s, e) for s, e in completed_merged)

    gap_intervals: list[tuple[datetime, datetime]] = list(lost_merged)
    for c_start, c_end in completed_merged:
        gap_intervals = [
            piece
            for raw in gap_intervals
            for piece in _subtract_busy(raw[0], raw[1], [(c_start, c_end)])
        ]
    gap_intervals = merge_intervals(gap_intervals)
    gap_total = sum(elapsed_seconds(s, e) for s, e in gap_intervals)

    def _split_evenly(total: int) -> dict[str, int]:
        share, remainder = divmod(total, len(codes))
        return {
            code: share + (remainder if idx == 0 else 0)
            for idx, code in enumerate(codes)
        }

    lost_share = _split_evenly(lost_total)
    completed_share = _split_evenly(completed_in_lost)
    gap_by_skill = {
        code: max(lost_share[code] - completed_share[code], 0) for code in codes
    }
    # 整数分摊可能导致各项之和与净值相差几秒，余量计入第一个能力。
    drift = gap_total - sum(gap_by_skill.values())
    if drift and codes:
        gap_by_skill[codes[0]] += drift

    lost_start = gap_intervals[0][0] if gap_intervals else None
    lost_end = gap_intervals[-1][1] if gap_intervals else None
    return SkillGap(
        student_id=student_id,
        activity_id=activity_id,
        activity_type=activity_type,
        skill_codes=codes,
        gap_seconds=gap_total,
        gap_by_skill=gap_by_skill,
        completed_seconds=completed_in_lost,
        lost_start=lost_start,
        lost_end=lost_end,
    )


def merge_gaps(gaps: list[SkillGap]) -> SkillGap | None:
    """把同一学生跨多个原活动的缺口合并为一个技能缺口视图。"""

    if not gaps:
        return None
    student_id = gaps[0].student_id
    codes = sorted({code for g in gaps for code in g.skill_codes})
    gap_by_skill: dict[str, int] = {}
    for g in gaps:
        for code, secs in g.gap_by_skill.items():
            gap_by_skill[code] = gap_by_skill.get(code, 0) + secs
    return SkillGap(
        student_id=student_id,
        activity_id="*",
        activity_type="*",
        skill_codes=codes,
        gap_seconds=sum(g.gap_seconds for g in gaps),
        gap_by_skill=gap_by_skill,
        completed_seconds=sum(g.completed_seconds for g in gaps),
    )


def _subtract_busy(
    start: datetime,
    end: datetime,
    busy: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """从 [start, end) 中扣除学生忙碌区间，返回剩余区间列表。"""

    pieces: list[tuple[datetime, datetime]] = [(start, end)]
    for b_start, b_end in merge_intervals(busy):
        next_pieces: list[tuple[datetime, datetime]] = []
        for p_start, p_end in pieces:
            overlap = _intersect(p_start, p_end, b_start, b_end)
            if overlap is None:
                next_pieces.append((p_start, p_end))
                continue
            o_start, o_end = overlap
            if p_start < o_start:
                next_pieces.append((p_start, o_start))
            if o_end < p_end:
                next_pieces.append((o_end, p_end))
        pieces = next_pieces
    return [(s, e) for s, e in pieces if e > s]


def credit_for(seconds: int, weight: float) -> int:
    """替代学时按权重折算回原活动学时（向下取整）。"""

    if seconds <= 0:
        return 0
    credited = int(seconds * weight)
    return max(credited, 0)


def _bookings_in_window(
    bookings: list[tuple[str, datetime, datetime]],
    w_start: datetime,
    w_end: datetime,
) -> int:
    """统计在某窗口内预约过的不同方案数（每个方案在该窗口占一个名额）。"""

    owners = {
        owner
        for owner, b_start, b_end in bookings
        if w_start <= b_start and b_end <= w_end
    }
    return len(owners)


def plan_compensation(
    gap: SkillGap,
    *,
    alternatives: Iterable[Alternative],
    availabilities: Iterable[AvailabilityWindow],
    busy: Iterable[BusyWindow] = (),
    reserved: Iterable[tuple[str, datetime, datetime] | tuple[str, str, datetime, datetime]] = (),
) -> CompensationProposal:
    """按能力要求与可用时段为一个学生生成补偿排期。

    - 只选择 ``skill_code`` 命中缺口能力的替代活动；
    - 时段必须落在该活动的共享可用窗口内，扣除学生本人忙碌时段；
    - ``reserved`` 为其他方案已占用窗口的预约，元素为
      ``(owner, alternative_id, start, end)``（也兼容省略 owner 的三元组）；
      容量按窗口内不同 owner（方案）数计数，同一方案在同一窗口的多个分段
      总共只占一个名额；
    - 每个替代时段按其权重折算为原活动缺口学时（``weight < 1`` 时可能
      需要更长的替代时长），跨活动/跨方案时统一以折算值累计。

    排期按“最早可预约时段优先”的确定性顺序贪心填充，直到各能力缺口均
    被覆盖或没有更多可用窗口。
    """

    alt_index = {a.alternative_id: a for a in alternatives}
    busy_intervals = [(to_utc(b.start_utc), to_utc(b.end_utc)) for b in busy]

    # 归一化为 (owner, alt_id, start, end)；owner 缺省用序号保证互不相同。
    bookings: list[tuple[str, str, datetime, datetime]] = []
    for idx, item in enumerate(reserved):
        if len(item) == 4:
            owner, alt_id, raw_start, raw_end = item  # type: ignore[misc]
        else:
            alt_id, raw_start, raw_end = item  # type: ignore[misc]
            owner = f"ext-{idx}"
        bookings.append((owner, alt_id, to_utc(raw_start), to_utc(raw_end)))

    remaining = dict(gap.gap_by_skill)
    proposal = CompensationProposal(
        student_id=gap.student_id,
        gap_seconds=gap.gap_seconds,
        gap_by_skill=dict(gap.gap_by_skill),
    )

    # 候选窗口按开始时间、结束时间、活动标识确定排序，保证结果可复现。
    candidates: list[tuple[datetime, datetime, Alternative, int]] = []
    for window in availabilities:
        alt = alt_index.get(window.alternative_id)
        if alt is None or alt.skill_code not in remaining:
            continue
        candidates.append(
            (
                to_utc(window.start_utc),
                to_utc(window.end_utc),
                alt,
                max(window.capacity, 1),
            )
        )
    candidates.sort(key=lambda c: (c[0], c[1], c[2].alternative_id))

    for w_start, w_end, alt, capacity in candidates:
        if remaining.get(alt.skill_code, 0) <= 0:
            continue
        alt_bookings = [
            (owner, b_start, b_end)
            for owner, alt_id, b_start, b_end in bookings
            if alt_id == alt.alternative_id
        ]
        external = _bookings_in_window(alt_bookings, w_start, w_end)
        # 窗口按“不同方案数”计名额；只要外部方案未占满，本方案即可在窗口
        # 内跨多个空闲分段排期，自身的分段不重复占名额。
        for piece_start, piece_end in _subtract_busy(
            w_start, w_end, busy_intervals
        ):
            need = remaining.get(alt.skill_code, 0)
            if need <= 0:
                break
            if external >= capacity:
                break
            piece_seconds = elapsed_seconds(piece_start, piece_end)
            # weight<1 时折算学时更短，按缺口反推所需替代时长（向上取整）。
            wanted_scheduled = need if alt.weight >= 1.0 else ceil(need / alt.weight)
            take_scheduled = min(piece_seconds, wanted_scheduled)
            if take_scheduled <= 0:
                continue
            slot_end = piece_start + timedelta(seconds=take_scheduled)
            credited = min(credit_for(take_scheduled, alt.weight), need)
            if credited <= 0:
                continue
            proposal.slots.append(
                ScheduledSlot(
                    alternative_id=alt.alternative_id,
                    title=alt.title,
                    skill_code=alt.skill_code,
                    start_utc=piece_start,
                    end_utc=slot_end,
                    scheduled_seconds=take_scheduled,
                    credited_seconds=credited,
                    weight=alt.weight,
                )
            )
            remaining[alt.skill_code] -= credited
            # 已排时段不能再与该学生后续分段重叠。
            busy_intervals.append((piece_start, slot_end))

    return proposal


def proposal_to_dict(proposal: CompensationProposal) -> dict[str, Any]:
    return {
        "student_id": proposal.student_id,
        "gap_seconds": proposal.gap_seconds,
        "gap_by_skill": dict(proposal.gap_by_skill),
        "covered": proposal.covered,
        "credited_seconds": proposal.credited_seconds,
        "slots": [
            {
                "alternative_id": s.alternative_id,
                "title": s.title,
                "skill_code": s.skill_code,
                "start_at": s.start_utc.isoformat().replace("+00:00", "Z"),
                "end_at": s.end_utc.isoformat().replace("+00:00", "Z"),
                "scheduled_seconds": s.scheduled_seconds,
                "credited_seconds": s.credited_seconds,
                "weight": s.weight,
            }
            for s in proposal.slots
        ],
    }
