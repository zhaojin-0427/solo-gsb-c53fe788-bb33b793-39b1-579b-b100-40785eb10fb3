# 配电保护选择性核验 API

基于 **Python + FastAPI + SQLite** 的配电保护选择性核验服务。客户端保存**不可变场景**
（单电源有向树 + 断路器动作曲线 + 故障集合），服务对每个故障核验主保护与各上级
断路器的时间选择性，并给出不合格电流范围、最小时间比等结构化结论。另支持断路器
倍率协调方案的纯计算求解，以及**多场景整定批次**的原子发布、幂等重试与
版本化读取（单 SQLite 写事务 + 乐观版本检查，跨进程并发安全）。

## 功能与规则

- **不可变场景**：场景仅可创建与读取（无更新/删除接口），整体存入 SQLite。
- **拓扑校验**：必须是单电源有向树——电源节点无入边、其余节点入度恰为 1、
  全部节点可沿支路方向从电源到达；自环、环、多重入度、森林等均被拒绝（HTTP 422）。
- **支路与断路器**：每条支路至多装一台断路器（`breaker` 可缺省）。
- **曲线**：至少两个折点；折点电流为正且**严格递增**，动作时间为正。
  电流与时间均取对数后**线性插值**（log-log 折线），**禁止外推**。
- **故障**：指定支路、正电流闭区间 `[current_min, current_max]` 及门槛 `R > 1`。
- **主保护**：故障支路到电源路径上**最近**的断路器。
  - 路径上没有任何断路器 → 该故障结论为 **无保护**（`unprotected`）；
  - 有主保护而无上级 → **合格**（`qualified`）；
  - 否则对每台上级核验整个区间内的 `t上/t主 ≥ R`（**等号算合格**），
    任一上级不合格则该故障 **不合格**（`unqualified`）。
- **曲线覆盖**：故障区间必须被路径上每台断路器的曲线电流范围完整覆盖，
  否则保存场景时即被拒绝（HTTP 422，禁止外推）。
- **分段求值**：以两曲线折点（与区间端点）的并集分段。取对数后比值关于 `ln I`
  分段线性，故每段最值必在端点、段内至多一次过零，可精确求出：
  - 所有**不合格电流范围**及**边界是否包含**
    （交点处 `t上/t主 = R` 为合格，故交点边界不包含；
    区间端点若本身不合格则包含）；
  - **最小时间比**及对应电流（并列时取**较小电流**）。
- 浮点容差：`|ln(t上/t主) − ln R| ≤ 1e-9` 视为相等（等号合格）；
  最小比值并列判定相对容差 `1e-9`。

## 协调方案（倍率调整）

`POST /scenarios/{id}/coordination-plan` 为已保存场景求解断路器**倍率协调方案**
（纯计算接口，不改动场景及原核验）：

- **请求**：非空的断路器倍率列表，每项为断路器 ID 与有限正数倍率闭区间
  `[k_min, k_max]`。未知或重复的断路器 ID、倒置区间（`k_min > k_max`）、
  非正/非有限倍率、空列表、场景故障集为空，均返回 **422**。
- **倍率语义**：断路器动作时间整体乘以 `k`（`ln t` 平移 `ln k`）；倍率按
  **断路器**施加，共用同一曲线的其他断路器不受影响；未指定的断路器倍率恒为 1。
- **约束**：要求全部故障有主保护；沿用折点分段求得的原始最小比值 `m`，
  联立所有配对的 `m·k上/k主 ≥ R`（等号合格）与各自倍率边界。对数化后为
  差分约束系统（`x上 − x主 ≥ ln R − ln m`，`x = ln k`），用 Bellman-Ford
  判可行并对主目标二分求解。
- **优化目标**（有解时）：先最小化所选断路器的 `max|ln k|`，再按断路器 ID
  顺序字典序最小化 `ln k`（差分约束系统的分量最小解即字典序最小解）。
- **有解**（`status=ok`）：返回所选断路器的倍率（按 ID 顺序）、目标值
  `objective = max|ln k|`，以及施加倍率后的逐故障核验结果
  （`verification`，结构同 `GET /scenarios/{id}/verification`）。
