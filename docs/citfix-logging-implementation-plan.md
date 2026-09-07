# CitFix 结构化日志 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **变更追踪真源：** 本文 + §9 变更影响登记表。配套可视化：会话 Canvas `citfix-logging-rollout.canvas.tsx`；流程图可在 draw.io（Mermaid 打开稿）中编辑。

**Goal:** 在正式产物树 `runs/<PRODUCT>/<run_id>/logs/` 落地可归档的结构化运行日志，使公司推广后即使删除 `runs_work` 仍可调试、监控与排障。

**Architecture:** 日志与阶段 `output/` 隔离；引擎/Watch/约定下的 Agent 写入 `run_events.jsonl`；阶段明细文本落 `logs/stages/`。`workflow_state.json` 仍为状态真源；日志是旁路可观测层，不抢状态写权。

**Tech Stack:** Python（`citfix/`）、JSONL 事件、现有 pytest、`contracts/` 契约、Cursor skill 约定。

## Global Constraints

- 正式落盘根：`runs/<PRODUCT>/<run_id>/logs/`（批次：`runs/<PRODUCT>/<batch_id>/logs/`）
- **禁止**将运行日志写入各阶段 `*/output/` 门禁目录
- **禁止**把持久日志唯一真源放在 `runs_work/`（过渡期可短时双写，以正式侧为准）
- 事件最小字段：`ts`, `run_id`, `bug_id`, `stage_id`, `actor`, `event`, `level`, `summary`, `refs`
- `actor` ∈ `engine | agent | watch | mcp | cli`
- 必须脱敏：token、password、Authorization、SSH 口令明文不得入日志
- 不引入外部日志平台（无 ELK/DB）；文件真源与现有 Watch 旁路模式一致
- 本仓仅用 `cit-*` skill / 仓内 MCP；不加载用户级 `bugfix-*`

### 已确认目录树（终态）

```text
runs/<PRODUCT>/<run_id>/
├── <NN_stage>/output/           # 交付真源（不变）
├── logs/
│   ├── run_events.jsonl         # 跨阶段结构化时间线
│   └── stages/
│       └── <run_id>_<stage>.log # 阶段明细文本
├── watch/                       # 监督旁路迁到正式树（与日志同期规划）
│   ├── events.jsonl
│   ├── watch_state.json
│   └── snapshots/
├── workflow_state.json
└── run.json / …
```

---

## 实施阶段总览

| 阶段 | 步骤 | 目标 | 工作流是否可继续跑 |
|------|------|------|-------------------|
| Phase 0 契约 | 1 | 路径与事件 schema 冻结 | 是（仅文档/契约） |
| Phase 1 核心库 | 2–3 | 可写正式 `logs/` 的 API + 路径 | 是（未接线则行为不变） |
| Phase 2 引擎/Auto | 4–6 | Auto 段全生命周期事件 + 阶段日志迁址 | 是（增强可观测） |
| Phase 3 Agent/Watch | 7–8 | Agent 里程碑 + Watch 正式路径/心跳 | 是（监控更准） |
| Phase 4 硬化交付 | 9–12 | CLI/测试/文档/从 runs_work 切流 | 是（推广口径对齐） |

```text
[现有 00–14 工作流] ──旁路写入──► runs/.../logs/run_events.jsonl
        ▲                              ▲
        │                              │
   workflow_state（真源）         watch/（监督，读状态+可选读事件）
```

---

## 编号实施步骤（含模块影响）

### 步骤 1：冻结路径与事件契约

- [ ] 新增/更新 `contracts/citfix_run_events.schema.json`（或等价 JSON Schema）
- [ ] 在 `docs/citfix-logging.md` 写明目录树、字段、脱敏、与 `output/` 边界
- [ ] 更新变更登记表（本文 §9）状态为 `planned → contracted`

