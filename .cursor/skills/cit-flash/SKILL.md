---
name: cit-flash
description: |
  cit-workflow 11_qfil_flash：整包 QFIL/xPCAT 或 adb_push 部署核验。禁止用户级 bugfix flash 技能名混淆。
  触发：/citfix 11、cit-flash、FLASH_DONE。MCP：qcom_flash / device_state / adb。
---

# cit-flash — CIT 烧录/部署（项目 skill）

> 吸取 bugfix 刷机/verify 部署经验：先定模式 → 执行 → **设备侧核验** → 结构化 JSON。  
> pipeline 旧 `skill_hints: qcom_flash,device_state` 是 MCP 工具名；**本 skill 才是阶段入口**。

## 边界

- MCP：`adb-mcp`（`device_state`、`adb_shell`、`flash`、`qcom_flash*`、`flash_status`）
- 读：`10/artifact_*.json`、`09/build_*.json`、`06/workspace.json`、以及 09 侧 `compile_plan.json`（若有）
- **正式产物**：`…/11_qfil_flash/output/flash_<bug_id>.json`
- 门禁：`ready_for_reproduce`；`deploy_pending=true` 禁止进入

## 模式选择

| 条件 | `flash_mode` | 说明 |
|------|--------------|------|
| `compile_plan.qfil_required=true` 或需整包指纹 | `qfil` / `xpcat` | 调 `qcom_flash`，记 task_id，轮询 `flash_status` |
| 配置/单文件已在 10 adb push | `adb_push` | `qfil_skipped=true` + 设备再核验 |
| 无设备 | — | CHECKPOINT，勿伪造 FLASH |

## 流程（adb_push / 配置类）

1. `device_state` 确认 `device_serial`
2. 对照 10：`artifacts[].remote_device_path` + `device_sha256`
3. 再读设备确认（grep/sha256sum）
4. 写 JSON：

```json
{
  "schema_version": "1.0",
  "bug_id": "<id>",
  "run_id": "<run_id>",
  "flash_mode": "adb_push",
  "qfil_skipped": true,
  "skip_reason": "compile_plan qfil_required=false; config deployed in stage 10",
  "device_serial": "<serial>",
  "patches": [
    {
      "remote_path": "/vendor/meig/apps/cit/etc/cit_common_config.xml",
      "deploy_tool": "adb root/remount/push",
      "sha256": "<hex>"
    }
  ],
  "device_verification": {
    "verified_on_device": true,
    "verified_at": "ISO8601",
    "checks": {"grep_or_sha": "..."}
  },
  "artifact_ref": ".../10_artifacts/output/artifact_<id>.json",
  "flashed_at": "ISO8601"
}
```

引擎要求：`qfil_skipped` 时必须有 `skip_reason` 且 `device_verification.verified_on_device===true`。

## 流程（整包 QFIL）

1. 从 10/制品目录取 firehose/rawprogram 或 plan 指定包
2. `qcom_flash(...)` → `flash_status` 至结束
3. 开机后 `device_state` + Build fingerprint / `getprop` 与期望对账
4. JSON：`qfil_skipped=false`，填 `programmer`/`image_dir`/`task_id`/`fingerprint`

失败 → CHECKPOINT，不要写假 verified。

## 完成后

`/citfix <id> --resume` → 进入 12（cit-reproduce → cit-verify）。
