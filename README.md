# 多设备现场录音时间轴对齐服务（timeline-align）

多台录音设备各自携带本地时钟，通过**同步锚点**（设备本地毫秒 ↔ UTC 毫秒）把录到的**声段**对齐到统一 UTC 时间轴。服务以 HTTP API 提供设备/**时钟周期**/锚点/声段录入、线性漂移对齐、锚点更正重算、不可变版本发布与版本间差异（位移/新增/移除）查询，以及面向现场复核的**覆盖审计**（判断指定 UTC 区间内是否始终有足够不同设备录到声音）。

- 纯 Python 标准库实现（`http.server` + `sqlite3`），**无第三方依赖**，Python ≥ 3.10
- SQLite 单文件存储，WAL 模式；写操作单事务（`BEGIN IMMEDIATE`），发布与更正并发时每个版本仍对应一套完整自洽的锚点+声段快照
- **时钟周期（cycle，v1.1 新增）**：设备每次重启本地毫秒归零，可按"周期"登记编号；不同周期可复用同一本地毫秒值，锚点递增校验与线性映射**按周期独立执行**，声段只使用所属周期的锚点。旧库启动即自动迁移，**已发布旧版本查询结果完全不变**。
- **周期封存（seal，v1.2 新增）**：现场核对完成后可封存单台设备的一个周期——封存请求携带周期详情中的内容指纹，在同一事务内核对当前内容、且全部声段可定位才生效；封存后该周期锚点/声段变更一律拒绝，防止晚到记录改动已确认时间线；封存不可撤销。旧库周期默认未封存。
- **周期归属批量纠正（reassign，v1.3 新增）**：现场整理时发现一批声段被记到错误的设备时钟周期，可按采集编号批量把它们在**同一设备的已登记周期**之间移动；每项声明预期原周期与目标周期，任一项无效（编号不存在/同批重复/原周期不符/目标未登记/源或目标已封存/原周期等于目标周期）即**整批拒绝、不改动任何数据**；成功后起止本地毫秒、备注、创建时间与采集编号保持不变，各声段按目标周期锚点重算定位，相关周期内容指纹随之更新；已发布版本保持原快照，之后发布的版本反映新归属。
- **覆盖审计（coverage audit，v1.4 新增）**：现场复核时判断指定 UTC 毫秒半开区间内是否**始终有足够不同设备录到声音**——只计可定位声段并与查询区间相交，同设备重叠声段只算一台，按设备集合变化切成最大半开片段（每段给出排序后的设备编号、数量与是否达标），不足门槛的相邻片段合并为连续缺口；可审计**当前时间轴**（一致快照）或**指定已发布版本**（历史审计只依据已冻结声段，后续锚点/周期变更不改动历史结果）；纯只读接口，无效输入或版本不存在明确拒绝且不改动数据。

## 快速开始

### 方式一：Docker Compose（推荐，一键启动并初始化数据库）

```bash
docker compose up --build
# 旧版 Compose：docker-compose up --build
```

### 方式二：Docker

```bash
docker build -t timeline-align .
docker run -d -p 8000:8000 -v timeline-data:/data --name timeline-align timeline-align
```

### 方式三：本地直接运行

```bash
python3 -m app          # 自动建库建表（默认 ./data/timeline.db），监听 8000
```

### 服务地址

