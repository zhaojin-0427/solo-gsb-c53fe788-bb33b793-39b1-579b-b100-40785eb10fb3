"""配电保护选择性核验 API（FastAPI 入口）。"""
from __future__ import annotations

import json
import math

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .core import DomainError, build_coordination_plan, prepare_setting_batch, validate_scenario, verify_scenario
from .db import IdempotencyConflict, RevisionConflict, ScenarioStore
from .schemas import (
    BreakerMultiplierOut,
    CoordinationPlanIn,
    CoordinationPlanOut,
    FaultReportOut,
    ScenarioEffectiveSettingOut,
    ScenarioIn,
    ScenarioOut,
    ScenarioSummary,
    SettingBatchIn,
    SettingBatchItemOut,
    SettingBatchOut,
    SettingBatchSummaryOut,
    VerifyResponseOut,
)


def _json_safe(obj):
    """递归替换非有限浮点（inf/nan）为其字符串形式，保证可 JSON 序列化。"""
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="配电保护选择性核验 API",
        version="1.2.0",
        description=(
            "保存不可变的配电场景（单电源有向树 + 断路器动作曲线 + 故障集合），"
            "并对每个故障核验主保护与各上级断路器的时间选择性 "
            "（对数-对数插值，禁止外推，t上/t主 ≥ R，等号合格）；"
            "另可求解断路器倍率协调方案（不改动场景），"
            "并支持多场景整定批次的原子发布、幂等重试与版本化读取。"
        ),
    )
    app.state.store = ScenarioStore(db_path)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors})

    @app.exception_handler(RevisionConflict)
    async def revision_conflict_handler(
        _: Request, exc: RevisionConflict
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "场景当前版本与 expected_revision 不符，整定批次未生效",
                "conflicts": exc.details,
            },
        )

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict_handler(
        _: Request, exc: IdempotencyConflict
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    "同一 request_id 已提交过内容不同的整定批次（异文冲突）；"
                    "同内容重试才会回放原批次"
                ),
                "existing_batch_id": exc.existing_batch_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 默认处理器在错误输入含 inf/nan 时无法序列化 detail（json.dumps 拒绝
        # 非有限浮点）而变成 500；这里先清洗再返回 422。
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"detail": _json_safe(exc.errors())}),
        )

    def _get_scenario_or_404(scenario_id: str) -> dict:
        record = app.state.store.get(scenario_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"场景不存在: {scenario_id}",
            )
        return record

    def _canonical_request(payload: SettingBatchIn) -> str:
        """同 request_id 请求的规范化形态：场景按 scenario_id、
        倍率按断路器 ID 排序后的紧凑 JSON（作为异文判定依据）。"""
        canonical = {
            "request_id": payload.request_id,
            "scenarios": [
                {
                    "scenario_id": s.scenario_id,
                    "expected_revision": s.expected_revision,
                    "multipliers": {
                        bid: float(k) for bid, k in sorted(s.multipliers.items())
                    },
                }
                for s in sorted(payload.scenarios, key=lambda s: s.scenario_id)
            ],
        }
        return json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    # ------------------------------------------------------------------
    @app.get("/", tags=["meta"])
    def root() -> dict:
        return {
            "service": "配电保护选择性核验 API",
            "version": "1.2.0",
            "docs": "/docs",
            "endpoints": {
                "create_scenario": "POST /scenarios",
                "list_scenarios": "GET /scenarios",
                "get_scenario": "GET /scenarios/{id}",
                "verify_all": "GET /scenarios/{id}/verification",
                "verify_one": "GET /scenarios/{id}/faults/{fault_id}/verification",
                "coordination_plan": "POST /scenarios/{id}/coordination-plan",
                "publish_setting_batch": "POST /setting-batches",
                "list_setting_batches": "GET /setting-batches",
                "get_setting_batch": "GET /setting-batches/{id}",
                "get_setting_batch_by_request": "GET /setting-batches/by-request/{request_id}",
                "get_effective_setting": "GET /scenarios/{id}/effective-setting",
                "list_scenario_revisions": "GET /scenarios/{id}/revisions",
            },
        }

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    @app.post(
        "/scenarios",
        response_model=ScenarioOut,
        status_code=status.HTTP_201_CREATED,
        tags=["scenarios"],
        summary="保存不可变场景（校验拓扑、曲线与故障覆盖）",
    )
    def create_scenario(payload: ScenarioIn) -> dict:
        validate_scenario(payload)
        return app.state.store.create(
            payload.model_dump(mode="json", by_alias=True)
        )

    @app.get(
        "/scenarios",
        response_model=list[ScenarioSummary],
        tags=["scenarios"],
        summary="列出全部场景",
    )
    def list_scenarios() -> list[dict]:
        return app.state.store.list()

    @app.get(
        "/scenarios/{scenario_id}",
        response_model=ScenarioOut,
        tags=["scenarios"],
        summary="读取场景（场景不可变，无更新/删除接口）",
    )
    def get_scenario(scenario_id: str) -> dict:
        return _get_scenario_or_404(scenario_id)

    # ------------------------------------------------------------------
    @app.get(
        "/scenarios/{scenario_id}/verification",
        response_model=VerifyResponseOut,
        tags=["verification"],
        summary="核验场景内全部故障的选择性",
    )
    def verify_all(scenario_id: str) -> VerifyResponseOut:
        record = _get_scenario_or_404(scenario_id)
        scenario = ScenarioIn.model_validate(record)
        return verify_scenario(scenario_id, scenario)

    @app.get(
        "/scenarios/{scenario_id}/faults/{fault_id}/verification",
        response_model=FaultReportOut,
        tags=["verification"],
        summary="核验单个故障的选择性",
    )
    def verify_one(scenario_id: str, fault_id: str) -> FaultReportOut:
        record = _get_scenario_or_404(scenario_id)
        scenario = ScenarioIn.model_validate(record)
        report = verify_scenario(scenario_id, scenario)
        for fault_report in report.faults:
            if fault_report.fault_id == fault_id:
                return fault_report
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"故障不存在: {fault_id}",
        )

    # ------------------------------------------------------------------
    @app.post(
        "/scenarios/{scenario_id}/coordination-plan",
        response_model=CoordinationPlanOut,
        tags=["coordination"],
        summary="求解断路器倍率协调方案（不改动场景及原核验）",
    )
    def coordination_plan(
        scenario_id: str, payload: CoordinationPlanIn
    ) -> CoordinationPlanOut:
        record = _get_scenario_or_404(scenario_id)
        scenario = ScenarioIn.model_validate(record)
        return build_coordination_plan(scenario_id, scenario, payload)

    # ------------------------------------------------------------------
    # 多场景整定发布
    # ------------------------------------------------------------------
    def _batch_out(record: dict) -> SettingBatchOut:
        return SettingBatchOut(
            id=record["id"],
            request_id=record["request_id"],
            seq=record["seq"],
            created_at=record["created_at"],
            replayed=record["replayed"],
            scenarios=[
                SettingBatchItemOut(
                    scenario_id=item["scenario_id"],
                    expected_revision=item["expected_revision"],
                    from_revision=item["from_revision"],
                    to_revision=item["to_revision"],
                    multipliers=[
                        BreakerMultiplierOut(**m) for m in item["multipliers"]
                    ],
                    verification=VerifyResponseOut.model_validate(
                        item["verification"]
                    ),
                )
                for item in record["scenarios"]
            ],
        )

    @app.post(
        "/setting-batches",
        response_model=SettingBatchOut,
        tags=["setting-batches"],
        summary="原子发布多场景整定批次（幂等：同 request_id 同内容回放，异文 409）",
    )
    def publish_setting_batch(
        payload: SettingBatchIn, response: Response
    ) -> SettingBatchOut:
        # 领域校验（未知/重复场景、未知断路器、跨场景倍率不一致、
        # 空故障集/无保护/不合格）全部在事务前完成；版本核对与落库
        # 在同一 SQLite 写事务内原子执行（见 ScenarioStore.publish_setting_batch）。
        scenarios: dict[str, ScenarioIn] = {}
        for sid in {s.scenario_id for s in payload.scenarios}:
            record = app.state.store.get(sid)
            if record is not None:
                scenarios[sid] = ScenarioIn.model_validate(record)
        items = prepare_setting_batch(payload, scenarios)
        record = app.state.store.publish_setting_batch(
            payload.request_id, _canonical_request(payload), items
        )
        response.status_code = (
            status.HTTP_200_OK if record["replayed"]
            else status.HTTP_201_CREATED
        )
        return _batch_out(record)

    @app.get(
        "/setting-batches",
        response_model=list[SettingBatchSummaryOut],
        tags=["setting-batches"],
        summary="列出全部整定批次（按提交顺序）",
    )
    def list_setting_batches() -> list[dict]:
        return app.state.store.list_setting_batches()

    @app.get(
        "/setting-batches/by-request/{request_id}",
        response_model=SettingBatchOut,
        tags=["setting-batches"],
        summary="按 request_id 查询整定批次",
    )
    def get_setting_batch_by_request(request_id: str) -> SettingBatchOut:
        record = app.state.store.get_setting_batch_by_request(request_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"request_id 对应的批次不存在: {request_id}",
            )
        return _batch_out(record)

    @app.get(
        "/setting-batches/{batch_id}",
        response_model=SettingBatchOut,
        tags=["setting-batches"],
        summary="读取整定批次及各场景生效版本、倍率与冻结核验",
    )
    def get_setting_batch(batch_id: str) -> SettingBatchOut:
        record = app.state.store.get_setting_batch(batch_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"整定批次不存在: {batch_id}",
            )
        return _batch_out(record)

    @app.get(
        "/scenarios/{scenario_id}/effective-setting",
        response_model=ScenarioEffectiveSettingOut,
        tags=["setting-batches"],
        summary="读取场景当前（或指定版本）生效倍率与同源核验（不混合版本）",
    )
    def get_effective_setting(
        scenario_id: str, revision: int | None = None
    ) -> ScenarioEffectiveSettingOut:
        record = app.state.store.get_effective_setting(scenario_id, revision)
        if record is None:
            _get_scenario_or_404(scenario_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"场景 {scenario_id} 不存在版本: {revision}",
            )
        if record["_item"] is None:
            # 修订 0：全部倍率 1，核验按不可变场景原文现场计算
            scenario = ScenarioIn.model_validate(
                _get_scenario_or_404(scenario_id)
            )
            return ScenarioEffectiveSettingOut(
                scenario_id=scenario_id,
                revision=0,
                batch_id=None,
                multipliers=[],
                verification=verify_scenario(scenario_id, scenario),
            )
        item = record["_item"]
        return ScenarioEffectiveSettingOut(
            scenario_id=scenario_id,
            revision=record["revision"],
            batch_id=record["batch_id"],
            multipliers=[BreakerMultiplierOut(**m) for m in item["multipliers"]],
            verification=VerifyResponseOut.model_validate(item["verification"]),
        )

    @app.get(
        "/scenarios/{scenario_id}/revisions",
        tags=["setting-batches"],
        summary="列出场景历次生效版本（版本 -> 批次）",
    )
    def list_scenario_revisions(scenario_id: str) -> dict:
        revisions = app.state.store.list_scenario_revisions(scenario_id)
        if revisions is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"场景不存在: {scenario_id}",
            )
        return {"scenario_id": scenario_id, "revisions": revisions}

    return app


app = create_app()
