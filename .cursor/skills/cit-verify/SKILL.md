---
name: cit-verify
description: |
  cit-workflow 12_cit_test：按 verify_mode 写 result_<bug_id>.json。禁止 bugfix-verify。
  触发：/citfix 12、cit-verify、VERIFY_PASS、VERIFY_FAIL。
---

# cit-verify — CIT 验证（项目 skill）

> 吸取 bugfix-verify：推送/读回对账、结构化判定、配置值类与 crash 类分流。  
> **差异**：输出必须满足 citfix 引擎门禁（`auto_assertions` 全 true 才 `VERIFY_PASS`）。

## 边界

- MCP：仓内 adb/ssh（`adb_shell`、`device_state`、必要时 `flash`/`verify`）
- **禁止** `bugfix-verify`
- 读：`06/context.json`（`routing.verify_mode`）、`10/artifact_*.json`、`11/flash_*.json`、reproduce 笔记
- **正式产物**：`D:\Workspace\cit-workflow\runs\<run_id>\12_cit_test\output\result_<bug_id>.json`
- 门禁：`docs/citfix-stage-gates.md`

## 前置

1. `gates.ready_for_reproduce==true`
2. 10 完成且非 `deploy_pending=true`（否则 11/12 入口会 blocked）
3. 11 已完成（整包或 adb_push + `device_verification.verified_on_device`）
4. 建议已跑过 **cit-reproduce**（至少配置读数摸底）

## verify_mode 行为

| `routing.verify_mode` | 12 必须做到 | 允许 |
|-----------------------|-------------|------|
| `auto` | 所有验收都在 `auto_assertions`，且全 `passed=true` | 无 human 断言，或 human 为空 |
| `human` | `auto_assertions` 可只含前置条件（配置已部署等） | `human_assertions.*.passed` 可为 `null` |
| `hybrid` | auto 前置全 true | human 项 `null` 直到 13 approved |

**禁止**：`marker=VERIFY_PASS` 但 auto 里仍有 `passed=null/false`。

## 流程

### 1. 决定断言清单

从 root_cause / change / 禅道预期拆两条：

- **auto**：adb/grep/sha/getprop/文件存在性可判
- **human**：声源、按键、主观听感、夹具、CIT 菜单人工点选结果

### 2. 执行 auto 检查（示例）

配置类：

```
adb_shell: grep -n loopbacktest_max /vendor/meig/apps/cit/etc/cit_common_config.xml
adb_shell: sha256sum <remote_path>   # 与 10.artifacts[].sha256 对账
```

对账规则：

- `expected` 与设备读数一致 → `passed=true`
- 不一致 → `passed=false`，整单 `marker=VERIFY_FAIL`（不要假 PASS）

### 3. 可选：用 adb `verify` 工具（产物推送型）

若 10 未部署干净，可：

```
verify(patches=[{local_path, remote_path}], verify_commands=[...], assertions=...)
```

但最终仍以本 skill 的 **result JSON** 为准（引擎不读 verify 工具文本）。

### 4. 写正式 result JSON

```json
{
  "schema_version": "1.0",
  "bug_id": "<id>",
  "run_id": "<run_id>",
  "marker": "VERIFY_PASS",
  "device_serial": "<serial>",
  "test_mode": "auto|config_plus_human_ui|...",
  "auto_assertions": {
    "config_threshold": {
      "expected": "88",
      "actual": "88",
      "source": "adb shell grep ...",
      "passed": true
    }
  },
  "human_assertions": {
    "ui_mic_88db_pass": {
      "expected": "MIC>=88dB 项通过",
      "passed": null,
      "note": "Requires calibrated sound source"
    }
  },
  "symptom_before": "...",
  "symptom_after": "...",
  "evidence": {
    "reproduce_log": ".../12_cit_test/logs/..."
  },
  "verified_at": "ISO8601"
}
```

### 5. 失败与回退

- 任一 auto `passed=false` → `marker=VERIFY_FAIL` → 阶段 blocked → 回 07/08（额度内）
- 仅 human 未做 → 仍可 `VERIFY_PASS`（auto 全 true）+ 进 13 `pending_human`

### 6. 完成后

`/citfix <id> --resume`

## 与 bugfix-verify 对照（勿照搬）

| bugfix | citfix |
|---------|--------|
| `symptom_gone: bool` | 多断言 + `verify_mode` |
| 单一 VERIFY_PASS/FAIL marker 文本 | 正式 `result_*.json` 字段门禁 |
| 可自由叙述 | 引擎拒绝 null 冒充 auto PASS |
