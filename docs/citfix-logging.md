# CitFix 运行日志（正式产物）

> 权威契约：`contracts/citfix_run_events.schema.json`  
> 实施计划：`docs/citfix-logging-implementation-plan.md`

## 目录

```text
runs/<PRODUCT>/<run_id>/
├── <NN_stage>/output/              # 交付真源（禁止写运行日志）
├── logs/
│   ├── run_events.jsonl            # 结构化时间线（主）
│   └── stages/
│       └── <run_id>_<stage>.log    # 阶段明细文本
├── watch/                          # 监督旁路（规划迁入正式树）
├── workflow_state.json             # 续跑状态真源
└── …
```

批次：`runs/<PRODUCT>/<batch_id>/logs/run_events.jsonl`（`/citfix project` 发现与串行推进：`batch_start` / `batch_ready` / `batch_item_*` / `batch_completed` 等）。

持久日志**不以** `runs_work/` 为唯一真源（过渡期可双写，正式侧为准）。

## 事件字段（最小集）

| 字段 | 说明 |
|------|------|
| `ts` | ISO8601 |
| `run_id` / `bug_id` / `stage_id` | 关联键 |
| `actor` | `engine` \| `agent` \| `watch` \| `mcp` \| `cli` |
| `event` | 如 `run_start`、`stage_start`、`stage_end`、`stage_blocked`、`gate_fail` |
| `level` | `info` \| `warn` \| `error` |
| `summary` | 一行摘要（已脱敏） |
| `refs` | 可选路径、exit_code、validation_errors 等 |

## 写入 API

```python
from citfix.run_log import append_run_event, emit_event

append_run_event(formal_run_dir, event_dict)  # 失败默认不抛，不阻断流水线
emit_event(formal_run_dir, actor="engine", event="stage_start", ...)
```

Agent CLI：

```powershell
.\.venv\Scripts\python.exe scripts\cit_run_event.py --run-dir "runs/<PRODUCT>/<run_id>" `
  --bug-id <id> --stage 07_analysis --event agent_checkpoint --summary "..."
```

Watch 监督状态优先写 `runs/<PRODUCT>/<run_id>/watch/`，并把 nudge/escalate 镜像进 `run_events.jsonl`（`actor=watch`）。

## 脱敏

禁止入日志：token、password、Authorization、api_key、SSH 口令明文等。  
`citfix.run_log.redact_text` / `redact_event` 为默认过滤器。

## 切流（LOG-12，2026-09-07）

- 阶段明细与 `run_events` **只写**正式 `runs/<PRODUCT>/<run_id>/logs/`（不再写入 `runs_work/.../<stage>/logs`）。
- Watch 默认 **`scan_intermediate_runs: false`**：只扫正式 `runs/` 的 `workflow_state` + `watch/`。
- 历史中间日志可选迁移：`python scripts/cit_migrate_logs.py --dry-run`。
- `runs_work` 仍可保留 brief/checkpoint/镜像；**删除整个 runs_work** 不在本步骤范围。

| 落点 | 职责 |
|------|------|
| `workflow_state.json` | 续跑真源 |
| `logs/run_events.jsonl` | 运行轨迹 |
| `watch/` | 监督动作 |
| `14_closure/.../outbox_events.jsonl` | 通知投递（≠ run_events） |
| `*/output/` | 门禁交付产物 |