- 默认地址：**http://localhost:8000**（容器/本地一致）
- 健康检查：`curl http://localhost:8000/health`
- 服务说明与端点清单：`curl http://localhost:8000/`

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TIMELINE_HOST` | `0.0.0.0` | 监听地址 |
| `TIMELINE_PORT` | `8000` | 监听端口 |
| `TIMELINE_DB_PATH` | 容器内 `/data/timeline.db`；本地运行 `./data/timeline.db` | SQLite 库文件路径，启动时自动初始化 |

## 核心概念与规则

### 时间表示

所有时间均为 **JSON 整数毫秒**（int64，`0 ~ 2^63-1`）：

- `local_ms`：设备**当前时钟周期内**的本地毫秒（如设备本轮开机计时的毫秒数，重启归零）
- `utc_ms`：Unix 纪元毫秒（UTC）

布尔、浮点、字符串、负数等一律视为**非法时间**，返回 `400 invalid_time` 且不写入。

### 时钟周期（cycle）

设备重启后本地毫秒重新从 0 起算，因此"本地毫秒"只有在同一轮计时（周期）内才可比较：

- **默认周期 `default`**：注册设备时自动创建；录入锚点/声段时省略 `cycle_id` 一律归入此周期，升级前的已有数据也全部视为 `default`。
- **登记周期**：`POST /devices/{id}/cycles` `{cycle_id, note?}` 登记一个设备内唯一的周期编号（命名规则同设备/采集编号）；同编号同 note 重传幂等，note 不一致 → `409 cycle_conflict`。
- **本地毫秒按周期复用**：锚点主键为 `(设备, 周期, local_ms)`，同一设备不同周期可使用相同的 `local_ms`；同一周期内仍要求本地时间严格唯一且 UTC 严格递增。
- **每周期独立映射**：锚点递增校验、线性漂移均在 `(设备, 周期)` 内独立执行；声段起止**只使用其所属周期的锚点**，所属周期覆盖不足仍为 `unlocatable`，绝不借用其他周期（包括 default）的锚点。
- **周期不存在即拒绝**：锚点/声段引用未登记周期 → `404 cycle_not_found`，且不写入任何数据。
- `GET /devices/{id}/cycles` 列出设备全部周期（含各周期锚点/声段计数与封存状态）；
  `GET /devices/{id}/anchors` 与 `GET /segments` 支持 `?cycle_id=` 过滤（按周期过滤声段时需同时给 `device_id`）。

### 周期封存（seal）

现场核对完成后，操作员可封存单台设备的一个时钟周期，防止晚到记录改动已确认时间线：

- **周期详情**：`GET /devices/{id}/cycles/{cycle_id}` 返回该周期的**内容稳定指纹**（由该周期全部锚点与声段内容生成的 sha256，内容不变指纹不变）与**封存状态**（`sealed` / `sealed_at`）。
- **封存**：`POST /devices/{id}/cycles/{cycle_id}/seal`，请求体 `{"fingerprint": "<详情中的指纹>"}`。服务在**同一事务内**重算当前指纹并核对，且该周期**全部声段均可定位**时才生效；成功返回封存时间（`sealed_at`）与最终指纹。
- **拒绝场景（状态不变）**：指纹与当前内容不一致（内容已被晚到记录改动、指纹过期）→ `409 seal_fingerprint_stale`；存在不可定位声段 → `409 seal_unlocatable_segments`（响应附不可定位采集编号列表）。
- **幂等**：重复**相同**封存请求（指纹与封存指纹一致）→ `200` 返回首次封存结果（`idempotent_replay: true`，封存时间不变）；已封存后携带不同指纹 → `409 seal_fingerprint_stale`。
- **不可撤销**：无解除封存入口；封存后该周期的锚点**新增/更正/删除**与声段**新增/删除**一律 `409 cycle_sealed`。
- **与既有规则共存**：封存周期内**同内容声段重传**仍按现有幂等规则返回（`200`，`idempotent_replay: true`），**异内容重传**仍按 `409 capture_conflict` 处理——二者都不修改内容，不视为变更。
- **更正的整体性**：一次 `PUT` 锚点更正若涉及任一已封存周期 → **整体拒绝**，其他周期不受影响（不写入）；只更正未封存周期不受影响。
- 旧库升级后所有周期**默认未封存**；已发布版本与未封存周期的原有行为完全保持。

### 周期归属批量纠正（reassign）

现场整理时，一批声段可能被记到错误的设备时钟周期。`POST /segments/reassign-cycle` 按采集编号批量纠正周期归属：

- **请求体**：`{"items": [{"capture_id", "from_cycle_id", "to_cycle_id"}, ...]}`——每项提交采集编号、**预期原周期**与**目标周期**；批次可跨设备，每项在**该声段所属设备**的已登记周期之间移动（设备归属不变）。
- **保持不变**：起止本地毫秒、备注、创建时间与采集编号；仅周期归属与内容指纹（按新周期重算）更新。
- **整批原子**：任一编号不存在（`404 capture_not_found`）、同批重复（`409 reassign_duplicate`）、原周期与预期不符（`409 reassign_cycle_mismatch`）、目标周期未在该设备登记（`404 cycle_not_found`，含只在别的设备上登记的同名周期）、源/目标周期已封存（`409 cycle_sealed`），或原周期等于目标周期（`400 reassign_same_cycle`）——**整批拒绝且不改动任何数据**；错误响应 `details` 列出全部问题项，便于一次性修正后重提。
- **成功响应（200）**：`segments` 为各声段按**目标周期锚点**计算的当前定位结果（目标周期覆盖不足仍为 `unlocatable`）；`cycles` 为相关周期（源+目标）**更新后的内容指纹**，与周期详情一致，可直接用于后续封存核对。
- **与既有规则共存**：纠正后同编号重传仍遵守**同内容幂等、异内容冲突**（按新周期计内容指纹：新周期同内容重传幂等，按旧周期重传 → `409 capture_conflict`）；已发布版本保持原快照，之后发布的版本反映新归属（`changes.displaced` 体现投影位移）；批量纠正与锚点更正、封存、版本发布在同一写锁下串行执行，快照不会撕裂。

### 覆盖审计（coverage audit）

现场复核一段录音时，需要回答"指定 UTC 区间内是否**始终**有至少 N 台不同设备录到声音"。`POST /coverage/audit` 按输入的半开区间与最少设备数计算覆盖：

- **请求体**：`{"start_ms", "end_ms", "min_devices", "version_no?"}`——审计半开区间 `[start_ms, end_ms)`；`min_devices` 为最少设备数（正整数）；省略 `version_no`（或为 `null`）审计**当前时间轴**，指定则审计**该已发布版本**。
- **只计可定位声段**：不可定位声段不参与；每段可定位声段先与查询区间**相交裁剪**，零长度片段不贡献覆盖。
- **同设备去重**：同一设备的重叠（含首尾相接）声段只算一台——先按设备求区间并集，再跨设备统计同时在线的设备集合。
- **最大半开片段**：按设备集合变化把查询区间切成最大的半开片段，每段返回**排序后的设备编号** `device_ids`、数量 `device_count` 与是否达标 `meets_threshold`；边界相接不重叠（结束于 t 的声段不覆盖 t 之后，开始于 t 的恰好接上）。
- **缺口合并**：不足门槛的**相邻**片段合并为连续缺口 `gaps`（含段内出现过的设备并集与峰值设备数）；无数据时整段为一个缺口。
- **整体结论**：`ok` 表示整段是否始终达标（无缺口）；`covered_ms` / `gap_ms` 为达标/不足总时长；`located_segment_count` 为实际参与计算的可定位声段数。
- **一致快照**：当前视图在单个读事务的一致快照上计算；**历史审计只依据发布时冻结的声段投影**（`version_segments`），后续锚点更正、声段增删、周期归属纠正与新发布都**不会改动历史结果**。
- **只读与校验**：本接口不改动任何数据；`start_ms < end_ms` 且时间须为 `[0, 2^63-1]` 整数毫秒（否则 `400 invalid_range` / `400 invalid_time`），门槛须为正整数（否则 `400 invalid_threshold`），版本不存在 → `404 version_not_found`。

响应示例（节选）：

```json
{
  "source": "current", "version_no": null,
  "start_ms": 0, "end_ms": 15000, "min_devices": 2,
  "ok": false, "covered_ms": 5000, "gap_ms": 10000, "located_segment_count": 2,
  "fragments": [
    {"start_ms": 0,     "end_ms": 5000,  "device_ids": ["D1"],       "device_count": 1, "meets_threshold": false},
    {"start_ms": 5000,  "end_ms": 10000, "device_ids": ["D1", "D2"], "device_count": 2, "meets_threshold": true},
    {"start_ms": 10000, "end_ms": 15000, "device_ids": ["D2"],       "device_count": 1, "meets_threshold": false}
  ],
  "gaps": [
    {"start_ms": 0,     "end_ms": 5000,  "device_ids": ["D1"], "max_device_count": 1},
    {"start_ms": 10000, "end_ms": 15000, "device_ids": ["D2"], "max_device_count": 1}
  ]
}
```

### 线性漂移映射

每台设备、**每个时钟周期**的锚点按本地时间排序后，在相邻锚点 `(l0,u0)`、`(l1,u1)` 间线性映射：

```
utc(t) = u0 + (t - l0) * (u1 - u0) / (l1 - l0)     # 结果四舍五入到毫秒整数
```

- `t` 恰好等于某锚点本地时间 → 取该锚点 UTC；
- 声段**起点或终点任一时刻**落在**所属周期**锚点覆盖范围 `[首锚点, 末锚点]` 之外 → 该声段标记为 `unlocatable`（无法定位），UTC 字段为 `null`；
- 锚点更正（按周期整体替换或删除）后，相关周期声段投影**实时重算**（当前时间轴即最新结果）。

### 写入校验（失败即拒绝，不产生任何写入）

| 场景 | 响应 |
|---|---|
| 同一周期内锚点本地时间重复、或插入后 UTC 不严格递增 | `400 anchors_not_increasing` |
| 声段起点 > 终点（起止倒置） | `400 segment_inverted` |
| 设备不存在 | `404 device_not_found` |
| 锚点/声段引用的周期不存在 | `404 cycle_not_found` |
| 周期编号已存在且 note 不一致 | `409 cycle_conflict` |
| 非法时间（非整数/负数/超范围） | `400 invalid_time` |
| 缺字段、字段类型错误 | `400 validation_error` |
| 请求体非 JSON / 非对象 | `400 invalid_json` / `400 validation_error` |
| 同采集编号异内容重传（含周期不同） | `409 capture_conflict` |
| 设备号已存在且 name 不一致 | `409 device_conflict` |
| 封存指纹与当前内容不一致（指纹过期） | `409 seal_fingerprint_stale` |
| 封存时周期内存在不可定位声段 | `409 seal_unlocatable_segments` |
| 变更已封存周期的锚点/声段 | `409 cycle_sealed` |
| 批量纠正：任一采集编号不存在 | `404 capture_not_found` |
| 批量纠正：同批采集编号重复 | `409 reassign_duplicate` |
| 批量纠正：原周期与预期不符 | `409 reassign_cycle_mismatch` |
| 批量纠正：目标周期未在该设备登记 | `404 cycle_not_found` |
| 批量纠正：源/目标周期已封存 | `409 cycle_sealed` |
| 批量纠正：原周期等于目标周期 | `400 reassign_same_cycle` |
| 覆盖审计：起点不小于终点 | `400 invalid_range` |
| 覆盖审计：门槛非正整数 | `400 invalid_threshold` |
| 覆盖审计：版本不存在 | `404 version_not_found` |

### 幂等与冲突

- **声段**：`capture_id` **全库全局唯一**。内容指纹包含**周期**（设备、`cycle_id`、本地起止、note）：同编号**同内容**重传 → `200` 幂等返回已有记录（`idempotent_replay: true`），不重复写入；任一字段不同（包括仅 `cycle_id` 不同）→ `409 capture_conflict`，响应中附带已存内容与本次内容。
- **设备**：同 `device_id` 同 `name` 重复注册幂等；`name` 不一致 → `409`。
- **周期**：同 `(device_id, cycle_id)` 同 `note` 重复登记幂等；`note` 不一致 → `409 cycle_conflict`。注册设备即幂等创建其 `default` 周期。

### 版本发布

- `POST /versions` 把**当前全部周期的锚点与声段**冻结为不可变快照，版本号从 1 起连续递增；
- 新版（`format_rev=2`）文档包含：分周期完整锚点快照（每条带 `cycle_id`）、全部声段投影（含周期归属、定位状态与 UTC）、以及与上一版的差异 `changes`：
  - `added`：本版新增声段；`removed`：本版移除声段；
  - `displaced`：两版都存在但定位结果变化的声段，含 `previous`/`current` 及 `start_shift_ms`、`end_shift_ms`（位移毫秒数；任一端不可定位时为 `null`）；
- 声段排序：**可定位声段按 (UTC 起点, 设备编号, 采集编号) 升序稳定排序；无法定位声段排在最后**，按 (设备编号, 采集编号) 升序（排序与周期无关，周期仅作为投影属性展示）；
- 已发布版本**不可变**：锚点更正只影响"当前时间轴"和之后的版本，`GET /versions/{n}` 永远返回发布时刻的快照；
- **旧版本兼容**：v1.1 升级前发布的版本为 `format_rev=1`，查询时**不返回** `cycle_id`/`format_rev` 字段，内容与升级前逐字节一致；升级后新发布版本才带周期归属；
- **并发一致性**：发布与锚点更正在同一写锁下串行执行，每个版本的（分周期）锚点快照与声段投影必然来自同一时刻的完整状态，不会出现撕裂。

### 锚点更正（按周期）

- `PUT /devices/{id}/anchors`：请求体顶层可带 `cycle_id`（省略为 `default`），`anchors` 每一项也可各自带 `cycle_id`（缺省取顶层值）；每个被引用周期独立做严格递增校验，**仅替换被引用周期**的锚点，其他周期不变；任一引用周期不存在或校验失败 → 整体拒绝、不写入。
- `DELETE /devices/{id}/anchors/{local_ms}?cycle_id=...`：删除单个锚点；`cycle_id` 省略时删 `default` 周期（与旧版路径行为一致）。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/devices` | 注册设备 `{device_id, name?}`（自动创建 `default` 周期） |
| GET | `/devices` / `/devices/{id}` | 设备列表 / 详情（含周期与锚点） |
| POST | `/devices/{id}/cycles` | 登记时钟周期 `{cycle_id, note?}` |
| GET | `/devices/{id}/cycles` | 周期列表（含各周期锚点/声段计数与封存状态） |
| GET | `/devices/{id}/cycles/{cycle_id}` | **周期详情**：内容稳定指纹 + 封存状态 |
| POST | `/devices/{id}/cycles/{cycle_id}/seal` | **封存周期** `{fingerprint}`（不可撤销） |
| POST | `/devices/{id}/anchors` | 录入锚点 `{local_ms, utc_ms, cycle_id?}`（省略周期=default） |
| GET | `/devices/{id}/anchors` | 锚点列表（可加 `?cycle_id=`；每条带 `cycle_id`） |
| PUT | `/devices/{id}/anchors` | **锚点更正**：按周期整体替换 `{cycle_id?, anchors:[...]}`（原子生效） |
| DELETE | `/devices/{id}/anchors/{local_ms}?cycle_id=` | **锚点更正**：删除单个锚点（省略周期=default） |
| POST | `/segments` | 提交声段 `{capture_id, device_id, local_start_ms, local_end_ms, cycle_id?, note?}` |
| POST | `/segments/reassign-cycle` | **批量纠正周期归属** `{items:[{capture_id, from_cycle_id, to_cycle_id}]}`（整批原子） |
| GET | `/segments` / `/segments/{capture_id}` | 声段列表（可加 `?device_id=&cycle_id=`）/ 单个 |
| DELETE | `/segments/{capture_id}` | 删除声段（下一版体现为"移除"） |
| GET | `/timeline/current` | 当前时间轴（按现有锚点实时重算，显示周期归属） |
| POST | `/coverage/audit` | **覆盖审计** `{start_ms, end_ms, min_devices, version_no?}`（只读，省略版本号=当前时间轴） |
| POST | `/versions` | 发布新版本（可选 `{"note": "..."}`），冻结各周期锚点与投影 |
| GET | `/versions` / `/versions/{n}` | 版本列表 / 版本完整快照与差异（旧版本无周期字段） |

