"""多场景整定发布：请求规范化、整批核验与响应构建。

语义（与 README 一致）：
- 每个场景项给出场景 ID、expected_revision（初次为 0）与
  断路器 ID -> 有限正倍率的映射；映射中未列出的该场景断路器倍率取 1。
- 同一断路器 ID 跨场景视为同一台物理设备：凡同时出现在多个提交场景中的
  断路器，其生效倍率（显式值或缺省 1）必须一致，否则整批拒绝。
- 按既有折点规则核验各场景施加倍率后的全部故障：空故障集、存在无保护
  故障或不合格故障时整批拒绝（HTTP 422）。
- 规范化仅用于幂等指纹与存储排序，不改变业务含义。
"""
from __future__ import annotations

import json
import math

from .core import DomainError, verify_scenario
from .db import ScenarioStore
from .schemas import (
    ScenarioIn,
    ScenarioTuningItemOut,
    ScenarioTuningStateOut,
    TuningBatchIn,
    TuningBatchOut,
    TuningMultiplierOut,
)


def canonical_payload(req: TuningBatchIn) -> str:
    """请求的幂等指纹：按场景 ID、断路器 ID 排序的确定性 JSON。

    expected_revision 参与指纹——同 request_id 但期望版本不同即为异文。
    """
    normalized = {
        "request_id": req.request_id,
        "scenarios": [
            {
                "scenario_id": item.scenario_id,
                "expected_revision": item.expected_revision,
                "multipliers": dict(sorted(item.multipliers.items())),
            }
            for item in sorted(req.scenarios, key=lambda s: s.scenario_id)
        ],
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def prepare_tuning_batch(
    store: ScenarioStore, req: TuningBatchIn
) -> tuple[str, list[dict]]:
    """写事务前的全部领域校验与整批核验；失败抛 DomainError（HTTP 422）。

    返回 (规范化指纹, 按场景 ID 排序的待写入项)。核验通过仅代表内容合格，
    版本核对仍在写事务内完成（可能 409）。
    """
    errors: list[str] = []

    # --- 重复场景 ---
    seen: set[str] = set()
    for item in req.scenarios:
        if item.scenario_id in seen:
            errors.append(f"批次中场景重复: {item.scenario_id!r}")
        seen.add(item.scenario_id)

    # --- 场景存在性 ---
    records: dict[str, dict] = {}
    scenarios: dict[str, ScenarioIn] = {}
    for sid in seen:
        record = store.get(sid)
        if record is None:
            errors.append(f"场景不存在: {sid!r}")
            continue
        records[sid] = record
        scenarios[sid] = ScenarioIn.model_validate(record)

    # --- 未知断路器 + 收集各场景显式倍率 ---
    explicit: dict[str, dict[str, float]] = {}
    for item in req.scenarios:
        scenario = scenarios.get(item.scenario_id)
        if scenario is None:
            continue
        known = {b.breaker.id for b in scenario.branches if b.breaker is not None}
        multipliers = dict(item.multipliers)
        for bid in multipliers:
            if bid not in known:
                errors.append(
                    f"场景 {item.scenario_id!r} 的倍率映射引用了不存在的断路器: "
                    f"{bid!r}"
                )
        explicit[item.scenario_id] = multipliers

    # --- 空故障集 ---
    for sid, scenario in scenarios.items():
        if not scenario.faults:
            errors.append(f"场景 {sid!r} 的故障集合为空，不能整定发布")

    # --- 同一设备跨场景倍率一致性（未列出按 1 计）---
    breaker_owners: dict[str, list[str]] = {}
    for sid, scenario in scenarios.items():
        for b in scenario.branches:
            if b.breaker is not None:
                breaker_owners.setdefault(b.breaker.id, []).append(sid)
    for bid, owners in breaker_owners.items():
        if len(owners) < 2:
            continue
        values = {
            sid: explicit.get(sid, {}).get(bid, 1.0) for sid in owners
        }
        distinct = set(values.values())
        if len(distinct) > 1:
            detail = "、".join(
                f"场景 {sid} 为 {values[sid]:g}" for sid in sorted(values)
            )
            errors.append(
                f"断路器 {bid!r} 跨场景倍率不一致（同一 ID 视为同一设备，"
                f"未列出取 1）：{detail}"
            )

    if errors:
        raise DomainError(errors)

    # --- 整批核验：施加倍率后不得有无保护/不合格故障 ---
    ordered = sorted(req.scenarios, key=lambda s: s.scenario_id)
    verify_errors: list[str] = []
    for item in ordered:
        scenario = scenarios[item.scenario_id]
        ln_k = {bid: math.log(k) for bid, k in item.multipliers.items()}
        result = verify_scenario(item.scenario_id, scenario, ln_k)
        faults_by_id = {f.id: f for f in scenario.faults}
        for report in result.faults:
            if report.status == "unprotected":
                verify_errors.append(
                    f"场景 {item.scenario_id!r} 的故障 {report.fault_id!r} "
                    f"无主保护（路径上无断路器），整批拒绝"
                )
            elif report.status == "unqualified":
                bad = next(p for p in report.pairs if not p.qualified)
                verify_errors.append(
                    f"场景 {item.scenario_id!r} 的故障 {report.fault_id!r} "
                    f"不合格：主保护 {report.main_breaker_id!r} 与上级 "
                    f"{bad.upstream_breaker_id!r} 的最小时间比 "
                    f"{bad.min_ratio:.6g} 小于门槛 "
                    f"{faults_by_id[report.fault_id].r:g}，整批拒绝"
                )
    if verify_errors:
        raise DomainError(verify_errors)

    prepared = [
        {
            "scenario_id": item.scenario_id,
            "expected_revision": item.expected_revision,
            "multipliers": explicit[item.scenario_id],
        }
        for item in ordered
    ]
    return canonical_payload(req), prepared


def _multipliers_out(multipliers: dict) -> list[TuningMultiplierOut]:
    return [
        TuningMultiplierOut(breaker_id=bid, k=float(multipliers[bid]))
        for bid in sorted(multipliers)
    ]


def _verify_item(scenario_id: str, multipliers: dict, store: ScenarioStore):
    record = store.get(scenario_id)
    scenario = ScenarioIn.model_validate(record)
    ln_k = {bid: math.log(float(k)) for bid, k in multipliers.items()}
    return verify_scenario(scenario_id, scenario, ln_k)


def build_batch_out(batch: dict, store: ScenarioStore, replayed: bool) -> TuningBatchOut:
    """由批次存储记录构建响应；核验按记录倍率现算（场景不可变，结论确定）。"""
    items = []
    for row in batch["items"]:
        items.append(
            ScenarioTuningItemOut(
                scenario_id=row["scenario_id"],
                expected_revision=row["expected_revision"],
                revision=row["revision"],
                multipliers=_multipliers_out(row["multipliers"]),
                verification=_verify_item(
                    row["scenario_id"], row["multipliers"], store
                ),
            )
        )
    return TuningBatchOut(
        request_id=batch["request_id"],
        replayed=replayed,
        created_at=batch["created_at"],
        scenarios=items,
    )


def build_state_out(scenario_id: str, state: dict, store: ScenarioStore):
    """构建单个场景某一整定版本的生效状态（倍率与核验同版本，不混合）。"""
    return ScenarioTuningStateOut(
        scenario_id=scenario_id,
        revision=state["revision"],
        batch_request_id=state["batch_request_id"],
        created_at=state["created_at"],
        multipliers=_multipliers_out(state["multipliers"]),
        verification=_verify_item(scenario_id, state["multipliers"], store),
    )
