# citfix 阶段对照表

与 `contracts/citfix_pipeline.json` 及 **现行门禁契约** `docs/citfix-stage-gates.md` 对齐。

> **[LEGACY-DESIGN 2026-08-29，非现行契约]** 下表若仍提到 EXP-CIT-*，仅作历史索引；评审与改码以代码 + `citfix-stage-gates.md` 为准。

| 类型 | 根目录 |
|------|--------|
| **正式产物** | `D:\Workspace\cit-workflow\runs\<PRODUCT>\<run_id>\`（含 `output/`、`logs/`、`watch/`） |
| **中间产物** | `D:\Workspace\cit-workflow\runs_work\projects\<PRODUCT>\runs\<run_id>\`（brief/checkpoint/镜像；非日志真源） |

| Stage ID | 正式目录 | 契约 | Executor | 主要 output |
|----------|----------|------|----------|-------------|
| 00_run_registry | `cit-workflow/runs/<PRODUCT>/<run_id>/` | stage-gates | auto | `workflow_state.json`, `run.json` |
| 01_req_parse | `runs/<PRODUCT>/<batch_id>/01_req_parse/`（单 bug skip） | stage-gates | **auto** + cit-req-parse | `candidate_rows.json`（`CANDIDATE_ROWS_READY`） |
| 02_bug_task_extract | `…/02_bug_task_extract/`；中间另有 `batches/<id>/` | stage-gates | **auto** + cit-bug-extract | `bug_ids.json`（`BUG_IDS_READY`，串行队列） |
| 03_zentao_fetch | `…/03_zentao_fetch/` | stage-gates | auto | `output/zentao_fetch_results.json` |
| 04_result_normalize | `…/04_result_normalize/` | stage-gates | auto | `output/tasks.json`（含 `verification`） |
| 05_cursor_handoff | `…/05_cursor_handoff/` | stage-gates | auto | `output/cursor_handoff_bundle.json` |
| 06_context_snapshot | `…/06_context_snapshot/` | stage-gates | auto | `context.json`（`routing.verify_mode`） |
| 06b_plan_bank | `plan_bank/<PRODUCT>/` | stage-gates | auto | `project_info.json`, `index.json`, `problems/<id>.json` |
| 07_analysis | `…/07_analysis/` | stage-gates | **agent** + cit-analyze | `root_cause_{bug_id}.json` |
| 08_changes | `…/08_changes/` | stage-gates | **agent** + cit-modify → cit-review | `change_{bug_id}.json` |
| 09_jenkins_build | `…/09_jenkins_build/` | stage-gates | **agent** + cit-compile | `build_{bug_id}.json` |
| 10_artifacts | `…/10_artifacts/` | stage-gates | **agent** + cit-artifacts | `artifact_{bug_id}.json` |
| 11_qfil_flash | `…/11_qfil_flash/` | stage-gates | **agent** + cit-flash | `flash_{bug_id}.json` |
| 12_cit_test | `…/12_cit_test/` | stage-gates | **agent** + cit-reproduce → cit-verify | `result_{bug_id}.json` |
| 13_human_gate | `…/13_human_gate/` | stage-gates | **agent** + cit-human-gate | `human_gate_{bug_id}.json` |
| 14_closure | `…/14_closure/` | stage-gates | **agent** + cit-submit | `closure_{bug_id}.json`（`CLOSURE_DONE`） |

## 13_human_gate 完成语义

| `routing.verify_mode` | 允许的 `gate_status` |
|-----------------------|----------------------|
| `auto` | `not_required` \| `auto_bypassed` |
| `human` / `hybrid` | `approved`（须 `approved_by`） |
| 任意 | `pending_human` → **blocked**，不得进 14 |

人工项 registry：`config/citfix/human_case_registry/<PRODUCT>.json`（运行时真源）。  
AI 提案：`04/.../intermediate/human_case_proposal.json` → `scripts/cit_human_case_approve.py`。

## Agent 段字段

详见 `docs/citfix-stage-gates.md`；引擎强制：`citfix/stages/` → `_validate_agent_output`。