## 调用示例

### curl

```bash
BASE=http://localhost:8000

# 1. 注册设备
curl -s -X POST $BASE/devices -H 'Content-Type: application/json' \
  -d '{"device_id":"D1","name":"录音笔-甲"}'

# 2. 录入同步锚点（本地毫秒 <-> UTC 毫秒）
curl -s -X POST $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"local_ms":1000,"utc_ms":1700000000000}'
curl -s -X POST $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"local_ms":61000,"utc_ms":1700000060500}'

# 3. 提交声段
curl -s -X POST $BASE/segments -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20000}'
# => status=located, utc_start_ms=1700000004033, utc_end_ms=1700000019158（线性漂移映射）

# 4. 发布版本
curl -s -X POST $BASE/versions

# 5. 锚点更正（整体替换）后声段自动重算，再发新版可见位移
curl -s -X PUT $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"anchors":[{"local_ms":1000,"utc_ms":1700000000250},{"local_ms":61000,"utc_ms":1700000060750}]}'
curl -s -X POST $BASE/versions    # v2 的 changes.displaced 列出 CAP-0001 位移 +250ms
curl -s $BASE/versions/1          # v1 保持发布时原样
```

### 时钟周期（设备重启后本地毫秒归零）

```bash
BASE=http://localhost:8000

# 1. 登记重启后的新周期（设备内周期编号唯一；default 周期注册设备时已自动创建）
curl -s -X POST $BASE/devices/D1/cycles -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","note":"第二次开机，本地毫秒归零"}'
curl -s $BASE/devices/D1/cycles

# 2. 新周期录入锚点：local_ms 可与 default 周期重复；各周期独立做递增校验与映射
curl -s -X POST $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","local_ms":1000,"utc_ms":1700010000000}'
curl -s -X POST $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","local_ms":61000,"utc_ms":1700010060500}'
curl -s "$BASE/devices/D1/anchors?cycle_id=boot-2"

# 3. 声段指定周期；相同本地起止在不同周期映射到不同 UTC
#    省略 cycle_id 即归入 default；引用未登记周期 -> 404 cycle_not_found 不写入
curl -s -X POST $BASE/segments -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-1001","device_id":"D1","cycle_id":"boot-2","local_start_ms":5000,"local_end_ms":20000}'
curl -s "$BASE/segments?device_id=D1&cycle_id=boot-2"

# 4. 采集编号全局唯一：同编号仅当周期也一致才算同内容；仅周期不同 -> 409 capture_conflict
curl -s -X POST $BASE/segments -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","cycle_id":"boot-2","local_start_ms":5000,"local_end_ms":20000}'

# 5. 当前时间轴：cycles/anchors/segments 均带周期归属
curl -s $BASE/timeline/current

# 6. 发布：原子冻结各周期锚点与声段投影（新版 format_rev=2，文档带 cycle_id）
curl -s -X POST $BASE/versions -H 'Content-Type: application/json' -d '{"note":"加入重启周期"}'

# 7. 只更正 boot-2 周期（default 等其他周期不受影响）
curl -s -X PUT $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","anchors":[{"local_ms":1000,"utc_ms":1700010000500},{"local_ms":61000,"utc_ms":1700010061000}]}'
# 也可一次替换多个周期：anchors 各项自带 cycle_id（缺省项归入顶层 cycle_id/default）
```