**涉及模块：** `contracts/`、`docs/`（新建说明）；不改运行时代码。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 后续实现有单一真源，审查可对照 schema | 契约过早过细会束缚；保持最小字段即可 |
| 模块依赖 | 引擎/Watch/skill 对齐同一事件形状 | 无运行时依赖变化 |
| 整体工作流 | 零运行影响；推广沟通口径统一 | 无 |

---

### 步骤 2：实现 `run_log` 追加 API（含脱敏）

- [ ] 创建 `citfix/run_log.py`：`append_run_event(formal_run_dir, event) -> Path`
- [ ] 原子追加（必要时临时文件+锁或单行 append + flush）；坏行可跳过读取
- [ ] `redact_text` / `redact_event`：过滤常见密钥字段
- [ ] 单元测试：字段校验、脱敏、目录自动创建

**涉及模块：** 新建 `citfix/run_log.py`；`tests/test_citfix_run_log.py`。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 单一写入入口，避免各处手写 jsonl | 新增模块需维护；写入失败策略需明确（默认吞掉并打 stderr，不阻断流水线） |
| 模块依赖 | 被 engine/stages/watch/cli 依赖；不依赖 MCP | 调用方若绕过 API 会导致格式分裂 |
| 整体工作流 | 未接线前无影响；接线后失败默认不阻断业务 | 若误设为硬失败，会放大 I/O 故障影响面 |

---

### 步骤 3：正式路径接入 `RunContext` / paths

- [ ] `citfix/models.py`：`run_events_path()`、`formal_stage_log_path()` 指向 `formal_dir/logs/...`
- [ ] `citfix/paths.py` / `artifact_paths.py`：如有硬布局校验，允许 `logs/`、`watch/` 作为 run 根合法子树
- [ ] 产品迁址（`ensure_formal_under_product`）时同步迁移已有 `logs/`、`watch/`

**涉及模块：** `citfix/models.py`、`citfix/paths.py`、`citfix/artifact_paths.py`、`citfix/engine.py`（迁址逻辑）。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 路径集中，消灭硬编码 | `stage_log_path` 语义从中间→正式，调用点需一并改 |
| 模块依赖 | 阶段 handler 继续只拿 Path；不感知产品名细节 | 迁址漏搬 `logs/` 会造成时间线断裂 |
| 整体工作流 | 为删 `runs_work` 铺路 | 短期正式目录体积增大 |

---

### 步骤 4：阶段文本日志迁到 `runs/.../logs/stages/`

- [ ] 修改 `RunContext.stage_log_path`：写入 `formal_dir/logs/stages/<run_id>_<stage>.log`
- [ ] 保留 `_log()` 于 `util_io.py`（或改为调用统一 helper）
- [ ] `workflow_state` 中 `log_path` 字段改为正式相对路径
- [ ] 过渡：可选双写中间路径一版，下一版本删除

**涉及模块：** `citfix/models.py`、`citfix/stages/util_io.py`、`citfix/stages/__init__.py`、`citfix/stages/auto_prepare.py`、`citfix/stages/req_extract.py`、`citfix/stages/prepare_common.py`、`citfix/project_batch.py`、`citfix/engine.py`（`rec.log_path`）。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 明细日志随 run 归档，同事可直接打开 | 旧 run 仍在 `runs_work/.../<stage>/logs/`，需文档说明兼容 |
| 模块依赖 | handler 签名不变（仍收 `log_path`） | `--debug` / 文档若仍指向中间路径会误导 |
| 整体工作流 | Auto 段排障不依赖中间树 | 正式树变「重」一点；需轮转/体积约定（P2） |

---

### 步骤 5：引擎生命周期埋点

- [ ] 在 `run_pipeline`：`stage_start` / `stage_end` / `stage_blocked` / `stage_error` / `pipeline_completed`
- [ ] `claim` / 新建 run / resume 入口写 `run_start` / `run_resume`
- [ ] 异常：`summary` + `refs.exc_type`（不写完整密钥栈若含环境变量）