- **无解**（`status=infeasible`，HTTP 200）：不返回方案
  （`multipliers`/`verification`/`objective` 为 `null`），`reasons`
  分类给出原因——
  - `unprotected_faults`：无主保护的故障；
  - `unadjusted_pair_violations`：主保护与上级均未指定倍率、且原始最小
    比值已违规的配对（倍率无法修复）；
  - `constraint_conflict`：可调整配对的约束与倍率边界联立后无可行解。

## 多场景整定发布（不可变批次 + 乐观并发）

`POST /setting-batches` 把**至少两个**已保存场景的断路器倍率整定成一个
**不可变批次**并原子切换生效倍率：

- **请求**：`request_id`（客户端幂等键）+ `scenarios[]`（≥2 项），每项给出
  `scenario_id`、`expected_revision`（客户端所见当前生效版本，首次为 `0`）
  与断路器 ID 到**有限正倍率**的映射；**未列出的断路器倍率取 1**。
- **同一设备**：同一断路器 ID 跨场景视为同一台物理设备，显式给出的倍率必须
  相同（不一致返回 **422**）；各场景未列出的设备一律取 1。
- **整批核验**：按既有折点分段规则（log-log 插值、禁止外推、等号合格）核验
  每个场景提交倍率下的全部故障。批次内场景重复、未知场景/断路器、跨场景倍率
  不一致、场景故障集为空、存在无保护故障或任一故障不合格，均返回 **422**
  且整批拒绝。
- **原子发布**：核验通过后，在**同一个 SQLite 写事务**（`BEGIN IMMEDIATE`）
  内核对全部场景的当前版本、追加不可变批次并原子推进各场景生效版本。任一
  `expected_revision` 与当前版本不符返回 **409**，整批回滚，**不会部分生效**。
  WAL + `busy_timeout` 保证跨进程争用时写事务串行化：多个进程并发整定同一
  场景时恰有一批生效，其余得到 409。
- **版本**：每个被批次覆盖的场景版本号 `0 → 1 → 2 …` 单调递增；版本与倍率、
  核验结果一起冻结在不可变批次中。读取单次返回同一版本的倍率与核验，
  **绝不混合版本**；历史版本（含 `revision=0` 的初始状态）可随时读取。
- **幂等与规范化**：同一 `request_id` 的请求按 `scenario_id`、断路器 ID 排序
  规范化。
  - **同内容重试**（含字段/场景顺序不同但语义相同）先于版本检查回放原批次：
    返回 **HTTP 200** 且 `replayed=true`，即使原批次已生效、重试携带的
    `expected_revision` 已过期也照常回放；
  - **异文**（同一 `request_id` 规范化后内容不同，例如倍率或期望版本不同）
    返回 **409**（`existing_batch_id` 指向原批次），不覆盖原批次；
  - 首次成功发布返回 **HTTP 201**、`replayed=false`。

