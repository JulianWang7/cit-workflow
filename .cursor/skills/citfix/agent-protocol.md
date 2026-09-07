# citfix Agent 交互协议

Agent 每轮执行 `/citfix` 时必须遵守本协议。

## 0. 能力隔离（强制）

- **只**加载本仓 `.cursor/skills/cit*/**`
- **禁止**加载 `~/.cursor/skills/bugfix-*` 或任何用户级 skill
- **只**使用本仓 `.cursor/mcp.json` → `cit_mcp_launch.py` 启动的 MCP（`ssh-mcp` / `zentao-mcp` / …）
- 若协议旧文仍出现 `bugfix-*`，一律替换为对应 `cit-*` 项目 skill

## 0.1 MCP 就绪门禁（强制，针对「SSH MCP isn't loaded」）

进入 **07+** 或任何需要远程搜码/改码/编译的步骤前：

1. 确认本会话可调用 MCP 工具：`set_workspace`、`search_code_tool`（或至少 `config_status`）
2. **若工具列表里没有 `ssh-mcp` / 调用失败**：
   - **禁止**假装「改用 bugflow SSH 直连」继续分析并当作 MCP 已通
   - **立即**按卡点四要素停下，`step=mcp_not_loaded`
   - `next_actions` 必须包含：
     1. `python scripts/cit_smoke_mcp.py`（或 `--render`）
     2. `python scripts/cit_render_mcp_json.py`（本机绝对路径 + venv）
     3. **新开** `cursor-agent --workspace <cit-workflow> "/citfix <id> --resume"`（旧会话常不重新加载 MCP）
     4. IDE：Settings → MCP → `ssh-mcp` 为启用/绿色
3. **唯一允许的非 MCP SSH**：确定性 Worker（如 `scripts/cit_closure_run.py` / 引擎 auto 段已封装的 bugflow 调用），且须在报告中写明 `ssh_path=worker`，**不得**口述「via bugflow SSH」混充 MCP

详见 `docs/cit-mcp-reference.md` §8、`.cursor/skills/citfix/cli-integration.md` §2.1。

## 1. 首轮 checklist

- [ ] 解析命令：`/citfix <bug_id>` 或 `/citfix project <alias>`（`project` 大小写不敏感）；flags：`--resume` / `--status` / `--new` / `--device` / `--dry-run` / `--debug`
- [ ] 若为 project：读/写 `runs_work/projects/<PRODUCT>/batches/<batch_id>/batch_state.json`；**串行**处理 queue；仅 auto CIT；子 bug 用 `force_verify_auto`
- [ ] 正式产物路径：`runs/<PRODUCT>/<run_id>/`（例 `runs/SLB783 - Android14/CIT-20260903-81097/`）
- [ ] 若 `--debug`：打印 formal/intermediate 路径、checkpoint、关键产物后可结束（与 `--status` 类似，信息更全）
- [ ] 定位 `run_id` 与 `workflow_state.json`
- [ ] 若 `--status`：输出阶段表后 **结束**，不修改文件
- [ ] 读取当前阶段 KB 分册（`kb_doc_path` 或 `workflow-stages.md`）
- [ ] 读取正式 output：`cit-workflow/runs/<run_id>/<stage>/output/`（中间镜像：`runs_work/projects/<PRODUCT>/runs/...`）

## 2. 阶段内行为

### 2.1 Agent 段运行里程碑（强制，LOG-07）

结构化事件写在正式目录（**禁止**写进 `*/output/`）：

```text
runs/<PRODUCT>/<run_id>/logs/run_events.jsonl
```

| 时机 | event | 谁写 |
|------|-------|------|
| 进入阶段 / brief 已写 | `agent_stage_enter` | 引擎（写 brief 时）+ Agent 可补一条 |
| 产物门禁通过 | `agent_gate_pass` | 引擎 |
| 产物门禁失败 | `agent_gate_fail` | 引擎 |
| 人工卡点 / 等待外部 | `agent_checkpoint` | **Agent 必须**用脚本追加 |

追加命令（一行即可，禁止把全量对话落盘）：

```powershell
cd D:\Workspace\cit-workflow
.\.venv\Scripts\python.exe scripts\cit_run_event.py `
  --run-dir "runs/<PRODUCT>/<run_id>" `
  --bug-id <id> --stage 07_analysis `
  --event agent_checkpoint --level warn `
  --summary "blocked: waiting device / more evidence"
```

卡点报告输出给用户后，**同时**写 `agent_checkpoint` 事件。详见 `docs/citfix-logging.md`。


### 自动段（03–06b）

Agent **可以**：

- 调**项目** MCP：`zentao_get_bug`、`set_workspace`、`config_status`
- 或运行状态同步：

  ```powershell
  cd D:\Workspace\cit-workflow
  .\.venv\Scripts\python.exe scripts\citfix.py <bug_id> --resume
  ```

Agent **不应**：

