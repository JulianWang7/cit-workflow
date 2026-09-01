# citfix 批量解决 Bug 接口（预留）

> 版本：v0.1（契约预留，实现待落地）  
> 日期：2026-09-01  
> 对齐：android-bugfix-flow `batch_solve` / `batch_state_query` / `zentao_update_bug`  
> 契约文件：`workflow/citfix_batch_api.json`

## 1. 现状对照（bugfix）

| 能力 | bugfix 现状 | 结论 |
|------|-------------|------|
| 传入多个 Bug ID | Skill：`/batch_solve 96110,94536`；CLI：`bugfix-batch --bug-id 123,456`；Excel 列表 | **支持**问题列表 |
| 批次 ID | 按自然日文件 `batch_state_YYYY-MM-DD.json`，无独立 `batch_id` 字段 | **弱支持**（日期即批次） |
| 批量更新本地进度 | `batch_state_query(action=update)` **每次仅 1 个** `bug_id` | **无真正批量 update** |
| 批量写禅道状态/备注 | `zentao_update_bug(bug_id, status, comment)` **单条**；CLI/Skill 循环调用 | **无批量写回 API** |
| 操作人 | 无显式 `operator` 参数（隐式鉴权账号） | **未建模** |
| 解决结果 / 备注 | `fix_status` + `notes`（本地）；禅道 `status` + `comment`（单条） | **字段分散** |
| 去重 / 失败重试 / 结果统计 | `get_pending_bugs` 跳终态；`retry_count≥3`；`state_summary` | **有雏形，非统一请求体** |

结论：bugfix **支持多 ID 入队跑流水线**，但 **不具备**「一次请求批量更新禅道状态 + 批量添加解决备注」的单一接口；本地状态亦为单 Bug 更新。

## 2. citfix 批量接口定位

| 项 | 说明 |
|----|------|
| 名称 | `citfix.batch_resolve` |
| 角色 | CIT 自动化流水线的**批量闭环写回 / 批次编排入口**（与单 Bug `/citfix {id}` 互补） |
| 对齐原则 | 请求体字段与 bugfix 批处理语义一致：批次、问题 ID 数组、操作人、解决结果、备注 |
| 实现形态（预留） | MCP 工具优先；可选 CLI / 内部 Python API；暂无 HTTP 服务时路径以逻辑路径表示 |

## 3. 接口路径

| 形态 | 路径 / 调用名 | 说明 |
|------|----------------|------|
| MCP（首选） | `citfix_batch_resolve` | cit-workflow MCP / misc 扩展预留名 |
| 逻辑 REST（预留） | `POST /citfix/v1/batch/resolve` | 将来若挂控制面服务时对齐 |
| CLI（预留） | `python scripts/citfix_batch.py resolve --request <json>` | 与 MCP 同 schema |
| 内部 Python | `citfix.batch.resolve(BatchResolveRequest) -> BatchResolveResult` | `tests/citfix/` 落地 |

配套查询（扩展预留，本版仅声明）：

| 操作 | 路径 |
|------|------|
| 查批次 | `GET /citfix/v1/batch/{batch_id}` / MCP `citfix_batch_query` |
| 重试失败项 | `POST /citfix/v1/batch/{batch_id}/retry` / MCP `citfix_batch_retry` |
| 汇总统计 | `GET /citfix/v1/batch/{batch_id}/stats` / MCP `citfix_batch_stats` |

## 4. 请求参数

### 4.1 主接口 `citfix_batch_resolve`

