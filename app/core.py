"""配电保护选择性核验核心逻辑。

规则概要：
- 场景为单电源有向树，每条支路至多一台断路器；
- 曲线为折线，电流/时间取对数后线性插值，禁止外推；
- 故障支路到电源路径上最近的断路器为主保护，其余为上级；
- 对每台上级，在整个故障电流闭区间内要求 t上/t主 >= R（等号合格）；
- 按两曲线折点的并集分段，求出所有不合格电流范围及边界包含性；
- 任一上级不合格则故障不合格；无主保护则无保护；有主保护而无上级则合格。
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass

from .schemas import (
    BreakerMultiplierOut,
    CoordinationPlanIn,
    CoordinationPlanOut,
    FaultIn,
    FaultReportOut,
    InfeasibleReasonsOut,
    PairResultOut,
    ScenarioIn,
    UnadjustedPairViolationOut,
    VerifyResponseOut,
    VerifySummaryOut,
    ViolationOut,
)

# 时间比判定的对数容差：|ln(t上/t主) - ln R| <= LOG_EPS 视为相等（等号合格）。
# 仅用于吸收浮点误差；真实违规与门槛的差距通常远大于该量级。
LOG_EPS = 1e-9
# 最小时间比“并列”判定的相对容差（并列时取较小电流）。
TIE_EPS = 1e-9


class DomainError(Exception):
    """领域校验失败（在 API 层映射为 HTTP 422）。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# ---------------------------------------------------------------------------
# 曲线
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Curve:
    """对数-对数分段线性动作曲线（禁止外推）。"""

    id: str
    currents: tuple[float, ...]
    log_currents: tuple[float, ...]
    log_times: tuple[float, ...]

    @classmethod
    def from_points(cls, curve_id: str, points) -> "Curve":
        currents = tuple(p.current for p in points)
        return cls(
            id=curve_id,
            currents=currents,
            log_currents=tuple(math.log(i) for i in currents),
            log_times=tuple(math.log(p.time) for p in points),
        )

    @property
    def i_min(self) -> float:
        return self.currents[0]

    @property
    def i_max(self) -> float:
        return self.currents[-1]

    def log_time_at(self, log_i: float) -> float:
        """ln t 关于 ln I 的分段线性插值。调用方保证 log_i 在定义域内。"""
        xs, ys = self.log_currents, self.log_times
        if log_i <= xs[0]:
            return ys[0]
        if log_i >= xs[-1]:
            return ys[-1]
        k = bisect_right(xs, log_i) - 1
        frac = (log_i - xs[k]) / (xs[k + 1] - xs[k])
        return ys[k] + frac * (ys[k + 1] - ys[k])


# ---------------------------------------------------------------------------
# 场景校验
# ---------------------------------------------------------------------------

def _path_breakers(scenario: ScenarioIn, branch_id: str):
    """从故障支路沿入边走向电源，返回路径上的断路器（近故障端在前）。

    生成器带 visited 防护，即使拓扑非法（调用方仍在收集错误）也不会死循环。
    """
    incoming = {b.to_node: b for b in scenario.branches}
    by_id = {b.id: b for b in scenario.branches}
    cur = by_id.get(branch_id)
    visited: set[str] = set()
    while cur is not None and cur.id not in visited:
        visited.add(cur.id)
        if cur.breaker is not None:
            yield cur.breaker
        cur = incoming.get(cur.from_node)


