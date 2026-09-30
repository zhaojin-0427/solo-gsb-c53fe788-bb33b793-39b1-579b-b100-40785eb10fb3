"""配电保护选择性核验 API（FastAPI 入口）。"""
from __future__ import annotations

import math

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .core import DomainError, build_coordination_plan, validate_scenario, verify_scenario
from .db import ScenarioStore, TuningConflict
from .schemas import (
    CoordinationPlanIn,
    CoordinationPlanOut,
    FaultReportOut,
    ScenarioIn,
    ScenarioOut,
    ScenarioSummary,
    TuningBatchIn,
    TuningBatchOut,
    TuningBatchSummary,
    ScenarioTuningStateOut,
    VerifyResponseOut,
)
from .tuning import (
    build_batch_out,
    build_state_out,
    prepare_tuning_batch,
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
            "并支持多场景整定批次的原子发布与版本化读取。"
        ),
    )
    app.state.store = ScenarioStore(db_path)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors})

    @app.exception_handler(TuningConflict)
    async def tuning_conflict_handler(_: Request, exc: TuningConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": [str(exc)]})

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
            "version": "1.2.0",
            "docs": "/docs",
            "endpoints": {
                "create_scenario": "POST /scenarios",
                "list_scenarios": "GET /scenarios",
                "get_scenario": "GET /scenarios/{id}",
                "verify_all": "GET /scenarios/{id}/verification",
                "verify_one": "GET /scenarios/{id}/faults/{fault_id}/verification",
                "coordination_plan": "POST /scenarios/{id}/coordination-plan",
                "publish_tuning_batch": "POST /tuning-batches",
                "list_tuning_batches": "GET /tuning-batches",
                "get_tuning_batch": "GET /tuning-batches/{request_id}",
                "get_current_tuning": "GET /scenarios/{id}/tuning",
                "get_tuning_revision": "GET /scenarios/{id}/tuning/revisions/{revision}",
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
    # 多场景整定发布（不可变批次 + 版本化生效倍率）
    # ------------------------------------------------------------------
    @app.post(
        "/tuning-batches",
        response_model=TuningBatchOut,
        tags=["tuning"],
        summary="多场景整定批次发布（整批核验通过后在同一事务原子生效）",
    )
    def publish_tuning_batch(payload: TuningBatchIn):
        # 内容/领域校验（422）先于事务；规范化指纹在其中生成
        canonical, prepared = prepare_tuning_batch(app.state.store, payload)
        # 事务内核对所有当前版本、追加不可变批次并原子切换：
        # 同 request_id 同文先回放；版本冲突或异文 -> 409，不部分生效
        batch, replayed = app.state.store.publish_tuning_batch(
            payload.request_id, canonical, prepared
        )
        out = build_batch_out(batch, app.state.store, replayed)
        return JSONResponse(
            status_code=(
                status.HTTP_200_OK if replayed else status.HTTP_201_CREATED
            ),
            content=jsonable_encoder(out),
        )

    @app.get(
        "/tuning-batches",
        response_model=list[TuningBatchSummary],
        tags=["tuning"],
        summary="列出全部整定批次（按创建时间排序）",
    )
    def list_tuning_batches() -> list[dict]:
        return app.state.store.list_tuning_batches()

    @app.get(
        "/tuning-batches/{request_id}",
        response_model=TuningBatchOut,
        tags=["tuning"],
        summary="读取一个整定批次（各场景生效版本、倍率与对应核验）",
    )
    def get_tuning_batch(request_id: str) -> TuningBatchOut:
        batch = app.state.store.get_tuning_batch(request_id)
        if batch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"整定批次不存在: {request_id}",
            )
        # 批次不可变：各场景倍率与核验永远对应同一次发布，不会混合版本
        return build_batch_out(batch, app.state.store, replayed=True)

    @app.get(
        "/scenarios/{scenario_id}/tuning",
        response_model=ScenarioTuningStateOut,
        tags=["tuning"],
        summary="读取场景当前生效整定版本（倍率与核验同版本，不混合）",
    )
    def get_current_tuning(scenario_id: str) -> ScenarioTuningStateOut:
        state = app.state.store.get_current_scenario_revision(scenario_id)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"场景不存在: {scenario_id}",
            )
        return build_state_out(scenario_id, state, app.state.store)

    @app.get(
        "/scenarios/{scenario_id}/tuning/revisions/{revision}",
        response_model=ScenarioTuningStateOut,
        tags=["tuning"],
        summary="读取场景指定整定版本（0=初始基线，倍率全为 1）",
    )
    def get_tuning_revision(scenario_id: str, revision: int) -> ScenarioTuningStateOut:
        state = app.state.store.get_scenario_revision(scenario_id, revision)
        if state is None:
            _get_scenario_or_404(scenario_id)  # 区分场景不存在与版本不存在
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"场景 {scenario_id} 的整定版本不存在: {revision}",
            )
        return build_state_out(scenario_id, state, app.state.store)

    return app


# 惰性创建：仅在属性被实际访问（uvicorn 加载 "app.main:app"）时才连接数据库，
# 避免仅导入模块（如测试收集）就触碰 DATABASE_PATH 指向的文件。
_app: FastAPI | None = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
