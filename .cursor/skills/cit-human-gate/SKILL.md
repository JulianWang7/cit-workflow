---
name: cit-human-gate
description: |
  cit-workflow 13_human_gate：按 verify_mode 写 human_gate_<bug_id>.json。
  触发：/citfix 13、human_gate、pending_human、approved、auto_bypassed。
---

# cit-human-gate — 人工门禁（项目 skill）

## 边界

- **无**独立 MCP「签字」工具；产物是结构化 JSON，由人填或临时探测批准
- 读：`06/context.routing.verify_mode`、`04/tasks.verification`、`12/result_*.json`
- **正式产物**：`…/13_human_gate/output/human_gate_<bug_id>.json`
- 门禁：`docs/citfix-stage-gates.md`

## 完成条件（引擎强制）

| verify_mode | 允许的 gate_status |
|-------------|-------------------|
| `auto` | `not_required` \| `auto_bypassed` |
| `human` / `hybrid` | 仅 `approved`（须 `approved_by`）才 completed |
| 任意 | `pending_human` → **blocked**（正确等待态） |

## 流程

### A. auto

若 12 已 `VERIFY_PASS` 且无 human_assertions 未决：

```json
{
  "schema_version": "1.0",
  "bug_id": "<id>",
  "run_id": "<run_id>",
  "verification_mode": "auto",
  "gate_status": "auto_bypassed",
  "reason": "no human cases; auto_assertions all passed",
  "verify_pass_ref": ".../12_cit_test/output/result_<id>.json",
  "approved_by": "engine-policy",
  "approved_at": "ISO8601"
}
```

### B. human / hybrid — 等待人测

1. 从 `verification.human_cases` / registry 列出步骤
2. 写 `pending_human`（**故意**让引擎 blocked）
3. CHECKPOINT 写清：谁测、测什么、改哪些字段后续 resume

```json
{
  "gate_status": "pending_human",
  "verification_mode": "human",
  "human_cases_required": [{"case_id": "...", "steps": []}],
  "approved_by": null,
  "pending_actions": ["..."]
}
```

### C. 人测通过后（或临时探测）

人工/操作者改同一文件：

```json
{
  "gate_status": "approved",
  "approved_by": "<name-or-temp-bypass-id>",
  "approved_at": "ISO8601",
  "result": "PASS",
  "comment": "..."
}
```

再 `/citfix <id> --resume`。

失败：`gate_status=rejected` → 不进 14，回分析/改码。

## 禁止

- human 模式下写 `auto_bypassed` 糊弄过关
- `approved` 不填 `approved_by`
- 自由文本聊天当作门禁通过（必须改 JSON）
