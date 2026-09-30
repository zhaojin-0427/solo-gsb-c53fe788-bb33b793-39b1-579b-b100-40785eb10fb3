"""请求/响应模型（Pydantic v2）。

场景一旦保存即不可变：仅提供创建与读取接口，无更新/删除接口。
整定批次同样不可变：仅可发布与读取，发布后各场景的生效版本单调递增。
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


# ---------------------------------------------------------------------------
# 场景输入模型
# ---------------------------------------------------------------------------

class CurvePoint(BaseModel):
    """曲线折点：电流为正，动作时间为正。"""

    current: float = Field(gt=0, description="电流（安培），必须为正")
    time: float = Field(gt=0, description="动作时间（秒），必须为正")


class CurveIn(BaseModel):
    """动作特性曲线：至少两个折点，电流严格递增。"""

    id: str = Field(min_length=1)
    points: list[CurvePoint] = Field(min_length=2)

    @model_validator(mode="after")
    def _currents_strictly_increasing(self) -> "CurveIn":
        for prev, nxt in zip(self.points, self.points[1:]):
            if not nxt.current > prev.current:
                raise ValueError(
                    f"曲线 {self.id} 的折点电流必须严格递增: "
                    f"{prev.current} 之后出现 {nxt.current}"
                )
        return self


class BreakerIn(BaseModel):
    """断路器：每条支路至多一台（可缺省）。"""

    id: str = Field(min_length=1)
    curve_id: str = Field(min_length=1, description="引用的曲线 id")


class BranchIn(BaseModel):
    """有向支路：from -> to。"""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(min_length=1)
    from_node: str = Field(min_length=1, alias="from")
    to_node: str = Field(min_length=1, alias="to")
    breaker: BreakerIn | None = None


class FaultIn(BaseModel):
    """故障：指定支路、正电流闭区间及门槛 R>1。"""

    id: str = Field(min_length=1)
    branch_id: str = Field(min_length=1)
    current_min: float = Field(gt=0)
    current_max: float = Field(gt=0)
    r: float = Field(gt=1, description="选择性门槛，必须大于 1")

    @model_validator(mode="after")
    def _interval_valid(self) -> "FaultIn":
        if self.current_min > self.current_max:
            raise ValueError(
                f"故障 {self.id} 的电流区间下界 {self.current_min} "
                f"大于上界 {self.current_max}"
            )
        return self


class ScenarioIn(BaseModel):
    """不可变场景：单电源有向树 + 曲线库 + 故障集合。"""

    name: str = Field(min_length=1)
    source: str = Field(min_length=1, description="电源节点名")
    branches: list[BranchIn] = Field(default_factory=list)
    curves: list[CurveIn] = Field(default_factory=list)
    faults: list[FaultIn] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 场景输出模型
# ---------------------------------------------------------------------------

class ScenarioOut(ScenarioIn):
    id: str
    created_at: str


class ScenarioSummary(BaseModel):
    id: str
    name: str
    created_at: str
    branch_count: int
    curve_count: int
    fault_count: int


# ---------------------------------------------------------------------------
# 核验结果模型
# ---------------------------------------------------------------------------

class ViolationOut(BaseModel):
    """一段不合格电流范围及边界是否包含。"""

    current_from: float
    current_to: float
    from_included: bool = Field(description="下边界本身是否不合格")
    to_included: bool = Field(description="上边界本身是否不合格")


class PairResultOut(BaseModel):
    """主保护与一台上级断路器的核验结果。"""

    upstream_breaker_id: str
    min_ratio: float = Field(description="整个故障区间内的最小时间比 t上/t主")
    min_ratio_current: float = Field(description="取得最小时间比的电流（并列取较小电流）")
    qualified: bool
    violations: list[ViolationOut] = Field(default_factory=list)


class FaultReportOut(BaseModel):
    fault_id: str
    branch_id: str
    status: str = Field(description="qualified=合格 / unqualified=不合格 / unprotected=无保护")
    main_breaker_id: str | None = Field(
        default=None, description="故障支路到电源路径上最近的断路器；无则为 null"
    )
    pairs: list[PairResultOut] = Field(default_factory=list)


class VerifySummaryOut(BaseModel):
    total: int
    qualified: int
    unqualified: int
    unprotected: int


class VerifyResponseOut(BaseModel):
    scenario_id: str
    scenario_name: str
    summary: VerifySummaryOut
    faults: list[FaultReportOut]


# ---------------------------------------------------------------------------
# 协调方案（断路器倍率调整）模型
# ---------------------------------------------------------------------------

class BreakerAdjustmentIn(BaseModel):
    """单台断路器的倍率范围：有限正数闭区间 [k_min, k_max]。"""

    breaker_id: str = Field(min_length=1)
    k_min: FiniteFloat = Field(gt=0, description="倍率下界（有限正数）")
    k_max: FiniteFloat = Field(gt=0, description="倍率上界（有限正数）")

    @model_validator(mode="after")
    def _interval_valid(self) -> "BreakerAdjustmentIn":
        if self.k_min > self.k_max:
            raise ValueError(
                f"断路器 {self.breaker_id} 的倍率区间倒置: "
                f"k_min {self.k_min} 大于 k_max {self.k_max}"
            )
        return self


class CoordinationPlanIn(BaseModel):
    """协调方案请求：非空的断路器倍率区间列表（未列出者倍率固定为 1）。"""

    adjustments: list[BreakerAdjustmentIn] = Field(min_length=1)


class BreakerMultiplierOut(BaseModel):
    """一台所选断路器的倍率解。"""

    breaker_id: str
    k: float = Field(description="倍率：该断路器动作时间整体乘以 k")
    ln_k: float = Field(description="ln k")


class UnadjustedPairViolationOut(BaseModel):
    """主保护与上级均未指定倍率、且原始最小比值已违规的配对。"""

    fault_id: str
    main_breaker_id: str
    upstream_breaker_id: str
    min_ratio: float = Field(description="原始最小时间比 t上/t主")
    r: float = Field(description="选择性门槛")


class InfeasibleReasonsOut(BaseModel):
    """无解原因的分类诊断。"""

    unprotected_faults: list[str] = Field(description="无主保护的故障 id 列表")
    unadjusted_pair_violations: list[UnadjustedPairViolationOut] = Field(
        description="配对双方均未调整、原本即违规（无法通过倍率修复）的配对"
    )
    constraint_conflict: bool = Field(
        description="可调整配对的约束与倍率边界联立后无可行解"
    )


class CoordinationPlanOut(BaseModel):
    scenario_id: str
    status: str = Field(description="ok=有解 / infeasible=无解（不返回方案）")
    objective: float | None = Field(
        default=None, description="主目标值 max|ln k|（无解时为 null）"
    )
    multipliers: list[BreakerMultiplierOut] | None = Field(
        default=None,
        description="所选断路器的倍率（按 ID 顺序；未指定的断路器倍率为 1）",
    )
    verification: VerifyResponseOut | None = Field(
        default=None,
        description="施加倍率后的逐故障核验结果（原场景与原核验不受影响）",
    )
    reasons: InfeasibleReasonsOut | None = Field(
        default=None, description="无解原因分类（有解时为 null）"
    )


# ---------------------------------------------------------------------------
# 多场景整定发布模型
# ---------------------------------------------------------------------------

# 有限正倍率（拒绝 0、负数、inf/nan）。
PositiveFiniteFloat = Annotated[float, Field(gt=0), FiniteFloat]


class ScenarioSettingIn(BaseModel):
    """批次中单个场景的整定项：期望版本 + 断路器 ID 到正倍率的映射。"""

    scenario_id: str = Field(min_length=1)
    expected_revision: int = Field(
        ge=0, description="客户端所见该场景当前生效版本（首次整定前为 0）"
    )
    multipliers: dict[str, PositiveFiniteFloat] = Field(
        description="断路器 ID 到有限正倍率的映射；未列出者倍率为 1"
    )

    @model_validator(mode="after")
    def _breaker_ids_nonempty(self) -> "ScenarioSettingIn":
        bad = [bid for bid in self.multipliers if not bid]
        if bad:
            raise ValueError("断路器 id 不能为空字符串")
        return self


class SettingBatchIn(BaseModel):
    """多场景整定发布请求：request_id + 至少两个场景项。"""

    request_id: str = Field(min_length=1, description="客户端幂等键")
    scenarios: list[ScenarioSettingIn] = Field(min_length=2)


class ScenarioEffectiveSettingOut(BaseModel):
    """单场景单次读取：版本与该版本生效倍率、核验严格同源，绝不混合版本。"""

    scenario_id: str
    revision: int = Field(description="当前（或指定）生效版本；从未被批次覆盖时为 0")
    batch_id: str | None = Field(
        default=None, description="产生该版本的批次 id；revision=0 时为 null"
    )
    multipliers: list[BreakerMultiplierOut] = Field(
        description="该版本生效的非默认（k≠1）倍率，按断路器 ID 排序；其余倍率为 1"
    )
    verification: VerifyResponseOut = Field(
        description="在上述倍率与版本下冻结的逐故障核验结果"
    )


class SettingBatchItemOut(BaseModel):
    """批次内单个场景的生效版本、倍率与冻结核验。"""

    scenario_id: str
    expected_revision: int = Field(description="提交时客户端声明的期望版本")
    from_revision: int = Field(description="该批次生效前的版本（= expected_revision）")
    to_revision: int = Field(description="该批次生效后的新版本")
    multipliers: list[BreakerMultiplierOut] = Field(
        description="提交的全部倍率（按断路器 ID 排序，未列出者为 1）"
    )
    verification: VerifyResponseOut = Field(
        description="该场景在本批次倍率下冻结的逐故障核验结果"
    )


class SettingBatchOut(BaseModel):
    """不可变整定批次。"""

    id: str
    request_id: str
    seq: int = Field(description="全局单调递增序号（按提交顺序）")
    created_at: str
    replayed: bool = Field(
        description="本次响应是否为同 request_id + 同内容重试的回放"
    )
    scenarios: list[SettingBatchItemOut]


class SettingBatchSummaryOut(BaseModel):
    """批次摘要（列表用）。"""

    id: str
    request_id: str
    seq: int
    created_at: str
    scenario_count: int
