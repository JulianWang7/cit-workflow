---
name: cit-analyze
description: |
  cit-workflow 07_analysis：在冻结 context 上定位 CIT 根因，写 root_cause_<bug_id>.json。
  触发：/citfix 07、cit-analyze、ROOT_CAUSE_FOUND。禁止 bugfix-analyze。
---

# cit-analyze — CIT 根因分析（项目 skill）

> 吸取 bugfix-analyze：证据链、模块定位、禁止空谈。  
> **差异**：焦点是 CIT 测项/配置/模块类，不是泛 AOSP crash。

## 目标

给出可执行的根因与推荐修复，写入正式 `root_cause_*.json`，供 08 改码。

## 边界

- MCP：仓内 ssh（`set_workspace`、`search_code_tool`、`locate_files_tool`、`read_file`、`run_command`）；zentao 只读复核
- **禁止** `bugfix-analyze` / 用户级 skill
- 输入：`06/.../{context,task_ref,workspace}.json`；附件 `06/.../input/attachments/`
- 门禁：`context.gates.ready_for_analyze==true`，否则 CHECKPOINT 回 citfix
- **正式产物**：`…/07_analysis/output/root_cause_<bug_id>.json`
- 中间：`runs_work/projects/<PRODUCT>/runs/<run_id>/07_analysis/`

## 前置

1. 06 已完成；`server`/`code_root` 非空  
2. CIT 标题门禁已过（04）  
3. **本会话已加载 `ssh-mcp`**，可调用 `set_workspace`（见 citfix `agent-protocol.md` §0.1）  
4. `set_workspace(server, code_root[, device_serial])`

若 MCP 未加载：写 CHECKPOINT `mcp_not_loaded` 并停止；**禁止**改用「bugflow SSH」 silently 搜码后仍标 `ROOT_CAUSE_FOUND` 而不说明。

## 工具流程

```text
1. Read context / task_ref / workspace / 附件
2. 从标题+steps 抽出：CIT 菜单名、期望/实际、配置关键字、模块名
3. search_code_tool / locate_files_tool 在 code_root + cit_source_path
4. read_file 锁定配置键 / Test 类 / overlay
5. 写 root_cause JSON（marker=ROOT_CAUSE_FOUND）
6. /citfix <id> --resume
```

## 定位分支表

| 症状类型 | 优先搜索 | 典型路径 |
|----------|----------|----------|
| 阈值/配置错误 | xml/prop 键名 | `vendor/meig/apps/cit/etc/`、`*cit*config*` |
| 某 Function Test 项 FAIL | 模块类名 / 菜单字符串 | MeiGLink `cit3.0`/`cit4` `modules/**` |
| 版本/指纹显示 | getprop / 定制字符串 | `*.mk`、overlay、Settings |
| 传感器/HAL | sensor type、HIDL/AIDL | vendor HAL + CIT probe |

**禁止**：未读代码只复述禅道原文当 root_cause。

## 输出契约（引擎强制）

```json
{
  "schema_version": "1.0",
  "bug_id": "<id>",
  "run_id": "<run_id>",
  "root_cause": "非空",
  "evidence_paths": ["至少一条绝对或仓库相对路径"],
  "recommended_fix": "非空可执行建议",
  "trigger_path": "可选：触发链路简述",
  "confidence": "high|medium|low",
  "analyzed_at": "ISO8601",
  "marker": "ROOT_CAUSE_FOUND"
}
```

## 失败 / CHECKPOINT

| 情况 | 动作 |
|------|------|
| `ready_for_analyze=false` | 停；修 06/附件/workspace |
| SSH 搜不到代码 | 记 errors；勿假 ROOT_CAUSE |
| 根因在硬件/夹具 | 写清，`recommended_fix` 标明需 human，供 04/13 分类 |

## 与相邻 skill

下一阶段 **cit-modify**；本阶段 **不改码、不编译**。
