"""端到端与核心逻辑测试。"""
from __future__ import annotations

import copy
import math

import pytest
from fastapi.testclient import TestClient

from app.core import Curve
from app.main import create_app


@pytest.fixture()
def client():
    app = create_app(":memory:")
    with TestClient(app) as c:
        yield c


def base_payload() -> dict:
    """合法场景：S -B1(CB1)-> A -B2(CB2)-> B -B3-> C，另有 S -B4-> D（无断路器）。"""
    return {
        "name": "demo-feeder",
        "source": "S",
        "branches": [
            {"id": "B1", "from": "S", "to": "A",
             "breaker": {"id": "CB1", "curve_id": "C_UP"}},
            {"id": "B2", "from": "A", "to": "B",
             "breaker": {"id": "CB2", "curve_id": "C_MAIN"}},
            {"id": "B3", "from": "B", "to": "C"},
            {"id": "B4", "from": "S", "to": "D"},
        ],
        "curves": [
            {"id": "C_MAIN", "points": [
                {"current": 10, "time": 10},
                {"current": 100, "time": 1},
                {"current": 1000, "time": 0.1},
            ]},
            {"id": "C_UP", "points": [
                {"current": 10, "time": 20},
                {"current": 100, "time": 1.5},
                {"current": 1000, "time": 0.12},
            ]},
        ],
        "faults": [
            # 比值 @10:2.0 @100:1.5 @1000:1.2
            {"id": "F1", "branch_id": "B3",
             "current_min": 10, "current_max": 1000, "r": 1.8},
            {"id": "F2", "branch_id": "B3",
             "current_min": 10, "current_max": 1000, "r": 1.1},
            # 区间右端比值恰为 R（等号合格）
            {"id": "F3", "branch_id": "B2",
             "current_min": 10, "current_max": 100, "r": 1.5},
            # 有主保护而无上级 -> 合格
            {"id": "F4", "branch_id": "B1",
             "current_min": 10, "current_max": 1000, "r": 2.0},
            # 路径上无断路器 -> 无保护（区间无需曲线覆盖）
            {"id": "F5", "branch_id": "B4",
             "current_min": 5, "current_max": 5000, "r": 3.0},
        ],
    }


