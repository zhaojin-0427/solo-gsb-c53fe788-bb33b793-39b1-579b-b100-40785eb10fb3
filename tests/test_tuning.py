"""多场景整定批次发布与版本化读取测试。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.db import ScenarioStore, TuningConflict
from app.main import create_app
from app.tuning import prepare_tuning_batch
from app.schemas import TuningBatchIn
from tests.test_api import base_payload, create_ok  # noqa: E402


@pytest.fixture()
def client():
    app = create_app(":memory:")
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 场景构造
# ---------------------------------------------------------------------------

def scenario_a() -> dict:
    """coord 场景：F1 在 k=1 不合格（最小比 1.2 < R=1.8），k_CB1=1.5 后等号合格。"""
    p = base_payload()
    p["name"] = "tuning-A"
    p["faults"] = [f for f in p["faults"] if f["id"] != "F5"]
    return p


def scenario_b() -> dict:
    """独立拓扑，含同 ID 断路器 CB1（跨场景同一设备）；k_CB1=1.5 时合格。"""
    return {
        "name": "tuning-B",
        "source": "S",
        "branches": [
            {"id": "L1", "from": "S", "to": "A",
             "breaker": {"id": "CB1", "curve_id": "CU"}},
            {"id": "L2", "from": "A", "to": "B",
             "breaker": {"id": "CB9", "curve_id": "CM"}},
            {"id": "L3", "from": "B", "to": "C"},
        ],
        "curves": [
            {"id": "CM", "points": [
                {"current": 10, "time": 10}, {"current": 1000, "time": 0.1}]},
            {"id": "CU", "points": [
                {"current": 10, "time": 30}, {"current": 1000, "time": 0.3}]},
        ],
        "faults": [
            {"id": "G1", "branch_id": "L3",
             "current_min": 10, "current_max": 1000, "r": 2.0},
            {"id": "G2", "branch_id": "L1",
             "current_min": 10, "current_max": 1000, "r": 2.0},
        ],
    }


def scenario_c() -> dict:
    """仅一级断路器 CB1，故障以 CB1 为主保护、无上级：任何倍率都合格。"""
    return {
        "name": "tuning-C",
        "source": "S",
        "branches": [
            {"id": "Q1", "from": "S", "to": "A",
             "breaker": {"id": "CB1", "curve_id": "CU"}},
        ],
        "curves": [
            {"id": "CU", "points": [
                {"current": 10, "time": 30}, {"current": 1000, "time": 0.3}]},
        ],
        "faults": [
            {"id": "H1", "branch_id": "Q1",
             "current_min": 10, "current_max": 1000, "r": 2.0},
        ],
    }


def scenario_unprotected() -> dict:
    p = base_payload()  # 含无保护的 F5（支路 B4）
    p["name"] = "tuning-unprotected"
    return p


def scenario_no_faults() -> dict:
    p = scenario_a()
    p["name"] = "tuning-no-faults"
    p["faults"] = []
    return p


def batch_body(
    request_id: str,
    items: list[tuple[str, int, dict | None]],
) -> dict:
    return {
        "request_id": request_id,
        "scenarios": [
            {"scenario_id": sid, "expected_revision": rev,
             "multipliers": mult if mult is not None else {}}
            for sid, rev, mult in items
        ],
    }


def publish(client, body):
    return client.post("/tuning-batches", json=body)


@pytest.fixture()
def two_scenarios(client):
    sid_a = create_ok(client, scenario_a())
    sid_b = create_ok(client, scenario_b())
    return sid_a, sid_b


# ---------------------------------------------------------------------------
# 成功路径与读取 API
# ---------------------------------------------------------------------------

def test_publish_batch_ok_and_read_apis(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    body = batch_body("req-1", [
        (sid_a, 0, {"CB1": 1.5}),
        (sid_b, 0, {"CB1": 1.5}),
    ])
    resp = publish(client, body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["request_id"] == "req-1"
    assert out["replayed"] is False
    assert out["created_at"]
    items = {s["scenario_id"]: s for s in out["scenarios"]}
    assert set(items) == {sid_a, sid_b}
    for sid in (sid_a, sid_b):
        item = items[sid]
        assert item["expected_revision"] == 0
        assert item["revision"] == 1
        assert item["multipliers"] == [{"breaker_id": "CB1", "k": 1.5}]
        ver = item["verification"]
        assert ver["scenario_id"] == sid
        assert ver["summary"]["unqualified"] == 0
        assert ver["summary"]["unprotected"] == 0
    # A 场景 F1 恰好等号合格
    f1 = next(f for f in items[sid_a]["verification"]["faults"] if f["fault_id"] == "F1")
    assert f1["status"] == "qualified"
    assert f1["pairs"][0]["min_ratio"] == pytest.approx(1.8, rel=1e-9)

    # 批次读取：内容与发布响应一致（replayed=true 表示回放既有批次）
    got = client.get("/tuning-batches/req-1")
    assert got.status_code == 200
    got_body = got.json()
    assert got_body["replayed"] is True
    assert got_body["scenarios"] == out["scenarios"]

    # 批次列表
    listing = client.get("/tuning-batches").json()
    assert listing == [
        {"request_id": "req-1", "created_at": out["created_at"],
         "scenario_count": 2}
    ]

    # 当前生效版本 = 1，倍率与核验同版本
    for sid in (sid_a, sid_b):
        state = client.get(f"/scenarios/{sid}/tuning").json()
        assert state["revision"] == 1
        assert state["batch_request_id"] == "req-1"
        assert state["created_at"] == out["created_at"]
        assert state["multipliers"] == [{"breaker_id": "CB1", "k": 1.5}]
        item = items[sid]
        assert state["verification"] == item["verification"]
        # 指定版本读取一致
        rev1 = client.get(f"/scenarios/{sid}/tuning/revisions/1").json()
        assert rev1 == state


def test_revision_zero_baseline_before_publish(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    state = client.get(f"/scenarios/{sid_a}/tuning").json()
    assert state["revision"] == 0
    assert state["batch_request_id"] is None
    assert state["created_at"] is None
    assert state["multipliers"] == []
    # 基线核验等价于原始核验（倍率全为 1）
    raw = client.get(f"/scenarios/{sid_a}/verification").json()
    assert state["verification"] == raw
    # 显式读版本 0 同样可得
    rev0 = client.get(f"/scenarios/{sid_a}/tuning/revisions/0").json()
    assert rev0 == state
    # 基线版本 A 的 F1 本就不合格（发布前的现状）
    assert rev0["verification"]["summary"] == {
        "total": 4, "qualified": 3, "unqualified": 1, "unprotected": 0}


def test_second_batch_increments_revisions(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    r1 = publish(client, batch_body("req-1", [
        (sid_a, 0, {"CB1": 1.5}), (sid_b, 0, {"CB1": 1.5})]))
    assert r1.status_code == 201
    r2 = publish(client, batch_body("req-2", [
        (sid_a, 1, {"CB1": 2.0}), (sid_b, 1, {"CB1": 2.0})]))
    assert r2.status_code == 201, r2.text
    items = {s["scenario_id"]: s for s in r2.json()["scenarios"]}
    assert items[sid_a]["revision"] == 2
    assert items[sid_b]["revision"] == 2
    assert items[sid_a]["expected_revision"] == 1
    # 当前版本为 2，历史版本 1 仍可读且不混合
    cur = client.get(f"/scenarios/{sid_a}/tuning").json()
    assert cur["multipliers"] == [{"breaker_id": "CB1", "k": 2.0}]
    old = client.get(f"/scenarios/{sid_a}/tuning/revisions/1").json()
    assert old["multipliers"] == [{"breaker_id": "CB1", "k": 1.5}]
    assert old["verification"] != cur["verification"]


def test_original_verification_untouched(client, two_scenarios):
    sid_a, _ = two_scenarios
    before = client.get(f"/scenarios/{sid_a}/verification").json()
    publish(client, batch_body("req-1", [
        (sid_a, 0, {"CB1": 1.5}),
        (two_scenarios[1], 0, {"CB1": 1.5}),
    ]))
    after = client.get(f"/scenarios/{sid_a}/verification").json()
    assert after == before  # 原始核验不被整定发布影响


# ---------------------------------------------------------------------------
# 幂等：同文回放（先于版本检查）、异文 409
# ---------------------------------------------------------------------------

def test_replay_same_content_order_insensitive(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    body = batch_body("req-1", [
        (sid_a, 0, {"CB1": 1.5}), (sid_b, 0, {"CB1": 1.5})])
    assert publish(client, body).status_code == 201

    # 场景顺序颠倒、映射键序打乱：规范化后同文 -> 200 回放
    retried = {
        "request_id": "req-1",
        "scenarios": [
            {"scenario_id": sid_b, "expected_revision": 0,
             "multipliers": {"CB1": 1.5}},
            {"scenario_id": sid_a, "expected_revision": 0,
             "multipliers": {"CB1": 1.5}},
        ],
    }
    resp = publish(client, retried)
    assert resp.status_code == 200, resp.text
    assert resp.json()["replayed"] is True
    # 回放不产生新版本
    for sid in (sid_a, sid_b):
        assert client.get(f"/scenarios/{sid}/tuning").json()["revision"] == 1
    assert len(client.get("/tuning-batches").json()) == 1


def test_replay_wins_over_stale_version(client, two_scenarios):
    """同文重试即使 expected_revision 已过期也回放（回放先于版本检查）。"""
    sid_a, sid_b = two_scenarios
    body = batch_body("req-1", [
        (sid_a, 0, {"CB1": 1.5}), (sid_b, 0, {"CB1": 1.5})])
    publish(client, body)
    publish(client, batch_body("req-2", [
        (sid_a, 1, {"CB1": 2.0}), (sid_b, 1, {"CB1": 2.0})]))
    # req-1 的期望版本 0 已过期，但同文重试仍回放原批次
    resp = publish(client, body)
    assert resp.status_code == 200
    assert resp.json()["replayed"] is True
    item_a = next(s for s in resp.json()["scenarios"] if s["scenario_id"] == sid_a)
    assert item_a["revision"] == 1  # 回放的是原批次，版本号不变


def test_same_request_id_different_content_409(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    body = batch_body("req-1", [
        (sid_a, 0, {"CB1": 1.5}), (sid_b, 0, {"CB1": 1.5})])
    publish(client, body)

    # 倍率异文
    other = batch_body("req-1", [
        (sid_a, 0, {"CB1": 2.0}), (sid_b, 0, {"CB1": 2.0})])
    resp = publish(client, other)
    assert resp.status_code == 409
    assert "req-1" in resp.text

    # expected_revision 异文也算异文
    other2 = batch_body("req-1", [
        (sid_a, 1, {"CB1": 1.5}), (sid_b, 1, {"CB1": 1.5})])
    assert publish(client, other2).status_code == 409


def test_stale_expected_revision_new_request_409(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    publish(client, batch_body("req-1", [
        (sid_a, 0, {"CB1": 1.5}), (sid_b, 0, {"CB1": 1.5})]))
    # 新 request_id 但期望版本停留在 0 -> 409，且不部分生效
    resp = publish(client, batch_body("req-stale", [
        (sid_a, 0, {"CB1": 2.0}), (sid_b, 0, {"CB1": 2.0})]))
    assert resp.status_code == 409
    assert "当前整定版本为 1" in resp.text and "期望版本 0" in resp.text
    for sid in (sid_a, sid_b):
        state = client.get(f"/scenarios/{sid}/tuning").json()
        assert state["revision"] == 1
        assert state["multipliers"] == [{"breaker_id": "CB1", "k": 1.5}]
    assert client.get("/tuning-batches/req-stale").status_code == 404


def test_one_scene_conflict_whole_batch_no_partial(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    publish(client, batch_body("req-1", [
        (sid_a, 0, {"CB1": 1.5}), (sid_b, 0, {"CB1": 1.5})]))
    # A 版本正确、B 版本过期：整批 409，A 也不得前进到版本 2
    resp = publish(client, batch_body("req-x", [
        (sid_a, 1, {"CB1": 2.0}), (sid_b, 0, {"CB1": 2.0})]))
    assert resp.status_code == 409
    assert sid_b in resp.text
    assert client.get(f"/scenarios/{sid_a}/tuning").json()["revision"] == 1
    assert client.get(f"/scenarios/{sid_b}/tuning").json()["revision"] == 1


# ---------------------------------------------------------------------------
# 422：请求与领域校验
# ---------------------------------------------------------------------------

def test_422_less_than_two_scenarios(client, two_scenarios):
    sid_a, _ = two_scenarios
    r = publish(client, batch_body("req-x", [(sid_a, 0, {"CB1": 1.5})]))
    assert r.status_code == 422
    assert client.post("/tuning-batches",
                       json={"request_id": "req-y", "scenarios": []}).status_code == 422
    assert client.post("/tuning-batches",
                       json={"request_id": "", "scenarios": []}).status_code == 422


def test_422_duplicate_scenario(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    r = publish(client, batch_body("req-x", [
        (sid_a, 0, {"CB1": 1.5}),
        (sid_b, 0, {"CB1": 1.5}),
        (sid_a, 0, {"CB1": 1.5}),
    ]))
    assert r.status_code == 422 and "场景重复" in r.text


def test_422_unknown_scenario_and_breaker(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    r = publish(client, batch_body("req-x", [
        (sid_a, 0, {"CB1": 1.5}), ("nope", 0, {})]))
    assert r.status_code == 422 and "场景不存在" in r.text and "nope" in r.text

    r = publish(client, batch_body("req-y", [
        (sid_a, 0, {"GHOST": 1.5}), (sid_b, 0, {"CB1": 1.5})]))
    assert r.status_code == 422 and "不存在的断路器" in r.text


def test_422_cross_scenario_inconsistent_explicit(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    r = publish(client, batch_body("req-x", [
        (sid_a, 0, {"CB1": 1.5}), (sid_b, 0, {"CB1": 2.0})]))
    assert r.status_code == 422
    assert "跨场景倍率不一致" in r.text
    assert "CB1" in r.text


def test_422_cross_scenario_inconsistent_implicit_default(client):
    """一方显式 1.5、另一方未列出（缺省 1）：同一设备倍率不一致。"""
    c = client
    sid_a = create_ok(c, scenario_a())
    sid_b = create_ok(c, scenario_b())
    r = publish(c, batch_body("req-x", [
        (sid_a, 0, {"CB1": 1.5}), (sid_b, 0, {})]))
    assert r.status_code == 422
    assert "跨场景倍率不一致" in r.text
    # 未列出取 1 的一方在错误信息中可见
    assert "为 1" in r.text


def test_cross_scenario_consistent_implicit_one_passes(client):
    """两边都未列出 CB1（同为缺省 1）且各场景本就合格：允许发布。"""
    sid_b = create_ok(client, scenario_b())
    sid_c = create_ok(client, scenario_c())
    r = publish(client, batch_body("req-ok", [
        (sid_b, 0, {}), (sid_c, 0, {})]))
    assert r.status_code == 201, r.text
    items = {s["scenario_id"]: s for s in r.json()["scenarios"]}
    assert items[sid_b]["multipliers"] == []
    assert items[sid_c]["multipliers"] == []


def test_422_empty_fault_set(client, two_scenarios):
    sid_a, _ = two_scenarios
    sid_d = create_ok(client, scenario_no_faults())
    r = publish(client, batch_body("req-x", [
        (sid_a, 0, {"CB1": 1.5}), (sid_d, 0, {})]))
    assert r.status_code == 422 and "故障集合为空" in r.text


def test_422_unprotected_rejects_whole_batch(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    sid_e = create_ok(client, scenario_unprotected())
    r = publish(client, batch_body("req-x", [
        (sid_a, 0, {"CB1": 1.5}),
        (sid_b, 0, {"CB1": 1.5}),
        (sid_e, 0, {"CB1": 1.5}),
    ]))
    assert r.status_code == 422
    assert "无主保护" in r.text and "F5" in r.text


def test_422_unqualified_rejects_whole_batch(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    # A 不给 CB1 倍率：F1 最小比 1.2 < 1.8；B 单独合格也不能带过
    r = publish(client, batch_body("req-x", [
        (sid_a, 0, {}), (sid_b, 0, {"CB1": 1.0})]))
    assert r.status_code == 422
    assert "不合格" in r.text and "F1" in r.text


def test_422_bad_multiplier_values(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    for bad in (0, -1):
        r = publish(client, batch_body("req-x", [
            (sid_a, 0, {"CB1": bad}), (sid_b, 0, {"CB1": bad})]))
        assert r.status_code == 422
    r = client.post(
        "/tuning-batches",
        content=(
            '{"request_id": "req-x", "scenarios": ['
            f'{{"scenario_id": "{sid_a}", "expected_revision": 0, '
            '"multipliers": {"CB1": Infinity}}},'
            f'{{"scenario_id": "{sid_b}", "expected_revision": 0, '
            '"multipliers": {"CB1": Infinity}}]}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_failed_validation_does_not_consume_request_id(client, two_scenarios):
    sid_a, sid_b = two_scenarios
    bad = batch_body("req-retry", [
        (sid_a, 0, {}), (sid_b, 0, {})])  # A 不合格
    assert publish(client, bad).status_code == 422
    # 同一 request_id 修正内容后仍可首次发布（422 未占用幂等键）
    good = batch_body("req-retry", [
        (sid_a, 0, {"CB1": 1.5}), (sid_b, 0, {"CB1": 1.5})])
    resp = publish(client, good)
    assert resp.status_code == 201
    assert resp.json()["replayed"] is False


# ---------------------------------------------------------------------------
# 404 读取
# ---------------------------------------------------------------------------

def test_tuning_reads_404(client, two_scenarios):
    sid_a, _ = two_scenarios
    publish(client, batch_body("req-1", [
        (sid_a, 0, {"CB1": 1.5}),
        (two_scenarios[1], 0, {"CB1": 1.5})]))
    assert client.get("/tuning-batches/nope").status_code == 404
    assert client.get("/scenarios/nope/tuning").status_code == 404
    assert client.get("/scenarios/nope/tuning/revisions/0").status_code == 404
    r = client.get(f"/scenarios/{sid_a}/tuning/revisions/99")
    assert r.status_code == 404 and "版本不存在" in r.text


# ---------------------------------------------------------------------------
# 跨进程争用：两个独立连接（各自 ScenarioStore）同时提交，仅一批生效
# ---------------------------------------------------------------------------

def test_cross_connection_contention_only_one_wins(tmp_path):
    db_file = str(tmp_path / "tuning-contention.db")

    # 用第一个应用建场景并发布初始批次
    with TestClient(create_app(db_file)) as c1:
        sid_a = create_ok(c1, scenario_a())
        sid_b = create_ok(c1, scenario_b())
        r = c1.post("/tuning-batches", json=batch_body("req-init", [
            (sid_a, 0, {"CB1": 1.5}), (sid_b, 0, {"CB1": 1.5})]))
        assert r.status_code == 201

    # 两个独立 store（模拟两个进程的独立连接），均基于版本 1 提交
    store1 = ScenarioStore(db_file)
    store2 = ScenarioStore(db_file)
    results: list[str] = []

    def contend(store: ScenarioStore, request_id: str, barrier: threading.Barrier):
        req = TuningBatchIn.model_validate(batch_body(request_id, [
            (sid_a, 1, {"CB1": 2.0}), (sid_b, 1, {"CB1": 2.0})]))
        canonical, prepared = prepare_tuning_batch(store, req)
        barrier.wait()
        try:
            store.publish_tuning_batch(request_id, canonical, prepared)
            return "committed"
        except TuningConflict:
            return "conflict"

    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(contend, store1, "req-win-a", barrier)
        f2 = pool.submit(contend, store2, "req-win-b", barrier)
        results = [f1.result(), f2.result()]

    assert sorted(results) == ["committed", "conflict"]

    # 重启应用验证最终状态：仅一个版本 2 的批次生效
    with TestClient(create_app(db_file)) as c2:
        state_a = c2.get(f"/scenarios/{sid_a}/tuning").json()
        state_b = c2.get(f"/scenarios/{sid_b}/tuning").json()
        assert state_a["revision"] == 2
        assert state_b["revision"] == 2
        winner = state_a["batch_request_id"]
        assert winner == state_b["batch_request_id"]
        assert winner in ("req-win-a", "req-win-b")
        assert state_a["multipliers"] == [{"breaker_id": "CB1", "k": 2.0}]
        # 版本 1、2 均可读；败者事务完全回滚，批次列表正好 2 条（初始 + 胜者）
        assert c2.get(f"/scenarios/{sid_a}/tuning/revisions/1").json()[
            "batch_request_id"] == "req-init"
        assert len(c2.get("/tuning-batches").json()) == 2
