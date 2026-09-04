# citfix Watch（APScheduler 监督者）

> 跨阶段监督，**不是** Cursor Agent 内置面板。权威设计：EXP-CIT-010。

## 1. 定位

| 角色 | 谁在跑 | 做什么 |
|------|--------|--------|
| Agent / `cursor-agent` | 你手动或 CLI 启动 | 执行 `/citfix` 阶段（分析/改码/验证） |
| Watch 监督者 | **另开进程** `cit_watch_run.py` | 定时读 `workflow_state.json`，僵死/阻塞时 nudge 或 escalate |

**默认：`/citfix 81097` 不会自动启动 APScheduler。** 81097 那次只有 Agent 在跑是预期行为。

## 2. 调度

- 入口：`scripts/cit_watch_run.py`（常驻）或 `cit_watch_once.py`（单次）
- 后端：优先 APScheduler；未安装则 threading 回退（`requirements-watch.txt`）
- 周期（`config/citfix/watch.yaml`）：
  - `scan_runs_seconds`（默认 60s）扫 active runs
  - `scan_batches_seconds`（120s）扫 project batches
  - `scan_retry_seconds`（30s）处理到期重试

## 3. 监控指标 / 决策

读中间态 `runs_work/.../workflow_state.json` 的 `workflow_status` / `current_stage` / `updated_at`，结合 `watch/watch_state.json`：

| 决策 | 条件（摘要） |
|------|----------------|
| `ok` | 正常推进或已 completed |
| `nudge` | 僵死超时、blocked 过久等 → 准备 `cursor-agent … /citfix <id> --resume` |
| `retry_wait` | 退避窗口未到 |
| `escalate` | nudge 次数用尽 / 墙钟超时 → 写 notifications |
| `skip` | claim-only 等 |

默认 **`nudge_dry_run: true`**：只记日志/事件，**不真正拉起** CLI。

## 4. 状态落盘（可观察）

每个 run 中间目录：

```text
runs_work/projects/<PRODUCT>/runs/<run_id>/watch/
  watch_state.json    # nudge_count / watch_status / next_retry_at
  events.jsonl        # 监督动作流水
  snapshots/          # nudge 前 workflow_state 快照
```

Escalate 还可写到正式 `14_closure/output/notifications/`（若配置开启）。

## 5. 如何启用与观察

```powershell
cd D:\Workspace\cit-workflow
pip install -r requirements-watch.txt
# 单次扫描（推荐先看）
.\.venv\Scripts\python.exe scripts\cit_watch_once.py --json
# 常驻（另开终端，Ctrl+C 结束）
.\.venv\Scripts\python.exe scripts\cit_watch_run.py --once-first
```

真 nudge：把 `config/citfix/watch.yaml` 里 `nudge_dry_run` 改为 `false`。

**当前没有 Web/可视化监控 UI**；观察方式 = 终端输出 + 上述 JSON/jsonl。可视化属后续扩展，非现网能力。
