"""服务端业务模块。"""

from __future__ import annotations

import threading
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.interruption import services as interruption_services
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

PV = SHANGHAI_PLAN["plan_version"]


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _register(client, iid="INT-01", **over):
    body = {
        "interruption_id": iid,
        "plan_version": PV,
        "kind": "natural_disaster",
        "occurred_at": "2024-04-01T00:00:00+08:00",
        "note": "台风导致合作企业停产",
    }
    body.update(over)
    return client.post("/api/interruptions", json=body)


def _impacts(client, iid, impacts):
    return client.post(f"/api/interruptions/{iid}/impacts", json={"impacts": impacts})


def _generate(client, iid, slots, student_ids=None):
    body = {"slots": slots}
    if student_ids is not None:
        body["student_ids"] = student_ids
    return client.post(f"/api/interruptions/{iid}/plans/generate", json=body)


def _slot(slot_id, capability, start, end, ratio=1.0, source=None):
    slot = {
        "slot_id": slot_id,
        "capability": capability,
        "start_at": start,
        "end_at": end,
        "conversion_ratio": ratio,
    }
    if source is not None:
        slot["source_plan_version"] = source
    return slot


def _confirm(client, plan_id, student_id):
    return client.post(
        f"/api/compensation-plans/{plan_id}/confirm", json={"student_id": student_id}
    )


def _settle(client, plan_id):
    return client.post(f"/api/compensation-plans/{plan_id}/settle")


def test_register_interruption_idempotent_and_requires_plan(client):
    # 未注册的培养方案不能登记中断。
    resp = client.post(
        "/api/interruptions",
        json={
            "interruption_id": "INT-X",
            "plan_version": "NOPE",
            "kind": "other",
            "occurred_at": "2024-04-01T00:00:00+08:00",
        },
    )
    assert resp.status_code == 404

    _create_plan(client)
    resp = _register(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["interruption"]["status"] == "open"
    assert body["interruption"]["occurred_at"] == "2024-03-31T16:00:00Z"

    # 重复登记幂等返回已存在的记录。
    again = _register(client).json()
    assert again["created"] is False
    assert again["interruption"]["interruption_id"] == "INT-01"

    fetched = client.get("/api/interruptions/INT-01").json()
    assert fetched["plan_version"] == PV
    assert client.get("/api/interruptions/INT-404").status_code == 404
    assert client.get("/api/compensation-plans/NOPE:S1").status_code == 404


def test_batch_impacts_and_gap_generation(client):
    """批量影响：成批登记名单，按能力要求与共享时段池生成缺口方案。"""
    _create_plan(client)
    _register(client, "INT-B")

    impacts = [
        {
            "student_id": "S1",
            "activity_id": "A1",
            "capabilities": {"welding": 7200, "safety": 3600},
            "completed": {"welding": 1800},
        },
        {
            "student_id": "S2",
            "activity_id": "A1",
            "capabilities": {"welding": 3600},
            "completed": {},
        },
        {
            "student_id": "S3",
            "activity_id": "A2",
            "capabilities": {"safety": 3600},
            "completed": {"safety": 3600},
        },
    ]
    resp = _impacts(client, "INT-B", impacts)
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"accepted": 3, "duplicates": []}

    # 重复导入同一批名单，全部记为 duplicates。
    again = _impacts(client, "INT-B", impacts).json()
    assert again == {"accepted": 0, "duplicates": ["S1", "S2", "S3"]}

    # 名单视图直接给出每人的能力缺口。
    roster = client.get("/api/interruptions/INT-B/impacts").json()["impacts"]
    assert roster[0]["gaps"] == {"welding": 5400, "safety": 3600}
    assert roster[1]["gaps"] == {"welding": 3600}
    assert roster[2]["gaps"] == {}

    slots = [
        _slot("SL-W1", "welding", "2024-04-02T09:00:00+08:00", "2024-04-02T11:00:00+08:00"),
        _slot("SL-S1", "safety", "2024-04-02T13:00:00+08:00", "2024-04-02T14:00:00+08:00"),
    ]
    resp = _generate(client, "INT-B", slots)
    assert resp.status_code == 201, resp.text
    plans = resp.json()["plans"]
    assert [p["student_id"] for p in plans] == ["S1", "S2", "S3"]

    # S1 先占用焊接时段池 5400 秒，安全时段全部覆盖；补偿项按时间排序。
    s1, s2, s3 = plans
    assert s1["gaps"] == {"welding": 5400, "safety": 3600}
    assert s1["unfilled"] == {}
    assert [(i["capability"], i["raw_seconds"]) for i in s1["items"]] == [
        ("welding", 5400),
        ("safety", 3600),
    ]
    weld = next(i for i in s1["items"] if i["capability"] == "welding")
    assert weld["start_at"] == "2024-04-02T01:00:00Z"
    assert weld["end_at"] == "2024-04-02T02:30:00Z"

    # 时段池只剩 1800 秒，S2 的缺口只能部分覆盖，其余进入 unfilled。
    assert s2["items"][0]["raw_seconds"] == 1800
    assert s2["items"][0]["start_at"] == "2024-04-02T02:30:00Z"
    assert s2["unfilled"] == {"welding": 1800}

    # S3 原活动已完成部分已满足要求，不生成补偿项，避免重复计入。
    assert s3["items"] == []

    # 重复生成不会覆盖已有方案，只报告 existing。
    regen = _generate(client, "INT-B", slots).json()
    assert regen["plans"] == []
    assert regen["existing"] == [p["plan_id"] for p in plans]

    listed = client.get("/api/interruptions/INT-B/plans").json()["plans"]
    assert len(listed) == 3

    # 不在名单中的学生不能生成方案。
    resp = _generate(client, "INT-B", slots, student_ids=["S9"])
    assert resp.status_code == 400