def create_ok(client: TestClient, payload: dict) -> str:
    resp = client.post("/scenarios", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# 基础流程
# ---------------------------------------------------------------------------

def test_create_and_get_roundtrip(client):
    payload = base_payload()
    sid = create_ok(client, payload)

    created = client.get(f"/scenarios/{sid}")
    assert created.status_code == 200
    body = created.json()
    assert body["id"] == sid
    assert body["name"] == "demo-feeder"
    assert body["created_at"]
    assert body["branches"][0]["from"] == "S"
    assert body["branches"][0]["to"] == "A"
    assert body["branches"][2]["breaker"] is None
    assert len(body["faults"]) == 5

    listing = client.get("/scenarios").json()
    assert len(listing) == 1
    assert listing[0]["id"] == sid
    assert listing[0]["branch_count"] == 4
    assert listing[0]["fault_count"] == 5


def test_root_and_health(client):
    assert client.get("/").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_scenario_is_immutable(client):
    sid = create_ok(client, base_payload())
    assert client.put(f"/scenarios/{sid}", json={}).status_code == 405
    assert client.patch(f"/scenarios/{sid}", json={}).status_code == 405
    assert client.delete(f"/scenarios/{sid}").status_code == 405
    # 重复提交相同内容只会创建新场景，不会覆盖
    other = create_ok(client, base_payload())
    assert other != sid


def test_unknown_scenario_404(client):
    assert client.get("/scenarios/nope").status_code == 404
    assert client.get("/scenarios/nope/verification").status_code == 404


def test_unknown_fault_404(client):
    sid = create_ok(client, base_payload())
    resp = client.get(f"/scenarios/{sid}/faults/NOPE/verification")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 核验结论
# ---------------------------------------------------------------------------

def test_verify_all_faults(client):
    sid = create_ok(client, base_payload())
    resp = client.get(f"/scenarios/{sid}/verification")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scenario_id"] == sid
    assert body["summary"] == {
        "total": 5, "qualified": 3, "unqualified": 1, "unprotected": 1,
    }
    reports = {f["fault_id"]: f for f in body["faults"]}

    # F1: 比值 2.0 -> 1.5 -> 1.2，R=1.8，违规范围 (cross, 1000]
    f1 = reports["F1"]
    assert f1["status"] == "unqualified"
    assert f1["main_breaker_id"] == "CB2"
    assert len(f1["pairs"]) == 1
    pair = f1["pairs"][0]
    assert pair["upstream_breaker_id"] == "CB1"
    assert pair["qualified"] is False
    assert pair["min_ratio"] == pytest.approx(1.2, rel=1e-9)
    assert pair["min_ratio_current"] == pytest.approx(1000.0)
    assert len(pair["violations"]) == 1
    v = pair["violations"][0]
    # 交点：ln 空间线性求根
    d0 = math.log(20) - math.log(10) - math.log(1.8)
    d1 = math.log(1.5) - math.log(1) - math.log(1.8)
    cross = math.exp(math.log(10) + (0 - d0) / (d1 - d0)
                     * (math.log(100) - math.log(10)))
    assert 23.2 < cross < 23.3
    assert v["current_from"] == pytest.approx(cross, rel=1e-9)
    assert v["current_to"] == pytest.approx(1000.0)
    assert v["from_included"] is False   # 交点处等号，合格
    assert v["to_included"] is True      # 区间右端比值 1.2 < 1.8

    # F2: R=1.1，处处合格，最小比值 1.2 @ 1000
    f2 = reports["F2"]
    assert f2["status"] == "qualified"
    assert f2["pairs"][0]["violations"] == []
    assert f2["pairs"][0]["min_ratio"] == pytest.approx(1.2, rel=1e-9)

    # F3: 区间右端比值恰等于 R，等号合格
    f3 = reports["F3"]
    assert f3["status"] == "qualified"
    assert f3["pairs"][0]["min_ratio"] == pytest.approx(1.5, rel=1e-9)
    assert f3["pairs"][0]["min_ratio_current"] == pytest.approx(100.0)
    assert f3["pairs"][0]["violations"] == []

    # F4: 有主保护而无上级 -> 合格
    f4 = reports["F4"]
    assert f4["status"] == "qualified"
    assert f4["main_breaker_id"] == "CB1"
    assert f4["pairs"] == []

    # F5: 路径上无断路器 -> 无保护
    f5 = reports["F5"]
    assert f5["status"] == "unprotected"
    assert f5["main_breaker_id"] is None
    assert f5["pairs"] == []


def test_verify_single_fault_matches_bulk(client):
    sid = create_ok(client, base_payload())
    bulk = client.get(f"/scenarios/{sid}/verification").json()
    single = client.get(f"/scenarios/{sid}/faults/F1/verification").json()
    assert single == next(f for f in bulk["faults"] if f["fault_id"] == "F1")


def test_multiple_upstreams_any_failure_fails_fault(client):
    payload = base_payload()
    payload["branches"] = [
        {"id": "L1", "from": "S", "to": "A",
         "breaker": {"id": "CB1", "curve_id": "C_SLOW"}},
        {"id": "L2", "from": "A", "to": "B",
         "breaker": {"id": "CB2", "curve_id": "C_MID"}},
        {"id": "L3", "from": "B", "to": "C",
         "breaker": {"id": "CB3", "curve_id": "C_FAST"}},
    ]
    payload["curves"] = [
        {"id": "C_FAST", "points": [
            {"current": 10, "time": 1}, {"current": 100, "time": 0.1}]},
        {"id": "C_MID", "points": [
            {"current": 10, "time": 2}, {"current": 100, "time": 0.2}]},
        {"id": "C_SLOW", "points": [
            {"current": 10, "time": 1.5}, {"current": 100, "time": 0.15}]},
    ]
    payload["faults"] = [
        {"id": "FX", "branch_id": "L3",
         "current_min": 10, "current_max": 100, "r": 1.8},
    ]
    sid = create_ok(client, payload)
    report = client.get(f"/scenarios/{sid}/faults/FX/verification").json()
    assert report["status"] == "unqualified"
    assert report["main_breaker_id"] == "CB3"
    pairs = {p["upstream_breaker_id"]: p for p in report["pairs"]}
    assert set(pairs) == {"CB2", "CB1"}
    # CB2 比值恒为 2.0 >= 1.8 合格；CB1 比值恒为 1.5 < 1.8 不合格
    assert pairs["CB2"]["qualified"] is True
    assert pairs["CB2"]["min_ratio"] == pytest.approx(2.0, rel=1e-9)
    assert pairs["CB1"]["qualified"] is False
    assert pairs["CB1"]["min_ratio"] == pytest.approx(1.5, rel=1e-9)
    v = pairs["CB1"]["violations"][0]
    assert (v["current_from"], v["current_to"]) == (10.0, 100.0)
    assert v["from_included"] is True and v["to_included"] is True


def test_min_ratio_tie_takes_smaller_current(client):
    payload = base_payload()
    payload["branches"] = [
        {"id": "L1", "from": "S", "to": "A",
         "breaker": {"id": "CB1", "curve_id": "C_UP2"}},
        {"id": "L2", "from": "A", "to": "B",
         "breaker": {"id": "CB2", "curve_id": "C_MAIN2"}},
    ]
    payload["curves"] = [
        {"id": "C_MAIN2", "points": [
            {"current": 10, "time": 2}, {"current": 100, "time": 1},
            {"current": 1000, "time": 0.5}]},
        {"id": "C_UP2", "points": [
            {"current": 10, "time": 4}, {"current": 100, "time": 2},
            {"current": 1000, "time": 1}]},
    ]
    payload["faults"] = [
        {"id": "FT", "branch_id": "L2",
         "current_min": 10, "current_max": 1000, "r": 1.5},
    ]
    sid = create_ok(client, payload)
    pair = client.get(
        f"/scenarios/{sid}/faults/FT/verification").json()["pairs"][0]
    # 比值恒为 2.0：并列最小取较小电流（区间下界 10）
    assert pair["min_ratio"] == pytest.approx(2.0, rel=1e-9)
    assert pair["min_ratio_current"] == pytest.approx(10.0)
    assert pair["qualified"] is True


def test_single_point_interval(client):
    payload = base_payload()
    payload["faults"] = [
        {"id": "FP", "branch_id": "B3",
         "current_min": 50, "current_max": 50, "r": 1.8},
    ]
    sid = create_ok(client, payload)
    report = client.get(f"/scenarios/{sid}/faults/FP/verification").json()
    pair = report["pairs"][0]
    # I=50 处比值 = 3.2712/2.0 ≈ 1.6356 < 1.8，单点违规
    assert report["status"] == "unqualified"
    assert pair["min_ratio"] == pytest.approx(1.6356, rel=1e-3)
    assert pair["min_ratio_current"] == pytest.approx(50.0)
    v = pair["violations"][0]
    assert v["current_from"] == v["current_to"] == 50.0
    assert v["from_included"] is True and v["to_included"] is True


# ---------------------------------------------------------------------------
# 校验：拓扑
# ---------------------------------------------------------------------------

def _expect_422(client, payload, fragment):
    resp = client.post("/scenarios", json=payload)
    assert resp.status_code == 422, resp.text
    assert fragment in resp.text


def test_reject_cycle(client):
    p = base_payload()
    p["branches"] = [
        {"id": "B1", "from": "S", "to": "A"},
        {"id": "B2", "from": "A", "to": "B"},
        {"id": "B3", "from": "B", "to": "A"},  # 环 + A 入度 2
    ]
    p["faults"] = []
    _expect_422(client, p, "不是树")


def test_reject_diamond_two_parents(client):
    p = base_payload()
    p["branches"] = [
        {"id": "B1", "from": "S", "to": "A"},
        {"id": "B2", "from": "S", "to": "B"},
        {"id": "B3", "from": "A", "to": "C"},
        {"id": "B4", "from": "B", "to": "C"},  # C 入度 2
    ]
    p["faults"] = []
    _expect_422(client, p, "入度")


def test_reject_disconnected(client):
    p = base_payload()
    p["branches"] = [
        {"id": "B1", "from": "S", "to": "A"},
        {"id": "B2", "from": "X", "to": "Y"},  # 与电源不相连
    ]
    p["faults"] = []
    _expect_422(client, p, "不可达")


def test_reject_edge_into_source(client):
    p = base_payload()
    p["branches"] = [
        {"id": "B1", "from": "A", "to": "S"},  # 指向电源
        {"id": "B2", "from": "S", "to": "B"},
    ]
    p["faults"] = []
    _expect_422(client, p, "入边")


def test_reject_unknown_source(client):
    p = base_payload()
    p["source"] = "NOWHERE"
    _expect_422(client, p, "电源节点")


def test_reject_duplicate_ids(client):
    p = base_payload()
    p["branches"].append({"id": "B1", "from": "S", "to": "Z"})
    _expect_422(client, p, "支路 id 重复")

    p = base_payload()
    p["curves"].append(p["curves"][0])
    _expect_422(client, p, "曲线 id 重复")

    p = base_payload()
    p["faults"].append(p["faults"][0])
    _expect_422(client, p, "故障 id 重复")

    p = base_payload()
    p["branches"][2]["breaker"] = {"id": "CB1", "curve_id": "C_MAIN"}
    _expect_422(client, p, "断路器 id 重复")


def test_reject_unknown_curve_reference(client):
    p = base_payload()
    p["branches"][0]["breaker"]["curve_id"] = "GHOST"
    _expect_422(client, p, "不存在的曲线")


# ---------------------------------------------------------------------------
# 校验：曲线与故障
# ---------------------------------------------------------------------------

def test_reject_bad_curves(client):
    p = base_payload()
    p["curves"][0]["points"] = [{"current": 10, "time": 1}]
    _expect_422(client, p, "at least 2")

    p = base_payload()
    p["curves"][0]["points"][1]["current"] = 10  # 非严格递增
    _expect_422(client, p, "严格递增")

    p = base_payload()
    p["curves"][0]["points"][0]["time"] = 0
    _expect_422(client, p, "greater than 0")

    p = base_payload()
    p["curves"][0]["points"][0]["current"] = -5
    _expect_422(client, p, "greater than 0")


def test_reject_bad_faults(client):
    p = base_payload()
    p["faults"][0]["current_min"] = 2000
    p["faults"][0]["current_max"] = 100
    _expect_422(client, p, "下界")

    p = base_payload()
    p["faults"][0]["r"] = 1.0
    _expect_422(client, p, "greater than 1")

    p = base_payload()
    p["faults"][0]["current_min"] = 0
    _expect_422(client, p, "greater than 0")

    p = base_payload()
    p["faults"][0]["branch_id"] = "GHOST"
    _expect_422(client, p, "不存在的支路")


def test_reject_insufficient_curve_coverage(client):
    p = base_payload()
    p["faults"] = [
        {"id": "FC", "branch_id": "B3",
         "current_min": 5, "current_max": 1000, "r": 1.5},
    ]
    _expect_422(client, p, "覆盖不足")

    p = base_payload()
    p["faults"] = [
        {"id": "FC", "branch_id": "B3",
         "current_min": 10, "current_max": 2000, "r": 1.5},
    ]
    _expect_422(client, p, "覆盖不足")


def test_interval_equal_to_curve_range_is_covered(client):
    p = base_payload()
    p["faults"] = [
        {"id": "FC", "branch_id": "B3",
         "current_min": 10, "current_max": 1000, "r": 1.5},
    ]
    create_ok(client, p)  # 边界恰好等于曲线范围：允许（不外推）


# ---------------------------------------------------------------------------
# 核心算法单元测试
# ---------------------------------------------------------------------------

def test_log_log_interpolation_no_extrapolation():
    curve = Curve.from_points("X", [
        type("P", (), {"current": 10.0, "time": 10.0})(),
        type("P", (), {"current": 100.0, "time": 1.0})(),
    ])
    # 几何中点电流 -> 几何中点时间（对数-对数线性）
    mid = math.sqrt(10.0 * 100.0)
    t = math.exp(curve.log_time_at(math.log(mid)))
    assert t == pytest.approx(math.sqrt(10.0 * 1.0), rel=1e-12)
    # 端点
    assert math.exp(curve.log_time_at(math.log(10.0))) == pytest.approx(10.0)
    assert math.exp(curve.log_time_at(math.log(100.0))) == pytest.approx(1.0)


def test_persistence_across_restarts(tmp_path):
    db_file = str(tmp_path / "scenarios.db")
    with TestClient(create_app(db_file)) as c1:
        sid = create_ok(c1, base_payload())
    # 用同一数据库文件重建应用（模拟重启）
    with TestClient(create_app(db_file)) as c2:
        assert c2.get(f"/scenarios/{sid}").status_code == 200
        assert c2.get(f"/scenarios/{sid}/verification").status_code == 200


# ---------------------------------------------------------------------------
# 协调方案（POST /scenarios/{id}/coordination-plan）
# ---------------------------------------------------------------------------

def coord_payload() -> dict:
    """全部故障均有主保护的场景（去掉无保护的 F5）。"""
    p = base_payload()
    p["faults"] = [f for f in p["faults"] if f["id"] != "F5"]
    return p


def post_plan(client: TestClient, sid: str, adjustments: list):
    return client.post(
        f"/scenarios/{sid}/coordination-plan", json={"adjustments": adjustments}
    )


def test_coordination_plan_ok_and_scenario_untouched(client):
    sid = create_ok(client, coord_payload())
    before_scenario = client.get(f"/scenarios/{sid}").json()
    before_verify = client.get(f"/scenarios/{sid}/verification").json()

    resp = post_plan(client, sid, [
        {"breaker_id": "CB2", "k_min": 0.5, "k_max": 2.0},
        {"breaker_id": "CB1", "k_min": 0.5, "k_max": 2.0},
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scenario_id"] == sid
    assert body["status"] == "ok"
    assert body["reasons"] is None

    # F1 约束 x_CB1 - x_CB2 >= ln(1.8/1.2) = ln 1.5 主导：
    # min max|ln k| = ln(1.5)/2，解为 k_CB1 = sqrt(1.5), k_CB2 = 1/sqrt(1.5)
    half = math.log(1.5) / 2
    assert body["objective"] == pytest.approx(half, rel=1e-6)
    mults = body["multipliers"]
    assert [m["breaker_id"] for m in mults] == ["CB1", "CB2"]  # 按 ID 顺序
    assert mults[0]["k"] == pytest.approx(math.sqrt(1.5), rel=1e-6)
    assert mults[0]["ln_k"] == pytest.approx(half, rel=1e-6)
    assert mults[1]["k"] == pytest.approx(1 / math.sqrt(1.5), rel=1e-6)
    assert mults[1]["ln_k"] == pytest.approx(-half, rel=1e-6)

    # 调整后的逐故障核验：全部合格；F1 恰好等号合格
    ver = body["verification"]
    assert ver["summary"] == {
        "total": 4, "qualified": 4, "unqualified": 0, "unprotected": 0,
    }
    f1 = next(f for f in ver["faults"] if f["fault_id"] == "F1")
    assert f1["status"] == "qualified"
    assert f1["pairs"][0]["min_ratio"] == pytest.approx(1.8, rel=1e-9)
    assert f1["pairs"][0]["violations"] == []

    # 原场景与原核验不受影响
    assert client.get(f"/scenarios/{sid}").json() == before_scenario
    assert client.get(f"/scenarios/{sid}/verification").json() == before_verify


def test_coordination_plan_unknown_scenario_404(client):
    resp = post_plan(client, "nope", [{"breaker_id": "CB1", "k_min": 1, "k_max": 2}])
    assert resp.status_code == 404


def test_coordination_plan_rejects_bad_requests(client):
    sid = create_ok(client, coord_payload())
    # 空调整列表
    assert post_plan(client, sid, []).status_code == 422
    # 未知断路器
    r = post_plan(client, sid, [{"breaker_id": "NOPE", "k_min": 1, "k_max": 2}])
    assert r.status_code == 422 and "不存在" in r.text
    # 重复断路器
    r = post_plan(client, sid, [
        {"breaker_id": "CB1", "k_min": 1, "k_max": 2},
        {"breaker_id": "CB1", "k_min": 1, "k_max": 3},
    ])
    assert r.status_code == 422 and "重复" in r.text
    # 倒置区间
    r = post_plan(client, sid, [{"breaker_id": "CB1", "k_min": 2, "k_max": 0.5}])
    assert r.status_code == 422 and "倒置" in r.text
    # 非正倍率
    assert post_plan(client, sid, [
        {"breaker_id": "CB1", "k_min": 0, "k_max": 2}]).status_code == 422
    assert post_plan(client, sid, [
        {"breaker_id": "CB1", "k_min": -1, "k_max": 2}]).status_code == 422
    # 非有限倍率（JSON 字面量 Infinity -> 服务端解析为 inf -> 拒绝）
    r = client.post(
        f"/scenarios/{sid}/coordination-plan",
        content='{"adjustments": [{"breaker_id": "CB1", "k_min": 1, "k_max": Infinity}]}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_coordination_plan_rejects_empty_fault_set(client):
    p = coord_payload()
    p["faults"] = []
    sid = create_ok(client, p)
    r = post_plan(client, sid, [{"breaker_id": "CB1", "k_min": 1, "k_max": 2}])
    assert r.status_code == 422 and "故障集合为空" in r.text


def test_coordination_plan_infeasible_unprotected(client):
    sid = create_ok(client, base_payload())  # 含无保护的 F5
    resp = post_plan(client, sid, [
        {"breaker_id": "CB1", "k_min": 0.5, "k_max": 2.0},
        {"breaker_id": "CB2", "k_min": 0.5, "k_max": 2.0},
    ])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "infeasible"
    # 无解：不返回方案
    assert body["multipliers"] is None
    assert body["verification"] is None
    assert body["objective"] is None
    reasons = body["reasons"]
    assert reasons["unprotected_faults"] == ["F5"]
    assert reasons["unadjusted_pair_violations"] == []
    assert reasons["constraint_conflict"] is False


def test_coordination_plan_infeasible_unadjusted_pair(client):
    # 三级链 CB1(慢)->CB2(中)->CB3(快)，故障在末端无断路器支路
    p = base_payload()
    p["branches"] = [
        {"id": "L1", "from": "S", "to": "A",
         "breaker": {"id": "CB1", "curve_id": "C_SLOW"}},
        {"id": "L2", "from": "A", "to": "B",
         "breaker": {"id": "CB2", "curve_id": "C_MID"}},
        {"id": "L3", "from": "B", "to": "C",
         "breaker": {"id": "CB3", "curve_id": "C_FAST"}},
        {"id": "L4", "from": "C", "to": "D"},
    ]
    p["curves"] = [
        {"id": "C_FAST", "points": [
            {"current": 10, "time": 1}, {"current": 100, "time": 0.1}]},
        {"id": "C_MID", "points": [
            {"current": 10, "time": 1.5}, {"current": 100, "time": 0.15}]},
        {"id": "C_SLOW", "points": [
            {"current": 10, "time": 2}, {"current": 100, "time": 0.2}]},
    ]
    p["faults"] = [
        {"id": "FX", "branch_id": "L4",
         "current_min": 10, "current_max": 100, "r": 1.8},
    ]
    sid = create_ok(client, p)
    # 只调整 CB1：违规配对 (CB3 主, CB2 上) 双方都未调整，无法修复
    resp = post_plan(client, sid, [{"breaker_id": "CB1", "k_min": 0.5, "k_max": 2.0}])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "infeasible"
    reasons = body["reasons"]
    assert reasons["unprotected_faults"] == []
    assert reasons["constraint_conflict"] is False
    assert len(reasons["unadjusted_pair_violations"]) == 1
    v = reasons["unadjusted_pair_violations"][0]
    assert v["fault_id"] == "FX"
    assert v["main_breaker_id"] == "CB3"
    assert v["upstream_breaker_id"] == "CB2"
    assert v["min_ratio"] == pytest.approx(1.5, rel=1e-9)
    assert v["r"] == pytest.approx(1.8)


def test_coordination_plan_infeasible_constraint_conflict(client):
    sid = create_ok(client, coord_payload())
    # F1 需要 ln k_CB1 >= ln(1.5)，但 k_max = 1.2 不够 -> 约束冲突
    resp = post_plan(client, sid, [{"breaker_id": "CB1", "k_min": 1.0, "k_max": 1.2}])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "infeasible"
    reasons = body["reasons"]
    assert reasons["unprotected_faults"] == []
    assert reasons["unadjusted_pair_violations"] == []
    assert reasons["constraint_conflict"] is True


def test_coordination_plan_already_qualified(client):
    p = coord_payload()
    p["faults"] = [f for f in p["faults"] if f["id"] in ("F2", "F3", "F4")]
    sid = create_ok(client, p)
    body = post_plan(client, sid, [
        {"breaker_id": "CB1", "k_min": 0.5, "k_max": 2.0},
        {"breaker_id": "CB2", "k_min": 0.5, "k_max": 2.0},
    ]).json()
    assert body["status"] == "ok"
    assert body["objective"] == pytest.approx(0.0, abs=1e-6)
    for m in body["multipliers"]:
        assert m["k"] == pytest.approx(1.0, rel=1e-6)


def test_coordination_plan_per_breaker_not_shared_curve(client):
    # CB1 与 CB2 共用曲线 CS；只调整 CB2，共用曲线的 CB1 不得变化
    p = base_payload()
    p["branches"] = [
        {"id": "L1", "from": "S", "to": "A",
         "breaker": {"id": "CB0", "curve_id": "C0"}},
        {"id": "L2", "from": "A", "to": "B",
         "breaker": {"id": "CB1", "curve_id": "CS"}},
        {"id": "L3", "from": "B", "to": "C",
         "breaker": {"id": "CB2", "curve_id": "CS"}},
    ]
    p["curves"] = [
        {"id": "C0", "points": [
            {"current": 10, "time": 4}, {"current": 100, "time": 0.4}]},
        {"id": "CS", "points": [
            {"current": 10, "time": 2}, {"current": 100, "time": 0.2}]},
    ]
    p["faults"] = [
        {"id": "FA", "branch_id": "L3",
         "current_min": 10, "current_max": 100, "r": 1.5},
        {"id": "FB", "branch_id": "L2",
         "current_min": 10, "current_max": 100, "r": 1.5},
    ]
    sid = create_ok(client, p)
    body = post_plan(client, sid, [
        {"breaker_id": "CB2", "k_min": 0.4, "k_max": 1.0}]).json()
    assert body["status"] == "ok"
    # FA 中 CB2(主)/CB1(上) 比值恒为 1.0，需 k_CB2 <= 2/3；字典序最小取 k = 2/3
    assert len(body["multipliers"]) == 1
    m = body["multipliers"][0]
    assert m["breaker_id"] == "CB2"
    assert m["k"] == pytest.approx(2 / 3, rel=1e-6)
    faults = {f["fault_id"]: f for f in body["verification"]["faults"]}
    fa = {p_["upstream_breaker_id"]: p_ for p_ in faults["FA"]["pairs"]}
    assert fa["CB1"]["min_ratio"] == pytest.approx(1.5, rel=1e-6)  # 等号合格
    assert fa["CB0"]["min_ratio"] == pytest.approx(3.0, rel=1e-6)
    # 关键：FB（主 CB1、上 CB0，均未调整）的比值保持 2.0 —— 共用曲线未被联动
    fb = faults["FB"]["pairs"][0]
    assert fb["min_ratio"] == pytest.approx(2.0, rel=1e-9)


def test_coordination_plan_lexicographic_min_ln_k(client):
    # 与故障无关的第三台断路器：字典序最小化将其 ln k 压到 -M*（而非 0）
    p = coord_payload()
    p["branches"][3]["breaker"] = {"id": "CB3", "curve_id": "C_MAIN"}  # B4: S->D
    p["faults"] = [f for f in p["faults"] if f["id"] == "F1"]
    sid = create_ok(client, p)
    body = post_plan(client, sid, [
        {"breaker_id": "CB1", "k_min": 0.5, "k_max": 2.0},
        {"breaker_id": "CB2", "k_min": 0.5, "k_max": 2.0},
        {"breaker_id": "CB3", "k_min": 0.5, "k_max": 2.0},
    ]).json()
    assert body["status"] == "ok"
    half = math.log(1.5) / 2
    ln_k = {m["breaker_id"]: m["ln_k"] for m in body["multipliers"]}
    assert ln_k["CB1"] == pytest.approx(half, rel=1e-6)
    assert ln_k["CB2"] == pytest.approx(-half, rel=1e-6)
    assert ln_k["CB3"] == pytest.approx(-half, rel=1e-6)


def test_coordination_plan_degenerate_interval(client):
    sid = create_ok(client, coord_payload())
    # k 固定为 1.5（闭区间退化为单点）：F1 恰好等号合格
    body = post_plan(client, sid, [
        {"breaker_id": "CB1", "k_min": 1.5, "k_max": 1.5}]).json()
    assert body["status"] == "ok"
    assert body["multipliers"][0]["k"] == pytest.approx(1.5, rel=1e-9)
    assert body["objective"] == pytest.approx(math.log(1.5), rel=1e-6)
    f1 = next(f for f in body["verification"]["faults"] if f["fault_id"] == "F1")
    assert f1["status"] == "qualified"
    assert f1["pairs"][0]["min_ratio"] == pytest.approx(1.8, rel=1e-9)