**读取 API**：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/setting-batches` | 批次列表（摘要，按提交顺序） |
| GET | `/setting-batches/{id}` | 批次详情：各场景 from/to 版本、倍率、冻结的核验 |
| GET | `/setting-batches/by-request/{request_id}` | 按 `request_id` 查批次（幂等查询） |
| GET | `/scenarios/{id}/effective-setting?revision=n` | 场景当前（省略参数）或第 `n` 版的生效倍率与同源核验 |
| GET | `/scenarios/{id}/revisions` | 场景历次生效版本（版本 → 批次） |

`GET /scenarios/{id}/verification` 始终返回场景原文（倍率恒为 1）的核验；
要读取某版生效倍率下的核验，用 `/effective-setting`。


## 快速开始

### Docker Compose（一键启动）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，SQLite 数据（不可变场景、整定批次与各场景
版本）保存在命名卷 `selectivity-data`（容器内 `/data/selectivity.db`），
重启不丢失；首次启动自动建表，旧库平滑升级（`CREATE TABLE IF NOT EXISTS`）。

### 本地运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库文件默认 `./data/selectivity.db`，可用环境变量 `DATABASE_PATH` 覆盖。

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

## 访问方式

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 服务信息与端点索引 |
| GET | `/health` | 健康检查 |
| POST | `/scenarios` | 保存不可变场景（校验拓扑/曲线/故障覆盖），201 返回 |
| GET | `/scenarios` | 场景列表（摘要） |
| GET | `/scenarios/{id}` | 读取场景原文 |
| GET | `/scenarios/{id}/verification` | 核验场景内全部故障（始终按原文、倍率 1） |
| GET | `/scenarios/{id}/faults/{fault_id}/verification` | 核验单个故障 |
| POST | `/scenarios/{id}/coordination-plan` | 求解断路器倍率协调方案（不改动场景） |
| POST | `/setting-batches` | 原子发布多场景整定批次（201 新建 / 200 同文回放；冲突 409） |
| GET | `/setting-batches` | 整定批次列表（摘要） |
| GET | `/setting-batches/{id}` | 读取批次：各场景生效版本、倍率与冻结的核验 |
| GET | `/setting-batches/by-request/{request_id}` | 按 request_id 查询批次 |
| GET | `/scenarios/{id}/effective-setting` | 当前（或 `?revision=n`）生效倍率与同源核验 |
| GET | `/scenarios/{id}/revisions` | 场景历次生效版本 |

交互式文档（Swagger UI）：<http://localhost:8000/docs>（OpenAPI：`/openapi.json`）。

错误约定：参数/领域校验失败返回 **422** 且 `detail` 为错误信息数组
（整定请求的 Pydantic 结构错误除外，其 `detail` 为标准校验错误对象数组）；
场景、故障、批次或指定版本不存在返回 **404**；整定版本冲突与 request_id
异文返回 **409**（响应体给出冲突明细）；场景不可变，`PUT/PATCH/DELETE`
返回 **405**。

## 数据模型

```jsonc
{
  "name": "demo-feeder",
  "source": "S",                       // 电源节点
  "branches": [                        // 有向支路（from -> to）
    {"id": "B1", "from": "S", "to": "A",
     "breaker": {"id": "CB1", "curve_id": "C_UP"}},   // 至多一台断路器，可缺省
    {"id": "B3", "from": "B", "to": "C"}              // 无断路器
  ],
  "curves": [                          // 动作曲线：>=2 折点，电流正且严格递增，时间正
    {"id": "C_UP", "points": [{"current": 10, "time": 20},
                              {"current": 100, "time": 1.5}]}
  ],
  "faults": [                          // 故障：支路 + 正电流闭区间 + 门槛 R>1
    {"id": "F1", "branch_id": "B3", "current_min": 10, "current_max": 1000, "r": 1.8}
  ]
}
```

核验结果字段：

- `status`：`qualified`（合格）/ `unqualified`（不合格）/ `unprotected`（无保护）；
- `main_breaker_id`：主保护断路器（无保护时为 `null`）；
- `pairs[]`：主保护与每台上级的核验——
  - `min_ratio` / `min_ratio_current`：整个区间内的最小时间比及对应电流（并列取较小电流）；
  - `violations[]`：不合格电流范围，`from_included` / `to_included` 表示边界是否包含
    （边界本身是否不合格）。

## 实际请求与响应示例

以下示例为对本实现的真实调用与响应（场景 id 与时间为每次运行动态生成，
下文以 `<id>` 代指创建返回的 id）。示例场景文件见
[`examples/scenario.json`](examples/scenario.json)，拓扑为
`S -B1(CB1)-> A -B2(CB2)-> B -B3-> C`，另有 `S -B4-> D`（无断路器）。

### 1. 保存场景

```bash
curl -s -X POST http://localhost:8000/scenarios \
  -H 'Content-Type: application/json' \
  -d @examples/scenario.json
