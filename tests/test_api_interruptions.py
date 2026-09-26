"""实训中断补偿 API 的集成测试。"""

from __future__ import annotations

import threading

import pytest

from tests.conftest import TestSessionLocal

PV = "P-SH-2024"


def _plan():
    return {
        "plan_version": PV,
        "iana_timezone": "Asia/Shanghai",
        "required_seconds": 10800,
    }


def _setup_plan(client):
    resp = client.post("/api/plans", json=_plan())
    assert resp.status_code == 201, resp.text


def _register_interruption(client, iid="I-01"):
    resp = client.post(
        f"/api/plans/{PV}/interruptions",
        json={
            "interruption_id": iid,
            "reason": "typhoon",
            "interrupt_start": "2024-07-10T00:00:00+08:00",
            "interrupt_end": "2024-07-12T00:00:00+08:00",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _impact(student, activity="ACT-1", skills=None, scheduled=None, completed=None):
    return {
        "student_id": student,
        "activity_id": activity,
        "activity_type": "internship",
        "required_skill_codes": skills or ["SK-WELD"],
        "scheduled_segments": scheduled
        or [
            [
                "2024-07-10T08:00:00+08:00",
                "2024-07-10T12:00:00+08:00",
            ],
            [
                "2024-07-11T08:00:00+08:00",
                "2024-07-11T12:00:00+08:00",
            ],
        ],
        "completed_segments": completed or [],
    }


def _catalog(client):
    resp = client.put(
        f"/api/plans/{PV}/catalog",
        json={
            "alternatives": [
                {
                    "alternative_id": "ALT-W",
                    "title": "welding workshop",
                    "skill_code": "SK-WELD",
                    "weight": 1.0,
                },
                {
                    "alternative_id": "ALT-W2",
                    "title": "online welding sim",
                    "skill_code": "SK-WELD",
                    "weight": 0.5,
                },
            ],
            "availabilities": [
                {
                    "alternative_id": "ALT-W",
                    "start_at": "2024-07-15T08:00:00+08:00",
                    "end_at": "2024-07-15T18:00:00+08:00",
                    "capacity": 2,
                },
                {
                    "alternative_id": "ALT-W2",
                    "start_at": "2024-07-16T08:00:00+08:00",
                    "end_at": "2024-07-16T18:00:00+08:00",
                    "capacity": 5,
                },
            ],
        },
    )
    assert resp.status_code == 200, resp.text


def _generate(client, iid="I-01", student_ids=None, confirm=False):
    resp = client.post(
        f"/api/plans/{PV}/interruptions/{iid}/compensations/generate",
        json={
            "interruption_id": iid,
            "student_ids": student_ids or [],
            "code_prefix": "CP",
            "confirm": confirm,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 中断登记与批量影响
# ---------------------------------------------------------------------------


def test_register_interruption_and_batch_impacts_compute_gaps(client):
    _setup_plan(client)
    _register_interruption(client)
    resp = client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={
            "impacts": [
                _impact("S1"),
                _impact("S2"),
                # 中断窗口前的排期不受影响 -> 零缺口。
                _impact(
                    "S3",
                    scheduled=[
                        [
                            "2024-07-01T08:00:00+08:00",
                            "2024-07-01T12:00:00+08:00",
                        ]
                    ],
                ),
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["accepted"] == 3
    assert body["duplicates"] == 0
    by_student = {i["student_id"]: i for i in body["impacts"]}
    # S1/S2 两天各 4 小时落入中断窗口 = 8 小时缺口。
    assert by_student["S1"]["lost_seconds"] == 8 * 3600
    assert by_student["S1"]["gap_seconds"] == 8 * 3600
    assert by_student["S1"]["skill_gap_seconds"]["SK-WELD"] == 8 * 3600
    assert by_student["S3"]["lost_seconds"] == 0
    assert by_student["S3"]["gap_seconds"] == 0


def test_completed_part_is_deduplicated_and_retained_as_source(client):
    _setup_plan(client)
    _register_interruption(client)
    resp = client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={
            "impacts": [
                _impact(
                    "S1",
                    # 第一天的 4 小时已在中断当天上午完成签到。
                    completed=[
                        [
                            "2024-07-10T08:00:00+08:00",
                            "2024-07-10T12:00:00+08:00",
                        ]
                    ],
                )
            ]
        },
    )
    body = resp.json()
    impact = body["impacts"][0]
    assert impact["lost_seconds"] == 8 * 3600
    assert impact["already_completed_seconds"] == 4 * 3600
    # 已完成部分不重复计入缺口：只剩第二天的 4 小时。
    assert impact["gap_seconds"] == 4 * 3600

    _catalog(client)
    plans = _generate(client)
    plan = plans[0]
    assert plan["gap_seconds"] == 4 * 3600
    assert plan["student_id"] == "S1"


def test_impact_import_is_idempotent(client):
    _setup_plan(client)
    _register_interruption(client)
    payload = {"impacts": [_impact("S1")]}
    first = client.post(f"/api/plans/{PV}/interruptions/I-01/impacts", json=payload)
    assert first.json()["accepted"] == 1
    second = client.post(f"/api/plans/{PV}/interruptions/I-01/impacts", json=payload)
    assert second.json()["accepted"] == 0
    assert second.json()["duplicates"] == 1
    assert len(second.json()["impacts"]) == 1


def test_register_interruption_validation(client):
    _setup_plan(client)
    resp = client.post(
        f"/api/plans/{PV}/interruptions",
        json={
            "interruption_id": "I-BAD",
            "reason": "",
            "interrupt_start": "2024-07-12T00:00:00+08:00",
            "interrupt_end": "2024-07-10T00:00:00+08:00",
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        f"/api/plans/NOPE/interruptions",
        json={
            "interruption_id": "I-X",
            "interrupt_start": "2024-07-10T00:00:00+08:00",
            "interrupt_end": "2024-07-12T00:00:00+08:00",
        },
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 方案生成：能力匹配、容量、幂等
# ---------------------------------------------------------------------------


def test_generate_matches_skill_and_availabilities(client):
    _setup_plan(client)
    _register_interruption(client)
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={"impacts": [_impact("S1")]},
    )
    _catalog(client)
    plans = _generate(client)
    assert len(plans) == 1
    plan = plans[0]
    assert plan["status"] == "PROPOSED"
    assert plan["locked_version"] == 1
    assert plan["plan_code"] == "CP-0001-S1"
    # 8 小时缺口：权重 1.0 的工坊 7 月 15 日窗口有 10 小时，一次补齐。
    assert len(plan["slots"]) == 1
    slot = plan["slots"][0]
    assert slot["alternative_id"] == "ALT-W"
    assert slot["scheduled_seconds"] == 8 * 3600
    assert slot["credited_seconds"] == 8 * 3600
    assert plan["fully_covered"] is True


def test_generate_respects_window_capacity_across_batch(client):
    _setup_plan(client)
    _register_interruption(client)
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={"impacts": [_impact("S1"), _impact("S2"), _impact("S3")]},
    )
    _catalog(client)
    plans = _generate(client)
    assert len(plans) == 3
    # 容量为 2 的 ALT-W 窗口只能容纳两个学生；第三人的方案仍生成，
    # 但无法被高权重窗口覆盖，落到下一可用窗口或未覆盖。
    alt_w_users = {
        p["student_id"]
        for p in plans
        for s in p["slots"]
        if s["alternative_id"] == "ALT-W"
    }
    assert len(alt_w_users) == 2
    third = next(p for p in plans if p["student_id"] not in alt_w_users)
    assert all(s["alternative_id"] != "ALT-W" for s in third["slots"])


def test_generate_is_idempotent(client):
    _setup_plan(client)
    _register_interruption(client)
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={"impacts": [_impact("S1")]},
    )
    _catalog(client)
    first = _generate(client)
    second = _generate(client)
    assert [p["plan_code"] for p in first] == [p["plan_code"] for p in second]
    assert len(second) == 1
    assert len(second[0]["slots"]) == len(first[0]["slots"])


# ---------------------------------------------------------------------------
# 并发确认（乐观锁）
# ---------------------------------------------------------------------------


def test_concurrent_confirmation_only_one_wins(client):
    _setup_plan(client)
    _register_interruption(client)
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={"impacts": [_impact("S1")]},
    )
    _catalog(client)
    plan = _generate(client)[0]
    code = plan["plan_code"]

    from app import services_interruption as si

    outcomes: list[bool] = []
    lock = threading.Lock()

    def _confirm():
        session = TestSessionLocal()
        try:
            try:
                si.confirm_compensation(
                    session,
                    plan_version=PV,
                    plan_code=code,
                    expected_version=1,
                )
                ok = True
            except si.CompensationConflictError:
                ok = False
            with lock:
                outcomes.append(ok)
        finally:
            session.close()

    threads = [threading.Thread(target=_confirm) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 5

    stored = client.get(f"/api/plans/{PV}/compensations/{code}").json()
    assert stored["status"] == "CONFIRMED"
    assert stored["locked_version"] == 2
    # 已锁定方案不能再次确认。
    again = client.post(
        f"/api/plans/{PV}/compensations/{code}/confirm",
        json={"expected_version": 1},
    )
    assert again.status_code == 409
    # 旧版本号确认被拒。
    stale = client.post(
        f"/api/plans/{PV}/compensations/{code}/confirm",
        json={"expected_version": 1},
    )
    assert stale.status_code == 409


def test_locked_plan_cannot_be_rescheduled_but_can_complete(client):
    _setup_plan(client)
    _register_interruption(client)
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={"impacts": [_impact("S1")]},
    )
    _catalog(client)
    plan = _generate(client, confirm=True)[0]
    slot_id = plan["slots"][0]["slot_id"]
    code = plan["plan_code"]

    # 确认后改期被拒。
    resp = client.patch(
        f"/api/plans/{PV}/compensations/{code}/slots/{slot_id}",
        json={
            "start_at": "2024-07-16T08:00:00+08:00",
            "end_at": "2024-07-16T12:00:00+08:00",
        },
    )
    assert resp.status_code == 409

    # 登记完成情况。
    resp = client.post(
        f"/api/plans/{PV}/compensations/{code}/slots/{slot_id}/complete",
        json={"credited_seconds": 8 * 3600},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slots"][0]["status"] == "COMPLETED"
    assert body["completed_seconds"] == 8 * 3600


def test_proposed_plan_can_be_rescheduled_within_capacity(client):
    _setup_plan(client)
    _register_interruption(client)
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={"impacts": [_impact("S1")]},
    )
    _catalog(client)
    plan = _generate(client)[0]
    slot_id = plan["slots"][0]["slot_id"]
    code = plan["plan_code"]

    resp = client.patch(
        f"/api/plans/{PV}/compensations/{code}/slots/{slot_id}",
        json={
            "start_at": "2024-07-15T09:00:00+08:00",
            "end_at": "2024-07-15T13:00:00+08:00",
        },
    )
    assert resp.status_code == 200, resp.text
    moved = resp.json()["slots"][0]
    assert moved["start_at"].startswith("2024-07-15T01:00:00")  # UTC
    assert moved["scheduled_seconds"] == 4 * 3600

    # 移出可用窗口被拒。
    resp = client.patch(
        f"/api/plans/{PV}/compensations/{code}/slots/{slot_id}",
        json={
            "start_at": "2024-08-01T09:00:00+08:00",
            "end_at": "2024-08-01T13:00:00+08:00",
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 跨方案折算（weight < 1）
# ---------------------------------------------------------------------------


def test_cross_plan_credit_conversion_with_weight(client):
    _setup_plan(client)
    _register_interruption(client)
    # 4 小时缺口。
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={
            "impacts": [
                _impact(
                    "S1",
                    scheduled=[
                        [
                            "2024-07-10T08:00:00+08:00",
                            "2024-07-10T12:00:00+08:00",
                        ]
                    ],
                )
            ]
        },
    )
    # 只提供权重 0.5 的替代活动：需要 8 小时替代时长才能折算出 4 小时。
    client.put(
        f"/api/plans/{PV}/catalog",
        json={
            "alternatives": [
                {
                    "alternative_id": "ALT-W2",
                    "title": "online sim",
                    "skill_code": "SK-WELD",
                    "weight": 0.5,
                }
            ],
            "availabilities": [
                {
                    "alternative_id": "ALT-W2",
                    "start_at": "2024-07-16T08:00:00+08:00",
                    "end_at": "2024-07-16T18:00:00+08:00",
                    "capacity": 5,
                }
            ],
        },
    )
    plan = _generate(client, confirm=True)[0]
    slot = plan["slots"][0]
    assert slot["alternative_id"] == "ALT-W2"
    assert slot["scheduled_seconds"] == 8 * 3600
    assert slot["weight"] == 0.5
    assert slot["credited_seconds"] == 4 * 3600
    assert plan["fully_covered"] is True

    # 完成后结算，折算学时与原活动保留来源分开列示。
    client.post(
        f"/api/plans/{PV}/compensations/{plan['plan_code']}/slots/"
        f"{slot['slot_id']}/complete",
        json={},
    )
    settle = client.post(
        f"/api/plans/{PV}/interruptions/I-01/settle"
    ).json()
    assert settle["status"] == "SETTLED"
    assert settle["total_gap_seconds"] == 4 * 3600
    assert settle["total_completed_seconds"] == 4 * 3600
    sources = settle["plans"][0]["source_credits"]
    kinds = {s["source"] for s in sources}
    assert kinds == {"original_activity", "alternative"}
    alt_credit = next(s for s in sources if s["source"] == "alternative")
    assert alt_credit["seconds"] == 4 * 3600
    assert alt_credit["alternative_id"] == "ALT-W2"


# ---------------------------------------------------------------------------
# 重启恢复：只释放尚未开始的补偿
# ---------------------------------------------------------------------------


def _plan_with_two_future_slots(client):
    _setup_plan(client)
    _register_interruption(client)
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={"impacts": [_impact("S1")]},
    )
    # 两个不同日期的窗口，各取一部分。
    client.put(
        f"/api/plans/{PV}/catalog",
        json={
            "alternatives": [
                {
                    "alternative_id": "ALT-W",
                    "title": "workshop",
                    "skill_code": "SK-WELD",
                    "weight": 1.0,
                }
            ],
            "availabilities": [
                {
                    "alternative_id": "ALT-W",
                    "start_at": "2024-07-15T08:00:00+08:00",
                    "end_at": "2024-07-15T12:00:00+08:00",
                    "capacity": 3,
                },
                {
                    "alternative_id": "ALT-W",
                    "start_at": "2024-07-16T08:00:00+08:00",
                    "end_at": "2024-07-16T16:00:00+08:00",
                    "capacity": 3,
                },
            ],
        },
    )
    plan = _generate(client, confirm=True)[0]
    assert len(plan["slots"]) == 2
    return plan


def test_resume_releases_only_not_started_slots(client):
    plan = _plan_with_two_future_slots(client)
    code = plan["plan_code"]
    early_slot, late_slot = plan["slots"]

    # 7 月 15 日的时段已完成（4 小时）。
    client.post(
        f"/api/plans/{PV}/compensations/{code}/slots/{early_slot['slot_id']}/complete",
        json={},
    )

    # 原活动于 7 月 16 日 07:00 恢复：15 日的已完成保留，16 日的尚未开始释放。
    resume = client.post(
        f"/api/plans/{PV}/interruptions/I-01/resume",
        json={"resume_at": "2024-07-16T07:00:00+08:00"},
    )
    assert resume.status_code == 200, resume.text
    body = resume.json()
    assert body["status"] == "RECOVERED"
    assert body["released_slot_count"] == 1
    assert body["released_seconds"] == late_slot["scheduled_seconds"]

    stored = client.get(f"/api/plans/{PV}/compensations/{code}").json()
    statuses = {s["slot_id"]: s["status"] for s in stored["slots"]}
    assert statuses[early_slot["slot_id"]] == "COMPLETED"
    assert statuses[late_slot["slot_id"]] == "RELEASED"
    released = next(s for s in stored["slots"] if s["slot_id"] == late_slot["slot_id"])
    assert released["credited_seconds"] == 0

    # 结算：已完成补偿保留来源；被释放时段不再产生学时。
    settle = client.post(f"/api/plans/{PV}/interruptions/I-01/settle").json()
    assert settle["total_completed_seconds"] == early_slot["credited_seconds"]
    assert settle["released_slot_count"] == 1
    assert settle["released_seconds"] == late_slot["scheduled_seconds"]
    settled_plan = settle["plans"][0]
    assert settled_plan["status"] == "SETTLED"
    assert settled_plan["settled_seconds"] == early_slot["credited_seconds"]
    # 已完成部分仍可追溯到替代活动来源。
    assert any(
        s["source"] == "alternative"
        and s["slot_id"] == early_slot["slot_id"]
        for s in settled_plan["source_credits"]
    )


def test_resume_before_any_slot_releases_all(client):
    plan = _plan_with_two_future_slots(client)
    resume = client.post(
        f"/api/plans/{PV}/interruptions/I-01/resume",
        json={"resume_at": "2024-07-14T00:00:00+08:00"},
    ).json()
    assert resume["released_slot_count"] == 2
    settle = client.post(f"/api/plans/{PV}/interruptions/I-01/settle").json()
    assert settle["total_completed_seconds"] == 0
    # 两个时段全部释放，缺口全部回到原活动（恢复后由原活动自行补足）。
    assert settle["released_seconds"] == 8 * 3600


def test_settle_without_resume_releases_unfinished_slots(client):
    plan = _plan_with_two_future_slots(client)
    code = plan["plan_code"]
    early_slot, late_slot = plan["slots"]
    client.post(
        f"/api/plans/{PV}/compensations/{code}/slots/{early_slot['slot_id']}/complete",
        json={},
    )
    # 直接结算（未先 resume）：未完成时段在结算时释放，完成部分保留来源。
    settle = client.post(f"/api/plans/{PV}/interruptions/I-01/settle").json()
    assert settle["released_slot_count"] == 1
    assert settle["released_seconds"] == late_slot["scheduled_seconds"]
    assert settle["total_completed_seconds"] == early_slot["credited_seconds"]
    stored = client.get(f"/api/plans/{PV}/compensations/{code}").json()
    assert all(s["status"] in {"COMPLETED", "RELEASED"} for s in stored["slots"])
    # 结算幂等：再次结算返回同样的汇总。
    settle_again = client.post(
        f"/api/plans/{PV}/interruptions/I-01/settle"
    )
    assert settle_again.status_code == 409


def test_settled_interruption_cannot_regenerate(client):
    _setup_plan(client)
    _register_interruption(client)
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={"impacts": [_impact("S1")]},
    )
    _catalog(client)
    _generate(client)
    client.post(f"/api/plans/{PV}/interruptions/I-01/settle")
    resp = client.post(
        f"/api/plans/{PV}/interruptions/I-01/compensations/generate",
        json={"interruption_id": "I-01"},
    )
    assert resp.status_code == 409


def test_busy_split_slots_consume_single_window_seat(client):
    """学生窗口中段有既有签到（忙碌），补偿在同一窗口排两段但只占一个名额。"""

    _setup_plan(client)
    # 学生在 7 月 15 日 12:00-14:00 有一段原活动签到，补偿窗口须绕开它。
    client.post(
        f"/api/plans/{PV}/events",
        json={
            "events": [
                {
                    "event_id": "E-BUSY",
                    "event_type": "checkin",
                    "student_id": "S1",
                    "payload": {
                        "activity_id": "OLD",
                        "activity_type": "regular",
                        "check_in_at": "2024-07-15T12:00:00+08:00",
                        "check_out_at": "2024-07-15T14:00:00+08:00",
                    },
                }
            ]
        },
    )
    _register_interruption(client)
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={"impacts": [_impact("S1")]},
    )
    # 仅一个容量为 1 的窗口（8h 可用：8-12 与 14-18）。
    client.put(
        f"/api/plans/{PV}/catalog",
        json={
            "alternatives": [
                {
                    "alternative_id": "ALT-W",
                    "title": "ws",
                    "skill_code": "SK-WELD",
                    "weight": 1.0,
                }
            ],
            "availabilities": [
                {
                    "alternative_id": "ALT-W",
                    "start_at": "2024-07-15T08:00:00+08:00",
                    "end_at": "2024-07-15T18:00:00+08:00",
                    "capacity": 1,
                }
            ],
        },
    )
    plan = _generate(client)[0]
    # 缺口 8h，可用 8h（4h+4h），同窗口分两段；容量为 1 仍应全部排下。
    assert len(plan["slots"]) == 2
    assert sum(s["credited_seconds"] for s in plan["slots"]) == 8 * 3600
    starts = sorted(s["start_at"] for s in plan["slots"])
    assert starts[0].startswith("2024-07-15T00:00:00")
    assert starts[1].startswith("2024-07-15T06:00:00")
    assert plan["fully_covered"] is True

    # 窗口名额已被 S1 方案占满；S2 不能再用该窗口。
    client.post(
        f"/api/plans/{PV}/interruptions/I-01/impacts",
        json={"impacts": [_impact("S2")]},
    )
    plans = _generate(client)
    s2 = next(p for p in plans if p["student_id"] == "S2")
    assert all(s["alternative_id"] != "ALT-W" for s in s2["slots"])
