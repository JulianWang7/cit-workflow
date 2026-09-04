---
name: cit-compile
description: |
  cit-workflow 09_jenkins_build：真实编译或可核验源码证据，写 build_<bug_id>.json。
  触发：/citfix 09、cit-compile、COMPILE_DONE。禁止 bugfix-compile；禁止 build_mode=skip。
---

# cit-compile — CIT 构建（项目 skill）

## 目标

为 10/11/12 提供**可核验**构建证据（真编译或 source_verified sha256）。

## 边界

- MCP：ssh `compile_*` / `run_command` / `read_file`
- 脚本优先：`scripts/compile/cit_compile_router.py` → `cit_compile_run.py`（见 `scripts/compile/README.md`）
- **禁止** `bugfix-compile`；**禁止** `build_mode=skip|none|noop|""`
- 读：`08/change_*.json`（须 `review_status=pass`）、`06/context|workspace`
- **正式产物**：`…/09_jenkins_build/output/build_<bug_id>.json`

## 前置

1. 08 审查通过  
2. `set_workspace` 绑定  
3. 无权限 → CHECKPOINT，勿假 COMPILE_DONE  
4. **禁止改** MeiGLink cit3/cit4 源码；配置改项目 `vendor/.../cit/`

## 模式选择

| build_mode | 何时 | 引擎必填 |
|------------|------|----------|
| `source_verified` | 纯配置/资源已落盘 | `source_server_path` + `source_file_sha256` |
| `compile_module` / `module_incremental` / `gradle` | 需重编模块 | `success/status` + artifact/log |
| `jenkins` | 出整包 | `jenkins` 或 `job_url`/`build_url` |

`context.routing.compile_mode=gradle_or_skip` **只是提示**，不是合法 `build_mode`。

## 工具流程

```text
1. Read change.files_changed → 判配置-only 还是需编译
2. 优先 cit_compile_router.py 生成 compile_plan（建议写入 `runs/<run_id>/09_*/`）
3. cit_compile_run.py 或 MCP compile_module / sha256sum
4. 写 build_*.json（marker=COMPILE_DONE）
5. /citfix --resume
```

### source_verified 示例命令

```text
ssh: sha256sum /home2/.../vendor/meig/apps/cit/etc/cit_common_config.xml
```

## 输出契约

```json
{
  "schema_version": "1.0",
  "bug_id": "<id>",
  "run_id": "<run_id>",
  "marker": "COMPILE_DONE",
  "build_mode": "source_verified",
  "build_scope": "config_only",
  "source_server_path": "...",
  "source_file_sha256": "<hex>",
  "changed_file": "vendor/meig/apps/cit/etc/...",
  "success": true,
  "status": "success",
  "plan_ref": "runs/<PRODUCT>/<run_id>/09_jenkins_build/intermediate/compile_plan.json",
  "built_at": "ISO8601"
}
```

## 失败 / CHECKPOINT

| 情况 | 动作 |
|------|------|
| 编译失败 | 记 log 路径；blocked；可回 08 |
| 无 sha | 禁止 source_verified |
| 想 skip | **拒绝**；改 source_verified 或真编译 |

## 与相邻 skill

上：**cit-review**；下：**cit-artifacts**。