### 周期封存（现场核对完成后冻结周期）

```bash
BASE=http://localhost:8000

# 1. 查看周期详情：内容稳定指纹 + 封存状态（未封存时 fingerprint 为当前内容实时计算）
curl -s $BASE/devices/D1/cycles/default
# => {"cycle":{"device_id":"D1","cycle_id":"default","anchor_count":2,"segment_count":1,
#      "sealed":false,"sealed_at":null,"fingerprint":"9f2c...(64位hex)"}}

# 2. 携带指纹封存（若期间有晚到记录改动内容 -> 409 seal_fingerprint_stale；
#    若周期内存在不可定位声段 -> 409 seal_unlocatable_segments，均不生效）
FP=$(curl -s $BASE/devices/D1/cycles/default | python3 -c "import sys,json;print(json.load(sys.stdin)['cycle']['fingerprint'])")
curl -s -X POST $BASE/devices/D1/cycles/default/seal -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP\"}"
# => 201 {"cycle":{"device_id":"D1","cycle_id":"default","sealed":true,
#         "sealed_at":"2026-10-05T08:00:00.000Z","fingerprint":"9f2c..."},"idempotent_replay":false}

# 3. 重复相同封存请求 -> 200 返回首次结果（封存时间不变）；封存不可撤销
curl -s -X POST $BASE/devices/D1/cycles/default/seal -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP\"}"

# 4. 封存后该周期锚点/声段变更一律 409 cycle_sealed
curl -s -X POST $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"local_ms":30000,"utc_ms":1700000030000}'
curl -s -X DELETE $BASE/segments/CAP-0001

# 5. 同内容声段重传仍按幂等规则返回；异内容仍 409 capture_conflict
curl -s -X POST $BASE/segments -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20000}'

# 6. 涉及已封存周期的锚点更正整体拒绝；未封存周期不受影响
curl -s -X PUT $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"anchors":[{"cycle_id":"default","local_ms":1000,"utc_ms":1700000000250},
                  {"cycle_id":"boot-2","local_ms":1000,"utc_ms":1700010000000}]}'
# => 409 cycle_sealed（boot-2 也不会被写入）
```

