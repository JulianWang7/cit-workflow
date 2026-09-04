---
name: cit-submit
description: |
  cit-workflow 14_closure：默认仅 SSH git commit 并记录 sha；Gerrit push / 禅道写回接口预留、默认禁用。
  触发：/citfix 进入 14、cit-submit、CLOSURE_DONE。禁止 bugfix-submit。
  禁止纸面 CLOSURE_PENDING / 无 commit 的假完成。禁止默认自动 push。
---

# cit-submit — CIT 闭环提交（项目 skill）

## 推荐入口（确定性 Worker）

优先用脚本（与 MCP 同栈），再 `--resume` 让引擎校验：

```powershell
cd D:\Workspace\cit-workflow
# 默认：仅本地 commit，记录 sha；人工再 push（推荐）
.\.venv\Scripts\python.exe scripts\cit_closure_run.py --run-dir D:\Workspace\cit-workflow\runs\<PRODUCT>\<run_id> --bug-id <id>

# 探测（无远端副作用）
.\.venv\Scripts\python.exe scripts\cit_closure_run.py --run-dir ... --bug-id <id> --mode evidence_only

# 预留（默认禁用）：Gerrit push + 禅道写回 — 仅显式 --mode full
.\.venv\Scripts\python.exe scripts\cit_closure_run.py --run-dir ... --bug-id <id> --mode full
```

### Commit message（过渡格式，必须）

由 worker 自动生成，格式固定为：

```text
[Product][BugID|TaskID]<id>[Description]<根因简述>[Solution]<修改简述>
```

字段均为**英文简述**（产品源码树 commit message 用英文）。示例：

```text
[SLB783 - Android14][BugID]81097[Description]MIC loopback threshold low[Solution]common threshold 85 to 88
```

| 段 | 含义 |
|----|------|
| `[Product]` | 禅道产品名，如 `SLB783 - Android14` |
| `[BugID]` / `[TaskID]` | 标识类型（`--id-kind`，默认 `BugID`） |
| `<id>` | 禅道编号，紧跟类型标签、无空格 |
| `[Description]…` / `[Solution]…` | **English** short text；总长建议 ≤120 字符（worker 会软截断） |

- **不要**手写 Change-Id（commit-msg hook 自动生成）
- Push / 禅道关闭：默认 `local_commit_only`（不 push、不关单）。后续门禁预留：`contracts/citfix_pipeline.json` → `gates.push_zentao_close_enforced`（当前 `false`）

14 校验通过后引擎：回写 `08` 的 `committed`/`pushed`，**upsert** outbox（同 dedupe_key 覆盖旧 SHA），投递 `notifications/`，写 `closure_report.json`。

若 Worker 再次提交产生新 SHA，执行：

```powershell
.\.venv\Scripts\python.exe scripts\cit_closure_resync.py --run-dir runs\<PRODUCT>\<run_id> --bug-id <id>
```

手动补投递：

```powershell
.\.venv\Scripts\python.exe scripts\cit_outbox_drain.py --run-dir D:\Workspace\cit-workflow\runs\<PRODUCT>\<run_id> --force
```

渠道开关：`config/citfix/outbox_channels.json`（默认仅 file；webhook/wecom 需显式 enabled）。

## 边界

- MCP：仓内 ssh `git_op`；禅道 `zentao_update_bug`（**默认不调用**）；可选 Gerrit 查询
- **禁止** `bugfix-submit` / 本机直接改编译机仓库以外的「假提交」
- **禁止**默认 `git push`；push 留给人工；`--mode full` 为预留接口
- 读：`06/.../context.json`、`08/.../change_*.json`、`12/.../result_*.json`、`13/.../human_gate_*.json`、`AGENT_BRIEF.md`
- 产物（正式）：`D:\Workspace\cit-workflow\runs\<PRODUCT>\<run_id>\14_closure\output\closure_<bug_id>.json`
- 中间：`runs_work/projects/<PRODUCT>/runs/<run_id>/14_closure/`
- 门禁契约：`docs/citfix-stage-gates.md`；字段由引擎 `_validate_agent_output` 强制
- 引擎强制：`marker=CLOSURE_DONE` + 上游 12/13 + 本 skill 必填字段

## 前置（缺一则 CHECKPOINT，勿写 CLOSURE_DONE）

1. `12`：`marker=VERIFY_PASS`，且 `auto_assertions` 全 true  
2. `13`：按 `context.routing.verify_mode`  
   - `auto` → `gate_status` ∈ `not_required|auto_bypassed`  
   - `human|hybrid` → `gate_status=approved` 且有 `approved_by`  
