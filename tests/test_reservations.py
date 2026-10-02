"""移液体积预留：建立、幂等、占用、批次绑定兑现与取消。"""

import threading
from decimal import Decimal

from app import ledger


def register(client, well, sample, volume):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == 201, resp.text
    return resp.json()


def reserve(client, wells, request_key="res-1"):
    """wells: list[(well, volume_str)]"""
    return client.post(
        "/reservations",
        json={
            "request_key": request_key,
            "wells": [{"well": w, "volume": v} for w, v in wells],
        },
    )


def cancel(client, reservation_id, request_key="cancel-1"):
    return client.post(
        f"/reservations/{reservation_id}/cancellations",
        json={"request_key": request_key},
    )


def batch(client, request_key, operations, reservation_id=None):
    body = {"request_key": request_key, "operations": operations}
    if reservation_id is not None:
        body["reservation_id"] = reservation_id
    return client.post("/batches", json=body)


def op(source, destination, volume):
    return {"source": source, "destination": destination, "volume": volume}


# ------------------------------------------------ 建立预留


def test_reserve_multiple_wells_success(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    resp = reserve(client, [("A1", "40"), ("A2", "30.5")], request_key="r-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "reserved"
    assert body["batch_id"] is None
    assert body["consumed_at"] is None
    assert body["replayed"] is False
    assert body["cancellation"] is None
    assert body["wells"] == [
        {"well": "A1", "volume": "40"},
        {"well": "A2", "volume": "30.5"},
    ]

    # 孔位视图暴露被预留占用量，液体本身不变
    a1 = client.get("/wells/A1").json()
    assert a1["volume"] == "100"
    assert a1["reserved_volume"] == "40"
    a2 = client.get("/wells/A2").json()
    assert a2["volume"] == "60"
    assert a2["reserved_volume"] == "30.5"

    # 列表与单查
    listing = client.get("/reservations").json()["reservations"]
    assert [r["reservation_id"] for r in listing] == [body["reservation_id"]]
    got = client.get(f"/reservations/{body['reservation_id']}").json()
    assert got["wells"] == body["wells"]


def test_reserve_unknown_well_is_404_and_no_partial_state(client):
    register(client, "A1", "S1", "100")
    resp = reserve(client, [("A1", "10"), ("Z9", "5")], request_key="r-404")
    assert resp.status_code == 404, resp.text
    # 不留部分预留、不消耗 request_key
    assert client.get("/wells/A1").json()["reserved_volume"] == "0"
    assert client.get("/reservations").json()["reservations"] == []
    assert reserve(client, [("A1", "10")], request_key="r-404").status_code == 201


def test_reserve_duplicate_well_in_request_is_422(client):
    register(client, "A1", "S1", "100")
    resp = reserve(client, [("A1", "10"), ("a1", "20")], request_key="r-dup")
    assert resp.status_code == 422, resp.text
    assert "重复" in resp.text
    assert client.get("/reservations").json()["reservations"] == []


def test_reserve_validation(client):
    register(client, "A1", "S1", "100")
    assert reserve(client, [], request_key="r-empty").status_code == 422
    assert reserve(client, [("A1", "0")], request_key="r-zero").status_code == 422
    assert reserve(client, [("A1", "-1")], request_key="r-neg").status_code == 422
    assert client.post(
        "/reservations", json={"request_key": " ", "wells": [{"well": "A1", "volume": "1"}]}
    ).status_code == 422


def test_reserve_insufficient_leaves_no_partial_state(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    # 第一次成功预留
    assert reserve(client, [("A1", "40"), ("A2", "30")], request_key="r-1").status_code == 201

    # A1 只剩 60 可用：请求 61 整次失败；A2 的 10 也不留
    resp = reserve(client, [("A1", "61"), ("A2", "10")], request_key="r-2")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_insufficient"
    assert detail["conflicting_wells"] == [
        {"well": "A1", "available": "60", "reserved": "40", "requested": "61"}
    ]
    assert client.get("/wells/A2").json()["reserved_volume"] == "30"
    assert len(client.get("/reservations").json()["reservations"]) == 1

    # 冲突不消耗 request_key：缩到 60 后同键成功（恰好等于可用量）
    ok = reserve(client, [("A1", "60")], request_key="r-2")
    assert ok.status_code == 201, ok.text
    assert client.get("/wells/A1").json()["reserved_volume"] == "100"
    # 此后再预留 1 都不行
    again = reserve(client, [("A1", "0.000000001")], request_key="r-3")
    assert again.status_code == 409
    assert again.json()["detail"]["conflicting_wells"][0]["available"] == "0"


def test_reserve_uses_decimal_quantum(client):
    register(client, "A1", "S1", "0.000000010")
    # 5 纳升 + 3 纳升 = 8，剩 2
    assert reserve(client, [("A1", "0.000000005")], request_key="r-n1").status_code == 201
    assert reserve(client, [("A1", "0.000000003")], request_key="r-n2").status_code == 201
    state = client.get("/wells/A1").json()
    assert Decimal(state["reserved_volume"]) == Decimal("0.000000008")
    resp = reserve(client, [("A1", "0.000000003")], request_key="r-n3")
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicting_wells"][0]["available"] == "0.000000002"


def test_reserve_idempotent_replay(client):
    register(client, "A1", "S1", "100")
    first = reserve(client, [("A1", "40")], request_key="r-idem")
    assert first.status_code == 201

    second = reserve(client, [("A1", "40")], request_key="r-idem")
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["reservation_id"] == first.json()["reservation_id"]
    # 不会重复占用
    assert client.get("/wells/A1").json()["reserved_volume"] == "40"
    assert len(client.get("/reservations").json()["reservations"]) == 1


def test_reserve_same_key_other_target_conflicts(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    assert reserve(client, [("A1", "40")], request_key="r-key").status_code == 201

    # 换孔
    resp = reserve(client, [("A2", "40")], request_key="r-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_wells"] == [{"well": "A1", "volume": "40"}]

    # 同孔换量
    resp = reserve(client, [("A1", "30")], request_key="r-key")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "request_key_conflict"

    # 大小写不影响同目标判定
    replay = reserve(client, [("a1", "40")], request_key="r-key")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    # 状态不变
    assert client.get("/wells/A2").json()["reserved_volume"] == "0"
    assert len(client.get("/reservations").json()["reservations"]) == 1


def test_reserve_quarantined_well_rejected_atomically(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    client.post(
        "/quarantines",
        json={"request_key": "q-1", "case_id": "CASE-1", "reason": "污染", "wells": ["A1"]},
    )
    resp = reserve(client, [("A1", "10"), ("A2", "10")], request_key="r-q")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "well_quarantined"
    assert detail["quarantined_wells"] == [{"well": "A1", "case_id": "CASE-1"}]
    # A2 未被部分预留，request_key 未消耗
    assert client.get("/wells/A2").json()["reserved_volume"] == "0"
    assert reserve(client, [("A2", "10")], request_key="r-q").status_code == 201


# ------------------------------------------------ 未绑定批次与补液


def test_unbound_batch_cannot_use_reserved_volume(client):
    register(client, "A1", "S1", "100")
    assert reserve(client, [("A1", "40")], request_key="r-1").status_code == 201

    # 未绑定预留：可用只有 60
    resp = batch(client, "b-1", [op("A1", "B1", "61")])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["well"] == "A1"
    assert detail["available"] == "60"
    assert detail["requested"] == "61"
    # 失败批次落库（既有语义），但 A1 液体不变、预留不消耗
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    rid = client.get("/reservations").json()["reservations"][0]["reservation_id"]
    assert client.get(f"/reservations/{rid}").json()["status"] == "reserved"

    # 60 可以
    ok = batch(client, "b-2", [op("A1", "B1", "60")])
    assert ok.status_code == 201, ok.text
    assert client.get("/wells/A1").json()["volume"] == "40"
    # 剩余 40 全部被预留占用，再吸 1 也不行
    assert batch(client, "b-3", [op("A1", "C1", "1")]).status_code == 409


def test_unbound_batch_per_step_checks_reserved_volume(client):
    # 同一批次内多步：第一步吸走可用部分后，第二步可用量随之减少
    register(client, "A1", "S1", "100")
    assert reserve(client, [("A1", "70")], request_key="r-1").status_code == 201
    resp = batch(
        client,
        "b-multi",
        [op("A1", "B1", "20"), op("A1", "B2", "11")],  # 可用 30：20 后剩 10
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["available"] == "10"
    assert detail["requested"] == "11"
    # 整批回滚
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_topup_cannot_free_reserved_but_adds_usable_volume(client):
    # 普通补液本身不会动用（减少）被预留体积；补液后未预留部分增加，可被普通批次使用
    register(client, "A1", "S1", "100")
    assert reserve(client, [("A1", "100")], request_key="r-full").status_code == 201
    assert batch(client, "b-blocked", [op("A1", "B1", "1")]).status_code == 409

    body = register(client, "A1", "S1", "20")
    assert body["volume"] == "120"
    assert body["reserved_volume"] == "100"
    ok = batch(client, "b-topup", [op("A1", "B1", "20")])
    assert ok.status_code == 201, ok.text
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/A1").json()["reserved_volume"] == "100"


# ------------------------------------------------ 绑定预留兑现


def test_batch_with_reservation_id_consumes_reservation(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    rid = reserve(client, [("A1", "40"), ("A2", "30")], request_key="r-1").json()["reservation_id"]

    resp = batch(
        client,
        "b-bound",
        [op("A1", "C1", "40"), op("A2", "C1", "30")],
        reservation_id=rid,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "committed"
    assert body["reservation_id"] == rid

    # 同一事务标记已消费
    reservation = client.get(f"/reservations/{rid}").json()
    assert reservation["status"] == "consumed"
    assert reservation["batch_id"] == body["batch_id"]
    assert reservation["consumed_at"]

    # 液体按预定量移走，孔位不再有预留占用
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/A1").json()["reserved_volume"] == "0"
    c1 = client.get("/wells/C1").json()
    assert c1["volume"] == "70"
    assert {c["sample"]: c["amount"] for c in c1["composition"]} == {"S1": "40", "S2": "30"}


def test_bound_batch_can_use_reserved_volume_despite_other_reservations(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]
    # 另一生效预留占 30，未绑定可用只剩 30
    assert reserve(client, [("A1", "30")], request_key="r-2").status_code == 201
    assert batch(client, "b-plain", [op("A1", "X1", "31")]).status_code == 409

    # 绑定 r-1 的批次可动用 r-1 自己的 40（仍扣除 r-2 的 30）
    ok = batch(client, "b-r1", [op("A1", "X1", "40")], reservation_id=rid)
    assert ok.status_code == 201, ok.text
    assert client.get("/wells/A1").json()["volume"] == "60"
    r2 = client.get("/reservations").json()["reservations"][1]
    assert r2["status"] == "reserved"  # 另一个预留完好
    # r-2 仍保证其 30 可取
    ok2 = batch(client, "b-r2", [op("A1", "X2", "30")], reservation_id=r2["reservation_id"])
    assert ok2.status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "30"


def test_bound_batch_mismatch_fails_without_consuming(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    rid = reserve(client, [("A1", "40"), ("A2", "30")], request_key="r-1").json()["reservation_id"]

    # 少一个孔
    resp = batch(client, "b-miss-1", [op("A1", "C1", "40")], reservation_id=rid)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_mismatch"
    assert detail["wells"] == [{"well": "A2", "reserved": "30", "requested": "0"}]

    # 量不符
    resp = batch(
        client,
        "b-miss-2",
        [op("A1", "C1", "39"), op("A2", "C1", "30")],
        reservation_id=rid,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["wells"] == [
        {"well": "A1", "reserved": "40", "requested": "39"}
    ]

    # 多取（同孔两步累计超出）
    resp = batch(
        client,
        "b-miss-3",
        [op("A1", "C1", "40"), op("A1", "C2", "1"), op("A2", "C1", "30")],
        reservation_id=rid,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["wells"] == [
        {"well": "A1", "reserved": "40", "requested": "41"}
    ]

    # 全部失败：不留批次、不消耗 request_key、不消耗预留、液体不动
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/A2").json()["volume"] == "60"
    assert client.get("/wells/C1").status_code == 404
    assert client.get("/batches").json()["batches"] == []
    assert client.get(f"/reservations/{rid}").json()["status"] == "reserved"

    # request_key 未消耗：改为完全兑现后同键成功
    ok = batch(
        client,
        "b-miss-1",
        [op("A1", "C1", "40"), op("A2", "C1", "30")],
        reservation_id=rid,
    )
    assert ok.status_code == 201, ok.text


def test_bound_batch_unknown_or_inactive_reservation(client):
    register(client, "A1", "S1", "100")

    # 不存在
    resp = batch(client, "b-no-res", [op("A1", "B1", "1")], reservation_id=999)
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "reservation_not_found"
    assert client.get("/batches").json()["batches"] == []

    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]
    # 取消后再绑定
    assert cancel(client, rid, request_key="c-1").status_code == 201
    resp = batch(client, "b-cancelled", [op("A1", "B1", "40")], reservation_id=rid)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_not_active"
    assert detail["status"] == "cancelled"

    # 消费后再绑定
    rid2 = reserve(client, [("A1", "30")], request_key="r-2").json()["reservation_id"]
    assert batch(client, "b-consume", [op("A1", "B1", "30")], reservation_id=rid2).status_code == 201
    resp = batch(client, "b-again", [op("A1", "B2", "30")], reservation_id=rid2)
    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "consumed"
    # 失败不留批次、不消耗 request_key
    assert client.post(
        "/batches",
        json={"request_key": "b-again", "operations": [op("A1", "B2", "1")]},
    ).status_code == 201


def test_bound_batch_replay_same_request_key(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]
    payload = {
        "request_key": "b-idem",
        "operations": [op("A1", "B1", "40")],
        "reservation_id": rid,
    }
    first = client.post("/batches", json=payload)
    assert first.status_code == 201
    second = client.post("/batches", json=payload)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["batch_id"] == first.json()["batch_id"]
    # 只兑现一次
    assert client.get("/wells/A1").json()["volume"] == "60"


def test_bound_batch_quarantine_conflict_does_not_consume(client):
    register(client, "A1", "S1", "100")
    register(client, "B1", "S0", "5")
    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]
    client.post(
        "/quarantines",
        json={"request_key": "q-1", "case_id": "CASE-9", "reason": "x", "wells": ["B1"]},
    )
    resp = batch(client, "b-q", [op("A1", "B1", "40")], reservation_id=rid)
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    assert client.get(f"/reservations/{rid}").json()["status"] == "reserved"
    assert client.get("/wells/A1").json()["volume"] == "100"


# ------------------------------------------------ 取消


def test_cancel_reservation_releases_volume(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]

    resp = cancel(client, rid, request_key="c-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reservation_id"] == rid
    assert body["request_key"] == "c-1"
    assert body["reservation"]["status"] == "cancelled"

    assert client.get("/wells/A1").json()["reserved_volume"] == "0"
    reservation = client.get(f"/reservations/{rid}").json()
    assert reservation["status"] == "cancelled"
    assert reservation["cancellation"]["cancellation_id"] == body["cancellation_id"]
    assert reservation["cancellation"]["request_key"] == "c-1"

    # 释放后普通批次可立即使用
    ok = batch(client, "b-freed", [op("A1", "B1", "100")])
    assert ok.status_code == 201, ok.text


def test_cancel_consumed_reservation_rejected(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]
    assert batch(client, "b-1", [op("A1", "B1", "40")], reservation_id=rid).status_code == 201

    resp = cancel(client, rid, request_key="c-late")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_not_active"
    assert detail["status"] == "consumed"
    # 预留状态不变
    assert client.get(f"/reservations/{rid}").json()["status"] == "consumed"


def test_cancel_already_cancelled_requires_same_key_replay(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]
    first = cancel(client, rid, request_key="c-1")
    assert first.status_code == 201

    # 同键重放
    replay = cancel(client, rid, request_key="c-1")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["cancellation_id"] == first.json()["cancellation_id"]

    # 新键再次取消 -> 409
    resp = cancel(client, rid, request_key="c-2")
    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "cancelled"


def test_cancel_request_key_conflicts_across_reservations(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    rid1 = reserve(client, [("A1", "10")], request_key="r-1").json()["reservation_id"]
    rid2 = reserve(client, [("A2", "10")], request_key="r-2").json()["reservation_id"]
    assert cancel(client, rid1, request_key="c-key").status_code == 201

    resp = cancel(client, rid2, request_key="c-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_reservation_id"] == rid1
    # rid2 未取消
    assert client.get(f"/reservations/{rid2}").json()["status"] == "reserved"


def test_cancel_unknown_reservation_is_404(client):
    resp = cancel(client, 42, request_key="c-x")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "reservation_not_found"


# ------------------------------------------------ 与隔离/撤销的交互


def test_quarantine_rejected_when_active_reservations_block(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    rid = reserve(client, [("A1", "40"), ("A2", "20")], request_key="r-1").json()["reservation_id"]

    resp = client.post(
        "/quarantines",
        json={"request_key": "q-1", "case_id": "CASE-1", "reason": "x", "wells": ["A1", "A2"]},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "active_reservations"
    blockers = {b["well"]: b["reservations"] for b in detail["blocking_wells"]}
    assert blockers["A1"] == [
        {"reservation_id": rid, "request_key": "r-1", "volume": "40"}
    ]
    assert blockers["A2"] == [
        {"reservation_id": rid, "request_key": "r-1", "volume": "20"}
    ]
    # 不留部分隔离
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/wells/A2").json()["quarantine"] is None

    # 取消预留后同键隔离成功（取消与隔离串行）
    assert cancel(client, rid, request_key="cancel-r1").status_code == 201
    ok = client.post(
        "/quarantines",
        json={"request_key": "q-1", "case_id": "CASE-1", "reason": "x", "wells": ["A1", "A2"]},
    )
    assert ok.status_code == 201


def test_quarantine_succeeds_after_reservation_consumed(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]
    assert batch(client, "b-1", [op("A1", "B1", "40")], reservation_id=rid).status_code == 201
    resp = client.post(
        "/quarantines",
        json={"request_key": "q-1", "case_id": "CASE-1", "reason": "x", "wells": ["A1"]},
    )
    assert resp.status_code == 201, resp.text


def test_reversal_blocked_when_restored_volume_undercuts_reservation(client):
    register(client, "A1", "S1", "100")
    # 批次新建目标孔 B1=40（批前快照为 NULL）
    bid = batch(client, "b-1", [op("A1", "B1", "40")]).json()["batch_id"]
    # 在 B1 上预留全部 40
    rid = reserve(client, [("B1", "40")], request_key="r-1").json()["reservation_id"]

    resp = client.post(f"/batches/{bid}/reversals", json={"request_key": "rev-1"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_undercut"
    assert detail["undercut_wells"] == [
        {"well": "B1", "restored_volume": "0", "reserved_volume": "40"}
    ]
    # 液体、预留、审计均不变
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert client.get(f"/reservations/{rid}").json()["status"] == "reserved"
    assert client.post(
        f"/batches/{bid}/reversals", json={"request_key": "rev-1"}
    ).status_code == 409  # request_key 未消耗？撤销冲突不消耗

    # 取消预留后同键撤销成功
    assert cancel(client, rid, request_key="c-1").status_code == 201
    ok = client.post(f"/batches/{bid}/reversals", json={"request_key": "rev-1"})
    assert ok.status_code == 201, ok.text
    assert client.get("/wells/B1").status_code == 404


def test_reversal_with_consumed_reservation_allowed(client):
    register(client, "A1", "S1", "100")
    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]
    bid = batch(client, "b-1", [op("A1", "B1", "40")], reservation_id=rid).json()["batch_id"]
    # 已消费预留不阻塞撤销
    resp = client.post(f"/batches/{bid}/reversals", json={"request_key": "rev-1"})
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


# ------------------------------------------------ 并发串行（BEGIN IMMEDIATE）


class _Gate:
    """线程门闩：工作线程进入临界区后置位 entered，等待 release。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_inside(self, timeout=10.0):
        assert self.entered.wait(timeout), "工作线程未在超时内进入门闩"


def _run_in_thread(target):
    result = {}

    def runner():
        try:
            result["value"] = target()
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    return thread, result


def _join(thread, result, timeout=30.0):
    thread.join(timeout)
    assert not thread.is_alive(), "工作线程超时未结束（可能死锁）"
    if "error" in result:
        raise result["error"]
    return result["value"]


def test_concurrent_reservation_and_unbound_batch_reservation_first(client, monkeypatch):
    """预留先持锁提交：未绑定批次在锁后读到占用，409 且不消耗批次键。"""
    register(client, "A1", "S1", "100")
    gate = _Gate()
    original = ledger.apply_reservation

    def gated(conn, request_key, wells):
        gate.entered.set()
        assert gate.release.wait(10.0), "预留线程未被释放"
        return original(conn, request_key, wells)

    monkeypatch.setattr(ledger, "apply_reservation", gated)

    t, r = _run_in_thread(lambda: reserve(client, [("A1", "100")], request_key="r-conc"))
    gate.wait_inside()

    b_t, b_r = _run_in_thread(lambda: batch(client, "b-conc", [op("A1", "B1", "1")]))
    threading.Event().wait(0.3)
    assert b_t.is_alive(), "批次应在预留的写锁上排队"

    gate.release.set()
    resp = _join(t, r)
    b_resp = _join(b_t, b_r)
    assert resp.status_code == 201
    assert b_resp.status_code == 409, b_resp.text
    assert b_resp.json()["detail"]["available"] == "0"
    # 失败批次落库（既有语义），但液体未动
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_concurrent_reservation_and_unbound_batch_batch_first(client, monkeypatch):
    """未绑定批次先持锁提交：预留在锁后读到减少后的体积，不足即整次失败。"""
    register(client, "A1", "S1", "100")
    gate = _Gate()
    original = ledger.execute_batch

    def gated(conn, request_key, operations, reservation_id=None):
        gate.entered.set()
        assert gate.release.wait(10.0), "批次线程未被释放"
        return original(conn, request_key, operations, reservation_id=reservation_id)

    monkeypatch.setattr(ledger, "execute_batch", gated)

    b_t, b_r = _run_in_thread(lambda: batch(client, "b-conc", [op("A1", "B1", "70")]))
    gate.wait_inside()

    r_t, r_r = _run_in_thread(lambda: reserve(client, [("A1", "50")], request_key="r-conc"))
    threading.Event().wait(0.3)
    assert r_t.is_alive(), "预留应在批次的写锁上排队"

    gate.release.set()
    b_resp = _join(b_t, b_r)
    r_resp = _join(r_t, r_r)
    assert b_resp.status_code == 201
    assert r_resp.status_code == 409
    detail = r_resp.json()["detail"]
    assert detail["reason"] == "reservation_insufficient"
    assert detail["conflicting_wells"][0]["available"] == "30"
    assert client.get("/wells/A1").json()["reserved_volume"] == "0"


def test_concurrent_cancel_and_quarantine_one_ordering(client, monkeypatch):
    """取消先持锁提交：隔离在锁后看到孔已无生效预留，正常建立。"""
    register(client, "A1", "S1", "100")
    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]
    gate = _Gate()
    original = ledger.apply_cancellation

    def gated(conn, reservation_id, request_key):
        gate.entered.set()
        assert gate.release.wait(10.0), "取消线程未被释放"
        return original(conn, reservation_id, request_key)

    monkeypatch.setattr(ledger, "apply_cancellation", gated)

    c_t, c_r = _run_in_thread(lambda: cancel(client, rid, request_key="c-conc"))
    gate.wait_inside()

    q_t, q_r = _run_in_thread(
        lambda: client.post(
            "/quarantines",
            json={"request_key": "q-conc", "case_id": "CASE-1", "reason": "x", "wells": ["A1"]},
        )
    )
    threading.Event().wait(0.3)
    assert q_t.is_alive(), "隔离应在取消的写锁上排队"

    gate.release.set()
    c_resp = _join(c_t, c_r)
    q_resp = _join(q_t, q_r)
    assert c_resp.status_code == 201
    assert q_resp.status_code == 201, q_resp.text
    assert client.get("/wells/A1").json()["quarantine"]["case_id"] == "CASE-1"


def test_concurrent_consume_and_quarantine_quarantine_waits_then_succeeds(client, monkeypatch):
    """兑现先持锁提交：隔离在锁后看到预留已消费，正常建立。"""
    register(client, "A1", "S1", "100")
    rid = reserve(client, [("A1", "40")], request_key="r-1").json()["reservation_id"]
    gate = _Gate()
    original = ledger.execute_batch

    def gated(conn, request_key, operations, reservation_id=None):
        gate.entered.set()
        assert gate.release.wait(10.0), "批次线程未被释放"
        return original(conn, request_key, operations, reservation_id=reservation_id)

    monkeypatch.setattr(ledger, "execute_batch", gated)

    b_t, b_r = _run_in_thread(
        lambda: batch(client, "b-conc", [op("A1", "B1", "40")], reservation_id=rid)
    )
    gate.wait_inside()

    q_t, q_r = _run_in_thread(
        lambda: client.post(
            "/quarantines",
            json={"request_key": "q-conc", "case_id": "CASE-1", "reason": "x", "wells": ["A1"]},
        )
    )
    threading.Event().wait(0.3)
    assert q_t.is_alive()

    gate.release.set()
    b_resp = _join(b_t, b_r)
    q_resp = _join(q_t, q_r)
    assert b_resp.status_code == 201
    assert q_resp.status_code == 201, q_resp.text
    assert client.get(f"/reservations/{rid}").json()["status"] == "consumed"


def test_concurrent_same_reservation_key_replays_once(client):
    """并发同键同目标预留：一个 201 一个重放 200，只占用一次。"""
    register(client, "A1", "S1", "100")
    barrier = threading.Barrier(2)
    threads, results = [], []
    for _ in range(2):
        t, r = _run_in_thread(
            lambda: (barrier.wait(), reserve(client, [("A1", "40")], request_key="r-idem-c"))[1]
        )
        threads.append(t)
        results.append(r)
    responses = [_join(t, r) for t, r in zip(threads, results)]

    assert sorted(resp.status_code for resp in responses) == [200, 201]
    bodies = [resp.json() for resp in responses]
    assert {b["reservation_id"] for b in bodies} == {bodies[0]["reservation_id"]}
    assert client.get("/wells/A1").json()["reserved_volume"] == "40"
