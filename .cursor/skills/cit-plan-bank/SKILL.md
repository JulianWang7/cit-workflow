---
name: cit-plan-bank
description: | Persist cit-prepare snapshot into plan_bank/<PROJECT>/ with English-key JSON.
  Creates project_info.json and plan_<YYYYMMDD>/plans.json; upserts by zentao_id.
  触发词：cit-plan-bank、plan_bank、持久化计划库、写 plans.json、project_info。
---

# cit-plan-bank — 将上下文快照写入 plan_bank

## 目录约定（已确认）

```text
plan_bank/
  <PROJECT>/                    # 项目名，如 MT5825（不要用 plan_bank_MT5825）
    project_info.json
    plan_<YYYYMMDD>/
      plans.json
```

旧目录 `plan_bank_SLB783-A14` 等为历史中文模板，**新项目一律用项目名 + 英文字段**。

## 英文字段

### `plans.json`

```json
{
  "schema_version": "1.0",
  "problems": [
    {
      "description": "...",
      "zentao_id": "97203",
      "project_name": "MT5825",
      "status": "active",
      "analysis_conclusion_path": ""
    }
  ]
}
```

- prepare / 禅道拉取阶段：`analysis_conclusion_path` 固定写 `""`（保留键）。
- 分析阶段完成后再回写该字段为结论文件相对路径；**只写 `plans.json`**，不写回 `task_ref`/`context`。

### `project_info.json`

```json
{
  "schema_version": "1.0",
  "project_name": "MT5825",
  "code_root": "...",
  "cit_source_root": "...",
  "device_serial": "",
  "server": "110",
  "repo_branches": [
    {"tree": "code", "path": "...", "branch": "", "head": ""},
    {"tree": "cit", "path": "...", "branch": "", "head": ""}
  ]
}
```

- `repo_branches`：明确对象数组（`tree` / `path` / `branch` / `head`）。
- 无设备时 `device_serial` 为 `""`。
- 分支名/ HEAD 需 SSH 查询时，查完用 `--branches-json` 传入脚本覆盖。

## 何时调用

在 `cit-context-prepare` 写完  
`runs/<run_id>/06_context_snapshot/output/{task_ref,workspace,context}.json`  
且烟测通过后：

```powershell
python scripts/cit_plan_bank_write.py --run-dir runs/<run_id>
```

可选：

```powershell
python scripts/cit_plan_bank_write.py --run-dir runs/<run_id> --plan-date 20260901
python scripts/cit_plan_bank_write.py --run-dir runs/<run_id> --branches-json "[{\"tree\":\"code\",\"path\":\"/x\",\"branch\":\"b\",\"head\":\"abc\"}]"
```

## 与 runs 三份 JSON 的关系

| 文件 | 作用 | 写入 plan_bank？ |
|------|------|------------------|
| `task_ref.json` | 禅道归一化问题 | → `plans.json` 的一条 `problems[]` |
| `workspace.json` | server/code_root/CIT/serial | → `project_info.json` |
| `context.json` | 门禁与 run 元数据 | 提供 `run_id`/project；**不**整文件拷进 plan_bank |

## 实现位置

- 脚本：`scripts/cit_plan_bank_write.py`（创建目录、按 `zentao_id` upsert）
- 本 skill：编排何时调用、字段含义
- 编排衔接：见 `cit-context-prepare` 流程末步

## 完成后

告知用户写出的两个绝对/仓库相对路径，并提醒：`analysis_conclusion_path` 与 `repo_branches[].branch/head` 可能仍为空，属本阶段预期。
