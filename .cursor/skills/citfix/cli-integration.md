# citfix × Cursor CLI 集成

`/citfix` 是 **cit-workflow 自动化流水线的固定 CLI/IDE 入口**（非通用调试工具）。  
依据 KB-002：长任务用 Cursor CLI + `--resume` 线程；工作区为 `cit-workflow`。

## 1. 工作区要求

```text
D:\Workspace\cit-workflow\     ← --workspace 指向此目录
  .cursor/skills/citfix/       ← Skill 自动发现
  .cursor/mcp.json             ← MCP 注册
  contracts/citfix_pipeline.json
```

测试床（只读/写产物，非 workspace 也行，但 Agent 需能 Read）：

```text
D:\Workspace\cit-workflow\runs_work\
```

## 2. 启动命令示例

### 新调试会话

```powershell
cd D:\Workspace\cit-workflow

cursor-agent --workspace . "/citfix 97203 --resume"
```

等价自然语言 prompt：

```text
Load the citfix skill from this workspace. Execute /citfix 97203 --resume.
Follow agent-protocol.md. Stop at checkpoint with full report.
```

### 恢复已有 CLI 线程

```powershell
# 列出历史线程（以本机 cursor-agent 实际子命令为准）
cursor-agent ls

# 恢复
cursor-agent --workspace D:\Workspace\cit-workflow --resume <thread-id> ^
  "Continue citfix for bug 97203. Read workflow_state.json first."
```

> **说明**：本机正确入口为 **`cursor-agent`**（不是 `cursor agent`）。核心不变：**workspace = cit-workflow**，**首句携带 /citfix**。

### 2.1 MCP 未加载时（「SSH MCP isn't loaded」）

Agent 若报 MCP 未加载并改口「via bugflow SSH」——**视为卡点，不是可接受降级**（关闭路径除外，见协议 §0.1）。

本机修复顺序：

```powershell
cd D:\Workspace\cit-workflow
.\.venv\Scripts\python.exe scripts\cit_smoke_mcp.py --render
# 完全退出 Cursor，或新开 CLI 会话（旧会话常不重载 MCP）
cursor-agent --workspace D:\Workspace\cit-workflow "/citfix <bug_id> --resume"
```

确认会话内可调用 `set_workspace` / `search_code_tool`。详见 `docs/cit-mcp-reference.md` §8。

## 3. 上下文传递

| 信息 | 传递方式 |
|------|----------|
| bug_id | 用户首句 `/citfix {id}` |
| 续跑意图 | `--resume` flag |
| 历史进度 | **文件** `workflow_state.json`（非 CLI 参数） |
| 卡点 | **文件** `CHECKPOINT.md` |
| 阶段文档 | Agent Read `kb_doc_path` |
| MCP 凭据 | 环境变量 `BUGFIX_CONFIG_DIR` |

**不要**依赖 CLI 会话内存作为唯一真源；文件 state 为准。

## 4. 与调度器配合（已落地）

监督者代码：`citfix/watch/` + `scripts/cit_watch_once.py` / `cit_watch_run.py`（见 EXP-CIT-010）。

1. 定时读 `workflow_status` / `updated_at` / `stages.*.status`
2. 僵死或 blocked 过久 → 快照 + 注入 `/citfix {bug_id} --resume`（默认 dry-run）
3. `completed` / claim-only / `CIT-BATCH-*` 不督促
4. escalate → `14_closure/.../notifications/watch_escalate_*.json`

配置：`config/citfix/watch.yaml`（从 `watch.yaml.example` 复制）。Skill 仍不实现调度；调度器只发 **/citfix** 口令。

## 5. 退出码（仅当 Agent 调用 citfix.py 时）

| 码 | 含义 | Agent 行为 |
|----|------|------------|
| 0 | 自动段同步完成或全流程 completed | 继续 agent 段或汇报完成 |
| 2 | 引擎层 blocked | 读 CHECKPOINT，按 Skill 卡点报告 |
| 1 | 配置/参数错误 | 引导 cit-setup |

Agent 在 IDE 内**不必**向用户展示退出码，应翻译为卡点四要素。

## 6. 示例：无人值守一轮

```powershell
cursor-agent --workspace D:\Workspace\cit-workflow --force ^
  "citfix skill: /citfix 97203 --resume. Max one stage per turn. If blocked, write CHECKPOINT.md and stop."
```

适合定时调度触发（APScheduler 等）。
