---
name: cit-context-prepare
description: |
  CIT 03–06 环境准备与上下文快照（可被 citfix auto 段调用或人工补洞）。
  正式产物写 runs/<run_id>/06_context_snapshot/；触发：cit-prepare、cit-context-prepare。
---

# cit-context-prepare — 环境准备与上下文快照

## 目标

把外部世界冻结成可重复输入包：`workspace` / `context` / `task_ref` / 附件，并产出 `gates.ready_for_*`。

## 边界

- 在 `/citfix` 中 **03–06b 通常由引擎 auto handler 执行**；本 skill 用于：补洞、手工重跑、理解契约
- MCP：zentao / ssh / misc / 可选 adb
- **止步于** analyze 门禁；不改码、不编译
- 正式：`cit-workflow/runs/<run_id>/`；中间：`runs_work/projects/<PRODUCT>/runs/<run_id>/`

## 前置

- `~/.bugfix-flow` 与 `BUGFIX_CONFIG_DIR` 已配  
- `.cursor/mcp.json` → `cit_mcp_launch.py`  
- 详读同目录 `cit_guide.md`

## 工具流程

```text
config_status
  → zentao_get_bug(bug_id)
  → 下载附件到 06/.../input/attachments/ + sha256
  → set_workspace(server, code_root[, device_serial])
  → cit_guide：识别 cit3/cit4 + meiglink
  → 写 workspace.json / task_ref.json / context.json
     （含 routing.verify_mode=auto|human|hybrid，来自 registry）
  → cit_smoke_context_prepare.py --run-dir <formal>
  → cit_plan_bank_write.py --run-dir <formal>   # 或阶段 06b
```

引擎等价路径：`scripts/citfix.py` auto 段 `_stage_03`…`_stage_06b`。

## 门禁字段

| 字段 | 含义 |
|------|------|
| `gates.ready_for_analyze` | server+code_root+CIT 标题；附件 missing 则 false（除非 allow_missing） |
| `gates.ready_for_reproduce` | analyze 条件 + `device_serial` |
| `routing.verify_mode` | `auto\|human\|hybrid`（**禁止**再写死 `cit`） |

CIT 标题：含 `CIT` / `【CIT】` / `[CIT问题]`（见 pipeline `cit_title_gate`）。

## 输出契约

见 `docs/cit-env-and-context.md`（字段门禁由 smoke / 引擎校验）。

## 失败 / CHECKPOINT

| 情况 | 动作 |
|------|------|
| 非 CIT 标题 | 04 blocked |
| 无 project_info | 06 workspace_bind blocked |
| 附件下载失败 | ready_for_analyze=false |

## 与相邻

下：07 **cit-analyze**；持久化：**cit-plan-bank**。