def validate_scenario(s: ScenarioIn) -> None:
    """保存场景前的全部领域校验；失败抛出 DomainError（HTTP 422）。"""
    errors: list[str] = []

    # --- 曲线 id 唯一（折点本身的合法性由 Pydantic 模型保证） ---
    curve_ids: set[str] = set()
    for c in s.curves:
        if c.id in curve_ids:
            errors.append(f"曲线 id 重复: {c.id!r}")
        curve_ids.add(c.id)

    # --- 拓扑：单电源有向树 ---
    nodes: set[str] = set()
    indeg: dict[str, int] = defaultdict(int)
    adj: dict[str, list[str]] = defaultdict(list)
    branch_ids: set[str] = set()
    breaker_ids: set[str] = set()
    for b in s.branches:
        if b.id in branch_ids:
            errors.append(f"支路 id 重复: {b.id!r}")
        branch_ids.add(b.id)
        if b.from_node == b.to_node:
            errors.append(f"支路 {b.id!r} 两端节点相同（自环），不是树")
        nodes.add(b.from_node)
        nodes.add(b.to_node)
        indeg[b.from_node] += 0  # 确保键存在
        indeg[b.to_node] += 1
        adj[b.from_node].append(b.to_node)
        if b.breaker is not None:
            if b.breaker.id in breaker_ids:
                errors.append(f"断路器 id 重复: {b.breaker.id!r}")
            breaker_ids.add(b.breaker.id)
            if b.breaker.curve_id not in curve_ids:
                errors.append(
                    f"支路 {b.id!r} 的断路器 {b.breaker.id!r} "
                    f"引用了不存在的曲线 {b.breaker.curve_id!r}"
                )

    if s.branches:
        if s.source not in nodes:
            errors.append(f"电源节点 {s.source!r} 不在任何支路的节点中")
        else:
            if indeg[s.source] != 0:
                errors.append(f"电源节点 {s.source!r} 存在入边，不是单电源有向树")
            for n in sorted(nodes):
                if n != s.source and indeg[n] != 1:
                    errors.append(
                        f"节点 {n!r} 的入度为 {indeg[n]}（树中应为 1），拓扑不是树"
                    )
            # 从电源沿支路方向可达性检查（排除环、森林等非树结构）
            visited: set[str] = set()
            stack = [s.source]
            while stack:
                u = stack.pop()
                if u in visited:
                    continue
                visited.add(u)
                stack.extend(adj[u])
            unreachable = sorted(nodes - visited)
            if unreachable:
                errors.append(
                    f"节点 {unreachable} 从电源不可达，不是单电源有向树"
                )

    # --- 故障 ---
    fault_ids: set[str] = set()
    curves_by_id = {c.id: c for c in s.curves}
    for f in s.faults:
        if f.id in fault_ids:
            errors.append(f"故障 id 重复: {f.id!r}")
        fault_ids.add(f.id)
        if f.branch_id not in branch_ids:
            errors.append(f"故障 {f.id!r} 引用了不存在的支路 {f.branch_id!r}")
            continue
        # 曲线覆盖：路径上每台断路器的曲线必须覆盖整个故障区间（禁止外推）。
        # 路径上没有任何断路器属于“无保护”，是合法结论而非校验错误。
        for br in _path_breakers(s, f.branch_id):
            cv = curves_by_id.get(br.curve_id)
            if cv is None:
                continue  # 曲线不存在已在上面报错
            lo, hi = cv.points[0].current, cv.points[-1].current
            if lo > f.current_min or hi < f.current_max:
                errors.append(
                    f"故障 {f.id!r} 的电流区间 [{f.current_min}, {f.current_max}] "
                    f"超出路径上断路器 {br.id!r} 的曲线 {cv.id!r} "
                    f"电流范围 [{lo}, {hi}]（禁止外推，覆盖不足）"
                )

    if errors:
        raise DomainError(errors)


# ---------------------------------------------------------------------------
# 选择性分析
# ---------------------------------------------------------------------------