- 把「跑 citfix.py」作为对用户的首条回复
- 在未读 state 的情况下新建 run 覆盖旧进度
- 调用用户级 bugfix skill

### Agent 段（07–14）— Skill 集成调度

进入 agent 段时，引擎会写入：

```text
runs_work/projects/<PRODUCT>/runs/<run_id>/<stage>/intermediate/agent_brief.json
runs_work/projects/<PRODUCT>/runs/<run_id>/<stage>/intermediate/AGENT_BRIEF.md
```

Agent **必须**按下列顺序（**不得跳过、不得改用用户级 bugfix-***）：

#### 07_analysis

1. Read `intermediate/AGENT_BRIEF.md`
2. Read 并**完整执行** `.cursor/skills/cit-analyze/SKILL.md`
3. 写入 `07_analysis/output/root_cause_<bug_id>.json`（含 `marker: ROOT_CAUSE_FOUND`）
4. `/citfix <id> --resume`（或更新 workflow_state）→ 引擎校验文件后标 completed，进入 08

#### 08_changes

1. Read `AGENT_BRIEF.md`（inputs 含 07 的 root_cause）
2. Read 并执行 `.cursor/skills/cit-modify/SKILL.md` → 写 `change_<bug_id>.json`（`PATCH_DONE`）
3. Read 并执行 `.cursor/skills/cit-review/SKILL.md` → 更新同文件 `review_status`
4. `/citfix <id> --resume` → completed 后进入 09

#### 12_cit_test / 13_human_gate / 14_closure

门禁契约见 `docs/citfix-stage-gates.md`（**运行时字段真源**）。业务框架（①–⑧ / ⑥ / ⑦）对齐知识库 EXP-CIT-004 **§2–5**；二者分层，互不替代。

- 读 `06/context.json` → `routing.verify_mode`（`auto|human|hybrid`）
- 12：写 `auto_assertions` / `human_assertions`；`VERIFY_PASS` 要求 auto 全 `passed=true`
- 13：`auto` → `gate_status=auto_bypassed|not_required`；`human|hybrid` → 须 `approved`；`pending_human` **不得** completed
- 14：仅 `marker=CLOSURE_DONE`；读并执行 `.cursor/skills/cit-submit/SKILL.md`；`closure_mode=local_commit_only|full|evidence_only`（默认 `local_commit_only`，禁 push）；上游 12/13 门禁必须已通过

#### 14_closure（摘要）

1. **优先**跑确定性 Worker（默认只 commit）：
   `python scripts/cit_closure_run.py --run-dir <formal_run> --bug-id <id>`
2. Commit message：`[Product][BugID|TaskID]<id>[Description]…[Solution]…`（见 cit-submit）
3. 或 Read `cit-submit/SKILL.md` 后手搓 MCP（**禁止默认 push**）
4. 写正式 `14_closure/output/closure_<bug_id>.json`（`CLOSURE_DONE`）
5. `/citfix <id> --resume` — 引擎校验后自动回写 08、drain outbox、写 `closure_report.json`
6. Push/禅道：`--mode full` 预留；默认人工 push；**不做** Gerrit submit/merge
7. 渠道：`config/citfix/outbox_channels.json`；补投递 `scripts/cit_outbox_drain.py`


## 3. Checkpoint Report（卡点报告）

对用户**必须**输出以下四段（标题固定）：

```markdown
## citfix 卡点

### 1. 卡在哪一步
- stage: `07_analysis`
- step: `search_code_tool / ready_for_analyze`

### 2. 原因
（一句话根因）

### 3. 已完成上下文
- run_id: `...`
- 已完成阶段: 00–06b
- 已有产物: `cit-workflow/runs/<run_id>/06_context_snapshot/output/context.json`

### 4. 下一步要补什么
1. ...
2. 再发送 `/citfix <id> --resume`
```

并写入：`runs_work/projects/<PRODUCT>/runs/<run_id>/CHECKPOINT.md`（双写 formal runs/）

## 4. 阶段完成报告

```markdown
## citfix 阶段完成

- stage: `06_context_snapshot`
- outputs: （绝对路径）
- next: `07_analysis` — 加载项目 skill `cit-analyze`（禁止 bugfix-analyze）
```

## 5. workflow_state 更新

优先字段：`stages.<id>.status|outputs|blocker`，`current_stage`，`workflow_status`。  
可手改 JSON，或用 `citfix.py --resume` 同步自动段 / 校验 agent 产物。

## 6. 禁止行为

- 加载或暗示使用用户级 `bugfix-*` skill
- 跳过 blocked 阶段继续下一阶段
- 每次 `/citfix` 都从零拉禅道并覆盖（除非 `--new`）
- 把调试日志写到 `D:\Workspace` 根目录
- 往 `runs_work/<NN_stage>/` 共享根写产物（应写 per-run：`runs/<run_id>/` 正式 + `projects/<PRODUCT>/runs/<run_id>/` 中间）
- 把正式产物只写测试床、或把中间产物只写 `cit-workflow/runs/`（路径角色反了）