### 周期归属批量纠正（现场整理时声段记错周期）

```bash
BASE=http://localhost:8000

# 0. 再登记一个周期并录入锚点作为演示目标（default 已封存，不能参与纠正）
curl -s -X POST $BASE/devices/D1/cycles -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-3","note":"第三次开机"}'
curl -s -X POST $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-3","local_ms":1000,"utc_ms":1700020000000}'
curl -s -X POST $BASE/devices/D1/anchors -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-3","local_ms":61000,"utc_ms":1700020060500}'

# 1. 现场整理发现 CAP-1001 实际录在 boot-3 周期（此前误记到 boot-2）：
#    按采集编号批量纠正，每项声明 预期原周期 -> 目标周期（同设备已登记周期之间）
curl -s -X POST $BASE/segments/reassign-cycle -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-1001","from_cycle_id":"boot-2","to_cycle_id":"boot-3"}]}'
# => 200 {"moved":1,
#         "segments":[{"capture_id":"CAP-1001","cycle_id":"boot-3","status":"located",
#                      "utc_start_ms":..., "utc_end_ms":..., ...}],   # 按目标周期锚点重算
#         "cycles":[{"device_id":"D1","cycle_id":"boot-2","fingerprint":"..."},
#                   {"device_id":"D1","cycle_id":"boot-3","fingerprint":"..."}]}  # 相关周期新指纹
#    起止本地毫秒、备注、创建时间与采集编号均不变；目标周期覆盖不足时该段保持 unlocatable

# 2. 纠正后同编号重传：按新周期同内容 -> 幂等；按旧周期 -> 409 capture_conflict
curl -s -X POST $BASE/segments -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-1001","device_id":"D1","cycle_id":"boot-3","local_start_ms":5000,"local_end_ms":20000}'
curl -s -X POST $BASE/segments -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-1001","device_id":"D1","cycle_id":"boot-2","local_start_ms":5000,"local_end_ms":20000}'

# 3. 任一项无效 -> 整批拒绝，不改动任何数据（details 列出全部问题项）
curl -s -X POST $BASE/segments/reassign-cycle -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-0001","from_cycle_id":"default","to_cycle_id":"boot-3"},
                {"capture_id":"CAP-9999","from_cycle_id":"default","to_cycle_id":"boot-3"}]}'
# => 404 capture_not_found（CAP-0001 也不会被移动）
curl -s -X POST $BASE/segments/reassign-cycle -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-1001","from_cycle_id":"boot-2","to_cycle_id":"default"}]}'
# => 409 reassign_cycle_mismatch（CAP-1001 当前在 boot-3，原周期与预期不符）
curl -s -X POST $BASE/segments/reassign-cycle -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-1001","from_cycle_id":"boot-3","to_cycle_id":"boot-3"}]}'
# => 400 reassign_same_cycle（原周期等于目标周期，无效）

# 4. 已发布版本保持原快照；之后发布的版本反映新归属（changes.displaced 体现位移）
curl -s -X POST $BASE/versions -H 'Content-Type: application/json' -d '{"note":"纠正周期归属"}'

# 5. 源/目标周期已封存 -> 409 cycle_sealed（default 已封存，归属已冻结）
curl -s -X POST $BASE/segments/reassign-cycle -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-0001","from_cycle_id":"default","to_cycle_id":"boot-3"}]}'
```

