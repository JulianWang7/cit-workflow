# citfix 阶段对照表

与 `workflow/citfix_pipeline.json` 及 EXP-CIT-004～011 对齐。  
测试床根目录：`D:\Workspace\cit-workflow-test`

| Stage ID | 目录 | KB | Executor | 主要 output |
|----------|------|-----|----------|-------------|
| 00_run_registry | `00_runs/<run_id>/` | EXP-CIT-004 | auto | `workflow_state.json`, `run.json` |
| 01_req_parse | `01_req_parse/` | EXP-CIT-005 | skip（/citfix 直连） | — |
| 02_bug_task_extract | `02_bug_task_extract/` | EXP-CIT-005 | skip | — |
| 03_zentao_fetch | `03_zentao_fetch/` | EXP-CIT-005 | auto | `output/zentao_fetch_results.json` |
| 04_result_normalize | `04_result_normalize/` | EXP-CIT-005 | auto | `output/tasks.json` |
| 05_cursor_handoff | `05_cursor_handoff/` | EXP-CIT-005 | auto | `output/cursor_handoff_bundle.json` |
| 06_context_snapshot | `06_context_snapshot/` | EXP-CIT-005 | auto+agent | `output/{task_ref,workspace,context}.json` |
| 06b_plan_bank | `plan_bank/<PROJECT>/` | EXP-CIT-005 | auto | `project_info.json`, `plan_*/plans.json` |
| 07_analysis | `07_analysis/` | EXP-CIT-006 | **agent** | `output/root_cause_{bug_id}.json` |
| 08_changes | `08_changes/` | EXP-CIT-006 | **agent** | `output/change_{bug_id}.json` |
| 09_jenkins_build | `09_jenkins_build/` | EXP-CIT-007 | **agent** | `output/build_{bug_id}.json` |
| 10_artifacts | `10_artifacts/` | EXP-CIT-007 | **agent** | `output/artifact_{bug_id}.json` |
| 11_qfil_flash | `11_qfil_flash/` | EXP-CIT-008 | **agent** | `output/flash_{bug_id}.json` |
| 12_cit_test | `12_cit_test/` | EXP-CIT-009 | **agent** | `output/result_{bug_id}.json` |
| 13_human_gate | `13_human_gate/` | EXP-CIT-009 | **agent** | `output/human_gate_{bug_id}.json` |
| 14_closure | `14_closure/` | EXP-CIT-011 | **agent** | `output/closure_{bug_id}.json` |

## KB 文档路径

根目录：

```text
D:\Workspace\GDocuments\embedded-workflow-lab\knowledge-base\topics\cit\cit-automation-pipeline\
```

文件名模式：`EXP-CIT-0XX-*.md`（见 pipeline.json 的 `kb_file` 字段）。

## Agent 段 output JSON 最低字段

### root_cause_{bug_id}.json

```json
{
  "schema_version": "1.0",
  "bug_id": "97203",
  "run_id": "...",
  "root_cause": "...",
  "evidence_paths": [],
  "recommended_fix": "...",
  "analyzed_at": "ISO8601"
}
```

### change_{bug_id}.json

```json
{
  "schema_version": "1.0",
  "bug_id": "97203",
  "files_changed": [],
  "patch_summary": "",
  "review_status": "pending|pass|fail"
}
```

其他阶段占位结构见各阶段测试床已有样例（如 `closure_97203.json`）。

## plan_bank（06b）

```text
cit-workflow/plan_bank/<PROJECT>/
  project_info.json
  plan_<YYYYMMDD>/plans.json
```

英文字段；`analysis_conclusion_path` 在分析完成后回写 `plans.json`。
