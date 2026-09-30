"""配电保护选择性核验 API（FastAPI 入口）。"""
from __future__ import annotations

import math

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .core import DomainError, build_coordination_plan, validate_scenario, verify_scenario
from .db import ScenarioStore
from .schemas import (
    CoordinationPlanIn,
    CoordinationPlanOut,
    FaultReportOut,
    ScenarioIn,
    ScenarioOut,
    ScenarioSummary,
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
        version="1.1.0",
        description=(
            "保存不可变的配电场景（单电源有向树 + 断路器动作曲线 + 故障集合），"
            "并对每个故障核验主保护与各上级断路器的时间选择性 "
            "（对数-对数插值，禁止外推，t上/t主 ≥ R，等号合格）；"
            "另可求解断路器倍率协调方案（不改动场景）。"
        ),
    )
    app.state.store = ScenarioStore(db_path)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors})

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

    # ------------------------------------------------------------------
    @app.get("/", tags=["meta"])
    def root() -> dict:
        return {
            "service": "配电保护选择性核验 API",
            "version": "1.1.0",
            "docs": "/docs",
            "endpoints": {
                "create_scenario": "POST /scenarios",
                "list_scenarios": "GET /scenarios",
                "get_scenario": "GET /scenarios/{id}",
                "verify_all": "GET /scenarios/{id}/verification",
                "verify_one": "GET /scenarios/{id}/faults/{fault_id}/verification",
                "coordination_plan": "POST /scenarios/{id}/coordination-plan",
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

    return app


app = create_app()