def test_concurrent_confirm_only_one_locks(client):
    """并发确认：多个确认同时到达时只发生一次状态迁移，其余幂等成功。"""
    _create_plan(client)
    _register(client, "INT-C")
    _impacts(
        client,
        "INT-C",
        [
            {
                "student_id": "S1",
                "activity_id": "A1",
                "capabilities": {"welding": 3600},
                "completed": {},
            }
        ],
    )
    _generate(
        client,
        "INT-C",
        [_slot("SL-1", "welding", "2024-04-02T09:00:00+08:00", "2024-04-02T10:00:00+08:00")],
    )
    plan_id = "INT-C:S1"

    results: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _do_confirm():
        session = TestSessionLocal()
        try:
            view = interruption_services.confirm_plan(
                session, plan_id, student_id="S1"
            )
            with lock:
                results.append(view["status"])
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_do_confirm) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert results == ["confirmed"] * 4

    plan = client.get(f"/api/compensation-plans/{plan_id}").json()
    assert plan["status"] == "confirmed"
    assert plan["confirmed_by"] == "S1"
    # 只发生一次迁移：版本只加一，审计里只有一条 confirm。
    assert plan["version"] == 2
    assert [a["action"] for a in plan["audit"]] == ["generate", "confirm"]

    # 已确认后重复确认幂等；他人确认被拒绝。
    assert _confirm(client, plan_id, "S1").status_code == 200
    assert client.get(f"/api/compensation-plans/{plan_id}").json()["version"] == 2
    assert _confirm(client, plan_id, "S2").status_code == 403