```

响应 `201 Created`（节选，完整内容为场景原文 + `id` + `created_at`）：

```json
{
  "name": "demo-feeder",
  "source": "S",
  "branches": [
    {"id": "B1", "from": "S", "to": "A", "breaker": {"id": "CB1", "curve_id": "C_UP"}},
    {"id": "B2", "from": "A", "to": "B", "breaker": {"id": "CB2", "curve_id": "C_MAIN"}},
    {"id": "B3", "from": "B", "to": "C", "breaker": null},
    {"id": "B4", "from": "S", "to": "D", "breaker": null}
  ],
  "curves": [
    {"id": "C_MAIN", "points": [{"current": 10.0, "time": 10.0},
                                {"current": 100.0, "time": 1.0},
                                {"current": 1000.0, "time": 0.1}]},
    {"id": "C_UP", "points": [{"current": 10.0, "time": 20.0},
                              {"current": 100.0, "time": 1.5},
                              {"current": 1000.0, "time": 0.12}]}
  ],
  "faults": ["... 5 个故障，见 examples/scenario.json ..."],
  "id": "04853c31e7e444848ef9535c7b54193b",
  "created_at": "2026-09-29T20:06:45.863799+00:00"
}
```

### 2. 场景列表

```bash
curl -s http://localhost:8000/scenarios
```

```json
[
  {
    "id": "04853c31e7e444848ef9535c7b54193b",
    "name": "demo-feeder",
    "created_at": "2026-09-29T20:06:45.863799+00:00",
    "branch_count": 4,
    "curve_count": 2,
    "fault_count": 5
  }
]
```

### 3. 核验全部故障

```bash
curl -s http://localhost:8000/scenarios/<id>/verification
```

```json
{
  "scenario_id": "04853c31e7e444848ef9535c7b54193b",
  "scenario_name": "demo-feeder",
  "summary": {"total": 5, "qualified": 3, "unqualified": 1, "unprotected": 1},
  "faults": [
    {
      "fault_id": "F1",
      "branch_id": "B3",
      "status": "unqualified",
      "main_breaker_id": "CB2",
      "pairs": [
        {
          "upstream_breaker_id": "CB1",
          "min_ratio": 1.1999999999999997,
          "min_ratio_current": 1000.0,
          "qualified": false,
          "violations": [
            {"current_from": 23.240176447111455, "current_to": 1000.0,
             "from_included": false, "to_included": true}
          ]
        }
      ]
    },
    {
      "fault_id": "F2", "branch_id": "B3", "status": "qualified",
      "main_breaker_id": "CB2",
      "pairs": [
        {"upstream_breaker_id": "CB1", "min_ratio": 1.1999999999999997,
         "min_ratio_current": 1000.0, "qualified": true, "violations": []}
      ]
    },
    {
      "fault_id": "F3", "branch_id": "B2", "status": "qualified",
      "main_breaker_id": "CB2",
      "pairs": [
        {"upstream_breaker_id": "CB1", "min_ratio": 1.5,
         "min_ratio_current": 100.0, "qualified": true, "violations": []}
      ]
    },
    {
      "fault_id": "F4", "branch_id": "B1", "status": "qualified",
      "main_breaker_id": "CB1", "pairs": []
    },
    {
      "fault_id": "F5", "branch_id": "B4", "status": "unprotected",
      "main_breaker_id": null, "pairs": []
    }
  ]
}
```

结果解读（与示例曲线一致）：

- **F1**（区间 [10, 1000]，R=1.8）：`t上/t主` 在 10A 处为 2.0、100A 处为 1.5、
  1000A 处为 1.2。比值在 I≈23.24A 处穿过 1.8，故不合格范围为
  **(23.24, 1000]**——下边界为交点（等号合格，`from_included=false`），
  上边界本身比值 1.2 < 1.8（`to_included=true`）。最小时间比 1.2 @ 1000A。
- **F2**（R=1.1）：最小比值 1.2 ≥ 1.1，处处合格。
- **F3**（区间 [10, 100]，R=1.5）：右端点比值恰为 1.5，**等号合格**。
- **F4**：主保护 CB1 之上无上级，直接合格。
- **F5**：路径上无任何断路器，无保护。

### 4. 核验单个故障

```bash
curl -s http://localhost:8000/scenarios/<id>/faults/F1/verification
```

```json
{
  "fault_id": "F1",
  "branch_id": "B3",
  "status": "unqualified",
  "main_breaker_id": "CB2",
  "pairs": [
    {
      "upstream_breaker_id": "CB1",
      "min_ratio": 1.1999999999999997,
      "min_ratio_current": 1000.0,
      "qualified": false,
      "violations": [
        {"current_from": 23.240176447111455, "current_to": 1000.0,
         "from_included": false, "to_included": true}
      ]
    }
  ]
}
```

### 5. 错误示例：非树拓扑（HTTP 422）

```bash
curl -s -X POST http://localhost:8000/scenarios \
  -H 'Content-Type: application/json' \
  -d '{"name":"bad","source":"S",
       "branches":[{"id":"B1","from":"S","to":"A"},
                   {"id":"B2","from":"A","to":"B"},
                   {"id":"B3","from":"B","to":"A"}],
       "curves":[],"faults":[]}'