### 覆盖审计（现场复核：区间内是否始终有足够设备在录音）

```bash
BASE=http://localhost:8000

# 1. 审计当前时间轴：半开区间 [start_ms,end_ms) + 最少设备数
#    只计可定位声段（与查询区间相交）；同设备重叠只算一台；
#    按设备集合变化切最大半开片段；不足门槛的相邻片段合并为缺口
curl -s -X POST $BASE/coverage/audit -H 'Content-Type: application/json' \
  -d '{"start_ms":1699999997000,"end_ms":1700000062000,"min_devices":2}'
# => {"source":"current","version_no":null,"ok":false,
#     "covered_ms":...,"gap_ms":...,"located_segment_count":...,
#     "fragments":[{"start_ms":...,"end_ms":...,"device_ids":["D1","D2"],
#                   "device_count":2,"meets_threshold":true}, ...],
#     "gaps":[{"start_ms":...,"end_ms":...,"device_ids":["D1"],
#              "max_device_count":1}, ...]}

# 2. 审计已发布版本：历史审计只依据冻结声段，
#    之后的锚点更正、声段增删、周期变更都不改动历史结果
curl -s -X POST $BASE/coverage/audit -H 'Content-Type: application/json' \
  -d '{"start_ms":1699999997000,"end_ms":1700000062000,"min_devices":2,"version_no":1}'

# 3. 非法输入一律拒绝且不改动数据：
#    起点不小于终点 -> 400 invalid_range；门槛非正整数 -> 400 invalid_threshold；
#    时间非整数/越界 -> 400 invalid_time；版本不存在 -> 404 version_not_found
curl -s -X POST $BASE/coverage/audit -H 'Content-Type: application/json' \
  -d '{"start_ms":2000,"end_ms":1000,"min_devices":1}'
curl -s -X POST $BASE/coverage/audit -H 'Content-Type: application/json' \
  -d '{"start_ms":0,"end_ms":1000,"min_devices":0}'
curl -s -X POST $BASE/coverage/audit -H 'Content-Type: application/json' \
  -d '{"start_ms":0,"end_ms":1000,"min_devices":1,"version_no":999}'
```

