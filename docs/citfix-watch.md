# citfix Watch（APScheduler 监督者）

> 跨阶段监督，**不是** Cursor Agent 内置面板。权威设计：EXP-CIT-010。  
> 日志约定：`docs/citfix-logging.md`（正式 `logs/` + `watch/`）。

## 1. 定位

| 角色 | 谁在跑 | 做什么 |
|------|--------|--------|
| Agent / `cursor-agent` | 你手动或 CLI 启动 | 执行 `/citfix` 阶段（分析/改码/验证） |
| Watch 监督者 | **另开进程** `cit_watch_run.py` | 定时读 `workflow_state.json`，僵死/阻塞时 nudge 或 escalate |

**默认：`/citfix 81097` 不会自动启动 APScheduler。**

## 2. 调度

- 入口：`scripts/cit_watch_run.py`（常驻）或 `cit_watch_once.py`（单次）
- 后端：优先 APScheduler；未安装则 threading 回退（`requirements-watch.txt`）
- 周期（`config/citfix/watch.yaml`）：
  - `scan_runs_seconds`（默认 60s）扫 active runs（默认**仅正式** `runs/`；`scan_intermediate_runs: false`）
  - `scan_batches_seconds`（120s）扫 project batches（仍在 `runs_work/.../batches`，批次状态尚未全部正式化）
  - `scan_retry_seconds`（30s）处理到期重试

## 3. 监控指标 / 决策

读 `workflow_status` / `current_stage` / `updated_at`，结合 `watch/watch_state.json`：

| 决策 | 条件（摘要） |
|------|----------------|
| `ok` | 正常推进或已 completed |
| `nudge` | 僵死超时、blocked 过久、或（可选）run_events 心跳超时 |
| `retry_wait` | 退避窗口未到 |
| `escalate` | nudge 次数用尽 / 墙钟超时 → 写 notifications |
| `skip` | claim-only 等 |

默认 **`nudge_dry_run: true`**：只记日志/事件，**不真正拉起** CLI。

可选 **`event_heartbeat_enabled`**（默认 `false`）：当 `status=running` 且 `logs/run_events.jsonl` 超过 `event_heartbeat_timeout_seconds` 无新行 → `EVENT_HEARTBEAT` nudge。务必先确认引擎/Agent 在写事件后再打开。

## 4. 状态落盘（正式优先）

```text
runs/<PRODUCT>/<run_id>/watch/
  watch_state.json
  events.jsonl              # 监督动作；同时镜像 actor=watch 到 logs/run_events.jsonl
  snapshots/
runs/<PRODUCT>/<run_id>/logs/run_events.jsonl
```

过渡期：若正式 `watch/` 尚无状态，可读中间 `runs_work/.../watch/`；**写入优先正式树**。  
LOG-12 后默认不再扫描 `runs_work/.../runs` 上的 active run（`scan_intermediate_runs: false`）。遗留日志可用 `scripts/cit_migrate_logs.py`。

## 5. 如何启用与观察

```powershell
cd D:\Workspace\cit-workflow
pip install -r requirements-watch.txt
.\.venv\Scripts\python.exe scripts\cit_watch_once.py --json
.\.venv\Scripts\python.exe scripts\cit_watch_run.py --once-first
```

真 nudge：`config/citfix/watch.yaml` 里 `nudge_dry_run: false`。

**当前没有 Web/可视化监控 UI**；观察方式 = 终端输出 + 上述 JSON/jsonl + `--debug` 最近 run_events。