def analyze_pair(
    main: Curve,
    upstream: Curve,
    i_lo: float,
    i_hi: float,
    r: float,
    upstream_breaker_id: str,
) -> PairResultOut:
    """核验一对（主保护, 上级）在整个故障区间 [i_lo, i_hi] 上的选择性。

    令 d(x) = ln t上(x) - ln t主(x) - ln R。d 关于 ln I 在两曲线折点的
    并集分段上线性，因此每段的最值必在段端点，段内至多一次过零。
    """
    log_r = math.log(r)

    # 分段点：故障区间端点 + 两曲线落在区间内部的折点
    cuts = {i_lo, i_hi}
    for curve in (main, upstream):
        for i_cur in curve.currents:
            if i_lo < i_cur < i_hi:
                cuts.add(i_cur)
    pts = sorted(cuts)
    ds = [
        upstream.log_time_at(math.log(p)) - main.log_time_at(math.log(p)) - log_r
        for p in pts
    ]

    def ok(d: float) -> bool:
        return d >= -LOG_EPS  # 等号（含浮点容差）合格

    # --- 不合格电流范围（q = t上/t主 < R 的集合） ---
    pieces: list[list] = []  # [lo, hi, lo_included, hi_included]
    if len(pts) == 1:
        # 退化区间（单点故障电流）
        if not ok(ds[0]):
            pieces.append([pts[0], pts[0], True, True])
    for k in range(len(pts) - 1):
        x0, x1 = pts[k], pts[k + 1]
        d0, d1 = ds[k], ds[k + 1]
        ok0, ok1 = ok(d0), ok(d1)
        if ok0 and ok1:
            continue
        if not ok0 and not ok1:
            pieces.append([x0, x1, True, True])
            continue
        # 段内恰有一次过零（d 在 ln I 上线性）
        frac = (0.0 - d0) / (d1 - d0)
        frac = min(1.0, max(0.0, frac))
        cross = math.exp(math.log(x0) + frac * (math.log(x1) - math.log(x0)))
        if not ok0:
            # 左端不合格，右端合格：cross 处等号合格，不包含
            pieces.append([x0, cross, True, False])
        else:
            pieces.append([cross, x1, False, True])

    # 合并共享违规端点的相邻片段（共享点本身不合格 => 范围连通）
    merged: list[list] = []
    for p in pieces:
        if merged and merged[-1][3] and p[2] and merged[-1][1] == p[0]:
            merged[-1][1] = p[1]
            merged[-1][3] = p[3]
        else:
            merged.append(list(p))
    violations = [
        ViolationOut(
            current_from=lo, current_to=hi,
            from_included=lo_inc, to_included=hi_inc,
        )
        for lo, hi, lo_inc, hi_inc in merged
    ]

    # --- 最小时间比：分段单调（ln 空间线性），最小值必在分段点处取得 ---
    ratios = [math.exp(d + log_r) for d in ds]
    min_ratio = min(ratios)
    tie_bound = min_ratio * (1.0 + TIE_EPS)
    min_current = min(p for p, rt in zip(pts, ratios) if rt <= tie_bound)

    return PairResultOut(
        upstream_breaker_id=upstream_breaker_id,
        min_ratio=min_ratio,
        min_ratio_current=min_current,
        qualified=not violations,
        violations=violations,
    )


def evaluate_fault(
    scenario: ScenarioIn,
    fault: FaultIn,
    curves: dict[str, Curve],
    ln_k: dict[str, float] | None = None,
) -> FaultReportOut:
    """评估单个故障的选择性结论。

    ln_k 非空时按断路器施加倍率（ln_k[breaker_id] = ln k，缺省为 0 即 k=1）。
    倍率只平移对应断路器自身的曲线；共用同一曲线的其他断路器不受影响。
    """
    path = list(_path_breakers(scenario, fault.branch_id))
    if not path:
        return FaultReportOut(
            fault_id=fault.id,
            branch_id=fault.branch_id,
            status="unprotected",
            main_breaker_id=None,
            pairs=[],
        )
    main_breaker = path[0]
    main_curve = curves[main_breaker.curve_id]
    if ln_k:
        main_curve = _shift_curve(main_curve, ln_k.get(main_breaker.id, 0.0))
    pairs = []
    for up in path[1:]:
        up_curve = curves[up.curve_id]
        if ln_k:
            up_curve = _shift_curve(up_curve, ln_k.get(up.id, 0.0))
        pairs.append(
            analyze_pair(
                main_curve,
                up_curve,
                fault.current_min,
                fault.current_max,
                fault.r,
                up.id,
            )
        )
    status = "qualified" if all(p.qualified for p in pairs) else "unqualified"
    return FaultReportOut(
        fault_id=fault.id,
        branch_id=fault.branch_id,
        status=status,
        main_breaker_id=main_breaker.id,
        pairs=pairs,
    )