```

```json
{"detail": ["节点 'A' 的入度为 2（树中应为 1），拓扑不是树"]}
```

### 6. 错误示例：路径上曲线覆盖不足（HTTP 422）

故障区间 `[5, 500]` 超出曲线电流范围 `[10, 100]`（禁止外推）：

```json
{
  "detail": [
    "故障 'F9' 的电流区间 [5.0, 500.0] 超出路径上断路器 'CB2' 的曲线 'C2' 电流范围 [10.0, 100.0]（禁止外推，覆盖不足）",
    "故障 'F9' 的电流区间 [5.0, 500.0] 超出路径上断路器 'CB1' 的曲线 'C1' 电流范围 [10.0, 100.0]（禁止外推，覆盖不足）"
  ]
}
```

### 7. 协调方案：有解（HTTP 200，`status=ok`）

示例场景见 [`examples/coordination-scenario.json`](examples/coordination-scenario.json)
（与 `examples/scenario.json` 同拓扑同曲线，故障为 F1–F4，均有主保护），
请求体见 [`examples/coordination-request.json`](examples/coordination-request.json)：

```bash
curl -s -X POST http://localhost:8000/scenarios \
  -H 'Content-Type: application/json' \
  -d @examples/coordination-scenario.json        # 201，记返回 id 为 <id2>

curl -s -X POST http://localhost:8000/scenarios/<id2>/coordination-plan \
  -H 'Content-Type: application/json' \
  -d @examples/coordination-request.json
```

请求体：

```json
{
  "adjustments": [
    {"breaker_id": "CB1", "k_min": 0.5, "k_max": 2.0},
    {"breaker_id": "CB2", "k_min": 0.5, "k_max": 2.0}
  ]
}
```

响应 `200 OK`（F1 要求 `k_CB1/k_CB2 ≥ 1.8/1.2 = 1.5`，最优解为
`k_CB1 = √1.5 ≈ 1.2247`、`k_CB2 = 1/√1.5 ≈ 0.8165`，目标值 `ln(1.5)/2 ≈ 0.20273`）：

```json
{
  "scenario_id": "9772d042a32341f0ad90fe0e49bf6d5f",
  "status": "ok",
  "objective": 0.20273255505358234,
  "multipliers": [
    {"breaker_id": "CB1", "k": 1.2247448701674568, "ln_k": 0.20273255305458232},
    {"breaker_id": "CB2", "k": 0.8164965801116376, "ln_k": -0.20273255505358234}
  ],
  "verification": {
    "scenario_id": "9772d042a32341f0ad90fe0e49bf6d5f",
    "scenario_name": "coordination-demo",
    "summary": {"total": 4, "qualified": 4, "unqualified": 0, "unprotected": 0},
    "faults": [
      {
        "fault_id": "F1", "branch_id": "B3", "status": "qualified",
        "main_breaker_id": "CB2",
        "pairs": [
          {"upstream_breaker_id": "CB1", "min_ratio": 1.8,
           "min_ratio_current": 1000.0, "qualified": true, "violations": []}
        ]
      },
      {
        "fault_id": "F2", "branch_id": "B3", "status": "qualified",
        "main_breaker_id": "CB2",
        "pairs": [
          {"upstream_breaker_id": "CB1", "min_ratio": 1.8,
           "min_ratio_current": 1000.0, "qualified": true, "violations": []}
        ]
      },
      {
        "fault_id": "F3", "branch_id": "B2", "status": "qualified",
        "main_breaker_id": "CB2",
        "pairs": [
          {"upstream_breaker_id": "CB1", "min_ratio": 2.2500000000000004,
           "min_ratio_current": 100.0, "qualified": true, "violations": []}
        ]
      },
      {
        "fault_id": "F4", "branch_id": "B1", "status": "qualified",
        "main_breaker_id": "CB1", "pairs": []
      }
    ]
  },
  "reasons": null
}
```

调整后 F1 的最小时间比恰为 1.8（**等号合格**），全部故障合格；
原场景的 `GET /scenarios/<id2>/verification` 结论保持不变
（仍为 `qualified: 3, unqualified: 1`）。

### 8. 协调方案：无解——无保护故障（HTTP 200，`status=infeasible`）

对第 1 步的 `demo-feeder` 场景（含无保护故障 F5）提交同一请求：

```bash
curl -s -X POST http://localhost:8000/scenarios/<id>/coordination-plan \
  -H 'Content-Type: application/json' \
  -d @examples/coordination-request.json
