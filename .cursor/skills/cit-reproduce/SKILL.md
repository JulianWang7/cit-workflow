---
name: cit-reproduce
description: |
  cit-workflow 12 前半：设备侧复现/摸底 CIT 症状。禁止 bugfix-reproduce。
  触发：/citfix 12、cit-reproduce、REPRODUCE_DONE。与 cit-verify 分工：本 skill 抓「修前/修后」设备证据。
---

# cit-reproduce — CIT 复现证据（项目 skill）

> 吸取 bugfix-reproduce：设备在线确认 → 清 logcat → 按类型选工具链 → 结构化证据。  
> **差异**：CIT 以测项/配置/模块类名为中心，不只看 crash log。

## 边界

- 门禁：`06/context.json` → `gates.ready_for_reproduce==true`（需 `device_serial`）
- MCP：仓内 **adb-mcp**（`device_state`、`clear_logcat_buffer`、`adb_shell`、`reproduce`、`ui_*`、`adb_suspend`、`serial_*`）
- **禁止**用户级 `bugfix-reproduce`
- 本 skill **不**单独完成 12；证据交给 `cit-verify` 写入 `result_*.json`
- 阶段明细日志 / run_events：`runs/<PRODUCT>/<run_id>/logs/`（**正式**；勿写 `runs_work/.../<stage>/logs`）

## 与 cit-verify 分工

| skill | 做什么 |
|-------|--------|
| **cit-reproduce** | 确认设备、跑 CIT/ADB 复现步骤、抓 log/截图/配置读数 |
| **cit-verify** | 对照 `verify_mode` 填 `auto_assertions`/`human_assertions`，写正式 `result_*.json` |

## 流程

### 1. 读上下文

1. `06/.../workspace.json` → `device_serial`、`server`
2. `06/.../context.json` → `routing.verify_mode`、`verification`
3. `08/.../change_*.json` / `07/.../root_cause_*.json` → 改了什么、测哪项
4. 禅道 steps（`04/tasks.json` 或 `task_ref`）→ 操作步骤与预期

### 2. 设备预检

```
device_state()
```

- 0 设备 → CHECKPOINT：`REPRODUCE_FAIL: no device`，停住
- 多设备 → 必须用 `workspace.device_serial` 限定后续命令

### 3. 清证据窗口

```
clear_logcat_buffer()
```

### 4. 按 CIT 类型选策略（不要一律裸 input tap）

| CIT/缺陷类型 | 工具链 | 说明 |
|--------------|--------|------|
| 配置阈值/XML（如 MIC loopback 85→88） | `adb_shell` grep/cat 配置路径 + sha256 | 属 auto 可证伪 |
| 系统属性/getprop | `adb_shell` | auto |
| 需进 CIT APK 菜单的 UI 项 | `ui_snapshot` → `ui_tap_text` → `ui_wait_text` | 常 human/hybrid |
| 音频/声压主观 | UI 进项 + **人工声源**；机器只记前置 | human |
| 按键/夹具 | 人工；机器最多记 adb 注入约定 | human |
| Crash/ANR 伴随 | `reproduce(commands, logcat_filter)` | 参考 bugfix |
| 休眠相关 | `adb_suspend` + 必要时 `serial_*` | 同 bugfix 兜底 |

**禁用**：无校验的裸 `adb shell input tap` 坐标点击（UI 项一律 `ui_*`）。

### 5. 配置类摸底示例（81097 类）

```
adb_shell: grep loopbacktest_max /vendor/meig/apps/cit/etc/cit_common_config.xml
adb_shell: sha256sum /vendor/meig/apps/cit/etc/cit_common_config.xml
```

把 stdout 写入正式 `logs/stages/` 下片段文件（或 `12_cit_test/intermediate/reproduce_snippets.txt` 后由引擎/习惯 promote），供 cit-verify 引用。路径优先：

`runs/<PRODUCT>/<run_id>/logs/stages/<run_id>_12_cit_test.log`

### 6. 输出交接

不强制单独 JSON；至少保证：

- 正式 `logs/stages/`（或阶段 intermediate 镜像）有时间戳命令与输出
- 口头/笔记交给 verify：哪些断言已能 auto、哪些必须 human

可选写：

`12_cit_test/intermediate/reproduce_notes_{bug_id}.json`

```json
{
  "schema_version": "1.0",
  "bug_id": "<id>",
  "device_serial": "...",
  "commands_run": [],
  "observations": [],
  "suggested_auto_assertions": [],
  "suggested_human_assertions": [],
  "marker": "REPRODUCE_DONE"
}
```

然后执行 **cit-verify**。