def verify_scenario(
    scenario_id: str,
    scenario: ScenarioIn,
    ln_k: dict[str, float] | None = None,
) -> VerifyResponseOut:
    """对场景内全部故障给出选择性结论（ln_k 非空时为施加倍率后的核验）。"""
    curves = {
        c.id: Curve.from_points(c.id, c.points) for c in scenario.curves
    }
    reports = [evaluate_fault(scenario, f, curves, ln_k) for f in scenario.faults]
    summary = VerifySummaryOut(
        total=len(reports),
        qualified=sum(1 for r in reports if r.status == "qualified"),
        unqualified=sum(1 for r in reports if r.status == "unqualified"),
        unprotected=sum(1 for r in reports if r.status == "unprotected"),
    )
    return VerifyResponseOut(
        scenario_id=scenario_id,
        scenario_name=scenario.name,
        summary=summary,
        faults=reports,
    )


# ---------------------------------------------------------------------------
# 协调方案（断路器倍率调整）
# ---------------------------------------------------------------------------
#
# 数学模型：对所选断路器 b 令 x_b = ln k_b（未指定的断路器 k=1，即 x=0，
# 用固定为 0 的源点表示）。每对（主保护, 上级）在故障电流全区间的选择性
# 要求 m·k上/k主 >= R（m 为折点分段求得的原始最小比值，等号合格），取对数
# 后全部为差分约束：
#
#     x_上 - x_主 >= ln R - ln m,   ln k_min <= x_b <= ln k_max
#
# 差分约束系统无可行解当且仅当约束图存在正环（Bellman-Ford 判定）；给定
# 上限 M 时加入 -M <= x_b <= M 即可二分主目标 min max|x_b|。系统的最长路
# 解是分量最小的可行解，因此自动就是按断路器 ID 顺序字典序最小化 ln k 的解。

# 正环（不可行）判定的相对容差：仅吸收浮点误差。
CYCLE_EPS = 1e-12
# 主目标二分迭代次数（精度远高于后续数值误差，足够）。
_OBJ_BISECT_ITERS = 60


def _shift_curve(curve: Curve, ln_k: float) -> Curve:
    """动作时间整体乘以 k（ln t 平移 ln k）；k=1 时返回原曲线。"""
    if ln_k == 0.0:
        return curve
    return Curve(
        id=curve.id,
        currents=curve.currents,
        log_currents=curve.log_currents,
        log_times=tuple(lt + ln_k for lt in curve.log_times),
    )


def _longest_paths(n_nodes: int, edges: list[tuple[int, int, float]]):
    """差分约束系统 x_v >= x_u + w（节点 0 固定 x_0 = 0）的分量最小解。

    边 (u, v, w) 表示 x_v >= x_u + w。返回各节点最长路；存在正环
    （系统不可行）时返回 None。
    """
    dist = [float("-inf")] * n_nodes
    dist[0] = 0.0
    for _ in range(n_nodes - 1):
        updated = False
        for u, v, w in edges:
            du = dist[u]
            if du == float("-inf"):
                continue
            cand = du + w
            if cand > dist[v]:
                dist[v] = cand
                updated = True
        if not updated:
            break
    for u, v, w in edges:
        du = dist[u]
        if du == float("-inf"):
            continue
        tol = CYCLE_EPS * max(1.0, abs(du), abs(dist[v]), abs(w))
        if du + w > dist[v] + tol:
            return None
    return dist


def _solve_multipliers(
    bounds: list[tuple[float, float]],
    pair_edges: list[tuple[int, int, float]],
):
    """求解倍率调整：先 min max|x_i|，再按变量顺序（断路器 ID 序）字典序最小化 x。

    bounds[i] = (ln k_min, ln k_max) 对应节点 i+1；pair_edges 中 (u, v, c)
    表示 x_v - x_u >= c（节点 0 固定为 0，即未指定断路器 k=1）。
    返回 [x_0, x_1, ...]（x_0 = 0）；不可行返回 None。
    """
    n = len(bounds)

    def edges(m_cap: float) -> list[tuple[int, int, float]]:
        es = list(pair_edges)
        for i, (lo_i, hi_i) in enumerate(bounds, start=1):
            es.append((0, i, max(lo_i, -m_cap)))
            es.append((i, 0, -min(hi_i, m_cap)))
        return es

    # 上界内可行性 = 原系统可行性（m_cap 覆盖全部边界时收紧无效）
    m_hi = max(max(abs(lo), abs(hi)) for lo, hi in bounds)
    if _longest_paths(n + 1, edges(m_hi)) is None:
        return None
    lo_m, hi_m = 0.0, m_hi
    for _ in range(_OBJ_BISECT_ITERS):
        mid = 0.5 * (lo_m + hi_m)
        if _longest_paths(n + 1, edges(mid)) is not None:
            hi_m = mid
        else:
            lo_m = mid
    # 微小松弛，吸收二分与正环判定的数值误差
    m_cap = hi_m + 1e-9 * max(1.0, hi_m)
    return _longest_paths(n + 1, edges(m_cap))


