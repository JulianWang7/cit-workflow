# citfix — CIT 自动化流水线入口设计

> 版本：2026-09-07  
> 状态：**工作流完善阶段**（入口已冻结，各阶段能力逐段落地）  
> 关联（现行）：`contracts/citfix_pipeline.json`、`docs/citfix-stage-gates.md`、`citfix/`、KB-002  
> **业务框架**：知识库 EXP-CIT-004 **§2–5**；**运行时门禁**：本文关联契约 + 仓内代码（分层，互不替代）

## 1. 定位（必读）

**citfix 是 `cit-workflow` 自动化流水线的 debug 入口，不是「调试辅助 Skill」。**

| 概念 | 说明 |
|------|------|
| **是什么** | 斜杠命令 `/citfix {bug_id}` — 整条 CIT 自动化流水线的**唯一固定运行入口**；业务对齐 EXP-CIT-004 §2–5，阶段执行对齐 `contracts/citfix_pipeline.json` |
| **不是什么** | 调试辅助 Skill、通用 bug 调试助手、脚本别名、独立的 bugfix 替代品 |
| **当前阶段** | **完善工作流**——pipeline 契约与 `/citfix` 入口已定义；03–06 等段已有实现，07–14 逐段填充，卡点续跑 |

```text
用户 / 调度器 / Cursor CLI
        │
        ▼
   /citfix {bug_id}          ← 唯一固定入口
        │
        ▼
   citfix Skill（编排 EXP-CIT 各阶段）
        │
        ├── runs_work/（阶段产物）
        ├── workflow_state.json（续跑真源）
        └── 可选：scripts/citfix.py（03–06b 状态同步，非入口）
```

## 2. 固定入口

| 用户输入 | 加载 |
|----------|------|
| `/citfix 97203` | `.cursor/skills/citfix/SKILL.md` + `citfix-guide.mdc` |
| `/citfix 97203 --resume` | 同上 + **必须先读** `workflow_state.json` / `CHECKPOINT.md` |

用户**不应**以「先手动跑 `scripts/citfix.py`」作为使用流水线的方式。Agent 在 citfix Skill 下**按阶段推进流水线**；脚本仅在 03–06b 状态同步时由 Agent **择机**调用。

## 3. 职责边界

```text
┌─────────────────────────────────────────────────────────┐
│  /citfix — CIT 自动化流水线入口（交互编排层）              │
│  · 按 citfix_pipeline.json 推进 00–14 阶段                │
│  · 读 KB 分册 + workflow_state + 测试床产物               │
│  · 阶段内：项目 MCP、项目 cit-* skill、写 JSON、卡点、续跑   │
│  · 完善阶段：未就绪段卡点停住，不跳步                      │
│  · 禁止：用户级 ~/.cursor/skills/bugfix-*                 │
└───────────────────────┬─────────────────────────────────┘
                        │ 底层（非入口）
                        ▼
┌─────────────────────────────────────────────────────────┐
│  scripts/citfix.py · citfix/ · pipeline.json      │
│  （auto 段执行 / agent 段仅校验 output 文件是否存在）       │
└─────────────────────────────────────────────────────────┘
```

## 4. 运行场景

### 4.1 主场景：Cursor CLI 跑流水线

工作区：`D:\Workspace\cit-workflow`

```powershell
cursor-agent --workspace . "/citfix 97203 --resume"
```

进度真源：`runs_work/projects/<PRODUCT>/runs/<run_id>/workflow_state.json`

### 4.2 建设场景：IDE 内完善流水线实现

触发：「完善 citfix 第 09 阶段 / 改 pipeline / 补引擎」→ `ide-maintenance.md`  
仍在 **cit-workflow 仓库**内建设，不改变 `/citfix` 作为运行入口的定位。

## 5. Skill 目录

```text
.cursor/skills/citfix/
├── SKILL.md                 # 流水线入口（本 Skill 主文档）
├── agent-protocol.md        # 阶段推进与卡点格式
├── workflow-stages.md       # 阶段 ↔ KB ↔ 产物
├── cli-integration.md       # Cursor CLI
└── ide-maintenance.md       # 建设期：改引擎/阶段代码

.cursor/rules/citfix-guide.mdc
contracts/citfix_pipeline.json
docs/citfix-skill-design.md  # 本文档
```

## 6. 核心工作流

```text
/citfix bug_id
 → 读 workflow_state，确定 current_stage
 → 读 EXP-CIT 当前分册
 → 执行本阶段（完善中的段允许 partial + 卡点）
 → 正式写 cit-workflow/runs/<run_id>/<stage>/output/；中间镜像 runs_work/projects/<PRODUCT>/runs/<run_id>/
 → 通过则 completed 并推进；否则 CHECKPOINT 停住
 → /citfix --resume 从停处继续
```

## 7. 与 android-bugfix-flow 的区别

| | citfix（本仓） | bugfix（用户级 / 插件仓） |
|--|----------------|---------------------------|
| 入口 | `/citfix` | `/bugfix` |
| Skill | 仅 `.cursor/skills/cit*` | `~/.cursor/skills/bugfix-*` |
| MCP | 仓内 `cit_mcp_launch` + 拷贝的 bugflow | 用户 mcp.json → android-bugfix-flow |
| 产物 | `runs_work/projects/<PRODUCT>/runs/<run_id>/` | 会话 + ~/.bugfix-flow |
| 状态 | `workflow_state.json` | 全局 `workspace.json` |
| 现状 | 完善阶段，逐段落地 | 已成熟 |

流水线 07+ **只调用本仓 `cit-*` skill**，不再调用用户级 `bugfix-*`。公共 MCP **源码**已拷贝进本仓；运行时凭据仍可读 `~/.bugfix-flow`。

## 8. 批量接口（预留）

单 Bug 入口：`/citfix {bug_id}`。跨多 Bug 闭环写回见：

- `docs/citfix-batch-api.md`
- `contracts/citfix_batch_api.json`（`citfix_batch_resolve`）

与 bugfix `/batch_solve` 对齐的输入：`batch_id`、`bug_ids[]`、`operator`、`resolve_result`、`comment`。

## 9. 验收（建设期）

- [ ] 用户说 `/citfix 97203` → 加载 citfix Skill，按 pipeline 报当前阶段
- [ ] 不以「请运行 citfix.py」作为对用户的主回复
- [ ] 卡点有四要素 + `CHECKPOINT.md`
- [ ] `--resume` 接续同一 run，已完成阶段不覆盖
- [ ] 产物在 `runs_work`，建设代码在 `cit-workflow/citfix/`
- [ ] 批量契约已预留，实现前不声称具备批量禅道写回 API
