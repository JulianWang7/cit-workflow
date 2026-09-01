# citfix Agent 交互协议

Agent 每轮执行 `/citfix` 时必须遵守本协议。

## 1. 首轮 checklist

- [ ] 解析 `bug_id` 与 flags（`--resume` / `--status` / `--new`）
- [ ] 定位 `run_id` 与 `workflow_state.json`
- [ ] 若 `--status`：输出阶段表后 **结束**，不修改文件
- [ ] 读取当前阶段 KB 分册（`kb_doc_path` 或 `workflow-stages.md`）
- [ ] 读取测试床已有 output，避免重复拉禅道

## 2. 阶段内行为

### 自动段（03–06b）

Agent **可以**：

- 直接调 MCP：`zentao_get_bug`、`set_workspace`、`config_status`
- 手写/更新 `cit-workflow-test/<stage>/output/*.json`
- 在阶段完成后，**可选**运行一次状态同步：

  ```powershell
  cd D:\Workspace\cit-workflow
  .\.venv\Scripts\python.exe scripts\citfix.py <bug_id> --resume
  ```

Agent **不应**：

- 把「跑 citfix.py」作为对用户的首条回复
- 在未读 state 的情况下新建 run 覆盖旧进度

### Agent 段（07–14）

Agent **必须**：

1. Read 对应 EXP-CIT 分册
2. 加载对应 `bugfix-*` skill
3. 用 MCP 完成复现/分析/改码/验证（视阶段而定）
4. 将结构化结果写入 `required_outputs` 路径（见 `workflow/citfix_pipeline.json`）
5. 更新 `workflow_state.json` 中该 stage 为 `completed`

若缺设备/权限/信息 → **卡点停止**，不伪造 output。

## 3. Checkpoint Report（卡点报告）

对用户**必须**输出以下四段（标题固定）：

```markdown
## citfix 卡点

### 1. 卡在哪一步
- stage: `07_analysis`
- step: `search_code_tool / device_state`

### 2. 原因
（一句话根因，如：无 ADB 设备 / 禅道 token 过期）

### 3. 已完成上下文
- run_id: `CIT-20260901-97203-002`
- 已完成阶段: 00–06b
- 已有产物:
  - `cit-workflow-test/06_context_snapshot/output/context.json`
  - ...

### 4. 下一步要补什么
1. USB 连接设备并 `adb devices` 可见
2. 执行 `set_workspace(..., device_serial=...)`
3. 再发送 `/citfix 97203 --resume`
```

并写入/更新：

`cit-workflow-test/00_runs/<run_id>/CHECKPOINT.md`

## 4. 阶段完成报告

阶段通过时简短汇报：

```markdown
## citfix 阶段完成

- stage: `06_context_snapshot`
- outputs: （列表绝对路径）
- next: `07_analysis` — 将加载 EXP-CIT-006，需 bugfix-analyze
```

## 5. workflow_state 更新

优先字段：

```json
{
  "stages": {
    "07_analysis": {
      "status": "completed|blocked|running",
      "kb_doc_read": true,
      "outputs": { "primary": "..." },
      "blocker": { "step", "reason", "completed_context", "next_actions" }
    }
  },
  "current_stage": "07_analysis",
  "workflow_status": "blocked|running|completed"
}
```

可手改 JSON，或用 `citfix.py --resume` 同步自动段。

## 6. 禁止行为

- 跳过 blocked 阶段继续下一阶段
- 每次 `/citfix` 都从零拉禅道并覆盖 `tasks.json`（除非 `--new`）
- 把调试日志写到 `D:\Workspace` 根目录
- 在 `cit-workflow-test` 根下新建未在 pipeline 中定义的目录（除非用户批准）
