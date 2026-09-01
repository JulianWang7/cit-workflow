# citfix × Cursor CLI 集成

`/citfix` 是 **cit-workflow 自动化流水线的固定 CLI/IDE 入口**（非通用调试工具）。  
依据 KB-002：长任务用 Cursor CLI + `--resume` 线程；工作区为 `cit-workflow`。

## 1. 工作区要求

```text
D:\Workspace\cit-workflow\     ← --workspace 指向此目录
  .cursor/skills/citfix/       ← Skill 自动发现
  .cursor/mcp.json             ← MCP 注册
  workflow/citfix_pipeline.json
```

测试床（只读/写产物，非 workspace 也行，但 Agent 需能 Read）：

```text
D:\Workspace\cit-workflow-test\
```

## 2. 启动命令示例

### 新调试会话

```powershell
cd D:\Workspace\cit-workflow

cursor agent --workspace . "/citfix 97203 --resume"
```

等价自然语言 prompt：

```text
Load the citfix skill from this workspace. Execute /citfix 97203 --resume.
Follow agent-protocol.md. Stop at checkpoint with full report.
```

### 恢复已有 CLI 线程

```powershell
# 列出历史线程（以本机 Cursor CLI 实际子命令为准）
cursor agent ls

# 恢复
cursor agent --workspace D:\Workspace\cit-workflow --resume <thread-id> ^
  "Continue citfix for bug 97203. Read workflow_state.json first."
```

> **说明**：Cursor CLI 子命令随版本可能为 `cursor agent` / `cursor chat`；以本机 `cursor --help` 为准。核心不变：**workspace = cit-workflow**，**首句携带 /citfix**。

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

## 4. 与 monitoring 脚本配合（可选）

`cit-workflow/monitoring/` 下 APScheduler 脚本可：

1. 读 `workflow_state.json` 的 `workflow_status`
2. 若 `blocked` 超过阈值，向 CLI 注入：`/citfix {bug_id} --resume`
3. 若 `completed`，停止推送

Skill 不实现调度；调度器只发 **/citfix** 口令，由 Agent 执行 Skill 协议。

## 5. 退出码（仅当 Agent 调用 citfix.py 时）

| 码 | 含义 | Agent 行为 |
|----|------|------------|
| 0 | 自动段同步完成或全流程 completed | 继续 agent 段或汇报完成 |
| 2 | 引擎层 blocked | 读 CHECKPOINT，按 Skill 卡点报告 |
| 1 | 配置/参数错误 | 引导 cit-setup |

Agent 在 IDE 内**不必**向用户展示退出码，应翻译为卡点四要素。

## 6. 示例：无人值守一轮

```powershell
cursor agent --workspace D:\Workspace\cit-workflow --force ^
  "citfix skill: /citfix 97203 --resume. Max one stage per turn. If blocked, write CHECKPOINT.md and stop."
```

适合 monitoring 定时触发。