def build_coordination_plan(
    scenario_id: str,
    scenario: ScenarioIn,
    req: CoordinationPlanIn,
) -> CoordinationPlanOut:
    """对已保存场景求解断路器倍率协调方案（不改动场景及原核验）。

    请求校验失败（未知/重复断路器、空故障集）抛出 DomainError（HTTP 422）；
    无解时返回 status=infeasible 及分类原因，不返回方案。
    """
    errors: list[str] = []
    known_breakers = {
        b.breaker.id for b in scenario.branches if b.breaker is not None
    }
    seen: set[str] = set()
    for adj in req.adjustments:
        if adj.breaker_id in seen:
            errors.append(f"调整的断路器 id 重复: {adj.breaker_id!r}")
        seen.add(adj.breaker_id)
        if adj.breaker_id not in known_breakers:
            errors.append(f"调整的断路器在场景中不存在: {adj.breaker_id!r}")
    if not scenario.faults:
        errors.append("场景的故障集合为空，无法生成协调方案")
    if errors:
        raise DomainError(errors)

    curves = {
        c.id: Curve.from_points(c.id, c.points) for c in scenario.curves
    }
    selected = {
        a.breaker_id: (math.log(a.k_min), math.log(a.k_max))
        for a in req.adjustments
    }

    # 逐故障收集约束：x_上 - x_主 >= ln R - ln m（m 为原始最小比值）
    unprotected: list[str] = []
    unadjusted: list[UnadjustedPairViolationOut] = []
    pair_refs: list[tuple[str | None, str | None, float]] = []
    for fault in scenario.faults:
        report = evaluate_fault(scenario, fault, curves)
        if report.status == "unprotected":
            unprotected.append(fault.id)
            continue
        main_id = report.main_breaker_id
        for pair in report.pairs:
            up_id = pair.upstream_breaker_id
            c = math.log(fault.r) - math.log(pair.min_ratio)
            main_sel = main_id in selected
            up_sel = up_id in selected
            if not main_sel and not up_sel:
                # 双方均未调整：k 恒为 1，退化为原始结论 m >= R（等号合格）
                if c > LOG_EPS:
                    unadjusted.append(
                        UnadjustedPairViolationOut(
                            fault_id=fault.id,
                            main_breaker_id=main_id,
                            upstream_breaker_id=up_id,
                            min_ratio=pair.min_ratio,
                            r=fault.r,
                        )
                    )
            else:
                pair_refs.append(
                    (main_id if main_sel else None, up_id if up_sel else None, c)
                )

    ids = sorted(selected)  # 字典序优化按断路器 ID 顺序
    node = {bid: i + 1 for i, bid in enumerate(ids)}
    pair_edges = [
        (node[m] if m is not None else 0, node[u] if u is not None else 0, c)
        for m, u, c in pair_refs
    ]
    bounds = [selected[bid] for bid in ids]
    x = _solve_multipliers(bounds, pair_edges)

    if unprotected or unadjusted or x is None:
        return CoordinationPlanOut(
            scenario_id=scenario_id,
            status="infeasible",
            reasons=InfeasibleReasonsOut(
                unprotected_faults=unprotected,
                unadjusted_pair_violations=unadjusted,
                constraint_conflict=x is None,
            ),
        )

    ln_k = {bid: x[node[bid]] for bid in ids}
    return CoordinationPlanOut(
        scenario_id=scenario_id,
        status="ok",
        objective=max(abs(v) for v in ln_k.values()),
        multipliers=[
            BreakerMultiplierOut(
                breaker_id=bid, k=math.exp(ln_k[bid]), ln_k=ln_k[bid]
            )
            for bid in ids
        ],
        verification=verify_scenario(scenario_id, scenario, ln_k),
    )