```

```json
{
  "scenario_id": "7d427391d0fe4db58fa3d56aaa6ddb40",
  "status": "infeasible",
  "objective": null,
  "multipliers": null,
  "verification": null,
  "reasons": {
    "unprotected_faults": ["F5"],
    "unadjusted_pair_violations": [],
    "constraint_conflict": false
  }
}
```

### 9. 协调方案：无解——约束冲突（HTTP 200，`status=infeasible`）

F1 需要 `k_CB1 ≥ 1.5`，但请求只允许 `k_CB1 ≤ 1.2`：

```bash
curl -s -X POST http://localhost:8000/scenarios/<id2>/coordination-plan \
  -H 'Content-Type: application/json' \
  -d '{"adjustments": [{"breaker_id": "CB1", "k_min": 1.0, "k_max": 1.2}]}'
```

```json
{
  "scenario_id": "9772d042a32341f0ad90fe0e49bf6d5f",
  "status": "infeasible",
  "objective": null,
  "multipliers": null,
  "verification": null,
  "reasons": {
    "unprotected_faults": [],
    "unadjusted_pair_violations": [],
    "constraint_conflict": true
  }
}
```

### 10. 协调方案：请求校验失败（HTTP 422）

未知断路器（重复 ID、倒置区间、非正/非有限倍率、空列表、空故障集同理）：

```bash
curl -s -X POST http://localhost:8000/scenarios/<id2>/coordination-plan \
  -H 'Content-Type: application/json' \
  -d '{"adjustments": [{"breaker_id": "CB9", "k_min": 1, "k_max": 2}]}'
```

```json
{"detail": ["调整的断路器在场景中不存在: 'CB9'"]}
```

### 11. 多场景整定：发布批次（HTTP 201）

下例对**两个**已保存场景（均用
[`examples/coordination-scenario.json`](examples/coordination-scenario.json)
创建，下文 id 记为 `<idA>`、`<idB>`）整定同一台设备 CB1：

```bash
curl -s -X POST http://localhost:8000/scenarios \
  -H 'Content-Type: application/json' \
  -d @examples/coordination-scenario.json   # 场景 A，记返回 id 为 <idA>
curl -s -X POST http://localhost:8000/scenarios \
  -H 'Content-Type: application/json' \
  -d @examples/coordination-scenario.json   # 场景 B，记返回 id 为 <idB>

curl -s -X POST http://localhost:8000/setting-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "batch-demo-0001",
    "scenarios": [
      {"scenario_id": "<idA>", "expected_revision": 0,
       "multipliers": {"CB1": 1.5}},
      {"scenario_id": "<idB>", "expected_revision": 0,
       "multipliers": {"CB1": 1.5}}
    ]
  }'
```

请求体模板另见
[`examples/setting-batch-request.json`](examples/setting-batch-request.json)
（其中 `<scenario-id-*>` 需替换为创建返回的 id）。以下为对本实现的真实响应
`201 Created`（CB1 动作时间 ×1.5 后，F1 的最小时间比 1.2×1.5=1.8 恰等号合格；
CB2 未列出故倍率为 1）：

```json
{
  "id": "b492ffcae25d4fcba0f83659e94791bb",
  "request_id": "batch-demo-0001",
  "seq": 1,
  "created_at": "2026-09-30T10:04:17.577965+00:00",
  "replayed": false,
  "scenarios": [
    {
      "scenario_id": "13734a7dd6ce4e3094650e3531b9f303",
      "expected_revision": 0,
      "from_revision": 0,
      "to_revision": 1,
      "multipliers": [
        {"breaker_id": "CB1", "k": 1.5, "ln_k": 0.4054651081081644}
      ],
      "verification": {
        "scenario_id": "13734a7dd6ce4e3094650e3531b9f303",
        "scenario_name": "coordination-demo",
        "summary": {"total": 4, "qualified": 4, "unqualified": 0, "unprotected": 0},
        "faults": [
          {"fault_id": "F1", "branch_id": "B3", "status": "qualified",
           "main_breaker_id": "CB2",
           "pairs": [{"upstream_breaker_id": "CB1",
                      "min_ratio": 1.7999999999999996,
                      "min_ratio_current": 1000.0,
                      "qualified": true, "violations": []}]},
          {"fault_id": "F2", "branch_id": "B3", "status": "qualified",
           "main_breaker_id": "CB2",
           "pairs": [{"upstream_breaker_id": "CB1",
                      "min_ratio": 1.7999999999999996,
                      "min_ratio_current": 1000.0,
                      "qualified": true, "violations": []}]},
          {"fault_id": "F3", "branch_id": "B2", "status": "qualified",
           "main_breaker_id": "CB2",
           "pairs": [{"upstream_breaker_id": "CB1",
                      "min_ratio": 2.25,
                      "min_ratio_current": 100.0,
                      "qualified": true, "violations": []}]},
          {"fault_id": "F4", "branch_id": "B1", "status": "qualified",
           "main_breaker_id": "CB1", "pairs": []}
        ]
      }
    },
    {
      "scenario_id": "aaa8c8a08b924cb4a76d2339f9b54858",
      "expected_revision": 0,
      "from_revision": 0,
      "to_revision": 1,
      "multipliers": [
        {"breaker_id": "CB1", "k": 1.5, "ln_k": 0.4054651081081644}
      ],
      "verification": { "...": "与场景 A 结构相同：summary 为 4/4 合格，F1 最小比值 1.8" }
    }
  ]
}
```

### 12. 多场景整定：同内容重试回放（HTTP 200，先于版本检查）

把两个场景项的顺序颠倒后用**同一 `request_id`** 重发（规范化后内容相同）。
此时两场景已在版本 1、请求里仍写 `expected_revision: 0`，但幂等回放先于
版本检查，返回 **200** 与原批次（`replayed=true`，`id` 不变，不产生新批次）：

```bash
curl -s -X POST http://localhost:8000/setting-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "batch-demo-0001",
    "scenarios": [
      {"scenario_id": "<idB>", "expected_revision": 0, "multipliers": {"CB1": 1.5}},
      {"scenario_id": "<idA>", "expected_revision": 0, "multipliers": {"CB1": 1.5}}
    ]
  }'
