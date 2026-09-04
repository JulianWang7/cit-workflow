---
name: cit-artifacts
description: |
  cit-workflow 10_artifacts：登记可核验制品并部署取证，写 artifact_<bug_id>.json。
  触发：/citfix 10、cit-artifacts、ARTIFACT_READY。禁止 verified=false 纸面通过。
---

# cit-artifacts — CIT 制品登记与部署取证（项目 skill）

## 目标

把 09 产物变成带 hash 的 `artifacts[]`，必要时 adb 推到设备并对账。

## 边界

- MCP：ssh（hash）、adb（`flash`/`adb_shell` push 校验）
- **禁止** bugfix-*；**禁止** `verified:false` 过门禁
- 输入：`09/build_*.json`（已引擎校验）
- **正式产物**：`…/10_artifacts/output/artifact_<bug_id>.json`

## 前置

1. 09 `COMPILE_DONE` + 合法 `build_mode`  
2. 要上设备：`device_serial` 已知；否则 `deploy_pending=true` 但本地/服务器 hash 仍须 `verified=true`

## 工具流程

```text
1. 从 build JSON 取 source_server_path 或 artifact_path
2. sha256sum（服务器或拉取后本地）
3. 需运行时生效 → adb root/remount/push → device_sha256 对账
4. 写 artifact JSON（marker 建议 ARTIFACT_READY）
5. /citfix --resume
```

路径参考（bugfix 习惯，CIT 配置常见）：

| 产物 | 设备路径示例 |
|------|----------------|
| cit_common_config.xml | `/vendor/meig/apps/cit/etc/cit_common_config.xml` |
| CIT apk | plan/`cit_prebuilt_apk` 或项目约定 |

## 输出契约（引擎强制）

- `artifacts` 非空数组  
- 每项 `verified===true`  
- 每项至少 `sha256` \| `device_sha256` \| `local_sha256` 之一  

```json
{
  "schema_version": "1.0",
  "bug_id": "<id>",
  "run_id": "<run_id>",
  "marker": "ARTIFACT_READY",
  "artifact_mode": "adb_push",
  "build_ref": ".../build_<id>.json",
  "deploy_pending": false,
  "device_serial": "...",
  "artifacts": [
    {
      "name": "cit_common_config.xml",
      "source_server_path": "...",
      "remote_device_path": "/vendor/meig/apps/cit/etc/cit_common_config.xml",
      "sha256": "<hex>",
      "device_sha256": "<hex>",
      "verified": true
    }
  ],
  "registered_at": "ISO8601"
}
```

## 与 11 的关系

- `deploy_pending=true` → 引擎 **禁止**进 11/12  
- 配置已 adb 核验 → 11 走 `cit-flash` 的 `adb_push`/`qfil_skipped` 路径，**禁止**假 09/10  

## 失败 / CHECKPOINT

| 情况 | 动作 |
|------|------|
| hash 不一致 | verified 不得 true |
| 无设备又需部署 | deploy_pending=true 或停等设备 |

## 与相邻 skill

上：**cit-compile**；下：**cit-flash**。