**涉及模块：** `citfix/engine.py`、`citfix/claim_only.py`；间接 `scripts/citfix.py`。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 状态机推进可对齐事件时间线 | engine 变「更重」；必须保持日志失败不阻断 |
| 模块依赖 | engine → run_log；Watch 后续可读事件 | 与 `save_state` 顺序：先状态后事件（或相反）需固定，避免半写 |
| 整体工作流 | 卡点/resume/完成可事后复盘；完善可靠性 | 高频阶段切换产生更多磁盘写入（可接受） |

---

### 步骤 6：阶段门禁与 MCP/编译结果 refs

- [ ] `validate_outputs` / agent_gate 失败时写 `gate_fail`（字段名列表进 `refs`）
- [ ] 编译/刷机相关阶段：把 `compile_log_path` / exit_code / log_tail 哈希写入 `refs`（不整份拷贝）
- [ ] Auto prepare 关键关键附件下载失败等已有 `_log` 处，同步一条 `warn` 事件

**涉及模块：** `citfix/stages/validate_outputs.py`、`citfix/stages/agent_gate.py`、`citfix/stages/auto_prepare.py`；MCP 调用封装处（若在 `bugflow/` / `mcp/` 有统一出口则挂一层薄包装）。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 门禁失败可检索；外部日志可挂接 | 各阶段散落调用易漏；优先在公共 gate/execute 出口埋 |
| 模块依赖 | stages → run_log；不改变 MCP 协议 | 若在 MCP server 内写日志，需传入 `formal_run_dir`，耦合上升——优先在 citfix 层记 |
| 整体工作流 | 编译/刷机失败与 run 时间线对齐 | `refs` 过大需截断 |

---

### 步骤 7：Agent 段里程碑（skill 约定 + brief）

- [ ] `agent_gate` / `AGENT_BRIEF` 增加「必须 append 的事件清单」：`agent_stage_enter`、`agent_gate_pass|fail`、`agent_checkpoint`
- [ ] 更新 `.cursor/skills/citfix/agent-protocol.md` 与各 `cit-*` 关键阶段说明（3～5 条，禁止全量对话落盘）
- [ ] 可选：提供 `scripts/cit_run_event.py` 供 Agent 一行命令追加

**涉及模块：** `citfix/stages/agent_gate.py`、`.cursor/skills/citfix/*`、各 `cit-analyze`…`cit-submit`；可选 `scripts/cit_run_event.py`。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 补齐 07+ 可观测空白 | skill 约定靠遵守，可能漂移；CLI 小工具可降低漂移 |
| 模块依赖 | Agent → 文件事件；不改状态机语义 | 多一个脚本入口需进 rollout 文档 |
| 整体工作流 | 会话消失后仍有里程碑；提高可维护性 | Agent 多几次工具调用，轻微延迟 |

---

### 步骤 8：Watch 正式路径 + 事件心跳判据

- [ ] Watch 状态/events/snapshots 写入 `runs/<PRODUCT>/<run_id>/watch/`（读 `workflow_state` 仍可双读中间/正式，以正式为准）
- [ ] `append_watch_event` 同时（或改为）写入/镜像一条 `actor=watch` 到 `run_events.jsonl`
- [ ] `classify`：可选「无新 run_event 且超时」→ nudge（配置开关，默认先 dry-run 观察）
- [ ] 更新 `docs/citfix-watch.md`、`config/citfix/watch.yaml.example`

**涉及模块：** `citfix/watch/*`（`state_io.py`、`scan.py`、`classify.py`、`actions.py`、`service.py`）、`scripts/cit_watch_*.py`、`docs/citfix-watch.md`。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 监督数据随正式 run 归档；心跳更真实 | 扫描路径从 `runs_work` 改为/兼扫 `runs/`，配置与测试要改 |
| 模块依赖 | Watch 仍只读状态真源；事件为辅助时钟 | 误配心跳超时 → 误 nudge（必须默认保守 + dry-run） |
| 整体工作流 | 监控与排障统一在 `runs/`；支撑删中间树 | 迁移期两套 watch 目录并存需兼容逻辑 |

---