```json
{
  "schema_version": "1.0",
  "batch_id": "CIT-BATCH-20260901-001",
  "bug_ids": [97203, 97210, 97211],
  "operator": "zhang.san",
  "resolve_result": "resolved",
  "comment": "CIT 流水线验证通过：SAR 传感器门限修正，见 run CIT-20260901-97203-002",
  "options": {
    "dedupe": true,
    "update_zentao_status": true,
    "add_zentao_comment": true,
    "update_local_state": true,
    "fail_fast": false,
    "max_retries": 3,
    "dry_run": false
  },
  "per_bug_overrides": [
    {
      "bug_id": 97210,
      "resolve_result": "active",
      "comment": "该项暂不关闭，备注仅记录分析结论"
    }
  ],
  "context": {
    "project": "MT5825",
    "run_ids": {},
    "source": "citfix"
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | 契约版本，当前 `"1.0"` |
| `batch_id` | string | 是* | 批次 ID；缺省时服务端生成 `CIT-BATCH-<date>-<seq>` |
| `bug_ids` | int[] | 是 | 问题 ID 数组；允许空数组仅当 `per_bug_overrides` 提供 ID |
| `operator` | string | 是 | 操作人（工号/账号）；写入备注前缀与审计 |
| `resolve_result` | string | 是 | 批次默认解决结果：禅道侧映射见 §4.2 |
| `comment` | string | 否 | 批次默认解决备注；空则只改状态不写评论 |
| `options` | object | 否 | 行为开关，见下表 |
| `per_bug_overrides` | object[] | 否 | 单 Bug 覆盖 `resolve_result` / `comment` |
| `context` | object | 否 | CIT 扩展：项目、run 映射、来源 |

\* 客户端可不传 `batch_id`；服务端生成后在响应中回传。

#### options

| 字段 | 默认 | 说明 |
|------|------|------|
| `dedupe` | true | 对 `bug_ids` + overrides 去重，保序 |
| `update_zentao_status` | true | 写禅道 status |
| `add_zentao_comment` | true | 写禅道 comment（失败不阻塞 status，对齐 bugfix） |
| `update_local_state` | true | 更新 citfix / batch_state 本地进度 |
| `fail_fast` | false | true：首个致命项后停止后续；false：逐项继续 |
| `max_retries` | 3 | 单 Bug 瞬时失败重试上限（网络/超时） |
| `dry_run` | false | 只校验与汇总，不写禅道/不改状态 |

### 4.2 resolve_result 映射

| `resolve_result` | 禅道 `status` | 本地 `fix_status`（对齐 bugfix） |
|------------------|---------------|-----------------------------------|
| `resolved` | `resolved` | `已修复已验证` |
| `active` | `active` | （不改终态 / 或 `可复现未修复`） |
| `closed` | `closed` | `已修复已验证` |
| `skip` | （不改） | 跳过该项 |
| `manual` | （不改） | `需人工介入` |

单 Bug 最终字段 = `per_bug_overrides[bug_id]` 覆盖批次默认值。

### 4.3 与 bugfix 字段对照

| citfix 批量请求 | bugfix 对应 |
|-----------------|-------------|
| `batch_id` | `batch_state_{date}.json` 的 date + 扩展显式 ID |
| `bug_ids[]` | `/batch_solve a,b,c` / `--bug-id a,b,c` |
| `operator` | （新增显式字段） |
| `resolve_result` | `fix_status` + `zentao status` |
| `comment` | `notes` + `zentao comment` |
| `options.max_retries` | `retry_count` / ≥3 人工介入 |

## 5. 返回结果

```json
{
  "schema_version": "1.0",
  "ok": true,
  "batch_id": "CIT-BATCH-20260901-001",
  "operator": "zhang.san",
  "stats": {
    "total": 3,
    "succeeded": 2,
    "failed": 1,
    "skipped": 0,
    "deduped_removed": 0,
    "retried": 1
  },
  "items": [
    {
      "bug_id": 97203,
      "status": "succeeded",
      "zentao_status": "resolved",
      "comment_written": true,
      "local_state_updated": true,
      "attempts": 1,
      "message": "状态 → resolved, 备注已添加"
    },
    {
      "bug_id": 97210,
      "status": "failed",
      "zentao_status": null,
      "comment_written": false,
      "local_state_updated": true,
      "attempts": 3,
      "error_code": "ZENTAO_TIMEOUT",
      "message": "禅道状态更新超时"
    }
  ],
  "errors": [
    {
      "bug_id": 97210,
      "error_code": "ZENTAO_TIMEOUT",
      "retryable": true,
      "message": "禅道状态更新超时"
    }
  ],
  "created_at": "2026-09-01T13:50:00+08:00",
  "finished_at": "2026-09-01T13:50:12+08:00"
}
```

| 字段 | 说明 |
|------|------|
| `ok` | 批次级：无致命错误且 `failed==0` 为 true；部分失败时 `ok=false` 但仍返回完整 `items` |
| `stats` | 汇总：总数/成功/失败/跳过/去重剔除/重试次数 |
| `items[]` | 按处理后顺序的逐 Bug 结果 |
| `errors[]` | 失败项摘要（便于重试入口直接消费） |

HTTP 预留：业务部分失败仍建议 **HTTP 200 + `ok:false`**；参数非法 **400**；鉴权失败 **401/403**；基础设施不可用 **503**。

## 6. 异常处理方案

### 6.1 分级

| 级别 | 条件 | 行为 |
|------|------|------|
| 请求级致命 | schema 非法、空 ID、鉴权失败、状态文件不可写 | 整批拒绝，不写禅道 |
| 基础设施致命 | 禅道连续不可达、磁盘满 | `fail_fast` 强制生效；已成功项保留；返回 `error_code=INFRA_*` |
| 单 Bug 非致命 | 单 ID 不存在、状态冲突、评论失败 | 记入 `items`/`errors`，继续下一项 |
| 评论降级 | status 成功、comment 失败 | `status=succeeded`，`comment_written=false`，`message` 带 warning（对齐 bugfix） |

### 6.2 错误码（预留）

| `error_code` | 含义 | 可重试 |
|--------------|------|--------|
| `INVALID_REQUEST` | 参数/schema 非法 | 否 |
| `EMPTY_BUG_LIST` | 去重后无有效 ID | 否 |
| `AUTH_FAILED` | 禅道鉴权失败 | 否 |
| `BUG_NOT_FOUND` | Bug 不存在 | 否 |
| `ZENTAO_TIMEOUT` | 写回超时 | 是 |
| `ZENTAO_REJECTED` | 禅道业务拒绝 | 否 |
| `STATE_IO_ERROR` | 本地状态落盘失败 | 视情况 |
| `RETRY_EXHAUSTED` | 超过 `max_retries` | 否（需人工或 `batch_retry`） |
| `INFRA_UNAVAILABLE` | 基础设施不可用 | 是 |

### 6.3 重试与续跑

1. 瞬时错误：同请求内按 `max_retries` 退避重试。  
2. 批次部分失败：调用方用同一 `batch_id` + `errors[].bug_id` 调 `citfix_batch_retry`。  
3. 与单 Bug 流水线：`/citfix {id} --resume` 仍走阶段状态机；本接口负责**跨多 Bug 的闭环写回批次**，不替代阶段 00–14。

## 7. 后续扩展点（契约已预留）

| 能力 | 扩展方式 |
|------|----------|
| 大量问题汇总 | `citfix_batch_stats` + `stats` 持久化到 `cit-workflow-test/00_runs/<batch_id>/batch_result.json` |
| 批量去重 | `options.dedupe`；扩展 `dedupe_key`（id / title hash） |
| 失败重试 | `citfix_batch_retry`；消费 `errors[]` |
| 结果统计 | `stats` + Excel/Markdown 报告（对齐 `export_excel`） |
| 按项目过滤 | `context.project` + plan_bank 校验 |
| 与流水线绑定 | `context.run_ids: { "97203": "CIT-20260901-97203-002" }` |

## 8. 落地状态

| 项 | 状态 |
|----|------|
| 契约 JSON | `workflow/citfix_batch_api.json` |
| 共享 stub | `tests/citfix/batch_api.py`（校验参数壳，返回 `NOT_IMPLEMENTED`） |
| MCP 壳 | `mcp/citfix_server.py` → 工具 `citfix_batch_*`；启动：`cit_mcp_launch.py citfix` |
| CLI 壳 | `scripts/citfix_batch.py`（resolve/query/retry/stats） |
| 业务实现 | **未做**（不写禅道、不改 batch 状态）；当前优先单 Bug `/citfix` |
