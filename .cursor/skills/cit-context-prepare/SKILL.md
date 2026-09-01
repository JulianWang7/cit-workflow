---
name: cit-context-prepare
description: | CIT 自动化「环境准备与上下文快照」阶段技能。
  从禅道拉 Bug、绑定 set_workspace、按 cit_guide 定位 MeiGLink、写入 runs/*/06_context_snapshot。
  触发词：cit-prepare、上下文快照、环境准备、CIT 任务接入、cit-context-prepare、context snapshot。
---

# cit-context-prepare — 环境准备与上下文快照

## 目标

把会变化的外部世界冻结成可重复消费的输入包（对齐 EXP-CIT-005），输出：

| 产物 | 路径 |
|------|------|
| workspace 绑定 | `runs/<run_id>/06_context_snapshot/output/workspace.json` |
| 上下文门禁 | `runs/<run_id>/06_context_snapshot/output/context.json` |
| 任务摘要 | `runs/<run_id>/06_context_snapshot/output/task_ref.json` |

本 skill **止步于** `gates.ready_for_analyze`；不进入改码/编译。

## 前置

- 已按 `cit-setup-guide` 配好环境变量与 `~/.bugfix-flow`
- 本仓库 `.cursor/mcp.json` 经 `scripts/cit_mcp_launch.py` 启动仓内 `mcp/` + `bugflow/`
- 详读同目录 `cit_guide.md`

## 工具链（本阶段）

| MCP | 工具 | 用途 |
|-----|------|------|
| zentao-mcp | `zentao_get_bug` / `zentao_download_attachment` | 任务真源与附件本地化 |
| ssh-mcp | `set_workspace` / `run_command` | 绑定 server+code_root；校验路径 |
| misc-mcp | `config_status` / `config_save` | 读配置；写 meiglink |
| adb-mcp | `device_state` / `adb_shell` | 可选：CIT 版本 label 判别 |

## 标准流程

```text
config_status
  → zentao_get_bug(bug_id)
  → zentao_download_attachment（如有）
  → set_workspace(server, code_root[, device_serial])
  → cit_guide：识别 CIT → 解析 meiglink/cit3|cit4
  → 写 workspace.json / task_ref.json / context.json
  → python scripts/cit_smoke_context_prepare.py --run-dir runs/<run_id>
  → （plan_bank）加载 skill cit-plan-bank，或执行：
       python scripts/cit_plan_bank_write.py --run-dir runs/<run_id>
```

plan_bank 目录为 `plan_bank/<PROJECT>/`（如 `MT5825`），英文字段；详见 `cit-plan-bank` skill。

## CIT 门禁（当前口径）

标题或内容含 `cit`（大小写不敏感）或 `【CIT】` → `cit_hint.title_has_cit_keyword=true`。
不满足则 `gates.ready_for_analyze=false`，并在 `validation.errors` 写明原因。

## context.json 最低字段

见 `schemas/cit_context_snapshot.schema.json` 与 `docs/cit-env-and-context.md`。

关键门禁：

- 有 `bug_id` 与归一化 title
- 附件若声明存在，则应有 `local_path`（本机路径）或明确 `missing`
- `workspace.server` + `workspace.code_root` 非空
- CIT 问题应尽量填 `cit.source_path`（MeiGLink 下 cit3.0/cit4）

## 与 Bugfix 的差异

| 项 | android-bugfix-flow | cit-workflow（本阶段） |
|----|---------------------|------------------------|
| 入口 | `/bugfix-prepare` | `cit-prepare` / 本 skill |
| 源码焦点 | `code_root` 系统树 | MeiGLink CIT 树 + 系统树仅作辅助 |
| 产物真源 | 会话 prompt / `~/.bugfix-flow` | 仓库内 `runs/.../context.json` |
| MCP 实现 | 内嵌于 bugflow 仓 | **已拷贝** `mcp/` + `bugflow/` 到本仓并注册 mcp.json |

## 完成后的用户提示

告知：快照已写入 `runs/<run_id>/...`；若已跑 `cit_plan_bank_write.py`，一并给出 `plan_bank/<PROJECT>/` 路径。下一阶段（分析）需另开 skill；分析结论路径稍后只回写 `plans.json` 的 `analysis_conclusion_path`。