### 步骤 9：批次与 CLI 表面

- [ ] `project_batch`：`runs/<PRODUCT>/<batch_id>/logs/run_events.jsonl` 记录 01/02 发现过程
- [ ] `citfix.py --debug` / `--status`：打印正式 `logs/` 与最近 N 条事件
- [ ] 确认 `outbox_events.jsonl`（14_closure）与 `run_events` 职责不混淆（通知 vs 运行轨迹）

**涉及模块：** `citfix/project_batch.py`、`scripts/citfix.py`、`citfix/outbox.py`（仅文档边界，尽量不改逻辑）。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 批次与单 bug 一致可观测 | CLI 输出变长 |
| 模块依赖 | 与 outbox 解耦（不同文件） | 命名相似易混——文档必须对比表 |
| 整体工作流 | 同事用 `--status` 即可看健康度 | 无 |

---

### 步骤 10：测试与回归

- [ ] `tests/test_citfix_run_log.py`：写入/脱敏/路径
- [ ] `tests/test_citfix_engine_events.py`：跑 fixture 流水线断言事件序列
- [ ] `tests/test_citfix_watch.py`：正式 `watch/` 路径 + 可选心跳
- [ ] 路径/迁址测试：产品从 `_unassigned` 迁入后 `logs/` 仍在

**涉及模块：** `tests/*`；间接锁定 engine/watch/models 行为。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 防止回归到 runs_work | CI 时间略增 |
| 模块依赖 | 契约测试成为跨模块粘合剂 | fixture 需带正式目录布局 |
| 整体工作流 | 提高可维护性与推广信心 | 无 |

---

### 步骤 11：文档与推广口径

- [x] 更新 `README.md` §1.1：日志 = 正式 `runs/.../logs/`；中间树不含日志真源
- [x] 更新 `.cursor/skills/citfix/*`、`AGENTS.md`、`cit-reproduce` / `cit-verify` 相关句
- [x] 更新 `docs/cit-company-rollout.md`：归档交付含 `logs/`
- [x] 标注 `runs_work` 为过渡镜像（可删目标；非日志真源）

**涉及模块：** 文档与 skill；无代码行为变化。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 公司推广口径一致 | 旧文档/经验库分册需后续对齐（EXP-CIT-* 另任务） |
| 模块依赖 | 降低「日志在中间树」误用 | 无 |
| 整体工作流 | 为删除 `runs_work` 做组织准备 | 无 |

---

### 步骤 12：切流与清理（cutover）

- [x] 关闭阶段日志对 `runs_work/.../<stage>/logs` 的写入（`stage_log_path` → 正式 `logs/stages/`）
- [x] Watch 默认 `scan_intermediate_runs: false`（只扫正式 `runs/`）
- [x] 可选迁移：`scripts/cit_migrate_logs.py`
- [x] 登记表状态 `done`；兼容窗口起算：2026-09-07（中间树旧 logs 只读，不新写）

**涉及模块：** engine/models/watch/scripts；可选 `scripts/cit_migrate_logs.py`。

| 影响面 | 正面 | 负面 / 风险 |
|--------|------|-------------|
| 模块自身 | 单一落点，删除中间树成为可能 | 未迁移的旧 run 排障仍要翻历史目录 |
| 模块依赖 | 依赖面收敛到 `runs/` | 激进删除 `runs_work` 前须确认 brief/checkpoint 也有正式侧方案（日志之外的另项工作） |
| 整体工作流 | 公司工具形态更清晰、可维护 | cutover 需发版说明；建议先「只写正式」再「停写中间」分两周 |

---

## 与现有工作流的集成方式（无缝性）

1. **不改阶段顺序与门禁语义**：00–14 契约、`citfix_pipeline.json`、stage gates 保持不变。  
2. **旁路写入**：事件在 `save_state` / stage execute 之后追加；失败默认不阻断。  
3. **职责分离**：`workflow_state` = 续跑真源；`run_events.jsonl` = 轨迹；`watch/` = 监督；`*/output/` = 交付。  
4. **推广友好**：同事只需分享/归档 `runs/<PRODUCT>/<run_id>/` 即可复盘。