3. `08`：`review_status=pass`，`files_changed` 非空，`committed/pushed` 可为 false（本阶段补 `committed`）  
4. SSH：`set_workspace(server, code_root)` 与 `change_*.json` / `workspace.json` 一致  

## 允许的 `closure_mode`

| mode | 含义 | 必填 |
|------|------|------|
| `local_commit_only` | **默认**：只 commit、记录 sha；`push_skipped` + `zentao_skipped` | `commit`、`push_skipped=true`、`skip_reason` |
| `full` | **预留**：本地 commit + Gerrit push + 禅道写回（须显式 `--mode full`） | `commit`、`pushed=true`、`zentao_updated=true` |
| `evidence_only` | 探测/门禁演练：不改远端（需理由） | `push_skipped`+`zentao_skipped`+`skip_reason`；**仍须**有真实 `commit` 或显式 `commit_skipped=true` |

生产默认用 `local_commit_only`。后续再完善自动提交步骤时再启用 `full`。

## 执行步骤（默认 local_commit_only）

1. **对齐工作区**  
   `set_workspace(server=<workspace.server>, code_root=<workspace.code_root>)`  
   `git_op(action="status")` / `diff` — 确认改动 ⊆ `08.files_changed`（无无关脏文件）

2. **本地提交（必须）**  
   Message 用上文过渡格式（`cit_closure_run.py` 自动生成；可用 `--product` / `--id-kind` / `--message-desc` / `--message-solution` 覆盖）。

3. **记录 commit（必须）**  
   写入 `closure_*.json`：`commit`（sha）、`message`、`change_id`（若 hook 已生成）、`pushed=false`、`push_skipped=true`。  
   **不要** `git_op(action="push")`。

4. **Push / 禅道（预留，默认跳过）**  
   代码路径仍在 worker 的 `--mode full` 中：`git_mod.push` + `zentao.update_bug`。  
   当前阶段：人工在编译机上自行 push；禅道评论可人工补或以后再开。

5. **回写 08（建议）**  
   更新同 run 的 `change_*.json`：`committed=true`、`pushed=false`（默认）

6. **写 closure JSON**（见下方模板）→ `/citfix <id> --resume`

## 禁止

- `marker: CLOSURE_PENDING` 当作完成  
- 无 SSH commit 却填假 sha  
- 默认路径下自动 push / 自动禅道写回  
- 跳过 13 未批准的 human/hybrid 强行闭环  

## 最低 JSON 示例（默认 local_commit_only）

```json
{
  "schema_version": "1.0",
  "bug_id": "81097",
  "run_id": "CIT-…",
  "marker": "CLOSURE_DONE",
  "closure_mode": "local_commit_only",
  "message": "[SLB783 - Android14][BugID]81097[Description]MIC回环阈值偏低[Solution]阈值85改88",
  "server": "110",
  "code_root": "/home2/.../LA.UM.12.2.1/",
  "local_branch": "SLB783_wanglingqi",
  "upstream": "origin/master_SLB783",
  "files_changed": ["vendor/meig/apps/cit/etc/cit_common_config.xml"],
  "commit": "<40-char-sha>",
  "change_id": "I…",
  "pushed": false,
  "push_skipped": true,
  "zentao_skipped": true,
  "zentao_updated": false,
  "skip_reason": "default_no_push: human push; gerrit/zentao interfaces reserved",
  "human_gate_ref": "…/13_human_gate/output/human_gate_81097.json",
  "human_gate_status": "approved",
  "verify_ref": "…/12_cit_test/output/result_81097.json",
  "change_ref": "…/08_changes/output/change_81097.json",
  "closed_at": "ISO8601"
}
```

## evidence_only 示例（探测用）

```json
{
  "marker": "CLOSURE_DONE",
  "closure_mode": "evidence_only",
  "commit_skipped": true,
  "push_skipped": true,
  "zentao_skipped": true,
  "skip_reason": "stage14 dry-run; no remote side effects",
  "pushed": false,
  "zentao_updated": false,
  "commit": null,
  "message": "evidence_only dry-run"
}
```

## 失败 / CHECKPOINT

| 情况 | 动作 |
|------|------|
| 13 未 approved（human/hybrid） | 禁止 CLOSURE_DONE |
| commit message 不符过渡格式 | worker/MCP 拒绝；改用 `cit_closure_run` 生成 |
| 误 push | 勿写 pushed=true（默认模式）；blocked |
| 仅想探测 | `--mode evidence_only` |
| 想自动 Gerrit submit | **拒绝**（保持人工/CI） |
| 想启用预留 push+禅道 | 显式 `--mode full`（后续再完善） |

## 与相邻 skill

上：**cit-human-gate**；副作用：outbox / `cit_outbox_drain.py`。
