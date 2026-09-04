---
name: cit-modify
description: |
  cit-workflow 08_changes：按 root_cause 最小改码，写 change_<bug_id>.json（pending）。
  触发：/citfix 08、cit-modify、PATCH_DONE。禁止 bugfix-modify。完成后必须 cit-review。
---

# cit-modify — CIT 改码（项目 skill）

## 目标

在项目 `code_root` 做最小可审改动，产出带审计字段的 change JSON；**不** push。

## 边界

- MCP：ssh `read_file` / `edit_file` / `write_file` / `git_op(status|diff)` / `run_command`
- **禁止** `bugfix-modify`；**禁止**本阶段 `git push` / 禅道写回
- 输入：`07/.../root_cause_*.json` + `06/workspace.json`
- **正式产物**：`…/08_changes/output/change_<bug_id>.json`
- 改码树：当次项目树（如 SLB783），**禁止**改 MeiGLink cit3/cit4 只读仓（除非 project_info 显式允许）

## 前置

1. 07 `marker=ROOT_CAUSE_FOUND`  
2. `set_workspace` 已绑定  
3. 工作树尽量干净或 diff 范围可控（无关脏文件先沟通）

## 工具流程

```text
1. Read root_cause.recommended_fix + evidence_paths
2. git rev-parse HEAD → source_base_sha
3. 最小 edit_file（公司 start/end 修改标记；旧代码注释保留）
4. git_op(diff) → 对 unified diff 正文做 sha256 → patch_sha256
5. 写 change JSON（review_status=pending, marker=PATCH_DONE）
6. 立即执行 cit-review（同阶段强制）
```

## 改码约束

| 允许 | 禁止 |
|------|------|
| vendor CIT 配置 xml/prop | 大范围无关重构 |
| 单模块明确 bugfix | 无标记删除大段历史 |
| 与 root_cause 一一对应 | 顺手改格式化全家桶 |

## 输出契约（引擎在 review 后强制）

```json
{
  "schema_version": "1.0",
  "bug_id": "<id>",
  "run_id": "<run_id>",
  "files_changed": ["至少一路径"],
  "patch_summary": "...",
  "source_base_sha": "40-char",
  "patch_sha256": "hex",
  "review_status": "pending",
  "committed": false,
  "pushed": false,
  "server": "110",
  "code_root": "/home2/.../",
  "branch_local": "...",
  "branch_upstream": "origin/...",
  "marker": "PATCH_DONE"
}
```

引擎完成 08 时还要求最终 **`review_status=pass`**（由 cit-review 写回）。

## 失败 / CHECKPOINT

| 情况 | 动作 |
|------|------|
| 无法取得 source_base_sha | 停 |
| diff 空 | 停；检查是否改错树 |
| 需改只读 MeiGLink | CHECKPOINT；改项目 vendor 副本策略 |

## 与相邻 skill

上：**cit-analyze**；下：**cit-review**（同 08）→ **cit-compile**。
