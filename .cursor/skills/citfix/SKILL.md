---
name: citfix
description: |
  cit-workflow 自动化流水线的 debug 入口（固定斜杠命令 /citfix {bug_id} 或 /citfix project <alias>）。
  不是「调试辅助 Skill」——是整条 CIT 流水线（EXP-CIT-004..011）的唯一运行入口。
  当前处于工作流完善阶段；正式产物写 cit-workflow/runs/<PRODUCT>/<run_id>/（交付真源），中间产物写 runs_work/projects/<PRODUCT>/runs/。
  触发词：/citfix、citfix 97203、citfix project slb783、citfix --resume、CIT 流水线、CIT 自动化。
---

# citfix — CIT 自动化流水线 debug 入口

> **定位**：`cit-workflow` 自动化流水线的**固定 debug 入口**（`/citfix {bug_id}`）。  
> **不是**「调试辅助 Skill」或通用调试工具；**是**整条流水线的唯一运行入口。  
> **现状**：**工作流完善阶段**——阶段定义已冻结于 `contracts/citfix_pipeline.json`，各阶段能力**逐段落地**；未实现段以卡点 + 续跑方式推进，不跳步。  
> **你是流水线编排者**，不是脚本运行器。用户输入 `/citfix 97203` 时，按预定义阶段推进 CIT 工作流；不要只执行 `scripts/citfix.py` 后结束。

## 产物双路径

