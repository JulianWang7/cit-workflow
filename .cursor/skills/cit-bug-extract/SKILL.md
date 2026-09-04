---
name: cit-bug-extract
description: |
  CIT 02_bug_task_extract：从 01 候选行抽出可串行跑的 bug id 队列。
  门禁：CIT 标题 + assignedTo=当前用户 + 产品别名 + verify_mode=auto。
  触发：cit-bug-extract、02_bug_task_extract、BUG_IDS_READY。
---

# cit-bug-extract — Bug/任务 ID 抽取（02）

## 目标

读 `01_req_parse/output/candidate_rows.json`，产出可调度队列：`bug_ids.json`（`marker=BUG_IDS_READY`）+ 兼容 `bug_task_ids.json`。

## 边界

- **主路径**：auto handler `_stage_02_bug_task_extract`
- `/citfix project`：02 完成后写 `batch_state.json`，再 **串行** 对每个 id 跑 `/citfix {id}`（子 run skip 01/02，`force_verify_auto`）
- **禁止**：调用 `zentao_get_bug` 拉详情（03）、批量写回禅道、并行多设备同批（本入口约定串行）

## 前置

- 01 已完成且 `marker=CANDIDATE_ROWS_READY`
- 优先读 `01_req_parse/intermediate/discover_result.json`（accepted/rejected）；缺失则按行再滤或重跑 discover

## 过滤门禁

| 门禁 | 规则 |
|------|------|
| assignedTo | 等于 `zentao_user`（当前配置用户） |
| 产品 | 命中 01 的 `product_id`/`product_name` |
| CIT 标题 | pipeline `cit_title_gate`（`CIT` / `【CIT】` / `[CIT问题]`） |
| verify_mode | **仅 `auto`**（project 强制；human/hybrid 进 rejected） |

## 工具流程

```text
读 candidate_rows.json (+ discover_result.json)
  → 应用门禁 → accepted / rejected
  → 写 bug_ids.json（bug_ids + accepted + rejected + gate）
  → 写 bug_task_ids.json（{"bug_ids":[...]} 兼容 03 输入形态）
  → project_batch：batch_state.json + 串行 run_pipeline
```

## 输出契约

| 路径 | 说明 |
|------|------|
| 正式 | `runs/<PRODUCT>/<batch_id>/02_bug_task_extract/output/bug_ids.json` |
| 中间 | `…/batches/<batch_id>/02_bug_task_extract/output/…` + `batch_state.json` |

| 字段 | 要求 |
|------|------|
| `marker` | `BUG_IDS_READY` |
| `bug_ids` | string[]，串行顺序 |
| `accepted` / `rejected` | 行级审计（rejected 含 `reject_reason`） |
| `force_verify_auto` | true |
| `serial` | true |

## 失败 / CHECKPOINT

| 情况 | 动作 |
|------|------|
| 缺 01 产物 | blocked；先跑 01 / `/citfix project <alias>` |
| `bug_ids` 为空 | 合法；batch_status=`empty`，不启动子 run |
| 子 run blocked | batch_status=`blocked`；`--resume` 从当前 index 续 |

## 与相邻

上：**cit-req-parse**；下：子 run 从 **03**（zentao_fetch）起；分析仍是 **cit-analyze**（07）。
