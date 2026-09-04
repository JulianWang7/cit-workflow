# citfix Skill 厚度与阶段职责

> 现行权威：`contracts/citfix_pipeline.json` + 本仓 `.cursor/skills/cit*/`  
> 吸取：android-bugfix-flow 的「目标→工具流程→分支表→禁止项→输出契约」写法。

## 1. 01 / 02 是什么？

| Stage | 职责 | `/citfix {bug_id}` | `/citfix project <alias>` |
|-------|------|--------------------|---------------------------|
| **01_req_parse** | 禅道清单 → `candidate_rows.json` | **skip**（`citfix_direct`） | auto：`cit-req-parse` → `CANDIDATE_ROWS_READY` |
| **02_bug_task_extract** | 门禁过滤 → `bug_ids.json` 串行队列 | **skip** | auto：`cit-bug-extract` → `BUG_IDS_READY` |

入口模式：

| `entry_mode` | 行为 |
|--------------|------|
| `citfix_direct` | 已有 bug id → skip 01–02，从 03 起 |
| `citfix_batch_discover` | 仅跑 intake（00–02），写正式 `runs/<PRODUCT>/<batch_id>/` + 中间 `batches/<batch_id>/` |
| `citfix_project` | 子 bug run：仍 skip 01–02，强制 `verify_mode=auto` |

实现：`citfix/stages/req_extract.py`；编排：`citfix/project_batch.run_batch_intake`。

## 2. agent+校验：skill 必须厚

引擎只做 brief + JSON 门禁；执行靠 skill。厚度七件套：

1. 目标  
2. 边界（允许/禁止 MCP；禁 bugfix-*）  
3. 前置门禁  
4. 工具流程（有序 + 分支表）  
5. 输出契约（路径 + JSON + marker）  
6. 失败 / CHECKPOINT  
7. 与相邻 skill 分工  

字段名对齐 `stages._validate_agent_output`；01/02 auto 另检 skill `marker`。

## 3. Skill 清单（加厚状态）

| Skill | 阶段 | 厚度 |
|-------|------|------|
| **cit-req-parse** | 01 | 已加厚（auto primary） |
| **cit-bug-extract** | 02 | 已加厚（auto primary） |
| cit-context-prepare | 03–06（契约/补洞） | 已加厚 |
| cit-plan-bank | 06b | 已加厚 |
| cit-analyze | 07 | 已加厚 |
| cit-modify | 08 | 已加厚 |
| cit-review | 08 | 已加厚 |
| cit-compile | 09 | 已加厚 |
| cit-artifacts | 10 | 已加厚 |
| cit-flash | 11 | 已加厚 |
| cit-reproduce | 12 | 已加厚 |
| cit-verify | 12 | 已加厚 |
| cit-human-gate | 13 | 已加厚 |
| cit-submit | 14 | 已加厚（含 Worker） |
| citfix | 编排入口 | 入口文档 |

MCP 工具名放入 pipeline `mcp_hints`；`skills[]` 只挂 `cit-*`。

## 4. 批量接口预留了吗？

| 资产 | 状态 |
|------|------|
| `/citfix project` + 01/02 auto | **已实现**（discover + 串行子 run） |
| `docs/citfix-batch-api.md` | 契约说明 v0.1 |
| `contracts/citfix_batch_api.json` | schema，`status: stub` |
| `citfix/batch_api.py` | `stub_batch_*` 返回 `NOT_IMPLEMENTED` |
| `mcp/citfix_server.py` | `citfix_batch_resolve/query/retry/stats` 壳 |

批量**解决/写回**（`batch_resolve`）仍 stub；主路径是 project discover + 单 Bug 流水线。

## 5. 优化原则（持续）

1. 控制面加严、执行面写厚  
2. 能脚本化的进 Worker（closure/outbox）  
3. human 不假装 auto  
4. 吸 bugfix 流程与工具表，不吸单 bool 闭环  