```

```json
{
  "id": "b492ffcae25d4fcba0f83659e94791bb",
  "request_id": "batch-demo-0001",
  "seq": 1,
  "created_at": "2026-09-30T10:04:17.577965+00:00",
  "replayed": true,
  "scenarios": [ "...与首次发布完全相同（且按 scenario_id 规范化排序）..." ]
}
```

### 13. 多场景整定：异文冲突（HTTP 409）

同一 `request_id` 但两个场景一致地把 CB1 改成 2.0（规范化后内容不同）→
**409**，原批次保持不变（`existing_batch_id` 指回原批次）：

```bash
curl -s -X POST http://localhost:8000/setting-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "batch-demo-0001",
    "scenarios": [
      {"scenario_id": "<idA>", "expected_revision": 0, "multipliers": {"CB1": 2.0}},
      {"scenario_id": "<idB>", "expected_revision": 0, "multipliers": {"CB1": 2.0}}
    ]
  }'
```

```json
{
  "detail": "同一 request_id 已提交过内容不同的整定批次（异文冲突）；同内容重试才会回放原批次",
  "existing_batch_id": "b492ffcae25d4fcba0f83659e94791bb"
}
```

### 14. 多场景整定：版本过期（HTTP 409，不部分生效）

换一个新 `request_id`，但仍带 `expected_revision: 0`（两场景当前已是版本 1）
→ **409**，响应体逐项给出冲突；两个场景版本都停留在 1，没有部分推进：

```json
{
  "detail": "场景当前版本与 expected_revision 不符，整定批次未生效",
  "conflicts": [
    {"scenario_id": "13734a7dd6ce4e3094650e3531b9f303",
     "expected_revision": 0, "current_revision": 1},
    {"scenario_id": "aaa8c8a08b924cb4a76d2339f9b54858",
     "expected_revision": 0, "current_revision": 1}
  ]
}
```

跨进程并发争用同一组场景时（多个进程同时以 `expected_revision: 0` 提交），
SQLite 单写事务保证**恰有一批 201、其余全部 409**，最终两场景版本原子一致；
该行为由测试 `test_setting_batch_cross_process_contention_only_one_wins`
用 4 个独立进程验证。

### 15. 多场景整定：读取生效版本、倍率与核验

当前生效版本（倍率与核验同源，不混合版本）：

```bash
curl -s http://localhost:8000/scenarios/<idA>/effective-setting
```

```json
{
  "scenario_id": "13734a7dd6ce4e3094650e3531b9f303",
  "revision": 1,
  "batch_id": "b492ffcae25d4fcba0f83659e94791bb",
  "multipliers": [
    {"breaker_id": "CB1", "k": 1.5, "ln_k": 0.4054651081081644}
  ],
  "verification": {
    "scenario_id": "13734a7dd6ce4e3094650e3531b9f303",
    "scenario_name": "coordination-demo",
    "summary": {"total": 4, "qualified": 4, "unqualified": 0, "unprotected": 0},
    "faults": [ "...与批次中该场景冻结的核验完全一致..." ]
  }
}
```

历史版本（`revision=0`：无批次、倍率全 1、按场景原文核验，F1 仍不合格；
`revision` 超过当前版本返回 404）：

```bash
curl -s 'http://localhost:8000/scenarios/<idA>/effective-setting?revision=0'
```

```json
{
  "scenario_id": "13734a7dd6ce4e3094650e3531b9f303",
  "revision": 0,
  "batch_id": null,
  "multipliers": [],
  "verification": {
    "scenario_name": "coordination-demo",
    "summary": {"total": 4, "qualified": 3, "unqualified": 1, "unprotected": 0},
    "faults": [ "...F1 为 unqualified（最小比值 1.2 < 1.8）..." ]
  }
}
```

版本历史、批次列表、批次详情与按 request_id 查询：

```bash
curl -s http://localhost:8000/scenarios/<idA>/revisions
curl -s http://localhost:8000/setting-batches
curl -s http://localhost:8000/setting-batches/b492ffcae25d4fcba0f83659e94791bb
curl -s http://localhost:8000/setting-batches/by-request/batch-demo-0001
```

```json
{"scenario_id": "13734a7dd6ce4e3094650e3531b9f303",
 "revisions": [{"revision": 1, "batch_id": "b492ffcae25d4fcba0f83659e94791bb", "from_revision": 0}]}