| 类型 | 根目录 | 用途 |
|------|--------|------|
| **正式产物（交付真源）** | `D:\Workspace\cit-workflow\runs\<run_id>\` | 校验通过的阶段 `output/` JSON、run.json、附件 |
| **中间产物** | `D:\Workspace\cit-workflow\runs_work\projects\<PRODUCT>\runs\<run_id>\` | logs、AGENT_BRIEF、_rejected、workflow_state |

引擎：正式 output → 镜像到中间；Agent 若只写中间，会 promote 到正式。

## 子文档（按需 Read）

| 文档 | 何时读 |
|------|--------|
| [agent-protocol.md](agent-protocol.md) | **每轮必遵** — 阶段推进与卡点格式 |
| [workflow-stages.md](workflow-stages.md) | 当前阶段、产物路径、KB 分册 |
| [cli-integration.md](cli-integration.md) | Cursor CLI 启动或 resume thread |
| [ide-maintenance.md](ide-maintenance.md) | 完善流水线引擎/阶段代码（建设期） |

阶段契约：`contracts/citfix_pipeline.json`  
门禁契约（现行）：`docs/citfix-stage-gates.md`  
设计说明：`docs/citfix-skill-design.md`  
**[LEGACY]** EXP-CIT-004/009 等旧分册不作评审依据。

---

## 固定入口

```text
/citfix 97203
/citfix 97203 --resume
/citfix 97203 --status
/citfix 97203 --new
/citfix 97203 --debug
/citfix 81097 97203
/citfix diff 81097 97203
/citfix project slb783
/citfix project slb783 --device <SN>
/citfix project slb783 --dry-run
/citfix project slb783 --resume
```

| 输入 | 动作 |
|------|------|
| `/citfix 97203` | 接续该 bug 最新 run，或新建 run 从阶段 00 起；自动识别禅道 product |
| `/citfix <id1> <id2>…` | **串行**执行多个 bug 流水线（前一个 blocked/失败则停，不启动后续） |
| `/citfix diff <ids>` 或 `--diff` | **只读**定位正式 run 的 commit / 文件列表 / diff 摘要（必须含 `diff` 字眼） |
| `--resume` | **必须**读 `workflow_state.json` + `CHECKPOINT.md` 后续跑 |
| `--status` | 只读 state，输出阶段表，不改文件 |
| `--new` | 新建 run_id / 新建 project batch |
| `--debug` | **人工调试**：打印 `runs/<PRODUCT>/<run_id>`、中间路径、checkpoint、关键产物 |
| `/citfix project <alias>` | 用禅道 `my_bugs` 拉**当前用户**任务，按产品别名模糊匹配 + CIT 门禁 + **仅 `verify_mode=auto`**，**串行**逐个 `/citfix <id>` |
| `--device <SN>` | 预留：覆盖本 batch 的 `device_serial`（写入 batch_state / workspace） |
| `--dry-run` | 只发现并写 01/02 产物，不跑单 bug 流水线 |

`project` 关键字大小写不敏感；`<alias>` 与禅道产品名做关键词模糊匹配（如 `slb783` → `SLB783 - Android14`）。

正式产物：`runs/<PRODUCT>/<run_id>/`（例：`runs/SLB783 - Android14/CIT-20260903-81097/`）。  
批次产物：正式 `runs/<PRODUCT>/<batch_id>/01_req_parse|02_bug_task_extract/`；中间 `runs_work/projects/<PRODUCT>/batches/<batch_id>/`（含镜像 + `batch_state.json`）。Skill：`cit-req-parse` / `cit-bug-extract`。

状态真源（续跑）：`runs_work/projects/<PRODUCT>/runs/<run_id>/workflow_state.json`（双写到 formal `runs/<PRODUCT>/<run_id>/`）

---

## 每轮流程（流水线推进）

1. **定位 run** — 按 `bug_id` 找最新未完成 run
2. **读状态** — `current_stage`、`workflow_status`、各 stage `status` / `blocker`
3. **读 KB** — 当前阶段 EXP-CIT-* 分册（`kb_doc_path`）
4. **读产物** — 正式 `cit-workflow/runs/<run_id>/<NN>_*/output/`；中间 brief/logs 在 `runs_work/projects/<PRODUCT>/runs/<run_id>/`
5. **执行当前阶段**：
   - **03–06b**：可跑 `citfix.py --resume` 或按协议用手写/MCP
   - **07**：必读中间 `07_analysis/intermediate/AGENT_BRIEF.md` → **`cit-analyze`** → 写**正式** `root_cause_*.json`
   - **08**：必读 brief → **`cit-modify`** 然后 **`cit-review`** → 写正式 `change_*.json`
   - **09+**：按 pipeline `skills` / brief 调度对应 `cit-*`
6. **门禁** — `required_outputs`；07 前 `ready_for_analyze`；11/12 前 `ready_for_reproduce`
7. **结束本轮**：通过则推进；否则 CHECKPOINT（含 skill 路径）后停止

附件落点（06 正式）：`cit-workflow/runs/<PRODUCT>/<run_id>/06_context_snapshot/input/attachments/`  
（按 **run / bug** 落本地仓库，不落编译机；文件名优先用禅道附件 title；`task_ref.json` / `context.json` 的 `local_path` 为仓库相对路径。）

---

## 与 bugfix / cit-prepare 的关系

| 入口 | 范围 |
|------|------|
| **`/citfix`** | **整条** CIT 自动化流水线（00–14），测试床 + 状态机；**仅本仓 skill/MCP** |
| `/bugfix` | 单 Bug 七步（android-bugfix-flow / 用户级 skill），**本仓会话禁止调用** |
| `cit-prepare` | 流水线子段（03–06），由 citfix 编排调用 |

---

## 建设期约束

- 阶段顺序以 `citfix_pipeline.json` 为准，**不可临时改序**
- 某阶段尚未自动化 → 卡点停住，写清缺什么，**层层填充**
- 底层 `scripts/citfix.py` 仅同步 03–06b 状态并校验 agent 产物是否存在，**不能**替代 citfix 编排，也**不会**调用任何 skill
- **禁止**加载 `~/.cursor/skills/bugfix-*`；07+ 使用 `.cursor/skills/cit-analyze` 等

## 阶段 × 项目 Skill

| 阶段 | 项目 skill（唯一允许） |
|------|------------------------|
| 03–06b | `citfix` + `cit-context-prepare` + `cit-plan-bank`（+ `citfix.py` auto） |
| 07 | `cit-analyze` |
| 08 | `cit-modify` → `cit-review` |
| 09 | `cit-compile`（禁止 `build_mode=skip`） |
| 10 | `cit-artifacts`（`verified=true` + sha256） |
| 11 | `cit-flash`（MCP：`qcom_flash` / `device_state`） |
| 12 | `cit-reproduce` → `cit-verify` |
| 13 | `cit-human-gate` |
| 14 | `cit-submit`（优先 `scripts/cit_closure_run.py`） |

Skill 厚度标准与批量预留说明：`docs/citfix-skill-thickness.md`。


辅助代码只放 `cit-workflow/citfix/`；**正式产物**放 `cit-workflow/runs/<run_id>/`；**中间产物**放 `runs_work/projects/<PRODUCT>/runs/<run_id>/`。

---

## 关联能力

| 阶段 | 能力 |
|------|------|
| 03–06 | `cit-context-prepare`、`cit-plan-bank`、项目 MCP |
| 07–14 | 本仓 `cit-analyze` / `cit-modify` / …（**禁止**用户级 `bugfix-*`） |

CIT 源码：`cit-context-prepare/cit_guide.md`
