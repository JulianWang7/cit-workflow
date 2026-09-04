---
name: cit-plan-bank
description: |
  将 06 快照写入 plan_bank/<PROJECT>/（project_info + problems/<id>.json + index.json）。
  触发：cit-plan-bank、06b、plan_bank。优先读正式 runs/ 快照。
  Agent 先读 index.json，再按 path 打开单个 problem，勿整目录灌入上下文。
---

# cit-plan-bank — 计划库持久化

## 目标

把可复用的项目绑定与问题条目落入 `plan_bank/`，供后续 run 复用 `device_serial`/`server`/`code_root`。

## 边界

- 实现：`scripts/cit_plan_bank_write.py`（`/citfix` 06b auto 调用）
- 输入优先：`cit-workflow/runs/<run_id>/06_context_snapshot/output/`
- **不**替代正式 runs 证据链

## 目录

```text
plan_bank/<PROJECT>/
  project_info.json           # server, code_root, device_serial, cit_* …
  index.json                  # 薄目录：zentao_id / status / description[:120] / path
  problems/<zentao_id>.json   # 单问题全文
  history/plan_<YYYYMMDD>.json  # 按日归档（可选审计）
```

## Agent 读取顺序（省 token）

1. `project_info.json`（环境，按需）
2. `index.json`（定位 zentao_id）
3. 仅打开 `problems/<id>.json`

## 工具流程

```powershell
python scripts/cit_plan_bank_write.py --run-dir D:\Workspace\cit-workflow\runs\<run_id>
# 旧 layout 一次性迁移：
python scripts/cit_plan_bank_write.py --migrate-legacy
```

`analysis_conclusion_path` 回写指向正式：  
`runs/<run_id>/07_analysis/output/root_cause_<id>.json`

## 字段映射

| 快照 | plan_bank |
|------|-----------|
| `workspace.json` | → `project_info.json` |
| `task_ref.json` | → `problems/<bug_id>.json` + `index.json` |
| `context.json` | 提供 run_id/project；不整文件拷贝 |

## 失败 / CHECKPOINT

| 情况 | 动作 |
|------|------|
| 缺 06 三件套 | 06b_input blocked |
| PRODUCT 名非法路径字符 | sanitize 后写入；保留可读中文空格名 |

## 与相邻

上：**cit-context-prepare** / 06；下：07+ 只读 `project_info` + 按需 `problems/<id>`。
