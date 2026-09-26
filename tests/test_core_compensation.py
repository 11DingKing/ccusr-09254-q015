"""中断补偿纯领域逻辑的单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.compensation import (
    Alternative,
    AvailabilityWindow,
    BusyWindow,
    build_gap,
    credit_for,
    merge_gaps,
    plan_compensation,
)

UTC = timezone.utc


def _dt(hour: int, day: int = 10) -> datetime:
    return datetime(2024, 7, day, hour, tzinfo=UTC)


def test_build_gap_intersects_window_and_subtracts_completed():
    gap = build_gap(
        student_id="S1",
        activity_id="A1",
        activity_type="internship",
        skill_codes=["SK1", "SK2"],
        interrupt_start=_dt(0),
        interrupt_end=_dt(0, day=12),
        scheduled_segments=[(_dt(8), _dt(12)), (_dt(8, 11), _dt(12, 11))],
        completed_segments=[(_dt(8), _dt(10))],
    )
    # 两天排期落入中断窗口共 8 小时；其中已完成 2 小时 -> 缺口 6 小时。
    assert gap.gap_seconds == 6 * 3600
    assert gap.completed_seconds == 2 * 3600
    # 两个能力均分 6 小时。
    assert sum(gap.gap_by_skill.values()) == 6 * 3600
    assert gap.gap_by_skill["SK1"] == 3 * 3600
    assert gap.gap_by_skill["SK2"] == 3 * 3600


def test_build_gap_zero_when_schedule_outside_window():
    gap = build_gap(
        student_id="S1",
        activity_id="A1",
        activity_type="regular",
        skill_codes=["SK1"],
        interrupt_start=_dt(0, day=20),
        interrupt_end=_dt(0, day=22),
        scheduled_segments=[(_dt(8, 10), _dt(12, 10))],
    )
    assert gap.gap_seconds == 0
    assert gap.completed_seconds == 0
    assert gap.lost_start is None


def test_merge_gaps_aggregates_skills():
    g1 = build_gap(
        student_id="S1",
        activity_id="A1",
        activity_type="internship",
        skill_codes=["SK1"],
        interrupt_start=_dt(0),
        interrupt_end=_dt(0, day=12),
        scheduled_segments=[(_dt(8), _dt(12))],
    )
    g2 = build_gap(
        student_id="S1",
        activity_id="A2",
        activity_type="internship",
        skill_codes=["SK2"],
        interrupt_start=_dt(0),
        interrupt_end=_dt(0, day=12),
        scheduled_segments=[(_dt(8, 11), _dt(12, 11))],
    )
    merged = merge_gaps([g1, g2])
    assert merged is not None
    assert merged.gap_seconds == 8 * 3600
    assert merged.gap_by_skill == {"SK1": 4 * 3600, "SK2": 4 * 3600}


def test_plan_respects_busy_windows_and_skill_match():
    gap = build_gap(
        student_id="S1",
        activity_id="A1",
        activity_type="internship",
        skill_codes=["SK-W"],
        interrupt_start=_dt(0),
        interrupt_end=_dt(0, day=12),
        scheduled_segments=[(_dt(8), _dt(16))],
    )
    alts = [
        Alternative("ALT-W", "ws", "SK-W", 1.0),
        Alternative("ALT-X", "other", "SK-OTHER", 1.0),
    ]
    windows = [
        AvailabilityWindow("ALT-W", _dt(8, 15), _dt(18, 15), 1),
        AvailabilityWindow("ALT-X", _dt(8, 15), _dt(18, 15), 1),
    ]
    # 学生 10:00-14:00 忙碌，窗口内只剩 8-10 与 14-18 共 6 小时。
    busy = [BusyWindow(_dt(10, 15), _dt(14, 15))]
    proposal = plan_compensation(
        gap, alternatives=alts, availabilities=windows, busy=busy
    )
    # 只能拿到 6 小时，8 小时缺口未覆盖。
    assert proposal.credited_seconds == 6 * 3600
    assert proposal.covered is False
    assert all(s.skill_code == "SK-W" for s in proposal.slots)
    assert all(s.alternative_id != "ALT-X" for s in proposal.slots)


def test_plan_weight_conversion_rounds_scheduled_up():
    gap = build_gap(
        student_id="S1",
        activity_id="A1",
        activity_type="internship",
        skill_codes=["SK-W"],
        interrupt_start=_dt(0),
        interrupt_end=_dt(0, day=12),
        scheduled_segments=[(_dt(8), _dt(11))],  # 3 小时缺口
    )
    proposal = plan_compensation(
        gap,
        alternatives=[Alternative("ALT-W2", "sim", "SK-W", 0.5)],
        availabilities=[
            AvailabilityWindow("ALT-W2", _dt(8, 16), _dt(20, 16), 5)
        ],
    )
    slot = proposal.slots[0]
    # 折算权重 0.5 -> 需要 6 小时替代时长。
    assert slot.scheduled_seconds == 6 * 3600
    assert slot.credited_seconds == 3 * 3600
    assert proposal.covered is True
    assert credit_for(5400, 0.5) == 2700


def test_plan_window_capacity_blocks_after_n_bookings():
    gap = build_gap(
        student_id="S9",
        activity_id="A1",
        activity_type="internship",
        skill_codes=["SK-W"],
        interrupt_start=_dt(0),
        interrupt_end=_dt(0, day=12),
        scheduled_segments=[(_dt(8), _dt(16))],  # 8 小时缺口
    )
    alt = Alternative("ALT-W", "ws", "SK-W", 1.0)
    window = AvailabilityWindow("ALT-W", _dt(8, 15), _dt(18, 15), 2)
    # 窗口已有两个预约 -> 名额满，本学生无法排入。
    reserved = [
        ("ALT-W", _dt(8, 15), _dt(10, 15)),
        ("ALT-W", _dt(14, 15), _dt(16, 15)),
    ]
    proposal = plan_compensation(
        gap, alternatives=[alt], availabilities=[window], reserved=reserved
    )
    assert proposal.slots == []
    assert proposal.covered is False

    # 只有一个预约 -> 仍有一个名额，可以排。
    proposal2 = plan_compensation(
        gap, alternatives=[alt], availabilities=[window], reserved=reserved[:1]
    )
    assert len(proposal2.slots) == 1


def test_plan_skips_unrelated_skills_and_prefers_earliest():
    gap = build_gap(
        student_id="S1",
        activity_id="A1",
        activity_type="internship",
        skill_codes=["SK-A"],
        interrupt_start=_dt(0),
        interrupt_end=_dt(0, day=12),
        scheduled_segments=[(_dt(8), _dt(10))],  # 2 小时
    )
    alts = [
        Alternative("LATE", "late", "SK-A", 1.0),
        Alternative("EARLY", "early", "SK-A", 1.0),
    ]
    windows = [
        AvailabilityWindow("LATE", _dt(8, 20), _dt(12, 20), 2),
        AvailabilityWindow("EARLY", _dt(8, 15), _dt(12, 15), 2),
    ]
    proposal = plan_compensation(
        gap, alternatives=alts, availabilities=windows
    )
    assert proposal.slots[0].alternative_id == "EARLY"
    assert proposal.slots[0].start_utc == _dt(8, 15)
    assert proposal.credited_seconds == 2 * 3600