完整演示脚本（覆盖幂等、冲突、越界、排序、版本差异）：

```bash
bash examples/demo.sh                 # curl 版
python3 examples/client.py            # Python 客户端版
```

### 响应示例

`POST /versions` 返回（v1.1 周期版，节选）：

```json
{
  "version_no": 2,
  "format_rev": 2,
  "based_on_version": 1,
  "created_at": "2026-10-04T12:00:00.000Z",
  "anchors": [
    {"device_id": "D1", "cycle_id": "default", "local_ms": 1000, "utc_ms": 1700000000250},
    {"device_id": "D1", "cycle_id": "boot-2", "local_ms": 1000, "utc_ms": 1700010000000}
  ],
  "segments": [
    {"capture_id": "CAP-0001", "device_id": "D1", "cycle_id": "default", "status": "located",
     "local_start_ms": 5000, "local_end_ms": 20000,
     "utc_start_ms": 1700000004283, "utc_end_ms": 1700000019408},
    {"capture_id": "CAP-0003", "device_id": "D1", "cycle_id": "boot-2", "status": "unlocatable",
     "utc_start_ms": null, "utc_end_ms": null}
  ],
  "changes": {
    "from_version": 1,
    "added": [],
    "removed": [],
    "displaced": [
      {"capture_id": "CAP-0001", "device_id": "D1",
       "previous": {"status": "located", "utc_start_ms": 1700000004033},
       "current":  {"status": "located", "utc_start_ms": 1700000004283},
       "start_shift_ms": 250, "end_shift_ms": 250}
    ]
  }
}
```

> 升级前发布的旧版本（`format_rev=1`）查询时不出现 `cycle_id`/`format_rev` 字段，结果与升级前完全一致。

错误响应统一为：

