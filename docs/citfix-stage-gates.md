# citfix 阶段门禁契约（现行真源）

> 版本：2026-09-07  
> **Authority（运行时门禁）**：本仓代码（`citfix/`、`contracts/`、`.cursor/skills/cit*/`）与本文  
> **Authority（业务框架）**：知识库 EXP-CIT-004 **第 2–5 章**（①–⑧ / 泳道 / ⑥ / ⑦）；框架变更先改该文档，再改本仓  
> 分册 EXP-CIT-005～011 / 设计 EXP-CIT-012～018：阶段细节对照；与本文字段冲突时以本文 + 代码为准

## 1. 角色

| 角色 | 职责 |
|------|------|
| Engine | `citfix/stages/` / `engine.py`：文件存在性 + JSON 字段门禁 |
| Agent + cit-* skill | 写正式 `runs/<run_id>/<stage>/output/*.json` |
| Config | `contracts/citfix_pipeline.json`、`plan_bank/<PRODUCT>/project_info.json`、`config/citfix/human_case_registry/` |

## 2. verification_mode

写入 `04_result_normalize/output/tasks.json` → `tasks[].verification`，并镜像到 `06/context.json` → `routing.verify_mode`。

| 值 | 含义 | 13_human_gate 完成条件 |
|----|------|------------------------|
| `auto` | 机器可完整验收 | `gate_status` ∈ `not_required` \| `auto_bypassed` |
| `human` | 必须人工验收 | `gate_status` = `approved` |
| `hybrid` | 自动断言 + 人工断言 | `approved`（且 12 的 auto 断言全 true） |

运行时真源：`config/citfix/human_case_registry/<PRODUCT>.json`（人工确认入库）。  
AI 只能写 `intermediate/human_case_proposal.json`，**不得**直接写终值 `mode`。

高风险关键词未命中 registry 时：分类为 `human`（写入 `classified_*`）。  
**当前 bootstrap**：`verify_runtime_force_auto=true` → 运行时仍按 `auto` 推进；恢复人工门禁时关掉该开关即可。

## 3. 引擎硬门禁（摘要）

| Stage | 关键字段 / 规则 | 失败 |
|-------|-----------------|------|
| 04 | CIT 标题；写入 `verification` | blocked |
| 06 | `gates.ready_for_*`；`routing.verify_mode` ∈ 枚举；`device_label_verified` 写入 source/gates | 07/11/12 入口拦 |
| 07 | `ROOT_CAUSE_FOUND`；同步 `plan_bank` `analysis_conclusion_path`（正式 `runs/<产品>/CIT-*/07_…`）；路径非法则中断 | output_validation / plan_bank_path |
| 08 | `review_status=pass`；`files_changed`；`source_base_sha`；`patch_sha256` | output_validation |
| 09 | `build_mode` 白名单 + 证据 | output_validation |
| 10 | `artifacts[].verified=true` + hash；`deploy_pending` 禁止进 11/12 | output_validation |
| 11 | `device_serial`；**`device_label_verified=true`**；skip 时须 `skip_reason` + 设备核验 | output_validation / device_label |
| 12 | **`device_label_verified=true`**；`VERIFY_PASS` + `auto_assertions` 全 true | output_validation / device_label |
| 13 | `cit-human-gate`；按 mode 的 `gate_status`；`pending_human` **不得** completed | human_gate / blocked |
| 14 | `closure_mode` + commit/push/禅道字段；`CLOSURE_PENDING` 不得当完成；`CLOSURE_DONE` 才 completed；commit 文案**英文** | output_validation |

**Bootstrap 说明**：`gates.verify_runtime_force_auto=true` 时，registry 的 human/manual **分类标记保留在 `classified_*`**，运行时 `routing.verify_mode=auto`（人工接口保留、暂不挡流程）。`push_zentao_close_enforced` 仍为 false。

细节以实现为准：`stages.validate_outputs`、`human_cases.apply_runtime_verify_policy`。

## 4. 人工项筛选

- **决策流程**：AI 建议 → 人确认（辅助判断）
- **运行时真源**：registry 配置（人工注释）
- **禁止**：AI 自动注释直接驱动 13 放行

## 5. 14_closure（cit-submit）

- Skill：`.cursor/skills/cit-submit/SKILL.md`
- **确定性 Worker**：`scripts/cit_closure_run.py`
  - **默认** `local_commit_only`：只 commit、记录 sha；**禁止自动 push / 禅道写回**
  - `--mode full`：**预留** push + 禅道（显式开启）
  - 可选 `--gerrit-score`（仅 full）；**禁止**自动 submit/merge
- Commit message（过渡）：`[Product][BugID|TaskID]<id>[Description]…[Solution]…`
- 干跑：`python scripts/cit_closure_run.py --mode evidence_only`
- 14 通过后副作用：`closure_ops` → 回写 08 + outbox → `cit_outbox_drain` → `closure_report.json`
- Outbox 配置：`config/citfix/outbox_channels.json`；手动投递：`scripts/cit_outbox_drain.py`
- 引擎：`stages._validate_agent_output`（`14_closure`）；14 AGENT_BRIEF 含 Worker 命令
