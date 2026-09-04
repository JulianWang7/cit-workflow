---
name: cit-review
description: |
  cit-workflow 08 审查：只读审查 patch，将 change JSON 的 review_status 写为 pass|fail。
  触发：cit-review、REVIEW_PASS。禁止 bugfix-review。禁止改业务源码。
---

# cit-review — CIT 改码审查（项目 skill）

> 对标 bugfix-reviewer：正确性 / 完整性 / 副作用；另加 citfix 审计字段校验。

## 目标

独立审查 08 patch；只有 `review_status=pass` 引擎才放行 09。

## 边界

- MCP：ssh **只读**（`read_file`、`git_op(diff|status)`、`run_command` 只读）
- **禁止** `edit_file` / `write_file` 改业务源码
- **仅允许**更新正式 change JSON 的审查字段
- 路径：`…/08_changes/output/change_<bug_id>.json`

## 前置

1. change JSON 已存在且含 `files_changed`、`patch_summary`  
2. 可取得当前 `git diff` 与 `source_base_sha`/`patch_sha256`

## 审查清单（必须逐项过）

| 项 | 通过标准 |
|----|----------|
| 正确性 | 对准 `root_cause` / `recommended_fix` |
| 完整性 | CIT 相关配置/模块改全，无漏第二处阈值等 |
| 副作用 | 未误伤其它测项/全局默认 |
| 风格 | 修改标记配对；无大段无故删除 |
| 审计 | `source_base_sha` 非空；`patch_sha256` 与当前 diff 正文 sha256 **一致** |

## 工具流程

```text
1. Read root_cause + change JSON
2. git_op(diff) → 计算 sha256，对比 patch_sha256
3. read_file 抽查关键 hunk
4. 写回 review_status=pass|fail；可加 review_notes
5. fail → CHECKPOINT；pass → /citfix --resume
```

## 输出

在原 change JSON 上更新：

```json
{
  "review_status": "pass",
  "review_notes": "…",
  "reviewed_at": "ISO8601"
}
```

`fail` 时 `review_notes` **必填**原因；引擎对非 `pass` → blocked。

## 禁止

- 未看 diff 直接 pass  
- 为过门禁改业务代码「补洞」——应退回 cit-modify  
- 使用用户级 bugfix-reviewer 技能名混淆

## 与相邻 skill

上：**cit-modify**；下：**cit-compile**。