def test_cross_plan_conversion_in_settlement(client):
    """跨方案折算：其他方案的替代活动按折算比例计入，且不重复计入。"""
    _create_plan(client)
    _register(client, "INT-X")
    _impacts(
        client,
        "INT-X",
        [
            {
                "student_id": "S1",
                "activity_id": "A1",
                "capabilities": {"welding": 5400},
                "completed": {"welding": 1800},
            }
        ],
    )
    _generate(
        client,
        "INT-X",
        [
            _slot(
                "SL-ONLINE",
                "welding",
                "2024-04-02T09:00:00+08:00",
                "2024-04-02T11:00:00+08:00",
                ratio=0.5,
                source="P-ONLINE-2024",
            )
        ],
    )
    plan = client.get("/api/compensation-plans/INT-X:S1").json()
    item = plan["items"][0]
    # 线上方案 7200 秒按 0.5 折算为 3600 秒，恰好覆盖缺口。
    assert item["raw_seconds"] == 7200
    assert item["credited_seconds"] == 3600
    assert item["source_plan_version"] == "P-ONLINE-2024"

    assert _confirm(client, "INT-X:S1", "S1").status_code == 200
    resp = _settle(client, "INT-X:S1")
    assert resp.status_code == 201, resp.text
    settlement = resp.json()["settlement"]

    cap = settlement["capabilities"]["welding"]
    assert cap["required_seconds"] == 5400
    assert cap["original_seconds"] == 1800
    assert cap["substitute_seconds"] == 3600
    assert cap["total_seconds"] == 5400
    assert cap["fulfilled"] is True
    # 原活动已完成部分与折算后的补偿部分分别保留来源。
    assert settlement["sources"] == [
        {"source": "original", "activity_id": "A1", "seconds": 1800},
        {
            "source": "substitute",
            "item_id": item["item_id"],
            "slot_id": "SL-ONLINE",
            "source_plan_version": "P-ONLINE-2024",
            "capability": "welding",
            "seconds": 3600,
        },
    ]
    assert settlement["total_seconds"] == 5400


