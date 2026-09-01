---
name: citfix
description: |
  cit-workflow 自动化流水线的 debug 入口（固定斜杠命令 /citfix {bug_id}）。
  不是「调试辅助 Skill」——是整条 CIT 流水线（EXP-CIT-004..011）的唯一运行入口。
  当前处于工作流完善阶段；读 workflow_state 续跑，写 cit-workflow-test 阶段产物。
  触发词：/citfix、citfix 97203、citfix --resume、CIT 流水线、CIT 自动化。
---

# citfix — CIT 自动化流水线 debug 入口

> **定位**：`cit-workflow` 自动化流水线的**固定 debug 入口**（`/citfix {bug_id}`）。  
> **不是**「调试辅助 Skill」或通用调试工具；**是**整条流水线的唯一运行入口。  
> **现状**：**工作流完善阶段**——阶段定义已冻结于 `workflow/citfix_pipeline.json`，各阶段能力**逐段落地**；未实现段以卡点 + 续跑方式推进，不跳步。  
> **你是流水线编排者**，不是脚本运行器。用户输入 `/citfix 97203` 时，按预定义阶段推进 CIT 工作流；不要只执行 `scripts/citfix.py` 后结束。

## 子文档（按需 Read）

| 文档 | 何时读 |
|------|--------|
| [agent-protocol.md](agent-protocol.md) | **每轮必遵** — 阶段推进与卡点格式 |
| [workflow-stages.md](workflow-stages.md) | 当前阶段、产物路径、KB 分册 |
| [cli-integration.md](cli-integration.md) | Cursor CLI 启动或 resume thread |
| [ide-maintenance.md](ide-maintenance.md) | 完善流水线引擎/阶段代码（建设期） |

阶段契约：`workflow/citfix_pipeline.json`  
设计说明：`docs/citfix-skill-design.md`

---

## 固定入口

```text
/citfix 97203
/citfix 97203 --resume
/citfix 97203 --status
/citfix 97203 --new
```

| 输入 | 动作 |
|------|------|
| `/citfix 97203` | 接续该 bug 最新 run，或新建 run 从阶段 00 起 |
| `--resume` | **必须**读 `workflow_state.json` + `CHECKPOINT.md` 后续跑 |
| `--status` | 只读 state，输出阶段表，不改文件 |
| `--new` | 新建 run_id |

状态真源：`D:\Workspace\cit-workflow-test\00_runs\<run_id>\workflow_state.json`

---

## 每轮流程（流水线推进）

1. **定位 run** — 按 `bug_id` 找最新未完成 run
2. **读状态** — `current_stage`、`workflow_status`、各 stage `status` / `blocker`
3. **读 KB** — 当前阶段 EXP-CIT-* 分册（`kb_doc_path`）
4. **读产物** — `cit-workflow-test/<NN>_*/output/` 已有 JSON
5. **执行当前阶段**（完善中的段可能需 Agent + MCP）：
   - **03–06b**：禅道接入、快照、plan_bank（部分可脚本同步）
   - **07–14**：分析/改码/构建/烧录/测试/闭环（逐段完善）
6. **门禁** — 本阶段 `required_outputs` 是否满足？
7. **结束本轮**：
   - 通过 → `completed`，推进下一阶段
   - 未通过 → **卡点报告**（四要素），**停止**，等待 `/citfix --resume`

---

## 与 bugfix / cit-prepare 的关系

| 入口 | 范围 |
|------|------|
| **`/citfix`** | **整条** CIT 自动化流水线（00–14），测试床 + 状态机 |
| `/bugfix` | 单 Bug 七步修复（android-bugfix-flow），非 CIT 流水线真源 |
| `cit-prepare` | 流水线子段（03–06），由 citfix 编排调用 |

---

## 建设期约束

- 阶段顺序以 `citfix_pipeline.json` 为准，**不可临时改序**
- 某阶段尚未自动化 → 卡点停住，写清缺什么，**层层填充**
- 底层 `scripts/citfix.py` 仅同步 03–06b 状态，**不能**替代 citfix 流水线编排
- 辅助代码只放 `cit-workflow/tests/citfix/`；产物只放 `cit-workflow-test/`

---

## 关联能力

| 阶段 | 能力 |
|------|------|
| 03–06 | `cit-context-prepare`、`cit-plan-bank`、MCP |
| 07–14 | `bugfix-*`（作为流水线内子能力，非独立入口） |

CIT 源码：`cit-context-prepare/cit_guide.md`