```

```json
[
  {"id": "b492ffcae25d4fcba0f83659e94791bb",
   "request_id": "batch-demo-0001", "seq": 1,
   "created_at": "2026-09-30T10:04:17.577965+00:00",
   "scenario_count": 2}
]
```

### 16. 多场景整定：请求校验失败（HTTP 422）

只给一个场景（要求至少两个）、同一设备跨场景倍率不一致、未知断路器：

```bash
# 只有一个场景
curl -s -X POST http://localhost:8000/setting-batches \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"x","scenarios":[
       {"scenario_id":"<idA>","expected_revision":0,"multipliers":{}}]}'
# -> 422（Pydantic 结构错误）：{"detail":[{"type":"too_short", ... "min_length":2 ...}]}

# CB1 在两个场景中被赋不同倍率
curl -s -X POST http://localhost:8000/setting-batches \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"x","scenarios":[
       {"scenario_id":"<idA>","expected_revision":0,"multipliers":{"CB1":1.5}},
       {"scenario_id":"<idB>","expected_revision":0,"multipliers":{"CB1":2.0}}]}'
```

```json
{"detail": ["断路器 'CB1' 跨场景倍率不一致: 同一设备在不同场景中必须相同（1.5 与 2.0）"]}
```

```bash
curl -s -X POST http://localhost:8000/setting-batches \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"x","scenarios":[
       {"scenario_id":"<idA>","expected_revision":0,"multipliers":{"CB9":1.0}},
       {"scenario_id":"<idB>","expected_revision":0,"multipliers":{}}]}'
```

```json
{"detail": ["场景 '13734a7dd6ce4e3094650e3531b9f303' 中的断路器不存在: 'CB9'"]}
```

同理，批次内场景重复、未知场景、倍率为 0/负数/`Infinity`/`NaN`、
`expected_revision` 为负、场景故障集为空、存在无保护故障或提交倍率核验
不合格（整批拒绝，不部分生效）均返回 422。

## 项目结构

```
├── app/
│   ├── main.py       # FastAPI 入口与路由
│   ├── core.py       # 领域校验 + 选择性分析引擎（log-log 分段求值）
│   │                 #   + 协调方案求解（差分约束 + Bellman-Ford + 目标二分）
│   │                 #   + 多场景整定整批核验
│   ├── schemas.py    # Pydantic 请求/响应模型
│   └── db.py         # SQLite 仓库：不可变场景/批次 + 单写事务原子发布
├── tests/test_api.py # 端到端、算法与跨进程并发测试（49 例）
├── examples/scenario.json
├── examples/coordination-scenario.json
├── examples/coordination-request.json
├── examples/setting-batch-request.json
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