def test_resume_releases_only_unstarted_and_keeps_sources(client):
    """原活动恢复：只释放尚未开始的补偿，已完成部分保留并计入结算。"""
    _create_plan(client)
    _register(client, "INT-R")
    _impacts(
        client,
        "INT-R",
        [
            {
                "student_id": "S1",
                "activity_id": "A1",
                "capabilities": {"welding": 7200},
                "completed": {"welding": 1800},
            }
        ],
    )
    _generate(
        client,
        "INT-R",
        [_slot("SL-1", "welding", "2024-04-02T09:00:00+08:00", "2024-04-02T11:00:00+08:00")],
    )
    assert _confirm(client, "INT-R:S1", "S1").status_code == 200

    # 10:00 恢复原活动：补偿项 09:00-10:30 跨越恢复点，已完成 3600 秒保留。
    resp = client.post(
        "/api/interruptions/INT-R/resume",
        json={"resumed_at": "2024-04-02T10:00:00+08:00"},
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert summary["status"] == "resumed"
    assert summary["items_retained"] == 1
    assert summary["items_released"] == 0
    assert summary["retained_seconds"] == 3600
    assert summary["released_seconds"] == 1800

    plan = client.get("/api/compensation-plans/INT-R:S1").json()
    item = plan["items"][0]
    assert item["status"] == "retained"
    assert item["retained_seconds"] == 3600
    assert item["released_seconds"] == 1800
    assert "resume_applied" in [a["action"] for a in plan["audit"]]

    settlement = _settle(client, "INT-R:S1").json()["settlement"]
    cap = settlement["capabilities"]["welding"]
    assert cap["original_seconds"] == 1800
    assert cap["substitute_seconds"] == 3600
    assert cap["released_seconds"] == 1800
    assert cap["total_seconds"] == 5400
    sources = {(s["source"], s.get("slot_id")): s["seconds"] for s in settlement["sources"]}
    assert sources == {("original", None): 1800, ("substitute", "SL-1"): 3600}

    # 恢复后中断进入终态：名单、生成、重复恢复均被拒绝。
    assert _impacts(client, "INT-R", [{"student_id": "S9", "activity_id": "A1", "capabilities": {}}]).status_code == 409
    assert _generate(client, "INT-R", []).status_code == 409
    assert (
        client.post(
            "/api/interruptions/INT-R/resume",
            json={"resumed_at": "2024-04-02T12:00:00+08:00"},
        ).status_code
        == 409
    )


def test_restart_recovery_resume_and_settle(client, db):
    """重启恢复：状态全部落库，新会话（模拟重启）可继续恢复与结算。"""
    _create_plan(client)
    _register(client, "INT-RE")
    _impacts(
        client,
        "INT-RE",
        [
            {
                "student_id": "S1",
                "activity_id": "A1",
                "capabilities": {"welding": 18000},
                "completed": {},
            }
        ],
    )
    _generate(
        client,
        "INT-RE",
        [
            _slot("SL-A", "welding", "2024-04-02T08:00:00+08:00", "2024-04-02T10:00:00+08:00"),
            _slot("SL-B", "welding", "2024-04-02T10:00:00+08:00", "2024-04-02T12:00:00+08:00"),
            _slot("SL-C", "welding", "2024-04-02T14:00:00+08:00", "2024-04-02T15:00:00+08:00"),
        ],
    )
    assert _confirm(client, "INT-RE:S1", "S1").status_code == 200

    # 模拟服务重启：用同一数据库文件创建全新的引擎与会话。
    restart_engine = create_engine(
        "sqlite:///./practice_hours_test.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    RestartSession = sessionmaker(
        bind=restart_engine, autoflush=False, autocommit=False, future=True
    )
    try:
        session = RestartSession()
        try:
            summary = interruption_services.resume_interruption(
                session,
                "INT-RE",
                resumed_at=datetime.fromisoformat("2024-04-02T11:00:00+08:00"),
            )
        finally:
            session.close()
        assert summary["items_retained"] == 2
        assert summary["items_released"] == 1
        assert summary["retained_seconds"] == 7200 + 3600
        # SL-B 跨越恢复点被拆分（释放 3600），SL-C 尚未开始整体释放（3600）。
        assert summary["released_seconds"] == 7200

        # 重启后的另一个新会话继续结算。
        session2 = RestartSession()
        try:
            settlement, created = interruption_services.settle_plan(
                session2, "INT-RE:S1"
            )
        finally:
            session2.close()
        assert created is True
        assert settlement["substitute_seconds"] == 10800
        assert settlement["released_seconds"] == 7200
        assert settlement["total_seconds"] == 10800
    finally:
        restart_engine.dispose()

    # 重启期间的写入对 API 可见：方案已结算，审计完整。
    db.expire_all()
    plan = client.get("/api/compensation-plans/INT-RE:S1").json()
    assert plan["status"] == "settled"
    assert [a["action"] for a in plan["audit"]] == [
        "generate",
        "confirm",
        "resume_applied",
        "settle",
    ]
    statuses = {i["slot_id"]: i["status"] for i in plan["items"]}
    assert statuses == {"SL-A": "retained", "SL-B": "retained", "SL-C": "released"}

    fetched = client.get("/api/compensation-plans/INT-RE:S1/settlement").json()
    assert fetched["settled_at"] == settlement["settled_at"]
    assert fetched["total_seconds"] == 10800


def test_adjust_records_audit_and_guards(client):
    """方案调整：保留审计轨迹，受版本与状态约束。"""
    _create_plan(client)
    _register(client, "INT-A")
    _impacts(
        client,
        "INT-A",
        [
            {
                "student_id": "S1",
                "activity_id": "A1",
                "capabilities": {"welding": 7200},
                "completed": {},
            }
        ],
    )
    _generate(
        client,
        "INT-A",
        [_slot("SL-1", "welding", "2024-04-02T09:00:00+08:00", "2024-04-02T11:00:00+08:00")],
    )
    plan_id = "INT-A:S1"
    item_id = client.get(f"/api/compensation-plans/{plan_id}").json()["items"][0]["item_id"]

    # 延长时段并改为跨方案折算 0.5，计入秒数重新折算。
    resp = client.post(
        f"/api/compensation-plans/{plan_id}/adjust",
        json={
            "actor_id": "coordinator-1",
            "reason": "改用线上替代课程",
            "item_updates": [
                {
                    "item_id": item_id,
                    "end_at": "2024-04-02T12:00:00+08:00",
                    "conversion_ratio": 0.5,
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["version"] == 2
    item = plan["items"][0]
    assert item["raw_seconds"] == 10800
    assert item["credited_seconds"] == 5400
    assert plan["audit"][-1]["action"] == "adjust"
    assert plan["audit"][-1]["actor_id"] == "coordinator-1"

    # 版本冲突与未知补偿项被拒绝。
    stale = client.post(
        f"/api/compensation-plans/{plan_id}/adjust",
        json={
            "actor_id": "coordinator-1",
            "reason": "过期版本",
            "expected_version": 1,
            "item_updates": [],
        },
    )
    assert stale.status_code == 409
    unknown = client.post(
        f"/api/compensation-plans/{plan_id}/adjust",
        json={
            "actor_id": "coordinator-1",
            "reason": "未知补偿项",
            "item_updates": [{"item_id": "NOPE", "conversion_ratio": 1.0}],
        },
    )
    assert unknown.status_code == 400

    # 确认后仍可调整；结算后进入终态，不可再调整。
    assert _confirm(client, plan_id, "S1").status_code == 200
    ok = client.post(
        f"/api/compensation-plans/{plan_id}/adjust",
        json={
            "actor_id": "coordinator-2",
            "reason": "确认后微调比例",
            "expected_version": 3,
            "item_updates": [{"item_id": item_id, "conversion_ratio": 2.0}],
        },
    )
    assert ok.status_code == 200
    # 折算上限仍受缺口约束，不会重复计入。
    assert ok.json()["items"][0]["credited_seconds"] == 7200

    assert _settle(client, plan_id).status_code == 201
    settled = client.post(
        f"/api/compensation-plans/{plan_id}/adjust",
        json={"actor_id": "coordinator-2", "reason": "结算后调整", "item_updates": []},
    )
    assert settled.status_code == 409


def test_settle_requires_confirmation_and_is_idempotent(client):
    """结算：必须先确认锁定，且结果持久化、重复结算幂等。"""
    _create_plan(client)
    _register(client, "INT-S")
    _impacts(
        client,
        "INT-S",
        [
            {
                "student_id": "S1",
                "activity_id": "A1",
                "capabilities": {"welding": 3600},
                "completed": {},
            }
        ],
    )
    _generate(
        client,
        "INT-S",
        [_slot("SL-1", "welding", "2024-04-02T09:00:00+08:00", "2024-04-02T10:00:00+08:00")],
    )
    plan_id = "INT-S:S1"

    # 未确认不能结算。
    assert _settle(client, plan_id).status_code == 409

    assert _confirm(client, plan_id, "S1").status_code == 200
    first = _settle(client, plan_id)
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    settled_at = first.json()["settlement"]["settled_at"]

    # 重复结算返回同一份结果，不重复写入。
    second = _settle(client, plan_id)
    assert second.json()["created"] is False
    assert second.json()["settlement"]["settled_at"] == settled_at

    # 已结算方案不能再确认或重复迁移。
    assert _confirm(client, plan_id, "S1").status_code == 409
    plan = client.get(f"/api/compensation-plans/{plan_id}").json()
    assert plan["status"] == "settled"
    assert [a["action"] for a in plan["audit"]] == ["generate", "confirm", "settle"]


def test_fully_completed_impact_needs_no_compensation(client):
    """避免重复计入：原活动已完成部分已满足要求时不产生补偿缺口。"""
    _create_plan(client)
    _register(client, "INT-F")
    _impacts(
        client,
        "INT-F",
        [
            {
                "student_id": "S1",
                "activity_id": "A1",
                "capabilities": {"welding": 3600},
                "completed": {"welding": 3600},
            }
        ],
    )
    resp = _generate(
        client,
        "INT-F",
        [_slot("SL-1", "welding", "2024-04-02T09:00:00+08:00", "2024-04-02T10:00:00+08:00")],
    )
    plan = resp.json()["plans"][0]
    assert plan["gaps"] == {}
    assert plan["items"] == []

    assert _confirm(client, "INT-F:S1", "S1").status_code == 200
    settlement = _settle(client, "INT-F:S1").json()["settlement"]
    cap = settlement["capabilities"]["welding"]
    assert cap["total_seconds"] == 3600
    assert cap["substitute_seconds"] == 0
    assert cap["fulfilled"] is True
    assert settlement["sources"] == [
        {"source": "original", "activity_id": "A1", "seconds": 3600}
    ]