```json
{"error": {"code": "capture_conflict", "message": "采集编号 CAP-0001 已存在且内容不一致，拒绝写入", "details": {}}}
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

端到端用例覆盖：

- `tests/test_api.py`（17 个）：锚点严格递增校验、非法时间、起止倒置、幂等/冲突、线性漂移与取整、无法定位、排序规则、版本不可变、位移/新增/移除、以及**更正与发布并发**下快照完整性；
- `tests/test_cycles.py`（19 个）：周期登记幂等/冲突、默认周期、跨周期复用本地毫秒、各周期独立递增与独立映射、声段仅用所属周期锚点（覆盖不足仍 `unlocatable`）、周期不存在拒绝不写入、采集编号全局唯一且**周期计入内容**、时间轴/新版显示周期归属、发布原子冻结各周期、以及 **v1 旧库在线迁移**（已有数据归 default、旧声段默认周期重传仍幂等、已发布旧版本查询结果逐字段不变、迁移后发布为周期格式）；
- `tests/test_seal.py`（18 个）：周期详情指纹与封存状态、指纹稳定性与内容敏感性、封存成功返回封存时间与最终指纹、重复封存返回首次结果、封存不可撤销、指纹过期/不可定位声段拒绝且状态不变、封存后锚点与声段写保护、同内容重传幂等与异内容冲突保持既有规则、涉封存周期的更正整体拒绝且其他周期不受影响、与版本发布共存、**旧库迁移后周期默认未封存且可正常封存**；
- `tests/test_reassign.py`（18 个）：批量纠正成功与字段保持、跨设备批次、互换周期、目标覆盖不足仍不可定位、时间轴/列表/计数同步、相关周期指纹更新而无关周期不变、纠正后重传幂等/冲突规则、编号不存在/同批重复/原周期不符/目标未登记（含跨设备同名）/源或目标已封存/原周期等于目标周期的整批拒绝且不改动数据、已发布版本不变而新版反映新归属、以及**纠正+锚点更正+发布并发**下快照自洽；
- `tests/test_coverage.py`（15 个）：覆盖审计的片段切分与达标判定、同设备重叠只算一台、边界相接不重叠、零长度声段不贡献、不可定位声段不计、无数据整段为缺口、相邻缺口合并、查询区间裁剪、int64 边界、**历史版本审计冻结**（后续锚点更正/声段增删/周期纠正/新发布均不改历史结果）、非法输入与版本不存在拒绝且数据不变、审计只读、以及**审计与更正/发布并发**下每次响应都是一致快照。

## 升级与数据迁移（v1.0 → v1.1 → v1.2 → v1.3 → v1.4）

- 沿用原启动方式（`python3 -m app` / Docker / Compose），服务启动时自动识别并在线迁移旧库，无需手工操作；
- 旧设备自动补登 `default` 周期；旧锚点与声段一律归入 `default`；旧声段内容指纹按含周期的新口径重算（默认周期同内容重传仍为幂等）；
- `anchors` / `version_anchors` 主键扩展为含 `cycle_id`（整表重建，数据保留）；`segments` / `version_segments` 增加 `cycle_id` 列；
- 已发布旧版本标记为 `format_rev=1` 且周期列为空，`GET /versions/{n}` 返回结果与升级前完全一致；升级后新发布为 `format_rev=2`；
- v1.2 起 `cycles` 表增加 `sealed_at` / `seal_fingerprint` 两列，旧库启动时自动补列，**旧周期一律默认未封存**，未封存周期的所有行为与升级前一致；
- v1.3 新增 `POST /segments/reassign-cycle` 批量纠正端点，**无表结构变更**，旧库直接可用；
- v1.4 新增 `POST /coverage/audit` 覆盖审计端点（只读），**无表结构变更**，旧库直接可用；历史版本审计基于既有 `version_segments` 冻结投影，旧版本（含 `format_rev=1`）同样可审计。

## 项目结构

```
app/
  __main__.py   # 入口：初始化数据库 + 启动 HTTP 服务
  server.py     # HTTP 路由、JSON 编解码、错误响应（http.server）
  service.py    # 服务层：各 API 操作（单事务校验+写入）
  logic.py      # 纯逻辑：校验、线性漂移映射、排序、版本差异、周期内容指纹
  db.py         # SQLite 连接、建表、旧库迁移（v1 整表迁移 + 封存列补齐）、事务助手（WAL）
tests/test_api.py      # 端到端测试（原有能力，保持兼容）
tests/test_cycles.py   # 端到端测试（时钟周期 + 旧库迁移/旧版本不变）
tests/test_seal.py     # 端到端测试（周期封存 + 旧库默认未封存）
tests/test_reassign.py # 端到端测试（周期归属批量纠正 + 并发一致性）
tests/test_coverage.py # 端到端测试（覆盖审计 + 历史冻结 + 并发一致性）
examples/demo.sh       # curl 调用示例（含周期、封存、批量纠正与覆盖审计用法）
examples/client.py     # Python 客户端示例（含周期、封存、批量纠正与覆盖审计用法）
Dockerfile / docker-compose.yml
```