### 使工作流更完善 / 可靠 / 可维护的点

| 维度 | 机制 |
|------|------|
| 完善 | 补齐 Agent 段与 MCP 结果在时间线上的空洞 |
| 可靠 | 会话消失后仍可对齐卡点；Watch 可用事件心跳降低误判 |
| 可维护 | 单一正式落点 + schema + 测试；为删除 `runs_work` 去耦 |

---

## §9 变更影响登记表（可追踪）

| ID | 步骤 | 关键模块 / 文件 | 改动点摘要 | 依赖变化 | 风险等级 | 状态 |
|----|------|-----------------|------------|----------|----------|------|
| LOG-01 | 1 | `contracts/`、`docs/citfix-logging.md` | schema + 目录契约 | 无运行时 | 低 | done |
| LOG-02 | 2 | `citfix/run_log.py`、`tests/test_citfix_run_log.py` | append + redact | 新建被依赖方 | 低 | done |
| LOG-03 | 3 | `models.py`、`paths.py`、`artifact_paths.py`、`engine.py` 迁址 | 正式 logs 路径 API | 路径语义变更 | 中 | done |
| LOG-04 | 4 | `stages/*`、`project_batch.py`、`engine` log_path | 阶段 .log 迁正式 | 调用点路径变 | 中 | done |
| LOG-05 | 5 | `engine.py`、`claim_only.py` | 生命周期事件 | engine→run_log | 中 | done |
| LOG-06 | 6 | `validate_outputs.py`、`agent_gate.py`、auto stages | gate/MCP refs | stages→run_log | 中 | done |
| LOG-07 | 7 | skills、`agent_gate`、`scripts/cit_run_event.py` | Agent 里程碑 | 约定依赖 | 中高 | done |
| LOG-08 | 8 | `citfix/watch/*`、watch 文档/配置 | 正式 watch + 心跳 | 扫描根变更 | 高 | done |
| LOG-09 | 9 | `project_batch.py`、`scripts/citfix.py` | CLI/批次表面 | 只读展示 | 低 | done |
| LOG-10 | 10 | `tests/test_citfix_*` | 回归锁定 | 无 | 低 | done |
| LOG-11 | 11 | README、citfix skill、AGENTS、company-rollout、相关 skill | 口径切换 | 无 | 低 | done |
| LOG-12 | 12 | engine/watch/scripts、`cit_migrate_logs.py` | 停扫/停写中间树日志；正式为唯一日志真源 | 收敛依赖 | 高 | done |

**登记规则：** 实现某步骤时把状态改为 `in_progress` → `done`，并在 PR/提交说明中引用 `LOG-0x`。扩展新事件类型时先改 schema（LOG-01）再改写入点。

---

## 明确不在本次范围

- 集中式日志平台 / Web 监控大盘  
- 全量 Agent 对话或全量 tool stdout 落盘  
- 删除整个 `runs_work`（仅日志切流；brief/checkpoint 正式化另立项）  
- 修改禅道/Gerrit 业务语义  

---

## 建议实施顺序（检查清单）

- [x] LOG-01 契约  
- [x] LOG-02 库  
- [x] LOG-03 路径  
- [x] LOG-04 阶段日志迁址  
- [x] LOG-05 引擎埋点  
- [x] LOG-06 门禁/MCP refs  
- [x] LOG-07 Agent 里程碑  
- [x] LOG-08 Watch  
- [x] LOG-09 CLI/批次  
- [x] LOG-10 测试  
- [x] LOG-11 文档  
- [x] LOG-12 切流（2026-09-07：只写正式 logs；Watch 默认不扫 runs_work runs；可选 `cit_migrate_logs.py`）  

**文档版本：** 2026-09-07  
**对齐决策：** 日志落正式 `runs/.../logs/`；不作为 `runs_work` 唯一真源。
