---
name: cit-req-parse
description: |
  CIT 01_req_parse：从禅道列表/产品别名解析候选任务行，产出 candidate_rows.json。
  通常由 /citfix project <alias> 的 auto handler 执行；亦可人工补洞。
  触发：cit-req-parse、01_req_parse、CANDIDATE_ROWS_READY。
---

# cit-req-parse — 需求/清单解析（01）

## 目标

把「项目别名 → 禅道产品 → 当前用户 my_bugs」冻结为可过滤候选行：`candidate_rows.json`（`marker=CANDIDATE_ROWS_READY`）。

## 边界

- **主路径**：引擎 auto handler `citfix/stages/req_extract.py::_stage_01_req_parse`
- **入口**：`/citfix project <alias>` → `citfix_batch_discover`（跑 00–02）；单 bug `/citfix {id}` **skip** 01/02
- MCP 提示：`zentao_my_bugs`、`zentao_list_products`、`zentao_search_bugs`
- **禁止**：拉单 bug 详情（那是 03）、分析改码（07+）、假装已过滤出最终队列（那是 02）

## 前置

- `~/.bugfix-flow/zentao.yaml` 可用；当前用户 = `assignedTo` 过滤基准
- 输入：`01_req_parse/input/query.json` 含 `project_alias`，或 `RunContext.project_alias`
- 产品别名可对 `plan_bank/<PRODUCT>/` 或禅道 `list_products` 模糊匹配

## 工具流程

```text
config_status（zentao ready）
  → zentao_list_products（或 plan_bank 产品名）
  → 别名打分 pick_best_product(alias)
  → zentao_my_bugs（当前用户）
  → 写出 candidate_rows.json（全量候选，含未过滤行）
  → intermediate/discover_result.json（accepted/rejected 预览，供 02）
```

引擎等价：`discover_project_cit_bugs` → `_stage_01_req_parse`。

## 输出契约

| 路径 | 说明 |
|------|------|
| 正式 | `runs/<PRODUCT>/<batch_id>/01_req_parse/output/candidate_rows.json` |
| 中间 | `runs_work/projects/<PRODUCT>/batches/<batch_id>/01_req_parse/output/…` |

JSON 必填：

| 字段 | 要求 |
|------|------|
| `marker` | `CANDIDATE_ROWS_READY` |
| `project_alias` | 入口别名 |
| `product_name` / `product_id` | 匹配产品 |
| `zentao_user` | 当前禅道用户 |
| `rows[]` | 候选行（含 `bug_id`/`title`/`assigned_to`/`cit_ok`/`verify_mode`） |

## 失败 / CHECKPOINT

| 情况 | 动作 |
|------|------|
| 无 project_alias | blocked；写 query.json 或 `/citfix project <alias>` |
| 产品匹配失败 | blocked；换更锐别名（如 `slb783`） |
| zentao 未配置 / my_bugs 失败 | blocked；检查 yaml 与 MCP |
| my_bugs 为空 | 允许空 `rows`；02 后 batch_status=empty |

## 与相邻

下：**cit-bug-extract**（02）；单 bug 入口跳过本 skill。
